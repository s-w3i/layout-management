from dataclasses import replace
import json
from pathlib import Path

import pytest

from amr_simulation.coordination import (
    DramCoordinator,
    RobotCoordinationState,
    can_replan,
    corridor_sides,
    detect_conflicts,
    load_fixture,
    next_epoch,
    straight_window,
    select_replan_robot,
    select_corridor_yield_side,
    wait_for_cycles,
)
from amr_simulation.engine import _Engine
from amr_simulation.models import CoordinationConfig, SimulationConfig
from amr_simulation.routing import GridRouter
from amr_simulation.results import write_csv
from amr_simulation.run_simulation import _checkpoint_metrics


def test_default_config_is_backward_compatible():
    config = SimulationConfig.load(Path("amr_simulation/config/default.json"))
    assert config.coordination.profile == "legacy_v1"
    assert config.coordination.max_reservation_nodes == 5
    assert config.snapshot()["coordination"]["deadlock_policy"] == "fail"


@pytest.mark.parametrize("field,value", [
    ("allocation_period_seconds", 0),
    ("max_reservation_nodes", 0),
    ("acknowledgement_latency_seconds", -1),
])
def test_coordination_config_validation(field, value):
    with pytest.raises(ValueError):
        replace(CoordinationConfig(), **{field: value}).validate()


def test_epoch_boundary_and_straight_window():
    coordinates = {"A": (0, 0), "B": (1, 0), "C": (2, 0), "D": (2, 1)}
    assert next_epoch(1.0, 1.0) == 1.0
    assert next_epoch(1.01, 1.0) == 2.0
    assert straight_window(("A", "B", "C", "D"), 0, 5, coordinates) == ("A", "B", "C")
    assert straight_window(("A", "B", "A"), 0, 5, coordinates) == ("A", "B")


def test_same_timestamp_event_priority_is_not_insertion_order():
    engine = object.__new__(_Engine)
    engine.field_coordination = True
    engine.sequence = 0
    engine.calendar = []
    engine.schedule(1.0, "movement_tail", None)
    engine.schedule(1.0, "allocation_epoch", None)
    engine.schedule(1.0, "node_crossing", None)
    assert [entry[3] for entry in sorted(engine.calendar)] == [
        "node_crossing", "allocation_epoch", "movement_tail",
    ]


def test_partial_grant_loaded_order_and_acknowledgement():
    config = replace(CoordinationConfig(), profile="dram_field_v1")
    coordinates = {name: (index, 0) for index, name in enumerate("ABCDE")}
    coordinator = DramCoordinator(config, coordinates, {"C": "BLOCKER"})
    coordinator.register_route("R2", ("A", "B", "C", "D"), loaded=False, now=0)
    coordinator.request_reservation("R2", 0)
    decision = coordinator.run_allocation_epoch(0)[0]
    assert decision.granted == ("A", "B")
    assert decision.blocker == ("C", "BLOCKER")
    assert coordinator.acknowledge_crossing("R2", "A", "B", now=.25, path_revision=1)
    assert "A" not in coordinator.owners
    json.dumps(coordinator.snapshot())


def test_loaded_robot_is_allocated_first_with_stable_ties():
    config = replace(CoordinationConfig(), profile="dram_field_v1")
    coordinates = {name: (index, 0) for index, name in enumerate("ABCD")}
    coordinator = DramCoordinator(config, coordinates)
    coordinator.register_route("R2", ("C", "D"), loaded=False, now=0)
    coordinator.register_route("R1", ("A", "B"), loaded=True, now=0)
    coordinator.request_reservation("R2", 0)
    coordinator.request_reservation("R1", 0)
    assert [decision.robot_id for decision in coordinator.run_allocation_epoch(0)] == ["R1", "R2"]


def test_stale_acknowledgement_is_ignored():
    coordinator = DramCoordinator(CoordinationConfig(), {"A": (0, 0), "B": (1, 0)})
    coordinator.register_route("R", ("A", "B"), loaded=False, now=0, path_revision=2)
    assert not coordinator.acknowledge_crossing("R", "A", "B", now=1, path_revision=1)
    assert coordinator.snapshot()["stale_event_count"] == 1


