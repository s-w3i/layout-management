from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
import tempfile
import unittest

from openpyxl import Workbook

from amr_simulation.inputs import assign_workstations, load_workload
from amr_simulation.models import MotionProfile, Rack, SimulationConfig, WorkloadTask, grid_name
from warehouse_layout.domain import GridProject, GridSpec, Marker
from warehouse_layout.rmf import RmfMapService

from current_heat_simulation.astar_planner import AStarPlanner, WarehouseMap
from current_heat_simulation.sim_types import RobotSnapshot
from current_heat_simulation.task_scheduler import StoreDayScheduler
from current_heat_simulation.warehouse_system import WarehouseSystem


ROOT = Path(__file__).resolve().parents[2]


def amr_config():
    return SimulationConfig(
        amr_count=1, spawn_nodes=("G0_0",), initial_heading_degrees=0,
        workstations=("WS",), store_workstation_overrides={},
        jack_up_seconds=0.1, jack_down_seconds=0.1, service_seconds=0.1,
        motion=MotionProfile(),
    )


def small_map(directory):
    project = GridProject(GridSpec(width_m=6, length_m=4))
    project.markers = {(0, 0): Marker("rack", "RACK"), (6, 4): Marker("workstation", "WS")}
    path = Path(directory)/"grid.json"
    RmfMapService().save_project(project, path)
    return WarehouseMap(path)


