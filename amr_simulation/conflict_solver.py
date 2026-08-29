"""DRAM low-level pattern matching with trivial cycle detection."""

from __future__ import annotations

from collections.abc import Hashable, Sequence
from dataclasses import dataclass
from typing import TypeVar


Node = TypeVar("Node", bound=Hashable)


@dataclass(frozen=True, slots=True)
class DramConflict:
    kind: str
    agent_indices: tuple[int, ...]
    overlap_node: Node


def has_extended_head_to_head(path1: Sequence[Node], path2: Sequence[Node]) -> bool:
    """Match DRAM's reversed overlapping-path conflict check."""
    for length in range(1, min(len(path1), len(path2))):
        if all(path1[offset] == path2[length - offset] for offset in range(length + 1)):
            return True
    return False


def _head_to_head_overlap(path1: Sequence[Node], path2: Sequence[Node]):
    limit = min(len(path1), len(path2))
    first1, first2 = path1[0], path2[0]
    for length in range(1, limit):
        if (
            path2[length] == first1
            and path1[length] == first2
            and path1[:length + 1] == path2[:length + 1][::-1]
        ):
            return path1[1]
    return None


def has_trivial_cycle(paths: Sequence[Sequence[Node]], agent_index: int) -> bool:
    """Detect a cycle in the current-node to next-node wait graph."""
    edges = {path[0]: path[1] for path in paths if len(path) >= 2}
    node = paths[agent_index][0]
    visited: set[Node] = set()
    while node in edges:
        if node in visited:
            return True
        visited.add(node)
        node = edges[node]
    return False


def is_partial_solvable(paths: Sequence[Sequence[Node]], agent_index: int) -> bool:
    """Equivalent to DRAM's llPatternMatchingWithCdSolver partial check."""
    selected = paths[agent_index]
    if any(
        index != agent_index and has_extended_head_to_head(selected, path)
        for index, path in enumerate(paths)
    ):
        return False
    return not has_trivial_cycle(paths, agent_index)


def partial_conflict(
    paths: Sequence[Sequence[Node]], agent_index: int
) -> DramConflict | None:
    """Return DRAM conflict details for priority arbitration and tracing."""
    selected = paths[agent_index]
    for index, path in enumerate(paths):
        if index == agent_index:
            continue
        overlap = _head_to_head_overlap(selected, path)
        if overlap is not None:
            return DramConflict("head_to_head", (agent_index, index), overlap)

    edges = {
        path[0]: (path[1], index)
        for index, path in enumerate(paths)
        if len(path) >= 2
    }
    node = selected[0]
    visited: dict[Node, int] = {}
    order: list[tuple[Node, int]] = []
    while node in edges:
        if node in visited:
            cycle = order[visited[node]:]
            return DramConflict(
                "cycle",
                tuple(sorted({index for _node, index in cycle})),
                node,
            )
        visited[node] = len(order)
        next_node, owner = edges[node]
        order.append((node, owner))
        node = next_node
    return None


def partial_conflicts(
    paths: Sequence[Sequence[Node]], agent_index: int
) -> tuple[DramConflict, ...]:
    """Return every pairwise conflict, followed by any cycle conflict."""
    selected = paths[agent_index]
    conflicts = []
    for index, path in enumerate(paths):
        if index == agent_index:
            continue
        if selected[0] not in path[1:] or path[0] not in selected[1:]:
            continue
        overlap = _head_to_head_overlap(selected, path)
        if overlap is not None:
            conflicts.append(
                DramConflict("head_to_head", (agent_index, index), overlap)
            )
    edges = {
        path[0]: (path[1], index)
        for index, path in enumerate(paths)
        if len(path) >= 2
    }
    node = selected[0]
    visited: dict[Node, int] = {}
    order: list[tuple[Node, int]] = []
    while node in edges:
        if node in visited:
            cycle = order[visited[node]:]
            conflicts.append(DramConflict(
                "cycle",
                tuple(sorted({index for _node, index in cycle})),
                node,
            ))
            break
        visited[node] = len(order)
        next_node, owner = edges[node]
        order.append((node, owner))
        node = next_node
    return tuple(conflicts)
