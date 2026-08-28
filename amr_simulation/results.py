"""CSV and JSON result exports."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from statistics import fmean

from .models import DayResult, SimulationConfig, ValidationReport, WorkloadTask


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _csv_value(value):
    return json.dumps(value, sort_keys=True) if isinstance(value, (dict, list)) else value


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = list(rows[0])
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(
            {key: _csv_value(row.get(key, "")) for key in fields} for row in rows
        )


def daily_row(result: DayResult, workstations: tuple[str, ...]) -> dict:
    metrics = result.metrics
    row = {
        key: value
        for key, value in metrics.items()
        if key not in {"workstations", "amrs"}
    }
    for station in workstations:
        values = metrics["workstations"][station]
        prefix = station.lower()
        row[f"{prefix}_utilization"] = values["utilization"]
        row[f"{prefix}_queue_wait_seconds"] = values["queue_wait_seconds"]
        row[f"{prefix}_max_queue"] = values["max_queue"]
    return row


def summarize(layout_name: str, results: list[DayResult]) -> dict:
    throughputs = [result.metrics["line_throughput_per_hour"] for result in results]
    total_lines = sum(result.metrics["completed_lines"] for result in results)
    total_hours = sum(result.metrics["makespan_hours"] for result in results)
    return {
        "layout": layout_name,
        "days": len(results),
        "total_completed_lines": total_lines,
        "total_tasks": sum(result.metrics["completed_tasks"] for result in results),
        "total_rack_presentations": sum(result.metrics["rack_presentations"] for result in results),
        "total_travel_distance_m": sum(result.metrics["travel_distance_m"] for result in results),
        "total_station_queue_time_seconds": sum(result.metrics["station_queue_time_seconds"] for result in results),
        "total_node_reservation_wait_seconds": sum(
            result.metrics["node_reservation_wait_seconds"] for result in results
        ),
        "total_reservation_conflicts": sum(
            result.metrics["reservation_conflicts"] for result in results
        ),
        "total_node_ownership_conflicts": sum(
            result.metrics["node_ownership_conflicts"] for result in results
        ),
        "total_dram_solver_conflicts": sum(
            result.metrics["dram_solver_conflicts"] for result in results
        ),
        "total_reservation_reroutes": sum(
            result.metrics["reservation_reroutes"] for result in results
        ),
        "total_dram_solver_reroutes": sum(
            result.metrics["dram_solver_reroutes"] for result in results
        ),
        "total_dram_conflict_wait_seconds": sum(
            result.metrics["dram_conflict_wait_seconds"] for result in results
        ),
        "total_loaded_priority_grants": sum(
            result.metrics["loaded_priority_grants"] for result in results
        ),
        "total_loaded_protected_waits": sum(
            result.metrics["loaded_protected_waits"] for result in results
        ),
        "total_following_wait_seconds": sum(
            result.metrics["following_wait_seconds"] for result in results
        ),
        "total_following_avoided_reroutes": sum(
            result.metrics["following_avoided_reroutes"] for result in results
        ),
        "total_wait_for_cycles": sum(result.metrics["wait_for_cycles"] for result in results),
        "total_cycle_breaking_reroutes": sum(
            result.metrics["cycle_breaking_reroutes"] for result in results
        ),
        "total_corridor_conflicts": sum(result.metrics["corridor_conflicts"] for result in results),
        "total_corridor_ownership_changes": sum(
            result.metrics["corridor_ownership_changes"] for result in results
        ),
        "total_corridor_yielding_amrs": sum(
            result.metrics["corridor_yielding_amrs"] for result in results
        ),
        "total_corridor_wait_seconds": sum(
            result.metrics["corridor_wait_seconds"] for result in results
        ),
        "mean_daily_throughput_lines_per_hour": fmean(throughputs),
        "weighted_throughput_lines_per_hour": total_lines / total_hours if total_hours else 0.0,
        "mean_amr_utilization": fmean(result.metrics["amr_utilization"] for result in results),
        "model_limitations": [
            "Node ownership is modeled without a time-expanded reservation table.",
            "Inventory depletion and quantity-dependent service time are not modeled.",
        ],
    }


def export_layout_results(
    directory: Path,
    layout_name: str,
    results: list[DayResult],
    config: SimulationConfig,
    mapping: dict[str, str],
    tasks: list[WorkloadTask],
    report: ValidationReport,
    event_log: bool,
) -> dict:
    directory.mkdir(parents=True, exist_ok=True)
    write_csv(directory / "daily_metrics.csv", [daily_row(result, config.workstations) for result in results])
    summary = summarize(layout_name, results)
    write_json(directory / "summary.json", summary)
    write_json(directory / "config_snapshot.json", config.snapshot())
    workloads = {
        store: sum(task.source_lines for task in tasks if task.store_id == store)
        for store in sorted(mapping)
    }
    write_json(
        directory / "store_workstation_mapping.json",
        {
            "mapping": mapping,
            "historical_line_workload": workloads,
            "date_start": min(task.task_date for task in tasks).isoformat(),
            "date_end": max(task.task_date for task in tasks).isoformat(),
        },
    )
    write_json(directory / "validation_report.json", report.to_dict())
    if event_log:
        rows = []
        for result in results:
            rows.extend({"date": result.metrics["date"], **event} for event in result.events)
        write_csv(directory / "event_log.csv", rows)
    return summary
