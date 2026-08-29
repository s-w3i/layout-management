"""Heap-based discrete-event scheduler with incremental node reservations."""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass

from warehouse_layout.domain import GridProject

from .coordination import following_queue, reversed_passage, same_direction_following, wait_cycle
from .conflict_solver import DramConflict, head_to_head_overlap
from .models import (
    DayResult,
    MotionSegment,
    Rack,
    SimulationConfig,
    WorkloadTask,
    grid_name,
    grid_position,
)
from .native_backend import load_native
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
    tabu_edges: set[tuple[tuple[int, int], tuple[int, int]]] | None = None
    request_time: float | None = None
    wait_since: float | None = None
    wait_token: int = 0
    blocked_node: tuple[int, int] | None = None
    blocked_reason: str | None = None
    conflict_signature: tuple | None = None
    conflict_amrs: tuple[str, ...] = ()
    dram_wait_seconds: float = 0.0
    dram_wait_since: float | None = None
    following_wait_since: float | None = None
    corridor_wait_since: float | None = None
    parking_backoff: bool = False
    route_cursor: int = 0
    route_suffix_distances: tuple[float, ...] = ()
    remaining_path_cache: tuple[tuple[int, int], ...] = ()
    remaining_path_position: tuple[int, int] | None = None
    remaining_nodes: frozenset[tuple[int, int]] = frozenset()
    remaining_edges: frozenset[tuple[tuple[int, int], tuple[int, int]]] = frozenset()
    parking_continuation_distance: float = 0.0


@dataclass(slots=True)
class _Station:
    station_id: str
    position: tuple[int, int]
    busy: bool = False
    service_seconds: float = 0.0
    queue_wait_seconds: float = 0.0
    max_queue: int = 0


@dataclass(slots=True)
class _Passage:
    nodes: tuple[tuple[int, int], ...]
    owner_front: str
    owner_direction: tuple[tuple[int, int], tuple[int, int]]
    yielding: tuple[str, ...]


