from __future__ import annotations

import argparse
import math
import re
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path

from .engine import simulate_day
from .io import assign_workstations, load_grid, load_layout, load_orders, validate
from .models import Config, parse_node
from .planner import LaCAMPlanner, distance_tables
from .render import render
from .reports import export, write_csv, write_json


class LineProgress:
    def __init__(self, names: list[str], total: int, stream=sys.stdout):
        self.names, self.total, self.stream = names, total, stream
        self.done = dict.fromkeys(names, 0); self.last_reported = dict.fromkeys(names, -1)
        self.started = time.monotonic(); self.lock = threading.Lock(); self.drawn = False

    @staticmethod
    def _duration(seconds: float) -> str:
        if not math.isfinite(seconds): return "--:--"
        seconds = max(0, int(seconds)); return f"{seconds // 60:02d}:{seconds % 60:02d}"

    def update(self, name: str, lines: int) -> None:
        with self.lock:
            self.done[name] = min(self.total, self.done[name] + lines)
            self._draw(name)

    def _draw(self, changed: str) -> None:
        elapsed = max(time.monotonic() - self.started, 1e-9)
        if not self.stream.isatty():
            bucket = (100 * self.done[changed] // self.total) // 10
            if bucket == self.last_reported[changed]: return
            self.last_reported[changed] = bucket
            self.stream.write(self._line(changed, elapsed) + "\n"); self.stream.flush(); return
        if self.drawn: self.stream.write(f"\033[{len(self.names)}A")
        for name in self.names: self.stream.write("\033[2K" + self._line(name, elapsed) + "\n")
        self.stream.flush(); self.drawn = True

    def _line(self, name: str, elapsed: float) -> str:
        done = self.done[name]; ratio = done / self.total
        width = 28; filled = int(width * ratio)
        rate = done / elapsed; eta = (self.total - done) / rate if rate else math.inf
        return (f"{name:<32} [{'█' * filled}{'░' * (width-filled)}] "
                f"{done:>9,}/{self.total:<9,} lines {ratio:6.1%} "
                f"{rate:8.1f} lines/s ETA {self._duration(eta)}")

    def start(self) -> None:
        with self.lock:
            if self.stream.isatty(): self._draw(self.names[0])


def _date(value: str) -> date:
    try: return date.fromisoformat(value)
    except ValueError as exc: raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc


def _name(path: Path) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", path.name.removesuffix(".slotting.json")).strip("._")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Standalone LaCAM AMR digital-twin engine")
    p.add_argument("mode", choices=("batch", "debug"))
    p.add_argument("--grid", type=Path, required=True); p.add_argument("--orders", type=Path, required=True)
    p.add_argument("--config", type=Path, required=True); p.add_argument("--layout", type=Path, action="append", required=True)
    p.add_argument("--start-date", type=_date); p.add_argument("--end-date", type=_date); p.add_argument("--date", type=_date)
    p.add_argument("--amrs", type=int); p.add_argument("--workers", type=int, default=1)
    p.add_argument("--seed", type=int); p.add_argument("--planner-timeout", type=float)
    p.add_argument("--output", type=Path); p.add_argument("--trace", action="store_true"); p.add_argument("--speed", type=float, default=20)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.mode == "debug" and (len(args.layout) != 1 or args.date is None):
        parser().error("debug requires one --layout and --date")
    if args.workers < 1: parser().error("--workers must be positive")
    if args.speed <= 0: parser().error("--speed must be positive")
    root = Path(__file__).resolve().parents[1]
    output = args.output or root / "results" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    try:
        config = Config.load(args.config)
        values = config.snapshot()
        if args.amrs is not None: values["amr_count"] = args.amrs
        if args.seed is not None: values["planner_seed"] = args.seed
        if args.planner_timeout is not None: values["planner_timeout_seconds"] = args.planner_timeout
        values["motion"] = config.motion
        config = Config(**values); config.validate()
        grid, tasks = load_grid(args.grid), load_orders(args.orders)
        if args.mode == "debug": selected = [task for task in tasks if task.date == args.date]
        else:
            selected = [task for task in tasks if (args.start_date is None or task.date >= args.start_date) and (args.end_date is None or task.date <= args.end_date)]
        if not selected: raise ValueError("selected date range contains no tasks")
        mapping = assign_workstations(selected, config.workstations)
        binary = LaCAMPlanner.build(root)
        print("Preparing shared warehouse distance tables…", flush=True)
        distance_cache = distance_tables(grid, grid.nodes)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr); return 2
    by_date = defaultdict(list)
    for task in selected: by_date[task.date].append(task)
    names = [_name(path) for path in args.layout]
    if len(set(names)) != len(names):
        print("error: layout filenames must produce unique names", file=sys.stderr); return 2
    total_lines = sum(sum(task.lines.values()) for task in selected)
    progress = LineProgress(names, total_lines); progress.start()

    def run_layout(layout_path):
        name, directory = _name(layout_path), output / _name(layout_path)
        layout = load_layout(layout_path)
        report = validate(grid, layout, selected, [parse_node(v) for v in config.spawn_nodes[:config.amr_count]], config.workstations)
        write_json(directory / "validation_report.json", report)
        if not report["valid"]: raise ValueError(f"preflight failed for {name}")
        results = []
        for day in sorted(by_date):
            result = simulate_day(
                grid, layout, by_date[day], mapping, config, binary,
                lambda count, layout_name=name: progress.update(layout_name, count),
                distance_cache,
            )
            results.append(result)
        return export(directory, name, results, config, report, args.trace or args.mode == "debug"), results

    try:
        if args.mode == "batch" and args.workers > 1 and len(args.layout) > 1:
            with ThreadPoolExecutor(max_workers=min(args.workers, len(args.layout))) as pool:
                completed = list(pool.map(run_layout, args.layout))
        else:
            completed = [run_layout(path) for path in args.layout]
    except (OSError, ValueError, RuntimeError) as exc:
            directory = output / "failed_run"
            write_json(directory / "validation_report.json", {"valid": False, "errors": [{"code": "run_error", "message": str(exc)}], "warnings": []})
            print(f"error: {exc}", file=sys.stderr); return 2
    summaries = [item[0] for item in completed]
    debug_result = completed[0][1][0] if args.mode == "debug" else None
    if len(summaries) > 1: write_csv(output / "layout_comparison.csv", summaries)
    print(f"results written to {output}")
    if debug_result is not None: render(grid, debug_result, config, args.speed)
    return int(any(s["failed_days"] for s in summaries))
