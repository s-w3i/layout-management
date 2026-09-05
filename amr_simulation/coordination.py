"""Pure deterministic DRAM coordination state and decisions."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Hashable, Mapping, Sequence

from .models import CoordinationConfig, CoordinationStatus


Node = Hashable
Edge = tuple[Node, Node]


@dataclass(slots=True)
class RobotCoordinationState:
    robot_id: str
    path: tuple[Node, ...]
    loaded: bool
    priority: int = 0
    physical_index: int = 0
    acknowledged_index: int = 0
    authorized_index: int = 0
    revision: int = 1
    reserved: set[Node] = field(default_factory=set)
    tabu_edges: set[Edge] = field(default_factory=set)
    wait_for: set[str] = field(default_factory=set)
    blocked_since: float | None = None
    last_progress: float = 0.0


@dataclass(frozen=True, slots=True)
class Conflict:
    kind: str
    participants: tuple[str, ...]
    node: Node | None = None
    edge: Edge | None = None
    step: int = 0


@dataclass(frozen=True, slots=True)
class ReservationDecision:
    robot_id: str
    granted: tuple[Node, ...]
    blocker: tuple[Node, str] | None = None


def next_epoch(now: float, period: float) -> float:
    return math.ceil((now - 1e-12) / period) * period


def straight_window(
    path: Sequence[Node], start: int, maximum: int, coordinates: Mapping[Node, tuple[float, float]]
) -> tuple[Node, ...]:
    """Return a bounded straight prefix, including the occupied start node."""
    result: list[Node] = []
    direction: tuple[float, float] | None = None
    for index in range(start, min(len(path), start + maximum)):
        if index > start:
            before, current = coordinates[path[index - 1]], coordinates[path[index]]
            candidate = (current[0] - before[0], current[1] - before[1])
            if direction is not None:
                cross = direction[0] * candidate[1] - direction[1] * candidate[0]
                dot = direction[0] * candidate[0] + direction[1] * candidate[1]
                if abs(cross) > 1e-9 or dot <= 0:
                    break
            direction = candidate
        result.append(path[index])
    return tuple(result)


def wait_for_cycles(wait_for: Mapping[str, set[str]]) -> tuple[tuple[str, ...], ...]:
    """Return deterministic directed cycles as strongly connected components."""
    index = 0
    indices: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    found: list[tuple[str, ...]] = []

    def visit(robot: str) -> None:
        nonlocal index
        indices[robot] = low[robot] = index
        index += 1
        stack.append(robot)
        on_stack.add(robot)
        for other in sorted(wait_for.get(robot, ())):
            if other not in indices:
                visit(other)
                low[robot] = min(low[robot], low[other])
            elif other in on_stack:
                low[robot] = min(low[robot], indices[other])
        if low[robot] != indices[robot]:
            return
        component = []
        while True:
            other = stack.pop()
            on_stack.remove(other)
            component.append(other)
            if other == robot:
                break
        members = tuple(sorted(component))
        if len(members) > 1 or robot in wait_for.get(robot, set()):
            found.append(members)

    for robot in sorted(wait_for):
        if robot not in indices:
            visit(robot)
    return tuple(sorted(found))


def detect_conflicts(
    robots: Mapping[str, RobotCoordinationState], config: CoordinationConfig
) -> tuple[Conflict, ...]:
    conflicts: set[Conflict] = set()
    names = sorted(robots)
    for offset, first_name in enumerate(names):
        first = robots[first_name].path[robots[first_name].physical_index :]
        for second_name in names[offset + 1 :]:
            second = robots[second_name].path[robots[second_name].physical_index :]
            for step in range(min(config.head_to_head_window - 1, len(first) - 1, len(second) - 1)):
                if first[step] == second[step + 1] and first[step + 1] == second[step]:
                    conflicts.add(Conflict("head_to_head", (first_name, second_name), edge=(first[step], first[step + 1]), step=step + 1))
                    break
            for step in range(min(config.head_to_head_window, len(first), len(second))):
                if first[step] == second[step] and step:
                    conflicts.add(Conflict("path_overlap", (first_name, second_name), node=first[step], step=step))
                    break
    for members in wait_for_cycles({name: state.wait_for for name, state in robots.items()}):
        conflicts.add(Conflict("wait_for_deadlock", members))
    intended = {
        name: state.path[state.physical_index + 1]
        for name, state in robots.items()
        if state.physical_index + 1 < min(
            len(state.path), state.physical_index + config.partial_cycle_window
        )
    }
    occupied = {
        state.path[state.physical_index]: name for name, state in robots.items()
    }
    dependencies = {
        name: {occupied[target]} if target in occupied and occupied[target] != name else set()
        for name, target in intended.items()
    }
    for members in wait_for_cycles(dependencies):
        conflicts.add(Conflict("partial_cycle", members))
    corridor_nodes = [set(corridor) for corridor in config.corridors]
    for conflict in tuple(conflicts):
        if (
            conflict.kind == "head_to_head"
            and conflict.edge is not None
            and any(set(conflict.edge) <= nodes for nodes in corridor_nodes)
        ):
            queued = set(conflict.participants)
            changed = True
            while changed:
                changed = False
                for name, state in robots.items():
                    if name not in queued and state.wait_for & queued:
                        queued.add(name)
                        changed = True
            if len(queued) >= 3:
                conflicts.add(Conflict("corridor_deadlock", tuple(sorted(queued)), edge=conflict.edge))
    return tuple(sorted(conflicts, key=lambda item: (item.kind, item.step, item.participants, repr(item.node), repr(item.edge))))


def can_replan(
    robot: RobotCoordinationState,
    graph: Mapping[Node, Sequence[tuple[Node, float]]],
    owners: Mapping[Node, str],
    rack_nodes: set[Node] | frozenset[Node] = frozenset(),
) -> bool:
    current = robot.path[robot.physical_index]
    return any(
        owners.get(neighbour) in (None, robot.robot_id)
        and (not robot.loaded or neighbour not in rack_nodes)
        and (current, neighbour) not in robot.tabu_edges
        for neighbour, _weight in graph.get(current, ())
    )


def select_replan_robot(
    participants: Sequence[str],
    robots: Mapping[str, RobotCoordinationState],
    graph: Mapping[Node, Sequence[tuple[Node, float]]],
    owners: Mapping[Node, str],
    rack_nodes: set[Node] | frozenset[Node] = frozenset(),
) -> str | None:
    ordered = sorted(
        participants,
        key=lambda name: (
            robots[name].loaded,
            robots[name].priority,
            -len(robots[name].path[robots[name].physical_index :]),
            name,
        ),
    )
    return next(
        (name for name in ordered if can_replan(robots[name], graph, owners, rack_nodes)),
        None,
    )


def corridor_sides(
    conflict: Conflict,
    robots: Mapping[str, RobotCoordinationState],
    corridors: Sequence[Sequence[Node]],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    for corridor in corridors:
        positions = {node: index for index, node in enumerate(corridor)}
        sides: dict[int, list[tuple[int, str]]] = {-1: [], 1: []}
        for name in conflict.participants:
            robot = robots[name]
            index = robot.physical_index
            if index + 1 >= len(robot.path):
                continue
            current, following = robot.path[index : index + 2]
            if current not in positions or following not in positions:
                continue
            direction = 1 if positions[following] > positions[current] else -1
            front_order = -positions[current] if direction > 0 else positions[current]
            sides[direction].append((front_order, name))
        if sides[-1] and sides[1]:
            return (
                tuple(name for _position, name in sorted(sides[-1])),
                tuple(name for _position, name in sorted(sides[1])),
            )
    return (), ()


def select_corridor_yield_side(
    side_a: Sequence[str],
    side_b: Sequence[str],
    robots: Mapping[str, RobotCoordinationState],
    graph: Mapping[Node, Sequence[tuple[Node, float]]],
    owners: Mapping[Node, str],
    rack_nodes: set[Node] | frozenset[Node] = frozenset(),
) -> tuple[str, ...]:
    candidates = [
        tuple(side) for side in (side_a, side_b)
        if side and can_replan(robots[side[0]], graph, owners, rack_nodes)
    ]
    if not candidates:
        return ()
    return min(
        candidates,
        key=lambda side: (
            len(side) - 1,
            robots[side[0]].loaded,
            robots[side[0]].priority,
            side[0],
        ),
    )


def load_fixture(path: Path) -> dict:
    try:
        fixture = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read coordination fixture: {exc}") from exc
    if fixture.get("schema") != "dram_coordination_fixture/v1":
        raise ValueError("coordination fixture schema must be 'dram_coordination_fixture/v1'")
    if "cases" in fixture:
        if not fixture["cases"] or any(
            not case.get("name") or "expected" not in case for case in fixture["cases"]
        ):
            raise ValueError("coordination fixture cases require name and expected")
    elif not fixture.get("name") or not fixture.get("robots") or not fixture.get("expected"):
        raise ValueError("coordination fixture requires name, robots, and expected")
    return fixture


class DramCoordinator:
    def __init__(
        self,
        config: CoordinationConfig,
        coordinates: Mapping[Node, tuple[float, float]],
        owners: Mapping[Node, str] | None = None,
    ):
        self.config = config
        self.coordinates = coordinates
        self.owners = dict(owners or {})
        self.robots: dict[str, RobotCoordinationState] = {}
        self.pending: set[str] = set()
        self.status = CoordinationStatus.VALID
        self.deadlock_snapshot: dict | None = None
        self.metrics = {
            "allocation_epoch_count": 0,
            "reservation_request_count": 0,
            "reservation_grant_count": 0,
            "reservation_denial_count": 0,
            "granted_window_nodes": 0,
            "max_granted_window_nodes": 0,
            "stale_event_count": 0,
        }

    def register_route(self, robot_id: str, path: Sequence[Node], *, loaded: bool, now: float, path_revision: int = 1, priority: int = 0) -> None:
        if not path:
            raise ValueError("coordination path must not be empty")
        previous = self.robots.get(robot_id)
        reserved = previous.reserved & set(path) if previous else {path[0]}
        if previous:
            for node in previous.reserved - reserved:
                if self.owners.get(node) == robot_id:
                    self.owners.pop(node)
        self.robots[robot_id] = RobotCoordinationState(
            robot_id, tuple(path), loaded, priority, revision=path_revision,
            reserved=reserved, last_progress=now,
        )
        self.owners[path[0]] = robot_id

    def request_reservation(self, robot_id: str, now: float) -> float:
        if robot_id not in self.robots:
            raise KeyError(robot_id)
        self.pending.add(robot_id)
        self.metrics["reservation_request_count"] += 1
        return next_epoch(now, self.config.allocation_period_seconds)

    def run_allocation_epoch(self, now: float) -> tuple[ReservationDecision, ...]:
        self.metrics["allocation_epoch_count"] += 1
        decisions = []
        ordered = sorted(
            self.pending,
            key=lambda name: (
                int(not self.robots[name].loaded),
                len(self.robots[name].path) - self.robots[name].physical_index,
                -self.robots[name].priority,
                name,
            ),
        )
        for name in ordered:
            robot = self.robots[name]
            candidate = straight_window(robot.path, robot.physical_index, self.config.max_reservation_nodes, self.coordinates)
            granted = []
            blocker = None
            robot.wait_for.clear()
            for node in candidate:
                owner = self.owners.get(node)
                if owner is not None and owner != name:
                    blocker = (node, owner)
                    robot.wait_for.add(owner)
                    break
                granted.append(node)
            if granted:
                for node in granted:
                    self.owners[node] = name
                robot.reserved.update(granted)
                robot.authorized_index = max(robot.authorized_index, robot.physical_index + len(granted) - 1)
                self.metrics["reservation_grant_count"] += 1
                self.metrics["granted_window_nodes"] += len(granted)
                self.metrics["max_granted_window_nodes"] = max(self.metrics["max_granted_window_nodes"], len(granted))
            if blocker:
                robot.blocked_since = robot.blocked_since if robot.blocked_since is not None else now
                self.metrics["reservation_denial_count"] += 1
            else:
                robot.blocked_since = None
                self.pending.discard(name)
            decisions.append(ReservationDecision(name, tuple(granted), blocker))
        return tuple(decisions)

    def acknowledge_crossing(self, robot_id: str, previous: Node, reached: Node, *, now: float, path_revision: int) -> bool:
        robot = self.robots[robot_id]
        if path_revision != robot.revision:
            self.metrics["stale_event_count"] += 1
            return False
        try:
            reached_index = robot.path.index(reached, robot.physical_index)
        except ValueError:
            self.metrics["stale_event_count"] += 1
            return False
        robot.physical_index = max(robot.physical_index, reached_index)
        robot.acknowledged_index = max(robot.acknowledged_index, reached_index)
        robot.last_progress = now
        if previous != reached and self.owners.get(previous) == robot_id:
            self.owners.pop(previous)
            robot.reserved.discard(previous)
        return True

    def complete_route(self, robot_id: str, now: float) -> None:
        robot = self.robots.get(robot_id)
        self.pending.discard(robot_id)
        if robot:
            current = robot.path[robot.physical_index]
            for node in robot.reserved - {current}:
                if self.owners.get(node) == robot_id:
                    self.owners.pop(node)
            robot.path = (current,)
            robot.physical_index = robot.acknowledged_index = robot.authorized_index = 0
            robot.reserved = {current}
            robot.wait_for.clear()
            robot.blocked_since = None
            robot.last_progress = now

    def release_nodes(self, robot_id: str, nodes: Sequence[Node]) -> None:
        robot = self.robots[robot_id]
        for node in nodes:
            if self.owners.get(node) == robot_id:
                self.owners.pop(node)
            robot.reserved.discard(node)

    def snapshot(self) -> dict:
        grants = self.metrics["reservation_grant_count"]
        metrics = dict(self.metrics)
        metrics["mean_granted_window_nodes"] = metrics.pop("granted_window_nodes") / grants if grants else 0.0
        return {
            "profile": self.config.profile,
            "status": self.status.value,
            **metrics,
            "owners": {str(node): owner for node, owner in sorted(self.owners.items(), key=lambda item: repr(item[0]))},
            "wait_for": {name: sorted(robot.wait_for) for name, robot in sorted(self.robots.items())},
            "robots": {
                name: {
                    "path": [str(node) for node in robot.path],
                    "physical_index": robot.physical_index,
                    "acknowledged_index": robot.acknowledged_index,
                    "authorized_index": robot.authorized_index,
                    "revision": robot.revision,
                    "loaded": robot.loaded,
                    "priority": robot.priority,
                    "reserved": sorted(map(str, robot.reserved)),
                    "tabu_edges": sorted([str(start), str(end)] for start, end in robot.tabu_edges),
                }
                for name, robot in sorted(self.robots.items())
            },
        }
