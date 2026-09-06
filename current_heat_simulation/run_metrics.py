"""Streaming operational KPIs; order lines complete after rack return/jack-down."""

from collections import Counter, defaultdict
import csv
import json
import math
from pathlib import Path
from statistics import mean

TIME_CATEGORIES = ("handling_service", "translation", "rotation", "safety_blocking",
                   "reservation_path_wait", "other_active_wait", "idle")


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False)+"\n")
    temporary.replace(path)


def write_csv(path: Path, rows: list[dict], fields=None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields or (list(rows[0]) if rows else []))
        writer.writeheader()
        writer.writerows(rows)


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    rank = (len(ordered)-1)*fraction
    low = int(rank)
    return ordered[low] + (ordered[min(low+1, len(ordered)-1)]-ordered[low])*(rank-low)


def ratio(numerator, denominator):
    return numerator/denominator if denominator else None


class RunMetricsRecorder:
    def __init__(self) -> None:
        self.planning_seconds = 0.0
        self.planning_count = 0
        self.failures = 0
        self.replans = 0
        self.robots = defaultdict(Counter)
        self.jobs = defaultdict(Counter)
        self.stations = defaultdict(Counter)
        self.hotspots = Counter()
        self.stages = defaultdict(dict)
        self.contexts = {}
        self.scheduler = None
        self.allocator = None

    def record_planning_result(self, *, latency_ms: float, success: bool, timeout: bool = False) -> None:
        self.planning_seconds += latency_ms/1000
        self.planning_count += 1
        self.failures += int(not success)

    def record_replan(self) -> None:
        self.replans += 1

    def record_stage(self, job_id, phase, now):
        self.stages[job_id].setdefault(phase + "_time_s", now)

    def observe_motion(self, robot, before, dt, allowed, target):
        """Called once per physical substep; motion stays on one straight segment."""
        x, y, heading = before
        distance = math.hypot(robot.x-x, robot.y-y)
        turn = abs((robot.heading-heading+math.pi) % (2*math.pi)-math.pi)
        context = self.contexts.get(robot.name)
        active = context is not None and context.assigned_task is not None
        if not active:
            category = "idle"
        elif context.phase in {"pickup_wait", "dropoff_wait", "return_wait"}:
            category = "handling_service"
        elif distance > 1e-10:
            category = "translation"
        elif turn > 1e-10:
            category = "rotation"
        elif target is not None and not allowed:
            category = "safety_blocking"
        elif target is None and context.last_goal != robot.current_vertex:
            category = "reservation_path_wait"
        else:
            category = "other_active_wait"
        counters = [self.robots[robot.name]]
        if active:
            job_id = context.assigned_task.task_id
            counters.append(self.jobs[job_id])
            if context.phase == "dropoff_wait":
                station = self.scheduler.jobs[job_id].task.workstation
                self.stations[station]["service_seconds"] += dt
        for counter in counters:
            counter[category+"_seconds"] += dt
            counter["loaded_distance_m" if robot.jack_up else "empty_distance_m"] += distance
        if category in {"reservation_path_wait", "safety_blocking"}:
            node = target
            if node is None and self.allocator is not None:
                state = self.allocator.states[robot.name]
                if state.current_index < len(state.full_path):
                    node = state.full_path[state.current_index]
            self.hotspots[(node or robot.current_vertex, category)] += dt

    def finalize(self, output, scheduler, now, failure_reason, safety_interventions, wall_seconds=0.0):
        output.mkdir(parents=True, exist_ok=True)
        tasks = [{
            "task_id": t.source.task_id, "date": t.source.task_date.isoformat(),
            "store_id": t.source.store_id, "workstation": t.workstation,
            "release_time_s": 0.0, "completion_time_s": t.completed_at,
            "source_lines": t.source.source_lines, "completed_lines": t.completed_lines,
            "status": "completed" if t.completed_at is not None else "unfinished",
        } for t in scheduler.tasks]
        stage_names = ("to_pickup", "pickup_wait", "to_dropoff_entry", "to_workstation",
                       "dropoff_wait", "to_ingestor_exit", "to_return", "return_wait", "idle")
        jobs = [{
            "job_id": j.request.task_id, "task_id": j.task.source.task_id,
            "store_id": j.task.source.store_id, "robot": j.request.robot_name,
            "rack_id": j.request.rack_id, "workstation": j.task.workstation,
            "covered_skus": json.dumps(sorted(j.lines)), "covered_lines": sum(j.lines.values()),
            "dispatch_time_s": j.dispatch_time, "completion_time_s": j.completion_time,
            **{name+"_time_s": self.stages[j.request.task_id].get(name+"_time_s") for name in stage_names},
            **self._counter_row(self.jobs[j.request.task_id]),
        } for j in scheduler.jobs.values()]
        robot_rows = []
        for name, counters in sorted(self.robots.items()):
            row = {"robot": name, **self._counter_row(counters)}
            row["busy_seconds"] = sum(counters[c+"_seconds"] for c in TIME_CATEGORIES if c != "idle")
            row["utilization"] = ratio(row["busy_seconds"], now)
            robot_rows.append(row)
        completed_jobs = [j for j in scheduler.jobs.values() if j.completion_time is not None]
        stations = []
        for name in scheduler.config.workstations:
            station_jobs = [j for j in completed_jobs if j.task.workstation == name]
            seconds = self.stations[name]["service_seconds"]
            stations.append({"workstation": name, "service_seconds": seconds,
                             "utilization": ratio(seconds, now),
                             "completed_lines": sum(sum(j.lines.values()) for j in station_jobs),
                             "completed_rack_jobs": len(station_jobs)})
        hotspots = [{"node": node, "reason": reason, "blocked_robot_seconds": seconds}
                    for (node, reason), seconds in sorted(self.hotspots.items())]
        lines = sum(t.completed_lines for t in scheduler.tasks)
        demand = sum(t.source.source_lines for t in scheduler.tasks)
        durations = [t.completed_at for t in scheduler.tasks if t.completed_at is not None]
        totals = Counter()
        for counters in self.robots.values():
            totals.update(counters)
        empty, loaded = totals["empty_distance_m"], totals["loaded_distance_m"]
        busy = sum(totals[c+"_seconds"] for c in TIME_CATEGORIES if c != "idle")
        success = scheduler.done and not failure_reason
        status = "completed" if success else ("incomplete" if failure_reason in {
            "simulation_time_limit_with_unfinished_tasks", "window_closed_before_completion", "interrupted"
        } else "failed")
        summary = {
            "method": "current_heat", "date": scheduler.tasks[0].source.task_date.isoformat(),
            "status": status, "sim_duration_s": now, "makespan_hours": now/3600 if success else None,
            "success_flag": success, "failure_reason": failure_reason,
            "released_tasks": len(tasks), "completed_tasks": scheduler.completed_tasks,
            "unfinished_tasks": len(tasks)-scheduler.completed_tasks,
            "source_lines": demand, "completed_lines": lines, "unfinished_lines": demand-lines,
            "completion_ratio": ratio(lines, demand), "rack_jobs": len(jobs),
            "completed_rack_jobs": len(completed_jobs), "line_throughput_per_hour": ratio(lines*3600, now),
            "planner_failures": self.failures, "replans": self.replans,
            "mean_planning_latency_ms": ratio(self.planning_seconds*1000, self.planning_count),
            "wall_clock_seconds": wall_seconds, "simulated_seconds_per_wall_second": ratio(now, wall_seconds),
            "safety_blocked_robot_substeps": safety_interventions,
            "empty_distance_m": empty, "loaded_distance_m": loaded, "travel_distance_m": empty+loaded,
            "travel_metres_per_completed_line": ratio(empty+loaded, lines),
            "lines_per_completed_rack_trip": ratio(lines, len(completed_jobs)),
            "skus_per_completed_rack_trip": ratio(sum(len(j.lines) for j in completed_jobs), len(completed_jobs)),
            "rack_trips_per_1000_lines": ratio(len(completed_jobs)*1000, lines),
            "mean_store_completion_seconds": mean(durations) if durations else None,
            "p95_store_completion_seconds": percentile(durations, .95),
            "amr_utilization": ratio(busy, now*scheduler.config.amr_count),
            "reservation_wait_seconds_per_line": ratio(totals["reservation_path_wait_seconds"], lines),
            "safety_wait_seconds_per_line": ratio(totals["safety_blocking_seconds"], lines),
            "replans_per_1000_lines": ratio(self.replans*1000, lines),
            **{c+"_seconds": totals[c+"_seconds"] for c in TIME_CATEGORIES},
        }
        write_csv(output/"tasks.csv", tasks)
        write_csv(output/"rack_jobs.csv", jobs, list(jobs[0]) if jobs else ["job_id", "completion_time_s"])
        write_csv(output/"robots.csv", robot_rows)
        write_csv(output/"workstations.csv", stations)
        write_csv(output/"blocking_hotspots.csv", hotspots, ["node", "reason", "blocked_robot_seconds"])
        write_json(output/"summary.json", summary)
        return summary

    @staticmethod
    def _counter_row(counter):
        return {key: counter[key] for key in [*(c+"_seconds" for c in TIME_CATEGORIES),
                                              "empty_distance_m", "loaded_distance_m"]}
