from __future__ import annotations

import json
import subprocess
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from .models import Grid, Node


@dataclass(frozen=True, slots=True)
class Plan:
    configurations: list[list[Node]]
    runtime_ms: float
    retries: int
    sum_of_costs: int


class PlannerError(RuntimeError):
    pass


class LaCAMPlanner:
    def __init__(self, grid: Grid, binary: Path, timeout_seconds: float, seed: int):
        self.grid, self.binary = grid, binary
        self.timeout_seconds, self.seed = timeout_seconds, seed
        self.calls = self.retries = self.timeouts = 0
        self.runtime_seconds = 0.0

    @staticmethod
    def build(package_root: Path) -> Path:
        build = package_root / "build"
        binary = build / "lacam_bridge"
        if not binary.exists():
            subprocess.run(["cmake", "-S", str(package_root), "-B", str(build)], check=True)
            subprocess.run(["cmake", "--build", str(build), "-j2"], check=True)
        return binary

    def solve(self, starts: list[Node], goals: list[Node], terminals: set[Node], locked=frozenset()) -> Plan:
        if len(set(starts)) != len(starts) or len(set(goals)) != len(goals):
            duplicate_starts = sorted({node for node in starts if starts.count(node) > 1})
            duplicate_goals = sorted({node for node in goals if goals.count(node) > 1})
            raise PlannerError(f"LaCAM requires unique starts and goals; duplicate starts={duplicate_starts}, goals={duplicate_goals}")
        total_runtime = 0.0
        for retry in range(2):
            self.calls += 1
            if retry: self.retries += 1
            payload = self._payload(starts, goals, terminals, self.seed + retry, locked)
            began = time.monotonic()
            try:
                run = subprocess.run(
                    [str(self.binary)], input=json.dumps(payload), text=True,
                    capture_output=True, timeout=self.timeout_seconds + 1,
                )
            except subprocess.TimeoutExpired:
                self.timeouts += 1; total_runtime += time.monotonic() - began; continue
            elapsed = time.monotonic() - began
            total_runtime += elapsed; self.runtime_seconds += elapsed
            try: raw = json.loads(run.stdout.strip().splitlines()[-1])
            except (json.JSONDecodeError, IndexError): raw = {"solved": False, "error": run.stderr or run.stdout}
            if not raw.get("solved"):
                if raw.get("runtime_ms", 0) >= self.timeout_seconds * 1000: self.timeouts += 1
                continue
            reverse = {y * self.grid.width + x: (x, y) for x, y in self.grid.nodes}
            configs = [[reverse[index] for index in config] for config in raw["solution"]]
            self.validate(configs, starts, goals, terminals, locked)
            soc = sum(sum(a != b for a, b in zip(configs[t - 1], configs[t])) for t in range(1, len(configs)))
            return Plan(configs, total_runtime * 1000, retry, soc)
        raise PlannerError("LaCAM failed after timeout/unsolved retry")

    def _payload(self, starts: list[Node], goals: list[Node], terminals: set[Node], seed: int, locked=frozenset()) -> dict:
        index = lambda n: n[1] * self.grid.width + n[0]
        active_terminals = set(starts) | set(goals)
        usable = self.grid.nodes - (terminals - active_terminals)
        edges = [[index(a), index(b)] for a, b in sorted(self.grid.edges) if a in usable and b in usable]
        return {
            "width": self.grid.width, "height": self.grid.height,
            "nodes": [[index(n), n[0], n[1]] for n in sorted(usable)],
            "edges": edges, "starts": [index(n) for n in starts],
            "goals": [index(n) for n in goals],
            "terminals": [index(n) for n in sorted(terminals & usable)],
            "locked_agents": sorted(locked),
            "timeout_ms": int(self.timeout_seconds * 1000), "seed": seed,
        }

    def validate(self, configs: list[list[Node]], starts: list[Node], goals: list[Node], terminals: set[Node], locked=frozenset()) -> None:
        if not configs or configs[0] != starts or configs[-1] != goals:
            raise PlannerError("planner returned wrong endpoints")
        count = len(starts)
        for tick, config in enumerate(configs):
            if len(config) != count or len(set(config)) != count:
                raise PlannerError(f"vertex collision at plan tick {tick}")
            if tick == 0: continue
            previous = configs[tick - 1]
            if any(config[agent] != starts[agent] for agent in locked):
                raise PlannerError(f"locked agent moved at plan tick {tick}")
            for agent, (a, b) in enumerate(zip(previous, config)):
                if a != b and (a, b) not in self.grid.edges:
                    raise PlannerError(f"invalid directed move {a}->{b}")
                if b in terminals and b != goals[agent] and b != starts[agent]:
                    raise PlannerError(f"agent {agent} traversed terminal {b}")
            for i in range(count):
                for j in range(i + 1, count):
                    if previous[i] == config[j] and previous[j] == config[i] and previous[i] != previous[j]:
                        raise PlannerError(f"opposing edge swap at plan tick {tick}")


def distance_tables(grid: Grid, goals) -> dict[Node, dict[Node, int]]:
    reverse: dict[Node, list[Node]] = {node: [] for node in grid.nodes}
    for a, b in grid.edges: reverse[b].append(a)
    tables = {}
    for goal in set(goals):
        result, queue = {goal: 0}, deque([goal])
        while queue:
            node = queue.popleft()
            for before in reverse[node]:
                if before not in result:
                    result[before] = result[node] + 1; queue.append(before)
        tables[goal] = result
    return tables


def distances(grid: Grid, goal: Node) -> dict[Node, int]:
    return distance_tables(grid, (goal,))[goal]
