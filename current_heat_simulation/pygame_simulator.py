"""Compare slotting layouts with identical store/day demand using current_heat."""

import argparse
from collections import defaultdict
from datetime import date, datetime
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import re
import sys

import yaml

from amr_simulation.inputs import assign_workstations, load_workload, racks_from_layout, validate_inputs
from amr_simulation.models import SimulationConfig
from warehouse_layout.slotting_repository import SlottingLayoutRepository

from .astar_planner import WarehouseMap
from .batch import code_fingerprint, execute_batch, file_hash, fingerprint
from .reporting import generate_report
from .run_metrics import write_json


def layout_name(path):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", path.name.removesuffix(".slotting.json")).strip("._") or "layout"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("current_heat.yaml"))
    parser.add_argument("--grid", type=Path)
    parser.add_argument("--orders", type=Path)
    parser.add_argument("--layout", type=Path, action="append", help="repeat for each slotting layout")
    parser.add_argument("--amr-config", type=Path)
    parser.add_argument("--amrs", type=int)
    parser.add_argument("--date", type=date.fromisoformat, action="append", help="select a date; repeat for separate dates")
    parser.add_argument("--start-date", type=date.fromisoformat)
    parser.add_argument("--end-date", type=date.fromisoformat)
    parser.add_argument("--headless", action="store_true", help="all observed dates by default")
    parser.add_argument("--max-seconds", type=float, help="per-day simulation limit; default 86400")
    parser.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 1))
    parser.add_argument("--resume", action="store_true", help="reuse compatible successful day checkpoints")
    parser.add_argument("--baseline-layout", help="layout name or supplied layout file path")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.date and (args.start_date or args.end_date):
        parser.error("use --date or --start-date/--end-date, not both")
    if args.date and len(set(args.date)) > 1 and not args.headless:
        parser.error("multiple dates require --headless")
    if (args.start_date or args.end_date) and not args.headless:
        parser.error("date ranges require --headless; use --date for a rendered run")
    if args.workers < 1:
        parser.error("--workers must be positive")
    if args.resume and args.output is None:
        parser.error("--resume requires --output pointing to the previous batch")
    if not args.headless and args.layout and len(args.layout) != 1:
        parser.error("rendered runs require exactly one layout")

    try:
        config_path = args.config.resolve()
        config = yaml.safe_load(config_path.read_text())
        if config.get("method") != "current_heat":
            raise ValueError("only method: current_heat is supported")
        if config.get("allocator", {}).get("priority_strategy") == "random":
            raise ValueError("layout comparisons require a deterministic allocation priority")
        if float(config.get("simulation", {}).get("fixed_sim_step_sec", .05)) != .05:
            raise ValueError("layout comparisons retain the 0.05-second simulation step")
        limit = args.max_seconds if args.max_seconds is not None else float(config.get("simulation", {}).get("stop_at_sim_time_sec", 86400))
        if not math.isfinite(limit) or limit <= 0:
            raise ValueError("--max-seconds must be positive and finite")

        def input_path(key, override):
            if override is not None:
                return override.resolve()
            path = Path(config[key])
            return path.resolve() if path.is_absolute() else (config_path.parent/path).resolve()

        grid_path = input_path("map_file", args.grid)
        orders_path = input_path("orders_file", args.orders)
        layout_paths = [p.resolve() for p in args.layout] if args.layout else [input_path("layout_file", None)]
        amr_path = input_path("amr_config", args.amr_config)
        names = [layout_name(path) for path in layout_paths]
        if len(set(names)) != len(names):
            raise ValueError("layout filenames must produce unique names")
        baseline = args.baseline_layout or ("map1_basic" if "map1_basic" in names else names[0])
        if baseline not in names:
            baseline = layout_name(Path(baseline))
        if baseline not in names:
            raise ValueError("--baseline-layout must identify one of the supplied layouts")
        amr_config = SimulationConfig.load(amr_path)
        if args.amrs is not None:
            amr_config = amr_config.with_amr_count(args.amrs)
        warehouse_map = WarehouseMap(grid_path)
        print("Loading shared workload and validating layouts...", flush=True)
        workload = load_workload(orders_path)
        if args.date:
            tasks = [task for day in sorted(set(args.date)) for task in workload.select(day, day)]
        elif args.headless:
            tasks = workload.select(args.start_date, args.end_date)
        else:
            tasks = workload.select(workload.min_date, workload.min_date)
        mapping = assign_workstations(tasks, amr_config)
        days = defaultdict(list)
        for task in tasks:
            days[task.task_date.isoformat()].append(task)
        output = (args.output or Path(__file__).parent/"results"/datetime.now().strftime("%Y%m%dT%H%M%S_%f")).resolve()
        layouts = {}
        for name, path in zip(names, layout_paths):
            payload = SlottingLayoutRepository().load(path)
            racks = racks_from_layout(payload["assignments"])
            report = validate_inputs(warehouse_map.project, warehouse_map.router, racks, tasks, amr_config, mapping)
            layouts[name] = {"racks": racks, "strategy": payload.get("strategy"), "path": str(path),
                             "validation": report.to_dict()}
        versions = {package: importlib.metadata.version(package) for package in ("numpy", "pygame", "PyYAML", "openpyxl")}
        identity = {
            "schema": "current_heat_batch/v1", "code": code_fingerprint(),
            "versions": {"python": platform.python_version(), **versions},
            "inputs": {str(p): file_hash(p) for p in [grid_path, orders_path, amr_path, *layout_paths]},
            "config": config, "amr": amr_config.snapshot(), "dates": sorted(days),
            "mapping": mapping, "layouts": names, "max_seconds": limit,
        }
        batch_id = fingerprint(identity)
        manifest_path = output/"manifest.json"
        if manifest_path.exists():
            if not args.resume:
                raise ValueError("output already contains a batch; use --resume or a new --output")
            if json.loads(manifest_path.read_text()).get("fingerprint") != batch_id:
                raise ValueError("resume fingerprint mismatch: inputs, dates, configuration or code changed; use a new --output")
        output.mkdir(parents=True, exist_ok=True)
        for name, layout in layouts.items():
            write_json(output/name/"validation_report.json", layout["validation"])
        if any(not layout["validation"]["valid"] for layout in layouts.values()):
            raise ValueError(f"preflight failed; see per-layout validation reports under {output}")
        write_json(manifest_path, {"fingerprint": batch_id, "identity": identity,
                                  "baseline_layout": baseline, "headless": args.headless})
        write_json(output/"config_snapshot.json", {"current_heat": config, "amr": amr_config.snapshot(), "max_seconds": limit})
        write_json(output/"store_workstation_mapping.json", mapping)
        write_json(output/"workload_snapshot.json", {
            "orders_file": str(orders_path), "release_rule": "all store/day groups at t=0",
            "days": {day: [{"task_id": t.task_id, "store_id": t.store_id, "line_counts": t.line_counts,
                             "source_lines": t.source_lines, "release_seconds": 0.0} for t in daily]
                     for day, daily in sorted(days.items())},
        })
        shared = {"fingerprint": batch_id, "config": config, "grid": grid_path, "days": dict(days),
                  "layouts": layouts, "mapping": mapping, "amr": amr_config,
                  "headless": args.headless, "max_seconds": limit}
        workers = min(args.workers, os.cpu_count() or 1) if args.headless else 1
        print(f"current_heat: {len(layouts)} layouts × {len(days)} days, {amr_config.amr_count} robots; up to {workers} workers", flush=True)
        results = execute_batch(shared, output, workers, args.resume)
        generate_report(output, results, layouts, sorted(days), baseline, warehouse_map)
        print(f"Report: {output/'comparison_report.md'}", flush=True)
        return 0 if all(r.get("success_flag") for r in results) else 1
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
