"""Paper-replication C&TBSA model tests."""

from __future__ import annotations

import unittest

import numpy as np

from warehouse_layout.ctbsa import (
    CtbsaCancelledError,
    CtbsaNsga2,
    CtbsaParameters,
)


class CtbsaNsga2Tests(unittest.TestCase):
    def setUp(self):
        # A/B and C/D are strongly correlated. The demand objective conflicts
        # with putting A/B together, producing the intended Pareto trade-off.
        self.demands = np.array([5, 5, 1, 1], dtype=np.int64)
        self.correlations = np.array([
            [0, 10, 0, 0],
            [10, 0, 0, 0],
            [0, 0, 0, 8],
            [0, 0, 8, 0],
        ], dtype=np.int64)
        self.model = CtbsaNsga2(
            self.demands, self.correlations, [2, 2]
        )
        self.parameters = CtbsaParameters(
            population_size=20,
            generations=30,
            random_seed=1,
        )

    def test_objectives_match_paper_equations(self):
        correlation, maximum_demand = self.model.evaluate(
            np.array([0, 1, 2, 3], dtype=np.int32)
        )
        self.assertEqual(correlation, 18)
        self.assertEqual(maximum_demand, 10)

        correlation, maximum_demand = self.model.evaluate(
            np.array([0, 2, 1, 3], dtype=np.int32)
        )
        self.assertEqual(correlation, 0)
        self.assertEqual(maximum_demand, 6)

    def test_nsga2_is_deterministic_and_returns_tradeoff(self):
        first = self.model.run(self.parameters)
        second = self.model.run(self.parameters)
        self.assertEqual(
            [
                (row["correlation"], row["maximum_cluster_demand"])
                for row in first.pareto
            ],
            [
                (row["correlation"], row["maximum_cluster_demand"])
                for row in second.pareto
            ],
        )
        self.assertIn((18, 10), [
            (row["correlation"], row["maximum_cluster_demand"])
            for row in first.pareto
        ])
        self.assertIn((0, 6), [
            (row["correlation"], row["maximum_cluster_demand"])
            for row in first.pareto
        ])

    def test_pmx_and_mutation_preserve_permutations(self):
        rng = np.random.default_rng(4)
        first = np.arange(20, dtype=np.int32)
        second = first[::-1].copy()
        children = self.model._pmx(first, second, rng)
        for child in children:
            self.assertEqual(sorted(child.tolist()), list(range(20)))
        mutation = self.model._mutate(first, rng)
        self.assertEqual(sorted(mutation.tolist()), list(range(20)))

    def test_dummy_empty_locations_do_not_change_objectives(self):
        model = CtbsaNsga2(
            self.demands, self.correlations, [3, 3], real_item_count=4
        )
        chromosome = np.array([0, 1, 4, 2, 3, 5], dtype=np.int32)
        self.assertEqual(model.evaluate(chromosome), (18, 10))

    def test_cancellation_is_checked_each_generation(self):
        with self.assertRaises(CtbsaCancelledError):
            self.model.run(self.parameters, cancelled=lambda: True)

    def test_three_objective_evaluation_normalizes_zone_capacity(self):
        model = CtbsaNsga2(
            np.array([8, 7, 2, 1], dtype=np.int64),
            np.zeros((4, 4), dtype=np.int64),
            [1, 1, 1, 1],
            cluster_zone_ids=["Z1", "Z1", "Z2", "Z2"],
            zone_capacities={"Z1": 2, "Z2": 2},
        )
        concentrated = model.evaluate(np.array([0, 1, 2, 3], dtype=np.int32))
        distributed = model.evaluate(np.array([0, 2, 1, 3], dtype=np.int32))
        self.assertEqual(concentrated, (0, 8, 7.5))
        self.assertEqual(distributed, (0, 8, 5.0))

    def test_three_objective_auto_and_manual_selection_are_deterministic(self):
        model = CtbsaNsga2(
            self.demands,
            self.correlations,
            [1, 1, 1, 1],
            cluster_zone_ids=["Z1", "Z1", "Z2", "Z2"],
            zone_capacities={"Z1": 2, "Z2": 2},
        )
        automatic = model.run(self.parameters)
        repeated = model.run(self.parameters)
        self.assertEqual(automatic.selection["mode"], "automatic_knee")
        self.assertEqual(
            automatic.selected_chromosome.tolist(),
            repeated.selected_chromosome.tolist(),
        )
        self.assertTrue(all(
            "maximum_zone_demand" in row for row in automatic.pareto
        ))
        manual = model.run(CtbsaParameters(
            population_size=20, generations=30, random_seed=1,
            extended_selected_solution=1,
        ))
        self.assertEqual(manual.selection["mode"], "manual_representative")
        self.assertEqual(manual.selection["selected_representative"], 1)

    def test_three_objective_rejects_zero_zone_capacity(self):
        with self.assertRaisesRegex(ValueError, "positive compatible capacity"):
            CtbsaNsga2(
                self.demands, self.correlations, [2, 2],
                cluster_zone_ids=["Z1", "Z2"],
                zone_capacities={"Z1": 2, "Z2": 0},
            )


if __name__ == "__main__":
    unittest.main()
