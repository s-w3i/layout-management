import json
import io
from pathlib import Path

import pytest

from datetime import date

from lacam_dte.engine import _action_seconds, _joint_path_step_seconds, simulate_day
from lacam_dte.cli import LineProgress
from lacam_dte.io import load_orders
from lacam_dte.models import Amr, Config, Grid, Motion, Task
from lacam_dte.planner import LaCAMPlanner, PlannerError
from lacam_dte.render import _profile_duration, _profile_progress


def line_grid() -> Grid:
    nodes = {(0, 0), (1, 0), (2, 0)}
    edges = {((0, 0), (1, 0)), ((1, 0), (0, 0)), ((1, 0), (2, 0)), ((2, 0), (1, 0))}
    return Grid(3, 1, 1, 1, nodes, edges, {}, {})


def service_grid() -> Grid:
    nodes = {(0, 0), (1, 0), (2, 0), (0, 1), (1, 1), (2, 1)}
    pairs = [((0, 0), (1, 0)), ((1, 0), (0, 1)), ((0, 1), (1, 1)), ((0, 1), (0, 0)),
             ((1, 1), (2, 1)), ((2, 1), (1, 0)), ((1, 0), (2, 0)), ((2, 1), (0, 0))]
    edges = {(a, b) for a, b in pairs} | {(b, a) for a, b in pairs}
    return Grid(3, 2, 1, 1, nodes, edges, {"RACK": (1, 0)}, {"WS": (1, 1)})


def test_config_validation(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "schema": "lacam_dte_config/v1", "amr_count": 1,
        "spawn_nodes": ["G0_0"], "workstations": ["WS"],
    }))
    assert Config.load(path).amr_count == 1


def test_plan_validation_rejects_edge_swap():
    planner = object.__new__(LaCAMPlanner); planner.grid = line_grid()
    with pytest.raises(PlannerError, match="edge swap"):
        planner.validate([[(0, 0), (1, 0)], [(1, 0), (0, 0)]], [(0, 0), (1, 0)], [(1, 0), (0, 0)], set())


def test_directed_move_rejected():
    grid = line_grid(); grid.edges.remove(((1, 0), (0, 0)))
    planner = object.__new__(LaCAMPlanner); planner.grid = grid
    with pytest.raises(PlannerError, match="directed"):
        planner.validate([[(1, 0)], [(0, 0)]], [(1, 0)], [(0, 0)], set())


def test_terminal_traversal_rejected():
    planner = object.__new__(LaCAMPlanner); planner.grid = line_grid()
    with pytest.raises(PlannerError, match="terminal"):
        planner.validate([[(0, 0)], [(1, 0)], [(2, 0)]], [(0, 0)], [(2, 0)], {(1, 0)})


def test_one_amr_day_completes(tmp_path):
    grid = service_grid()
    config = Config(1, ("G0_0",), ("WS",), planner_timeout_seconds=2,
                    workstation_entries={"WS": "G0_1"}, workstation_exits={"WS": "G2_1"})
    binary = LaCAMPlanner.build(Path(__file__).resolve().parents[1])
    result = simulate_day(
        grid, {"G1_0": {"SKU"}},
        [Task("T", date(2024, 1, 1), "STORE", 0, {"SKU": 2})],
        {"STORE": "WS"}, config, binary,
    )
    assert not result.metrics["failed"]
    assert result.metrics["completed_lines"] == 2
    assert result.metrics["rack_presentations"] == 1
    times = {event["event"]: event["time_seconds"] for event in result.events}
    assert times["depart_rack_for_queue"] - times["rack_reached"] == pytest.approx(4)
    assert times["service_done"] - times["station_reached"] == pytest.approx(10)
    assert times["job_complete"] - times["rack_returned"] == pytest.approx(4)


def test_progress_counts_order_lines():
    stream = io.StringIO()
    progress = LineProgress(["layout"], 10, stream)
    progress.update("layout", 3)
    progress.update("layout", 7)
    assert progress.done["layout"] == 10
    assert "10/10" in stream.getvalue().replace(" ", "")


def test_motion_profile_charges_actual_turn_angle():
    grid = service_grid(); config = Config(1, ("G0_0",), ("WS",))
    amr = Amr("AMR", (0, 0), (0, 0), heading_radians=0)
    straight = _action_seconds(grid, config, [(amr, (1, 0))])
    turning = _action_seconds(grid, config, [(amr, (0, 1))])
    assert straight > 0
    assert turning > straight


