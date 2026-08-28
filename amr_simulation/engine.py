"""Heap-based discrete-event scheduler with incremental node reservations."""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass

from warehouse_layout.domain import GridProject

from .conflict_solver import DramConflict, partial_conflicts
from .models import (
    DayResult,
    MotionSegment,
    Rack,
    SimulationConfig,
    WorkloadTask,
    grid_name,
    grid_position,
)
from .routing import GridRouter, Route


@dataclass(slots=True)
class _Task:
    source: WorkloadTask
    workstation: str
    outstanding: dict[str, int]
    released: bool = False
    inflight: int = 0
    completed_lines: int = 0
    completed_at: float | None = None


@dataclass(slots=True)
class _AMR:
    amr_id: str
    position: tuple[int, int]
    heading: float
    free: bool = True
    busy_seconds: float = 0.0
    travel_seconds: float = 0.0
    travel_distance_m: float = 0.0


@dataclass(slots=True)
class _Job:
    job_id: str
    task: _Task
    rack: Rack
    lines: dict[str, int]
    amr: _AMR
    workstation: str
    dispatch_time: float
    rack_departure: float | None = None
    rack_return: float | None = None
    station_queue_position: tuple[int, int] | None = None
    station_queue_enter: float | None = None
    station_admitted: float | None = None
    station_arrival: float | None = None
    service_start: float | None = None
    completion_time: float | None = None
    travel_distance_m: float = 0.0
    travel_seconds: float = 0.0
    node_wait_seconds: float = 0.0
    reservation_conflicts: int = 0
    reroutes: int = 0
    stage: str = ""
    stage_goal: tuple[int, int] | None = None
    stage_route: Route | None = None
    stage_loaded: bool = False
    stage_arrival_kind: str = ""
    tabu: set[tuple[int, int]] | None = None
    request_time: float | None = None
    wait_since: float | None = None
    wait_token: int = 0
    blocked_node: tuple[int, int] | None = None
    blocked_reason: str | None = None
    conflict_signature: tuple | None = None
    conflict_amrs: tuple[str, ...] = ()
    dram_wait_seconds: float = 0.0
    dram_wait_since: float | None = None
    parking_backoff: bool = False


@dataclass(slots=True)
class _Station:
    station_id: str
    position: tuple[int, int]
    busy: bool = False
    service_seconds: float = 0.0
    queue_wait_seconds: float = 0.0
    max_queue: int = 0


