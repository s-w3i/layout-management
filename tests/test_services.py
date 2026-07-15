"""Regression tests for the object-oriented application services."""

from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from warehouse_layout import (
    GridProject,
    GridSpec,
    InventoryService,
    Marker,
    RmfMapService,
    SlottingLayoutRepository,
    SlottingService,
)


class WarehouseServiceTests(unittest.TestCase):
    def setUp(self):
        self.rmf = RmfMapService()
        self.slotting = SlottingService(self.rmf)
        self.layouts = SlottingLayoutRepository()
        self.inventory = InventoryService(self.slotting)
        self.project = GridProject(
            GridSpec(3, 2, 1, "test_warehouse", "L1"),
            {
                (0, 0): Marker("rack", "RACK_LEFT"),
                (2, 0): Marker("rack", "RACK_RIGHT"),
                (3, 2): Marker("workstation", "WS_01"),
            },
        )
        self.building = self.project.to_building_dict()
        self.skus = [
            {
                "sku": f"SKU_{index:02d}",
                "pick_frequency": 100 - index,
                "velocity_class": "A" if index < 3 else "B",
                "total_quantity_ea": 1,
                "active_days": 1,
            }
            for index in range(6)
        ]

    def test_project_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "warehouse.grid.json"
            self.rmf.save_project(self.project, path)
            loaded = self.rmf.load_project(path)
        self.assertEqual(loaded.to_project_dict(), self.project.to_project_dict())

    def test_aisles_restart_for_each_zone(self):
        _level, racks, _workstations, _unreachable = self.slotting.rack_distances(
            self.building
        )
        zones = {
            "G0_0": "Z01",
            "G2_0": "Z02",
        }
        self.slotting.apply_zone_local_aisles(self.building, racks, zones)
        self.assertEqual({rack["aisle_id"] for rack in racks}, {"A01"})
        self.assertEqual({rack["zone_id"] for rack in racks}, {"Z01", "Z02"})

    def test_dynamic_address_layer_for_each_handling_unit(self):
        expected_layers = {
            "AMR shelf": "bay",
            "Tote": "slot",
            "Pallet": "slot",
        }
        for handling_unit, expected_layer in expected_layers.items():
            with self.subTest(handling_unit=handling_unit):
                rows, _summary = self.slotting.generate_basic(
                    copy.deepcopy(self.building),
                    copy.deepcopy(self.skus),
                    1,
                    3,
                    handling_unit,
                )
                self.assertTrue(rows)
                self.assertTrue(
                    all(row["dynamic_address_level"] == expected_layer for row in rows)
                )

    def test_sku_slot_swap_exchanges_locations(self):
        rows, _summary = self.slotting.generate_basic(
            copy.deepcopy(self.building), copy.deepcopy(self.skus), 1, 3, "AMR shelf"
        )
        first_address = rows[0]["static_address"]
        second_address = rows[4]["static_address"]
        self.inventory.swap_sku_slots(rows, rows[0]["sku"], rows[4]["sku"])
        self.assertEqual(rows[0]["static_address"], second_address)
        self.assertEqual(rows[4]["static_address"], first_address)

    def test_whole_shelf_swap_preserves_shelf_ids(self):
        rows, _summary = self.slotting.generate_basic(
            copy.deepcopy(self.building), copy.deepcopy(self.skus), 1, 3, "AMR shelf"
        )
        first_unit = rows[0]["handling_unit_id"]
        second_unit = rows[4]["handling_unit_id"]
        first_rack = rows[0]["rack_id"]
        second_rack = rows[4]["rack_id"]
        self.inventory.swap_whole_shelf_units(rows, first_unit, second_unit)
        self.assertEqual(
            {row["rack_id"] for row in rows if row["handling_unit_id"] == first_unit},
            {second_rack},
        )
        self.assertEqual(
            {row["rack_id"] for row in rows if row["handling_unit_id"] == second_unit},
            {first_rack},
        )

    def test_whole_shelf_swap_rejects_slot_level_units(self):
        rows, _summary = self.slotting.generate_basic(
            copy.deepcopy(self.building), copy.deepcopy(self.skus), 1, 3, "Tote"
        )
        with self.assertRaisesRegex(ValueError, "only available for AMR shelf"):
            self.inventory.swap_whole_shelf_units(
                rows, rows[0]["handling_unit_id"], rows[1]["handling_unit_id"]
            )

    def test_layout_repository_round_trip(self):
        rows, summary = self.slotting.generate_basic(
            copy.deepcopy(self.building), copy.deepcopy(self.skus), 1, 3, "AMR shelf"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "layout.slotting.json"
            self.layouts.save(
                rows,
                self.building,
                summary,
                path,
                strategy="basic",
                handling_unit_type="AMR shelf",
                levels_per_rack=1,
                slots_per_level=3,
                zone_assignments={"G0_0": "Z01", "G2_0": "Z02"},
            )
            payload = self.layouts.load(path)
        self.assertEqual(payload["assignments"], rows)
        self.assertEqual(payload["summary"], summary)

    def test_zone_id_auto_increment(self):
        self.assertEqual(self.slotting.next_zone_id("Z01"), "Z02")
        self.assertEqual(self.slotting.next_zone_id("ZONE_009"), "ZONE_010")


if __name__ == "__main__":
    unittest.main()
