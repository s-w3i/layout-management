from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Set, Tuple

from .astar_planner import WarehouseMap
from .conflict_detection import CORRIDOR_SIDE_OFFSET
from .sim_types import RobotSnapshot


PATH_OVERLAP_REPLAN_SEC = 6.0


class ConflictResolver:
    def __init__(self, warehouse_map: WarehouseMap) -> None:
        self.map = warehouse_map
        self.waiting_conflicts: Dict[str, Dict[str, object]] = {}

    def resolve(
        self,
        *,
        conflicts: List[Dict[str, object]],
        priority_order: Sequence[str],
        robot_snapshots: Dict[str, RobotSnapshot],
        jack_states: Dict[str, bool],
        sim_time_sec: float,
    ) -> Dict[str, str]:
        actions: Dict[str, str] = {}
        current_grids = {name: snapshot.current_vertex for name, snapshot in robot_snapshots.items()}

        for conflict in conflicts:
            conflict_type = str(conflict["type"])
            entries = list(conflict["robots"])  # type: ignore[arg-type]
            robots = [str(entry["robot"]) for entry in entries]

            if conflict_type == "corridor_deadlock":
                side_a, side_b = self._extract_corridor_sides(entries)
                yielding_side = self._select_yielding_side(side_a, side_b, priority_order, current_grids, jack_states)
                holding_side = side_b if yielding_side == side_a else side_a
                for robot in yielding_side:
                    actions[robot] = "replan"
                for robot in holding_side:
                    actions.setdefault(robot, "wait")
                continue

            if conflict_type == "wait_chain":
                replan_robot = next(
                    (
                        robot
                        for robot in reversed(robots)
                        if self._robot_can_replan(robot, current_grids, jack_states)
                    ),
                    None,
                )
                if replan_robot is None:
                    replan_robot = self._select_robot_to_replan(robots, priority_order, current_grids, jack_states)
                if replan_robot is None:
                    for robot in robots:
                        actions.setdefault(robot, "wait")
                else:
                    actions[replan_robot] = "replan"
                    for robot in robots:
                        if robot != replan_robot:
                            actions.setdefault(robot, "wait")
                continue

            if conflict_type == "path_overlap":
                lower = self._lowest_priority(robots, priority_order)
                if lower is None:
                    continue
                node_key = self._node_key_for_robot(entries, lower)
                conflict_key = self._conflict_key(conflict_type, entries)
                resolution = "wait"
                existing = self.waiting_conflicts.get(conflict_key)
                start_time = sim_time_sec
                if existing:
                    start_time = float(existing.get("start_time", sim_time_sec))
                    if sim_time_sec - start_time >= PATH_OVERLAP_REPLAN_SEC:
                        replan_robot = self._select_robot_to_replan(robots, priority_order, current_grids, jack_states)
                        if replan_robot is not None:
                            lower = replan_robot
                            resolution = "replan"
                            node_key = self._node_key_for_robot(entries, lower)
                self.waiting_conflicts[conflict_key] = {
                    "start_time": start_time,
                    "node": node_key,
                    "resolution": resolution,
                    "robot": lower,
                }
                actions[lower] = resolution
                continue

            replan_robot = self._select_robot_to_replan(robots, priority_order, current_grids, jack_states)
            if replan_robot is None:
                for robot in robots:
                    actions.setdefault(robot, "wait")
            else:
                actions[replan_robot] = "replan"
                for robot in robots:
                    if robot != replan_robot:
                        actions.setdefault(robot, "wait")

        active_conflict_keys = {
            self._conflict_key(str(conflict["type"]), list(conflict["robots"]))  # type: ignore[arg-type]
            for conflict in conflicts
            if str(conflict["type"]) == "path_overlap"
        }
        for key in list(self.waiting_conflicts):
            if key not in active_conflict_keys:
                del self.waiting_conflicts[key]
        return actions

    def _node_key_for_robot(self, entries: Sequence[Dict[str, object]], robot_name: str) -> str:
        for entry in entries:
            if str(entry["robot"]) == robot_name:
                return f"{entry['from']}->{entry['to']}"
        return ""

    def _conflict_key(self, conflict_type: str, entries: Sequence[Dict[str, object]]) -> str:
        robots = ",".join(sorted(str(entry["robot"]) for entry in entries))
        targets = ",".join(sorted(str(entry.get("to", "")) for entry in entries))
        return f"{conflict_type}:{robots}:{targets}"

    def _lowest_priority(self, robots: Sequence[str], priority_order: Sequence[str]) -> Optional[str]:
        if not robots:
            return None
        ranked = sorted(
            robots,
            key=lambda name: priority_order.index(name) if name in priority_order else float("inf"),
            reverse=True,
        )
        return ranked[0] if ranked else None

    def _select_robot_to_replan(
        self,
        robots: Sequence[str],
        priority_order: Sequence[str],
        current_grids: Dict[str, str],
        jack_states: Dict[str, bool],
    ) -> Optional[str]:
        ranked = sorted(
            robots,
            key=lambda name: priority_order.index(name) if name in priority_order else float("inf"),
            reverse=True,
        )
        for robot in ranked:
            if self._robot_can_replan(robot, current_grids, jack_states):
                return robot
        return ranked[0] if ranked else None

    def _robot_can_replan(
        self,
        robot_name: str,
        current_grids: Dict[str, str],
        jack_states: Dict[str, bool],
    ) -> bool:
        current = current_grids.get(robot_name)
        if not current:
            return False
        current_idx = self.map.name_to_index.get(current)
        if current_idx is None:
            return False
        occupied = {grid for other, grid in current_grids.items() if other != robot_name}
        for neighbor_idx, _ in self.map.adjacency[current_idx]:
            neighbor = self.map.vertices[neighbor_idx].name
            if neighbor in occupied:
                continue
            if jack_states.get(robot_name, False) and neighbor in self.map.pickup_dispensers:
                continue
            return True
        return False

    def _extract_corridor_sides(
        self,
        entries: Sequence[Dict[str, object]],
    ) -> Tuple[List[str], List[str]]:
        side_a: List[str] = []
        side_b: List[str] = []
        for entry in entries:
            robot_name = str(entry["robot"])
            step = int(entry.get("step", 0))
            if step >= CORRIDOR_SIDE_OFFSET:
                side_b.append(robot_name)
            else:
                side_a.append(robot_name)
        return side_a, side_b

    def _select_yielding_side(
        self,
        side_a: Sequence[str],
        side_b: Sequence[str],
        priority_order: Sequence[str],
        current_grids: Dict[str, str],
        jack_states: Dict[str, bool],
    ) -> List[str]:
        can_a = any(self._robot_can_replan(robot, current_grids, jack_states) for robot in side_a)
        can_b = any(self._robot_can_replan(robot, current_grids, jack_states) for robot in side_b)
        if can_a and not can_b:
            return list(side_a)
        if can_b and not can_a:
            return list(side_b)
        if len(side_a) != len(side_b):
            return list(side_a if len(side_a) < len(side_b) else side_b)
        low_a = self._lowest_priority(side_a, priority_order)
        low_b = self._lowest_priority(side_b, priority_order)
        if low_a is None:
            return list(side_b)
        if low_b is None:
            return list(side_a)
        idx_a = priority_order.index(low_a) if low_a in priority_order else float("inf")
        idx_b = priority_order.index(low_b) if low_b in priority_order else float("inf")
        return list(side_a if idx_a >= idx_b else side_b)
