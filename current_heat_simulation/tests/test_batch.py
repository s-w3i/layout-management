from datetime import date, datetime
from dataclasses import replace
from concurrent.futures import Future
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from openpyxl import Workbook
import yaml

from amr_simulation.models import Rack, WorkloadTask
from current_heat_simulation.batch import execute_batch, fingerprint, read_checkpoint, run_day
from current_heat_simulation.reporting import aggregate, generate_report, csv_rows
from current_heat_simulation.run_metrics import TIME_CATEGORIES, write_json
from current_heat_simulation.warehouse_system import WarehouseSystem
from current_heat_simulation.diagnostics import DiagnosticLog
from current_heat_simulation.pygame_simulator import main
from test_current_heat import amr_config, small_map

TIMING = {"mean_planning_latency_ms", "wall_clock_seconds", "run_wall_clock_seconds",
          "simulated_seconds_per_wall_second", "layout", "slotting_strategy"}


def shared_fixture(directory):
    small_map(directory)
    racks = {"G0_0": Rack("G0_0", (0, 0), frozenset({"x"}))}
    return {"fingerprint": "fixture", "grid": Path(directory)/"grid.json",
            "config": {"conflicts": {"print_conflicts": False}}, "amr": amr_config(),
            "layouts": {"basic": {"racks": racks, "strategy": "basic"},
                        "replica": {"racks": racks, "strategy": "replica"}},
            "mapping": {"A": "WS"}, "headless": True, "max_seconds": 200,
            "days": {day: [WorkloadTask(day+"/A", date.fromisoformat(day), 0, "A", {"x": count})]
                     for day, count in [("2023-01-03", 2), ("2023-01-04", 3)]}}


