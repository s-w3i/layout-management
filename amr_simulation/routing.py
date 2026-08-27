"""Directed A* routing and acceleration-aware AMR motion timing."""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass

from warehouse_layout.domain import GridPosition, GridProject

from .models import MotionProfile, MotionSegment


@dataclass(frozen=True, slots=True)
class Route:
    positions: tuple[GridPosition, ...]
    distance_m: float


@dataclass(frozen=True, slots=True)
class StraightRun:
    start: GridPosition
    end: GridPosition
    distance_m: float
    heading: float


def normalize_angle(value: float) -> float:
    return (value + math.pi) % (2 * math.pi) - math.pi


def motion_phases(
    amount: float, maximum_rate: float, acceleration: float
) -> list[tuple[str, float, float, float, float]]:
    """Return phase name, duration, amount, starting rate, and ending rate."""
    if amount <= 1e-12:
        return []
    threshold = maximum_rate**2 / acceleration
    if amount <= threshold + 1e-12:
        peak = math.sqrt(amount * acceleration)
        duration = peak / acceleration
        return [
            ("accel", duration, amount / 2, 0.0, peak),
            ("decel", duration, amount / 2, peak, 0.0),
        ]
    acceleration_time = maximum_rate / acceleration
    acceleration_amount = 0.5 * maximum_rate**2 / acceleration
    cruise_amount = amount - 2 * acceleration_amount
    return [
        ("accel", acceleration_time, acceleration_amount, 0.0, maximum_rate),
        ("cruise", cruise_amount / maximum_rate, cruise_amount, maximum_rate, maximum_rate),
        ("decel", acceleration_time, acceleration_amount, maximum_rate, 0.0),
    ]


def merge_straight_runs(project: GridProject, path: tuple[GridPosition, ...]) -> list[StraightRun]:
    if len(path) < 2:
        return []
    run_start, previous = path[0], path[1]
    x0, y0 = project.coordinates(*path[0])
    x1, y1 = project.coordinates(*path[1])
    direction = (x1 - x0, y1 - y0)
    distance = math.hypot(*direction)
    runs: list[StraightRun] = []
    for current in path[2:]:
        px, py = project.coordinates(*previous)
        cx, cy = project.coordinates(*current)
        candidate = (cx - px, cy - py)
        cross = direction[0] * candidate[1] - direction[1] * candidate[0]
        dot = direction[0] * candidate[0] + direction[1] * candidate[1]
        if abs(cross) <= 1e-9 and dot > 0:
            distance += math.hypot(*candidate)
        else:
            runs.append(
                StraightRun(run_start, previous, distance, math.atan2(direction[1], direction[0]))
            )
            run_start, direction, distance = previous, candidate, math.hypot(*candidate)
        previous = current
    runs.append(StraightRun(run_start, path[-1], distance, math.atan2(direction[1], direction[0])))
    return runs


