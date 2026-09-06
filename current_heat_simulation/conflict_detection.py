from __future__ import annotations

from typing import Dict, List, Sequence, Set, Tuple


CORRIDOR_SIDE_OFFSET = 1000


from .astar_planner import get_path_overlap_conflicts, detect_deadlock_wait_for


class ConflictDetector:
    def __init__(
        self,
        *,
        head_to_head_window: int = 3,
        trivial_cyclic_window: int = 3,
        overlap_window: int = 3,
        deadlock_window: int = 3,
        print_conflicts: bool = True,
    ) -> None:
        self.head_to_head_window = head_to_head_window
        self.trivial_cyclic_window = trivial_cyclic_window
        self.overlap_window = overlap_window
        self.deadlock_window = deadlock_window
        self.print_conflicts = print_conflicts
        self._active_conflict_signatures: Set[str] = set()

    def detect_conflicts(
        self,
        planned_paths: Dict[str, List[Tuple[str, float, float]]],
    ) -> List[Dict[str, object]]:
        conflict_msgs: List[Dict[str, object]] = []
        current_nodes: Dict[str, str] = {}
        next_nodes: Dict[str, str] = {}
        waited_by: Dict[str, Set[str]] = {robot_name: set() for robot_name in planned_paths}

        for robot_name, path in planned_paths.items():
            if path:
                current_nodes[robot_name] = path[0][0]
            if len(path) >= 2:
                next_nodes[robot_name] = path[1][0]

        for robot_name, next_node in next_nodes.items():
            for other_name, current_node in current_nodes.items():
                if robot_name == other_name:
                    continue
                if next_node == current_node:
                    waited_by[other_name].add(robot_name)
        wait_for = {
            robot_name: next(
                (
                    other_name
                    for other_name, current_node in current_nodes.items()
                    if other_name != robot_name and next_nodes.get(robot_name) == current_node
                ),
                None,
            )
            for robot_name in planned_paths
        }

        def add_conflict(
            conflict_type: str,
            robots: Sequence[str],
            from_nodes: Sequence[str],
            to_nodes: Sequence[str],
            steps: Sequence[int],
        ) -> None:
            entries = []
            for idx, robot_name in enumerate(robots):
                entries.append(
                    {
                        "robot": robot_name,
                        "from": from_nodes[idx] if idx < len(from_nodes) else "",
                        "to": to_nodes[idx] if idx < len(to_nodes) else "",
                        "step": steps[idx] if idx < len(steps) else 0,
                    }
                )
            conflict_msgs.append({"type": conflict_type, "robots": entries})

        h2h_paths = {
            robot_name: path[: self.head_to_head_window]
            for robot_name, path in planned_paths.items()
            if len(path) >= self.head_to_head_window
        }
        names = list(h2h_paths.keys())
        h2h_candidates: List[Dict[str, object]] = []
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                r1, r2 = names[i], names[j]
                p1, p2 = h2h_paths[r1], h2h_paths[r2]
                horizon = min(len(p1), len(p2)) - 1
                for t in range(horizon):
                    if t > 0:
                        break
                    curr1, next1 = p1[t][0], p1[t + 1][0]
                    curr2, next2 = p2[t][0], p2[t + 1][0]
                    if curr1 == next2 and curr2 == next1:
                        h2h_candidates.append(
                            {
                                "robots": (r1, r2),
                                "from": (curr1, curr2),
                                "to": (next1, next2),
                                "step": t + 1,
                            }
                        )
                        continue

        handled_h2h: Set[str] = set()
        for candidate in h2h_candidates:
            r1, r2 = candidate["robots"]  # type: ignore[assignment]
            if r1 in handled_h2h or r2 in handled_h2h:
                continue

            queue_a = self._gather_corridor_queue(r1, r2, waited_by)
            queue_b = self._gather_corridor_queue(r2, r1, waited_by)
            corridor_group = queue_a + [robot for robot in queue_b if robot not in queue_a]
            if len(queue_a) >= 1 and len(queue_b) >= 1 and len(corridor_group) >= 3:
                from_nodes: List[str] = []
                to_nodes: List[str] = []
                steps: List[int] = []

                for idx, robot_name in enumerate(queue_a):
                    path = planned_paths.get(robot_name, [])
                    curr = path[0][0] if len(path) >= 1 else ""
                    nxt = path[1][0] if len(path) >= 2 else curr
                    from_nodes.append(curr)
                    to_nodes.append(nxt)
                    steps.append(idx + 1)

                for idx, robot_name in enumerate(queue_b):
                    path = planned_paths.get(robot_name, [])
                    curr = path[0][0] if len(path) >= 1 else ""
                    nxt = path[1][0] if len(path) >= 2 else curr
                    from_nodes.append(curr)
                    to_nodes.append(nxt)
                    steps.append(CORRIDOR_SIDE_OFFSET + idx + 1)

                add_conflict("corridor_deadlock", queue_a + queue_b, from_nodes, to_nodes, steps)
                handled_h2h.update(corridor_group)
                continue

            curr1, curr2 = candidate["from"]  # type: ignore[assignment]
            next1, next2 = candidate["to"]  # type: ignore[assignment]
            step = int(candidate["step"])
            add_conflict("head-to-head", [r1, r2], [curr1, curr2], [next1, next2], [step, step])
            handled_h2h.update({r1, r2})

        overlaps = get_path_overlap_conflicts(planned_paths, self.overlap_window)
        for r1, r2, node, step in overlaps:
            def prev_node(robot_name: str, s: int) -> str:
                path = planned_paths.get(robot_name, [])
                if not path:
                    return ""
                idx = max(0, s - 1)
                return path[idx][0]

            prev1 = prev_node(r1, step)
            prev2 = prev_node(r2, step)
            add_conflict("path_overlap", [r1, r2], [prev1, prev2], [node, node], [step, step])

        for robot_name, path in planned_paths.items():
            visited: Dict[str, int] = {}
            for t, (node, _x, _y) in enumerate(path[: self.trivial_cyclic_window]):
                if node in visited:
                    prev = path[t - 1][0] if t > 0 and len(path) > 0 else node
                    if prev == node:
                        continue
                    add_conflict("partial_trivial_cyclic", [robot_name], [prev], [node], [t])
                else:
                    visited[node] = t

        windowed = {robot_name: path[: self.deadlock_window] for robot_name, path in planned_paths.items()}
        if windowed:
            info, extended = detect_deadlock_wait_for(windowed)
            if info:
                robots = list(info.keys())
                from_nodes: List[str] = []
                to_nodes: List[str] = []
                steps: List[int] = []
                for robot_name in robots:
                    path = planned_paths.get(robot_name, [])
                    if not extended:
                        curr = path[0][0] if len(path) >= 1 else ""
                        nxt = path[1][0] if len(path) >= 2 else curr
                    else:
                        curr = path[1][0] if len(path) >= 2 else ""
                        nxt = path[2][0] if len(path) >= 3 else ""
                    from_nodes.append(curr)
                    to_nodes.append(nxt)
                    steps.append(1)
                add_conflict("deadlock", robots, from_nodes, to_nodes, steps)

        for chain in self._detect_wait_chains(planned_paths, waited_by, wait_for):
            from_nodes: List[str] = []
            to_nodes: List[str] = []
            steps: List[int] = []
            for idx, robot_name in enumerate(chain):
                path = planned_paths.get(robot_name, [])
                curr = path[0][0] if len(path) >= 1 else ""
                nxt = path[1][0] if len(path) >= 2 else curr
                from_nodes.append(curr)
                to_nodes.append(nxt)
                steps.append(idx + 1)
            add_conflict("wait_chain", chain, from_nodes, to_nodes, steps)

        return conflict_msgs

    def detect_and_log_conflicts(
        self,
        planned_paths: Dict[str, List[Tuple[str, float, float]]],
    ) -> List[Dict[str, object]]:
        conflicts = self.detect_conflicts(planned_paths)
        signatures = {self._conflict_signature(conflict): conflict for conflict in conflicts}
        if self.print_conflicts:
            for signature, conflict in signatures.items():
                if signature not in self._active_conflict_signatures:
                    print(self.format_conflict(conflict), flush=True)
        self._active_conflict_signatures = set(signatures)
        return conflicts

    def format_conflict(self, conflict: Dict[str, object]) -> str:
        entries = conflict["robots"]  # type: ignore[index]
        parts = []
        for entry in entries:  # type: ignore[assignment]
            parts.append(f"{entry['robot']} {entry['from']} -> {entry['to']} @ step {entry['step']}")
        return f"[conflict] {conflict['type']}: " + "; ".join(parts)

    def _conflict_signature(self, conflict: Dict[str, object]) -> str:
        entries = conflict["robots"]  # type: ignore[index]
        signature_parts = [str(conflict["type"])]
        for entry in entries:  # type: ignore[assignment]
            signature_parts.append(f"{entry['robot']}:{entry['from']}->{entry['to']}@{entry['step']}")
        return "|".join(signature_parts)

    def _gather_corridor_queue(
        self,
        front: str,
        opposing_front: str,
        waited_by: Dict[str, Set[str]],
    ) -> List[str]:
        queue = [front]
        visited = {front, opposing_front}
        current = front

        while True:
            followers = [robot_name for robot_name in waited_by.get(current, set()) if robot_name not in visited]
            if not followers:
                return queue
            if len(followers) > 1:
                return []
            next_robot = followers[0]
            queue.append(next_robot)
            visited.add(next_robot)
            current = next_robot

    def _detect_wait_chains(
        self,
        planned_paths: Dict[str, List[Tuple[str, float, float]]],
        waited_by: Dict[str, Set[str]],
        wait_for: Dict[str, str | None],
    ) -> List[List[str]]:
        chains: List[List[str]] = []
        seen: Set[Tuple[str, ...]] = set()
        heads = [
            robot_name
            for robot_name, blocker in wait_for.items()
            if blocker is not None and not waited_by.get(robot_name)
        ]

        for head in heads:
            chain: List[str] = []
            visited: Set[str] = set()
            current = head
            while current is not None and current not in visited:
                chain.append(current)
                visited.add(current)
                current = wait_for.get(current)

            if current is not None:
                continue
            if len(chain) < 3:
                continue

            sink_path = planned_paths.get(chain[-1], [])
            sink_is_stationary = len(sink_path) < 2
            if not sink_is_stationary:
                continue

            signature = tuple(chain)
            if signature in seen:
                continue
            seen.add(signature)
            chains.append(chain)

        return chains