def test_conflict_detectors_and_wait_for_cycle():
    config = CoordinationConfig()
    swap = {
        "R1": RobotCoordinationState("R1", ("A", "B", "C"), True),
        "R2": RobotCoordinationState("R2", ("B", "A", "D"), False),
    }
    assert "head_to_head" in {item.kind for item in detect_conflicts(swap, config)}
    assert wait_for_cycles({"R1": {"R2"}, "R2": {"R3"}, "R3": {"R1"}}) == (("R1", "R2", "R3"),)
    cycle = {
        "R1": RobotCoordinationState("R1", ("A", "B"), False),
        "R2": RobotCoordinationState("R2", ("B", "C"), False),
        "R3": RobotCoordinationState("R3", ("C", "A"), False),
    }
    assert "partial_cycle" in {item.kind for item in detect_conflicts(cycle, config)}


def test_annotated_corridor_and_replan_selection():
    config = replace(CoordinationConfig(), corridors=(("A", "B", "C"),))
    robots = {
        "R1": RobotCoordinationState("R1", ("A", "B"), True),
        "R2": RobotCoordinationState("R2", ("B", "A"), True),
        "R3": RobotCoordinationState("R3", ("C", "B"), False, wait_for={"R2"}),
    }
    conflicts = detect_conflicts(robots, config)
    corridor = next(item for item in conflicts if item.kind == "corridor_deadlock")
    graph = {"A": [("B", 1.0)], "B": [("A", 1.0)], "C": [("B", 1.0)]}
    assert select_replan_robot(tuple(robots), robots, graph, {}) == "R3"
    sides = corridor_sides(corridor, robots, config.corridors)
    assert select_corridor_yield_side(*sides, robots, graph, {}) in sides


def test_replan_feasibility_respects_loaded_rack_restriction():
    robot = RobotCoordinationState("R", ("A", "B"), True)
    graph = {"A": [("B", 1.0)]}
    assert not can_replan(robot, graph, {}, {"B"})
    assert can_replan(replace(robot, loaded=False), graph, {}, {"B"})


class _Project:
    markers = {}

    def iter_positions(self):
        return iter(((0, 0), (1, 0), (0, 1), (1, 1)))

    def coordinates(self, x, y):
        return float(x), float(y)

    def iter_traversable_lane_positions(self):
        return iter((
            ((0, 0), (1, 0)), ((1, 0), (0, 0)),
            ((0, 0), (0, 1)), ((0, 1), (1, 1)),
            ((1, 1), (1, 0)),
        ))


def test_directed_edges_and_cache_are_isolated():
    router = GridRouter(_Project())
    direct = router.route((0, 0), (1, 0))
    detour = router.route((0, 0), (1, 0), blocked_edges=frozenset({((0, 0), (1, 0))}))
    reverse = router.route((1, 0), (0, 0), blocked_edges=frozenset({((0, 0), (1, 0))}))
    assert direct.positions == ((0, 0), (1, 0))
    assert detour.positions == ((0, 0), (0, 1), (1, 1), (1, 0))
    assert reverse.positions == ((1, 0), (0, 0))


def test_fixture_matrix_is_complete():
    fixture = load_fixture(Path("tests/fixtures/dram_coordination/scenarios.json"))
    assert {case["id"] for case in fixture["cases"]} == {
        "C01", "C02", "C03", "C04", "C05", "C06", "C07", "C08", "C09", "C10",
        "R01", "R02", "R03", "R04", "A01", "A02", "E01", "E02", "D01", "D02",
    }


def test_checkpoint_preserves_coordination_status(tmp_path):
    path = tmp_path / "daily.csv"
    write_csv(path, [{
        "date": "2023-01-03",
        "coordination_status": "FAILED_DEADLOCK",
        "eligible_for_comparison": False,
        "coordination": {"profile": "dram_field_v1", "status": "FAILED_DEADLOCK"},
    }])
    restored = _checkpoint_metrics(path)[0]
    assert restored["coordination_status"] == "FAILED_DEADLOCK"
    assert restored["eligible_for_comparison"] is False
