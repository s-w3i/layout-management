"""DRAM directional overlay with simulated-time publication and no ROS dependency.

Numerical/update methods ported from dram_viz/directional_cost_layer.py.
"""
from __future__ import annotations
from collections import Counter
import math
from typing import Dict, List, Sequence, Set, Tuple
VertexName = str
EdgeKey = Tuple[str, str]

class DirectionalCostLayer:
    def __init__(self, warehouse_map, config):
        self.graph = warehouse_map
        self.vertex_index = warehouse_map.name_to_index
        self.alignment_gain = config.directional_alignment_gain
        self.conflict_gain = config.directional_conflict_gain
        self.opposite_penalty_gain = config.directional_opposite_penalty_gain
        self.max_directional_cost = config.max_directional_heat_cost
        self.decay_alpha = config.directional_smoothing_alpha
        self.publish_period = config.directional_publish_period_sec
        self.elapsed = 0.0
        self.base_edge_costs = {}
        self.committed_paths = {}
        self._directional_cache = {}
        self._flow_histogram = Counter()
        self._robot_ordered_edges = {}
        self._pending_robot_paths = {}
        self._needs_histogram_reset = False
        self._needs_full_recompute = False
        self._canonical_orientation = {}
        for a, b, _ in warehouse_map.adjacency_items():
            names = (warehouse_map.vertices[a].name, warehouse_map.vertices[b].name)
            self._canonical_orientation.setdefault(tuple(sorted(names)), names)

    def set_base_costs(self, entries):
        costs = {}
        for edge, value in entries.items():
            start, end = edge.split('->')
            if start not in self.vertex_index or end not in self.vertex_index:
                raise ValueError(f'Unknown heat edge {edge}')
            value = float(value)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f'Invalid heat cost for {edge}')
            costs[tuple(sorted((start, end)))] = value
        self.base_edge_costs = costs
        self._needs_full_recompute = True

    def path_changed(self, robot, path):
        if path:
            self.committed_paths[robot] = list(path)
        else:
            self.committed_paths.pop(robot, None)
        self._pending_robot_paths[robot] = list(path)

    def update(self, dt, planner):
        planner.base_edge_heat_costs = {
            tuple(sorted((self.vertex_index[a], self.vertex_index[b]))): cost
            for (a, b), cost in self.base_edge_costs.items()
        }
        self.elapsed += dt
        if self.publish_period > 0 and self.elapsed + 1e-9 < self.publish_period:
            return
        self.elapsed = 0.0
        if self._process_pending_updates():
            costs = {}
            for (a, b), (forward, reverse) in self._directional_cache.items():
                costs[a, b] = forward
                costs[b, a] = reverse
            planner.set_directional_heat_costs(costs)

    def _mark_dirty(self):
        pass  # Publication is driven by update() at simulation tick boundaries.

    def _update_robot_edges(
        self, robot_name: str, seq: Sequence[VertexName], publish: bool = True
    ) -> bool:
        if self.graph is None:
            return False
        old_edges = self._robot_ordered_edges.pop(robot_name, [])
        affected: Set[EdgeKey] = set()
        for start, end in old_edges:
            key = tuple(sorted((start, end)))
            count = self._flow_histogram.get((start, end), 0)
            if count > 0:
                self._flow_histogram[(start, end)] = count - 1
                if self._flow_histogram[(start, end)] <= 0:
                    del self._flow_histogram[(start, end)]
            affected.add(key)

        ordered_edges: List[Tuple[VertexName, VertexName]] = []
        if seq and len(seq) >= 2:
            valid_vertices = set(self.vertex_index.keys())
            for start, end in zip(seq[:-1], seq[1:]):
                if not start or not end:
                    continue
                if start not in valid_vertices or end not in valid_vertices:
                    continue
                ordered_edges.append((start, end))
                self._flow_histogram[(start, end)] += 1
                affected.add(tuple(sorted((start, end))))
        if ordered_edges:
            self._robot_ordered_edges[robot_name] = ordered_edges

        if affected:
            self._refresh_directional_costs(affected)
            if publish:
                self._mark_dirty()
            return True
        return False

    def _process_pending_updates(self) -> bool:
        changed = False

        if self._needs_histogram_reset:
            self._needs_histogram_reset = False
            self._reset_histogram()
            for robot, seq in self.committed_paths.items():
                self._pending_robot_paths[robot] = seq
            changed = True

        if self._pending_robot_paths:
            updates = list(self._pending_robot_paths.items())
            self._pending_robot_paths.clear()
            for robot, seq in updates:
                changed = self._update_robot_edges(robot, seq, publish=False) or changed

        if self._needs_full_recompute:
            self._needs_full_recompute = False
            self._recompute_all_directional_costs()
            changed = True

        return changed

    def _refresh_directional_costs(self, affected_edges: Set[EdgeKey]) -> None:
        for undirected in affected_edges:
            orientation = self._canonical_orientation.get(undirected)
            if orientation is None:
                # Fallback to sorted orientation if graph hasn't provided one.
                orientation = undirected
            start, end = orientation
            base_cost = self.base_edge_costs.get(undirected, 0.0)
            forward_flow = float(self._flow_histogram.get((start, end), 0.0))
            reverse_flow = float(self._flow_histogram.get((end, start), 0.0))
            forward, reverse = self._blend_directional_costs(
                orientation,
                base_cost,
                forward_flow,
                reverse_flow,
            )
            self._directional_cache[orientation] = (forward, reverse)

    def _recompute_all_directional_costs(self) -> None:
        if self.graph is None:
            return
        if not self.base_edge_costs:
            return
        new_cache: Dict[EdgeKey, Tuple[float, float]] = {}
        for undirected, orientation in self._canonical_orientation.items():
            start, end = orientation
            base_cost = self.base_edge_costs.get(undirected, 0.0)
            forward_flow = float(self._flow_histogram.get((start, end), 0.0))
            reverse_flow = float(self._flow_histogram.get((end, start), 0.0))
            forward, reverse = self._blend_directional_costs(
                orientation,
                base_cost,
                forward_flow,
                reverse_flow,
            )
            new_cache[orientation] = (forward, reverse)
        self._directional_cache = new_cache

    def _reset_histogram(self) -> None:
        self._flow_histogram = Counter()
        self._robot_ordered_edges = {}

    def _blend_directional_costs(
        self,
        key: EdgeKey,
        base_cost: float,
        forward_flow: float,
        reverse_flow: float,
    ) -> Tuple[float, float]:
        total = forward_flow + reverse_flow
        if total <= 0.0:
            forward = reverse = base_cost
        else:
            bias = (forward_flow - reverse_flow) / total
            conflict = min(forward_flow, reverse_flow) / total
            forward = base_cost - self.alignment_gain * bias
            reverse = base_cost + self.alignment_gain * bias
            penalty = self.conflict_gain * conflict
            forward += penalty
            reverse += penalty
            if self.opposite_penalty_gain > 0.0 and abs(bias) > 1e-6:
                minority_penalty = self.opposite_penalty_gain * abs(bias)
                if bias > 0.0:
                    reverse += minority_penalty
                else:
                    forward += minority_penalty

        max_cost = max(0.5, self.max_directional_cost)
        forward = max(0.0, min(max_cost, forward))
        reverse = max(0.0, min(max_cost, reverse))

        if self.decay_alpha > 0.0 and self.decay_alpha < 1.0:
            previous = self._directional_cache.get(key)
            if previous is not None:
                old_forward, old_reverse = previous
                forward = (1.0 - self.decay_alpha) * old_forward + self.decay_alpha * forward
                reverse = (1.0 - self.decay_alpha) * old_reverse + self.decay_alpha * reverse

        return forward, reverse
