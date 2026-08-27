"""Heap-based discrete-event scheduler for AMR rack presentations."""

from __future__ import annotations

import heapq
import math
from collections import deque
from dataclasses import dataclass, field
from datetime import date

from warehouse_layout.domain import GridProject

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
    station_entry_route: Route | None = None
    station_queue_enter: float | None = None
    station_admitted: float | None = None
    station_arrival: float | None = None
    service_start: float | None = None
    completion_time: float | None = None
    travel_distance_m: float = 0.0
    travel_seconds: float = 0.0


@dataclass(slots=True)
class _Station:
    station_id: str
    position: tuple[int, int]
    busy: bool = False
    queue: deque[_Job] = field(default_factory=deque)
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
        self.events: list[dict] = []
        self.motion_segments = []
        self.paths: dict[str, list[tuple[int, int]]] = {}
        self.dispatch_needed = False

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
                self._handle(when, kind, payload)
            if self.dispatch_needed:
                self.dispatch_needed = False
                self._dispatch(now)
        incomplete = [task.source.task_id for task in self.tasks if task.completed_at is None]
        if incomplete:
            raise RuntimeError("simulation deadlocked with incomplete tasks: " + ", ".join(incomplete[:10]))
        return self._result()

    def _handle(self, now: float, kind: str, payload: object) -> None:
        if kind == "task_release":
            task = payload
            task.released = True
            self.dispatch_needed = True
            self.record(now, kind, task_id=task.source.task_id, store_id=task.source.store_id)
            return
        job = payload[0] if isinstance(payload, tuple) else payload
        if kind in {"pickup_arrival", "station_queue_arrival", "station_arrival", "rack_home"}:
            _job, position, heading = payload
            job.amr.position, job.amr.heading = position, heading
        self.record(now, kind, job)
        if kind == "pickup_arrival":
            self._stationary(job, "jack_up", now, now + self.config.jack_up_seconds, job.rack.position, False)
            self.schedule(now + self.config.jack_up_seconds, "jack_up_done", job)
        elif kind == "jack_up_done":
            job.rack_departure = now
            route = self._required_route(job.rack.position, self.stations[job.workstation].position)
            approach, job.station_entry_route = self._split_station_entry(route)
            job.station_queue_position = approach.positions[-1]
            self._move(job, approach, now, True, "station_queue_arrival")
        elif kind == "station_queue_arrival":
            station = self.stations[job.workstation]
            job.station_queue_enter = now
            station.queue.append(job)
            self.record(now, "station_queue_enter", job, queue_length=len(station.queue))
            if not station.busy:
                self._admit_station(station, now)
            station.max_queue = max(station.max_queue, len(station.queue))
        elif kind == "station_arrival":
            station = self.stations[job.workstation]
            job.station_arrival = now
            job.service_start = now
            self._stationary(job, "service", now, now + self.config.service_seconds, station.position, True)
            self.record(now, "service_start", job, queue_length=len(station.queue))
            self.schedule(now + self.config.service_seconds, "service_done", job)
        elif kind == "service_done":
            station = self.stations[job.workstation]
            station.busy = False
            station.service_seconds += self.config.service_seconds
            route = self._required_route(station.position, job.rack.position)
            self._move(job, route, now, True, "rack_home")
            if station.queue:
                self._admit_station(station, now)
        elif kind == "rack_home":
            job.rack_return = now
            self._stationary(job, "jack_down", now, now + self.config.jack_down_seconds, job.rack.position, True)
            self.schedule(now + self.config.jack_down_seconds, "jack_down_done", job)
        elif kind == "jack_down_done":
            self.reserved_racks.remove(job.rack.rack_id)
            job.amr.free = True
            self.dispatch_needed = True
            job.amr.busy_seconds += now - job.dispatch_time
            job.completion_time = now
            job.task.inflight -= 1
            completed = sum(job.lines.values())
            job.task.completed_lines += completed
            if not job.task.outstanding and not job.task.inflight:
                job.task.completed_at = now
                self.record(now, "task_complete", job, completed_lines=job.task.completed_lines)

    def _admit_station(self, station: _Station, now: float) -> None:
        job = station.queue.popleft()
        station.busy = True
        job.station_admitted = now
        queue_enter = float(job.station_queue_enter)
        station.queue_wait_seconds += now - queue_enter
        self._stationary(
            job, "station_queue_wait", queue_enter, now,
            job.station_queue_position, True,
        )
        self.record(now, "station_admitted", job, queue_length=len(station.queue))
        self._move(job, job.station_entry_route, now, True, "station_arrival")

    def _split_station_entry(self, route: Route) -> tuple[Route, Route]:
        if len(route.positions) < 2:
            raise RuntimeError("rack and workstation cannot share one grid node")
        queue_position, station_position = route.positions[-2:]
        x1, y1 = self.project.coordinates(*queue_position)
        x2, y2 = self.project.coordinates(*station_position)
        entry_distance = math.hypot(x2 - x1, y2 - y1)
        return (
            Route(route.positions[:-1], route.distance_m - entry_distance),
            Route(route.positions[-2:], entry_distance),
        )

    def _required_route(self, start: tuple[int, int], goal: tuple[int, int]) -> Route:
        route = self.router.route(start, goal)
        if route is None:
            raise RuntimeError(f"validated route became unavailable: {start} -> {goal}")
        return route

    def _move(
        self, job: _Job, route: Route, now: float, loaded: bool, arrival_kind: str
    ) -> None:
        end, heading, segments = self.router.motion(
            job.job_id, job.amr.amr_id, route, now, job.amr.heading,
            self.config.motion, loaded,
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
        self.schedule(end, arrival_kind, (job, route.positions[-1], heading))

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
                job.job_id, job.amr.amr_id, kind, start, end,
                coordinates, coordinates, job.amr.heading, job.amr.heading,
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
                candidates = []
                candidate_ids: set[str] = set()
                for sku in task.outstanding:
                    candidate_ids.update(self.racks_by_sku.get(sku, ()))
                for rack_id in candidate_ids:
                    rack = self.racks[rack_id]
                    if rack.rack_id in self.reserved_racks:
                        continue
                    covered = sorted(rack.skus & task.outstanding.keys())
                    if not covered:
                        continue
                    outbound = self.router.route(rack.position, station_position)
                    if outbound is None:
                        continue
                    candidates.append((-len(covered), outbound.distance_m, rack.rack_id, rack, covered))
                if candidates:
                    for candidate in sorted(candidates):
                        rack = candidate[3]
                        amr_choices = []
                        for amr in free_amrs:
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
                f"J{self.job_sequence:06d}", task, rack, lines, amr,
                task.workstation, now,
            )
            self.jobs.append(job)
            self.record(now, "dispatch", job, covered_skus=covered, covered_lines=sum(lines.values()))
            self._move(job, pickup, now, False, "pickup_arrival")

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