class GridRouter:
    """Deterministic routing with every non-endpoint rack treated as blocked."""

    def __init__(self, project: GridProject):
        self.project = project
        self.positions = set(project.iter_positions())
        self.rack_positions = {
            position for position, marker in project.markers.items() if marker.role == "rack"
        }
        self.workstations = {
            marker.endpoint_id: position
            for position, marker in project.markers.items()
            if marker.role == "workstation"
        }
        self.graph: dict[GridPosition, list[tuple[GridPosition, float]]] = {
            position: [] for position in self.positions
        }
        for start, end in project.iter_traversable_lane_positions():
            if start not in self.graph or end not in self.graph:
                continue
            x1, y1 = project.coordinates(*start)
            x2, y2 = project.coordinates(*end)
            self.graph[start].append((end, math.hypot(x2 - x1, y2 - y1)))
        for edges in self.graph.values():
            edges.sort(key=lambda item: item[0])
        self._cache: dict[tuple[GridPosition, GridPosition], Route | None] = {}
        self._runs_cache: dict[tuple[GridPosition, ...], tuple[StraightRun, ...]] = {}

    def route(self, start: GridPosition, goal: GridPosition) -> Route | None:
        key = (start, goal)
        if key in self._cache:
            return self._cache[key]
        if start not in self.graph or goal not in self.graph:
            self._cache[key] = None
            return None
        blocked = self.rack_positions - {start, goal}
        distances = {start: 0.0}
        previous: dict[GridPosition, GridPosition | None] = {start: None}
        queue = [(self._heuristic(start, goal), 0.0, start)]
        while queue:
            _priority, travelled, node = heapq.heappop(queue)
            if travelled > distances[node] + 1e-9:
                continue
            if node == goal:
                path = [node]
                while previous[path[-1]] is not None:
                    path.append(previous[path[-1]])
                result = Route(tuple(reversed(path)), travelled)
                self._cache[key] = result
                return result
            for neighbour, weight in self.graph[node]:
                if neighbour in blocked:
                    continue
                candidate = travelled + weight
                if candidate + 1e-9 < distances.get(neighbour, math.inf):
                    distances[neighbour] = candidate
                    previous[neighbour] = node
                    heapq.heappush(
                        queue,
                        (candidate + self._heuristic(neighbour, goal), candidate, neighbour),
                    )
        self._cache[key] = None
        return None

    def _heuristic(self, start: GridPosition, goal: GridPosition) -> float:
        x1, y1 = self.project.coordinates(*start)
        x2, y2 = self.project.coordinates(*goal)
        return math.hypot(x2 - x1, y2 - y1)

    def motion(
        self,
        job_id: str,
        amr_id: str,
        route: Route,
        start_time: float,
        start_heading: float,
        profile: MotionProfile,
        loaded: bool,
    ) -> tuple[float, float, list[MotionSegment]]:
        now, heading = start_time, start_heading
        segments: list[MotionSegment] = []
        angular_speed = math.radians(profile.max_angular_speed_degps)
        angular_acceleration = math.radians(profile.angular_acceleration_degps2)
        for run in self._runs(route):
            position = self.project.coordinates(*run.start)
            turn = normalize_angle(run.heading - heading)
            sign = 1.0 if turn >= 0 else -1.0
            for name, duration, amount, start_rate, end_rate in motion_phases(
                abs(turn), angular_speed, angular_acceleration
            ):
                end_heading = normalize_angle(heading + sign * amount)
                segments.append(
                    MotionSegment(
                        job_id, amr_id, f"rotate_{name}", now, now + duration,
                        position, position, heading, end_heading,
                        sign * start_rate, sign * (end_rate - start_rate) / duration, loaded,
                    )
                )
                now, heading = now + duration, end_heading
            heading = run.heading
            x, y = position
            for name, duration, amount, start_rate, end_rate in motion_phases(
                run.distance_m, profile.max_linear_speed_mps, profile.linear_acceleration_mps2
            ):
                end = (x + math.cos(heading) * amount, y + math.sin(heading) * amount)
                segments.append(
                    MotionSegment(
                        job_id, amr_id, f"linear_{name}", now, now + duration,
                        (x, y), end, heading, heading,
                        start_rate, (end_rate - start_rate) / duration, loaded,
                    )
                )
                now, (x, y) = now + duration, end
        return now, normalize_angle(heading), segments

    def predicted_motion_time(
        self, route: Route, heading: float, profile: MotionProfile
    ) -> float:
        duration = 0.0
        angular_speed = math.radians(profile.max_angular_speed_degps)
        angular_acceleration = math.radians(profile.angular_acceleration_degps2)
        for run in self._runs(route):
            duration += sum(
                phase[1]
                for phase in motion_phases(
                    abs(normalize_angle(run.heading - heading)),
                    angular_speed,
                    angular_acceleration,
                )
            )
            duration += sum(
                phase[1]
                for phase in motion_phases(
                    run.distance_m,
                    profile.max_linear_speed_mps,
                    profile.linear_acceleration_mps2,
                )
            )
            heading = run.heading
        return duration

    def _runs(self, route: Route) -> tuple[StraightRun, ...]:
        if route.positions not in self._runs_cache:
            self._runs_cache[route.positions] = tuple(
                merge_straight_runs(self.project, route.positions)
            )
        return self._runs_cache[route.positions]
