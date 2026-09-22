"""Run/debug ASRS tote cycles and compare slotting layouts on identical store/day demand."""

import argparse
from concurrent.futures import ProcessPoolExecutor, wait, FIRST_COMPLETED
from contextlib import ExitStack
from datetime import date, datetime
import hashlib
import importlib.metadata
import json
import multiprocessing
import os
from pathlib import Path
import platform
from queue import Empty
import sys
import tempfile
from time import perf_counter

from tqdm import tqdm

from amr_simulation.inputs import load_workload

from .engine import Schedule
from .inputs import load_config, load_layout, load_map, number
from .reporting import generate_report, save_run, write_json

REQUIRED_FILES = ('summary.json', 'tasks.csv', 'tote_jobs.csv', 'cranes.csv', 'workstations.csv', 'allocation.csv')
_SHARED = None


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def code_fingerprint():
    root = Path(__file__).resolve().parent.parent
    return fingerprint({str(p.relative_to(root)): file_hash(p)
                        for package in ('asrs_simulation', 'amr_simulation', 'warehouse_layout')
                        for p in sorted((root/package).glob('*.py'))})


def layout_name(path):
    return Path(path).name.removesuffix('.slotting.json')


def checkpoint_key(batch, name, day):
    return fingerprint({'batch': batch, 'layout': name, 'date': day})


def read_checkpoint(directory, expected):
    try:
        record = json.loads((directory/'checkpoint.json').read_text())
        if record['fingerprint'] != expected:
            return None
        if any(file_hash(directory/name) != record['files'][name] for name in REQUIRED_FILES):
            return None
        summary = json.loads((directory/'summary.json').read_text())
        return summary if summary['success_flag'] and summary['status'] == 'completed' else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def check_resume(manifest_path, expected, resume):
    if manifest_path.exists():
        if not resume:
            raise ValueError('output already contains a batch; use --resume or a new --output')
        if json.loads(manifest_path.read_text()).get('fingerprint') != expected:
            raise ValueError('resume fingerprint mismatch: input, dates, configuration or code changed; use a new --output')
    elif resume:
        raise ValueError('--resume requires an existing matching manifest')
    elif manifest_path.parent.exists() and any(manifest_path.parent.iterdir()):
        raise ValueError('output directory is not empty; use a new --output')


def run_day(shared, name, day, output, progress=None):
    started = perf_counter()
    directory = Path(output)/name/day
    directory.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f'.{day}-', dir=directory.parent) as scratch:
        staged = Path(scratch)/'day'
        staged.mkdir()
        try:
            if progress is not None:
                progress(0, 0.0, 'scheduling')
            schedule = Schedule(shared['config'], shared['cranes'], shared['layouts'][name]['totes'], shared['days'][day])
            reason = None
            if shared['headless']:
                schedule.run(shared['limit'],
                             (lambda lines, seconds: progress(lines, seconds, 'simulating')) if progress else None)
            else:
                from .debugger import play
                reason = play(schedule, shared['map'], shared['layouts'][name]['totes'], shared['limit'], name)
            if progress is not None:
                progress(schedule.completed_lines(), schedule.now, 'exporting')
            summary = save_run(staged, schedule, perf_counter()-started, reason)
        except Exception as exc:
            demand = sum(t.source_lines for t in shared['days'][day])
            summary = {'method': 'asrs', 'date': day, 'status': 'failed', 'success_flag': False,
                       'failure_reason': f'{type(exc).__name__}: {exc}', 'source_lines': demand,
                       'completed_lines': 0, 'unfinished_lines': demand, 'sim_duration_s': 0.0,
                       'line_throughput_per_hour': None}
        summary.update(layout=name, slotting_strategy=shared['layouts'][name]['strategy'],
                       run_wall_clock_seconds=perf_counter()-started)
        write_json(staged/'summary.json', summary)
        if summary['success_flag']:
            write_json(staged/'checkpoint.json', {
                'fingerprint': checkpoint_key(shared['fingerprint'], name, day),
                'files': {f: file_hash(staged/f) for f in REQUIRED_FILES}})
        backup = Path(scratch)/'previous'
        if directory.exists():
            directory.replace(backup)
        try:
            staged.replace(directory)
        except BaseException:
            if backup.exists():
                backup.replace(directory)
            raise
    return summary


def init_worker(shared):
    global _SHARED
    _SHARED = shared


def worker(name, day, output):
    return run_day(_SHARED, name, day, output,
                   lambda lines, seconds, stage: _SHARED['progress_queue'].put((name, day, lines, seconds, stage)))


