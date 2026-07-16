"""Regression tests for the object-oriented application services."""

from __future__ import annotations

import copy
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from openpyxl import Workbook

from warehouse_layout import (
    GridProject,
    GridSpec,
    InventoryService,
    Marker,
    RmfMapService,
    SlottingLayoutRepository,
    SlottingService,
)
from warehouse_layout.affinity import AffinityService


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

    def affinity_analysis(self, directory):
        path = Path(directory) / "orders.xlsx"
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["Date", "Store ID", "Item or SKU"])
        picked_date = date(2026, 1, 1)
        for store, pair, count in (
            ("STORE_AB", ("SKU_00", "SKU_01"), 5),
            ("STORE_AC", ("SKU_00", "SKU_02"), 2),
            ("STORE_BC", ("SKU_01", "SKU_02"), 1),
        ):
            for _ in range(count):
                for sku in pair:
                    sheet.append([picked_date, store, sku])
                picked_date += timedelta(days=1)
        workbook.save(path)
        service = AffinityService(Path(directory) / "cache")
        return path, service.analyze(service.load_orders(path))

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

    def test_unreachable_racks_remain_last_resort_storage_capacity(self):
        building = copy.deepcopy(self.building)
        level = next(iter(building["levels"].values()))
        level["lanes"] = []
        rows, summary = self.slotting.generate_basic(
            building, copy.deepcopy(self.skus[:2]), 1, 1, "AMR shelf"
        )
        self.assertEqual(summary["assigned_count"], 2)
        self.assertEqual(summary["unreachable_rack_count"], 2)
        self.assertTrue(
            all(row["routing_status"] == "UNREACHABLE_LAST_RESORT" for row in rows)
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
                source_affinity="/input/orders.xlsx",
                affinity_configuration={"minimum_shared_store_days": 4},
            )
            payload = self.layouts.load(path)
        self.assertEqual(payload["assignments"], rows)
        self.assertEqual(payload["summary"], summary)
        self.assertEqual(
            payload["sources"]["affinity_order_workbook"], "/input/orders.xlsx"
        )
        self.assertEqual(
            payload["affinity_configuration"]["minimum_shared_store_days"], 4
        )

    def test_abc_affinity_auto_tunes_and_accepts_user_adjustment(self):
        with tempfile.TemporaryDirectory() as directory:
            _path, analysis = self.affinity_analysis(directory)
            baseline_rows, baseline_summary = self.slotting.generate_basic(
                copy.deepcopy(self.building), copy.deepcopy(self.skus), 1, 3
            )
            auto_rows, auto_summary = self.slotting.generate_abc_affinity(
                copy.deepcopy(self.building),
                copy.deepcopy(self.skus),
                analysis,
                0.6,
                1,
                3,
            )
            adjusted_rows, adjusted_summary = self.slotting.generate_abc_affinity(
                copy.deepcopy(self.building),
                copy.deepcopy(self.skus),
                analysis,
                0.6,
                1,
                3,
                tuning_parameters={
                    "maximum_service_distance_increase": 0.0,
                    "minimum_shared_store_days": 1,
                    "minimum_affinity_score": 0.0,
                },
            )

        self.assertEqual(auto_summary["affinity_tuning"]["parameter_status"], "AUTO_SUGGESTED")
        self.assertEqual(auto_summary["strategy"], "abc_affinity")
        self.assertEqual(auto_summary["assigned_count"], baseline_summary["assigned_count"])
        self.assertEqual(
            [row["velocity_class"] for row in auto_rows],
            [row["velocity_class"] for row in baseline_rows],
        )
        self.assertTrue(
            all(
                "affinity_weight" in row
                for row in auto_rows
                if row["assignment_status"] == "ASSIGNED"
            )
        )
        adjusted = adjusted_summary["affinity_tuning"]
        self.assertEqual(adjusted["parameter_status"], "USER_ADJUSTED")
        self.assertEqual(adjusted["minimum_shared_store_days"], 1)
        self.assertEqual(adjusted["minimum_affinity_score"], 0.0)
        self.assertEqual(adjusted["maximum_service_distance_increase"], 0.0)
        self.assertEqual(len(adjusted_rows), len(auto_rows))

    def test_zone_id_auto_increment(self):
        self.assertEqual(self.slotting.next_zone_id("Z01"), "Z02")
        self.assertEqual(self.slotting.next_zone_id("ZONE_009"), "ZONE_010")


if __name__ == "__main__":
    unittest.main()
