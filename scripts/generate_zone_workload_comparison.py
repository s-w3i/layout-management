#!/usr/bin/env python3
"""Generate matching map1 C&TBSA samples with zone balancing OFF and ON."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from warehouse_layout.affinity import AffinityService
from warehouse_layout.ctbsa import CtbsaParameters
from warehouse_layout.rmf import RmfMapService
from warehouse_layout.slotting_repository import SlottingLayoutRepository
from warehouse_layout.traffic import TrafficAwareSlottingService


MAP = ROOT / "resources/map/map1_1.grid.json"
BASELINE = ROOT / "resources/map/basic_CO_quantity_slotting_layout.slotting.json"
ORDERS = ROOT / "resources/data/Sample Data.xlsx"
CACHE = ROOT / "resources/data/sku_affinity_cache"
OFF_OUTPUT = ROOT / "resources/map/traffic_aware_zone_off_map1.slotting.json"
ON_OUTPUT = ROOT / "resources/map/traffic_aware_zone_on_map1.slotting.json"
SUMMARY = ROOT / "resources/map/traffic_aware_zone_comparison_map1.json"
REPORT = ROOT / "resources/map/traffic_aware_zone_comparison_map1.md"


def progress(current: int, total: int, message: str) -> None:
    print(f"[{current}/{total}] {message}", flush=True)


def main() -> None:
    project = RmfMapService().load_project(MAP)
    baseline = SlottingLayoutRepository().load(BASELINE)
    service = TrafficAwareSlottingService()
    network = service.network_from_rmf(project.to_building_dict())
    affinity = AffinityService(CACHE)
    dataset = affinity.load_orders(ORDERS)
    parameters = CtbsaParameters(
        population_size=3, generations=1, random_seed=0, selected_solution=3
    )
    results = {}
    for label, enabled, destination in (
        ("zone_off", False, OFF_OUTPUT),
        ("zone_on", True, ON_OUTPUT),
    ):
        pipeline = service.run_existing_layout(
            baseline,
            dataset,
            network,
            ctbsa_parameters=parameters,
            zone_workload_enabled=enabled,
            baseline_path=str(BASELINE.resolve()),
            source_orders=str(ORDERS.resolve()),
            progress=progress,
        )
        destination.write_text(
            json.dumps(pipeline.output_payload, indent=2) + "\n", encoding="utf-8"
        )
        optimization = pipeline.optimization
        results[label] = {
            "output": str(destination.relative_to(ROOT)),
            "zone_workload_enabled": enabled,
            "occupied_racks": optimization.parameters["selected_rack_count"],
            "movement_resources": optimization.after.metrics,
            "zone_workload": optimization.after.zone_analysis["metrics"],
        }
    SUMMARY.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    off = results["zone_off"]
    on = results["zone_on"]
    metrics = (
        ("Occupied racks", "occupied_racks"),
        ("Expected travel", "expected_travel"),
        ("Peak normalized zone demand", "peak_normalized_zone_demand"),
        ("P95 normalized zone demand", "p95_normalized_zone_demand"),
        ("Peak normalized zone traffic", "peak_normalized_zone_traffic"),
        ("P95 normalized zone traffic", "p95_normalized_zone_traffic"),
    )
    lines = [
        "# map1 Optional Zone-Aware C&TBSA Comparison", "",
        "Both runs use the same Basic quantity baseline, orders, C&TBSA settings, and seed.",
        "", "| Metric | Zone OFF | Zone ON |", "|---|---:|---:|",
    ]
    for label, key in metrics:
        if key == "occupied_racks":
            first, second = off[key], on[key]
        elif key == "expected_travel":
            first = off["movement_resources"][key]
            second = on["movement_resources"][key]
        else:
            first = off["zone_workload"][key]
            second = on["zone_workload"][key]
        lines.append(f"| {label} | {first:.4f} | {second:.4f} |")
    lines += [
        "", "## Interpretation", "",
        "The configured objective is lexicographic: zone-demand peak, "
        "zone-traffic peak, zone-demand P95, zone-traffic P95, then travel. "
        "A lower-priority metric may therefore increase when zone-demand peak "
        "improves.",
    ]
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
