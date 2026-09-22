"""Integration coverage for Direct C&TBSA on ASRS handling units."""

import copy
from datetime import date
from pathlib import Path
import unittest

import numpy as np

from warehouse_layout.affinity import AffinityDataset, AffinityService
from warehouse_layout.attributes import AttributeDefinition, PHYSICAL_ATTRIBUTE_KEYS
from warehouse_layout.ctbsa import CtbsaParameters
from warehouse_layout.traffic import TrafficAwareSlottingService


class AsrsCtbsaTests(unittest.TestCase):
    def test_tote_and_pallet_pipelines_preserve_slot_assignments(self):
        building = {
            "coordinate_system": "cartesian_meters",
            "levels": {"L1": {
                "vertices": [
                    [0, 0, 0, "WS", {"dropoff_ingestor": [1, "WS"]}],
                    [1, 0, 0, "R1", {"pickup_dispenser": [1, "R1"]}],
                    [10, 0, 0, "R2", {"pickup_dispenser": [1, "R2"]}],
                ],
                "lanes": [
                    [0, 1, {"bidirectional": [4, True]}],
                    [0, 2, {"bidirectional": [4, True]}],
                ],
            }},
        }
        zones = {"R1": "Z1", "R2": "Z2"}
        catalog = {
            key: AttributeDefinition(key, key, "number", "capacity")
            for key in PHYSICAL_ATTRIBUTE_KEYS
        }
        location_attributes = {
            zone: {key: 1 for key in PHYSICAL_ATTRIBUTE_KEYS}
            for zone in ("Z1", "Z2")
        }
        requirements = {key: 0.1 for key in PHYSICAL_ATTRIBUTE_KEYS}
        sku_rows = [
            {
                "sku": "A", "velocity_class": "A", "pick_frequency": 100,
                "required_slots": 2, "total_required_ea": 2,
                "total_slotted_ea": 2, "slots_per_unit": 1,
                "sku_requirements": requirements,
            },
            {
                "sku": "B", "velocity_class": "A", "pick_frequency": 50,
                "sku_requirements": requirements,
            },
            {
                "sku": "C", "velocity_class": "A", "pick_frequency": 25,
                "sku_requirements": requirements,
            },
        ]
        day = date(2023, 1, 3)
        dataset = AffinityDataset(
            Path("unused.xlsx"), 0, 0, "orders",
            np.full(3, day.toordinal()), np.arange(3),
            np.zeros(3, dtype=int), ("A", "B", "C"), ("store",),
            3, 0, day, day,
        )
        service = TrafficAwareSlottingService()
        network = service.network_from_rmf(building)
        parameters = CtbsaParameters(
            population_size=4, generations=2, selected_solution=1,
            minimize_rack_count=True,
        )

        for handling_unit_type in ("Tote", "Pallet"):
            with self.subTest(handling_unit_type=handling_unit_type):
                result = service.run_full_pipeline(
                    copy.deepcopy(building), copy.deepcopy(sku_rows), dataset,
                    network, initial_strategy="physical_feasibility",
                    levels_per_rack=1, slots_per_level=2,
                    handling_unit_type=handling_unit_type,
                    zone_assignments=zones, attribute_catalog=catalog,
                    location_attributes=copy.deepcopy(location_attributes),
                    ctbsa_parameters=parameters, zone_workload_enabled=True,
                    maximum_same_sku_slots_per_rack=1,
                )
                payload = result.output_payload
                rows = payload["assignments"]
                self.assertEqual(len(rows), 4)
                self.assertEqual(
                    {row["handling_unit_type"] for row in rows},
                    {handling_unit_type},
                )
                addresses = [row["static_address"] for row in rows]
                self.assertEqual(len(addresses), len(set(addresses)))
                a_racks = {row["rack_id"] for row in rows if row["sku"] == "A"}
                self.assertEqual(len(a_racks), 2)
                configuration = payload["traffic_configuration"]
                self.assertEqual(configuration["cluster_unit"], handling_unit_type)
                self.assertTrue(configuration["minimize_rack_count"])
                self.assertTrue(configuration["zone_workload_enabled"])


if __name__ == "__main__":
    unittest.main()
