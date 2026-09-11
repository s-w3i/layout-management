import copy
from datetime import date
from pathlib import Path
import tempfile
import unittest

import numpy as np

from warehouse_layout.affinity import AffinityDataset, AffinityService
from warehouse_layout.attributes import AttributeDefinition, PHYSICAL_ATTRIBUTE_KEYS
from warehouse_layout.slotting import SlottingService
from warehouse_layout.slotting_repository import SlottingLayoutRepository
from warehouse_layout.slotting_strategies.zone_workload import balanced_zone_candidates


class ZoneWorkloadTests(unittest.TestCase):
    def setUp(self):
        self.building = {"coordinate_system": "cartesian_meters", "levels": {"L1": {
            "vertices": [
                [0, 0, 0, "WS", {"dropoff_ingestor": [1, "WS"]}],
                [1, 0, 0, "R1", {"pickup_dispenser": [1, "R1"]}],
                [10, 0, 0, "R2", {"pickup_dispenser": [1, "R2"]}],
            ],
            "lanes": [[0, 1, {"bidirectional": [4, True]}], [0, 2, {"bidirectional": [4, True]}]],
        }}}
        self.zones = {"R1": "Z1", "R2": "Z2"}
        self.catalog = {key: AttributeDefinition(key, key, "number", "capacity") for key in PHYSICAL_ATTRIBUTE_KEYS}
        self.attributes = {zone: {key: 1 for key in PHYSICAL_ATTRIBUTE_KEYS} for zone in ("Z1", "Z2")}
        self.skus = [dict(sku=str(i), velocity_class="A", pick_frequency=100,
                          sku_requirements={key: .1 for key in PHYSICAL_ATTRIBUTE_KEYS}) for i in range(4)]
        day = date(2023, 1, 3)
        dataset = AffinityDataset(Path("unused.xlsx"), 0, 0, "orders", np.full(4, day.toordinal()),
                                  np.arange(4), np.zeros(4, dtype=int), tuple(str(i) for i in range(4)),
                                  ("store",), 4, 0, day, day)
        self.analysis = AffinityService.analyze(dataset)

    def generate(self, strategy, **kwargs):
        options = dict(levels_per_rack=1, slots_per_level=4, zone_assignments=self.zones,
                       attribute_catalog=self.catalog, location_attributes=copy.deepcopy(self.attributes))
        options.update(kwargs)
        service = SlottingService()
        if strategy == "basic":
            return service.generate_basic(copy.deepcopy(self.building), copy.deepcopy(self.skus), **options)
        return service.generate_abc_affinity(copy.deepcopy(self.building), copy.deepcopy(self.skus),
                                             self.analysis, 1.0, **options)

    def test_both_strategies_toggle_and_persistence(self):
        for strategy in ("basic", "abc_affinity"):
            with self.subTest(strategy=strategy):
                default = self.generate(strategy)
                disabled = self.generate(strategy, zone_workload_enabled=False)
                self.assertEqual(default, disabled)
                rows, summary = self.generate(strategy, zone_workload_enabled=True)
                self.assertEqual(summary["unassigned_count"], 0)
                self.assertEqual([summary["zone_workload"][z]["demand"] for z in ("Z1", "Z2")], [200, 200])
                self.assertEqual(max(v["normalized_demand"] for v in disabled[1]["zone_workload"].values()), 100)
                self.assertEqual(len({r["storage_location_address"] for r in rows}), 4)
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "layout.json"
                    repository = SlottingLayoutRepository()
                    repository.save(rows, self.building, summary, path, strategy=strategy,
                                    handling_unit_type="AMR shelf", levels_per_rack=1, slots_per_level=4,
                                    zone_assignments=self.zones, attribute_catalog=self.catalog,
                                    location_attributes=self.attributes)
                    restored = repository.load(path)["summary"]
                    self.assertTrue(restored["zone_workload_enabled"])
                    self.assertEqual(restored["maximum_same_sku_slots_per_rack"], 4)

    def test_capacity_normalization_and_ties(self):
        candidates = [((), 0, {"zone_id": "small"}), ((), 1, {"zone_id": "large"})]
        self.assertEqual(balanced_zone_candidates(candidates, {}, {"small": 2, "large": 4}, 10), candidates[1:])
        self.assertEqual(balanced_zone_candidates(candidates, {}, {"small": 2, "large": 4}, 0), candidates)

    def test_replica_demand_is_not_double_counted(self):
        self.skus = [dict(self.skus[0], pick_frequency=90, required_slots=2, total_required_ea=3, slots_per_unit=1)]
        rows, summary = self.generate("basic", zone_workload_enabled=True)
        self.assertEqual(len(rows), 2)
        self.assertEqual(sorted(v["demand"] for v in summary["zone_workload"].values()), [0, 90])
        self.skus[0]["total_required_ea"] = 1
        _, summary = self.generate("basic", zone_workload_enabled=True)
        self.assertEqual(sorted(v["demand"] for v in summary["zone_workload"].values()), [0, 90])

    def test_same_sku_fills_rack_contiguously_until_limit(self):
        self.skus = [
            dict(self.skus[0], required_slots=5, total_required_ea=5, slots_per_unit=1),
            dict(self.skus[1], required_slots=1, total_required_ea=1, slots_per_unit=1),
        ]
        for strategy in ("basic", "abc_affinity"):
            with self.subTest(strategy=strategy):
                rows, summary = self.generate(
                    strategy, zone_workload_enabled=True,
                    maximum_same_sku_slots_per_rack=3,
                )
                rack_slots = {}
                for row in rows:
                    if row["sku"] != "0":
                        continue
                    rack_slots.setdefault(row["rack_id"], []).append(row["storage_slot"])
                self.assertEqual(sorted(map(len, rack_slots.values())), [2, 3])
                self.assertIn([1, 2, 3], [sorted(value) for value in rack_slots.values()])
                self.assertEqual(summary["maximum_same_sku_slots_per_rack"], 3)

    def test_same_sku_limit_validation_and_default(self):
        for invalid in (0, -1, 1.5, True, "2"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.generate("basic", maximum_same_sku_slots_per_rack=invalid)
        _, summary = self.generate("basic")
        self.assertEqual(summary["maximum_same_sku_slots_per_rack"], 4)

    def test_same_sku_moves_to_another_level_when_level_is_full(self):
        self.skus = [dict(
            self.skus[0], required_slots=3, total_required_ea=3,
            slots_per_unit=1,
        )]
        rows, _ = self.generate(
            "basic", levels_per_rack=2, slots_per_level=2,
            maximum_same_sku_slots_per_rack=3,
        )
        self.assertEqual(len({row["rack_id"] for row in rows}), 1)
        level_slots = {}
        for row in rows:
            level_slots.setdefault(row["storage_level"], []).append(row["storage_slot"])
        self.assertEqual(sorted(map(len, level_slots.values())), [1, 2])
        self.assertIn([1, 2], [sorted(value) for value in level_slots.values()])

    def test_oversize_footprints_count_physical_cells_toward_limit(self):
        self.catalog["oversize_capable"] = AttributeDefinition(
            "oversize_capable", "Oversize capable", "boolean"
        )
        for values in self.attributes.values():
            values["oversize_capable"] = True
        requirements = {key: 0.5 for key in PHYSICAL_ATTRIBUTE_KEYS}
        requirements["max_item_width"] = 3.0
        self.skus = [dict(
            self.skus[0], required_slots=6, total_required_ea=2,
            slots_per_unit=3, sku_requirements=requirements,
        )]
        rows, _ = self.generate(
            "basic", maximum_same_sku_slots_per_rack=5,
        )
        self.assertEqual([row["occupied_slot_count"] for row in rows], [3, 3])
        self.assertEqual(len({row["rack_id"] for row in rows}), 2)
        self.assertTrue(all(row["occupied_horizontal_slot_span"] == 3 for row in rows))

    def test_zone_preference_cannot_override_compatibility(self):
        self.catalog["chilled"] = AttributeDefinition("chilled", "Chilled", "boolean")
        self.attributes["Z1"]["chilled"] = False
        self.attributes["Z2"]["chilled"] = True
        for row in self.skus:
            row["sku_requirements"]["chilled"] = True
        for strategy in ("basic", "abc_affinity"):
            rows, summary = self.generate(strategy, zone_workload_enabled=True)
            self.assertEqual(summary["unassigned_count"], 0)
            self.assertEqual({row["zone_id"] for row in rows}, {"Z2"})


if __name__ == "__main__":
    unittest.main()
