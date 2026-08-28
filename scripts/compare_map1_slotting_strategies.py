#!/usr/bin/env python3
"""Generate and compare four reproducible slotting strategies on map1."""

from __future__ import annotations

import copy
import csv
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from warehouse_layout.affinity import AffinityService
from warehouse_layout.ctbsa import CtbsaParameters
from warehouse_layout.rmf import RmfMapService
from warehouse_layout.slotting import SlottingService
from warehouse_layout.slotting_repository import SlottingLayoutRepository
from warehouse_layout.traffic import TrafficAwareSlottingService


MAP = ROOT / "resources/map/map1_1.grid.json"
VELOCITY = ROOT / "resources/data/sku_velocity_output/sku_velocity_summary.csv"
ATTRIBUTES = ROOT / "resources/data/medicine_sku_attributes.csv"
STOCK = ROOT / "resources/data/minimum_stock_requirements.csv"
ORDERS = ROOT / "resources/data/Sample Data.xlsx"
CACHE = ROOT / "resources/data/sku_affinity_cache"

LAYOUT_DIR = ROOT / "resources/map"
RESULT_DIR = ROOT / "resources/slotting_strategy_comparison"
LAYOUTS = {
    "Basic": LAYOUT_DIR / "map1_basic.slotting.json",
    "Pure affinity": LAYOUT_DIR / "map1_pure_affinity.slotting.json",
    "Traffic aware - zone balance off": (
        LAYOUT_DIR / "map1_traffic_zone_balance_off.slotting.json"
    ),
    "Traffic aware - zone balance on": (
        LAYOUT_DIR / "map1_traffic_zone_balance_on.slotting.json"
    ),
}

STRATEGY_ORDER = tuple(LAYOUTS)
STRATEGY_COLORS = ("#4C78A8", "#F58518", "#54A24B", "#B279A2")


def progress(current: int, total: int, message: str) -> None:
    print(f"[{current}/{total}] {message}", flush=True)


def assigned_rows(payload: dict) -> list[dict]:
    return [
        row for row in payload.get("assignments", [])
        if row.get("assignment_status") == "ASSIGNED" and row.get("rack_id")
    ]


def distribution(values: list[float], prefix: str) -> dict[str, float]:
    return {
        f"{prefix}_min": min(values, default=0.0),
        f"{prefix}_max": max(values, default=0.0),
        f"{prefix}_average": statistics.fmean(values) if values else 0.0,
    }


def load_inputs():
    project = RmfMapService().load_project(MAP)
    slotting = SlottingService()
    slotting.attributes.set_standard_storage_defaults(
        project.warehouse_storage_defaults
    )
    skus = slotting.load_velocity(VELOCITY, project.attribute_catalog, ATTRIBUTES)
    with STOCK.open(newline="", encoding="utf-8-sig") as stream:
        skus = slotting.apply_stock_requirements(skus, list(csv.DictReader(stream)))
    affinity_service = AffinityService(CACHE)
    affinity_analysis = affinity_service.analyze(
        affinity_service.load_orders(ORDERS, progress=progress)
    )
    return project, slotting, skus, affinity_analysis


def save_initial_layout(
    repository: SlottingLayoutRepository,
    project,
    rows: list[dict],
    summary: dict,
    destination: Path,
    *,
    strategy: str,
    source_affinity: str = "",
) -> None:
    storage_layout = project.storage_layout
    repository.save(
        rows,
        project.to_building_dict(),
        summary,
        destination,
        strategy=strategy,
        handling_unit_type=storage_layout.handling_unit_type,
        levels_per_rack=storage_layout.levels_per_rack,
        slots_per_level=storage_layout.slots_per_level,
        zone_assignments=summary.get("zone_assignments", project.zone_assignments),
        attribute_catalog=project.attribute_catalog,
        location_attributes=summary.get(
            "location_attributes", project.location_attributes
        ),
        source_grid_project=str(MAP.resolve()),
        source_velocity=str(VELOCITY.resolve()),
        source_chilled=str(ATTRIBUTES.resolve()),
        source_affinity=source_affinity,
        affinity_configuration=summary.get("affinity_tuning", {}),
        standard_storage_defaults=project.warehouse_storage_defaults,
        storage_layout=storage_layout,
    )