class CurrentHeatTests(unittest.TestCase):
    def test_workbook_groups_store_per_day_and_ignores_order_line_time(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"orders.xlsx"
            workbook = Workbook()
            sheet = workbook.active
            sheet.append(["Date", "Store ID", "Item or SKU", "Order Line Time"])
            sheet.append([datetime(2023, 1, 3, 8), "A", "sku1", "08:00"])
            sheet.append([datetime(2023, 1, 3, 23), "A", "sku1", "23:59"])
            sheet.append([datetime(2023, 1, 3, 15), "A", "sku2", "15:00"])
            sheet.append([datetime(2023, 1, 3, 9), "B", "sku1", "09:00"])
            sheet.append([datetime(2023, 1, 4, 9), "A", "sku1", "09:00"])
            workbook.save(path)
            fresh = load_workload(path, cache_dir=Path(directory)/"cache")
            cached = load_workload(path, cache_dir=Path(directory)/"cache")
            self.assertEqual(fresh.tasks, cached.tasks)
            self.assertEqual(len(fresh.tasks), 3)
            self.assertEqual(fresh.tasks[0].line_counts, {"sku1": 2, "sku2": 1})
            self.assertTrue(all(task.release_seconds == 0 for task in fresh.tasks))
            self.assertEqual(len(fresh.select(date(2023, 1, 3), date(2023, 1, 3))), 2)

    def test_native_map_preserves_directed_lanes_and_rack_obstacles(self):
        warehouse = WarehouseMap(ROOT/"resources/map/map1_1.grid.json")
        edges = {(warehouse.vertices[a].name, warehouse.vertices[b].name) for a, b, _ in warehouse.adjacency_items()}
        expected = {(grid_name(a), grid_name(b)) for a, b in warehouse.project.iter_traversable_lane_positions()}
        self.assertEqual(edges, expected)
        self.assertEqual(len(warehouse.vertices), len(list(warehouse.project.iter_positions())))
        self.assertEqual(warehouse.workstations["WS_2_20"], "G2_20")
        planner = AStarPlanner(warehouse)
        for loaded in (False, True):
            path = planner.plan("G17_10", "G2_20", loaded)
            self.assertFalse(set(path[1:-1]) & warehouse.pickup_dispensers)
            self.assertTrue(all(edge in edges for edge in zip(path, path[1:])))

    def test_coverage_priority_and_predicted_pickup_assignment(self):
        with tempfile.TemporaryDirectory() as directory:
            warehouse = small_map(directory)
            config = amr_config()
            task = WorkloadTask("2023-01-03/A", date(2023, 1, 3), 80000, "A", {"x": 2, "y": 1})
            racks = {
                "G0_0": Rack("G0_0", (0, 0), frozenset({"x", "y"})),
                "G5_4": Rack("G5_4", (5, 4), frozenset({"x"})),
            }
            scheduler = StoreDayScheduler([task], racks, {"A": "WS"}, config, warehouse)
            snapshots = {
                "far": RobotSnapshot("far", "G6_3", 6, 3, 0, 0, False),
                "near": RobotSnapshot("near", "G0_1", 0, 1, 0, 0, False),
            }
            request = scheduler.dispatch_next(snapshots, ["far", "near"], 0)
            self.assertEqual(request.rack_id, "G0_0")
            self.assertEqual(request.robot_name, "near")
            self.assertEqual(scheduler.jobs[request.task_id].lines, {"x": 2, "y": 1})
            self.assertFalse(scheduler.done)
            scheduler.mark_completed(request.task_id, 10)
            self.assertTrue(scheduler.done)
            self.assertEqual(scheduler.tasks[0].completed_lines, 3)

    def test_complete_store_groups_return_home_and_export_release_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            warehouse = small_map(directory)
            config = amr_config()
            tasks = [WorkloadTask(f"2023-01-03/{store}", date(2023, 1, 3), 80000, store, {"x": count})
                     for store, count in [("A", 2), ("B", 3)]]
            racks = {"G0_0": Rack("G0_0", (0, 0), frozenset({"x"}))}
            mapping = assign_workstations(tasks, config)
            system = WarehouseSystem({"conflicts": {"print_conflicts": False},
                                      "motion": {"enforce_collision_safety": True}},
                                     warehouse, tasks, racks, mapping, config)
            summary = system.run(headless=True, output=Path(directory)/"results", max_seconds=200)
            self.assertTrue(summary["success_flag"], summary)
            self.assertEqual(summary["completed_tasks"], 2)
            self.assertEqual(summary["completed_lines"], 5)
            self.assertEqual(summary["completed_rack_jobs"], 2)
            self.assertEqual(summary["rack_presentations"], 2)
            self.assertEqual(summary["completed_order_lines_per_rack_presentation"], 2.5)
            self.assertEqual(system.task_state_machine.rack_positions, {"G0_0": "G0_0"})
            self.assertEqual(system.simulator.robots[0].current_vertex, "G0_0")
            import csv
            with (Path(directory)/"results/tasks.csv").open() as stream:
                self.assertTrue(all(float(row["release_time_s"]) == 0 for row in csv.DictReader(stream)))
            # A fresh day resets robots, reservations, jobs, and task completion.
            next_tasks = [replace(tasks[0], task_date=date(2023, 1, 4), task_id="2023-01-04/A")]
            next_day = WarehouseSystem({}, warehouse, next_tasks, racks, mapping, config)
            self.assertEqual(next_day.sim_time_sec, 0)
            self.assertFalse(next_day.done)
            self.assertEqual(next_day.simulator.robots[0].current_vertex, "G0_0")

    def test_duration_limit_marks_incomplete(self):
        with tempfile.TemporaryDirectory() as directory:
            warehouse = small_map(directory)
            tasks = [WorkloadTask("2023-01-03/A", date(2023, 1, 3), 0, "A", {"x": 1})]
            racks = {"G0_0": Rack("G0_0", (0, 0), frozenset({"x"}))}
            system = WarehouseSystem({}, warehouse, tasks, racks, {"A": "WS"}, amr_config())
            result = system.run(headless=True, output=Path(directory)/"results", max_seconds=0.07)
            self.assertAlmostEqual(result["sim_duration_s"], 0.07)
            self.assertFalse(result["success_flag"])
            self.assertEqual(result["failure_reason"], "simulation_time_limit_with_unfinished_tasks")


if __name__ == "__main__":
    unittest.main()
