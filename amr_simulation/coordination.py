"""Deterministic DRAM-style passage coordination helpers."""

from __future__ import annotations

from collections import deque

Node = tuple[int, int]


def same_direction_following(
    follower: tuple[Node, ...], leader: tuple[Node, ...], blocked: Node
) -> bool:
    """Return whether both routes leave the shared blocked node identically."""
    if blocked not in follower or blocked not in leader:
        return False
    fi, li = follower.index(blocked), leader.index(blocked)
    return (
        fi + 1 < len(follower)
        and li + 1 < len(leader)
        and follower[fi + 1] == leader[li + 1]
    )


def reversed_passage(path1: tuple[Node, ...], path2: tuple[Node, ...]) -> tuple[Node, ...]:
    """Return the longest contiguous edge sequence traversed in opposition."""
    best: tuple[Node, ...] = ()
    for first in range(len(path1) - 1):
        edge = (path1[first], path1[first + 1])
        for second in range(len(path2) - 1):
            if edge != (path2[second + 1], path2[second]):
                continue
            left, right = first, first + 1
            other_left, other_right = second, second + 1
            while (
                left > 0
                and other_right + 1 < len(path2)
                and path1[left - 1] == path2[other_right + 1]
            ):
                left -= 1
                other_right += 1
            while (
                right + 1 < len(path1)
                and other_left > 0
                and path1[right + 1] == path2[other_left - 1]
            ):
                right += 1
                other_left -= 1
            candidate = path1[left:right + 1]
            if len(candidate) > len(best) or (len(candidate) == len(best) and candidate < best):
                best = candidate
    return best


def wait_cycle(wait_for: dict[str, set[str]]) -> tuple[str, ...]:
    """Return the first deterministic directed wait-for cycle."""
    visiting: list[str] = []
    visited: set[str] = set()

    def visit(node: str):
        if node in visiting:
            return tuple(visiting[visiting.index(node):])
        if node in visited:
            return ()
        visiting.append(node)
        for blocker in sorted(wait_for.get(node, ())):
            cycle = visit(blocker)
            if cycle:
                return cycle
        visiting.pop()
        visited.add(node)
        return ()

    for robot in sorted(wait_for):
        cycle = visit(robot)
        if cycle:
            return cycle
    return ()


def following_queue(front: str, wait_for: dict[str, set[str]]) -> tuple[str, ...]:
    """Return a front-to-tail queue from transitive wait-for dependencies."""
    reverse: dict[str, list[str]] = {}
    for follower, blockers in wait_for.items():
        for blocker in blockers:
            reverse.setdefault(blocker, []).append(follower)
    result, pending, seen = [], deque((front,)), set()
    while pending:
        robot = pending.popleft()
        if robot in seen:
            continue
        seen.add(robot)
        result.append(robot)
        pending.extend(sorted(reverse.get(robot, ())))
    return tuple(result)