def generate_layouts() -> tuple[dict[str, dict], object, object]:
    project, slotting, skus, affinity_analysis = load_inputs()
    repository = SlottingLayoutRepository()
    storage = project.storage_layout
    building = project.to_building_dict()

    print("Generating Basic layout", flush=True)
    basic_rows, basic_summary = slotting.generate_basic(
        copy.deepcopy(building),
        copy.deepcopy(skus),
        storage.levels_per_rack,
        storage.slots_per_level,
        storage.handling_unit_type,
        "Z01",
        project.zone_assignments,
        project.attribute_catalog,
        copy.deepcopy(project.location_attributes),
        storage_layout=storage,
    )
    save_initial_layout(
        repository, project, basic_rows, basic_summary, LAYOUTS["Basic"],
        strategy="basic",
    )
    if basic_summary.get("unassigned_count"):
        raise RuntimeError(
            f"Basic layout has {basic_summary['unassigned_count']} unassigned loads"
        )

    print("Generating Pure affinity layout (affinity weight = 100%)", flush=True)
    affinity_rows, affinity_summary = slotting.generate_abc_affinity(
        copy.deepcopy(building),
        copy.deepcopy(skus),
        affinity_analysis,
        1.0,
        storage.levels_per_rack,
        storage.slots_per_level,
        storage.handling_unit_type,
        "Z01",
        project.zone_assignments,
        project.attribute_catalog,
        copy.deepcopy(project.location_attributes),
        tuning_parameters=None,
        storage_layout=storage,
    )
    save_initial_layout(
        repository,
        project,
        affinity_rows,
        affinity_summary,
        LAYOUTS["Pure affinity"],
        strategy="pure_affinity",
        source_affinity=str(ORDERS.resolve()),
    )
    if affinity_summary.get("unassigned_count"):
        raise RuntimeError(
            "Pure affinity layout has "
            f"{affinity_summary['unassigned_count']} unassigned loads"
        )

    payloads = {
        "Basic": repository.load(LAYOUTS["Basic"]),
        "Pure affinity": repository.load(LAYOUTS["Pure affinity"]),
    }
    traffic = TrafficAwareSlottingService()
    network = traffic.network_from_rmf(building)
    parameters = CtbsaParameters(
        population_size=10,
        generations=5,
        random_seed=0,
        selected_solution=3,
        extended_selected_solution=None,
    )
    for label, zone_enabled in (
        ("Traffic aware - zone balance off", False),
        ("Traffic aware - zone balance on", True),
    ):
        print(f"Generating {label}", flush=True)
        pipeline = traffic.run_existing_layout(
            payloads["Basic"],
            affinity_analysis,
            network,
            ctbsa_parameters=parameters,
            zone_workload_enabled=zone_enabled,
            baseline_path=str(LAYOUTS["Basic"].resolve()),
            source_orders=str(ORDERS.resolve()),
            progress=progress,
        )
        LAYOUTS[label].write_text(
            json.dumps(pipeline.output_payload, indent=2) + "\n",
            encoding="utf-8",
        )
        payloads[label] = pipeline.output_payload
    return payloads, affinity_analysis, network


def rack_metrics(rows: list[dict], demand) -> dict[str, dict]:
    states: dict[str, dict] = defaultdict(
        lambda: {"skus": set(), "loads": set(), "workload": 0.0}
    )
    unit_to_rack = {}
    for row in rows:
        rack = str(row["rack_id"])
        states[rack]["skus"].add(str(row.get("sku", "")))
        states[rack]["loads"].add(
            str(row.get("inventory_load_id") or row.get("sku", ""))
        )
        unit = str(row.get("handling_unit_id", ""))
        if unit:
            unit_to_rack[unit] = rack
    for unit, visits in demand.unit_visits.items():
        rack = unit_to_rack.get(str(unit))
        if rack:
            states[rack]["workload"] += float(visits)
    return dict(states)


def buffer_utilization(payload: dict, rows: list[dict]) -> tuple[int, int, float]:
    buffers = payload.get("buffers") or (
        (payload.get("storage_layout") or {}).get("buffers") or []
    )
    total = len(buffers)
    occupied_ids = {
        str(buffer_id)
        for row in rows
        for buffer_id in (row.get("occupied_buffer_ids") or [])
        if str(buffer_id)
    }
    occupied = len(occupied_ids)
    return occupied, total, (100.0 * occupied / total if total else 0.0)


