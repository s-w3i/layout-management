#!/usr/bin/env python3
"""Generate the map1 Affinity sample and compare slotting strategies."""

from __future__ import annotations

import argparse
import copy
import csv
import json
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from warehouse_layout.affinity import AffinityService
from warehouse_layout.rmf import RmfMapService
from warehouse_layout.slotting import SlottingService
from warehouse_layout.slotting_repository import SlottingLayoutRepository
from warehouse_layout.slotting_strategies.affinity_support import (
    order_rack_touch_metrics,
)
from warehouse_layout.traffic import TrafficAwareSlottingService


MAP = ROOT / "resources/map/map1.grid.json"
VELOCITY = ROOT / "resources/data/sku_velocity_output/sku_velocity_summary.csv"
ATTRIBUTES = ROOT / "resources/data/medicine_sku_attributes.csv"
STOCK = ROOT / "resources/data/minimum_stock_requirements.csv"
ORDERS = ROOT / "resources/data/Sample Data.xlsx"
CACHE = ROOT / "resources/data/sku_affinity_cache"
ABC = ROOT / "resources/map/basic_CO_quantity_slotting_layout.slotting.json"
AFFINITY = ROOT / "resources/map/affinity_CO_quantity_slotting_layout.slotting.json"
CTBSA = ROOT / "resources/map/traffic_aware_CO_slotting_layout.slotting.json"
SUMMARY_CSV = ROOT / "resources/map/slotting_strategy_comparison.csv"
SUMMARY_JSON = ROOT / "resources/map/slotting_strategy_comparison.json"
RACK_CSV = ROOT / "resources/map/slotting_strategy_rack_comparison.csv"
REPORT = ROOT / "resources/map/slotting_strategy_comparison.md"


def generate_affinity() -> None:
    project = RmfMapService().load_project(MAP)
    service = SlottingService()
    service.attributes.set_standard_storage_defaults(
        project.warehouse_storage_defaults
    )
    skus = service.load_velocity(VELOCITY, project.attribute_catalog, ATTRIBUTES)
    with STOCK.open(newline="", encoding="utf-8-sig") as stream:
        skus = service.apply_stock_requirements(skus, list(csv.DictReader(stream)))
    affinity_service = AffinityService(CACHE)
    analysis = affinity_service.analyze(affinity_service.load_orders(ORDERS))
    rows, summary = service.generate_abc_affinity(
        project.to_building_dict(),
        skus,
        analysis,
        0.5,
        3,
        4,
        "AMR shelf",
        "Z01",
        project.zone_assignments,
        project.attribute_catalog,
        copy.deepcopy(project.location_attributes),
        tuning_parameters=None,
        storage_layout=project.storage_layout,
    )
    SlottingLayoutRepository().save(
        rows,
        project.to_building_dict(),
        summary,
        AFFINITY,
        strategy="abc_affinity",
        handling_unit_type="AMR shelf",
        levels_per_rack=3,
        slots_per_level=4,
        zone_assignments=summary.get(
            "zone_assignments", project.zone_assignments
        ),
        attribute_catalog=project.attribute_catalog,
        location_attributes=summary.get(
            "location_attributes", project.location_attributes
        ),
        source_grid_project=str(MAP.resolve()),
        source_velocity=str(VELOCITY.resolve()),
        source_chilled=str(ATTRIBUTES.resolve()),
        source_affinity=str(ORDERS.resolve()),
        affinity_configuration=summary.get("affinity_tuning", {}),
        standard_storage_defaults=project.warehouse_storage_defaults,
        storage_layout=project.storage_layout,
    )
    if summary.get("unassigned_count"):
        raise RuntimeError(
            f"Affinity generation left {summary['unassigned_count']} loads unassigned"
        )


def assigned(payload: dict) -> list[dict]:
    return [
        row for row in payload["assignments"]
        if row.get("assignment_status") == "ASSIGNED"
    ]


def load_id(row: dict) -> str:
    value = str(row.get("inventory_load_id", "")).strip()
    if value:
        return value
    return f"legacy:{row.get('sku', '')}:{row.get('handling_unit_id', '')}"


def position(row: dict) -> tuple:
    occupied = tuple(sorted(
        (
            str(unit.get("rack_id", row.get("rack_id", ""))),
            int(unit.get("storage_level") or 0),
            int(unit.get("storage_slot") or 0),
            str(unit.get("handling_unit_id", "")),
        )
        for unit in (row.get("occupied_handling_units") or [])
    ))
    return (
        str(row.get("rack_id", "")),
        int(row.get("storage_level") or 0),
        int(row.get("storage_slot") or 0),
        occupied,
    )


