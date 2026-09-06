from __future__ import annotations

from collections import Counter, deque
import heapq
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Deque, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Set, Tuple



VertexId = int
Point = Tuple[float, float]
PlannerState = Tuple[VertexId, Optional[VertexId]]
CORRIDOR_SIDE_OFFSET = 1000


@dataclass(frozen=True)
class Vertex:
    index: VertexId
    name: str
    x: float
    y: float
    meta: dict


@dataclass
class PlannerConfig:
    weighted_astar_epsilon: float = 1.3
    turning_penalty: float = 1.0
    use_turn_penalty: bool = True
    use_committed_path_awareness: bool = True
    committed_node_penalty: float = 2.0
    committed_edge_penalty: float = 1.0
    use_directional_heat: bool = True
    heat_weight: float = 1.0
    directional_alignment_gain: float = 0.8
    directional_conflict_gain: float = 0.6
    directional_opposite_penalty_gain: float = 0.8
    max_directional_heat_cost: float = 10.0


@dataclass
class PlanResult:
    path: List[str]
    total_cost: float


def get_path_overlap_conflicts(
    robots: Dict[str, List[Tuple[str, float, float]]],
    overlap_window: int,
) -> List[Tuple[str, str, str, int]]:
    conflicts: List[Tuple[str, str, str, int]] = []
    names = list(robots.keys())
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            r1, r2 = names[i], names[j]
            p1 = robots[r1][:overlap_window]
            p2 = robots[r2][:overlap_window]
            for t in range(min(len(p1), len(p2))):
                if p1[t][0] == p2[t][0]:
                    conflicts.append((r1, r2, p1[t][0], t))
    return conflicts


def detect_deadlock_wait_for(
    robots: Dict[str, List[Tuple[str, float, float]]],
) -> Tuple[Dict[str, Tuple[str, int]], bool]:
    wait_for: Dict[str, Set[str]] = {r: set() for r in robots}
    extended = False

    for r, path_r in robots.items():
        if len(path_r) < 2:
            continue
        next_node_r = path_r[1][0]
        curr_nodes_lookup: Dict[str, str] = {}

        for s, path_s in robots.items():
            if r == s or not path_s:
                continue
            curr_node_s = path_s[0][0]
            curr_nodes_lookup[curr_node_s] = s

            if next_node_r == curr_node_s:
                wait_for[r].add(s)

        if not wait_for[r] and len(path_r) > 2:
            _, x0, y0 = path_r[0]
            _, x1, y1 = path_r[1]
            _, x2, y2 = path_r[2]
            v2 = (x2 - x1, y2 - y1)
            v1 = (x1 - x0, y1 - y0)
            cross = v1[0] * v2[1] - v1[1] * v2[0]
            if abs(cross) > 0.1:
                next_node_r = path_r[2][0]
                if next_node_r in curr_nodes_lookup:
                    extended = True
                    wait_for[r].add(curr_nodes_lookup[next_node_r])

    visited: Set[str] = set()
    on_stack: Set[str] = set()
    stack: List[str] = []
    cycle_nodes: Set[str] = set()
    cycle_found = False

    def dfs(robot_name: str) -> bool:
        nonlocal cycle_found
        visited.add(robot_name)
        stack.append(robot_name)
        on_stack.add(robot_name)
        for neighbor in sorted(wait_for[robot_name]):
            if neighbor not in visited:
                if dfs(neighbor):
                    return True
            elif neighbor in on_stack:
                idx = stack.index(neighbor)
                cycle_nodes.update(stack[idx:])
                cycle_found = True
                return True
        stack.pop()
        on_stack.remove(robot_name)
        return False

    for robot_name in wait_for:
        if robot_name not in visited and dfs(robot_name):
            break

    if not cycle_found:
        return {}, extended
    if len(cycle_nodes) < 3:
        return {}, extended

    conflict_info: Dict[str, Tuple[str, int]] = {}
    for robot_name in sorted(cycle_nodes):
        if len(robots[robot_name]) >= 2:
            node = robots[robot_name][1][0]
            step = 1
        else:
            node = robots[robot_name][0][0]
            step = 0
        conflict_info[robot_name] = (node, step)
    return conflict_info, extended


