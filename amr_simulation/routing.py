"""Directed A* routing and acceleration-aware AMR motion timing."""

from __future__ import annotations

import heapq
import math
from collections import deque
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
        self.coordinates = {
            position: project.coordinates(*position) for position in self.positions
        }
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
        self.edge_weights: dict[tuple[GridPosition, GridPosition], float] = {}
        for start, end in project.iter_traversable_lane_positions():
            if start not in self.graph or end not in self.graph:
                continue
            x1, y1 = self.coordinates[start]
            x2, y2 = self.coordinates[end]
            weight = math.hypot(x2 - x1, y2 - y1)
            self.graph[start].append((end, weight))
            self.edge_weights[(start, end)] = weight
        for edges in self.graph.values():
            edges.sort(key=lambda item: item[0])
        self._edge_directions = {
            (start, end): self._direction(start, end)
            for start, edges in self.graph.items()
            for end, _weight in edges
        }
        self._goal_heuristics: dict[GridPosition, dict[GridPosition, float]] = {}
        self._endpoint_obstacles: dict[
            tuple[GridPosition, GridPosition], frozenset[GridPosition]
        ] = {}
        self._reachability: dict[tuple[GridPosition, GridPosition], bool] = {}
        self._cache: dict[
            tuple[
                GridPosition,
                GridPosition,
                frozenset[GridPosition],
                frozenset[tuple[GridPosition, GridPosition]],
            ],
            Route | None,
        ] = {}
        self._runs_cache: dict[tuple[GridPosition, ...], tuple[StraightRun, ...]] = {}

    def route(
        self,
        start: GridPosition,
        goal: GridPosition,
        blocked_nodes: frozenset[GridPosition] = frozenset(),
        blocked_edges: frozenset[tuple[GridPosition, GridPosition]] = frozenset(),
    ) -> Route | None:
        key = (start, goal, blocked_nodes, blocked_edges)
        if key in self._cache:
            return self._cache[key]
        if start not in self.graph or goal not in self.graph:
            self._cache[key] = None
            return None
        endpoint_key = (start, goal)
        base_blocked = self._endpoint_obstacles.get(endpoint_key)
        if base_blocked is None:
            base_blocked = frozenset(self.rack_positions - {start, goal})
            self._endpoint_obstacles[endpoint_key] = base_blocked
        blocked = base_blocked | blocked_nodes
        if goal in blocked:
            self._cache[key] = None
            return None
        heuristics = self._goal_heuristics.get(goal)
        if heuristics is None:
            goal_x, goal_y = self.coordinates[goal]
            heuristics = {
                node: math.hypot(x - goal_x, y - goal_y)
                for node, (x, y) in self.coordinates.items()
            }
            self._goal_heuristics[goal] = heuristics
        graph = self.graph
        directions = self._edge_directions
        start_state = (start, (0, 0))
        costs = {start_state: (0.0, 0)}
        previous = {start_state: None}
        queue = [(heuristics[start], 0, 0.0, start, (0, 0))]
        push, pop = heapq.heappush, heapq.heappop
        infinity = math.inf
        while queue:
            _priority, turns, travelled, node, incoming = pop(queue)
            state = (node, incoming)
            best_distance, best_turns = costs.get(state, (infinity, infinity))
            if travelled > best_distance + 1e-9 or (
                abs(travelled - best_distance) <= 1e-9 and turns > best_turns
            ):
                continue
            if node == goal:
                states = [state]
                while previous[states[-1]] is not None:
                    states.append(previous[states[-1]])
                result = Route(tuple(item[0] for item in reversed(states)), travelled)
                self._cache[key] = result
                return result
            for neighbour, weight in graph[node]:
                if neighbour in blocked or (node, neighbour) in blocked_edges:
                    continue
                candidate = travelled + weight
                direction = directions.get((node, neighbour))
                if direction is None:
                    direction = self._direction(node, neighbour)
                candidate_turns = turns + int(incoming != (0, 0) and incoming != direction)
                neighbour_state = (neighbour, direction)
                old_distance, old_turns = costs.get(neighbour_state, (infinity, infinity))
                if candidate + 1e-9 < old_distance or (
                    abs(candidate - old_distance) <= 1e-9 and candidate_turns < old_turns
                ):
                    costs[neighbour_state] = (candidate, candidate_turns)
                    previous[neighbour_state] = state
                    push(
                        queue,
                        (
                            candidate + heuristics[neighbour],
                            candidate_turns,
                            candidate,
                            neighbour,
                            direction,
                        ),
                    )
        self._cache[key] = None
        return None

    @staticmethod
    def _direction(start: GridPosition, end: GridPosition) -> tuple[int, int]:
        dx, dy = end[0] - start[0], end[1] - start[1]
        divisor = math.gcd(abs(dx), abs(dy))
        return dx // divisor, dy // divisor

    def _heuristic(self, start: GridPosition, goal: GridPosition) -> float:
        x1, y1 = self.coordinates[start]
        x2, y2 = self.coordinates[goal]
        return math.hypot(x2 - x1, y2 - y1)

    def reachable(self, start: GridPosition, goal: GridPosition) -> bool:
        """Return static directed reachability without constructing an A* route."""
        key = (start, goal)
        if key in self._reachability:
            return self._reachability[key]
        if start not in self.graph or goal not in self.graph:
            self._reachability[key] = False
            return False
        blocked = self._endpoint_obstacles.get(key)
        if blocked is None:
            blocked = frozenset(self.rack_positions - {start, goal})
            self._endpoint_obstacles[key] = blocked
        queue = deque((start,))
        visited = {start}
        while queue:
            node = queue.popleft()
            if node == goal:
                self._reachability[key] = True
                return True
            for neighbour, _weight in self.graph[node]:
                if neighbour not in blocked and neighbour not in visited:
                    visited.add(neighbour)
                    queue.append(neighbour)
        self._reachability[key] = False
        return False

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
            position = self.coordinates[run.start]
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

    def route_distance(self, positions: tuple[GridPosition, ...]) -> float:
        return sum(
            self.edge_weights[(start, end)]
            for start, end in zip(positions, positions[1:])
        )

    def crossing_times(
        self, route: Route, segments: list[MotionSegment]
    ) -> list[tuple[GridPosition, float]]:
        """Return the simulated time at which each forward route node is reached."""
        linear = [segment for segment in segments if segment.kind.startswith("linear_")]
        result: list[tuple[GridPosition, float]] = []
        previous_time = segments[0].start_time if segments else 0.0
        for position in route.positions[1:]:
            target = self.coordinates[position]
            for segment in linear:
                if segment.end_time + 1e-9 < previous_time:
                    continue
                sx, sy = segment.start_position
                ex, ey = segment.end_position
                dx, dy = ex - sx, ey - sy
                length = math.hypot(dx, dy)
                if length <= 1e-12:
                    continue
                projection = ((target[0] - sx) * dx + (target[1] - sy) * dy) / length
                cross = dx * (target[1] - sy) - dy * (target[0] - sx)
                if abs(cross) > 1e-7 or projection < -1e-9 or projection > length + 1e-9:
                    continue
                distance = min(length, max(0.0, projection))
                if abs(segment.acceleration) <= 1e-12:
                    elapsed = distance / segment.start_rate
                else:
                    discriminant = max(
                        0.0,
                        segment.start_rate**2 + 2 * segment.acceleration * distance,
                    )
                    roots = [
                        (-segment.start_rate + sign * math.sqrt(discriminant))
                        / segment.acceleration
                        for sign in (1.0, -1.0)
                    ]
                    elapsed = min(
                        value
                        for value in roots
                        if -1e-9 <= value <= segment.end_time - segment.start_time + 1e-9
                    )
                reached = segment.start_time + max(0.0, elapsed)
                if reached + 1e-9 >= previous_time:
                    result.append((position, reached))
                    previous_time = reached
                    break
            else:
                raise RuntimeError(f"motion does not cross route node {position}")
        return result

    def _runs(self, route: Route) -> tuple[StraightRun, ...]:
        if route.positions not in self._runs_cache:
            positions = route.positions
            if len(positions) < 2:
                runs = []
            else:
                run_start, previous = positions[0], positions[1]
                x0, y0 = self.coordinates[positions[0]]
                x1, y1 = self.coordinates[positions[1]]
                direction = (x1 - x0, y1 - y0)
                distance = math.hypot(*direction)
                runs = []
                for current in positions[2:]:
                    px, py = self.coordinates[previous]
                    cx, cy = self.coordinates[current]
                    candidate = (cx - px, cy - py)
                    cross = direction[0] * candidate[1] - direction[1] * candidate[0]
                    dot = direction[0] * candidate[0] + direction[1] * candidate[1]
                    if abs(cross) <= 1e-9 and dot > 0:
                        distance += math.hypot(*candidate)
                    else:
                        runs.append(StraightRun(
                            run_start, previous, distance,
                            math.atan2(direction[1], direction[0]),
                        ))
                        run_start, direction, distance = (
                            previous, candidate, math.hypot(*candidate)
                        )
                    previous = current
                runs.append(StraightRun(
                    run_start, positions[-1], distance,
                    math.atan2(direction[1], direction[0]),
                ))
            self._runs_cache[route.positions] = tuple(runs)
        return self._runs_cache[route.positions]
