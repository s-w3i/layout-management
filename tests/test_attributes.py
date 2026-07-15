"""Tests for inherited storage attributes, constrained slotting, and persistence."""

from __future__ import annotations

import copy
import csv
import json
import tempfile
import unittest
from pathlib import Path

from warehouse_layout import (
    AttributeDefinition,
    GridProject,
    GridSpec,
    InventoryService,
    Marker,
    OVERSIZE_STORAGE_DEFAULTS,
    STANDARD_STORAGE_DEFAULTS,
    SlottingLayoutRepository,
    SlottingService,
    StorageAttributeService,
)
from warehouse_layout.config import LEGACY_SLOTTING_SCHEMA, SLOTTING_SCHEMA


class StorageAttributeTests(unittest.TestCase):
    def setUp(self):
        self.attributes = StorageAttributeService()
        self.catalog = self.attributes.starter_catalog()

    def test_inheritance_override_clear_and_no_upward_flow(self):
        local = {
            "Z01": {"chilled": False, "max_item_weight": 100},
            "Z01/A01/BAY-G1_1/L01/S01": {"chilled": True},
        }
        slot = "Z01/A01/BAY-G1_1/L01/S01"
        effective, sources = self.attributes.effective_attributes(slot, local)
        self.assertEqual(effective, {"chilled": True, "max_item_weight": 100})
        self.assertEqual(sources["chilled"], slot)
        zone_effective, _ = self.attributes.effective_attributes("Z01", local)
        self.assertEqual(zone_effective["chilled"], False)

        del local[slot]["chilled"]
        inherited, inherited_sources = self.attributes.effective_attributes(slot, local)
        self.assertEqual(inherited["chilled"], False)
        self.assertEqual(inherited_sources["chilled"], "Z01")

    def test_types_exact_capacity_and_missing_values(self):
        catalog = {
            **self.catalog,
            "temperature": AttributeDefinition(
                "temperature", "Temperature", "choice", choices=("ambient", "frozen")
            ),
        }
        requirements = {"chilled": True, "max_item_weight": 25, "temperature": "frozen"}
        effective = {"chilled": True, "max_item_weight": 30, "temperature": "frozen"}
        self.assertEqual(
            self.attributes.compatibility_issues(requirements, effective, catalog), []
        )
        issues = self.attributes.compatibility_issues(
            requirements,
            {"chilled": False, "max_item_weight": 20},
            catalog,
        )
        self.assertEqual(len(issues), 3)
        self.assertIn("not defined", issues[2])
        self.assertTrue(self.attributes.parse_value(self.catalog["chilled"], "yes"))
        self.assertEqual(
            self.attributes.parse_value(self.catalog["max_item_weight"], "12.5"), 12.5
        )

    def test_full_paths_keep_same_aisle_id_separate_by_zone(self):
        racks = [
            {"zone_id": "Z01", "aisle_id": "A01", "static_bay_id": "BAY-G0_0"},
            {"zone_id": "Z02", "aisle_id": "A01", "static_bay_id": "BAY-G2_0"},
        ]
        paths = self.attributes.hierarchy_paths(racks, 1, 1)
        self.assertIn("Z01/A01", paths)
        self.assertIn("Z02/A01", paths)
        self.assertIn("Z01/A01/BAY-G0_0/L01/S01", paths)
        self.assertIn("Z02/A01/BAY-G2_0/L01/S01", paths)


class AttributeSlottingIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.attributes = StorageAttributeService()
        self.catalog = self.attributes.starter_catalog()
        self.slotting = SlottingService(attributes=self.attributes)
        self.layouts = SlottingLayoutRepository()
        self.inventory = InventoryService(self.slotting, self.attributes)
        project = GridProject(
            GridSpec(3, 2, 1, "attributes", "L1"),
            {
                (0, 0): Marker("rack", "RACK_LEFT"),
                (2, 0): Marker("rack", "RACK_RIGHT"),
                (3, 2): Marker("workstation", "WS_01"),
            },
        )
        self.building = project.to_building_dict()
        _level, self.racks, _workstations, _unreachable = self.slotting.rack_distances(
            self.building
        )
        self.zones = {rack["waypoint"]: "Z01" for rack in self.racks}
        self.slotting.apply_zone_local_aisles(self.building, self.racks, self.zones)
        self.slot_paths = [
            path
            for path in self.attributes.hierarchy_paths(self.racks, 1, 1)
            if path.endswith("/S01")
        ]
        self.base_local = {
            "Z01": {"chilled": False, **STANDARD_STORAGE_DEFAULTS}
        }

    @staticmethod
    def sku(name, frequency, requirements=None):
        physical = {
            "max_item_length": 5,
            "max_item_width": 6,
            "max_item_height": 7,
            "max_item_weight": 10,
        }
        physical.update(requirements or {})
        return {
            "sku": name,
            "pick_frequency": frequency,
            "velocity_class": "A",
            "total_quantity_ea": 1,
            "active_days": 1,
            "sku_requirements": physical,
        }

    def test_velocity_order_uses_compatible_positions_and_reports_reasons(self):
        local = copy.deepcopy(self.base_local)
        local[self.slot_paths[0]] = {"chilled": True}
        skus = [
            self.sku("HOT_CHILLED", 100, {"chilled": True}),
            self.sku("AMBIENT", 90, {"chilled": False}),
            self.sku("NO_CAPACITY", 80),
        ]
        rows, summary = self.slotting.generate_basic(
            copy.deepcopy(self.building), skus, 1, 1, "AMR shelf", "Z01",
            self.zones, self.catalog, local,
        )
        by_sku = {row["sku"]: row for row in rows}
        self.assertEqual(by_sku["HOT_CHILLED"]["assignment_status"], "ASSIGNED")
        self.assertEqual(by_sku["AMBIENT"]["assignment_status"], "ASSIGNED")
        self.assertEqual(
            by_sku["NO_CAPACITY"]["assignment_status"], "UNASSIGNED_NO_CAPACITY"
        )
        self.assertEqual(summary["unassigned_no_compatible_location_count"], 0)
        self.assertEqual(summary["unassigned_no_capacity_count"], 1)

        chilled_rows, chilled_summary = self.slotting.generate_basic(
            copy.deepcopy(self.building),
            [self.sku("CHILLED_WITHOUT_ZONE", 1, {"chilled": True})],
            1, 1, "AMR shelf", "Z01", self.zones, self.catalog,
            self.base_local,
        )
        self.assertEqual(
            chilled_rows[0]["assignment_status"],
            "UNASSIGNED_NO_CHILLED_LOCATION",
        )
        self.assertEqual(
            chilled_summary["unassigned_status_counts"][
                "UNASSIGNED_NO_CHILLED_LOCATION"
            ],
            1,
        )

        capacity_rows, capacity_summary = self.slotting.generate_basic(
            copy.deepcopy(self.building),
            [self.sku("ONE", 3), self.sku("TWO", 2), self.sku("THREE", 1)],
            1, 1, "AMR shelf", "Z01", self.zones, self.catalog,
            self.base_local,
        )
        self.assertEqual(capacity_rows[-1]["assignment_status"], "UNASSIGNED_NO_CAPACITY")
        self.assertEqual(capacity_summary["unassigned_no_capacity_count"], 1)

    def test_abc_class_fills_matching_rack_before_mixing(self):
        skus = [
            {**self.sku("A_1", 100), "velocity_class": "A"},
            {**self.sku("A_2", 90), "velocity_class": "A"},
            {**self.sku("B_1", 80), "velocity_class": "B"},
            {**self.sku("B_2", 70), "velocity_class": "B"},
        ]
        rows, _summary = self.slotting.generate_basic(
            copy.deepcopy(self.building), skus, 1, 2, "AMR shelf", "Z01",
            self.zones, self.catalog, copy.deepcopy(self.base_local),
        )
        rack_classes = {}
        for row in rows:
            rack_classes.setdefault(row["rack_id"], set()).add(
                row["velocity_class"]
            )
        self.assertEqual(len(rack_classes), 2)
        self.assertTrue(all(len(classes) == 1 for classes in rack_classes.values()))

    def test_oversize_uses_level_three_when_mixed_with_standard(self):
        project = GridProject(
            GridSpec(2, 1, 1, "mixed_physical", "L1"),
            {
                (0, 0): Marker("rack", "RACK_ONLY"),
                (2, 1): Marker("workstation", "WS_01"),
            },
        )
        building = project.to_building_dict()
        _level, racks, _workstations, _unreachable = self.slotting.rack_distances(
            building
        )
        zones = {racks[0]["waypoint"]: "Z01"}
        rows, _summary = self.slotting.generate_basic(
            building,
            [
                self.sku("OVERSIZE", 100, {"max_item_length": 17}),
                self.sku("STANDARD", 90),
            ],
            3,
            1,
            "AMR shelf",
            "Z01",
            zones,
            self.catalog,
            copy.deepcopy(self.base_local),
        )
        by_sku = {row["sku"]: row for row in rows}
        self.assertEqual(by_sku["STANDARD"]["storage_level"], 1)
        self.assertEqual(by_sku["OVERSIZE"]["physical_storage_class"], "OVERSIZE")
        self.assertEqual(by_sku["OVERSIZE"]["storage_level"], 3)
        self.assertEqual(
            by_sku["OVERSIZE"]["compatibility_status"],
            "COMPATIBLE_AUTO_OVERRIDE",
        )

    def test_csv_requirement_columns_and_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sku.csv"
            with path.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(
                    stream,
                    fieldnames=("sku", "pick_frequency", "velocity_class", "req_chilled", "req_max_item_weight"),
                )
                writer.writeheader()
                writer.writerow({
                    "sku": "SKU_1", "pick_frequency": 10, "velocity_class": "A",
                    "req_chilled": "true", "req_max_item_weight": "25.5",
                })
            rows = self.slotting.load_velocity(path, self.catalog)
            self.assertEqual(
                rows[0]["sku_requirements"],
                {"chilled": True, "max_item_weight": 25.5},
            )
            with path.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(
                    stream,
                    fieldnames=("sku", "pick_frequency", "velocity_class", "req_unknown"),
                )
                writer.writeheader()
                writer.writerow({
                    "sku": "SKU_1", "pick_frequency": 10,
                    "velocity_class": "A", "req_unknown": "x",
                })
            with self.assertRaisesRegex(ValueError, "unknown SKU requirement"):
                self.slotting.load_velocity(path, self.catalog)

    def test_rotation_physical_classes_and_unverified_matching(self):
        rotated = {
            "chilled": False,
            "max_item_length": 16,
            "max_item_width": 14,
            "max_item_height": 12,
            "max_item_weight": 20,
        }
        compatible, issues, status = self.attributes.evaluate_location(
            rotated, {"chilled": False, **STANDARD_STORAGE_DEFAULTS}, self.catalog
        )
        self.assertTrue(compatible, issues)
        self.assertEqual(status, "COMPATIBLE")
        too_large = {**rotated, "max_item_length": 17}
        compatible, issues, _status = self.attributes.evaluate_location(
            too_large, {"chilled": False, **STANDARD_STORAGE_DEFAULTS}, self.catalog
        )
        self.assertFalse(compatible)
        self.assertIn("any allowed rotation", "; ".join(issues))
        self.assertEqual(
            self.attributes.physical_profile(
                {**rotated, "max_item_weight": 251}
            )["storage_class"],
            "OVERWEIGHT",
        )

        local = copy.deepcopy(self.base_local)
        rows, summary = self.slotting.generate_basic(
            copy.deepcopy(self.building),
            [{
                "sku": "UNKNOWN_SIZE", "pick_frequency": 10,
                "velocity_class": "A", "sku_requirements": {"chilled": False},
            }],
            1, 1, "AMR shelf", "Z01", self.zones, self.catalog, local,
        )
        self.assertEqual(rows[0]["assignment_status"], "ASSIGNED")
        self.assertEqual(rows[0]["compatibility_status"], "UNVERIFIED")
        self.assertEqual(rows[0]["storage_area_type"], "STANDARD")
        self.assertEqual(summary["assigned_unverified_count"], 1)

    def test_standard_prefers_normal_slot_then_can_use_larger_child_slot(self):
        local = copy.deepcopy(self.base_local)
        local[self.slot_paths[1]] = dict(OVERSIZE_STORAGE_DEFAULTS)
        rows, _summary = self.slotting.generate_basic(
            copy.deepcopy(self.building),
            [self.sku("FIRST", 2), self.sku("SECOND", 1)],
            1, 1, "AMR shelf", "Z01", self.zones, self.catalog, local,
        )
        self.assertEqual(rows[0]["assignment_status"], "ASSIGNED")
        self.assertEqual(rows[0]["storage_area_type"], "STANDARD")
        self.assertEqual(rows[1]["assignment_status"], "ASSIGNED")
        self.assertEqual(rows[1]["storage_area_type"], "OVERSIZE")
        self.assertEqual(rows[0]["zone_storage_type"], "STANDARD")
        self.assertEqual(rows[1]["zone_storage_type"], "STANDARD")

    def test_known_oversize_uses_child_override_in_standard_parent_zone(self):
        local = copy.deepcopy(self.base_local)
        local[self.slot_paths[1]] = dict(OVERSIZE_STORAGE_DEFAULTS)
        rows, summary = self.slotting.generate_basic(
            copy.deepcopy(self.building),
            [
                self.sku("NORMAL", 2),
                self.sku("LARGE", 1, {"max_item_length": 17}),
            ],
            1, 1, "AMR shelf", "Z01", self.zones, self.catalog, local,
        )
        large = next(row for row in rows if row["sku"] == "LARGE")
        self.assertEqual(large["assignment_status"], "ASSIGNED")
        self.assertEqual(large["static_address"], self.slot_paths[1])
        self.assertEqual(summary["zone_storage_types"], {"Z01": "MIXED"})

    def test_chilled_standard_can_use_oversize_when_no_standard_chilled_slot_exists(self):
        local = copy.deepcopy(self.base_local)
        local[self.slot_paths[1]] = {
            "chilled": True,
            **OVERSIZE_STORAGE_DEFAULTS,
        }
        rows, _summary = self.slotting.generate_basic(
            copy.deepcopy(self.building),
            [self.sku("CHILLED_STANDARD", 1, {"chilled": True})],
            1, 1, "AMR shelf", "Z01", self.zones, self.catalog, local,
        )
        self.assertEqual(rows[0]["assignment_status"], "ASSIGNED")
        self.assertEqual(rows[0]["storage_area_type"], "OVERSIZE")

    def test_physical_requirements_create_child_overrides_not_unassigned_rows(self):
        cases = (
            (
                self.sku("TOO_LONG", 1, {"max_item_length": 17}),
                "max_item_length", 17,
            ),
            (
                self.sku("TOO_HEAVY", 1, {"max_item_weight": 251}),
                "max_item_weight", 251,
            ),
        )
        for sku, key, expected_value in cases:
            with self.subTest(key=key):
                local = copy.deepcopy(self.base_local)
                rows, _summary = self.slotting.generate_basic(
                    copy.deepcopy(self.building), [sku], 1, 1, "AMR shelf",
                    "Z01", self.zones, self.catalog, local,
                )
                self.assertEqual(rows[0]["assignment_status"], "ASSIGNED")
                self.assertEqual(
                    rows[0]["compatibility_status"], "COMPATIBLE_AUTO_OVERRIDE"
                )
                self.assertEqual(
                    local[rows[0]["static_address"]][key], expected_value
                )

        chilled_rows, _summary = self.slotting.generate_basic(
            copy.deepcopy(self.building),
            [self.sku(
                "CHILLED_LARGE", 1,
                {"max_item_length": 17, "chilled": True},
            )],
            1, 1, "AMR shelf", "Z01", self.zones, self.catalog,
            copy.deepcopy(self.base_local),
        )
        self.assertEqual(
            chilled_rows[0]["assignment_status"],
            "UNASSIGNED_NO_CHILLED_LOCATION",
        )

    def test_chilled_csv_merge_validation_and_exclusivity(self):
        fields = (
            "sku", "pick_frequency", "velocity_class",
            "req_max_item_length", "req_max_item_width", "req_max_item_height",
            "req_max_item_weight",
        )
        with tempfile.TemporaryDirectory() as directory:
            velocity = Path(directory) / "velocity.csv"
            chilled = Path(directory) / "chilled.csv"
            with velocity.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                for sku in ("SKU_A", "SKU_B"):
                    writer.writerow({
                        "sku": sku, "pick_frequency": 1, "velocity_class": "A",
                        "req_max_item_length": 1, "req_max_item_width": 1,
                        "req_max_item_height": 1, "req_max_item_weight": 1,
                    })
            chilled.write_text(
                "sku,chilled_required\nSKU_B,true\n", encoding="utf-8"
            )
            rows = self.slotting.load_velocity(velocity, self.catalog, chilled)
            by_sku = {row["sku"]: row for row in rows}
            self.assertFalse(by_sku["SKU_A"]["sku_requirements"]["chilled"])
            self.assertTrue(by_sku["SKU_B"]["sku_requirements"]["chilled"])

            chilled.write_text(
                "sku,chilled_required\nUNKNOWN,true\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "unknown SKU"):
                self.slotting.load_velocity(velocity, self.catalog, chilled)

            chilled.write_text(
                "sku,chilled_required\nSKU_A,true\nSKU_A,true\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "duplicate SKU"):
                self.slotting.load_velocity(velocity, self.catalog, chilled)

            chilled.write_text(
                "sku,chilled_required\nSKU_A,maybe\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "must be true/false"):
                self.slotting.load_velocity(velocity, self.catalog, chilled)

            conflict_fields = (*fields, "req_chilled")
            with velocity.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=conflict_fields)
                writer.writeheader()
                writer.writerow({
                    "sku": "SKU_A", "pick_frequency": 1, "velocity_class": "A",
                    "req_max_item_length": 1, "req_max_item_width": 1,
                    "req_max_item_height": 1, "req_max_item_weight": 1,
                    "req_chilled": "true",
                })
            chilled.write_text(
                "sku,chilled_required\nSKU_A,false\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "conflicting chilled"):
                self.slotting.load_velocity(velocity, self.catalog, chilled)

    def _compatible_swap_layout(self, handling_unit="AMR shelf"):
        local = {
            **copy.deepcopy(self.base_local),
            self.slot_paths[0]: {"chilled": True},
            self.slot_paths[1]: {"chilled": False},
        }
        rows, _summary = self.slotting.generate_basic(
            copy.deepcopy(self.building),
            [
                self.sku("CHILLED", 100, {"chilled": True}),
                self.sku("AMBIENT", 90, {"chilled": False}),
            ],
            1, 1, handling_unit, "Z01", self.zones, self.catalog, local,
        )
        self.assertTrue(all(row["assignment_status"] == "ASSIGNED" for row in rows))
        return rows, local

    def test_invalid_sku_swap_is_atomic(self):
        rows, local = self._compatible_swap_layout()
        before = [row["static_address"] for row in rows]
        with self.assertRaisesRegex(ValueError, "incompatible"):
            self.inventory.swap_sku_slots(
                rows, "CHILLED", "AMBIENT", self.catalog, local
            )
        self.assertEqual([row["static_address"] for row in rows], before)

    def test_sku_swap_applies_soft_target_override(self):
        local = copy.deepcopy(self.base_local)
        rows, _summary = self.slotting.generate_basic(
            copy.deepcopy(self.building),
            [
                self.sku("HEAVY", 2, {"max_item_weight": 300}),
                self.sku("NORMAL", 1),
            ],
            1, 1, "AMR shelf", "Z01", self.zones, self.catalog, local,
        )
        heavy = self.inventory.find_sku(rows, "HEAVY")
        normal = self.inventory.find_sku(rows, "NORMAL")
        target = normal["static_address"]
        self.inventory.swap_sku_slots(
            rows, "HEAVY", "NORMAL", self.catalog, local
        )
        self.assertEqual(local[target]["max_item_weight"], 300)
        self.assertEqual(
            heavy["compatibility_status"], "COMPATIBLE_AUTO_OVERRIDE"
        )

    def test_invalid_whole_shelf_swap_is_atomic(self):
        rows, local = self._compatible_swap_layout()
        before = [row["static_address"] for row in rows]
        with self.assertRaisesRegex(ValueError, "incompatible"):
            self.inventory.swap_whole_shelf_units(
                rows,
                rows[0]["handling_unit_id"],
                rows[1]["handling_unit_id"],
                self.catalog,
                local,
            )
        self.assertEqual([row["static_address"] for row in rows], before)

    def test_v2_round_trip_and_v1_normalization(self):
        rows, local = self._compatible_swap_layout()
        summary = {"assigned_count": 2}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "v2.slotting.json"
            self.layouts.save(
                rows, self.building, summary, path,
                strategy="basic", handling_unit_type="AMR shelf",
                levels_per_rack=1, slots_per_level=1,
                zone_assignments=self.zones,
                attribute_catalog=self.catalog,
                location_attributes=local,
                source_chilled="demo_chilled.csv",
            )
            loaded = self.layouts.load(path)
            self.assertEqual(loaded["schema"], SLOTTING_SCHEMA)
            self.assertEqual(loaded["location_attributes"], local)
            self.assertEqual(
                loaded["sources"]["chilled_requirements_csv"],
                "demo_chilled.csv",
            )

            legacy_path = Path(directory) / "v1.slotting.json"
            legacy_path.write_text(json.dumps({
                "schema": LEGACY_SLOTTING_SCHEMA,
                "building": self.building,
                "assignments": [{"sku": "OLD", "assignment_status": "ASSIGNED"}],
            }), encoding="utf-8")
            legacy = self.layouts.load(legacy_path)
            self.assertEqual(legacy["schema"], SLOTTING_SCHEMA)
            self.assertEqual(legacy["source_schema"], LEGACY_SLOTTING_SCHEMA)
            self.assertEqual(legacy["attribute_catalog"], [])
            self.assertEqual(legacy["assignments"][0]["sku_requirements"], {})


if __name__ == "__main__":
    unittest.main()
