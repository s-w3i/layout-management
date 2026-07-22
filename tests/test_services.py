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

    def test_affinity_weight_controls_abc_vs_affinity_order_ratio(self):
        rows = [
            {"sku": "A_SKU", "velocity_class": "A"},
            {"sku": "B_SKU", "velocity_class": "B"},
            {"sku": "C_SKU", "velocity_class": "C"},
        ]
        neighbors = {
            "A_SKU": [("B_SKU", 1.0, 1.0, 1)],
            "B_SKU": [("A_SKU", 1.0, 1.0, 1)],
            "C_SKU": [("B_SKU", 10.0, 1.0, 10)],
        }

        pure_abc = self.slotting._affinity_placement_order(rows, neighbors, 0.0)
        pure_affinity = self.slotting._affinity_placement_order(
            rows, neighbors, 1.0
        )
        blended = self.slotting._affinity_placement_order(rows, neighbors, 0.7)

        self.assertEqual([row["sku"] for row in pure_abc], [
            "A_SKU", "B_SKU", "C_SKU",
        ])
        self.assertEqual([row["sku"] for row in pure_affinity[:2]], [
            "C_SKU", "B_SKU",
        ])
        self.assertEqual([row["sku"] for row in blended[:2]], [
            "C_SKU", "B_SKU",
        ])

    def test_project_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "warehouse.grid.json"
            self.rmf.save_project(self.project, path)
            loaded = self.rmf.load_project(path)
        self.assertEqual(loaded.to_project_dict(), self.project.to_project_dict())

    def test_grid_spec_uses_short_final_interval_for_odd_spacing(self):
        spec = GridSpec(20, 10, 3, "odd_spacing", "L1")
        spec.validate()
        self.assertEqual(spec.columns, 7)
        self.assertEqual(spec.rows, 4)
        self.assertEqual(
            [spec.x_coordinate(column) for column in range(spec.columns + 1)],
            [0, 3, 6, 9, 12, 15, 18, 20],
        )
        self.assertEqual(
            [spec.y_coordinate(row) for row in range(spec.rows + 1)],
            [0, 3, 6, 9, 10],
        )

    def test_grid_spec_supports_rectangular_x_y_spacing(self):
        spec = GridSpec(8, 6, 2, "rectangular", "L1", 3)
        spec.validate()
        self.assertEqual((spec.columns, spec.rows), (4, 2))
        self.assertEqual(
            [spec.x_coordinate(column) for column in range(spec.columns + 1)],
            [0, 2, 4, 6, 8],
        )
        self.assertEqual(
            [spec.y_coordinate(row) for row in range(spec.rows + 1)],
            [0, 3, 6],
        )

    def test_buffer_assignment_is_saved_without_changing_rmf_building(self):
        before = self.project.to_building_dict()
        layout = self.project.assign_storage_buffers("AMR", 2, 4)
        after = self.project.to_building_dict()
        self.assertEqual(before, after)
        self.assertEqual(len(layout.buffers), 2)
        self.assertTrue(all(item["status"] == "EMPTY" for item in layout.buffers))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "warehouse.grid.json"
            self.rmf.save_project(self.project, path)
            loaded = self.rmf.load_project(path)
        self.assertEqual(loaded.storage_layout.to_dict(), layout.to_dict())

    def test_amr_grid_buffers_use_static_grid_and_dynamic_shelf_addresses(self):
        layout = self.project.assign_storage_buffers("AMR", 1, 3)
        rows, summary = self.slotting.generate_basic(
            copy.deepcopy(self.project.to_building_dict()),
            copy.deepcopy(self.skus),
            1,
            3,
            "AMR shelf",
            storage_layout=layout,
        )
        self.assertEqual(summary["buffer_count"], 2)
        self.assertEqual(summary["occupied_buffer_count"], 2)
        self.assertEqual(summary["buffer_occupancy_rate"], 1.0)
        self.assertEqual(len({row["static_address"] for row in rows[:3]}), 1)
        self.assertRegex(rows[0]["static_address"], r"^Z01/A\d{2}/B-G\d+_\d+$")
        self.assertEqual(rows[0]["dynamic_address"], "SHELF_001/L01/S01")
        self.assertEqual(rows[2]["dynamic_address"], "SHELF_001/L01/S03")

    def test_racks_are_ranked_by_assigned_pick_frequency_after_slotting(self):
        layout = self.project.assign_storage_buffers("AMR", 1, 3)
        rows, summary = self.slotting.generate_basic(
            copy.deepcopy(self.project.to_building_dict()),
            copy.deepcopy(self.skus),
            1,
            3,
            "AMR shelf",
            storage_layout=layout,
        )
        ranking = summary["rack_frequency_ranking"]
        self.assertEqual(len(ranking), 2)
        self.assertGreater(
            ranking[0]["rack_pick_frequency"],
            ranking[1]["rack_pick_frequency"],
        )
        self.assertEqual(ranking[0]["rack_frequency_rank"], 1)
        self.assertEqual(ranking[0]["rack_velocity_class"], "A")
        self.assertEqual(ranking[1]["rack_velocity_class"], "C")

        by_rack = {item["rack_id"]: item for item in ranking}
        for row in rows:
            if row["assignment_status"] != "ASSIGNED":
                continue
            rack_record = by_rack[row["rack_id"]]
            self.assertEqual(
                row["rack_pick_frequency"],
                rack_record["rack_pick_frequency"],
            )
            self.assertEqual(
                row["rack_velocity_class"],
                rack_record["rack_velocity_class"],
            )

    def test_asrs_slot_buffers_are_counted_independently(self):
        layout = self.project.assign_storage_buffers("Mini-load ASRS", 1, 3)
        rows, summary = self.slotting.generate_basic(
            copy.deepcopy(self.project.to_building_dict()),
            copy.deepcopy(self.skus[:2]),
            1,
            3,
            "Tote",
            storage_layout=layout,
        )
        self.assertEqual(summary["buffer_count"], 6)
        self.assertEqual(summary["occupied_buffer_count"], 2)
        self.assertAlmostEqual(summary["buffer_occupancy_rate"], 2 / 6)
        self.assertRegex(
            rows[0]["static_address"],
            r"^Z01/A\d{2}/B-G\d+_\d+/L01/S01$",
        )
        self.assertEqual(rows[0]["dynamic_address"], "TOTE_001")

    def test_slotting_layout_persists_buffer_occupancy_records(self):
        layout = self.project.assign_storage_buffers("AMR", 1, 3)
        building = self.project.to_building_dict()
        rows, summary = self.slotting.generate_basic(
            copy.deepcopy(building), copy.deepcopy(self.skus[:1]),
            1, 3, "AMR shelf", storage_layout=layout,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "layout.slotting.json"
            self.layouts.save(
                rows, building, summary, path,
                strategy="basic", handling_unit_type="AMR shelf",
                levels_per_rack=1, slots_per_level=3,
                zone_assignments={}, storage_layout=layout,
                source_grid_project="/input/warehouse.grid.json",
            )
            payload = self.layouts.load(path)
        self.assertEqual(payload["sources"]["grid_project_json"], "/input/warehouse.grid.json")
        self.assertEqual(
            sum(item["status"] == "OCCUPIED" for item in payload["buffers"]), 1
        )
        self.assertEqual(
            sum(item["status"] == "EMPTY" for item in payload["buffers"]), 1
        )

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

    def test_high_affinity_weight_consolidates_cross_class_skus_in_one_bay(self):
        cross_class_skus = [
            {
                **copy.deepcopy(self.skus[index]),
                "velocity_class": velocity_class,
            }
            for index, velocity_class in enumerate(("A", "B", "C", "A"))
        ]
        with tempfile.TemporaryDirectory() as directory:
            _path, analysis = self.affinity_analysis(directory)
            basic_rows, basic_summary = self.slotting.generate_basic(
                copy.deepcopy(self.building),
                copy.deepcopy(cross_class_skus),
                1,
                2,
            )
            affinity_rows, affinity_summary = self.slotting.generate_abc_affinity(
                copy.deepcopy(self.building),
                copy.deepcopy(cross_class_skus),
                analysis,
                0.8,
                1,
                2,
                tuning_parameters={
                    "maximum_service_distance_increase": 10.0,
                    "minimum_shared_store_days": 1,
                    "minimum_affinity_score": 0.0,
                },
            )
            low_affinity_rows, low_affinity_summary = self.slotting.generate_abc_affinity(
                copy.deepcopy(self.building),
                copy.deepcopy(cross_class_skus),
                analysis,
                0.2,
                1,
                2,
                tuning_parameters={
                    "maximum_service_distance_increase": 10.0,
                    "minimum_shared_store_days": 1,
                    "minimum_affinity_score": 0.0,
                },
            )

        basic_by_sku = {row["sku"]: row for row in basic_rows}
        affinity_by_sku = {row["sku"]: row for row in affinity_rows}
        low_affinity_by_sku = {row["sku"]: row for row in low_affinity_rows}
        self.assertNotEqual(
            basic_by_sku["SKU_00"]["rack_id"],
            basic_by_sku["SKU_01"]["rack_id"],
        )
        self.assertEqual(
            affinity_by_sku["SKU_00"]["rack_id"],
            affinity_by_sku["SKU_01"]["rack_id"],
        )
        self.assertNotEqual(
            low_affinity_by_sku["SKU_00"]["rack_id"],
            low_affinity_by_sku["SKU_01"]["rack_id"],
        )
        self.assertGreater(
            affinity_summary["affinity_metrics"]["affinity_pair_same_bay_fraction"],
            low_affinity_summary["affinity_metrics"][
                "affinity_pair_same_bay_fraction"
            ],
        )
        self.assertGreater(
            affinity_summary["affinity_metrics"]["mixed_abc_rack_count"], 0
        )

    def test_zone_id_auto_increment(self):
        self.assertEqual(self.slotting.next_zone_id("Z01"), "Z02")
        self.assertEqual(self.slotting.next_zone_id("ZONE_009"), "ZONE_010")


if __name__ == "__main__":
    unittest.main()
