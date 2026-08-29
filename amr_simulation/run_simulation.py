"""Command-line entry point for batch comparison and debug playback."""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from warehouse_layout.rmf import RmfMapService
from warehouse_layout.slotting_repository import SlottingLayoutRepository

from amr_simulation.debugger import run_debugger
from amr_simulation.engine import simulate_day
from amr_simulation.inputs import (
    assign_workstations,
    load_workload,
    racks_from_layout,
    validate_inputs,
)
from amr_simulation.models import SimulationConfig
from amr_simulation.native_backend import load_native
from amr_simulation.native_day_backend import load_day_engine
from amr_simulation.results import export_layout_results, write_csv, write_json
from amr_simulation.routing import GridRouter


ROOT = Path(__file__).resolve().parent


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc


def _layout_name(path: Path) -> str:
    name = path.name.removesuffix(".slotting.json")
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("._") or "layout"


def _duration(seconds: float) -> str:
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(int(minutes), 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:05.2f}"


def _simulate_one(
    name, index, project, racks, tasks, mapping, config, trace,
    coordination_backend, simulation_backend,
):
    started = time.monotonic()
    result = simulate_day(
        project, GridRouter(project), racks, tasks, mapping, config, trace=trace,
        coordination_backend=coordination_backend,
        simulation_backend=simulation_backend,
    )
    return name, index, result, time.monotonic() - started


def _show_progress(
    progress: dict[str, int], total: int, started: float, *, redraw: bool,
    changed: str | None = None,
) -> None:
    width = 30
    elapsed = time.monotonic() - started
    label_width = max(map(len, progress))
    lines = []
    for name, done in progress.items():
        filled = width * done // total
        lines.append(
            f"  {name:<{label_width}}  [{'█' * filled}{'░' * (width - filled)}] "
            f"{done:>{len(str(total))}}/{total}  {done / total:6.1%}  {elapsed:6.1f}s"
        )
    if sys.stdout.isatty():
        if redraw:
            print(f"\033[{len(lines)}A", end="")
        print("".join(f"\033[2K{line}\n" for line in lines), end="", flush=True)
    else:
        for name, line in zip(progress, lines):
            if name == changed and progress[name] == total:
                print(line, flush=True)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--mode", choices=("batch", "debug"), required=True)
    value.add_argument("--grid", type=Path, required=True)
    value.add_argument("--orders", type=Path, required=True)
    value.add_argument("--config", type=Path, required=True)
    value.add_argument("--layout", type=Path, action="append", required=True)
    value.add_argument("--start-date", type=_date)
    value.add_argument("--end-date", type=_date)
    value.add_argument("--date", type=_date, help="single debug date")
    value.add_argument("--output", type=Path)
    value.add_argument("--event-log", action="store_true")
    value.add_argument(
        "--amrs", type=int,
        help="override fleet size using the first N configured spawn nodes",
    )
    value.add_argument(
        "--workers", type=int, default=min(4, os.cpu_count() or 1),
        help="parallel batch processes (default: up to 4)",
    )
    value.add_argument(
        "--speed", type=float, default=120.0,
        help="debug multiplier in simulation seconds per real second (default: 120)",
    )
    value.add_argument(
        "--coordination-backend", choices=("auto", "python", "native"),
        default="python", help=argparse.SUPPRESS,
    )
    value.add_argument(
        "--simulation-backend", choices=("auto", "python", "native"),
        default="auto", help="day simulation implementation (default: auto)",
    )
    return value


