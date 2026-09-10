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
                    self.assertTrue(repository.load(path)["summary"]["zone_workload_enabled"])

    def test_capacity_normalization_and_ties(self):
        candidates = [((), 0, {"zone_id": "small"}), ((), 1, {"zone_id": "large"})]
        self.assertEqual(balanced_zone_candidates(candidates, {}, {"small": 2, "large": 4}, 10), candidates[1:])
        self.assertEqual(balanced_zone_candidates(candidates, {}, {"small": 2, "large": 4}, 0), candidates)

    def test_replica_demand_is_not_double_counted(self):
        self.skus = [dict(self.skus[0], pick_frequency=90, required_slots=2, total_required_ea=3, slots_per_unit=1)]
        rows, summary = self.generate("basic", zone_workload_enabled=True)
        self.assertEqual(len(rows), 2)
        self.assertEqual(sorted(v["demand"] for v in summary["zone_workload"].values()), [30, 60])
        self.skus[0]["total_required_ea"] = 1
        _, summary = self.generate("basic", zone_workload_enabled=True)
        self.assertEqual(sorted(v["demand"] for v in summary["zone_workload"].values()), [0, 90])

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
