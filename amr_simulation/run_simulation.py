"""Command-line entry point for batch comparison and debug playback."""

from __future__ import annotations

import argparse
import csv
import ctypes
import gc
import json
import os
import re
import sys
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import date, datetime, timezone
from multiprocessing import Manager
from pathlib import Path
from queue import Empty

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
from amr_simulation.results import (
    append_csv_row,
    daily_row,
    export_layout_summary,
    export_layout_results,
    write_csv,
    write_json,
)
from amr_simulation.routing import GridRouter


ROOT = Path(__file__).resolve().parent


def _release_day_memory() -> None:
    """Collect a completed day and return free glibc heap pages when available."""
    gc.collect()
    try:
        ctypes.CDLL(None).malloc_trim(0)
    except (AttributeError, OSError):
        pass


_INTEGER_METRICS = {
    "completed_lines", "completed_tasks", "rack_presentations",
    "reservation_conflicts", "node_ownership_conflicts",
    "dram_solver_conflicts", "reservation_reroutes",
    "dram_solver_reroutes", "max_reserved_nodes", "deadlock_count",
    "coordination_fallback_count",
}
_SUMMARY_METRICS = {
    "date", "completed_lines", "completed_tasks", "rack_presentations",
    "makespan_hours", "line_throughput_per_hour", "travel_distance_m",
    "station_queue_time_seconds", "node_reservation_wait_seconds",
    "reservation_conflicts", "node_ownership_conflicts",
    "dram_solver_conflicts", "reservation_reroutes", "dram_solver_reroutes",
    "dram_conflict_wait_seconds", "amr_utilization",
    "coordination_fallback_count",
}


def _checkpoint_metrics(path: Path) -> list[dict]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    metrics = []
    for row in rows:
        item = {"date": row["date"]}
        for key in _SUMMARY_METRICS - {"date"}:
            value = row.get(key, "0") or "0"
            item[key] = int(float(value)) if key in _INTEGER_METRICS else float(value)
        if row.get("coordination"):
            item["coordination"] = json.loads(row["coordination"])
            item["coordination_status"] = row.get("coordination_status", "VALID")
            item["eligible_for_comparison"] = (
                row.get("eligible_for_comparison", "True").lower() == "true"
            )
        metrics.append(item)
    return metrics


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc


def _layout_name(path: Path) -> str:
    name = path.name.removesuffix(".slotting.json")
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("._") or "layout"


def _simulate_layout(
    name, project, racks, dated_tasks, mapping, config, trace, progress_queue,
    directory, report,
):
    """Simulate and checkpoint one layout without retaining heavy day state."""
    results = []
    daily_path = directory / "daily_metrics.csv"
    metrics = [] if trace else _checkpoint_metrics(daily_path)
    completed_dates = {item["date"] for item in metrics}
    if trace:
        daily_path.unlink(missing_ok=True)
    for tasks in dated_tasks:
        task_date = tasks[0].task_date.isoformat()
        if not trace and task_date in completed_dates:
            if progress_queue is not None:
                progress_queue.put(name)
            continue
        # Route caches are day-local; retaining them across 179 days only grows RAM.
        result = simulate_day(
            project, GridRouter(project), racks, tasks, mapping, config, trace=trace
        )
        if result.deadlock_snapshot and not trace:
            write_json(directory / f"deadlock_snapshot_{task_date}.json", result.deadlock_snapshot)
        append_csv_row(daily_path, daily_row(result, config.workstations))
        # Detailed logs intentionally retain full results. Normal batch runs keep
        # metrics only and release jobs, paths, events, and motion segments now.
        if trace:
            results.append(result)
        else:
            metrics.append(result.metrics)
            del result
            _release_day_memory()
        if progress_queue is not None:
            progress_queue.put(name)
    all_tasks = [task for day in dated_tasks for task in day]
    if not trace:
        metrics.sort(key=lambda item: item["date"])
        return export_layout_summary(
            directory, name, metrics, config, mapping, all_tasks, report
        )
    return export_layout_results(
        directory, name, results, config, mapping, all_tasks, report, trace
    )


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
    value.add_argument(
        "--include-degraded",
        action="store_true",
        help="include degraded or failed layouts in layout_comparison.csv",
    )
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
    return value


def main(argv: list[str] | None = None) -> int:
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
    except (OSError, ValueError) as exc:
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
    started = time.monotonic()
    progress = dict.fromkeys(names, 0)
    if args.mode == "batch" and dates:
        _show_progress(progress, len(dates), started, redraw=False)
    dated_tasks = [tasks_by_date[task_date] for task_date in dates]
    if args.mode == "batch" and args.workers > 1 and len(loaded_layouts) > 1:
        with Manager() as manager, ProcessPoolExecutor(
            max_workers=min(args.workers, len(loaded_layouts))
        ) as pool:
            progress_queue = manager.Queue()
            futures = {
                pool.submit(
                    _simulate_layout, name, project, racks, dated_tasks,
                    mapping, config, retain_events, progress_queue, directory,
                    report,
                ): name
                for name, racks, report, directory in loaded_layouts
            }
            pending = set(futures)
            while pending:
                try:
                    name = progress_queue.get(timeout=0.1)
                    progress[name] += 1
                    _show_progress(
                        progress, len(dates), started, redraw=True, changed=name
                    )
                except Empty:
                    pass
                finished = {future for future in pending if future.done()}
                for future in finished:
                    name = futures[future]
                    completed[name] = future.result()
                    if progress[name] < len(dates):
                        progress[name] = len(dates)
                        _show_progress(
                            progress, len(dates), started, redraw=True,
                            changed=name,
                        )
                pending -= finished
    else:
        for name, racks, report, directory in loaded_layouts:
            if args.mode == "batch":
                class _Progress:
                    def put(self, changed):
                        progress[changed] += 1
                        _show_progress(
                            progress, len(dates), started, redraw=True,
                            changed=changed,
                        )

                completed[name] = _simulate_layout(
                    name, project, racks, dated_tasks, mapping, config,
                    retain_events, _Progress(), directory, report,
                )
            else:
                completed[name] = [simulate_day(
                    project, GridRouter(project), racks, dated_tasks[0],
                    mapping, config, trace=True,
                )]

    summaries = []
    debug_result = None
    for name, racks, report, directory in loaded_layouts:
        if args.mode == "batch":
            summary = completed[name]
        else:
            results = completed[name]
            summary = export_layout_results(
                directory, name, results, config, mapping, tasks, report,
                retain_events,
            )
            debug_result = results[0]
        summaries.append(summary)
    if len(summaries) > 1:
        comparison = summaries if args.include_degraded else [
            summary for summary in summaries
            if summary.get("eligible_for_comparison", True)
        ]
        write_csv(output / "layout_comparison.csv", comparison)
    if debug_result is not None:
        run_debugger(
            project, debug_result, config.spawn_nodes,
            speed=args.speed,
            initial_heading_degrees=config.initial_heading_degrees,
        )
    print(f"results written to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
