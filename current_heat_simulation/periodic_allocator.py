from __future__ import annotations

import math
from typing import Callable, Dict, List, Optional, Sequence

from .astar_planner import AStarPlanner, MutexPassage, WarehouseMap
from .priority_scheduling import get_alloc_order
from .sim_types import RobotAllocationState, RobotSnapshot, RobotTaskContext


class PeriodicAllocator:
    def __init__(self, *, warehouse_map: WarehouseMap, allocator_cfg: Dict[str, object], robot_names: Sequence[str]) -> None:
        self.map = warehouse_map
        self.allocator_cfg = {
            "max_reservation": 8,
            "priority_strategy": "ascPathLength",
            "allocation_interval_sec": 0.5,
            "replan_wait_sec": 5.0,
            "blocked_replan_cooldown_sec": 5.0,
            "path_recovery_cooldown_sec": 5.0,
            **allocator_cfg,
        }
        self.states: Dict[str, RobotAllocationState] = {name: RobotAllocationState() for name in robot_names}
        self.global_reservations: Dict[str, str] = {}
        self.tabu_sets: Dict[str, set[str]] = {}
        self.mutex_passage = MutexPassage()
        self.conflict_resolutions: Dict[str, str] = {}
        self.pending_replans: set[str] = set()
        self.recovery_robots: set[str] = set()
        self.sim_time_sec = 0.0
        self._allocation_elapsed = 0.0

    def set_conflict_resolutions(self, resolutions: Dict[str, str]) -> None:
        self.conflict_resolutions = dict(resolutions)

    def set_full_path(self, robot_name: str, path: Sequence[str], last_goal: Optional[str]) -> None:
        state = self.states[robot_name]
        preserved_window = list(self.mutex_passage.robots_move_buffer.get(robot_name, ()))
        merged_path = self._merge_preserved_window_with_new_path(preserved_window, list(path))
        state.full_path = merged_path
        state.current_index = len(preserved_window) if preserved_window else 0
        state.last_goal = last_goal
        state.conflict_start_times.clear()
        state.last_arrival_time = 0.0
        state.last_arrival_node = None
        state.last_blocked_replan_key = None
        state.last_blocked_replan_time = 0.0
        state.last_path_recovery_time = 0.0

        self._release_robot_reservations(robot_name, keep_nodes=set(preserved_window))
        self.mutex_passage.remove_all_planned_paths(robot_name)
        for node_name in preserved_window:
            self.mutex_passage.update_move_buffer(robot_name, node_name)
        if state.full_path:
            self.mutex_passage.add_planned_path(robot_name, state.full_path)

    def clear_robot(self, robot_name: str) -> None:
        self._release_robot_reservations(robot_name, keep_nodes=set())
        self.states[robot_name] = RobotAllocationState()
        self.mutex_passage.remove_all_planned_paths(robot_name)
        self.tabu_sets.pop(robot_name, None)

    def robot_at_goal(self, robot_name: str) -> bool:
        state = self.states[robot_name]
        return not state.full_path or len(state.full_path) <= 1

    def needs_path_recovery(self, robot_name: str, current_vertex: str, goal_vertex: Optional[str]) -> bool:
        if not goal_vertex or current_vertex == goal_vertex:
            return False
        state = self.states[robot_name]
        window_nodes = self._window_nodes(robot_name, current_vertex)
        if len(window_nodes) >= 2:
            return False
        if not state.full_path:
            return True
        if state.current_index < len(state.full_path):
            return False
        return len(state.full_path) == 1 and state.full_path[0] == current_vertex

    def can_attempt_path_recovery(self, robot_name: str) -> bool:
        cooldown = float(self.allocator_cfg.get("path_recovery_cooldown_sec", self.allocator_cfg.get("replan_wait_sec", 5.0)))
        state = self.states[robot_name]
        return state.last_path_recovery_time <= 0.0 or (self.sim_time_sec - state.last_path_recovery_time) >= cooldown

    def mark_path_recovery_attempt(self, robot_name: str) -> None:
        self.states[robot_name].last_path_recovery_time = self.sim_time_sec

    def sync_robot_to_current_vertex(self, robot_name: str, current_vertex: str) -> None:
        state = self.states[robot_name]
        state.full_path = [current_vertex]
        state.current_index = 1
        state.conflict_start_times.clear()
        state.last_arrival_time = self.sim_time_sec
        state.last_arrival_node = current_vertex
        state.last_blocked_replan_key = None
        self._release_robot_reservations(robot_name, keep_nodes={current_vertex})
        self.mutex_passage.remove_all_planned_paths(robot_name)
        self.mutex_passage.update_move_buffer(robot_name, current_vertex)
        self.mutex_passage.add_planned_path(robot_name, [current_vertex])

    def full_paths(self) -> Dict[str, List[str]]:
        return {robot_name: list(state.full_path) for robot_name, state in self.states.items() if state.full_path}

    def window_paths(self, robot_snapshots: Dict[str, RobotSnapshot]) -> Dict[str, List[str]]:
        return {
            robot_name: ([] if robot_name in self.pending_replans else self._window_nodes(robot_name, robot_snapshots[robot_name].current_vertex))
            for robot_name in self.states
            if robot_name in robot_snapshots
        }

    def conflict_paths(self, robot_snapshots: Dict[str, RobotSnapshot]) -> Dict[str, List[tuple[str, float, float]]]:
        paths: Dict[str, List[tuple[str, float, float]]] = {}
        for robot_name, state in self.states.items():
            snapshot = robot_snapshots[robot_name]
            names = list(state.full_path) if state.full_path else [snapshot.current_vertex]
            if names and names[0] != snapshot.current_vertex:
                if snapshot.current_vertex in names:
                    names = names[names.index(snapshot.current_vertex) :]
                else:
                    names.insert(0, snapshot.current_vertex)
            compact: List[str] = []
            for name in names:
                if not compact or compact[-1] != name:
                    compact.append(name)
            paths[robot_name] = [(name, *self.map.point(name)) for name in compact]
        return paths

    def update(
        self,
        *,
        dt: float,
        planner: AStarPlanner,
        robot_snapshots: Dict[str, RobotSnapshot],
        task_contexts: Dict[str, RobotTaskContext],
        replan_callback: Callable[[str], None],
        return_replan_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.sim_time_sec += dt
        self._allocation_elapsed += dt

        for robot_name, snapshot in robot_snapshots.items():
            self._consume_progress(robot_name, snapshot.current_vertex)
            self._refresh_last_arrival_marker(robot_name, snapshot.current_vertex)

        planner.set_tabu_edges(self.tabu_sets)
        planner.set_committed_paths(self.full_paths())

        if self._allocation_elapsed >= float(self.allocator_cfg.get("allocation_interval_sec", 0.5)):
            self._allocation_elapsed = 0.0
            self.allocate_windows(
                planner=planner,
                robot_snapshots=robot_snapshots,
                task_contexts=task_contexts,
                replan_callback=replan_callback,
                return_replan_callback=return_replan_callback,
            )

    def allocate_windows(
        self,
        *,
        planner: AStarPlanner,
        robot_snapshots: Dict[str, RobotSnapshot],
        task_contexts: Dict[str, RobotTaskContext],
        replan_callback: Callable[[str], None],
        return_replan_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._reserve_stationary_positions(robot_snapshots)
        order = get_alloc_order(self.states, robot_snapshots, task_contexts, str(self.allocator_cfg.get("priority_strategy", "ascPathLength")), self.map, self.mutex_passage.robots_move_buffer)
        for robot_name in order:
            self._allocate_robot_window(
                robot_name=robot_name,
                planner=planner,
                robot_snapshots=robot_snapshots,
                task_contexts=task_contexts,
                replan_callback=replan_callback,
                return_replan_callback=return_replan_callback,
            )

    def _consume_progress(self, robot_name: str, current_vertex: str) -> None:
        state = self.states[robot_name]
        move_buffer = self.mutex_passage.robots_move_buffer.get(robot_name)
        while move_buffer and current_vertex in move_buffer and move_buffer[0] != current_vertex:
            front_node = move_buffer[0]
            if self.global_reservations.get(front_node) == robot_name and front_node != current_vertex:
                del self.global_reservations[front_node]
            if state.full_path and state.full_path[0] == front_node:
                state.full_path.pop(0)
                if state.current_index > 0:
                    state.current_index -= 1
            self.mutex_passage.pop_move_buffer(robot_name)
            move_buffer = self.mutex_passage.robots_move_buffer.get(robot_name)

        if state.full_path and state.full_path[0] != current_vertex:
            if current_vertex in state.full_path:
                shift = state.full_path.index(current_vertex)
                state.full_path = state.full_path[shift:]
                state.current_index = max(0, state.current_index - shift)
            else:
                state.full_path = [current_vertex]
                state.current_index = 1

    def _reserve_stationary_positions(self, robot_snapshots: Dict[str, RobotSnapshot]) -> None:
        active_reserved: Dict[str, str] = {}
        for robot_name, snapshot in robot_snapshots.items():
            for node in self._window_nodes(robot_name, snapshot.current_vertex):
                active_reserved[node] = robot_name
        self.global_reservations = active_reserved
        for robot_name, snapshot in robot_snapshots.items():
            if not self._window_nodes(robot_name, snapshot.current_vertex):
                self.global_reservations[snapshot.current_vertex] = robot_name

    def _window_nodes(self, robot_name: str, current_vertex: str) -> List[str]:
        buffer_nodes = list(self.mutex_passage.robots_move_buffer.get(robot_name, ()))
        if not buffer_nodes:
            state = self.states[robot_name]
            if state.full_path and state.full_path[0] == current_vertex:
                return [current_vertex]
            return []

        if current_vertex in buffer_nodes:
            buffer_nodes = buffer_nodes[buffer_nodes.index(current_vertex) :]
        elif buffer_nodes[0] != current_vertex:
            buffer_nodes.insert(0, current_vertex)

        compact: List[str] = []
        for node in buffer_nodes:
            if not compact or compact[-1] != node:
                compact.append(node)
        return compact

    def _get_straight_segment_indices(self, state: RobotAllocationState) -> List[int]:
        path = state.full_path
        start_idx = state.current_index
        if start_idx >= len(path):
            return []
        segment_indices = [start_idx]
        turn_threshold = math.radians(10.0)
        i = start_idx
        found_turn = False
        while i + 2 < len(path):
            x1, y1 = self.map.point(path[i])
            x2, y2 = self.map.point(path[i + 1])
            x3, y3 = self.map.point(path[i + 2])
            h1 = math.atan2(y2 - y1, x2 - x1)
            h2 = math.atan2(y3 - y2, x3 - x2)
            angle_diff = abs(h1 - h2)
            if angle_diff > math.pi:
                angle_diff = 2 * math.pi - angle_diff
            if angle_diff < turn_threshold:
                segment_indices.append(i + 1)
                i += 1
            else:
                found_turn = True
                break
        if not found_turn:
            while i + 1 < len(path):
                segment_indices.append(i + 1)
                i += 1
        return segment_indices

    def _allocate_robot_window(
        self,
        *,
        robot_name: str,
        planner: AStarPlanner,
        robot_snapshots: Dict[str, RobotSnapshot],
        task_contexts: Dict[str, RobotTaskContext],
        replan_callback: Callable[[str], None],
        return_replan_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        state = self.states[robot_name]
        snapshot = robot_snapshots[robot_name]
        if robot_name in self.recovery_robots:
            return
        if robot_name in self.pending_replans:
            return
        resolution = self.conflict_resolutions.get(robot_name, "allocate")
        buffer = self.mutex_passage.robots_move_buffer.get(robot_name, ())
        if resolution == "replan" and len(buffer) <= 1:
            # DRAM's explicit resolution requests a path without adding a tabu edge.
            replan_callback(robot_name)
            return
        # Standard DRAM's "wait" falls through to normal reservation checks.
        if state.current_index >= len(state.full_path):
            return

        window_nodes = self._window_nodes(robot_name, snapshot.current_vertex)
        if window_nodes:
            last_node = window_nodes[-1]
            if snapshot.current_vertex != last_node and len(window_nodes) >= 6:
                return

        segment_indices = self._get_straight_segment_indices(state)
        if not segment_indices:
            return

        if not window_nodes and state.full_path and state.full_path[0] == snapshot.current_vertex:
            self.mutex_passage.update_move_buffer(robot_name, snapshot.current_vertex)
            window_nodes = self._window_nodes(robot_name, snapshot.current_vertex)

        for _ in segment_indices[: int(self.allocator_cfg.get("max_reservation", 8))]:
            if state.current_index >= len(state.full_path):
                break
            node_name = state.full_path[state.current_index]
            if window_nodes and node_name == window_nodes[-1]:
                state.current_index += 1
                continue

            owner = self.global_reservations.get(node_name)
            if owner and owner != robot_name:
                self.mutex_passage.add_wait_for_robot(robot_name, {owner})
                state.conflict_start_times.setdefault(node_name, self.sim_time_sec)
                # DRAM allows followers and loaded robots to wait; explicit
                # conflict-resolution replans above remain available to both.
                leader = self.states.get(owner)
                if leader and self.is_following_path(state.full_path, leader.full_path, node_name):
                    return
                if snapshot.jack_up:
                    return
                conflict_wait = self.sim_time_sec - state.conflict_start_times[node_name]
                arrival_wait = self.sim_time_sec - state.last_arrival_time if state.last_arrival_time else 0.0
                if (
                    conflict_wait > float(self.allocator_cfg.get("replan_wait_sec", 5.0))
                    and arrival_wait > float(self.allocator_cfg.get("replan_wait_sec", 5.0))
                ):
                    self._trigger_blocked_replan(
                        robot_name=robot_name,
                        state=state,
                        snapshot=snapshot,
                        blocked_node=node_name,
                        planner=planner,
                        task_context=task_contexts[robot_name],
                        replan_callback=replan_callback,
                        return_replan_callback=return_replan_callback,
                    )
                return

            state.conflict_start_times.pop(node_name, None)
            state.last_blocked_replan_key = None
            if task_contexts[robot_name].carrying_rack:
                mutual_conflicts = self.mutex_passage.get_mutual_conflicted(robot_name)
                upcoming_robot = None
                if mutual_conflicts:
                    for next_path in state.full_path[state.current_index + 1 :]:
                        if next_path in self.global_reservations:
                            upcoming_robot = self.global_reservations[next_path]
                            break
                if not (mutual_conflicts and upcoming_robot in mutual_conflicts):
                    cyclic_conflicts = self.mutex_passage.get_cyclic_conflicted(robot_name)
                    conflicted_robots, not_conflicted = self.mutex_passage.get_passage_availability(
                        robot_name, state.full_path[state.current_index :]
                    )
                    not_conflicted.update(mutual_conflicts)
                    if cyclic_conflicts:
                        not_conflicted.update(cyclic_conflicts)
                    if snapshot.current_vertex != node_name and conflicted_robots.difference(not_conflicted):
                        self.mutex_passage.add_wait_for_robot(robot_name, conflicted_robots.difference(not_conflicted))
                        return

            self.global_reservations[node_name] = robot_name
            self.mutex_passage.clear_wait_for_robot(robot_name)
            self.mutex_passage.update_move_buffer(robot_name, node_name)
            state.current_index += 1
            if node_name != snapshot.current_vertex:
                state.last_arrival_time = 0.0
                state.last_arrival_node = None
            window_nodes = self._window_nodes(robot_name, snapshot.current_vertex)

    @staticmethod
    def is_following_path(current_path: Sequence[str], leader_path: Sequence[str], conflict_node: str) -> bool:
        """Match DRAM's two-node continuation test at the reserved node."""
        try:
            current_index = current_path.index(conflict_node)
            leader_index = leader_path.index(conflict_node)
        except ValueError:
            return False
        return current_path[current_index:current_index + 2] == leader_path[leader_index:leader_index + 2]

    def _merge_preserved_window_with_new_path(self, preserved_window: List[str], node_list: List[str]) -> List[str]:
        if not preserved_window:
            return list(node_list)
        if not node_list:
            return list(preserved_window)
        tail_name = preserved_window[-1]
        try:
            tail_idx = node_list.index(tail_name)
        except ValueError:
            return list(preserved_window)
        merged = list(preserved_window)
        for node_name in node_list[tail_idx + 1 :]:
            if merged and merged[-1] == node_name:
                continue
            merged.append(node_name)
        return merged

    def _release_robot_reservations(self, robot_name: str, keep_nodes: set[str]) -> None:
        for node_name in list(self.global_reservations):
            if self.global_reservations.get(node_name) == robot_name and node_name not in keep_nodes:
                del self.global_reservations[node_name]

    def _publish_tabu_item(self, robot_name: str, from_node: str, to_node: str) -> None:
        if not from_node or not to_node:
            return
        self.tabu_sets.setdefault(robot_name, set()).add(f"{from_node}->{to_node}")

    def _trigger_blocked_replan(
        self,
        *,
        robot_name: str,
        state: RobotAllocationState,
        snapshot: RobotSnapshot,
        blocked_node: str,
        planner: AStarPlanner,
        task_context: RobotTaskContext,
        replan_callback: Callable[[str], None],
        return_replan_callback: Optional[Callable[[str], None]],
    ) -> None:
        window = self._window_nodes(robot_name, snapshot.current_vertex)
        from_node = window[-1] if window else snapshot.current_vertex
        block_key = f"{from_node}->{blocked_node}"
        state.last_blocked_replan_key = block_key
        state.last_blocked_replan_time = self.sim_time_sec
        self._publish_tabu_item(robot_name, from_node, blocked_node)
        planner.set_tabu_edges(self.tabu_sets)

        if return_replan_callback is not None and task_context.phase == "to_return" and task_context.carrying_rack:
            return_replan_callback(robot_name)
            return

        replan_callback(robot_name)

    def _refresh_last_arrival_marker(self, robot_name: str, current_vertex: str) -> None:
        state = self.states[robot_name]
        move_buffer = self.mutex_passage.robots_move_buffer.get(robot_name)
        if not move_buffer:
            state.last_arrival_time = 0.0
            state.last_arrival_node = None
            return

        tail_node = move_buffer[-1]
        if current_vertex == tail_node:
            if state.last_arrival_node != tail_node:
                state.last_arrival_time = self.sim_time_sec
                state.last_arrival_node = tail_node
            return

        state.last_arrival_time = 0.0
        state.last_arrival_node = None