def calculate_comparison(
    payloads: dict[str, dict], affinity_analysis, network
) -> tuple[list[dict], list[dict], list[dict], dict]:
    service = TrafficAwareSlottingService()
    route_cache = service._shortest_routes(network)
    comparison_rows = []
    all_rack_rows = []
    all_zone_rows = []
    analyses = {}
    reference = None
    for strategy in STRATEGY_ORDER:
        payload = payloads[strategy]
        rows = assigned_rows(payload)
        demand = service.build_demand(
            affinity_analysis.dataset, rows, network=network
        )
        analysis = service.analyze(
            rows,
            network,
            demand,
            route_cache=route_cache,
            relative_reference=reference,
        )
        if reference is None:
            reference = float(analysis.metrics["relative_reference"])
        analysis.zone_analysis = service.analyze_zones(rows, analysis, payload)
        analyses[strategy] = analysis

        racks = rack_metrics(rows, demand)
        rack_workloads = [state["workload"] for state in racks.values()]
        sku_counts = [len(state["skus"]) for state in racks.values()]
        zone_rows = analysis.zone_analysis.get("zones", [])
        zone_workloads = [float(row["expected_visits"]) for row in zone_rows]
        normalized_zone_workloads = [
            float(row["normalized_demand_workload"]) for row in zone_rows
        ]
        occupied_buffers, buffer_count, buffer_rate = buffer_utilization(
            payload, rows
        )
        available_racks = int((payload.get("summary") or {}).get("rack_count") or 0)
        if not available_racks:
            _level, available, _workstations, _unreachable = (
                service.slotting.rack_distances(payload["building"])
            )
            available_racks = len(available)
        task_count = int(demand.fulfillment_groups)
        total_travel = float(analysis.metrics["expected_travel"])
        summary = payload.get("summary") or {}
        comparison = {
            "strategy": strategy,
            "layout_file": str(LAYOUTS[strategy].relative_to(ROOT)),
            "assigned_loads": len(rows),
            "unassigned_loads": int(summary.get("unassigned_count") or 0),
            "available_racks": available_racks,
            "used_racks": len(racks),
            "rack_use_percentage": 100.0 * len(racks) / available_racks,
            **distribution(rack_workloads, "rack_workload_expected_visits"),
            **distribution(zone_workloads, "zone_workload_expected_visits"),
            **distribution(
                normalized_zone_workloads,
                "normalized_zone_workload_expected_visits_per_slot",
            ),
            "average_distinct_skus_per_used_rack": (
                statistics.fmean(sku_counts) if sku_counts else 0.0
            ),
            "occupied_buffers": occupied_buffers,
            "available_buffers": buffer_count,
            "buffer_utilization_percentage": buffer_rate,
            "fulfillment_tasks": task_count,
            "expected_travel_distance_total": total_travel,
            "expected_travel_distance_per_task": (
                total_travel / task_count if task_count else 0.0
            ),
        }
        comparison_rows.append(comparison)
        for rack_id, state in sorted(racks.items()):
            all_rack_rows.append({
                "strategy": strategy,
                "rack_id": rack_id,
                "workload_expected_visits": state["workload"],
                "distinct_sku_count": len(state["skus"]),
                "inventory_load_count": len(state["loads"]),
            })
        for zone in zone_rows:
            all_zone_rows.append({
                "strategy": strategy,
                "zone_id": zone["zone_id"],
                "usable_racks": zone["usable_racks"],
                "usable_slots": zone["usable_slots"],
                "occupied_racks": zone["occupied_rack_count"],
                "distinct_skus": zone["sku_count"],
                "workload_expected_visits": zone["expected_visits"],
                "normalized_workload_expected_visits_per_slot": (
                    zone["normalized_demand_workload"]
                ),
                "attributed_resource_flow": zone["attributed_resource_flow"],
                "normalized_traffic_workload": (
                    zone["normalized_traffic_workload"]
                ),
            })
    return comparison_rows, all_rack_rows, all_zone_rows, analyses


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0]) if rows else []
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def grouped_bars(ax, values, title, ylabel, *, groups=None, percent=False):
    x = np.arange(len(STRATEGY_ORDER))
    if groups:
        width = 0.22
        offsets = np.arange(len(groups)) - (len(groups) - 1) / 2
        for offset, (label, key) in zip(offsets, groups):
            ax.bar(x + offset * width, [row[key] for row in values], width, label=label)
        ax.legend(frameon=False, fontsize=8)
    else:
        ax.bar(x, values, color=STRATEGY_COLORS)
    ax.set_title(title, fontsize=11, weight="bold")
    ax.set_ylabel(ylabel)
    ax.set_xticks(x, ("Basic", "Affinity", "Traffic\nzone off", "Traffic\nzone on"))
    ax.grid(axis="y", alpha=0.25)
    if percent:
        ax.set_ylim(0, 100)