def execute_batch(shared, output, workers, resume):
    results, cached_runs, pending = [], {}, []
    for day in sorted(shared['days']):
        for name in shared['layouts']:
            cached = read_checkpoint(output/name/day, checkpoint_key(shared['fingerprint'], name, day)) if resume else None
            if cached is not None:
                cached_runs[name, day] = cached
            else:
                pending.append((name, day))
    if not shared['headless']:
        return list(cached_runs.values())+[run_day(shared, name, day, output) for name, day in pending]
    workers = min(workers, len(shared['layouts']), len(pending))
    with ExitStack() as stack:
        pool, progress_queue = None, None
        if workers > 1:
            manager = stack.enter_context(multiprocessing.Manager())
            progress_queue = manager.Queue()
            pool = stack.enter_context(ProcessPoolExecutor(
                max_workers=workers, mp_context=multiprocessing.get_context('spawn'),
                initializer=init_worker, initargs=({**shared, 'progress_queue': progress_queue},)))
        # As in current_heat, finish every layout for a date before starting another date.
        for day in sorted(shared['days']):
            total = sum(t.source_lines for t in shared['days'][day])
            bars = {name: tqdm(total=total, desc=f'{day} {name.removesuffix("_slotting_layout")}',
                               unit=' lines', position=index, dynamic_ncols=True, mininterval=.2, leave=True,
                               bar_format='{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} [{elapsed}{postfix}]')
                    for index, name in enumerate(shared['layouts'])}
            finished = set()

            def update(name, event_day, lines, seconds, stage):
                if event_day != day or name in finished:
                    return
                bar = bars[name]
                bar.set_postfix_str(f'{stage}, sim {seconds:,.0f}s', refresh=False)
                bar.update(max(0, lines-bar.n))

            def drain_progress():
                if progress_queue is not None:
                    while True:
                        try:
                            item = progress_queue.get_nowait()
                        except Empty:
                            break
                        update(*item)

            def collect(result, resumed=False):
                results.append(result)
                name = result['layout']
                bars[name].update(result['completed_lines']-bars[name].n)
                update(name, day, result['completed_lines'], result['sim_duration_s'],
                       'resumed' if resumed else result['status'])
                finished.add(name)  # Ignore any queued progress older than the final result.
                bars[name].refresh()

            try:
                for name in bars:
                    bars[name].set_postfix_str('queued', refresh=False)
                    if (name, day) in cached_runs:
                        collect(cached_runs[name, day], resumed=True)
                names = [name for name in bars if name not in finished]
                if pool is None:
                    for name in names:
                        collect(run_day(shared, name, day, output,
                                        lambda lines, seconds, stage: update(name, day, lines, seconds, stage)))
                else:
                    futures = {pool.submit(worker, name, day, output) for name in names}
                    while futures:
                        done, futures = wait(futures, timeout=.1, return_when=FIRST_COMPLETED)
                        drain_progress()
                        for future in done:
                            collect(future.result())
            finally:
                drain_progress()
                for bar in reversed(list(bars.values())):
                    bar.close()
                # tqdm leaves the cursor below position zero; retain the other layout rows too.
                if len(bars) > 1:
                    print('\n'*(len(bars)-1), end='', file=sys.stderr, flush=True)
    return sorted(results, key=lambda r: (r['date'], r['layout']))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path(__file__).with_name('asrs.yaml'))
    parser.add_argument('--grid', '--map', dest='grid', type=Path)
    parser.add_argument('--orders', type=Path)
    parser.add_argument('--layout', type=Path, action='append', help='repeat for each slotting layout; headless runs every selected layout on every selected date')
    parser.add_argument('--date', type=date.fromisoformat, action='append', help='select a date (YYYY-MM-DD); repeat for separate dates')
    parser.add_argument('--start-date', type=date.fromisoformat)
    parser.add_argument('--end-date', type=date.fromisoformat)
    parser.add_argument('--headless', action='store_true', help='fast batch simulation; all observed dates and configured layouts by default')
    parser.add_argument('--max-seconds', type=float)
    parser.add_argument('--workers', type=int)
    parser.add_argument('--baseline-layout')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.date and (args.start_date or args.end_date):
            raise ValueError('use --date or a date range, not both')
        if args.start_date and args.end_date and args.start_date > args.end_date:
            raise ValueError('--start-date must be <= --end-date')
        if args.resume and not args.output:
            raise ValueError('--resume requires --output')
        if not args.headless and ((args.start_date or args.end_date) or len(set(args.date or [])) > 1 or len(args.layout or []) > 1):
            raise ValueError('debugging requires one layout and one date; use --headless for comparisons/ranges')
        cfg_path = args.config.resolve()
        cfg = load_config(cfg_path)
        limit = number(args.max_seconds if args.max_seconds is not None else cfg['simulation']['max_seconds'], 'max_seconds', True)
        workers = args.workers if args.workers is not None else cfg['simulation']['workers']
        if workers < 1:
            raise ValueError('--workers must be positive')
        def resolve(value):
            path = Path(value).expanduser()
            return path.resolve() if path.is_absolute() else (cfg_path.parent/path).resolve()
        grid = args.grid.resolve() if args.grid else resolve(cfg['map_file'])
        orders = args.orders.resolve() if args.orders else resolve(cfg['orders_file'])
        paths = [p.resolve() for p in args.layout] if args.layout else [resolve(p) for p in cfg['layout_files']]
        if not args.headless:
            paths = paths[:1]
        names = [layout_name(p) for p in paths]
        if len(set(names)) != len(names):
            raise ValueError('layout filenames must produce unique names')
        baseline = layout_name(args.baseline_layout) if args.baseline_layout else cfg.get('baseline_layout')
        if baseline not in names:
            if args.baseline_layout:
                raise ValueError('--baseline-layout must identify a supplied layout')
            baseline = names[0]
        map_data, cranes, geometry, racks = load_map(grid, cfg)
        print('Loading shared store/day workload...', flush=True)
        workload = load_workload(orders, cache_dir=Path(__file__).parent/'.cache')
        if args.date:
            requested = set(args.date)
            observed = {t.task_date for t in workload.tasks}
            if requested-observed:
                raise ValueError(f'no orders for requested dates: {sorted(requested-observed)}')
            tasks = [t for t in workload.tasks if t.task_date in requested]
        elif args.headless:
            tasks = workload.select(args.start_date, args.end_date)
        else:
            tasks = workload.select(workload.min_date, workload.min_date)
        if not tasks:
            raise ValueError('no store/day demand in selected dates')
        days = {}
        for task in tasks:
            days.setdefault(task.task_date.isoformat(), []).append(task)
        required = {sku for task in tasks for sku in task.line_counts}
        layouts = {name: load_layout(p, cranes, geometry, racks, required) for name, p in zip(names, paths)}
        identity = {'schema': 'asrs_batch/v1', 'code': code_fingerprint(),
                    'versions': {'python': platform.python_version(), **{p: importlib.metadata.version(p)
                                 for p in ('numpy', 'openpyxl', 'PyYAML', 'matplotlib', 'pygame', 'tqdm')}},
                    'inputs': {str(p): file_hash(p) for p in [grid, orders, *paths]},
                    'config': cfg, 'dates': sorted(days), 'layouts': names, 'baseline_layout': baseline,
                    'max_seconds': limit, 'headless': args.headless}
        batch_id = fingerprint(identity)
        output = (args.output or Path(__file__).parent/'results'/datetime.now().strftime('%Y%m%dT%H%M%S_%f')).resolve()
        manifest = output/'manifest.json'
        check_resume(manifest, batch_id, args.resume)
        for name, layout in layouts.items():
            write_json(output/name/'validation_report.json', layout['validation'])
        if any(not l['validation']['valid'] for l in layouts.values()):
            raise ValueError(f'preflight failed; see validation reports under {output}')
        write_json(manifest, {'fingerprint': batch_id, 'identity': identity})
        write_json(output/'config_snapshot.json', {'config': cfg, 'max_seconds': limit, 'baseline_layout': baseline})
        write_json(output/'workload_snapshot.json', {
            'orders_file': str(orders), 'release_rule': 'all store/day groups at t=0',
            'days': {day: [{'task_id': t.task_id, 'store_id': t.store_id, 'line_counts': t.line_counts,
                           'source_lines': t.source_lines, 'release_seconds': 0.0} for t in daily]
                     for day, daily in sorted(days.items())}})
        shared = {'fingerprint': batch_id, 'config': cfg, 'cranes': cranes, 'layouts': layouts,
                  'days': days, 'map': map_data, 'limit': limit, 'headless': args.headless}
        print(f'ASRS: {len(cranes)} cranes, {len(layouts)} layouts × {len(days)} days', flush=True)
        results = execute_batch(shared, output, min(workers, os.cpu_count() or 1) if args.headless else 1, args.resume)
        generate_report(output, results, layouts, sorted(days), baseline)
        print(f'Report: {output/"comparison_report.md"}', flush=True)
        return 0 if all(r['success_flag'] for r in results) else 1
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print('Interrupted. Completed day checkpoints can be resumed.', file=sys.stderr)
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