class MutexPassage:
    def __init__(self) -> None:
        self.grids: Dict[str, Set[str]] = {}
        self.path_index: Dict[str, Dict[str, int]] = {}
        self.conflict_pairs: Set[Tuple[str, str]] = set()
        self.robots_move_buffer: Dict[str, Deque[str]] = {}
        self.robots_planned_path: Dict[str, List[str]] = {}
        self.wait_for_dependency: Dict[str, Set[str]] = {}

    def add_wait_for_robot(self, robot_name: str, wait_for_robot: Set[str]) -> None:
        for wait_robot in wait_for_robot:
            self.wait_for_dependency.setdefault(wait_robot, set()).add(robot_name)

    def clear_wait_for_robot(self, robot_name: str) -> None:
        for wait_for_set in self.wait_for_dependency.values():
            wait_for_set.discard(robot_name)

    def add_planned_path(self, robot_name: str, paths: Sequence[str]) -> None:
        for idx, path_name in enumerate(paths):
            self.grids.setdefault(path_name, set()).add(robot_name)
            self.path_index.setdefault(robot_name, {})[path_name] = idx

            for moving_robot, move_buffer in self.robots_move_buffer.items():
                if moving_robot == robot_name:
                    continue
                if path_name in move_buffer:
                    self.conflict_pairs.add((moving_robot, robot_name))

        self.robots_planned_path[robot_name] = list(paths)

    def update_move_buffer(self, robot_name: str, path_name: str) -> None:
        self.grids.setdefault(path_name, set()).add(robot_name)

        others = [name for name in self.grids.get(path_name, set()) if name != robot_name]
        if others:
            planned_path = self.robots_planned_path.get(robot_name, [])
            is_start_grid = bool(planned_path) and path_name in planned_path and planned_path.index(path_name) == 0
            for other in others:
                other_planned = self.robots_planned_path.get(other, [])
                if not other_planned or (is_start_grid and path_name == other_planned[-1]):
                    continue
                self.conflict_pairs.add((robot_name, other))

        move_buffer = self.robots_move_buffer.setdefault(robot_name, deque())
        if path_name not in move_buffer:
            move_buffer.append(path_name)

    def pop_move_buffer(self, robot_name: str) -> None:
        move_buffer = self.robots_move_buffer.get(robot_name)
        if not move_buffer:
            return

        previous_path = move_buffer.popleft()
        if previous_path in self.grids:
            self.grids[previous_path].discard(robot_name)
            if not self.grids[previous_path]:
                del self.grids[previous_path]

        if move_buffer:
            subsequent_path_robots: Set[str] = set()
            for path_name in move_buffer:
                subsequent_path_robots.update(self.grids.get(path_name, set()))

            if previous_path in self.grids:
                to_release = self.grids[previous_path].difference(subsequent_path_robots)
                for other_robot in to_release:
                    self.conflict_pairs.discard((robot_name, other_robot))
        elif previous_path in self.grids:
            for other_robot in self.grids[previous_path]:
                if other_robot != robot_name:
                    self.conflict_pairs.discard((robot_name, other_robot))

    def get_passage_availability(self, robot_name: str, subsequent_path: Sequence[str]) -> Tuple[Set[str], Set[str]]:
        if not subsequent_path:
            return set(), set()

        next_node = subsequent_path[0]
        existing_blocked = self._get_conflicted_robots(robot_name, subsequent_path)
        next_node_conflicts = self.grids.get(next_node, set())
        conflicted_robots = set(existing_blocked) & set(next_node_conflicts)
        not_conflicted: Set[str] = set()

        if conflicted_robots and len(subsequent_path) > 1:
            for conflicted_robot in sorted(conflicted_robots):
                for idx, node_name in enumerate(subsequent_path[1:]):
                    robots_on_node = self.grids.get(node_name, set())
                    if conflicted_robot not in robots_on_node:
                        if idx == 0:
                            not_conflicted.add(conflicted_robot)
                        break
                    conflict_index = self.path_index.get(conflicted_robot, {}).get(next_node, -1)
                    if conflicted_robot in robots_on_node and self.path_index.get(conflicted_robot, {}).get(node_name, -1) >= conflict_index:
                        not_conflicted.add(conflicted_robot)
                        break
                    if node_name in self.robots_move_buffer.get(conflicted_robot, ()):
                        break

        return conflicted_robots, not_conflicted

    def get_mutual_conflicted(self, robot_name: str) -> Set[str]:
        return {blocked for blocker, blocked in self.conflict_pairs if blocker == robot_name and (blocked, blocker) in self.conflict_pairs}

    def get_cyclic_conflicted(self, robot_name: str) -> Set[str]:
        return self._has_cycle_wait_for(robot_name)

    def remove_all_planned_paths(self, robot_name: str) -> None:
        if robot_name in self.robots_move_buffer:
            del self.robots_move_buffer[robot_name]

        self.robots_planned_path[robot_name] = []

        grids_to_clean = []
        for grid_pos, robots in self.grids.items():
            if robot_name in robots:
                robots.discard(robot_name)
                if not robots:
                    grids_to_clean.append(grid_pos)

        for grid_pos in grids_to_clean:
            del self.grids[grid_pos]

        conflicts_to_remove = []
        for blocker, blocked in sorted(self.conflict_pairs):
            if blocker == robot_name or blocked == robot_name:
                conflicts_to_remove.append((blocker, blocked))
        for pair in conflicts_to_remove:
            self.conflict_pairs.discard(pair)

        if robot_name in self.path_index:
            del self.path_index[robot_name]
        self.clear_wait_for_robot(robot_name)

    def _get_conflicted_robots(self, robot_name: str, subsequent_path: Sequence[str]) -> Set[str]:
        blockers_map: Dict[str, List[str]] = {}
        self_blocking: Set[str] = set()

        if robot_name in self.robots_move_buffer and self.robots_move_buffer[robot_name]:
            current_grid_robots = self.grids.get(self.robots_move_buffer[robot_name][-1], set())
        else:
            current_grid_robots = set()

        for blocker, blocked in sorted(self.conflict_pairs):
            blockers_map.setdefault(blocked, []).append(blocker)
            if blocker == robot_name and blocked in current_grid_robots:
                self_blocking.add(blocked)

        visited: Set[str] = set()
        stack: List[Tuple[str, int]] = [(robot_name, 0)]

        while stack:
            current, depth = stack.pop()
            for blocker in blockers_map.get(current, []):
                if depth == 0:
                    conflict_index: Optional[int] = None
                    conflict_start = False
                    for path_name in subsequent_path:
                        robots_on_node = self.grids.get(path_name, set())
                        baseline_index = conflict_index if conflict_index is not None else -1
                        if conflict_start and self.path_index.get(blocker, {}).get(path_name, -1) >= baseline_index:
                            break
                        if conflict_start and path_name in self.robots_move_buffer.get(blocker, ()):
                            if blocker not in visited:
                                visited.add(blocker)
                                stack.append((blocker, depth + 1))
                            break
                        if blocker in robots_on_node and not conflict_start:
                            conflict_start = True
                            conflict_index = self.path_index.get(blocker, {}).get(path_name, -1)
                else:
                    if blocker != robot_name and blocker not in visited and blocker not in self_blocking:
                        visited.add(blocker)
                        stack.append((blocker, depth + 1))

        return visited

    def _has_cycle_wait_for(self, start_robot: str) -> Set[str]:
        visited: Set[str] = set()
        stack: List[str] = []

        def dfs(robot_name: str) -> Set[str]:
            if robot_name in stack:
                cycle_start = stack.index(robot_name)
                return set(stack[cycle_start:] + [robot_name])

            visited.add(robot_name)
            stack.append(robot_name)

            for next_robot in sorted(self.wait_for_dependency.get(robot_name, set())):
                if next_robot in visited and next_robot not in stack:
                    continue
                result = dfs(next_robot)
                if result:
                    return result

            stack.pop()
            return set()

        cycle = dfs(start_robot)
        if cycle:
            cycle.discard(start_robot)
        return cycle