def write_overview_figure(comparison: list[dict]) -> None:
    fig, axes = plt.subplots(2, 3, figsize=(16, 9), constrained_layout=True)
    grouped_bars(
        axes[0, 0], [row["rack_use_percentage"] for row in comparison],
        "Rack use", "% of available racks", percent=True,
    )
    grouped_bars(
        axes[0, 1], comparison, "Rack workload", "Expected visits",
        groups=(
            ("Min", "rack_workload_expected_visits_min"),
            ("Average", "rack_workload_expected_visits_average"),
            ("Max", "rack_workload_expected_visits_max"),
        ),
    )
    grouped_bars(
        axes[0, 2], comparison, "Normalized zone workload",
        "Expected visits / usable slot",
        groups=(
            ("Min", "normalized_zone_workload_expected_visits_per_slot_min"),
            ("Average", "normalized_zone_workload_expected_visits_per_slot_average"),
            ("Max", "normalized_zone_workload_expected_visits_per_slot_max"),
        ),
    )
    grouped_bars(
        axes[1, 0],
        [row["average_distinct_skus_per_used_rack"] for row in comparison],
        "SKU density", "Average distinct SKUs / used rack",
    )
    grouped_bars(
        axes[1, 1],
        [row["buffer_utilization_percentage"] for row in comparison],
        "Buffer utilization", "% of buffers", percent=True,
    )
    grouped_bars(
        axes[1, 2],
        [row["expected_travel_distance_per_task"] for row in comparison],
        "Expected travel", "Distance per fulfillment task",
    )
    fig.suptitle("map1_1 slotting strategy comparison", fontsize=16, weight="bold")
    fig.savefig(RESULT_DIR / "strategy_comparison_overview.png", dpi=180)
    fig.savefig(RESULT_DIR / "strategy_comparison_overview.svg")
    plt.close(fig)


def write_distribution_figure(rack_rows: list[dict], zone_rows: list[dict]) -> None:
    rack_values = [
        [row["workload_expected_visits"] for row in rack_rows if row["strategy"] == strategy]
        for strategy in STRATEGY_ORDER
    ]
    zone_values = [
        [
            row["normalized_workload_expected_visits_per_slot"]
            for row in zone_rows if row["strategy"] == strategy
        ]
        for strategy in STRATEGY_ORDER
    ]
    labels = ("Basic", "Pure affinity", "Traffic zone off", "Traffic zone on")
    fig, axes = plt.subplots(1, 2, figsize=(15, 6), constrained_layout=True)
    for ax, values, title, ylabel in (
        (axes[0], rack_values, "Rack workload distribution", "Expected visits"),
        (
            axes[1], zone_values, "Normalized zone workload distribution",
            "Expected visits / usable slot",
        ),
    ):
        boxes = ax.boxplot(values, tick_labels=labels, patch_artist=True, showmeans=True)
        for patch, color in zip(boxes["boxes"], STRATEGY_COLORS):
            patch.set_facecolor(color)
            patch.set_alpha(0.65)
        ax.set_title(title, weight="bold")
        ax.set_ylabel(ylabel)
        ax.tick_params(axis="x", rotation=18)
        ax.grid(axis="y", alpha=0.25)
    fig.savefig(RESULT_DIR / "workload_distributions.png", dpi=180)
    fig.savefig(RESULT_DIR / "workload_distributions.svg")
    plt.close(fig)


