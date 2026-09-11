from __future__ import annotations

from math import hypot
from random import shuffle
from typing import Dict, List

from .sim_types import RobotAllocationState, RobotSnapshot, RobotTaskContext


def get_alloc_order(
    robot_states: Dict[str, RobotAllocationState],
    robot_snapshots: Dict[str, RobotSnapshot],
    task_contexts: Dict[str, RobotTaskContext],
    strategy: str,
    warehouse_map=None,
    move_buffers=None,
) -> List[str]:
    def move_buffer_size(name: str) -> int:
        state = robot_states[name]
        return len((move_buffers or {}).get(name, ()))

    def last_reservation_time(name: str) -> float:
        return robot_states[name].last_reservation_time

    def next_move_cost(name: str) -> float:
        state = robot_states[name]
        snapshot = robot_snapshots[name]
        if state.full_path and state.current_index < len(state.full_path):
            x, y = warehouse_map.point(state.full_path[state.current_index])
            return hypot(x - snapshot.x, y - snapshot.y)
        return float("inf")

    def path_length(name: str) -> int:
        state = robot_states[name]
        return len(state.full_path)

    def path_cost(name: str) -> float:
        path = robot_states[name].full_path
        return sum(warehouse_map.distance(a, b) for a, b in zip(path, path[1:]))

    primary_key_funcs = {
        "ascPathCost": path_cost,
        "descPathCost": path_cost,
        "ascMoveBufferSize": move_buffer_size,
        "descMoveBufferSize": move_buffer_size,
        "ascLastReservationTime": last_reservation_time,
        "descLastReservationTime": last_reservation_time,
        "ascNextMoveCost": next_move_cost,
        "descNextMoveCost": next_move_cost,
        "ascPathLength": path_length,
        "descPathLength": path_length,
    }
    if strategy not in primary_key_funcs and strategy != "random":
        raise ValueError(f"Unknown allocation strategy: {strategy}")

    descending = strategy.startswith("desc")

    def build_key(name: str):
        primary = primary_key_funcs[strategy](name)
        if descending:
            primary = -primary
        carrying = robot_snapshots[name].jack_up if name in robot_snapshots else task_contexts.get(name, RobotTaskContext()).carrying_rack
        return (0 if carrying else 1, primary, -robot_states[name].priority, name)

    names = list(robot_states.keys())
    if strategy == "random":
        carrying = [name for name in names if (robot_snapshots[name].jack_up if name in robot_snapshots else task_contexts.get(name, RobotTaskContext()).carrying_rack)]
        not_carrying = [name for name in names if not (robot_snapshots[name].jack_up if name in robot_snapshots else task_contexts.get(name, RobotTaskContext()).carrying_rack)]
        shuffle(carrying)
        shuffle(not_carrying)
        return carrying + not_carrying

    names.sort(key=build_key)
    return names