class WarehouseMap:
    def __init__(self, map_file: str | Path, level_name: str = "L1") -> None:
        from warehouse_layout.rmf import RmfMapService
        from amr_simulation.models import grid_name
        from amr_simulation.routing import GridRouter

        self.project = RmfMapService().load_project(Path(map_file))
        self.router = GridRouter(self.project)
        self.vertices: List[Vertex] = []
        self.name_to_index: Dict[str, VertexId] = {}
        self.pickup_dispensers: Set[str] = set()
        self.dropoff_ingestors: Set[str] = set()
        self.spawn_vertices: Dict[str, str] = {}
        self.workstations = {
            station: grid_name(position)
            for station, position in self.router.workstations.items()
        }
        for position in sorted(self.router.positions):
            name = grid_name(position)
            index = len(self.vertices)
            x, y = self.router.coordinates[position]
            meta = {"name": name}
            if position in self.router.rack_positions:
                self.pickup_dispensers.add(name)
                meta["pickup_dispenser"] = name
            if name in self.workstations.values():
                self.dropoff_ingestors.add(name)
                meta["dropoff_ingestor"] = name
            self.vertices.append(Vertex(index, name, x, y, meta))
            self.name_to_index[name] = index
        self.adjacency = {
            self.name_to_index[grid_name(position)]: [
                (self.name_to_index[grid_name(end)], cost) for end, cost in edges
            ]
            for position, edges in self.router.graph.items()
        }

    def adjacency_items(self) -> Iterator[Tuple[VertexId, VertexId, float]]:
        for start, neighbors in self.adjacency.items():
            for end, cost in neighbors:
                yield start, end, cost

    def get_vertex(self, name: str) -> Vertex:
        return self.vertices[self.name_to_index[name]]

    def point(self, name: str) -> Point:
        vertex = self.get_vertex(name)
        return (vertex.x, vertex.y)

    def distance(self, a: str, b: str) -> float:
        return self.distance_by_index(self.name_to_index[a], self.name_to_index[b])

    def distance_by_index(self, a: VertexId, b: VertexId) -> float:
        va = self.vertices[a]
        vb = self.vertices[b]
        return math.hypot(vb.x - va.x, vb.y - va.y)

    def heuristic(self, a: VertexId, b: VertexId) -> float:
        return self.distance_by_index(a, b)