def write_rack_map_figure(payloads: dict[str, dict], analyses: dict) -> None:
    coordinates = {}
    for rack in TrafficAwareSlottingService().slotting.rack_distances(
        payloads["Basic"]["building"]
    )[1]:
        coordinates[str(rack["rack_id"])] = (float(rack["x"]), float(rack["y"]))
    workloads = {}
    global_max = 0.0
    for strategy in STRATEGY_ORDER:
        rows = assigned_rows(payloads[strategy])
        state = rack_metrics(rows, analyses[strategy].demand)
        workloads[strategy] = {rack: values["workload"] for rack, values in state.items()}
        global_max = max(global_max, max(workloads[strategy].values(), default=0.0))
    fig, axes = plt.subplots(2, 2, figsize=(15, 11), constrained_layout=True)
    scatter = None
    for ax, strategy in zip(axes.flat, STRATEGY_ORDER):
        occupied = workloads[strategy]
        empty_x, empty_y, used_x, used_y, colors = [], [], [], [], []
        for rack, (x, y) in coordinates.items():
            if rack in occupied:
                used_x.append(x)
                used_y.append(y)
                colors.append(occupied[rack])
            else:
                empty_x.append(x)
                empty_y.append(y)
        ax.scatter(empty_x, empty_y, s=8, c="#D9D9D9", marker="s", linewidths=0)
        scatter = ax.scatter(
            used_x, used_y, s=18, c=colors, cmap="viridis", marker="s",
            vmin=0, vmax=global_max, linewidths=0,
        )
        ax.set_title(strategy, weight="bold")
        ax.set_aspect("equal", adjustable="box")
        ax.set_xlabel("Map X")
        ax.set_ylabel("Map Y")
        ax.grid(alpha=0.15)
    if scatter is not None:
        fig.colorbar(scatter, ax=axes, label="Expected rack visits", shrink=0.82)
    fig.suptitle("map1_1 occupied-rack workload maps", fontsize=16, weight="bold")
    fig.savefig(RESULT_DIR / "rack_workload_maps.png", dpi=180)
    fig.savefig(RESULT_DIR / "rack_workload_maps.svg")
    plt.close(fig)


def write_metadata(comparison: list[dict]) -> None:
    metadata = {
        "map": str(MAP.relative_to(ROOT)),
        "inputs": {
            "velocity": str(VELOCITY.relative_to(ROOT)),
            "sku_attributes": str(ATTRIBUTES.relative_to(ROOT)),
            "stock_requirements": str(STOCK.relative_to(ROOT)),
            "orders": str(ORDERS.relative_to(ROOT)),
        },
        "definitions": {
            "rack_workload": (
                "Store ID + Date fulfillment-task handling-unit visits, summed by "
                "occupied rack; zero-demand occupied racks are included."
            ),
            "zone_workload": (
                "Rack workload summed by zone. Normalized workload divides expected "
                "visits by the zone's compatible usable rack-slot capacity."
            ),
            "buffer_utilization": (
                "Distinct occupied storage buffers divided by map storage buffers."
            ),
            "expected_travel_per_task": (
                "Total endpoint-weighted route distance divided by Store ID + Date "
                "fulfillment groups."
            ),
        },
        "traffic_configuration": {
            "baseline": "Basic",
            "population_size": 10,
            "generations": 5,
            "random_seed": 0,
            "selected_solution": 3,
            "zone_on_extended_selection": "automatic normalized knee",
        },
        "strategies": comparison,
    }
    (RESULT_DIR / "comparison_metadata.json").write_text(
        json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
    )


def main() -> None:
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    payloads, affinity_analysis, network = generate_layouts()
    comparison, rack_rows, zone_rows, analyses = calculate_comparison(
        payloads, affinity_analysis, network
    )
    write_csv(RESULT_DIR / "strategy_comparison.csv", comparison)
    write_csv(RESULT_DIR / "rack_workload_detail.csv", rack_rows)
    write_csv(RESULT_DIR / "zone_workload_detail.csv", zone_rows)
    write_overview_figure(comparison)
    write_distribution_figure(rack_rows, zone_rows)
    write_rack_map_figure(payloads, analyses)
    write_metadata(comparison)
    print(json.dumps(comparison, indent=2), flush=True)


if __name__ == "__main__":
    main()