def main(argv: list[str] | None = None) -> int:
    run_started = time.monotonic()
    started_at = datetime.now(timezone.utc)
    args = parser().parse_args(argv)
    if args.amrs is not None and args.amrs < 1:
        parser().error("--amrs must be positive")
    if args.workers < 1:
        parser().error("--workers must be positive")
    if args.mode == "debug":
        if len(args.layout) != 1 or args.date is None:
            parser().error("debug mode requires exactly one --layout and one --date")
        if args.start_date or args.end_date:
            parser().error("debug mode does not accept --start-date or --end-date")
        if args.speed <= 0:
            parser().error("--speed must be positive")
        start = end = args.date
    else:
        if args.date:
            parser().error("--date is available only in debug mode")
        start, end = args.start_date, args.end_date

    output = args.output or ROOT / "results" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output.mkdir(parents=True, exist_ok=True)
    try:
        config = SimulationConfig.load(args.config)
        if args.amrs is not None:
            config = config.with_amr_count(args.amrs)
        project = RmfMapService().load_project(args.grid)
        workload = load_workload(args.orders)
        tasks = workload.select(start, end)
        mapping = assign_workstations(tasks, config)
        day_engine, simulation_backend_info = load_day_engine(
            args.simulation_backend
        )
        warm_kernel, backend_info = load_native(
            config.amr_count, args.coordination_backend
        )
        if warm_kernel is not None:
            warm_kernel.close()
        if backend_info.fallback_reason:
            print(
                f"warning: native coordination unavailable; using Python: "
                f"{backend_info.fallback_reason}",
                file=sys.stderr,
            )
        if simulation_backend_info.fallback_reason:
            print(
                "warning: native day simulation unavailable; using Python: "
                f"{simulation_backend_info.fallback_reason}",
                file=sys.stderr,
            )
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    names = [_layout_name(path) for path in args.layout]
    if len(set(names)) != len(names):
        print("error: layout filenames must produce unique result names", file=sys.stderr)
        return 2
    router = GridRouter(project)
    loaded_layouts = []
    any_invalid = False
    for name, path in zip(names, args.layout):
        directory = output / name
        directory.mkdir(parents=True, exist_ok=True)
        try:
            payload = SlottingLayoutRepository().load(path)
            racks = racks_from_layout(payload["assignments"])
            report = validate_inputs(project, router, racks, tasks, config, mapping)
        except (OSError, ValueError) as exc:
            from amr_simulation.models import ValidationReport

            report = ValidationReport()
            report.error("invalid_layout", str(exc), layout=str(path))
            racks = {}
        write_json(directory / "validation_report.json", report.to_dict())
        write_json(directory / "config_snapshot.json", config.snapshot())
        any_invalid |= not report.valid
        loaded_layouts.append((name, racks, report, directory))
    if any_invalid:
        print(f"preflight failed; see validation reports under {output}", file=sys.stderr)
        return 2

    tasks_by_date = defaultdict(list)
    for task in tasks:
        tasks_by_date[task.task_date].append(task)
    retain_events = args.event_log or config.detailed_event_log or args.mode == "debug"
    dates = sorted(tasks_by_date)
    completed = {}
    day_timings = []
    simulation_started = time.monotonic()
    progress = dict.fromkeys(names, 0)
    if args.mode == "batch" and dates:
        _show_progress(progress, len(dates), simulation_started, redraw=False)
    dated_tasks = [tasks_by_date[task_date] for task_date in dates]
    if args.mode == "batch" and dates:
        completed = {name: [None] * len(dates) for name in names}
        with ProcessPoolExecutor(
            max_workers=min(args.workers, len(loaded_layouts) * len(dates))
        ) as pool:
            futures = {
                pool.submit(
                    _simulate_one, name, index, project, racks, day_tasks,
                    mapping, config, retain_events, backend_info.selected,
                    simulation_backend_info.selected,
                ): name
                for name, racks, _report, _directory in loaded_layouts
                for index, day_tasks in enumerate(dated_tasks)
            }
            pending = set(futures)
            last_redraw = simulation_started
            while pending:
                time.sleep(0.1)
                now = time.monotonic()
                if now - last_redraw >= 1.0:
                    _show_progress(
                        progress, len(dates), simulation_started, redraw=True
                    )
                    last_redraw = now
                finished = {future for future in pending if future.done()}
                for future in finished:
                    name, index, result, elapsed = future.result()
                    completed[name][index] = result
                    day_timings.append({
                        "layout": name,
                        "date": dates[index].isoformat(),
                        "seconds": elapsed,
                    })
                    progress[name] += 1
                    _show_progress(
                        progress, len(dates), simulation_started,
                        redraw=True, changed=name,
                    )
                pending -= finished
    else:
        for name, racks, _report, _directory in loaded_layouts:
            router = GridRouter(project)
            results = []
            for day_tasks in dated_tasks:
                results.append(
                    simulate_day(
                        project, router, racks, day_tasks, mapping, config,
                        trace=retain_events,
                        coordination_backend=backend_info.selected,
                        simulation_backend=simulation_backend_info.selected,
                    )
                )
                if args.mode == "batch":
                    progress[name] += 1
                    _show_progress(
                        progress, len(dates), simulation_started, redraw=True, changed=name
                    )
            completed[name] = results

    simulation_finished = time.monotonic()

    summaries = []
    debug_result = None
    for name, racks, report, directory in loaded_layouts:
        results = completed[name]
        summary = export_layout_results(
            directory, name, results, config, mapping, tasks, report, retain_events
        )
        summaries.append(summary)
        debug_result = results[0] if args.mode == "debug" else None
    if len(summaries) > 1:
        write_csv(output / "layout_comparison.csv", summaries)
    exported_at = time.monotonic()
    timing = {
        "started_at_utc": started_at.isoformat(),
        "completed_at_utc": datetime.now(timezone.utc).isoformat(),
        "mode": args.mode,
        "layout_count": len(loaded_layouts),
        "date_count": len(dates),
        "workers_requested": args.workers,
        "simulation_backend_requested": simulation_backend_info.requested,
        "simulation_backend": simulation_backend_info.selected,
        "simulation_build_hash": simulation_backend_info.build_hash,
        "simulation_compile_seconds": simulation_backend_info.compile_seconds,
        "native_simulation_seconds": simulation_backend_info.native_simulation_seconds,
        "native_decoding_seconds": simulation_backend_info.decoding_seconds,
        "simulation_fallback_reason": simulation_backend_info.fallback_reason,
        "coordination_backend_requested": backend_info.requested,
        "coordination_backend": backend_info.selected,
        "coordination_build_hash": backend_info.build_hash,
        "coordination_compile_seconds": backend_info.compile_seconds,
        "coordination_fallback_reason": backend_info.fallback_reason,
        "workers_used": (
            min(args.workers, len(loaded_layouts) * len(dates))
            if args.mode == "batch" and dates else 1
        ),
        "day_runs": sorted(
            day_timings, key=lambda item: (item["layout"], item["date"])
        ),
        "setup_seconds": simulation_started - run_started,
        "simulation_seconds": simulation_finished - simulation_started,
        "export_seconds": exported_at - simulation_finished,
        "total_seconds": exported_at - run_started,
    }
    write_json(output / "run_timing.json", timing)
    if debug_result is not None:
        run_debugger(
            project, debug_result, config.spawn_nodes,
            speed=args.speed,
            initial_heading_degrees=config.initial_heading_degrees,
        )
    print(
        f"simulation completed in {_duration(timing['simulation_seconds'])} "
        f"({timing['simulation_seconds']:.2f}s)"
    )
    print(
        f"total runtime {_duration(timing['total_seconds'])} "
        f"({timing['total_seconds']:.2f}s); results written to {output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