@dataclass(slots=True)
class _ConflictContext:
    active: tuple[_Job, ...]
    paths: dict[str, tuple[tuple[int, int], ...]]
    active_indices: dict[str, int]
    next_edges: dict[tuple[int, int], tuple[tuple[int, int], int]]


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
        coordination_backend: str,
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
        self.native_kernel, self.backend_info = load_native(
            len(self.amrs), coordination_backend
        )
        self.native_agent_ids = {
            amr.amr_id: index for index, amr in enumerate(self.amrs)
        }
        native_nodes = sorted(router.coordinates)
        self.native_node_ids = {node: index for index, node in enumerate(native_nodes)}
        self.native_nodes = tuple(native_nodes)
        self.native_resolution_active = False
        self.native_deferred_paths: dict[str, tuple[tuple[int, int], ...] | None] = {}
        self.native_synced_paths: dict[str, tuple[tuple[int, int], ...]] = {}
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
        self.node_waiters: dict[tuple[int, int], set[str]] = {}
        self.conflict_waiters: dict[str, set[str]] = {}
        self.active_cache: tuple[_Job, ...] | None = None
        self.route_future_users: dict[tuple[int, int], set[str]] = {}
        self.coordination_generation = 0
        self.dispatch_needed = False
        self.reservation_conflicts = 0
        self.node_ownership_conflicts = 0
        self.dram_solver_conflicts = 0
        self.dram_solver_reroutes = 0
        self.reroute_count = 0
        self.max_reserved_nodes = len(self.node_owners)
        self.wait_for: dict[str, set[str]] = {}
        self.passages: dict[frozenset[tuple[int, int]], _Passage] = {}
        self.loaded_priority_grants = 0
        self.loaded_protected_waits = 0
        self.following_wait_seconds = 0.0
        self.following_avoided_reroutes = 0
        self.wait_for_cycles = 0
        self.cycle_breaking_reroutes = 0
        self.corridor_conflicts = 0
        self.corridor_ownership_changes = 0
        self.corridor_yielding_amrs: set[str] = set()
        self.corridor_wait_seconds = 0.0
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
        previous_time = None
        same_time_resolutions = 0
        while self.calendar:
            now = self.calendar[0][0]
            if previous_time is not None and abs(now - previous_time) <= 1e-9:
                same_time_resolutions += 1
            else:
                previous_time = now
                same_time_resolutions = 0
            if same_time_resolutions > 10_000:
                waiting = {
                    amr_id: {
                        "node": grid_name(job.blocked_node) if job.blocked_node else None,
                        "reason": job.blocked_reason,
                        "conflicts": job.conflict_amrs,
                    }
                    for amr_id, job in sorted(self.pending.items())
                }
                raise RuntimeError(
                    f"coordination livelock at {now:.6f}s after "
                    f"{same_time_resolutions} unchanged-time resolutions: {waiting}"
                )
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
                self.pending_dirty.update(self.node_waiters.get(previous, ()))
            job.amr.position = position
            self._advance_route_cache(job, position)
            self.record(now, "node_entered", job, node=grid_name(position))
            self._clear_wait(job, now)
            self._release_passages(now)
            self._wake_dram_waiters(job.amr.amr_id)
            return
        if kind == "movement_tail":
            job, heading, remaining = payload
            job.amr.heading = heading
            if job.parking_backoff:
                job.parking_backoff = False
                route = self.router.route(
                    job.amr.position, job.stage_goal, frozenset(job.tabu),
                    frozenset(job.tabu_edges),
                ) or self._required_route(job.amr.position, job.stage_goal)
                self._set_stage_route(job, route)
                self._request_reservation(job, now)
                return
            self._set_stage_route(job, remaining)
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
            if self.config.loaded_priority_enabled and job.stage_loaded:
                self.loaded_protected_waits += 1
                self.record(now, "loaded_reroute_protected", job)
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
                job.amr.position, job.stage_goal, frozenset(job.tabu),
                frozenset(job.tabu_edges),
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
            self._clear_block_indexes(job)
            self._set_stage_route(job, route)
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
        if kind == "coordination_timeout":
            job, token = payload
            if self.pending.get(job.amr.amr_id) is not job or token != job.wait_token:
                return
            victim = self._break_wait_cycle(now)
            if victim is None:
                path = self._remaining_path(job)
                if len(path) >= 2:
                    if job.stage_loaded:
                        blockers = [
                            self.active_jobs[value]
                            for value in job.conflict_amrs
                            if value in self.active_jobs
                        ]
                        unloaded_blockers = [blocker for blocker in blockers if not blocker.stage_loaded]
                        if unloaded_blockers:
                            victim = max(unloaded_blockers, key=self._priority_key)
                            victim_path = self._remaining_path(victim)
                            if len(victim_path) >= 2 and self._coordination_reroute(
                                victim, now, (victim_path[0], victim_path[1]),
                                "yield_to_loaded",
                            ):
                                return
                        participants = blockers + [job]
                        victim = max(participants, key=self._priority_key)
                        if victim is not job:
                            victim_path = self._remaining_path(victim)
                            if len(victim_path) >= 2 and self._coordination_reroute(
                                victim, now, (victim_path[0], victim_path[1]),
                                "loaded_cycle_override",
                            ):
                                self.record(
                                    now, "loaded_protection_overridden", victim,
                                    reason="loaded_cycle",
                                )
                                return
                        self.record(now, "loaded_protection_overridden", job, reason="coordination_liveness")
                    self._coordination_reroute(
                        job, now, (path[0], path[1]), "coordination_liveness"
                    )
            return

        job = payload
        self.record(now, kind, job)
        if kind == "pickup_arrival":
            job.tabu.clear()
            job.tabu_edges.clear()
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
            job.tabu_edges.clear()
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
            job.tabu_edges.clear()
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
            self.active_cache = None
            if self.native_kernel is not None:
                if self.native_resolution_active:
                    self.native_deferred_paths[job.amr.amr_id] = None
                else:
                    self.native_kernel.remove(self.native_agent_ids[job.amr.amr_id])
                    self.native_synced_paths.pop(job.amr.amr_id, None)
            self._update_route_membership(job, ())
            self.coordination_generation += 1
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
        self._set_stage_route(job, route)
        job.stage_loaded = loaded
        job.stage_arrival_kind = arrival_kind
        job.tabu = set()
        job.tabu_edges = set()
        job.request_time = job.wait_since = None
        job.blocked_node = None
        job.blocked_reason = None
        job.conflict_signature = None
        job.conflict_amrs = ()
        job.wait_token += 1
        self._clear_wait(job, now)
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

    def _set_stage_route(self, job: _Job, route: Route) -> None:
        job.stage_route = route
        job.route_cursor = 0
        suffix = [0.0] * len(route.positions)
        for index in range(len(route.positions) - 2, -1, -1):
            suffix[index] = (
                suffix[index + 1]
                + self.router.edge_weights[(route.positions[index], route.positions[index + 1])]
            )
        job.route_suffix_distances = tuple(suffix)
        job.remaining_path_cache = ()
        job.remaining_path_position = None
        self._update_route_membership(job, route.positions)
        job.parking_continuation_distance = 0.0
        if job.parking_backoff and route.positions[-1] != job.stage_goal:
            continuation = self.router.route(route.positions[-1], job.stage_goal)
            if continuation is not None:
                job.parking_continuation_distance = continuation.distance_m
        self.coordination_generation += 1

    def _advance_route_cache(self, job: _Job, position: tuple[int, int]) -> None:
        positions = job.stage_route.positions
        try:
            cursor = positions.index(position, job.route_cursor)
        except ValueError:
            return
        job.route_cursor = cursor
        self._update_route_membership(job, positions[cursor:])
        job.remaining_path_cache = ()
        job.remaining_path_position = None
        self.coordination_generation += 1

    def _sync_native_path(
        self, job: _Job, positions: tuple[tuple[int, int], ...]
    ) -> None:
        if self.native_kernel is None:
            return
        if self.native_synced_paths.get(job.amr.amr_id) == positions:
            return
        if self.native_resolution_active:
            self.native_deferred_paths[job.amr.amr_id] = positions
            return
        self.native_kernel.set_path(
            self.native_agent_ids[job.amr.amr_id],
            tuple(self.native_node_ids[node] for node in positions),
        )
        self.native_synced_paths[job.amr.amr_id] = positions

    def _update_route_membership(
        self, job: _Job, remaining: tuple[tuple[int, int], ...]
    ) -> None:
        amr_id = job.amr.amr_id
        for node in job.remaining_nodes:
            users = self.route_future_users.get(node)
            if users is None:
                continue
            users.discard(amr_id)
            if not users:
                del self.route_future_users[node]
        job.remaining_nodes = frozenset(remaining[1:])
        job.remaining_edges = frozenset(zip(remaining, remaining[1:]))
        for node in job.remaining_nodes:
            self.route_future_users.setdefault(node, set()).add(amr_id)

    def _clear_block_indexes(self, job: _Job) -> None:
        amr_id = job.amr.amr_id
        if job.blocked_node in self.node_waiters:
            waiters = self.node_waiters[job.blocked_node]
            waiters.discard(amr_id)
            if not waiters:
                del self.node_waiters[job.blocked_node]
        for blocker in job.conflict_amrs:
            waiters = self.conflict_waiters.get(blocker)
            if waiters is None:
                continue
            waiters.discard(amr_id)
            if not waiters:
                del self.conflict_waiters[blocker]

    def _index_block(self, job: _Job) -> None:
        amr_id = job.amr.amr_id
        if job.blocked_node is not None:
            self.node_waiters.setdefault(job.blocked_node, set()).add(amr_id)
        for blocker in job.conflict_amrs:
            self.conflict_waiters.setdefault(blocker, set()).add(amr_id)

    def _wake_dram_waiters(self, progressed_amr: str) -> None:
        for amr_id in tuple(self.conflict_waiters.get(progressed_amr, ())):
            waiting = self.pending.get(amr_id)
            if (
                waiting is not None
                and waiting.blocked_reason == "dram_solver"
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
        return job.stage_route.distance_m + job.parking_continuation_distance

    def _remaining_path(self, job: _Job) -> tuple[tuple[int, int], ...]:
        if job.remaining_path_position == job.amr.position:
            return job.remaining_path_cache
        positions = job.stage_route.positions
        try:
            remaining = positions[positions.index(job.amr.position):]
        except ValueError:
            route = self.router.route(
                job.amr.position, job.stage_goal, frozenset(job.tabu),
                frozenset(job.tabu_edges),
            )
            remaining = route.positions if route is not None else (job.amr.position,)
        job.remaining_path_position = job.amr.position
        job.remaining_path_cache = remaining
        job.remaining_nodes = frozenset(remaining[1:])
        job.remaining_edges = frozenset(zip(remaining, remaining[1:]))
        return remaining

    def _priority_key(self, job: _Job) -> tuple[int, float, float, str]:
        request_time = job.request_time
        return (
            int(self.config.loaded_priority_enabled and not job.stage_loaded),
            self._remaining_distance(job),
            float(request_time) if request_time is not None else -math.inf,
            job.amr.amr_id,
        )

    def _clear_wait(self, job: _Job, now: float) -> None:
        self._clear_block_indexes(job)
        previous = self.wait_for.pop(job.amr.amr_id, set())
        if previous:
            self.coordination_generation += 1
            self.record(now, "wait_for_cleared", job, blockers=sorted(previous))
        if job.following_wait_since is not None:
            self.following_wait_seconds += now - job.following_wait_since
            job.following_wait_since = None
        if job.corridor_wait_since is not None:
            self.corridor_wait_seconds += now - job.corridor_wait_since
            job.corridor_wait_since = None

    def _set_wait(self, job: _Job, blockers: tuple[str, ...], now: float) -> None:
        if not self.config.mutex_passage_enabled or not blockers:
            return
        value = set(blockers)
        if self.wait_for.get(job.amr.amr_id) != value:
            self.wait_for[job.amr.amr_id] = value
            self.coordination_generation += 1
            self.record(now, "wait_for_updated", job, blockers=sorted(value))

    def _following(
        self,
        job: _Job,
        blocker_id: str,
        blocked: tuple[int, int],
        base_paths: dict[str, tuple[tuple[int, int], ...]],
    ) -> bool:
        blocker = self.active_jobs.get(blocker_id)
        if self.config.mutex_passage_enabled and blocker and self.native_kernel is not None:
            return self.native_kernel.following(
                self.native_agent_ids[job.amr.amr_id],
                self.native_agent_ids[blocker_id],
                self.native_node_ids[blocked],
            )
        return bool(
            self.config.mutex_passage_enabled
            and blocker
            and same_direction_following(
                base_paths[job.job_id], base_paths[blocker.job_id], blocked
            )
        )

    def _coordination_reroute(
        self,
        job: _Job,
        now: float,
        edge: tuple[tuple[int, int], tuple[int, int]],
        reason: str,
    ) -> bool:
        if self._hard_station_wait(job):
            return False
        job.tabu_edges.add(edge)
        route = self.router.route(
            job.amr.position, job.stage_goal, frozenset(job.tabu),
            frozenset(job.tabu_edges),
        )
        if route is None:
            job.tabu_edges.remove(edge)
            if reason not in {"wait_for_cycle", "coordination_liveness"}:
                return False
            route = self._parking_route(job)
            if route is None:
                return False
            job.parking_backoff = True
        self._clear_wait(job, now)
        self._set_stage_route(job, route)
        job.blocked_node = None
        job.blocked_reason = None
        job.conflict_signature = None
        job.conflict_amrs = ()
        job.wait_token += 1
        job.reroutes += 1
        self.reroute_count += 1
        self.pending_dirty.add(job.amr.amr_id)
        self._wake_dram_waiters(job.amr.amr_id)
        self.record(
            now, "coordination_reroute", job, reason=reason,
            blocked_edge=[grid_name(edge[0]), grid_name(edge[1])],
            path=[grid_name(node) for node in route.positions],
        )
        return True

    def _break_wait_cycle(self, now: float) -> str | None:
        cycle = wait_cycle(self.wait_for)
        if not cycle:
            return None
        jobs = [self.active_jobs[amr_id] for amr_id in cycle if amr_id in self.active_jobs]
        if not jobs:
            return None
        unloaded = [job for job in jobs if not job.stage_loaded]
        victim = max(unloaded or jobs, key=self._priority_key)
        path = self._remaining_path(victim)
        if len(path) < 2:
            return None
        self.wait_for_cycles += 1
        if victim.stage_loaded:
            self.record(now, "loaded_protection_overridden", victim, cycle=list(cycle))
        if self._coordination_reroute(victim, now, (path[0], path[1]), "wait_for_cycle"):
            self.cycle_breaking_reroutes += 1
            return victim.amr.amr_id
        return None

    def _release_passages(self, now: float) -> None:
        for key, passage in list(self.passages.items()):
            owner = self.active_jobs.get(passage.owner_front)
            path = self._remaining_path(owner) if owner else ()
            occupied = any(node in self.node_owners for node in passage.nodes)
            if not occupied and len(set(path) & set(passage.nodes)) < 2:
                del self.passages[key]
                self.coordination_generation += 1
                self.record(now, "corridor_released", owner, nodes=[grid_name(n) for n in passage.nodes])

    def _corridor_resolution(
        self,
        selected: _Job,
        involved: list[_Job],
        base_paths: dict[str, tuple[tuple[int, int], ...]],
        now: float,
    ) -> str:
        if (
            not self.config.corridor_coordination_enabled
            or len(involved) != 2
            or self._hard_station_wait(selected)
            or any(job.amr.amr_id not in self.pending for job in involved)
        ):
            return "deny"
        first, second = involved
        first_id, second_id = first.amr.amr_id, second.amr.amr_id
        if (
            second_id not in self.wait_for.get(first_id, set())
            or first_id not in self.wait_for.get(second_id, set())
        ):
            return "deny"
        if self.native_kernel is not None:
            passage_nodes = tuple(
                self.native_nodes[node]
                for node in self.native_kernel.reversed_passage(
                    self.native_agent_ids[first_id], self.native_agent_ids[second_id]
                )
            )
        else:
            passage_nodes = reversed_passage(
                base_paths[first.job_id], base_paths[second.job_id]
            )
        if len(passage_nodes) < 2:
            return "deny"
        key = frozenset(passage_nodes)
        passage = self.passages.get(key)
        if passage is None:
            queues = (
                following_queue(first.amr.amr_id, self.wait_for),
                following_queue(second.amr.amr_id, self.wait_for),
            )

            def has_unloaded(queue):
                return any(
                    not self.active_jobs[amr_id].stage_loaded
                    for amr_id in queue if amr_id in self.active_jobs
                )

            candidates = [(first, queues[0]), (second, queues[1])]
            preferred = [item for item in candidates if has_unloaded(item[1])]
            candidates = preferred or candidates
            shortest = min(len(queue) for _front, queue in candidates)
            candidates = [item for item in candidates if len(item[1]) == shortest]
            yielding_front, yielding_queue = max(
                candidates, key=lambda item: (self._priority_key(item[0]), item[0].amr.amr_id)
            )
            holding_front = second if yielding_front is first else first
            holding_path = base_paths[holding_front.job_id]
            ordered = sorted(passage_nodes, key=holding_path.index)
            passage = _Passage(
                tuple(ordered), holding_front.amr.amr_id,
                (ordered[0], ordered[-1]), yielding_queue,
            )
            self.passages[key] = passage
            self.coordination_generation += 1
            self.corridor_conflicts += 1
            self.corridor_ownership_changes += 1
            self.record(
                now, "corridor_owned", holding_front,
                nodes=[grid_name(node) for node in passage.nodes],
                holding_queue=list(queues[0] if holding_front is first else queues[1]),
                yielding_queue=list(yielding_queue),
            )
            tail_jobs = [
                self.active_jobs[amr_id]
                for amr_id in reversed(yielding_queue)
                if amr_id in self.active_jobs
            ]
            reroutable = [job for job in tail_jobs if not job.stage_loaded]
            for victim in reroutable or tail_jobs:
                path = base_paths[victim.job_id]
                indices = [i for i, node in enumerate(path) if node in key]
                if not indices:
                    continue
                index = min(indices)
                edge = (path[index - 1], path[index]) if index else (path[0], path[1])
                if edge in victim.tabu_edges:
                    continue
                if victim.stage_loaded:
                    self.record(now, "loaded_protection_overridden", victim, reason="corridor")
                if self._coordination_reroute(victim, now, edge, "corridor_tail_yield"):
                    self.corridor_yielding_amrs.add(victim.amr.amr_id)
                    if victim is selected:
                        return "rerouted"
                    break

        selected_path = base_paths[selected.job_id]
        indices = [i for i, node in enumerate(selected_path) if node in key]
        if len(indices) >= 2:
            direction = (selected_path[min(indices)], selected_path[max(indices)])
            if direction == passage.owner_direction:
                return "allow"
        return "deny"

    def _dram_conflict_after(
        self,
        selected: _Job,
        selected_index: int,
        position: tuple[int, int],
        context: _ConflictContext,
    ) -> tuple[DramConflict | None, list[_Job], _Job | None]:
        active, base_paths = context.active, context.paths
        original = base_paths[selected.job_id]
        selected_path = original[original.index(position):]
        candidates = tuple(
            sorted(
                (
                    context.active_indices[amr_id]
                    for amr_id in self.route_future_users.get(position, ())
                    if amr_id in context.active_indices
                )
            )
        )
        for index in candidates:
            if index == selected_index:
                continue
            path = base_paths[active[index].job_id]
            if path[0] not in selected_path[1:]:
                continue
            overlap = head_to_head_overlap(selected_path, path)
            if overlap is None:
                continue
            conflict = DramConflict("head_to_head", (selected_index, index), overlap)
            involved = [selected, active[index]]
            winner = min(involved, key=self._priority_key)
            if winner is not selected:
                return conflict, involved, winner

        node = selected_path[0]
        visited: dict[tuple[int, int], int] = {}
        order: list[tuple[tuple[int, int], int]] = []
        while True:
            if node == selected_path[0] and len(selected_path) >= 2:
                edge = (selected_path[1], selected_index)
            elif node == original[0]:
                edge = None
            else:
                edge = context.next_edges.get(node)
            if edge is None:
                break
            if node in visited:
                cycle = order[visited[node]:]
                indices = tuple(sorted({index for _node, index in cycle}))
                return (
                    DramConflict("cycle", indices, node),
                    [active[index] for index in indices],
                    None,
                )
            visited[node] = len(order)
            next_node, owner = edge
            order.append((node, owner))
            node = next_node
        return None, [], None

    def _native_conflict_after_prefix(
        self,
        selected: _Job,
        candidates: list[tuple[int, int]],
        context: _ConflictContext,
    ) -> tuple[int, DramConflict | None, list[_Job], _Job | None]:
        ranks = [len(self.amrs)] * len(self.amrs)
        for rank, job in enumerate(sorted(context.active, key=self._priority_key)):
            ranks[self.native_agent_ids[job.amr.amr_id]] = rank
        result = self.native_kernel.check_prefix(
            self.native_agent_ids[selected.amr.amr_id],
            tuple(self.native_node_ids[node] for node in candidates),
            tuple(ranks),
        )
        if result.kind is None:
            return result.safe_count, None, [], None
        involved = [
            self.active_jobs[self.amrs[index].amr_id]
            for index in result.participants
            if self.amrs[index].amr_id in self.active_jobs
        ]
        indices = tuple(
            sorted(context.active_indices[job.amr.amr_id] for job in involved)
        )
        conflict = DramConflict(
            result.kind,
            indices,
            self.native_nodes[result.overlap_node],
        )
        winner = (
            self.active_jobs.get(self.amrs[result.winner].amr_id)
            if result.winner is not None else None
        )
        return result.safe_count, conflict, involved, winner

    def _conflict_context(self) -> _ConflictContext:
        if self.active_cache is None:
            self.active_cache = tuple(
                self.active_jobs[amr_id] for amr_id in sorted(self.active_jobs)
            )
        paths = {job.job_id: self._remaining_path(job) for job in self.active_cache}
        if self.native_kernel is not None:
            for job in self.active_cache:
                self._sync_native_path(job, paths[job.job_id])
        next_edges = {
            path[0]: (path[1], index)
            for index, job in enumerate(self.active_cache)
            if len(path := paths[job.job_id]) >= 2
        }
        return _ConflictContext(
            self.active_cache,
            paths,
            {job.amr.amr_id: index for index, job in enumerate(self.active_cache)},
            next_edges,
        )

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
            key=self._priority_key,
        )
        context = self._conflict_context()
        if self.native_kernel is not None:
            self.native_resolution_active = True
        active = context.active
        active_indices = context.active_indices
        base_paths = context.paths
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
            following_blocked = False
            coordination_rerouted = False
            corridor_blocked = False
            for node in desired:
                owner = self.node_owners.get(node)
                if owner is not None and owner != job.amr.amr_id:
                    blocker = node
                    blocking_owner = owner
                    following_blocked = self._following(job, owner, node, base_paths)
                    break
                available.append(node)
            if blocker is None:
                safe = []
                remaining = available
                while remaining:
                    if self.native_kernel is not None:
                        count, conflict, conflicting_jobs, winner = (
                            self._native_conflict_after_prefix(job, remaining, context)
                        )
                        safe.extend(remaining[:count])
                        node = remaining[count] if conflict is not None else None
                    else:
                        node = remaining[0]
                        conflict, conflicting_jobs, winner = self._dram_conflict_after(
                            job,
                            active_indices[job.amr.amr_id],
                            node,
                            context,
                        )
                        count = 0
                    if conflict is None:
                        if self.native_kernel is None:
                            safe.append(node)
                            remaining = remaining[1:]
                            continue
                        break
                    if conflict.kind == "head_to_head":
                        corridor = self._corridor_resolution(
                            job, conflicting_jobs, base_paths, now
                        )
                        if corridor == "allow":
                            conflict = None
                            safe.append(node)
                            remaining = remaining[count + 1:]
                            continue
                        if corridor == "rerouted":
                            coordination_rerouted = True
                        elif corridor == "deny":
                            corridor_blocked = True
                    blocker = conflict.overlap_node
                    break
                available = safe
            if coordination_rerouted:
                continue
            if atomic and blocker is not None:
                available = []
            if not available:
                self._clear_block_indexes(job)
                job.blocked_node = blocker
                solver_blocked = conflict is not None
                reason = (
                    "following" if following_blocked
                    else "corridor" if corridor_blocked
                    else "dram_solver" if solver_blocked
                    else "node_owned"
                )
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
                self._set_wait(job, conflict_amrs, now)
                if following_blocked:
                    if job.following_wait_since is None:
                        job.following_wait_since = now
                        self.following_avoided_reroutes += 1
                    self.record(now, "following_wait", job, leader=blocking_owner)
                if corridor_blocked and job.corridor_wait_since is None:
                    job.corridor_wait_since = now
                    self.record(now, "corridor_wait", job)
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
                    if (
                        not following_blocked
                        and not corridor_blocked
                        and not (self.config.loaded_priority_enabled and job.stage_loaded)
                    ):
                        self.schedule(now + timeout, "reservation_timeout", (job, job.wait_token))
                    else:
                        if self.config.loaded_priority_enabled and job.stage_loaded:
                            self.loaded_protected_waits += 1
                        self.schedule(
                            now + self.config.dram_conflict_wait_seconds,
                            "coordination_timeout", (job, job.wait_token),
                        )
                self._index_block(job)
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
            if self.config.loaded_priority_enabled and job.stage_loaded:
                self.loaded_priority_grants += 1
            for node in available:
                self.node_owners[node] = job.amr.amr_id
            self.coordination_generation += 1
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
            self._clear_wait(job, now)
            job.request_time = job.wait_since = None
            job.wait_token += 1
            job.blocked_node = None
            job.blocked_reason = None
            job.conflict_signature = None
            job.conflict_amrs = ()
            self._move_reserved(job, available, now)
        if self.native_kernel is not None:
            self.native_resolution_active = False
            for amr_id, positions in self.native_deferred_paths.items():
                agent = self.native_agent_ids[amr_id]
                if positions is None:
                    self.native_kernel.remove(agent)
                    self.native_synced_paths.pop(amr_id, None)
                else:
                    self.native_kernel.set_path(
                        agent,
                        tuple(self.native_node_ids[node] for node in positions),
                    )
                    self.native_synced_paths[amr_id] = positions
            self.native_deferred_paths.clear()

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
            self.active_cache = None
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
            "loaded_priority_grants": self.loaded_priority_grants,
            "loaded_protected_waits": self.loaded_protected_waits,
            "following_wait_seconds": self.following_wait_seconds,
            "following_avoided_reroutes": self.following_avoided_reroutes,
            "wait_for_cycles": self.wait_for_cycles,
            "cycle_breaking_reroutes": self.cycle_breaking_reroutes,
            "corridor_conflicts": self.corridor_conflicts,
            "corridor_ownership_changes": self.corridor_ownership_changes,
            "corridor_yielding_amrs": len(self.corridor_yielding_amrs),
            "corridor_wait_seconds": self.corridor_wait_seconds,
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
    coordination_backend: str = "python",
    simulation_backend: str = "python",
) -> DayResult:
    if not tasks:
        raise ValueError("cannot simulate an empty day")
    dates = {task.task_date for task in tasks}
    if len(dates) != 1:
        raise ValueError("simulate_day requires tasks from exactly one date")
    if simulation_backend == "native":
        from .native_day_backend import load_day_engine

        native, _info = load_day_engine("native")
        return native.simulate(
            router, racks, tasks, workstation_mapping, config, trace
        )
    if simulation_backend != "python":
        raise ValueError(f"unsupported selected simulation backend: {simulation_backend}")
    return _Engine(
        project, router, racks, tasks, workstation_mapping, config, trace,
        coordination_backend,
    ).run()
