"""Run with python3 -m unittest discover -s tests -p 'test_ctbsa*.py'."""

import unittest
from collections import Counter
from dataclasses import replace
from types import SimpleNamespace

import numpy as np

from warehouse_layout.ctbsa import CtbsaParameters, CtbsaPlacementPlanner, cluster_capacities


class MinimumRackTests(unittest.TestCase):
    def test_capacity_boundaries(self):
        for loads, expected in [(0, []), (1, [2]), (4, [2, 2]), (5, [2, 2, 2])]:
            self.assertEqual(cluster_capacities(loads, 5, 2, minimize_rack_count=True), expected)
        self.assertEqual(cluster_capacities(5, 5, 2), [2] * 5)
        with self.assertRaises(ValueError):
            cluster_capacities(11, 5, 2, minimize_rack_count=True)
        with self.assertRaises(ValueError):
            CtbsaParameters(minimize_rack_count="false").validate()

    def test_planner_modes_and_zone_balance(self):
        # Five ambient loads, two chilled loads, and one fixed exception rack.
        racks = [dict(rack_id=f"A{i}", distance_m=i + 1, zone_id=f"ambient{i % 2}") for i in range(5)]
        racks += [dict(rack_id=f"C{i}", distance_m=i + 1, zone_id=f"cold{i % 2}") for i in range(3)]
        racks += [dict(rack_id="fixed", distance_m=0, zone_id="ambient0")]
        rows = [dict(sku=str(i), inventory_load_id=str(i), rack_id=rack_id,
                     assignment_status="ASSIGNED", occupied_slot_count=1,
                     handling_unit_type="AMR shelf", sku_requirements={"chilled": rack_id.startswith("C")})
                for i, rack_id in enumerate(["A0", "A0", "A1", "A1", "A2", "C0", "C0", "fixed"])]
        rows[-1]["occupied_slot_count"] = 2
        attributes = SimpleNamespace(
            physical_profile=lambda _: {"data_status": "COMPLETE", "storage_class": "STANDARD"},
            configured_zone_attribute_keys=lambda _: {"chilled"},
            normalize_catalog=lambda _: {"chilled": SimpleNamespace(value_type="boolean")},
            effective_attributes=lambda zone, _: ({"chilled": zone.startswith("cold")}, {}),
        )
        slotting = SimpleNamespace(
            attributes=attributes,
            rack_distances=lambda _: ("L1", racks, [], []),
            apply_zone_local_aisles=lambda *args: None,
        )
        analysis = SimpleNamespace(
            dataset=SimpleNamespace(skus=tuple(str(i) for i in range(8))),
            sku_store_day_totals=np.array([10, 8, 6, 4, 2, 5, 3, 1]),
            shared_store_days=np.ones((8, 8), dtype=np.int64),
        )
        planner = CtbsaPlacementPlanner(slotting)
        params = CtbsaParameters(population_size=6, generations=3)
        for zone_balance in (False, True):
            def build(parameters):
                return planner.build(rows, {}, analysis, levels_per_rack=1, slots_per_level=2,
                                     parameters=parameters, zone_workload_enabled=zone_balance)

            original = build(params)
            self.assertEqual(original, build(replace(params, minimize_rack_count=False)))
            self.assertEqual(len(original.cluster_rows), 8)
            compact = build(replace(params, minimize_rack_count=True))
            self.assertEqual(len(compact.cluster_rows), 4)
            self.assertEqual(compact.parameters["optimized_rack_count"], 4)
            self.assertTrue(compact.parameters["minimize_rack_count"])
            self.assertEqual(compact.target_racks["7"], "fixed")
            self.assertEqual(set(compact.target_racks), {str(i) for i in range(8)})
            counts = Counter(compact.target_racks[i] for i in compact.optimized_loads)
            self.assertEqual(len(counts), 4)
            self.assertLessEqual(max(counts.values()), 2)
            self.assertTrue(all(compact.target_racks[str(i)].startswith("A") for i in range(5)))
            self.assertTrue(all(compact.target_racks[str(i)].startswith("C") for i in (5, 6)))
            # The same demand objective is retained, with no lost/duplicated loads.
            self.assertEqual(sum(row["cluster_demand"] for row in compact.cluster_rows), 38)

    def test_same_sku_target_rack_limit(self):
        racks = [dict(rack_id=f"R{i}", distance_m=i, zone_id="ambient") for i in range(3)]
        rows = [
            dict(sku="A", inventory_load_id=f"A{i}", rack_id=f"R{i // 2}",
                 assignment_status="ASSIGNED", occupied_slot_count=1,
                 handling_unit_type="AMR shelf", sku_requirements={})
            for i in range(5)
        ]
        attributes = SimpleNamespace(
            physical_profile=lambda _: {"data_status": "COMPLETE", "storage_class": "STANDARD"},
            configured_zone_attribute_keys=lambda _: set(),
            normalize_catalog=lambda _: {},
            effective_attributes=lambda *_: ({}, {}),
        )
        slotting = SimpleNamespace(
            attributes=attributes,
            rack_distances=lambda _: ("L1", racks, [], []),
            apply_zone_local_aisles=lambda *args: None,
        )
        analysis = SimpleNamespace(
            dataset=SimpleNamespace(skus=("A",)),
            sku_store_day_totals=np.array([10]),
            shared_store_days=np.ones((1, 1), dtype=np.int64),
        )
        plan = CtbsaPlacementPlanner(slotting).build(
            rows, {}, analysis, levels_per_rack=1, slots_per_level=2,
            maximum_same_sku_slots_per_rack=2,
            parameters=CtbsaParameters(
                population_size=6, generations=3, minimize_rack_count=True,
            ),
        )
        counts = Counter(plan.target_racks[value] for value in plan.optimized_loads)
        self.assertEqual(sorted(counts.values()), [1, 2, 2])
        self.assertEqual(plan.parameters["maximum_same_sku_slots_per_rack"], 2)


if __name__ == "__main__":
    unittest.main()
