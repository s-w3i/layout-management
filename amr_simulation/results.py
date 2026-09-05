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


def append_csv_row(path: Path, row: dict) -> None:
    """Append one fixed-schema row, creating the CSV header when needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists() or path.stat().st_size == 0
    fields = list(row)
    if not new_file:
        with path.open(newline="", encoding="utf-8") as stream:
            reader = csv.reader(stream)
            existing_fields = next(reader)
            existing_rows = list(reader)
        if existing_fields != fields:
            normalized = [
                dict(zip(existing_fields, values)) for values in existing_rows
            ]
            with path.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                writer.writerows(
                    {key: values.get(key, "") for key in fields}
                    for values in normalized
                )
    with path.open("a", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        if new_file:
            writer.writeheader()
        writer.writerow({key: _csv_value(value) for key, value in row.items()})


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


def summarize_metrics(layout_name: str, metrics: list[dict]) -> dict:
    throughputs = [item["line_throughput_per_hour"] for item in metrics]
    total_lines = sum(item["completed_lines"] for item in metrics)
    total_hours = sum(item["makespan_hours"] for item in metrics)
    statuses = [item.get("coordination_status", "VALID") for item in metrics]
    status = (
        "FAILED_DEADLOCK" if "FAILED_DEADLOCK" in statuses
        else "DEGRADED" if "DEGRADED" in statuses else "VALID"
    )
    coordination_rows = [item.get("coordination", {}) for item in metrics]
    conflict_kinds = ("path_overlap", "head_to_head", "partial_cycle", "wait_for_deadlock", "corridor_deadlock")
    coordination = {
        "profile": next((item.get("profile") for item in coordination_rows if item), "legacy_v1"),
        "status": status,
        **{
            key: sum(item.get(key, 0) for item in coordination_rows)
            for key in (
                "allocation_epoch_count", "reservation_request_count",
                "reservation_grant_count", "reservation_denial_count",
                "frontier_stop_count", "frontier_wait_seconds",
                "acknowledgement_delay_seconds", "replan_requested_count",
                "replan_completed_count", "replan_failed_count",
                "tabu_edge_count", "stale_event_count", "deadlock_count",
            )
        },
        "max_granted_window_nodes": max(
            (item.get("max_granted_window_nodes", 0) for item in coordination_rows),
            default=0,
        ),
        "conflicts": {
            kind: sum(item.get("conflicts", {}).get(kind, 0) for item in coordination_rows)
            for kind in conflict_kinds
        },
        "serialization_fallback_used": any(
            item.get("serialization_fallback_used", False) for item in coordination_rows
        ),
    }
    grants = coordination["reservation_grant_count"]
    coordination["mean_granted_window_nodes"] = (
        sum(
            item.get("mean_granted_window_nodes", 0.0)
            * item.get("reservation_grant_count", 0)
            for item in coordination_rows
        ) / grants if grants else 0.0
    )
    return {
        "layout": layout_name,
        "coordination_status": status,
        "eligible_for_comparison": status == "VALID",
        "coordination": coordination,
        "days": len(metrics),
        "total_completed_lines": total_lines,
        "total_tasks": sum(item["completed_tasks"] for item in metrics),
        "total_rack_presentations": sum(item["rack_presentations"] for item in metrics),
        "total_travel_distance_m": sum(item["travel_distance_m"] for item in metrics),
        "total_station_queue_time_seconds": sum(item["station_queue_time_seconds"] for item in metrics),
        "total_node_reservation_wait_seconds": sum(
            item["node_reservation_wait_seconds"] for item in metrics
        ),
        "total_reservation_conflicts": sum(
            item["reservation_conflicts"] for item in metrics
        ),
        "total_node_ownership_conflicts": sum(
            item["node_ownership_conflicts"] for item in metrics
        ),
        "total_dram_solver_conflicts": sum(
            item["dram_solver_conflicts"] for item in metrics
        ),
        "total_reservation_reroutes": sum(
            item["reservation_reroutes"] for item in metrics
        ),
        "total_dram_solver_reroutes": sum(
            item["dram_solver_reroutes"] for item in metrics
        ),
        "total_dram_conflict_wait_seconds": sum(
            item["dram_conflict_wait_seconds"] for item in metrics
        ),
        "total_coordination_fallbacks": sum(
            item.get("coordination_fallback_count", 0) for item in metrics
        ),
        "mean_daily_throughput_lines_per_hour": fmean(throughputs),
        "weighted_throughput_lines_per_hour": total_lines / total_hours if total_hours else 0.0,
        "mean_amr_utilization": fmean(item["amr_utilization"] for item in metrics),
        "model_limitations": [
            "Node ownership is modeled without a time-expanded reservation table.",
            "Inventory depletion and quantity-dependent service time are not modeled.",
            "Repeated coordination states use serialized recovery and are counted explicitly.",
        ],
    }


def summarize(layout_name: str, results: list[DayResult]) -> dict:
    return summarize_metrics(layout_name, [result.metrics for result in results])


def export_layout_summary(
    directory: Path,
    layout_name: str,
    metrics: list[dict],
    config: SimulationConfig,
    mapping: dict[str, str],
    tasks: list[WorkloadTask],
    report: ValidationReport,
) -> dict:
    summary = summarize_metrics(layout_name, metrics)
    deadlock_files = [
        f"deadlock_snapshot_{item['date']}.json"
        for item in metrics
        if item.get("coordination_status") in {"DEGRADED", "FAILED_DEADLOCK"}
    ]
    if deadlock_files:
        summary["deadlock_snapshots"] = deadlock_files
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
    return summary


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
    summary = export_layout_summary(
        directory, layout_name, [result.metrics for result in results],
        config, mapping, tasks, report,
    )
    if event_log:
        rows = []
        for result in results:
            rows.extend({"date": result.metrics["date"], **event} for event in result.events)
        write_csv(directory / "event_log.csv", rows)
    for result in results:
        if result.deadlock_snapshot:
            write_json(
                directory / f"deadlock_snapshot_{result.metrics['date']}.json",
                result.deadlock_snapshot,
            )
    return summary