def percentile(values: list[float], value: float) -> float:
    return float(np.percentile(values, value)) if values else 0.0


def distribution(values: list[float], prefix: str) -> dict:
    return {
        f"{prefix}_average": statistics.fmean(values) if values else 0.0,
        f"{prefix}_median": statistics.median(values) if values else 0.0,
        f"{prefix}_p95": percentile(values, 95),
        f"{prefix}_maximum": max(values, default=0.0),
        f"{prefix}_stddev": statistics.pstdev(values) if len(values) > 1 else 0.0,
    }


def rack_state(rows: list[dict], demand) -> dict[str, dict]:
    racks: dict[str, dict] = defaultdict(
        lambda: {"skus": set(), "loads": set(), "weight": 0.0,
                     "quantity": 0.0, "slot_count": 0, "visit_load": 0.0}
    )
    unit_to_rack: dict[str, str] = {}
    for row in rows:
        rack = str(row.get("rack_id", ""))
        if not rack:
            continue
        state = racks[rack]
        state["skus"].add(str(row.get("sku", "")))
        state["loads"].add(load_id(row))
        state["weight"] += float(row.get("inventory_load_weight_kg") or 0)
        state["quantity"] += float(row.get("quantity_ea") or 0)
        state["slot_count"] += int(row.get("occupied_slot_count") or 1)
        unit_to_rack[str(row.get("handling_unit_id", ""))] = rack
    for unit, visits in demand.unit_visits.items():
        rack = unit_to_rack.get(str(unit))
        if rack:
            racks[rack]["visit_load"] += float(visits)
    return dict(racks)


def analyze_strategy(
    name: str,
    payload: dict,
    affinity_analysis,
    traffic: TrafficAwareSlottingService,
    network,
    route_cache,
    relative_reference: float | None,
) -> tuple[dict, dict, object]:
    rows = assigned(payload)
    demand = traffic.build_demand(
        affinity_analysis.dataset, rows, network=network
    )
    analysis = traffic.analyze(
        rows,
        network,
        demand,
        route_cache=route_cache,
        relative_reference=relative_reference,
    )
    racks = rack_state(rows, demand)
    weights = [state["weight"] for state in racks.values()]
    visits = [state["visit_load"] for state in racks.values()]
    slots = [float(state["slot_count"]) for state in racks.values()]
    summary = payload.get("summary", {})
    metrics = {
        "strategy": name,
        "occupied_racks": len(racks),
        "assigned_inventory_loads": len(rows),
        "assigned_skus": len({str(row.get("sku", "")) for row in rows}),
        "occupied_slots": sum(int(row.get("occupied_slot_count") or 1) for row in rows),
        "compact_rack_count": int(summary.get("compact_rack_count") or len(racks)),
        "final_occupied_rack_count": int(
            summary.get("final_occupied_rack_count") or len(racks)
        ),
        "consolidation_status": str(summary.get("consolidation_status", "")),
        "expected_travel": float(analysis.metrics["expected_travel"]),
        "peak_normalized_resource_load": float(analysis.metrics["peak_load"]),
        "p95_normalized_resource_load": float(analysis.metrics["p95_load"]),
        "raw_peak_resource_load": float(analysis.metrics["raw_peak_load"]),
        "raw_p95_resource_load": float(analysis.metrics["raw_p95_load"]),
        "handling_unit_visits": int(demand.handling_unit_visits),
        "fulfillment_groups": int(demand.fulfillment_groups),
        "matched_order_events": int(demand.matched_events),
        "unmatched_sku_count": len(demand.unmatched_skus),
        **distribution(weights, "rack_weight_kg"),
        **distribution(visits, "rack_visit_load"),
        **distribution(slots, "occupied_slots_per_rack"),
        **order_rack_touch_metrics(affinity_analysis, rows),
    }
    visit_average = metrics["rack_visit_load_average"]
    metrics["rack_visit_load_cv"] = (
        metrics["rack_visit_load_stddev"] / visit_average
        if visit_average else 0.0
    )
    return metrics, racks, analysis


def pct_delta(value: float, baseline: float) -> float | None:
    if baseline == 0:
        return None
    return (value - baseline) / baseline * 100.0


