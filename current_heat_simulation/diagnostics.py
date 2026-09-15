"""Flushed live snapshots, independent of the atomic final-results directory."""

from collections import Counter
from datetime import datetime, timezone
import json
from time import perf_counter

from .run_metrics import TIME_CATEGORIES


class DiagnosticLog:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = path.open("w")
        self.started = self.last_wall = perf_counter()
        self.last_sim = 0.0
        self.previous = {}
        self.last_lines = 0
        self.last_completion_sim = 0.0
        self.last_motion_sim = {}

    def write(self, system, event="snapshot", reason=""):
        wall = perf_counter()
        if event == "snapshot" and wall - self.last_wall < 10:
            return
        now = system.sim_time_sec
        metrics, allocator = system.metrics, system.allocator
        lines = sum(task.completed_lines for task in system.task_scheduler.tasks)
        if lines != self.last_lines:
            self.last_completion_sim = now
        robots = []
        for robot in system.simulator.robots:
            name = robot.name
            context = system.task_state_machine.contexts[name]
            state = allocator.states[name]
            counters = dict(metrics.robots.get(name, {}))
            previous = self.previous.get(name, {})
            delta = {key: value - previous.get(key, 0) for key, value in counters.items()}
            distance = delta.get("empty_distance_m", 0) + delta.get("loaded_distance_m", 0)
            if distance > 1e-9:
                self.last_motion_sim[name] = now
            next_node = state.full_path[state.current_index] if state.current_index < len(state.full_path) else None
            robots.append({
                "robot": name, "phase": context.phase,
                "job": context.assigned_task.task_id if context.assigned_task else None,
                "node": robot.current_vertex, "x": robot.x, "y": robot.y,
                "heading": robot.heading, "speed": robot.speed, "loaded": robot.jack_up,
                "goal": context.last_goal, "service_remaining_s": context.action_timer,
                "travel_m_since_snapshot": distance,
                "no_translation_sampled_sim_s": now - self.last_motion_sim.get(name, 0),
                "activity_seconds_since_snapshot": {c: delta.get(c + "_seconds", 0) for c in TIME_CATEGORIES},
                "window_remaining": robot.active_window_path[robot.path_index:],
                "full_path": state.full_path, "allocation_index": state.current_index,
                "next_unreserved_node": next_node,
                "next_reservation_owner": allocator.global_reservations.get(next_node),
                "waiting_for": sorted(owner for owner, waiters in allocator.mutex_passage.wait_for_dependency.items() if name in waiters),
                "reservation_wait_sim_s": {node: now - start for node, start in state.conflict_start_times.items()},
                "last_substep_safety_block": system.simulator.safety_blockers.get(name),
                "resolution": allocator.conflict_resolutions.get(name, "allocate"),
                "pending_replan": name in allocator.pending_replans,
                "tabu_edges": sorted(allocator.tabu_sets.get(name, ())),
            })
            self.previous[name] = counters
        record = {
            "event": event, "reason": reason, "utc": datetime.now(timezone.utc).isoformat(),
            "sim_time_s": now, "wall_time_s": wall - self.started,
            "sample_sim_s": now - self.last_sim,
            "sim_seconds_per_wall_second": (now - self.last_sim) / max(wall - self.last_wall, 1e-9),
            "completed_lines": lines, "completed_lines_since_snapshot": lines - self.last_lines,
            "no_completion_sampled_sim_s": now - self.last_completion_sim,
            "phase_counts": dict(Counter(r["phase"] for r in robots)),
            "planner_calls": metrics.planning_count, "planner_failures": metrics.failures,
            "planning_wall_s": metrics.planning_seconds, "replans": metrics.replans,
            "safety_interventions": system.simulator.safety_interventions,
            "conflicts": system.latest_conflicts,
            "recovery": system.deadlock_recovery.last_event,
            "recent_recovery_events": list(system.deadlock_recovery.events),
            "active_recovery": system.deadlock_recovery.active,
            "no_task_progress_sim_s": now-system.deadlock_recovery.last_task_progress,
            "station_admission_limit": system.task_scheduler.workstation_admission_limit,
            "unfinished_store_tasks": [
                {"task": t.source.task_id, "station": t.workstation,
                 "outstanding_lines": sum(t.outstanding.values()), "inflight": t.inflight,
                 "station_inflight": t.station_inflight}
                for t in system.task_scheduler.tasks if t.completed_at is None
            ],
            "robots": robots,
        }
        self.stream.write(json.dumps(record, allow_nan=False) + "\n")
        self.stream.flush()
        self.last_wall, self.last_sim, self.last_lines = wall, now, lines

    def close(self):
        self.stream.close()