class BatchTests(unittest.TestCase):
    def test_all_layouts_finish_before_next_date_is_submitted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shared = shared_fixture(root)
            shared['layouts'].update(third=shared['layouts']['basic'], fourth=shared['layouts']['basic'])
            submitted = []
            class CollectedFuture(Future):
                collected = False
                def result(self, *args, **kwargs):
                    self.collected = True
                    return super().result(*args, **kwargs)
            test = self
            class Pool:
                def __init__(self, **kwargs):
                    pass
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    pass
                def submit(self, worker, name, day, output):
                    test.assertTrue(all(f.collected for old_day, _, f in submitted if old_day != day))
                    future = CollectedFuture()
                    future.set_result({'layout': name, 'date': day, 'status': 'completed',
                                       'success_flag': True, 'completed_lines': 1})
                    submitted.append((day, name, future))
                    return future
            from tqdm import tqdm
            with patch('current_heat_simulation.batch.ProcessPoolExecutor', Pool), \
                 patch('current_heat_simulation.batch.tqdm', wraps=tqdm) as progress:
                results = execute_batch(shared, root/'output', 4)
            self.assertEqual([call.kwargs['total'] for call in progress.call_args_list], [2]*4 + [3]*4)
            self.assertEqual([call.kwargs['initial'] for call in progress.call_args_list], [0]*8)
            self.assertTrue(all(call.kwargs['desc'].startswith(day)
                                for call, day in zip(progress.call_args_list, ['2023-01-03']*4 + ['2023-01-04']*4)))
            self.assertEqual(len(results), 8)
            self.assertEqual([day for day, _, _ in submitted], ['2023-01-03']*4 + ['2023-01-04']*4)
            calls = []
            def run_one(shared, name, day, output):
                calls.append(day)
                return {'layout': name, 'date': day, 'status': 'completed', 'success_flag': True}
            with patch('current_heat_simulation.batch.run_day', side_effect=run_one):
                execute_batch(shared, root/'sequential', 1)
            self.assertEqual(calls, ['2023-01-03']*4 + ['2023-01-04']*4)

    def test_live_diagnostics_flush_cadence_blockers_and_interruption(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shared = shared_fixture(root)
            system = WarehouseSystem(shared["config"], small_map(root),
                shared["days"]["2023-01-03"], shared["layouts"]["basic"]["racks"],
                shared["mapping"], shared["amr"])
            robot = system.simulator.robots[0]
            robot.active_window_path, robot.path_index = ["G0_0", "G1_0"], 1
            obstacle = replace(robot, name="obstacle", current_vertex="G1_0", x=1,
                               active_window_path=[], path_index=0)
            system.simulator.robots.append(obstacle)
            system.simulator.motion_cfg["enforce_collision_safety"] = True
            self.assertNotIn(robot.name, system.simulator._compute_translation_permissions())
            self.assertEqual(system.simulator.safety_blockers[robot.name]["robots"], ["obstacle"])
            system.simulator.robots.pop()
            path = root/"live.jsonl"
            with patch("current_heat_simulation.diagnostics.perf_counter", return_value=0) as clock:
                log = DiagnosticLog(path)
                try:
                    log.write(system, "started")
                    clock.return_value = 9
                    log.write(system)
                    self.assertEqual(len(path.read_text().splitlines()), 1)
                    clock.return_value = 10
                    system.sim_time_sec = 30
                    system.metrics.robots[robot.name]["safety_blocking_seconds"] = 30
                    system.allocator.mutex_passage.add_wait_for_robot(robot.name, {"obstacle"})
                    log.write(system)
                    row = json.loads(path.read_text().splitlines()[-1])
                    self.assertEqual(row["sim_seconds_per_wall_second"], 3)
                    self.assertEqual(row["robots"][0]["waiting_for"], ["obstacle"])
                    self.assertEqual(row["robots"][0]["activity_seconds_since_snapshot"]["safety_blocking"], 30)
                    self.assertEqual(row["robots"][0]["travel_m_since_snapshot"], 0)
                    self.assertEqual(row["robots"][0]["last_substep_safety_block"]["reason"], "occupied_target")
                finally:
                    log.close()
            with patch.object(WarehouseSystem, "step", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    run_day(shared, "basic", "2023-01-03", root/"output")
            rows = [json.loads(line) for line in (root/"output/basic/2023-01-03.diagnostics.jsonl").read_text().splitlines()]
            self.assertEqual([row["event"] for row in rows], ["started", "finished"])
            self.assertEqual(rows[-1]["reason"], "interrupted")

    def test_parallel_replica_resume_and_accounting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shared = shared_fixture(root)
            sequential = execute_batch(shared, root/"sequential", 1)
            parallel = execute_batch(shared, root/"parallel", 2)
            for a, b in zip(sequential, parallel):
                self.assertTrue(a["success_flag"])
                self.assertEqual({k:v for k,v in a.items() if k not in TIMING},
                                 {k:v for k,v in b.items() if k not in TIMING})
                self.assertAlmostEqual(a["travel_distance_m"], 20, places=6)
                self.assertAlmostEqual(a["lines_per_completed_rack_trip"], a["source_lines"])
                self.assertAlmostEqual(a["loaded_distance_m"], 20, places=6)
                day_dir = root/"parallel"/a["layout"]/a["date"]
                job = csv_rows(day_dir/"rack_jobs.csv")[0]
                station = csv_rows(day_dir/"workstations.csv")[0]
                service_interval = float(job["to_ingestor_exit_time_s"])-float(job["dropoff_wait_time_s"])
                self.assertAlmostEqual(float(station["service_seconds"]), service_interval, places=7)
                self.assertAlmostEqual(service_interval, .1, places=7)
                self.assertAlmostEqual(float(job["idle_time_s"]), float(job["completion_time_s"]), places=7)
                self.assertAlmostEqual(sum(a[c+"_seconds"] for c in TIME_CATEGORIES), a["sim_duration_s"], places=7)
            for i in range(2):
                self.assertEqual({k:v for k,v in sequential[i].items() if k not in TIMING},
                                 {k:v for k,v in sequential[i+2].items() if k not in TIMING})
            checkpoint = root/"parallel/basic/2023-01-03/checkpoint.json"
            before = checkpoint.stat().st_mtime_ns
            execute_batch(shared, root/"parallel", 2, resume=True)
            self.assertEqual(checkpoint.stat().st_mtime_ns, before)
            timing = json.loads((root/"parallel/batch_timing.json").read_text())
            self.assertEqual(timing["scheduled_runs"], 0)
            day_dir = checkpoint.parent
            key = fingerprint({"batch": "fixture", "layout": "basic", "date": "2023-01-03"})
            self.assertIsNotNone(read_checkpoint(day_dir, key))
            self.assertIsNone(read_checkpoint(day_dir, "different"))
            (day_dir/"robots.csv").unlink()
            self.assertIsNone(read_checkpoint(day_dir, key))
            execute_batch(shared, root/"parallel", 1, resume=True)
            self.assertEqual(json.loads((root/"parallel/batch_timing.json").read_text())["scheduled_runs"], 1)
            (day_dir/"checkpoint.json").write_text("{interrupted")
            self.assertIsNone(read_checkpoint(day_dir, key))
            generate_report(root/"parallel", parallel, shared["layouts"], sorted(shared["days"]), "basic", small_map(root))
            self.assertEqual(len(list((root/"parallel/charts").glob("*.png"))), 7)
            self.assertTrue((root/"parallel/charts/completed_lines_per_rack_presentation.png").exists())
            summary = json.loads((root/"parallel/replica/summary.json").read_text())
            self.assertEqual(summary["throughput_improvement_percent"], 0)
            self.assertEqual(summary["mean_daily_completed_order_lines_per_rack_presentation"], 2.5)
            self.assertEqual(summary["min_completed_order_lines_per_rack_presentation"], 2)
            self.assertEqual(summary["max_completed_order_lines_per_rack_presentation"], 3)

    def test_incomplete_runtime_failure_and_atomic_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shared = shared_fixture(root)
            result = run_day(shared, "basic", "2023-01-03", root/"output")
            self.assertTrue(result["success_flag"])
            checkpoint = root/"output/basic/2023-01-03/checkpoint.json"
            previous = checkpoint.read_bytes()
            original_replace = Path.replace
            def fail_publication(path, target):
                if path.name == "day":
                    raise OSError("publication interrupted")
                return original_replace(path, target)
            with patch.object(Path, "replace", fail_publication):
                with self.assertRaises(OSError):
                    run_day(shared, "basic", "2023-01-03", root/"output")
            self.assertEqual(checkpoint.read_bytes(), previous)
            shared["max_seconds"] = .1
            result = run_day(shared, "basic", "2023-01-03", root/"output")
            self.assertEqual(result["status"], "incomplete")
            self.assertFalse(checkpoint.exists())
            with patch.object(WarehouseSystem, "step", side_effect=RuntimeError("test failure")):
                result = run_day(shared, "basic", "2023-01-03", root/"output")
            self.assertEqual(result["status"], "failed")
            self.assertIn("test failure", result["failure_reason"])
            self.assertFalse(checkpoint.exists())
            with patch("current_heat_simulation.batch.WarehouseSystem", side_effect=ValueError("bad input")):
                result = run_day(shared, "basic", "2023-01-04", root/"output")
            self.assertEqual(result["status"], "failed")

    def test_paired_coverage_and_no_common_day(self):
        rows = [{"layout": layout, "date": day, "status": "completed", "success_flag": True,
                 "line_throughput_per_hour": rate, "completed_lines": 100, "sim_duration_s": 360000/rate}
                for layout, day, rate in [("A", "1", 100), ("A", "2", 300), ("B", "1", 120)]]
        rows.append({"layout": "B", "date": "2", "status": "incomplete", "success_flag": False,
                     "line_throughput_per_hour": 999})
        summaries, paired, days = aggregate(rows, ["A", "B"], ["1", "2"], "A")
        self.assertEqual(days, ["1"])
        self.assertEqual(summaries[0]["mean_daily_throughput_lines_per_hour"], 100)
        self.assertAlmostEqual(summaries[1]["throughput_improvement_percent"], 20)
        self.assertEqual(summaries[1]["comparison_status"], "provisional")
        self.assertEqual(len(paired), 2)
        rows[2]["success_flag"] = False
        summaries, paired, days = aggregate(rows, ["A", "B"], ["1", "2"], "A")
        self.assertEqual(days, [])
        self.assertIsNone(summaries[0]["mean_daily_throughput_lines_per_hour"])
        self.assertEqual(summaries[0]["comparison_status"], "unavailable")

    def test_cli_defaults_dates_shared_inputs_and_resume_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            small_map(root)
            write_json(root/"amr.json", amr_config().snapshot())
            workbook = Workbook()
            sheet = workbook.active
            sheet.append(["Date", "Store ID", "Item or SKU"])
            for day in [3, 4]:
                sheet.append([datetime(2023, 1, day, 23), "A", "x"])
            workbook.save(root/"orders.xlsx")
            for name in ["map1_basic", "replica"]:
                write_json(root/(name+".slotting.json"), {"schema": "inventory_slotting_layout/v2", "building": {},
                           "strategy": "basic", "assignments": [{"sku": "x", "rack_id": "G0_0", "assignment_status": "ASSIGNED"}]})
            config = {"method": "current_heat", "map_file": "grid.json", "orders_file": "orders.xlsx",
                      "layout_file": "map1_basic.slotting.json", "amr_config": "amr.json",
                      "conflicts": {"print_conflicts": False}}
            (root/"config.yaml").write_text(yaml.safe_dump(config))
            args = ["--config", str(root/"config.yaml"), "--headless", "--workers", "1",
                    "--layout", str(root/"map1_basic.slotting.json"), "--layout", str(root/"replica.slotting.json"),
                    "--output", str(root/"results")]
            with patch("current_heat_simulation.pygame_simulator.generate_report"):
                self.assertEqual(main(args), 0)
                manifest = json.loads((root/"results/manifest.json").read_text())
                self.assertEqual(manifest["identity"]["dates"], ["2023-01-03", "2023-01-04"])
                self.assertEqual(manifest["baseline_layout"], "map1_basic")
                snapshot = json.loads((root/"results/workload_snapshot.json").read_text())
                self.assertTrue(all(t["release_seconds"] == 0 for day in snapshot["days"].values() for t in day))
                self.assertEqual(main(args+["--resume"]), 0)
                self.assertEqual(main(args+["--resume", "--max-seconds", "2"]), 2)
                with patch("current_heat_simulation.pygame_simulator.execute_batch", return_value=[]) as execute:
                    self.assertEqual(main(args[:-1]+[str(root/"selected"),
                        "--date", "2023-01-04", "--date", "2023-01-03", "--date", "2023-01-04"]), 0)
                    selected = execute.call_args.args[0]["days"]
                    self.assertEqual(sorted(selected), ["2023-01-03", "2023-01-04"])
                    self.assertEqual(sum(len(tasks) for tasks in selected.values()), 2)
                    self.assertEqual(main(args[:-1]+[str(root/"single"), "--date", "2023-01-03"]), 0)
                    self.assertEqual(list(execute.call_args.args[0]["days"]), ["2023-01-03"])
                    execute.reset_mock()
                    self.assertEqual(main(args[:-1]+[str(root/"missing"),
                        "--date", "2023-01-03", "--date", "2023-02-03"]), 2)
                    execute.assert_not_called()
                # Invalid layouts reject the entire batch before simulation.
                payload = json.loads((root/"replica.slotting.json").read_text())
                payload["assignments"][0]["sku"] = "unmapped"
                write_json(root/"replica.slotting.json", payload)
                with patch("current_heat_simulation.pygame_simulator.execute_batch") as execute:
                    self.assertEqual(main(args[:-1]+[str(root/"invalid")]), 2)
                    execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