def test_kinematic_profile_reaches_endpoint_continuously():
    duration = _profile_duration(2.0, 1.5, .75)
    halfway = _profile_progress(2.0, 1.5, .75, duration / 2)
    assert 0 < halfway < 2
    assert _profile_progress(2.0, 1.5, .75, duration) == pytest.approx(2)


def test_complete_straight_path_does_not_stop_at_each_grid():
    grid = line_grid(); config = Config(1, ("G0_0",), ("WS",))
    amr = Amr("AMR", (0, 0), (2, 0), heading_radians=0)
    durations = _joint_path_step_seconds(
        grid, config, [amr], [[(0, 0)], [(1, 0)], [(2, 0)]], frozenset(),
    )
    per_edge = _action_seconds(grid, config, [(amr, (1, 0))])
    assert sum(durations) < 2 * per_edge
    assert all(duration > 0 for duration in durations)


def test_idle_amr_on_requested_rack_owns_goal():
    grid = service_grid()
    config = Config(2, ("G0_0", "G1_0"), ("WS",), planner_timeout_seconds=2,
                    workstation_entries={"WS": "G0_1"}, workstation_exits={"WS": "G2_1"})
    binary = LaCAMPlanner.build(Path(__file__).resolve().parents[1])
    result = simulate_day(
        grid, {"G1_0": {"SKU"}},
        [Task("T", date(2024, 1, 1), "STORE", 0, {"SKU": 1})],
        {"STORE": "WS"}, config, binary,
    )
    assert not result.metrics["failed"]
    assert result.jobs[0]["amr_id"] == "AMR_002"
    assert result.jobs[0]["station_queue_node"] != (1, 0)
    assert "station_queue_arrival" in {event["event"] for event in result.events}


def test_same_station_jobs_use_admission_backpressure():
    grid = service_grid()
    config = Config(2, ("G0_0", "G1_0"), ("WS",), planner_timeout_seconds=2,
                    workstation_entries={"WS": "G0_1"}, workstation_exits={"WS": "G2_1"})
    binary = LaCAMPlanner.build(Path(__file__).resolve().parents[1])
    result = simulate_day(
        grid, {"G0_0": {"A"}, "G1_0": {"B"}},
        [Task("T", date(2024, 1, 1), "STORE", 0, {"A": 1, "B": 1})],
        {"STORE": "WS"}, config, binary,
    )
    dispatches = [event for event in result.events if event["event"] == "job_dispatched"]
    assert not result.metrics["failed"]
    assert len(dispatches) == 2
    assert dispatches[1]["tick"] > dispatches[0]["tick"]
    assert result.metrics["planner_calls"] < result.metrics["makespan_ticks"]
    for job in result.jobs:
        history = [event["event"] for event in result.events if event.get("job_id") == job["job_id"]]
        assert history == [
            "job_dispatched", "rack_reached", "depart_rack_for_queue",
            "station_queue_arrival", "station_reached", "service_done",
            "workstation_exit", "rack_returned", "job_complete",
        ]


def test_external_holding_cell_is_not_station_entry():
    grid = service_grid()
    config = Config(
        2, ("G0_0", "G1_0"), ("WS",), planner_timeout_seconds=2,
        station_queue_capacity=2,
        workstation_entries={"WS": "G0_1"}, workstation_exits={"WS": "G2_1"},
        workstation_holding_paths={"WS": ("G0_0", "G0_1")},
    )
    binary = LaCAMPlanner.build(Path(__file__).resolve().parents[1])
    result = simulate_day(
        grid, {"G0_0": {"A"}, "G1_0": {"B"}},
        [Task("T", date(2024, 1, 1), "STORE", 0, {"A": 1, "B": 1})],
        {"STORE": "WS"}, config, binary,
    )
    assert not result.metrics["failed"]
    for job in result.jobs:
        history = [event["event"] for event in result.events if event.get("job_id") == job["job_id"]]
        assert history.count("station_queue_arrival") == 1
        assert history.index("station_queue_arrival") < history.index("station_reached")


def test_orders_group_by_date_and_store_and_ignore_release_time(tmp_path):
    from openpyxl import Workbook
    book = Workbook(); sheet = book.active
    sheet.append(["Date", "Store ID", "Item or SKU", "Release Seconds"])
    sheet.append([date(2024, 1, 1), "STORE", "A", 10])
    sheet.append([date(2024, 1, 1), "STORE", "B", 999])
    path = tmp_path / "orders.xlsx"; book.save(path); book.close()
    tasks = load_orders(path)
    assert len(tasks) == 1
    assert tasks[0].store == "STORE"
    assert tasks[0].release_seconds == 0
    assert tasks[0].lines == {"A": 1, "B": 1}
