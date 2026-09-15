#!/usr/bin/env python3
"""Run the independent global congestion-balanced slotting pipeline."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from warehouse_layout.affinity import AffinityService
from warehouse_layout.attributes import StorageAttributeService
from warehouse_layout.global_traffic import GlobalTrafficSlottingService
from warehouse_layout.rmf import RmfMapService
from warehouse_layout.slotting import SlottingLayoutRepository, SlottingService
from warehouse_layout.traffic import TrafficAwareSlottingService


def _date(value: str) -> date | None:
    return date.fromisoformat(value) if value.strip() else None


def _progress(current: int, total: int, message: str) -> None:
    percent = round(100 * current / max(1, total))
    print(f"[{percent:3d}%] {message}", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Globally balance warehouse congestion with simultaneous handling-"
            "unit placement. Existing Traffic-Aware Slotting is not used or "
            "modified."
        )
    )
    parser.add_argument(
        "--mode",
        choices=("existing", "full"),
        required=True,
        help="Optimize a saved layout, or generate an initial layout first.",
    )
    parser.add_argument("--orders", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--network", type=Path)
    parser.add_argument("--start-date", default="")
    parser.add_argument("--end-date", default="")
    parser.add_argument("--time-limit", type=float, default=180.0)
    parser.add_argument("--gap-percent", type=float, default=0.0)
    parser.add_argument("--max-travel-increase-percent", type=float, default=0.0)
    parser.add_argument(
        "--max-controllable-p95-increase-percent",
        type=float,
        default=0.0,
        help="Reject a recommendation exceeding this controllable P95 increase.",
    )
    parser.add_argument(
        "--max-relocated-percent",
        type=float,
        default=50.0,
        help="Maximum percentage of handling units that may be relocated.",
    )
    parser.add_argument(
        "--neighbourhood",
        choices=("shared_resource", "zone"),
        default="shared_resource",
    )

    existing = parser.add_argument_group("existing layout mode")
    existing.add_argument("--layout", type=Path)

    full = parser.add_argument_group("full pipeline mode")
    full.add_argument("--grid-project", type=Path)
    full.add_argument("--velocity", type=Path)
    full.add_argument("--chilled", type=Path)
    full.add_argument(
        "--initial-strategy",
        choices=("basic", "abc_affinity"),
        default="basic",
    )
    full.add_argument(
        "--affinity-weight",
        type=float,
        default=0.5,
        help="Affinity fraction from 0.0 to 1.0.",
    )
    return parser


def _required(parser: argparse.ArgumentParser, value, option: str) -> None:
    if value is None:
        parser.error(f"{option} is required for the selected mode")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.time_limit < 0:
        parser.error("--time-limit cannot be negative")
    if not 0 <= args.gap_percent <= 100:
        parser.error("--gap-percent must be between 0 and 100")
    if args.max_travel_increase_percent < 0:
        parser.error("--max-travel-increase-percent cannot be negative")
    if args.max_controllable_p95_increase_percent < 0:
        parser.error(
            "--max-controllable-p95-increase-percent cannot be negative"
        )
    if not 0 <= args.max_relocated_percent <= 100:
        parser.error("--max-relocated-percent must be between 0 and 100")
    if not 0 <= args.affinity_weight <= 1:
        parser.error("--affinity-weight must be between 0 and 1")

    attributes = StorageAttributeService()
    rmf = RmfMapService()
    slotting = SlottingService(rmf, attributes)
    traffic = TrafficAwareSlottingService(attributes, slotting)
    service = GlobalTrafficSlottingService(traffic)
    affinity = AffinityService()
    dataset = affinity.load_orders(args.orders.resolve())
    start, end = _date(args.start_date), _date(args.end_date)

    if args.mode == "existing":
        _required(parser, args.layout, "--layout")
        baseline_path = args.layout.resolve()
        baseline = SlottingLayoutRepository().load(baseline_path)
        network = (
            traffic.load_network(args.network.resolve())
            if args.network
            else traffic.network_from_rmf(baseline["building"])
        )
        result = service.optimize_existing_layout(
            baseline,
            dataset,
            network,
            start_date=start,
            end_date=end,
            maximum_travel_increase=args.max_travel_increase_percent / 100.0,
            time_limit_seconds=args.time_limit,
            relative_gap_limit=args.gap_percent / 100.0,
            maximum_controllable_p95_increase=(
                args.max_controllable_p95_increase_percent / 100.0
            ),
            maximum_relocation_fraction=args.max_relocated_percent / 100.0,
            neighbourhood_mode=args.neighbourhood,
            baseline_path=str(baseline_path),
            source_orders=str(args.orders.resolve()),
            progress=_progress,
        )
    else:
        _required(parser, args.grid_project, "--grid-project")
        _required(parser, args.velocity, "--velocity")
        project_path = args.grid_project.resolve()
        velocity_path = args.velocity.resolve()
        project = rmf.load_project(project_path)
        if project.storage_layout is None or not project.storage_layout.buffers:
            parser.error(
                "the grid project has no storage buffers; assign and save "
                "buffers in Grid Map Editor first"
            )
        building = project.to_building_dict()
        network = (
            traffic.load_network(args.network.resolve())
            if args.network
            else traffic.network_from_rmf(building)
        )
        catalog = attributes.normalize_catalog(
            project.attribute_catalog or attributes.starter_catalog()
        )
        rows = slotting.load_velocity(
            velocity_path,
            catalog,
            args.chilled.resolve() if args.chilled else None,
        )
        affinity_source = (
            affinity.analyze(dataset, start, end)
            if args.initial_strategy == "abc_affinity"
            else dataset
        )
        layout = project.storage_layout
        result = service.run_full_pipeline(
            building,
            rows,
            affinity_source,
            network,
            initial_strategy=args.initial_strategy,
            affinity_weight=args.affinity_weight,
            levels_per_rack=layout.levels_per_rack,
            slots_per_level=layout.slots_per_level,
            handling_unit_type=layout.handling_unit_type,
            zone_assignments=project.zone_assignments,
            attribute_catalog=catalog,
            location_attributes=project.location_attributes,
            storage_layout=layout,
            start_date=start,
            end_date=end,
            maximum_travel_increase=args.max_travel_increase_percent / 100.0,
            time_limit_seconds=args.time_limit,
            relative_gap_limit=args.gap_percent / 100.0,
            maximum_controllable_p95_increase=(
                args.max_controllable_p95_increase_percent / 100.0
            ),
            maximum_relocation_fraction=args.max_relocated_percent / 100.0,
            neighbourhood_mode=args.neighbourhood,
            source_grid_project=str(project_path),
            source_velocity=str(velocity_path),
            source_chilled=str(args.chilled.resolve()) if args.chilled else "",
            source_orders=str(args.orders.resolve()),
            progress=_progress,
        )

    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result.output_payload, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "output": str(output),
        "solver_status": result.solver["status"],
        "global_optimum_proven": result.solver["global_optimum_proven"],
        "relative_gap": result.solver["relative_gap"],
        "relocation_count": len(result.relocations),
        "peak_before": result.before.metrics["peak_load"],
        "peak_after": result.after.metrics["peak_load"],
        "travel_before": result.before.metrics["expected_travel"],
        "travel_after": result.after.metrics["expected_travel"],
        "controllable_peak_before": result.balance_metrics[
            "controllable_resources_before"
        ]["peak_load"],
        "controllable_peak_after": result.balance_metrics[
            "controllable_resources_after"
        ]["peak_load"],
        "controllable_p95_before": result.balance_metrics[
            "controllable_resources_before"
        ]["p95_load"],
        "controllable_p95_after": result.balance_metrics[
            "controllable_resources_after"
        ]["p95_load"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
