from __future__ import annotations

import csv
import gzip
import json
from pathlib import Path
from statistics import fmean

from .models import Config, DayResult


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows: path.write_text("", encoding="utf-8"); return
    fields = list(rows[0])
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields); writer.writeheader(); writer.writerows(rows)


def summary(name: str, results: list[DayResult]) -> dict:
    metrics = [r.metrics for r in results]
    hours = sum(m["makespan_hours"] for m in metrics)
    lines = sum(m["completed_lines"] for m in metrics)
    return {
        "layout": name, "days": len(metrics), "failed_days": sum(m["failed"] for m in metrics),
        "total_completed_lines": lines, "total_completed_tasks": sum(m["completed_tasks"] for m in metrics),
        "total_rack_presentations": sum(m["rack_presentations"] for m in metrics),
        "total_travel_distance_m": sum(m["travel_distance_m"] for m in metrics),
        "total_wait_ticks": sum(m["wait_ticks"] for m in metrics),
        "total_station_queue_time_seconds": sum(m["station_queue_time_seconds"] for m in metrics),
        "weighted_throughput_lines_per_hour": lines / hours if hours else 0,
        "mean_daily_throughput_lines_per_hour": fmean(m["line_throughput_per_hour"] for m in metrics),
        "mean_amr_utilization": fmean(m["amr_utilization"] for m in metrics),
        "planner_calls": sum(m["planner_calls"] for m in metrics),
        "planner_time_seconds": sum(m["planner_time_seconds"] for m in metrics),
        "planner_retries": sum(m["planner_retries"] for m in metrics),
        "planner_timeouts": sum(m["planner_timeouts"] for m in metrics),
        "lacam_sum_of_costs": sum(m["lacam_sum_of_costs"] for m in metrics),
        "collision_validation_failures": sum(m["collision_validation_failures"] for m in metrics),
        "no_progress_failures": sum(m["no_progress_failures"] for m in metrics),
        "model_limitations": ["conservative fixed-tick motion", "centralized perfect state knowledge", "fixed dispatch policy", "no inventory depletion", "no stochastic execution delays"],
    }


def export(directory: Path, name: str, results: list[DayResult], config: Config, validation: dict, trace: bool) -> dict:
    directory.mkdir(parents=True, exist_ok=True)
    write_csv(directory / "daily_metrics.csv", [r.metrics for r in results])
    value = summary(name, results)
    write_json(directory / "summary.json", value)
    write_json(directory / "config_snapshot.json", config.snapshot())
    write_json(directory / "validation_report.json", validation)
    if trace:
        with gzip.open(directory / "trace.json.gz", "wt", encoding="utf-8") as stream:
            json.dump([{"metrics": r.metrics, "events": r.events, "trajectory": r.trajectory, "jobs": r.jobs} for r in results], stream, default=str)
    return value