class _Engine:
    def __init__(
        self,
        project: GridProject,
        router: GridRouter,
        racks: dict[str, Rack],
        tasks: list[WorkloadTask],
        mapping: dict[str, str],
        config: SimulationConfig,
        trace: bool,
    ):
        self.project, self.router, self.racks, self.config = project, router, racks, config
        self.trace = trace
        self.tasks = [
            _Task(task, mapping[task.store_id], dict(task.line_counts))
            for task in sorted(tasks, key=lambda item: item.task_id)
        ]
        initial_heading = math.radians(config.initial_heading_degrees)
        self.amrs = [
            _AMR(f"AMR_{index + 1:02d}", grid_position(node), initial_heading)
            for index, node in enumerate(config.spawn_nodes)
        ]
        self.node_owners = {amr.position: amr.amr_id for amr in self.amrs}
        self.stations = {
            station: _Station(station, router.workstations[station])
            for station in config.workstations
        }
        self.reserved_racks: set[str] = set()
        self.racks_by_sku: dict[str, set[str]] = {}
        for rack in racks.values():
            for sku in rack.skus:
                self.racks_by_sku.setdefault(sku, set()).add(rack.rack_id)
        self.calendar: list[tuple[float, int, str, object]] = []
        self.sequence = 0
        self.job_sequence = 0
        self.jobs: list[_Job] = []
        self.active_jobs: dict[str, _Job] = {}
        self.events: list[dict] = []
        self.motion_segments: list[MotionSegment] = []
        self.paths: dict[str, list[tuple[int, int]]] = {}
        self.pending: dict[str, _Job] = {}
        self.pending_dirty: set[str] = set()
        self.dispatch_needed = False
        self.reservation_conflicts = 0
        self.node_ownership_conflicts = 0
        self.dram_solver_conflicts = 0
        self.dram_solver_reroutes = 0
        self.reroute_count = 0
        self.max_reserved_nodes = len(self.node_owners)
        self.turn_cache: dict[tuple[tuple[int, int], ...], tuple[int, ...]] = {}
        self.processed_events = 0
        self.next_calendar_compaction = 50_000

    def schedule(self, when: float, kind: str, payload: object) -> None:
        self.sequence += 1
        heapq.heappush(self.calendar, (when, self.sequence, kind, payload))

    def record(self, when: float, kind: str, job: _Job | None = None, **values) -> None:
        if not self.trace:
            return
        self.events.append({
            "time_seconds": when,
            "event": kind,
            "job_id": job.job_id if job else "",
            "task_id": job.task.source.task_id if job else values.pop("task_id", ""),
            "amr_id": job.amr.amr_id if job else "",
            "rack_id": job.rack.rack_id if job else "",
            "workstation": job.workstation if job else "",
            **values,
        })

    def run(self) -> DayResult:
        for task in self.tasks:
            self.schedule(0.0, "task_release", task)
        while self.calendar:
            now = self.calendar[0][0]
            while self.calendar and abs(self.calendar[0][0] - now) <= 1e-9:
                when, _sequence, kind, payload = heapq.heappop(self.calendar)
                self.processed_events += 1
                self._handle(when, kind, payload)
            if self.dispatch_needed:
                self.dispatch_needed = False
                self._dispatch(now)
            self._resolve_reservations(now)
            if self.processed_events >= self.next_calendar_compaction:
                self._compact_calendar()
                self.next_calendar_compaction += 50_000
        incomplete = [task.source.task_id for task in self.tasks if task.completed_at is None]
        if incomplete:
            waiting = ", ".join(
                f"{job.amr.amr_id}:{grid_name(job.blocked_node) if job.blocked_node else '?'}"
                for job in self.pending.values()
            )
            raise RuntimeError(
                "simulation deadlocked with incomplete tasks: "
                + ", ".join(incomplete[:10])
                + (f"; waiting {waiting}" if waiting else "")
            )
        return self._result()

    def _compact_calendar(self) -> None:
        def live(entry) -> bool:
            _when, _sequence, kind, payload = entry
            if kind != "reservation_timeout":
                return True
            job, token = payload
            return (
                self.pending.get(job.amr.amr_id) is job
                and token == job.wait_token
            )

        compacted = [entry for entry in self.calendar if live(entry)]
        if len(compacted) != len(self.calendar):
            self.calendar = compacted
            heapq.heapify(self.calendar)

    def _handle(self, now: float, kind: str, payload: object) -> None:
        if kind == "task_release":
            task = payload
            task.released = True
            self.dispatch_needed = True
            self.record(now, kind, task_id=task.source.task_id, store_id=task.source.store_id)
            return
        if kind == "node_crossing":
            job, position = payload
            previous = job.amr.position
            if previous != position and self.node_owners.get(previous) == job.amr.amr_id:
                del self.node_owners[previous]
                self.record(now, "node_released", job, node=grid_name(previous))
                self.pending_dirty.update(
                    amr_id
                    for amr_id, waiting in self.pending.items()
                    if waiting.blocked_node == previous
                )
            job.amr.position = position
            self.record(now, "node_entered", job, node=grid_name(position))
            self._wake_dram_waiters(job.amr.amr_id)
            return
        if kind == "movement_tail":
            job, heading, remaining = payload
            job.amr.heading = heading
            if job.parking_backoff:
                job.parking_backoff = False
                job.stage_route = self.router.route(
                    job.amr.position, job.stage_goal, frozenset(job.tabu)
                ) or self._required_route(job.amr.position, job.stage_goal)
                self._request_reservation(job, now)
                return
            job.stage_route = remaining
            if len(remaining.positions) == 1:
                self.schedule(now, job.stage_arrival_kind, job)
            else:
                self._request_reservation(job, now)
            return
        if kind == "reservation_timeout":
            job, token = payload
            if self.pending.get(job.amr.amr_id) is not job or token != job.wait_token:
                return
            if job.blocked_node is None or job.blocked_reason is None:
                return
            if self._hard_station_wait(job) or job.blocked_node == job.stage_goal:
                return
            dram_timeout = job.blocked_reason == "dram_solver"
            if dram_timeout and job.dram_wait_since is not None:
                job.dram_wait_seconds += now - job.dram_wait_since
                job.dram_wait_since = None
            job.tabu.add(job.blocked_node)
            job.reroutes += 1
            self.reroute_count += 1
            if dram_timeout:
                self.dram_solver_reroutes += 1
            self.record(now, "tabu_added", job, node=grid_name(job.blocked_node))
            route = self.router.route(
                job.amr.position, job.stage_goal, frozenset(job.tabu)
            )
            if route is None:
                job.tabu.clear()
                self.record(now, "tabu_cleared", job, reason="no_route")
                route = self._parking_route(job)
                if route is not None:
                    job.parking_backoff = True
                    if job.amr.position != job.stage_goal:
                        job.tabu.add(job.amr.position)
                    self.record(
                        now, "reservation_backoff", job,
                        node=grid_name(route.positions[-1]),
                    )
                else:
                    route = self._required_route(job.amr.position, job.stage_goal)
            job.stage_route = route
            job.wait_since = now
            job.blocked_node = None
            job.blocked_reason = None
            job.conflict_signature = None
            job.conflict_amrs = ()
            job.wait_token += 1
            self.record(
                now, "rerouted", job,
                reason="dram_timeout" if dram_timeout else "node_timeout",
                path=[grid_name(node) for node in route.positions],
            )
            if len(route.positions) == 1:
                self.pending.pop(job.amr.amr_id, None)
                self.schedule(now, job.stage_arrival_kind, job)
                self._wake_dram_waiters(job.amr.amr_id)
                return
            self.pending_dirty.add(job.amr.amr_id)
            self._wake_dram_waiters(job.amr.amr_id)
            return

        job = payload
        self.record(now, kind, job)
        if kind == "pickup_arrival":
            job.tabu.clear()
            self._stationary(job, "jack_up", now, now + self.config.jack_up_seconds, job.rack.position, False)
            self.schedule(now + self.config.jack_up_seconds, "jack_up_done", job)
        elif kind == "jack_up_done":
            job.rack_departure = now
            self._start_stage(
                job,
                self._required_route(job.rack.position, self.stations[job.workstation].position),
                now,
                "to_station",
                True,
                "station_arrival",
            )
        elif kind == "station_arrival":
            job.tabu.clear()
            station = self.stations[job.workstation]
            if station.busy:
                raise RuntimeError(f"reserved workstation {station.station_id} is already busy")
            station.busy = True
            job.station_arrival = job.service_start = now
            self._stationary(job, "service", now, now + self.config.service_seconds, station.position, True)
            self.record(now, "service_start", job)
            self.schedule(now + self.config.service_seconds, "service_done", job)
        elif kind == "service_done":
            station = self.stations[job.workstation]
            station.busy = False
            station.service_seconds += self.config.service_seconds
            self._start_stage(
                job,
                self._required_route(station.position, job.rack.position),
                now,
                "return_rack",
                True,
                "rack_home",
            )
        elif kind == "rack_home":
            job.tabu.clear()
            job.rack_return = now
            self._stationary(job, "jack_down", now, now + self.config.jack_down_seconds, job.rack.position, True)
            self.schedule(now + self.config.jack_down_seconds, "jack_down_done", job)
        elif kind == "jack_down_done":
            self.reserved_racks.remove(job.rack.rack_id)
            job.amr.free = True
            self.dispatch_needed = True
            job.amr.busy_seconds += now - job.dispatch_time
            job.completion_time = now
            self.active_jobs.pop(job.amr.amr_id, None)
            job.task.inflight -= 1
            completed = sum(job.lines.values())
            job.task.completed_lines += completed
            if not job.task.outstanding and not job.task.inflight:
                job.task.completed_at = now
                self.record(now, "task_complete", job, completed_lines=job.task.completed_lines)

    def _start_stage(
        self,
        job: _Job,
        route: Route,
        now: float,
        stage: str,
        loaded: bool,
        arrival_kind: str,
    ) -> None:
        self._wake_dram_waiters(job.amr.amr_id)
        self.record(
            now,
            "stage_path_planned",
            job,
            stage=stage,
            start=grid_name(route.positions[0]),
            goal=grid_name(route.positions[-1]),
            path=[grid_name(node) for node in route.positions],
            distance_m=route.distance_m,
        )
        job.stage = stage
        job.stage_goal = route.positions[-1]
        job.stage_route = route
        job.stage_loaded = loaded
        job.stage_arrival_kind = arrival_kind
        job.tabu = set()
        job.request_time = job.wait_since = None
        job.blocked_node = None
        job.blocked_reason = None
        job.conflict_signature = None
        job.conflict_amrs = ()
        job.wait_token += 1
        if len(route.positions) == 1:
            self.schedule(now, arrival_kind, job)
        else:
            self._request_reservation(job, now)

    def _request_reservation(self, job: _Job, now: float) -> None:
        if job.request_time is None:
            job.request_time = now
        if job.wait_since is None:
            job.wait_since = now
        self.pending[job.amr.amr_id] = job
        self.pending_dirty.add(job.amr.amr_id)
        self.record(now, "reservation_requested", job)

    def _wake_dram_waiters(self, progressed_amr: str) -> None:
        for amr_id, waiting in self.pending.items():
            if (
                waiting.blocked_reason == "dram_solver"
                and progressed_amr in waiting.conflict_amrs
            ):
                waiting.conflict_signature = None
                waiting.wait_token += 1
                self.pending_dirty.add(amr_id)

    def _turn_indices(self, positions: tuple[tuple[int, int], ...]) -> list[int]:
        if positions in self.turn_cache:
            return list(self.turn_cache[positions])
        turns = []
        for index in range(1, len(positions) - 1):
            before = self.router.coordinates[positions[index - 1]]
            current = self.router.coordinates[positions[index]]
            after = self.router.coordinates[positions[index + 1]]
            first = (current[0] - before[0], current[1] - before[1])
            second = (after[0] - current[0], after[1] - current[1])
            if abs(first[0] * second[1] - first[1] * second[0]) > 1e-9:
                turns.append(index)
        self.turn_cache[positions] = tuple(turns)
        return turns

    def _reservation_window(self, job: _Job) -> tuple[list[tuple[int, int]], bool]:
        positions = job.stage_route.positions
        turns = self._turn_indices(positions)
        if not turns:
            return list(positions[1:]), False
        first = turns[0]
        if first > 1:
            return list(positions[1:first]), False
        return list(positions[1:3]), True

    def _hard_station_wait(self, job: _Job) -> bool:
        if job.stage != "to_station":
            return False
        turns = self._turn_indices(job.stage_route.positions)
        return not turns or turns == [1]

    def _remaining_distance(self, job: _Job) -> float:
        distance = self.router.route_distance(job.stage_route.positions)
        if job.parking_backoff:
            continuation = self.router.route(job.stage_route.positions[-1], job.stage_goal)
            if continuation is not None:
                distance += continuation.distance_m
        return distance

    def _remaining_path(self, job: _Job) -> tuple[tuple[int, int], ...]:
        positions = job.stage_route.positions
        try:
            return positions[positions.index(job.amr.position):]
        except ValueError:
            route = self.router.route(
                job.amr.position, job.stage_goal, frozenset(job.tabu)
            )
            return route.positions if route is not None else (job.amr.position,)

    def _priority_key(self, job: _Job) -> tuple[float, float, str]:
        request_time = job.request_time
        return (
            self._remaining_distance(job),
            float(request_time) if request_time is not None else -math.inf,
            job.amr.amr_id,
        )

    def _dram_conflict_after(
        self,
        selected: _Job,
        position: tuple[int, int],
        active: tuple[_Job, ...],
        base_paths: dict[str, tuple[tuple[int, int], ...]],
    ) -> tuple[DramConflict | None, list[_Job], _Job | None]:
        paths = []
        for job in active:
            path = base_paths[job.job_id]
            if job is selected:
                path = path[path.index(position):]
            paths.append(path)
        for conflict in partial_conflicts(paths, active.index(selected)):
            involved = [active[index] for index in conflict.agent_indices]
            winner = min(involved, key=self._priority_key)
            if conflict.kind == "cycle" or winner is not selected:
                return conflict, involved, None if conflict.kind == "cycle" else winner
        return None, [], None

    def _parking_route(self, job: _Job) -> Route | None:
        candidates = []
        for neighbour, weight in self.router.graph[job.amr.position]:
            if neighbour in self.node_owners or neighbour in self.router.rack_positions:
                continue
            if self.router.route(
                neighbour,
                job.stage_goal,
                frozenset({job.amr.position}),
            ) is None:
                continue
            candidates.append((weight, neighbour))
        if not candidates:
            return None
        weight, neighbour = min(candidates)
        return Route((job.amr.position, neighbour), weight)

    def _resolve_reservations(self, now: float) -> None:
        ready = self.pending_dirty
        self.pending_dirty = set()
        if not ready:
            return
        ordered = sorted(
            (
                self.pending[amr_id]
                for amr_id in ready
                if amr_id in self.pending
            ),
            key=lambda job: (
                self._remaining_distance(job),
                float(job.request_time),
                job.amr.amr_id,
            ),
        )
        active = tuple(self.active_jobs[amr_id] for amr_id in sorted(self.active_jobs))
        base_paths = {job.job_id: self._remaining_path(job) for job in active}
        for job in ordered:
            if self.pending.get(job.amr.amr_id) is not job:
                continue
            desired, atomic = self._reservation_window(job)
            if not desired:
                del self.pending[job.amr.amr_id]
                self.schedule(now, job.stage_arrival_kind, job)
                continue
            available = []
            blocker = None
            blocking_owner = None
            conflict = None
            conflicting_jobs = []
            winner = None
            for node in desired:
                owner = self.node_owners.get(node)
                if owner is not None and owner != job.amr.amr_id:
                    blocker = node
                    blocking_owner = owner
                    break
                available.append(node)
            if blocker is None:
                safe = []
                for node in available:
                    conflict, conflicting_jobs, winner = self._dram_conflict_after(
                        job, node, active, base_paths
                    )
                    if conflict is not None:
                        blocker = conflict.overlap_node
                        break
                    safe.append(node)
                available = safe
            if atomic and blocker is not None:
                available = []
            if not available:
                job.blocked_node = blocker
                solver_blocked = conflict is not None
                reason = "dram_solver" if solver_blocked else "node_owned"
                job.reservation_conflicts += 1
                self.reservation_conflicts += 1
                if solver_blocked:
                    self.dram_solver_conflicts += 1
                else:
                    self.node_ownership_conflicts += 1
                conflict_amrs = (
                    tuple(sorted(other.amr.amr_id for other in conflicting_jobs if other is not job))
                    if solver_blocked else ((blocking_owner,) if blocking_owner else ())
                )
                self.record(
                    now, "reservation_denied", job, node=grid_name(blocker),
                    reason=reason,
                    conflict_type=conflict.kind if conflict else "node_ownership",
                    conflicting_amrs=list(conflict_amrs),
                    winner=winner.amr.amr_id if winner else "",
                    resolution="wait",
                )
                signature = (
                    conflict.kind if conflict else "node_ownership",
                    conflict_amrs,
                    blocker,
                    winner.amr.amr_id if winner else "",
                )
                if job.blocked_reason != reason or job.conflict_signature != signature:
                    if reason == "dram_solver" and job.dram_wait_since is None:
                        job.dram_wait_since = now
                    elif reason != "dram_solver" and job.dram_wait_since is not None:
                        job.dram_wait_seconds += now - job.dram_wait_since
                        job.dram_wait_since = None
                    job.blocked_reason = reason
                    job.conflict_signature = signature
                    job.conflict_amrs = conflict_amrs
                    job.wait_token += 1
                    timeout = (
                        self.config.dram_conflict_wait_seconds
                        if solver_blocked else self.config.reservation_wait_seconds
                    )
                    self.schedule(now + timeout, "reservation_timeout", (job, job.wait_token))
                if self._hard_station_wait(job):
                    station = self.stations[job.workstation]
                    if job.station_queue_enter is None:
                        job.station_queue_enter = now
                        job.station_queue_position = job.amr.position
                    waiting = sum(
                        other.stage == "to_station" and self._hard_station_wait(other)
                        for other in self.pending.values()
                    )
                    station.max_queue = max(station.max_queue, waiting)
                continue
            del self.pending[job.amr.amr_id]
            for node in available:
                self.node_owners[node] = job.amr.amr_id
            self.max_reserved_nodes = max(self.max_reserved_nodes, len(self.node_owners))
            waited = now - float(job.wait_since)
            if waited > 0:
                job.node_wait_seconds += waited
                self._stationary(job, "reservation_wait", job.wait_since, now, job.amr.position, job.stage_loaded)
            if job.dram_wait_since is not None:
                job.dram_wait_seconds += now - job.dram_wait_since
                job.dram_wait_since = None
            if job.stage == "to_station" and job.stage_goal in available:
                station = self.stations[job.workstation]
                job.station_queue_position = job.station_queue_position or job.amr.position
                job.station_queue_enter = job.station_queue_enter if job.station_queue_enter is not None else now
                job.station_admitted = now
                station.queue_wait_seconds += now - job.station_queue_enter
            self.record(
                now,
                "reservation_granted",
                job,
                nodes=[grid_name(node) for node in available],
            )
            job.request_time = job.wait_since = None
            job.wait_token += 1
            job.blocked_node = None
            job.blocked_reason = None
            job.conflict_signature = None
            job.conflict_amrs = ()
            self._move_reserved(job, available, now)

    def _move_reserved(
        self, job: _Job, reserved: list[tuple[int, int]], now: float
    ) -> None:
        positions = (job.amr.position, *reserved)
        route = Route(positions, self.router.route_distance(positions))
        end, heading, segments = self.router.motion(
            job.job_id,
            job.amr.amr_id,
            route,
            now,
            job.amr.heading,
            self.config.motion,
            job.stage_loaded,
        )
        duration = end - now
        job.travel_seconds += duration
        job.travel_distance_m += route.distance_m
        job.amr.travel_seconds += duration
        job.amr.travel_distance_m += route.distance_m
        if self.trace:
            self.motion_segments.extend(segments)
            path = self.paths.setdefault(job.job_id, [])
            path.extend(route.positions if not path else route.positions[1:])
        for position, reached in self.router.crossing_times(route, segments):
            self.schedule(reached, "node_crossing", (job, position))
        count = len(reserved)
        remaining_positions = job.stage_route.positions[count:]
        remaining = Route(
            remaining_positions,
            self.router.route_distance(remaining_positions),
        )
        self.schedule(end, "movement_tail", (job, heading, remaining))

    def _required_route(self, start: tuple[int, int], goal: tuple[int, int]) -> Route:
        route = self.router.route(start, goal)
        if route is None:
            raise RuntimeError(f"validated route became unavailable: {start} -> {goal}")
        return route

    def _stationary(
        self,
        job: _Job,
        kind: str,
        start: float,
        end: float,
        position: tuple[int, int],
        loaded: bool,
    ) -> None:
        if not self.trace or end <= start:
            return
        coordinates = self.project.coordinates(*position)
        self.motion_segments.append(
            MotionSegment(
                job.job_id,
                job.amr.amr_id,
                kind,
                start,
                end,
                coordinates,
                coordinates,
                job.amr.heading,
                job.amr.heading,
                loaded=loaded,
            )
        )

    def _dispatch(self, now: float) -> None:
        while True:
            free_amrs = [amr for amr in self.amrs if amr.free]
            if not free_amrs:
                return
            choice = None
            for task in self.tasks:
                if not task.released or not task.outstanding:
                    continue
                station_position = self.stations[task.workstation].position
                station_x, station_y = self.router.coordinates[station_position]
                candidate_ids = {
                    rack_id
                    for sku in task.outstanding
                    for rack_id in self.racks_by_sku.get(sku, ())
                }
                candidates = []
                for rack_id in candidate_ids:
                    rack = self.racks[rack_id]
                    if rack_id in self.reserved_racks:
                        continue
                    covered = sorted(rack.skus & task.outstanding.keys())
                    rack_x, rack_y = self.router.coordinates[rack.position]
                    if covered:
                        # Rank racks without planning the future delivery stage.
                        distance = math.hypot(station_x - rack_x, station_y - rack_y)
                        candidates.append((-len(covered), distance, rack_id, rack, covered))
                for candidate in sorted(candidates):
                    rack = candidate[3]
                    owner = self.node_owners.get(rack.position)
                    amr_choices = []
                    for amr in free_amrs:
                        if owner is not None and owner != amr.amr_id:
                            continue
                        pickup = self.router.route(amr.position, rack.position)
                        if pickup is None:
                            continue
                        predicted = self.router.predicted_motion_time(
                            pickup, amr.heading, self.config.motion
                        )
                        amr_choices.append((predicted, amr.amr_id, amr, pickup))
                    if amr_choices:
                        choice = task, candidate, min(amr_choices)
                        break
                if choice is not None:
                    break
            if choice is None:
                return
            task, (_coverage, _distance, _rack_id, rack, covered), amr_choice = choice
            _predicted, _amr_id, amr, pickup = amr_choice
            lines = {sku: task.outstanding.pop(sku) for sku in covered}
            task.inflight += 1
            self.reserved_racks.add(rack.rack_id)
            amr.free = False
            self.job_sequence += 1
            job = _Job(
                f"J{self.job_sequence:06d}",
                task,
                rack,
                lines,
                amr,
                task.workstation,
                now,
                tabu=set(),
            )
            self.jobs.append(job)
            self.active_jobs[amr.amr_id] = job
            self.record(now, "dispatch", job, covered_skus=covered, covered_lines=sum(lines.values()))
            self._start_stage(job, pickup, now, "to_pickup", False, "pickup_arrival")

    def _result(self) -> DayResult:
        first_release = 0.0
        last_completion = max(float(task.completed_at) for task in self.tasks)
        makespan = last_completion - first_release
        completed_lines = sum(task.completed_lines for task in self.tasks)
        station_metrics = {
            station_id: {
                "utilization": station.service_seconds / makespan if makespan else 0.0,
                "service_seconds": station.service_seconds,
                "queue_wait_seconds": station.queue_wait_seconds,
                "max_queue": station.max_queue,
            }
            for station_id, station in self.stations.items()
        }
        metrics = {
            "date": self.tasks[0].source.task_date.isoformat(),
            "first_release_seconds": first_release,
            "final_completion_seconds": last_completion,
            "makespan_seconds": makespan,
            "makespan_hours": makespan / 3600.0,
            "completed_lines": completed_lines,
            "completed_tasks": len(self.tasks),
            "rack_presentations": len(self.jobs),
            "line_throughput_per_hour": completed_lines * 3600.0 / makespan if makespan else 0.0,
            "travel_distance_m": sum(amr.travel_distance_m for amr in self.amrs),
            "travel_time_seconds": sum(amr.travel_seconds for amr in self.amrs),
            "station_queue_time_seconds": sum(station.queue_wait_seconds for station in self.stations.values()),
            "node_reservation_wait_seconds": sum(job.node_wait_seconds for job in self.jobs),
            "reservation_conflicts": self.reservation_conflicts,
            "node_ownership_conflicts": self.node_ownership_conflicts,
            "dram_solver_conflicts": self.dram_solver_conflicts,
            "reservation_reroutes": self.reroute_count,
            "dram_solver_reroutes": self.dram_solver_reroutes,
            "dram_conflict_wait_seconds": sum(job.dram_wait_seconds for job in self.jobs),
            "max_reserved_nodes": self.max_reserved_nodes,
            "deadlock_count": 0,
            "amr_utilization": (
                sum(amr.busy_seconds for amr in self.amrs) / (makespan * len(self.amrs))
                if makespan else 0.0
            ),
            "workstations": station_metrics,
            "amrs": {
                amr.amr_id: {
                    "utilization": amr.busy_seconds / makespan if makespan else 0.0,
                    "busy_seconds": amr.busy_seconds,
                    "travel_seconds": amr.travel_seconds,
                    "travel_distance_m": amr.travel_distance_m,
                }
                for amr in self.amrs
            },
        }
        job_rows = [
            {
                "job_id": job.job_id,
                "task_id": job.task.source.task_id,
                "store_id": job.task.source.store_id,
                "rack_id": job.rack.rack_id,
                "amr_id": job.amr.amr_id,
                "workstation": job.workstation,
                "covered_skus": sorted(job.lines),
                "covered_lines": sum(job.lines.values()),
                "dispatch_time": job.dispatch_time,
                "rack_departure": job.rack_departure,
                "rack_return": job.rack_return,
                "station_queue_position": grid_name(job.station_queue_position),
                "station_queue_enter": job.station_queue_enter,
                "station_admitted": job.station_admitted,
                "station_arrival": job.station_arrival,
                "service_start": job.service_start,
                "completion_time": job.completion_time,
                "travel_distance_m": job.travel_distance_m,
                "travel_time_seconds": job.travel_seconds,
                "node_reservation_wait_seconds": job.node_wait_seconds,
                "reservation_conflicts": job.reservation_conflicts,
                "reservation_reroutes": job.reroutes,
                "dram_conflict_wait_seconds": job.dram_wait_seconds,
            }
            for job in self.jobs
        ]
        return DayResult(metrics, self.events, self.motion_segments, job_rows, self.paths)


def simulate_day(
    project: GridProject,
    router: GridRouter,
    racks: dict[str, Rack],
    tasks: list[WorkloadTask],
    workstation_mapping: dict[str, str],
    config: SimulationConfig,
    *,
    trace: bool = False,
) -> DayResult:
    if not tasks:
        raise ValueError("cannot simulate an empty day")
    dates = {task.task_date for task in tasks}
    if len(dates) != 1:
        raise ValueError("simulate_day requires tasks from exactly one date")
    return _Engine(project, router, racks, tasks, workstation_mapping, config, trace).run()
