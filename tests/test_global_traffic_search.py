"""Automatic global traffic parameter-search tests."""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from warehouse_layout.global_traffic_search import (
    GlobalTrafficParameterSearch,
    GlobalTrafficSearchScenario,
)


def fake_result(
    *,
    peak: float,
    p95: float,
    neighbourhood: float,
    top_five: float,
    zone: float,
    travel: float,
    relocations: int,
):
    controllable = {
        "peak_load": peak,
        "p95_load": p95,
        "top_5_percent_mean": top_five,
    }
    return SimpleNamespace(
        balance_metrics={
            "controllable_resources_after": controllable,
            "neighbourhood_after": {
                "peak_normalized_load": neighbourhood
            },
            "zone_after": {"peak_normalized_load": zone},
        },
        after=SimpleNamespace(metrics={"expected_travel": travel}),
        relocations=[{} for _ in range(relocations)],
        solver={"status": "FEASIBLE", "relative_gap": 0.1},
        output_payload={
            "schema": "inventory_slotting_layout/v2",
            "global_traffic_configuration": {},
            "operation_log": [],
        },
    )


class GlobalTrafficParameterSearchTests(unittest.TestCase):
    def setUp(self):
        self.search = GlobalTrafficParameterSearch()
        self.scenarios = (
            GlobalTrafficSearchScenario("A", 0.0, 0.5),
            GlobalTrafficSearchScenario("B", 0.05, 0.75),
            GlobalTrafficSearchScenario("C", 0.10, 1.0),
        )

    def test_search_refines_best_valid_scenario_and_ignores_failed_run(self):
        calls = []

        def optimize(scenario, seconds, _progress):
            calls.append((scenario.scenario_id, seconds))
            if scenario.scenario_id == "C":
                raise RuntimeError("no feasible incumbent")
            if scenario.scenario_id == "B":
                return fake_result(
                    peak=0.8 if seconds == 20 else 0.9,
                    p95=0.7,
                    neighbourhood=0.8,
                    top_five=0.75,
                    zone=0.9,
                    travel=110,
                    relocations=4,
                )
            return fake_result(
                peak=1.0,
                p95=0.6,
                neighbourhood=0.7,
                top_five=0.65,
                zone=0.8,
                travel=90,
                relocations=2,
            )

        result = self.search.search(
            optimize,
            scenarios=self.scenarios,
            screening_seconds=10,
            final_seconds=20,
            finalist_count=1,
        )

        self.assertEqual(result.best_trial.scenario.scenario_id, "B")
        self.assertEqual(result.best_trial.phase, "final")
        self.assertEqual(len(result.trials), 4)
        self.assertEqual(sum(trial.accepted for trial in result.trials), 3)
        self.assertEqual(calls[-1], ("B", 20))
        metadata = result.best_result.output_payload[
            "global_traffic_configuration"
        ]["parameter_search"]
        self.assertEqual(metadata["selected_scenario"], "B")
        self.assertEqual(metadata["failed_trial_count"], 1)

    def test_ranking_is_congestion_first_not_travel_first(self):
        lower_peak = fake_result(
            peak=0.9,
            p95=0.8,
            neighbourhood=0.8,
            top_five=0.8,
            zone=0.8,
            travel=200,
            relocations=10,
        )
        shorter_travel = fake_result(
            peak=1.0,
            p95=0.1,
            neighbourhood=0.1,
            top_five=0.1,
            zone=0.1,
            travel=50,
            relocations=1,
        )

        self.assertLess(
            self.search.ranking_key(lower_peak),
            self.search.ranking_key(shorter_travel),
        )

    def test_save_writes_best_layout_and_comparison_reports(self):
        result = self.search.search(
            lambda scenario, _seconds, _progress: fake_result(
                peak=1.0 + scenario.maximum_travel_increase,
                p95=1,
                neighbourhood=1,
                top_five=1,
                zone=1,
                travel=100,
                relocations=1,
            ),
            scenarios=self.scenarios[:2],
            screening_seconds=1,
            final_seconds=2,
            finalist_count=1,
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "best.slotting.json"
            layout, comparison, summary = self.search.save(
                result, output
            )
            payload = json.loads(layout.read_text(encoding="utf-8"))
            with comparison.open(newline="", encoding="utf-8") as stream:
                rows = list(csv.DictReader(stream))
            report = json.loads(summary.read_text(encoding="utf-8"))

        self.assertEqual(len(rows), 3)
        self.assertEqual(sum(row["selected"] == "True" for row in rows), 1)
        self.assertEqual(report["best_layout"], str(output.resolve()))
        self.assertEqual(
            payload["global_traffic_configuration"][
                "parameter_search"
            ]["status"],
            "COMPLETED",
        )

    def test_save_can_write_a_user_selected_successful_trial(self):
        result = self.search.search(
            lambda scenario, _seconds, _progress: fake_result(
                peak=1.0 + scenario.maximum_travel_increase,
                p95=1,
                neighbourhood=1,
                top_five=1,
                zone=1,
                travel=100,
                relocations=1,
            ),
            scenarios=self.scenarios[:2],
            screening_seconds=1,
            final_seconds=2,
            finalist_count=1,
        )
        selected = next(
            trial
            for trial in result.trials
            if trial.scenario.scenario_id == "B"
        )
        selected.result.output_payload["selected_payload"] = "B"
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "selected.slotting.json"
            layout, comparison, summary = self.search.save(
                result, output, selected
            )
            payload = json.loads(layout.read_text(encoding="utf-8"))
            with comparison.open(newline="", encoding="utf-8") as stream:
                rows = list(csv.DictReader(stream))
            report = json.loads(summary.read_text(encoding="utf-8"))

        metadata = payload["global_traffic_configuration"][
            "parameter_search"
        ]
        self.assertEqual(payload["selected_payload"], "B")
        self.assertEqual(metadata["selected_scenario"], "B")
        self.assertTrue(metadata["user_selected_trial"])
        self.assertEqual(report["selected_scenario"], "B")
        selected_rows = [row for row in rows if row["selected"] == "True"]
        self.assertEqual(len(selected_rows), 1)
        self.assertEqual(selected_rows[0]["scenario"], "B")

    def test_default_portfolio_contains_nine_combinations(self):
        scenarios = self.search.default_scenarios()
        self.assertEqual(len(scenarios), 9)
        self.assertEqual(
            {
                scenario.maximum_travel_increase
                for scenario in scenarios
            },
            {0.0, 0.05, 0.10},
        )
        self.assertEqual(
            {
                scenario.maximum_relocation_fraction
                for scenario in scenarios
            },
            {0.5, 0.75, 1.0},
        )


if __name__ == "__main__":
    unittest.main()