class AStarPlanner:
    def __init__(self, warehouse_map: WarehouseMap, config: Optional[PlannerConfig] = None) -> None:
        self.map = warehouse_map
        self.config = config or PlannerConfig()
        self.directional_heat_costs: Dict[Tuple[VertexId, VertexId], float] = {}
        self.committed_paths: Dict[str, List[str]] = {}
        self.committed_path_indices: Dict[str, List[VertexId]] = {}
        self._committed_cell_counts: Counter[VertexId] = Counter()
        self._committed_edge_counts: Counter[Tuple[VertexId, VertexId]] = Counter()
        self._directional_flow_counts: Counter[Tuple[VertexId, VertexId]] = Counter()
        self.tabu_edges: Dict[str, Set[str]] = {}

    def set_committed_paths(self, committed_paths: Mapping[str, Sequence[str]]) -> None:
        self.committed_paths = {name: list(path) for name, path in committed_paths.items() if path}
        self.committed_path_indices = {}
        self._committed_cell_counts = Counter()
        self._committed_edge_counts = Counter()
        self._directional_flow_counts = Counter()

        for robot_name, path_names in self.committed_paths.items():
            path_indices = [self.map.name_to_index[name] for name in path_names if name in self.map.name_to_index]
            if not path_indices:
                continue
            self.committed_path_indices[robot_name] = path_indices
            self._committed_cell_counts.update(path_indices)
            edge_pairs = list(zip(path_indices, path_indices[1:]))
            self._committed_edge_counts.update(edge_pairs)
            self._directional_flow_counts.update(edge_pairs)

    def set_tabu_edges(self, tabu_edges: Mapping[str, Iterable[str]]) -> None:
        self.tabu_edges = {robot_name: set(edges) for robot_name, edges in tabu_edges.items() if edges}

    def set_directional_heat_costs(
        self,
        directional_heat_costs: Mapping[Tuple[str, str], float] | Mapping[Tuple[VertexId, VertexId], float],
    ) -> None:
        converted: Dict[Tuple[VertexId, VertexId], float] = {}
        for key, value in directional_heat_costs.items():
            start, end = key
            if isinstance(start, str):
                start_idx = self.map.name_to_index[start]
                end_idx = self.map.name_to_index[end]  # type: ignore[index]
            else:
                start_idx = start
                end_idx = end  # type: ignore[assignment]
            converted[(start_idx, end_idx)] = float(value)
        self.directional_heat_costs = converted

    def plan(
        self,
        start_name: str,
        goal_name: str,
        carrying_rack: bool,
        occupied_shelves: Optional[Iterable[str]] = None,
        robot_name: Optional[str] = None,
    ) -> List[str]:
        return self.plan_detailed(
            start_name=start_name,
            goal_name=goal_name,
            carrying_rack=carrying_rack,
            occupied_shelves=occupied_shelves,
            robot_name=robot_name,
        ).path

    def plan_detailed(
        self,
        start_name: str,
        goal_name: str,
        carrying_rack: bool,
        occupied_shelves: Optional[Iterable[str]] = None,
        robot_name: Optional[str] = None,
    ) -> PlanResult:
        # Match amr_simulation: all rack markers are obstacles except endpoints,
        # for both empty and loaded robots.
        blocked_names = set(self.map.pickup_dispensers) | set(occupied_shelves or ())
        blocked_names.discard(start_name)
        blocked_names.discard(goal_name)
        robot_tabu_edges = set(self.tabu_edges.get(robot_name, set())) if robot_name else set()

        start = self.map.name_to_index[start_name]
        goal = self.map.name_to_index[goal_name]
        blocked_indices = {
            self.map.name_to_index[name]
            for name in blocked_names
            if name in self.map.name_to_index
        }

        committed_cells, committed_edges = self._committed_summary(exclude_robot=robot_name)
        start_state: PlannerState = (start, None)
        frontier: List[Tuple[float, PlannerState]] = [(0.0, start_state)]
        came_from: Dict[PlannerState, Optional[PlannerState]] = {start_state: None}
        g_score: Dict[PlannerState, float] = {start_state: 0.0}

        while frontier:
            _, current_state = heapq.heappop(frontier)
            current, previous = current_state
            if current == goal:
                path_indices = self._reconstruct(came_from, current_state)
                return PlanResult(
                    path=[self.map.vertices[index].name for index in path_indices],
                    total_cost=g_score[current_state],
                )

            for neighbor, base_edge_cost in self.map.adjacency[current]:
                if neighbor in blocked_indices:
                    continue
                edge_name = f"{self.map.vertices[current].name}->{self.map.vertices[neighbor].name}"
                if edge_name in robot_tabu_edges:
                    continue

                next_state = (neighbor, current)
                step_cost = self._edge_cost(current, neighbor, base_edge_cost)
                if math.isinf(step_cost):
                    continue
                step_cost += self._turn_cost(previous, current, neighbor)
                step_cost += self._committed_penalty(current, neighbor, committed_cells, committed_edges)

                tentative_g = g_score[current_state] + step_cost
                if tentative_g >= g_score.get(next_state, math.inf):
                    continue

                came_from[next_state] = current_state
                g_score[next_state] = tentative_g
                f_score = tentative_g + self.config.weighted_astar_epsilon * self.map.heuristic(neighbor, goal)
                heapq.heappush(frontier, (f_score, next_state))

        raise ValueError(f"No path found from {start_name} to {goal_name}")

    def nearest_reachable_goal(
        self,
        start_name: str,
        candidate_goals: Sequence[str],
        carrying_rack: bool,
        occupied_shelves: Optional[Iterable[str]] = None,
        robot_name: Optional[str] = None,
    ) -> Tuple[str, List[str]]:
        best_goal: Optional[str] = None
        best_path: Optional[List[str]] = None
        best_cost = math.inf

        for goal_name in candidate_goals:
            try:
                result = self.plan_detailed(
                    start_name=start_name,
                    goal_name=goal_name,
                    carrying_rack=carrying_rack,
                    occupied_shelves=occupied_shelves,
                    robot_name=robot_name,
                )
            except ValueError:
                continue

            if result.total_cost < best_cost:
                best_cost = result.total_cost
                best_goal = goal_name
                best_path = result.path

        if best_goal is None or best_path is None:
            raise ValueError(f"No reachable goal found from {start_name}")

        return best_goal, best_path

    def path_length(self, path: Sequence[str]) -> float:
        total = 0.0
        for a, b in zip(path, path[1:]):
            total += self.map.distance(a, b)
        return total

    def _edge_cost(self, start: VertexId, end: VertexId, base_edge_cost: float) -> float:
        if not self.config.use_directional_heat:
            return base_edge_cost
        heat = self._directional_heat_cost(start, end)
        return base_edge_cost + self.config.heat_weight * heat

    def _turn_cost(self, previous: Optional[VertexId], current: VertexId, neighbor: VertexId) -> float:
        if not self.config.use_turn_penalty or previous is None:
            return 0.0
        v1 = self._unit_vector(previous, current)
        v2 = self._unit_vector(current, neighbor)
        if v1 == (0.0, 0.0) or v2 == (0.0, 0.0):
            return 0.0
        dot = max(-1.0, min(1.0, v1[0] * v2[0] + v1[1] * v2[1]))
        return math.acos(dot) * self.config.turning_penalty

    def _unit_vector(self, start: VertexId, end: VertexId) -> Point:
        sx, sy = self.map.vertices[start].x, self.map.vertices[start].y
        ex, ey = self.map.vertices[end].x, self.map.vertices[end].y
        dx = ex - sx
        dy = ey - sy
        mag = math.hypot(dx, dy)
        if mag == 0.0:
            return (0.0, 0.0)
        return (dx / mag, dy / mag)

    def _committed_summary(self, exclude_robot: Optional[str]) -> Tuple[Set[VertexId], Set[Tuple[VertexId, VertexId]]]:
        if not self.config.use_committed_path_awareness:
            return set(), set()

        committed_cells = set(self._committed_cell_counts.keys())
        committed_edges = set(self._committed_edge_counts.keys())
        if exclude_robot is None:
            return committed_cells, committed_edges

        excluded_indices = self.committed_path_indices.get(exclude_robot, [])
        for vertex in set(excluded_indices):
            if self._committed_cell_counts.get(vertex, 0) <= 1:
                committed_cells.discard(vertex)
        for edge in set(zip(excluded_indices, excluded_indices[1:])):
            if self._committed_edge_counts.get(edge, 0) <= 1:
                committed_edges.discard(edge)
        return committed_cells, committed_edges

    def _directional_heat_cost(self, start: VertexId, end: VertexId) -> float:
        static_forward = self.directional_heat_costs.get((start, end), 0.0)
        static_reverse = self.directional_heat_costs.get((end, start), 0.0)
        forward_flow = float(self._directional_flow_counts.get((start, end), 0))
        reverse_flow = float(self._directional_flow_counts.get((end, start), 0))

        total = forward_flow + reverse_flow
        if total <= 0.0:
            return static_forward

        bias = (forward_flow - reverse_flow) / total
        conflict = min(forward_flow, reverse_flow) / total
        forward_cost = static_forward - self.config.directional_alignment_gain * bias
        reverse_cost = static_reverse + self.config.directional_alignment_gain * bias
        penalty = self.config.directional_conflict_gain * conflict
        forward_cost += penalty
        reverse_cost += penalty

        if self.config.directional_opposite_penalty_gain > 0.0 and abs(bias) > 1e-6:
            minority_penalty = self.config.directional_opposite_penalty_gain * abs(bias)
            if bias > 0.0:
                reverse_cost += minority_penalty
            else:
                forward_cost += minority_penalty

        max_cost = max(0.5, self.config.max_directional_heat_cost)
        return max(0.0, min(max_cost, forward_cost))

    def _committed_penalty(
        self,
        current: VertexId,
        neighbor: VertexId,
        committed_cells: Set[VertexId],
        committed_edges: Set[Tuple[VertexId, VertexId]],
    ) -> float:
        if not self.config.use_committed_path_awareness:
            return 0.0
        penalty = 0.0
        if neighbor in committed_cells:
            penalty += self.config.committed_node_penalty
        if (current, neighbor) in committed_edges:
            penalty += self.config.committed_edge_penalty
        return penalty

    def _reconstruct(
        self,
        came_from: Dict[PlannerState, Optional[PlannerState]],
        current_state: PlannerState,
    ) -> List[VertexId]:
        path_indices = [current_state[0]]
        while came_from[current_state] is not None:
            current_state = came_from[current_state]  # type: ignore[assignment]
            path_indices.append(current_state[0])
        path_indices.reverse()
        return path_indices