def compare() -> dict:
    repository = SlottingLayoutRepository()
    payloads = {
        "ABC baseline": repository.load(ABC),
        "Affinity": repository.load(AFFINITY),
        "C&TBSA": repository.load(CTBSA),
    }
    project = RmfMapService().load_project(MAP)
    affinity_service = AffinityService(CACHE)
    affinity_analysis = affinity_service.analyze(
        affinity_service.load_orders(ORDERS)
    )
    traffic = TrafficAwareSlottingService()
    network = traffic.network_from_rmf(project.to_building_dict())
    route_cache = traffic._shortest_routes(network)
    results: dict[str, dict] = {}
    racks: dict[str, dict] = {}
    abc_metrics, abc_racks, abc_analysis = analyze_strategy(
        "ABC baseline", payloads["ABC baseline"], affinity_analysis,
        traffic, network, route_cache, None,
    )
    results["ABC baseline"] = abc_metrics
    racks["ABC baseline"] = abc_racks
    reference = float(abc_analysis.metrics["relative_reference"])
    for name in ("Affinity", "C&TBSA"):
        metrics, rack_values, _analysis = analyze_strategy(
            name, payloads[name], affinity_analysis, traffic, network,
            route_cache, reference,
        )
        results[name] = metrics
        racks[name] = rack_values

    abc_positions = {
        load_id(row): position(row) for row in assigned(payloads["ABC baseline"])
    }
    abc_rows = {load_id(row): row for row in assigned(payloads["ABC baseline"])}
    all_racks = sorted(set().union(*(set(value) for value in racks.values())))
    rack_rows = []
    for rack in all_racks:
        row = {"rack_id": rack}
        for name, prefix in (
            ("ABC baseline", "abc"), ("Affinity", "affinity"), ("C&TBSA", "ctbsa")
        ):
            state = racks[name].get(rack)
            row.update({
                f"{prefix}_present": bool(state),
                f"{prefix}_skus": "|".join(sorted(state["skus"])) if state else "",
                f"{prefix}_sku_count": len(state["skus"]) if state else 0,
                f"{prefix}_load_count": len(state["loads"]) if state else 0,
                f"{prefix}_weight_kg": state["weight"] if state else 0.0,
                f"{prefix}_visit_load": state["visit_load"] if state else 0.0,
                f"{prefix}_occupied_slots": state["slot_count"] if state else 0,
            })
        for name, prefix in (("Affinity", "affinity"), ("C&TBSA", "ctbsa")):
            row[f"{prefix}_sku_set_changed_vs_abc"] = (
                racks[name].get(rack, {}).get("skus", set())
                != abc_racks.get(rack, {}).get("skus", set())
            )
            row[f"{prefix}_load_set_changed_vs_abc"] = (
                racks[name].get(rack, {}).get("loads", set())
                != abc_racks.get(rack, {}).get("loads", set())
            )
        rack_rows.append(row)

    for name in ("Affinity", "C&TBSA"):
        rows = assigned(payloads[name])
        positions = {load_id(row): position(row) for row in rows}
        current_rows = {load_id(row): row for row in rows}
        changed_loads = {
            key for key in set(abc_positions) | set(positions)
            if abc_positions.get(key) != positions.get(key)
        }
        changed_racks = set()
        for key in changed_loads:
            for source in (abc_rows.get(key), current_rows.get(key)):
                if source and source.get("rack_id"):
                    changed_racks.add(str(source["rack_id"]))
        results[name]["loads_with_different_position_vs_abc"] = len(changed_loads)
        results[name]["racks_affected_by_position_changes_vs_abc"] = len(changed_racks)
        results[name]["racks_with_different_sku_set_vs_abc"] = sum(
            racks[name].get(rack, {}).get("skus", set())
            != abc_racks.get(rack, {}).get("skus", set())
            for rack in all_racks
        )
        results[name]["racks_with_different_load_set_vs_abc"] = sum(
            racks[name].get(rack, {}).get("loads", set())
            != abc_racks.get(rack, {}).get("loads", set())
            for rack in all_racks
        )
    results["ABC baseline"].update({
        "loads_with_different_position_vs_abc": 0,
        "racks_affected_by_position_changes_vs_abc": 0,
        "racks_with_different_sku_set_vs_abc": 0,
        "racks_with_different_load_set_vs_abc": 0,
    })

    delta_keys = (
        "occupied_racks", "expected_travel", "peak_normalized_resource_load",
        "p95_normalized_resource_load", "rack_weight_kg_average",
        "rack_weight_kg_maximum", "rack_visit_load_cv", "total_rack_touches",
        "average_racks_per_group",
    )
    for name, metrics in results.items():
        for key in delta_keys:
            value = float(metrics[key])
            baseline = float(abc_metrics[key])
            metrics[f"{key}_delta_vs_abc"] = value - baseline
            metrics[f"{key}_pct_vs_abc"] = pct_delta(value, baseline)

    SUMMARY_JSON.write_text(json.dumps({
        "baseline": "ABC baseline",
        "definitions": {
            "rack_position_change": (
                "A rack is affected when it is the source or destination of at least "
                "one inventory_load_id whose rack/level/slot footprint differs from ABC."
            ),
            "rack_sku_set_change": (
                "The distinct logical SKU set assigned to that rack differs from ABC."
            ),
            "traffic_comparison": (
                "All layouts use the same order workbook, movement network, alternative-"
                "replica routing policy, and ABC relative-load normalization reference."
            ),
        },
        "inputs": {"map": str(MAP), "orders": str(ORDERS)},
        "strategies": list(results.values()),
    }, indent=2) + "\n", encoding="utf-8")

    fields = list(results["ABC baseline"].keys())
    with SUMMARY_CSV.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(
            stream, fieldnames=fields, extrasaction="ignore", lineterminator="\n"
        )
        writer.writeheader()
        writer.writerows(results.values())
    rack_fields = list(rack_rows[0].keys()) if rack_rows else ["rack_id"]
    with RACK_CSV.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=rack_fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rack_rows)

    def value(name: str, key: str, digits: int = 2) -> str:
        raw = results[name][key]
        if isinstance(raw, int):
            return f"{raw:,}"
        return f"{float(raw):,.{digits}f}"

    lines = [
        "# Slotting strategy comparison (ABC baseline)", "",
        "All three layouts use `map1`, the same quantity/attribute inputs, and the same order history. "
        "Affinity uses the UI default 50% weight with automatic map/workbook tuning. Traffic is "
        "recomputed consistently for every strategy; lower travel, peak/P95 load, and rack-load CV are better.",
        "",
        "| Metric | ABC baseline | Affinity | C&TBSA |", "|---|---:|---:|---:|",
    ]
    table_metrics = (
        ("Occupied racks", "occupied_racks", 0),
        ("Loads moved vs ABC", "loads_with_different_position_vs_abc", 0),
        ("Racks affected by moved loads", "racks_affected_by_position_changes_vs_abc", 0),
        ("Racks with different SKU set", "racks_with_different_sku_set_vs_abc", 0),
        ("Expected travel", "expected_travel", 2),
        ("Peak normalized resource load", "peak_normalized_resource_load", 4),
        ("P95 normalized resource load", "p95_normalized_resource_load", 4),
        ("Average rack weight (kg)", "rack_weight_kg_average", 3),
        ("Maximum rack weight (kg)", "rack_weight_kg_maximum", 3),
        ("Rack visit-load CV", "rack_visit_load_cv", 4),
        ("Total rack touches", "total_rack_touches", 0),
        ("Average racks per fulfillment group", "average_racks_per_group", 4),
    )
    for label, key, digits in table_metrics:
        lines.append("| " + label + " | " + " | ".join(
            value(name, key, digits) for name in results
        ) + " |")
    lines += [
        "", "## Interpretation", "",
        "- A ‘rack affected by moved loads’ is a source or destination rack for a load whose rack, level, slot, or oversize footprint changed from ABC; rack map coordinates are unchanged.",
        "- ‘Different SKU set’ compares distinct logical SKUs per rack. The rack-detail CSV also reports load-set changes, weight, visits, and occupied slots.",
        "- C&TBSA preserves its original shelf-load-balancing objective while treating separate quantity loads of one SKU as independent assignable items.",
        "", "## Generated files", "",
        f"- Affinity layout: `{AFFINITY.relative_to(ROOT)}`",
        f"- Machine-readable summary: `{SUMMARY_JSON.relative_to(ROOT)}`",
        f"- Summary CSV: `{SUMMARY_CSV.relative_to(ROOT)}`",
        f"- Rack-detail CSV: `{RACK_CSV.relative_to(ROOT)}`",
    ]
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--skip-affinity-generation", action="store_true",
        help="Compare existing layouts without regenerating the Affinity layout.",
    )
    args = parser.parse_args()
    if not args.skip_affinity_generation:
        generate_affinity()
    results = compare()
    for name, metrics in results.items():
        print(
            f"{name}: racks={metrics['occupied_racks']}, "
            f"travel={metrics['expected_travel']:.2f}, "
            f"peak={metrics['peak_normalized_resource_load']:.4f}, "
            f"rack_weight_max={metrics['rack_weight_kg_maximum']:.3f} kg"
        )


if __name__ == "__main__":
    main()
