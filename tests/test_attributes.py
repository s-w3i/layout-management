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
    OVERSIZE_STORAGE_DEFAULTS as WAREHOUSE_OVERSIZE_STORAGE_DEFAULTS,
    PHYSICAL_ATTRIBUTE_KEYS,
    STANDARD_STORAGE_DEFAULTS as WAREHOUSE_STANDARD_STORAGE_DEFAULTS,
    SlottingLayoutRepository,
    SlottingService,
    StorageAttributeService,
)
from warehouse_layout.config import LEGACY_SLOTTING_SCHEMA, SLOTTING_SCHEMA
from warehouse_layout.storage_planning import (
    materialize_independent_attribute_zones,
)


# Most integration fixtures intentionally use compact unitless values. Keep
# their historical capacity scale independent from the production m/kg defaults.
STANDARD_STORAGE_DEFAULTS = {
    "max_item_length": 25.0,
    "max_item_width": 19.3,
    "max_item_height": 19.2,
    "max_item_weight": 465.0,
}
OVERSIZE_STORAGE_DEFAULTS = {
    "max_item_length": 150.0,
    "max_item_width": 50.0,
    "max_item_height": 95.0,
    "max_item_weight": 640.0,
}


class StorageAttributeTests(unittest.TestCase):
    def test_disconnected_matching_racks_become_different_zones(self):
        positions = []
        for index in range(3):
            rack_id = f"R{index + 1}"
            address = f"Z01/A01/{rack_id}/L01/S01"
            positions.append({
                "rack_id": rack_id,
                "waypoint": rack_id,
                "x": 0,
                "y": index,
                "zone_id": "Z01",
                "static_bay_id": rack_id,
                "static_address": address,
                "storage_location_address": address,
                "planned_storage_type": "STANDARD",
                "planned_zone_id": "Z01_STANDARD",
            })
        generated = {
            "Z01__tablet_T": {
                "parent_zone_id": "Z01",
                "attributes": {"tablet": True},
                "rack_ids": ["R1", "R3"],
                "hierarchy_path": [],
            },
            "Z01__tablet_F": {
                "parent_zone_id": "Z01",
                "attributes": {"tablet": False},
                "rack_ids": ["R2"],
                "hierarchy_path": [],
            },
        }
        result = materialize_independent_attribute_zones(
            positions, [], generated, {}, {}
        )

        tablet_zones = [
            definition for definition in result.values()
            if definition["attributes"] == {"tablet": True}
        ]
        self.assertEqual(len(tablet_zones), 2)
        self.assertTrue(all(
            len(definition["rack_ids"]) == 1
            for definition in tablet_zones
        ))

    def setUp(self):
        self.attributes = StorageAttributeService(STANDARD_STORAGE_DEFAULTS)
        self.catalog = self.attributes.starter_catalog()

    def test_custom_standard_storage_defaults_control_physical_classification(self):
        self.attributes.set_standard_storage_defaults({
            "max_item_length": 10,
            "max_item_width": 8,
            "max_item_height": 6,
            "max_item_weight": 100,
        })
        profile = self.attributes.physical_profile({
            "max_item_length": 11,
            "max_item_width": 8,
            "max_item_height": 6,
            "max_item_weight": 50,
        })
        self.assertEqual(profile["storage_class"], "OVERSIZE")

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
            "flammable": AttributeDefinition(
                "flammable", "Flammable", "boolean", hierarchy_level=2
            ),
        }
        requirements = {"chilled": True, "max_item_weight": 25, "flammable": True}
        effective = {"chilled": True, "max_item_weight": 30, "flammable": True}
        self.assertEqual(
            self.attributes.compatibility_issues(requirements, effective, catalog), []
        )
        issues = self.attributes.compatibility_issues(
            requirements,
            {"chilled": False, "max_item_weight": 20},
            catalog,
        )
        self.assertEqual(len(issues), 3)
        self.assertIn("location is False", issues[2])
        self.assertTrue(self.attributes.parse_value(self.catalog["chilled"], "yes"))
        self.assertEqual(
            self.attributes.parse_value(self.catalog["max_item_weight"], "12.5"), 12.5
        )

    def test_only_physical_attributes_may_be_numeric(self):
        with self.assertRaisesRegex(ValueError, "must be Boolean"):
            AttributeDefinition(
                "security_tier", "Security tier", "number", "capacity"
            ).validate()
        definition = AttributeDefinition(
            "tablet", "Tablet", "boolean", "exact", hierarchy_level=2
        )
        self.assertEqual(
            AttributeDefinition.from_dict(definition.to_dict()), definition
        )

    def test_blank_physical_zone_limits_are_unbounded(self):
        local = self.attributes.validate_location_attributes(
            {
                "Z01": {
                    "chilled": False,
                    "max_item_length": None,
                    "max_item_width": "",
                    "max_item_height": None,
                    "max_item_weight": "",
                }
            },
            self.catalog,
        )
        effective, _sources = self.attributes.effective_attributes("Z01", local)
        requirements = {
            "chilled": False,
            "max_item_length": 10000,
            "max_item_width": 9000,
            "max_item_height": 8000,
            "max_item_weight": 7000,
        }
        compatible, issues, status = self.attributes.evaluate_location(
            requirements, effective, self.catalog
        )
        self.assertTrue(compatible)
        self.assertEqual(issues, [])
        self.assertEqual(status, "COMPATIBLE")
        self.assertEqual(
            self.attributes.required_local_overrides(
                requirements, effective, self.catalog
            ),
            {},
        )
        self.assertEqual(
            SlottingService.required_slot_footprint(requirements, effective),
            (1, 1),
        )

    def test_missing_physical_data_types_are_classified_conservatively(self):
        complete_size = {
            "max_item_length": 5,
            "max_item_width": 6,
            "max_item_height": 7,
        }
        unknown_weight = self.attributes.physical_profile(complete_size)
        self.assertEqual(unknown_weight["missing_data_type"], "UNKNOWN_WEIGHT")
        self.assertEqual(unknown_weight["storage_class"], "UNKNOWN_WEIGHT")

        unknown_size = self.attributes.physical_profile({"max_item_weight": 10})
        self.assertEqual(unknown_size["missing_data_type"], "UNKNOWN_SIZE")
        self.assertEqual(unknown_size["storage_class"], "OVERSIZE")

        no_physical_data = self.attributes.physical_profile({})
        self.assertEqual(
            no_physical_data["missing_data_type"], "NON_VOLUMETRIC_DATA"
        )
        self.assertEqual(
            no_physical_data["storage_class"], "NON_VOLUMETRIC_DATA"
        )

    def test_asrs_machine_envelope_is_an_independent_oversize_rule(self):
        self.attributes.set_machine_carrying_capacity({
            "max_item_length": 8,
            "max_item_width": 8,
            "max_item_height": 8,
            "max_item_weight": 200,
        }, "Tote")
        profile = self.attributes.physical_profile({
            "max_item_length": 9,
            "max_item_width": 7,
            "max_item_height": 6,
            "max_item_weight": 20,
        })
        self.assertEqual(profile["storage_class"], "OVERSIZE")
        self.assertFalse(profile["slot_volumetric_oversize"])
        self.assertTrue(profile["machine_volumetric_oversize"])

    def test_amr_machine_rule_uses_weight_and_ignores_dimensions(self):
        self.attributes.set_machine_carrying_capacity({
            "max_item_length": 1,
            "max_item_width": 1,
            "max_item_height": 1,
            "max_item_weight": 15,
        }, "AMR shelf")
        profile = self.attributes.physical_profile({
            "max_item_length": 5,
            "max_item_width": 6,
            "max_item_height": 7,
            "max_item_weight": 20,
        })
        self.assertEqual(profile["storage_class"], "OVERWEIGHT")
        self.assertFalse(profile["machine_volumetric_oversize"])
        self.assertTrue(profile["machine_overweight"])

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
        self.attributes = StorageAttributeService(STANDARD_STORAGE_DEFAULTS)
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
            "Z01": {
                "chilled": False,
                "oversize_capable": False,
                **STANDARD_STORAGE_DEFAULTS,
            }
        }

    def test_weight_heuristic_prefers_center_level_and_zero_disables_it(self):
        project = GridProject(
            GridSpec(2, 1, 1, "ergonomic-weight", "L1"),
            {
                (0, 0): Marker("rack", "RACK_01"),
                (2, 1): Marker("workstation", "PACK_01"),
            },
        )
        building = project.to_building_dict()
        capacity = {
            "Z01": {
                "chilled": False,
                "oversize_capable": True,
                **OVERSIZE_STORAGE_DEFAULTS,
            }
        }
        base_requirements = {
            "chilled": False,
            "max_item_length": 5,
            "max_item_width": 5,
            "max_item_height": 5,
        }
        weighted_rows, weighted_summary = self.slotting.generate_basic(
            building,
            [{
                "sku": "HEAVY", "pick_frequency": 1, "velocity_class": "A",
                "sku_requirements": {**base_requirements, "max_item_weight": 500},
            }],
            5, 1, "AMR shelf", attribute_catalog=self.catalog,
            location_attributes=copy.deepcopy(capacity),
        )
        self.assertEqual(weighted_rows[0]["storage_level"], 2)
        self.assertTrue(weighted_rows[0]["ergonomic_weight_heuristic"])
        self.assertEqual(weighted_rows[0]["ergonomic_preferred_level"], 2)
        self.assertTrue(weighted_summary["ergonomic_weight_heuristic"])

        disabled_rows, _summary = self.slotting.generate_basic(
            building,
            [{
                "sku": "DISABLED", "pick_frequency": 1, "velocity_class": "A",
                "sku_requirements": {**base_requirements, "max_item_weight": 0},
            }],
            5, 1, "AMR shelf", attribute_catalog=self.catalog,
            location_attributes=copy.deepcopy(capacity),
        )
        self.assertEqual(disabled_rows[0]["storage_level"], 1)
        self.assertFalse(disabled_rows[0]["ergonomic_weight_heuristic"])
        self.assertEqual(disabled_rows[0]["physical_data_status"], "MISSING")

        unknown_weight_rows, _summary = self.slotting.generate_basic(
            building,
            [{
                "sku": "UNKNOWN_WEIGHT",
                "pick_frequency": 1,
                "velocity_class": "A",
                "sku_requirements": base_requirements,
            }],
            5, 1, "AMR shelf", attribute_catalog=self.catalog,
            location_attributes=copy.deepcopy(capacity),
        )
        self.assertEqual(unknown_weight_rows[0]["storage_level"], 1)
        self.assertFalse(
            unknown_weight_rows[0]["ergonomic_weight_heuristic"]
        )

    def test_exception_categories_can_mix_in_the_same_rack(self):
        project = GridProject(
            GridSpec(6, 1, 1, "unknown-category-racks", "L1"),
            {
                (0, 0): Marker("rack", "RACK_A"),
                (2, 0): Marker("rack", "RACK_B"),
                (4, 0): Marker("rack", "RACK_C"),
                (6, 1): Marker("workstation", "PACK_01"),
            },
        )
        building = project.to_building_dict()
        capacity = {
            "Z01": {
                "chilled": False,
                "oversize_capable": True,
                **OVERSIZE_STORAGE_DEFAULTS,
            }
        }
        rows, summary = self.slotting.generate_basic(
            building,
            [
                {
                    "sku": "UNKNOWN_SIZE",
                    "pick_frequency": 3,
                    "velocity_class": "A",
                    "sku_requirements": {"chilled": False, "max_item_weight": 1},
                },
                {
                    "sku": "UNKNOWN_WEIGHT",
                    "pick_frequency": 2,
                    "velocity_class": "A",
                    "sku_requirements": {
                        "chilled": False,
                        "max_item_length": 5,
                        "max_item_width": 5,
                        "max_item_height": 5,
                    },
                },
                {
                    "sku": "NON_VOLUMETRIC",
                    "pick_frequency": 1,
                    "velocity_class": "A",
                    "sku_requirements": {"chilled": False},
                },
            ],
            2,
            2,
            "AMR shelf",
            attribute_catalog=self.catalog,
            location_attributes=copy.deepcopy(capacity),
        )
        by_sku = {row["sku"]: row for row in rows}
        self.assertEqual(
            by_sku["UNKNOWN_SIZE"]["physical_missing_data_type"], "UNKNOWN_SIZE"
        )
        self.assertEqual(
            by_sku["UNKNOWN_WEIGHT"]["physical_missing_data_type"], "UNKNOWN_WEIGHT"
        )
        self.assertEqual(
            by_sku["NON_VOLUMETRIC"]["physical_missing_data_type"],
            "NON_VOLUMETRIC_DATA",
        )
        self.assertGreaterEqual(
            len([
                row for row in by_sku.values()
                if row["rack_id"] == by_sku["UNKNOWN_SIZE"]["rack_id"]
            ]),
            2,
        )

    def test_overweight_requires_level_two_in_basic_slotting(self):
        project = GridProject(
            GridSpec(2, 1, 1, "overweight-hard-level", "L1"),
            {
                (0, 0): Marker("rack", "RACK_01"),
                (2, 1): Marker("workstation", "PACK_01"),
            },
        )
        rows, summary = self.slotting.generate_basic(
            project.to_building_dict(),
            [
                {
                    "sku": "HEAVY_1",
                    "pick_frequency": 2,
                    "velocity_class": "A",
                    "sku_requirements": {
                        "chilled": False,
                        "max_item_length": 5,
                        "max_item_width": 5,
                        "max_item_height": 5,
                        "max_item_weight": 500,
                    },
                },
                {
                    "sku": "HEAVY_2",
                    "pick_frequency": 1,
                    "velocity_class": "C",
                    "sku_requirements": {
                        "chilled": False,
                        "max_item_length": 5,
                        "max_item_width": 5,
                        "max_item_height": 5,
                        "max_item_weight": 500,
                    },
                },
            ],
            3,
            1,
            "AMR shelf",
            attribute_catalog=self.catalog,
            location_attributes={
                "Z01": {
                    "chilled": False,
                    "oversize_capable": True,
                    **OVERSIZE_STORAGE_DEFAULTS,
                }
            },
        )
        by_sku = {row["sku"]: row for row in rows}
        self.assertEqual(by_sku["HEAVY_1"]["assignment_status"], "ASSIGNED")
        self.assertEqual(by_sku["HEAVY_1"]["storage_level"], 2)
        self.assertEqual(
            by_sku["HEAVY_2"]["assignment_status"],
            "UNASSIGNED_NO_COMPATIBLE_LOCATION",
        )
        self.assertIn(
            "overweight inventory requires level 2",
            "; ".join(by_sku["HEAVY_2"]["compatibility_issues"]),
        )
        self.assertEqual(summary["assigned_count"], 1)

    def test_standard_zone_does_not_raise_map_capacity(self):
        project = GridProject(
            GridSpec(2, 1, 1, "oversize-overweight-footprint", "L1"),
            {
                (0, 0): Marker("rack", "RACK_01"),
                (2, 1): Marker("workstation", "PACK_01"),
            },
        )
        rows, summary = self.slotting.generate_basic(
            project.to_building_dict(),
            [{
                "sku": "LARGE_HEAVY",
                "pick_frequency": 1,
                "velocity_class": "A",
                "sku_requirements": {
                    "chilled": False,
                    "max_item_length": 14,
                    "max_item_width": 26,
                    "max_item_height": 20,
                    "max_item_weight": 500,
                },
            }],
            3,
            4,
            "AMR shelf",
            attribute_catalog=self.catalog,
            location_attributes={"Z01": {
                "chilled": False,
                "max_item_length": 15,
                "max_item_width": 16,
                "max_item_height": 13,
                "max_item_weight": 250,
            }},
        )
        row = rows[0]
        self.assertEqual(
            row["assignment_status"], "UNASSIGNED_NO_COMPATIBLE_LOCATION"
        )
        self.assertEqual(row["physical_storage_class"], "OVERSIZE_AND_OVERWEIGHT")
        self.assertEqual(row["auto_attribute_overrides"], {})
        self.assertEqual(summary["occupied_slot_count"], 0)

    def test_oversize_zone_raises_weight_capacity_from_compatible_skus(self):
        project = GridProject(
            GridSpec(2, 1, 1, "auto-oversize-weight", "L1"),
            {
                (0, 0): Marker("rack", "RACK_01"),
                (2, 1): Marker("workstation", "PACK_01"),
            },
        )
        local = {
            "Z01": {
                "chilled": False,
                "oversize_capable": True,
                "max_item_length": 25,
                "max_item_width": 25,
                "max_item_height": 25,
                "max_item_weight": 465,
            }
        }
        rows, summary = self.slotting.generate_basic(
            project.to_building_dict(),
            [{
                "sku": "HEAVY",
                "pick_frequency": 1,
                "velocity_class": "A",
                "sku_requirements": {
                    "chilled": False,
                    "max_item_length": 5,
                    "max_item_width": 5,
                    "max_item_height": 5,
                    "max_item_weight": 640,
                },
            }],
            3,
            1,
            "AMR shelf",
            attribute_catalog=self.catalog,
            location_attributes=local,
        )

        self.assertEqual(rows[0]["assignment_status"], "ASSIGNED")
        self.assertEqual(rows[0]["storage_level"], 2)
        self.assertEqual(local["Z01"]["max_item_weight"], 640)
        self.assertEqual(
            summary["auto_adjusted_oversize_zone_weight_capacities"],
            {
                "Z01": {
                    "previous_max_item_weight": 465,
                    "updated_max_item_weight": 640,
                }
            },
        )

    def test_oversize_zone_weight_uses_only_matching_boolean_profile(self):
        project = GridProject(
            GridSpec(2, 1, 1, "profiled-oversize-weight", "L1"),
            {
                (0, 0): Marker("rack", "RACK_01"),
                (2, 1): Marker("workstation", "PACK_01"),
            },
        )
        local = {
            "Z01": {
                "chilled": False,
                "oversize_capable": True,
                **STANDARD_STORAGE_DEFAULTS,
            }
        }
        _rows, summary = self.slotting.generate_basic(
            project.to_building_dict(),
            [
                self.sku("AMBIENT_HEAVY", 2, {"max_item_weight": 525}),
                self.sku(
                    "CHILLED_HEAVY", 1,
                    {"chilled": True, "max_item_weight": 640},
                ),
            ],
            3,
            2,
            "AMR shelf",
            attribute_catalog=self.catalog,
            location_attributes=local,
        )

        self.assertEqual(local["Z01"]["max_item_weight"], 525)
        self.assertEqual(
            summary["auto_adjusted_oversize_zone_weight_capacity_count"], 1
        )

    def test_velocity_csv_accepts_zero_weight_as_heuristic_opt_out(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "velocity.csv"
            path.write_text(
                "sku,pick_frequency,velocity_class,req_max_item_length,"
                "req_max_item_width,req_max_item_height,req_max_item_weight\n"
                "SKU_ZERO,1,A,1,1,1,0\n",
                encoding="utf-8",
            )
            rows = self.slotting.load_velocity(path, self.catalog)
        self.assertEqual(rows[0]["sku_requirements"]["max_item_weight"], 0)
        self.assertEqual(rows[0]["physical_data_status"], "MISSING")

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

    def test_compatible_rack_is_filled_before_opening_next_rack(self):
        skus = [
            {**self.sku("A_1", 100), "velocity_class": "A"},
            {**self.sku("B_1", 80), "velocity_class": "B"},
            {**self.sku("B_2", 70), "velocity_class": "B"},
        ]
        rows, summary = self.slotting.generate_basic(
            copy.deepcopy(self.building), skus, 1, 2, "AMR shelf", "Z01",
            self.zones, self.catalog, copy.deepcopy(self.base_local),
        )
        rack_classes = {}
        for row in rows:
            rack_classes.setdefault(row["rack_id"], set()).add(
                row["velocity_class"]
            )
        rack_counts = {
            rack_id: sum(row["rack_id"] == rack_id for row in rows)
            for rack_id in rack_classes
        }
        filled_rack = next(rack_id for rack_id, count in rack_counts.items() if count == 2)
        self.assertEqual(rack_classes[filled_rack], {"A", "B"})

    def test_predefined_ambient_oversize_does_not_block_standard_first(self):
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
        local = copy.deepcopy(self.base_local)
        local["Z01/A01/BAY-G0_0/L03"] = {
            "oversize_capable": True,
            **OVERSIZE_STORAGE_DEFAULTS,
        }
        rows, summary = self.slotting.generate_basic(
            building,
            [
                self.sku("OVERSIZE", 100, {"max_item_length": 26}),
                self.sku("STANDARD", 90),
            ],
            3,
            1,
            "AMR shelf",
            "Z01",
            zones,
            self.catalog,
            local,
        )
        by_sku = {row["sku"]: row for row in rows}
        self.assertEqual(by_sku["STANDARD"]["assignment_status"], "ASSIGNED")
        self.assertEqual(by_sku["STANDARD"]["storage_area_type"], "STANDARD")
        self.assertEqual(by_sku["OVERSIZE"]["physical_storage_class"], "OVERSIZE")
        self.assertEqual(by_sku["OVERSIZE"]["storage_level"], 3)
        self.assertEqual(by_sku["OVERSIZE"]["planned_zone_id"], "Z01")
        self.assertEqual(
            by_sku["STANDARD"]["planned_zone_id"],
            by_sku["OVERSIZE"]["planned_zone_id"],
        )
        self.assertEqual(by_sku["OVERSIZE"]["planned_storage_type"], "OVERSIZE")
        self.assertEqual(by_sku["STANDARD"]["planned_storage_type"], "STANDARD")
        self.assertEqual(
            by_sku["OVERSIZE"]["compatibility_status"],
            "COMPATIBLE",
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
                    "velocity_class": "A", "req_unknown": "true",
                })
            inferred_catalog = {}
            inferred_rows = self.slotting.load_velocity(path, inferred_catalog)
            self.assertEqual(
                inferred_rows[0]["sku_requirements"], {"unknown": True}
            )
            self.assertEqual(inferred_catalog["unknown"].value_type, "boolean")
            self.assertEqual(inferred_catalog["unknown"].hierarchy_level, 1)

    def test_rotation_physical_classes_and_unverified_matching(self):
        rotated = {
            "chilled": False,
            "max_item_length": 16,
            "max_item_width": 10,
            "max_item_height": 7,
            "max_item_weight": 20,
        }
        compatible, issues, status = self.attributes.evaluate_location(
            rotated, {"chilled": False, **STANDARD_STORAGE_DEFAULTS}, self.catalog
        )
        self.assertTrue(compatible, issues)
        self.assertEqual(status, "COMPATIBLE")
        too_large = {**rotated, "max_item_length": 26}
        compatible, issues, _status = self.attributes.evaluate_location(
            too_large, {"chilled": False, **STANDARD_STORAGE_DEFAULTS}, self.catalog
        )
        self.assertFalse(compatible)
        self.assertIn("any allowed rotation", "; ".join(issues))
        self.assertEqual(
            self.attributes.physical_profile(
                {**rotated, "max_item_weight": 466}
            )["storage_class"],
            "OVERWEIGHT",
        )

        local = copy.deepcopy(self.base_local)
        rejected, _rejected_summary = self.slotting.generate_basic(
            copy.deepcopy(self.building),
            [{
                "sku": "UNKNOWN_SIZE", "pick_frequency": 10,
                "velocity_class": "A", "sku_requirements": {"chilled": False},
            }],
            1, 1, "AMR shelf", "Z01", self.zones, self.catalog, local,
        )
        self.assertEqual(
            rejected[0]["assignment_status"],
            "UNASSIGNED_NO_COMPATIBLE_LOCATION",
        )
        self.assertEqual(
            rejected[0]["physical_missing_data_type"],
            "NON_VOLUMETRIC_DATA",
        )

        local["Z01"]["oversize_capable"] = True
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
        self.assertEqual(rows[0]["storage_area_type"], "OVERSIZE")
        self.assertEqual(
            rows[0]["physical_missing_data_type"], "NON_VOLUMETRIC_DATA"
        )
        self.assertEqual(summary["assigned_unverified_count"], 1)

    def test_user_oversize_flags_are_preserved_when_no_outlier_exists(self):
        local = copy.deepcopy(self.base_local)
        local[self.slot_paths[1]] = {
            "oversize_capable": True,
            **OVERSIZE_STORAGE_DEFAULTS,
        }
        rows, _summary = self.slotting.generate_basic(
            copy.deepcopy(self.building),
            [self.sku("FIRST", 2), self.sku("SECOND", 1)],
            1, 1, "AMR shelf", "Z01", self.zones, self.catalog, local,
        )
        self.assertEqual(rows[0]["assignment_status"], "ASSIGNED")
        self.assertEqual(rows[0]["storage_area_type"], "STANDARD")
        self.assertEqual(rows[1]["assignment_status"], "ASSIGNED")
        self.assertEqual(
            {row["planned_storage_type"] for row in rows},
            {"STANDARD", "OVERSIZE"},
        )

    def test_known_oversize_uses_predefined_oversize_slot(self):
        local = copy.deepcopy(self.base_local)
        local[self.slot_paths[1]] = {
            "oversize_capable": True,
            **OVERSIZE_STORAGE_DEFAULTS,
        }
        rows, summary = self.slotting.generate_basic(
            copy.deepcopy(self.building),
            [
                self.sku("NORMAL", 2),
                self.sku("LARGE", 1, {"max_item_length": 26}),
            ],
            1, 1, "AMR shelf", "Z01", self.zones, self.catalog, local,
        )
        large = next(row for row in rows if row["sku"] == "LARGE")
        self.assertEqual(large["assignment_status"], "ASSIGNED")
        self.assertEqual(
            large["storage_location_address"].split("/", 1)[0],
            large["zone_id"],
        )
        self.assertEqual(
            set(summary["zone_storage_types"].values()),
            {"STANDARD"},
        )

    def test_constrained_planner_reserves_overweight_level_before_greedy_fill(self):
        project = GridProject(
            GridSpec(2, 1, 1, "constrained-reservation", "L1"),
            {
                (0, 0): Marker("rack", "RACK_01"),
                (2, 1): Marker("workstation", "PACK_01"),
            },
        )
        building = project.to_building_dict()
        _level, racks, _workstations, _unreachable = self.slotting.rack_distances(
            building
        )
        zones = {racks[0]["waypoint"]: "Z01"}
        local = {"Z01": {
            "chilled": False,
            "oversize_capable": True,
            "max_item_length": 25.0,
            "max_item_width": 19.3,
            "max_item_height": 19.2,
            "max_item_weight": 465.0,
        }}
        skus = [
            self.sku(f"WIDE_{number}", 100 - number, {
                "max_item_length": 30,
                "max_item_width": 10,
                "max_item_height": 10,
            })
            for number in range(1, 5)
        ]
        skus.extend([
            self.sku("HEAVY", 1, {"max_item_weight": 560}),
            self.sku("IMPOSSIBLE_DEPTH", 0, {
                "max_item_length": 100,
                "max_item_width": 100,
                "max_item_height": 100,
            }),
        ])

        rows, summary = self.slotting.generate_basic(
            building, skus, 3, 4, "AMR shelf", "Z01", zones,
            self.catalog, local,
        )

        by_sku = {row["sku"]: row for row in rows}
        self.assertTrue(all(
            by_sku[f"WIDE_{number}"]["assignment_status"] == "ASSIGNED"
            for number in range(1, 5)
        ))
        self.assertEqual(by_sku["HEAVY"]["assignment_status"], "ASSIGNED")
        self.assertEqual(by_sku["HEAVY"]["storage_level"], 2)
        self.assertEqual(
            by_sku["IMPOSSIBLE_DEPTH"]["assignment_status"],
            "UNASSIGNED_NO_COMPATIBLE_LOCATION",
        )
        occupied = [
            address
            for row in rows if row["assignment_status"] == "ASSIGNED"
            for address in row["occupied_storage_location_addresses"]
        ]
        self.assertEqual(len(occupied), len(set(occupied)))
        self.assertEqual(summary["constrained_inventory_count"], 6)
        self.assertEqual(summary["reserved_constrained_inventory_count"], 5)
        self.assertEqual(summary["reserved_constrained_slot_count"], 9)
        self.assertEqual(summary["unreservable_constrained_inventory_count"], 1)
        self.assertFalse(summary["constrained_feasibility_plan_complete"])

    def test_chilled_standard_uses_replanned_standard_segment(self):
        local = copy.deepcopy(self.base_local)
        local[self.slot_paths[1]] = {
            "chilled": True,
            "oversize_capable": True,
            **OVERSIZE_STORAGE_DEFAULTS,
        }
        rows, summary = self.slotting.generate_basic(
            copy.deepcopy(self.building),
            [self.sku("CHILLED_STANDARD", 1, {"chilled": True})],
            1, 1, "AMR shelf", "Z01", self.zones, self.catalog, local,
        )
        self.assertEqual(rows[0]["assignment_status"], "ASSIGNED")
        self.assertEqual(rows[0]["planned_storage_type"], "OVERSIZE")
        self.assertEqual(rows[0]["planned_zone_id"], "Z01")
        self.assertEqual(summary["generated_attribute_zones"], {})

    def test_oversize_does_not_automatically_plan_a_dedicated_segment(self):
        local = copy.deepcopy(self.base_local)
        rows, summary = self.slotting.generate_basic(
            copy.deepcopy(self.building),
            [self.sku("TOO_LONG", 1, {"max_item_length": 26})],
            1, 2, "AMR shelf", "Z01", self.zones, self.catalog, local,
        )
        self.assertEqual(
            rows[0]["assignment_status"],
            "UNASSIGNED_NO_COMPATIBLE_LOCATION",
        )
        self.assertEqual(rows[0]["planned_storage_type"], "")
        self.assertEqual(rows[0]["planned_zone_id"], "")
        self.assertEqual(rows[0]["occupied_slot_count"], 0)
        self.assertIn(
            "oversize-capable zone",
            "; ".join(rows[0]["compatibility_issues"]),
        )
        self.assertNotIn(
            "max_item_length", rows[0]["auto_attribute_overrides"]
        )
        self.assertEqual(summary["auto_planned_oversize_segment_count"], 0)

        heavy_rows, _summary = self.slotting.generate_basic(
            copy.deepcopy(self.building),
            [self.sku("TOO_HEAVY", 1, {"max_item_weight": 466})],
            1, 1, "AMR shelf", "Z01", self.zones, self.catalog,
            copy.deepcopy(self.base_local),
        )
        self.assertEqual(
            heavy_rows[0]["assignment_status"],
            "UNASSIGNED_NO_COMPATIBLE_LOCATION",
        )

        zero_weight_rows, _summary = self.slotting.generate_basic(
            copy.deepcopy(self.building),
            [self.sku("OVERSIZE_WEIGHT_OPT_OUT", 1, {
                "max_item_length": 26,
                "max_item_weight": 0,
            })],
            1, 2, "AMR shelf", "Z01", self.zones, self.catalog,
            copy.deepcopy(self.base_local),
        )
        self.assertEqual(
            zero_weight_rows[0]["assignment_status"],
            "UNASSIGNED_NO_COMPATIBLE_LOCATION",
        )
        self.assertEqual(zero_weight_rows[0]["occupied_slot_count"], 0)
        self.assertEqual(
            zero_weight_rows[0]["physical_missing_data_type"], "UNKNOWN_WEIGHT"
        )
        self.assertEqual(
            zero_weight_rows[0]["physical_storage_class"],
            "OVERSIZE",
        )

        chilled_rows, _summary = self.slotting.generate_basic(
            copy.deepcopy(self.building),
            [self.sku(
                "CHILLED_LARGE", 1,
                {"max_item_length": 26, "chilled": True},
            )],
            1, 1, "AMR shelf", "Z01", self.zones, self.catalog,
            copy.deepcopy(self.base_local),
        )
        self.assertEqual(
            chilled_rows[0]["assignment_status"],
            "UNASSIGNED_NO_CHILLED_LOCATION",
        )

    def test_oversize_zone_can_leave_all_maximums_unbounded(self):
        project = GridProject(
            GridSpec(2, 1, 1, "oversize-zone", "L1"),
            {
                (0, 0): Marker("rack", "RACK_01"),
                (2, 1): Marker("workstation", "PACK_01"),
            },
        )
        building = project.to_building_dict()
        _level, racks, _workstations, _unreachable = self.slotting.rack_distances(
            building
        )
        zones = {racks[0]["waypoint"]: "Z99"}
        local = {"Z99": {
            "chilled": False,
            "oversize_capable": True,
            **{key: None for key in STANDARD_STORAGE_DEFAULTS},
        }}
        rows, summary = self.slotting.generate_basic(
            building,
            [self.sku("OUTLIER", 1, {
                "max_item_length": 1000,
                "max_item_width": 900,
                "max_item_height": 800,
                "max_item_weight": 700,
            })],
            1, 1, "AMR shelf", "Z99", zones, self.catalog, local,
        )
        self.assertEqual(rows[0]["assignment_status"], "ASSIGNED")
        self.assertEqual(rows[0]["storage_area_type"], "OVERSIZE")
        self.assertEqual(rows[0]["auto_attribute_overrides"], {})
        self.assertEqual(summary["unassigned_no_oversize_location_count"], 0)

    def test_chilled_zone_is_not_split_for_unknown_size_inventory(self):
        local = copy.deepcopy(self.base_local)
        local["Z01"]["chilled"] = True
        local["Z01"]["oversize_capable"] = True
        rows, summary = self.slotting.generate_basic(
            copy.deepcopy(self.building),
            [
                self.sku("CHILLED_STANDARD", 100, {"chilled": True}),
                {
                    "sku": "CHILLED_UNKNOWN_SIZE",
                    "pick_frequency": 1,
                    "velocity_class": "A",
                    "sku_requirements": {
                        "chilled": True,
                        "max_item_width": 6,
                        "max_item_height": 7,
                        "max_item_weight": 10,
                    },
                },
            ],
            1, 1, "AMR shelf", "Z01", self.zones, self.catalog, local,
        )
        by_sku = {row["sku"]: row for row in rows}
        self.assertEqual(
            by_sku["CHILLED_UNKNOWN_SIZE"]["physical_missing_data_type"],
            "UNKNOWN_SIZE",
        )
        self.assertLess(
            by_sku["CHILLED_STANDARD"]["placement_rank"],
            by_sku["CHILLED_UNKNOWN_SIZE"]["placement_rank"],
        )
        self.assertEqual(
            by_sku["CHILLED_UNKNOWN_SIZE"]["storage_area_type"], "OVERSIZE"
        )
        self.assertEqual(
            by_sku["CHILLED_STANDARD"]["storage_area_type"], "OVERSIZE"
        )
        self.assertNotEqual(
            by_sku["CHILLED_UNKNOWN_SIZE"]["storage_location_address"],
            by_sku["CHILLED_STANDARD"]["storage_location_address"],
        )
        self.assertNotEqual(
            by_sku["CHILLED_UNKNOWN_SIZE"]["rack_id"],
            by_sku["CHILLED_STANDARD"]["rack_id"],
        )
        self.assertEqual(
            by_sku["CHILLED_STANDARD"]["planned_storage_type"], "OVERSIZE"
        )
        self.assertEqual(
            by_sku["CHILLED_UNKNOWN_SIZE"]["planned_storage_type"],
            "OVERSIZE",
        )
        for row in by_sku.values():
            self.assertTrue(
                row["static_address"].startswith(row["zone_id"] + "/")
            )
        self.assertEqual(
            set(summary["zone_storage_types"].values()),
            {"OVERSIZE"},
        )
        self.assertEqual(len(summary["zone_storage_types"]), 1)
        self.assertEqual(summary["generated_attribute_zones"], {})

    def test_ambient_outlier_does_not_change_whole_map_zones(self):
        zones = {
            self.racks[0]["waypoint"]: "Z01",
            self.racks[1]["waypoint"]: "Z02",
        }
        local = {
            "Z01": {"chilled": False, **STANDARD_STORAGE_DEFAULTS},
            "Z02": {
                "chilled": False,
                "oversize_capable": True,
                **OVERSIZE_STORAGE_DEFAULTS,
            },
        }
        rows, summary = self.slotting.generate_basic(
            copy.deepcopy(self.building),
            [
                self.sku("AMBIENT_STANDARD", 100),
                self.sku("AMBIENT_OUTLIER", 1, {"max_item_length": 26}),
            ],
            1, 2, "AMR shelf", "Z01", zones, self.catalog, local,
        )
        by_sku = {row["sku"]: row for row in rows}
        self.assertEqual(by_sku["AMBIENT_OUTLIER"]["assignment_status"], "ASSIGNED")
        self.assertEqual(
            by_sku["AMBIENT_OUTLIER"]["planned_storage_type"], "OVERSIZE"
        )
        self.assertEqual(by_sku["AMBIENT_STANDARD"]["planned_storage_type"], "STANDARD")
        self.assertEqual(summary["zone_assignments"], zones)
        self.assertEqual(summary["generated_attribute_zones"], {})
        self.assertNotIn("MIXED", summary["zone_storage_types"].values())

    def test_outlier_is_rejected_when_no_contiguous_footprint_exists(self):
        local = copy.deepcopy(self.base_local)
        local[self.slot_paths[0]] = {
            "oversize_capable": True,
            **OVERSIZE_STORAGE_DEFAULTS,
            "max_item_length": 16,
        }
        rows, _summary = self.slotting.generate_basic(
            copy.deepcopy(self.building),
            [self.sku("TOO_LONG_FOR_OUTLIER_SLOT", 1, {
                "max_item_length": 151,
                "max_item_width": 51,
                "max_item_height": 96,
            })],
            1, 1, "AMR shelf", "Z01", self.zones, self.catalog, local,
        )
        self.assertEqual(
            rows[0]["assignment_status"],
            "UNASSIGNED_NO_COMPATIBLE_LOCATION",
        )
        self.assertIn(
            "item dimensions do not fit this rack footprint in any rotation",
            rows[0]["compatibility_issues"],
        )

    def test_swap_cannot_move_oversize_sku_into_standard_slot(self):
        local = copy.deepcopy(self.base_local)
        local[self.slot_paths[1]] = {
            "oversize_capable": True,
            **OVERSIZE_STORAGE_DEFAULTS,
        }
        rows, _summary = self.slotting.generate_basic(
            copy.deepcopy(self.building),
            [
                self.sku("STANDARD", 2),
                self.sku("OVERSIZE", 1, {"max_item_length": 26}),
            ],
            1, 1, "AMR shelf", "Z01", self.zones, self.catalog, local,
        )
        before = [row["storage_location_address"] for row in rows]
        with self.assertRaisesRegex(ValueError, "reserved|not predefined"):
            self.inventory.swap_sku_slots(
                rows, "STANDARD", "OVERSIZE", self.catalog, local
            )
        self.assertEqual(
            [row["storage_location_address"] for row in rows], before
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
            self.assertNotIn("chilled", by_sku["SKU_A"]["sku_requirements"])
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

    def test_general_sku_attributes_csv_merges_medicine_requirements(self):
        with tempfile.TemporaryDirectory() as directory:
            velocity = Path(directory) / "velocity.csv"
            attributes = Path(directory) / "attributes.csv"
            velocity.write_text(
                "sku,pick_frequency,velocity_class,req_max_item_weight\n"
                "MED_1,10,A,25\n",
                encoding="utf-8",
            )
            attributes.write_text(
                "sku,max_item_length,max_item_width,max_item_height,chilled,"
                "tablet,flammable\n"
                "MED_1,5,6,7,true,true,false\n",
                encoding="utf-8",
            )
            catalog, summary = self.slotting.inspect_sku_attribute_csv(
                attributes, self.catalog
            )
            rows = self.slotting.load_velocity(
                velocity, catalog, attributes
            )

        self.assertEqual(summary["sku_count"], 1)
        self.assertEqual(
            set(summary["attributes"]),
            {
                "max_item_length", "max_item_width", "max_item_height",
                "chilled", "tablet", "flammable", "oversize",
            },
        )
        self.assertEqual(rows[0]["sku_requirements"], {
            "max_item_weight": 25,
            "max_item_length": 5,
            "max_item_width": 6,
            "max_item_height": 7,
            "chilled": True,
            "tablet": True,
            "flammable": False,
        })

    def test_attribute_schema_is_inferred_without_starter_definitions(self):
        with tempfile.TemporaryDirectory() as directory:
            velocity = Path(directory) / "velocity.csv"
            attributes = Path(directory) / "attributes.csv"
            velocity.write_text(
                "sku,pick_frequency,velocity_class\n"
                "SKU_1,10,A\nSKU_2,5,B\n",
                encoding="utf-8",
            )
            attributes.write_text(
                "sku,cold_chain,tablet,flammable\n"
                "SKU_1,true,true,false\n"
                "SKU_2,false,false,true\n",
                encoding="utf-8",
            )
            catalog = {}
            rows = self.slotting.load_velocity(
                velocity, catalog, attributes
            )

        self.assertEqual(set(catalog), {
            "cold_chain", "tablet", "flammable"
        })
        self.assertEqual(catalog["cold_chain"].value_type, "boolean")
        self.assertEqual(catalog["cold_chain"].hierarchy_level, 1)
        self.assertEqual(catalog["tablet"].hierarchy_level, 2)
        self.assertEqual(catalog["flammable"].hierarchy_level, 2)
        self.assertNotIn("chilled", catalog)
        self.assertEqual(rows[0]["sku_requirements"], {
            "cold_chain": True,
            "tablet": True,
            "flammable": False,
        })

    def test_attribute_inspection_counts_dynamic_boolean_combinations(self):
        with tempfile.TemporaryDirectory() as directory:
            attributes = Path(directory) / "attributes.csv"
            attributes.write_text(
                "sku,cold_chain,hazardous\n"
                "SKU_1,true,false\n"
                "SKU_2,true,false\n"
                "SKU_3,false,true\n",
                encoding="utf-8",
            )
            _catalog, summary = self.slotting.inspect_sku_attribute_csv(
                attributes, {}
            )

        self.assertEqual(summary["attribute_combinations"], [
            {
                "storage_type": "STANDARD",
                "attributes": {"cold_chain": False, "hazardous": True},
                "sku_count": 1,
            },
            {
                "storage_type": "STANDARD",
                "attributes": {"cold_chain": True, "hazardous": False},
                "sku_count": 2,
            },
        ])

    def test_overlay_combinations_use_only_selected_boolean_attributes(self):
        with tempfile.TemporaryDirectory() as directory:
            attributes = Path(directory) / "attributes.csv"
            attributes.write_text(
                "sku,chilled,tablet,flammable,max_item_weight\n"
                "SKU_1,true,true,false,1\n"
                "SKU_2,true,false,true,1\n"
                "SKU_3,false,true,true,1\n",
                encoding="utf-8",
            )
            catalog, summary = self.slotting.inspect_sku_attribute_csv(
                attributes, {}, ["tablet"]
            )

        self.assertIn("chilled", catalog)
        self.assertIn("flammable", catalog)
        self.assertEqual(
            summary["available_combination_attributes"],
            ["chilled", "tablet", "flammable"],
        )
        self.assertEqual(summary["combination_attributes"], ["tablet"])
        self.assertEqual(
            summary["attribute_combinations"],
            [
                {
                    "storage_type": "STANDARD",
                    "attributes": {"tablet": False},
                    "sku_count": 1,
                },
                {
                    "storage_type": "STANDARD",
                    "attributes": {"tablet": True},
                    "sku_count": 2,
                },
            ],
        )

    def test_oversize_is_a_selectable_derived_grouping_attribute(self):
        attributes_service = StorageAttributeService(
            WAREHOUSE_STANDARD_STORAGE_DEFAULTS
        )
        slotting = SlottingService(attributes=attributes_service)
        with tempfile.TemporaryDirectory() as directory:
            attributes = Path(directory) / "attributes.csv"
            attributes.write_text(
                "sku,chilled,max_item_length,max_item_width,max_item_height,"
                "max_item_weight\n"
                "SKU_STANDARD,false,0.1,0.1,0.1,0.1\n"
                "SKU_OVERSIZE,false,3.1,0.1,0.1,0.1\n",
                encoding="utf-8",
            )
            catalog, summary = slotting.inspect_sku_attribute_csv(
                attributes, {}, ["oversize"]
            )
            requirements = slotting.load_sku_attribute_requirements(
                attributes,
                {"SKU_STANDARD", "SKU_OVERSIZE"},
                catalog,
                include_derived_grouping=True,
            )

        self.assertIn("oversize", summary["available_combination_attributes"])
        self.assertEqual(summary["combination_attributes"], ["oversize"])
        self.assertNotIn("oversize", catalog)
        self.assertFalse(requirements["SKU_STANDARD"]["oversize"])
        self.assertTrue(requirements["SKU_OVERSIZE"]["oversize"])
        self.assertEqual(
            summary["attribute_combinations"],
            [
                {
                    "storage_type": "OVERSIZE",
                    "attributes": {"oversize": True},
                    "sku_count": 1,
                },
                {
                    "storage_type": "STANDARD",
                    "attributes": {"oversize": False},
                    "sku_count": 1,
                },
            ],
        )

    def test_json_attribute_definition_overrides_csv_inference(self):
        with tempfile.TemporaryDirectory() as directory:
            attributes = Path(directory) / "attributes.csv"
            attributes.write_text(
                "sku,tablet\nSKU_1,true\n",
                encoding="utf-8",
            )
            explicit = {
                "tablet": AttributeDefinition(
                    "tablet", "Tablet area", "boolean", "exact",
                    hierarchy_level=4,
                )
            }
            catalog, summary = self.slotting.inspect_sku_attribute_csv(
                attributes, explicit
            )

        self.assertEqual(catalog["tablet"].hierarchy_level, 4)
        self.assertEqual(
            summary["attributes"]["tablet"]["hierarchy_level"], 4
        )

    def test_inferred_custom_attribute_is_a_hard_zone_requirement(self):
        catalog = {
            "fish_area": AttributeDefinition(
                "fish_area", "Fish area", "boolean", "exact",
                hierarchy_level=1,
            )
        }
        rows, _summary = self.slotting.generate_basic(
            copy.deepcopy(self.building),
            [{
                "sku": "VACCINE_1", "pick_frequency": 1,
                "velocity_class": "A",
                "sku_requirements": {"fish_area": True},
            }],
            1, 1, "AMR shelf", "Z01", self.zones, catalog,
            {"Z01": {"fish_area": False}},
            auto_plan_oversize=False,
        )
        self.assertEqual(
            rows[0]["assignment_status"],
            "UNASSIGNED_NO_COMPATIBLE_LOCATION",
        )
        self.assertIn("Fish area", "; ".join(rows[0]["compatibility_issues"]))

    def test_missing_zone_attributes_are_ignored_in_strategy_order(self):
        catalog = {
            "tablet": AttributeDefinition(
                "tablet", "Tablet", "boolean", "exact",
                hierarchy_level=1,
            ),
            "flammable": AttributeDefinition(
                "flammable", "Flammable", "boolean", "exact",
                hierarchy_level=2,
            ),
        }
        local = {}
        rows, summary = self.slotting.generate_basic(
            copy.deepcopy(self.building),
            [
                {
                    "sku": "GENERAL_1", "pick_frequency": 10,
                    "velocity_class": "A",
                    "sku_requirements": {
                        "tablet": False, "flammable": False,
                    },
                },
                {
                    "sku": "VACCINE_1", "pick_frequency": 100,
                    "velocity_class": "A",
                    "sku_requirements": {
                        "tablet": True, "flammable": False,
                    },
                },
            ],
            1, 1, "AMR shelf", "Z01", self.zones, catalog, local,
            auto_plan_oversize=False,
        )

        by_sku = {row["sku"]: row for row in rows}
        self.assertTrue(all(
            row["assignment_status"] == "ASSIGNED" for row in rows
        ))
        self.assertLess(
            by_sku["VACCINE_1"]["placement_rank"],
            by_sku["GENERAL_1"]["placement_rank"],
        )
        self.assertEqual(
            by_sku["VACCINE_1"]["auto_attribute_overrides"],
            {},
        )
        self.assertEqual(summary["auto_overridden_slot_count"], 0)
        self.assertEqual(local, {})
        self.assertEqual(
            {row["planned_zone_id"] for row in rows}, {"Z01"}
        )
        self.assertEqual(summary["generated_attribute_zones"], {})

    def test_unconfigured_exact_attributes_do_not_create_rack_groups(self):
        catalog = {
            "tablet": AttributeDefinition(
                "tablet", "Tablet", "boolean", "exact",
                hierarchy_level=1,
            )
        }
        local = {}
        rows, summary = self.slotting.generate_basic(
            copy.deepcopy(self.building),
            [
                {
                    "sku": f"TABLET_{value}_{number}",
                    "pick_frequency": 100 - number,
                    "velocity_class": "A",
                    "sku_requirements": {"tablet": value},
                }
                for number, value in enumerate(
                    (True, False, True, False), start=1
                )
            ],
            1, 2, "AMR shelf", "Z01", self.zones, catalog, local,
            auto_plan_oversize=False,
        )
        rack_values = {}
        for row in rows:
            self.assertEqual(row["assignment_status"], "ASSIGNED")
            rack_values.setdefault(row["rack_id"], set()).add(
                row["sku_requirements"]["tablet"]
            )
            self.assertEqual(row["planned_zone_id"], "Z01")
        self.assertTrue(any(len(values) > 1 for values in rack_values.values()))
        generated_zones = {
            row["generated_attribute_zone_id"] for row in rows
        }
        self.assertEqual(generated_zones, {"Z01"})
        self.assertEqual(summary["generated_attribute_zones"], {})
        rack_paths = [path for path in local if path.count("/") == 2]
        self.assertEqual(rack_paths, [])

    def test_single_exact_profile_keeps_the_source_zone_whole(self):
        catalog = {
            "tablet": AttributeDefinition(
                "tablet", "Tablet", "boolean", "exact",
                hierarchy_level=1,
            )
        }
        local = {}
        rows, summary = self.slotting.generate_basic(
            copy.deepcopy(self.building),
            [{
                "sku": f"TABLET_{number}",
                "pick_frequency": 100 - number,
                "velocity_class": "A",
                "sku_requirements": {"tablet": True},
            } for number in range(4)],
            1, 2, "AMR shelf", "Z01", self.zones, catalog, local,
            auto_plan_oversize=False,
        )
        self.assertEqual(
            {row["generated_attribute_zone_id"] for row in rows}, {"Z01"}
        )
        self.assertEqual(local, {})
        self.assertEqual(summary["generated_attribute_zones"], {})

    def test_boolean_hierarchy_does_not_create_subzones(self):
        project = GridProject(
            GridSpec(4, 2, 1, "hierarchical-zones", "L1"),
            {
                (0, 0): Marker("rack", "RACK_1"),
                (1, 0): Marker("rack", "RACK_2"),
                (2, 0): Marker("rack", "RACK_3"),
                (4, 2): Marker("workstation", "WS_1"),
            },
        )
        building = project.to_building_dict()
        _level, racks, _workstations, _unreachable = (
            self.slotting.rack_distances(building)
        )
        zones = {rack["waypoint"]: "Z01" for rack in racks}
        catalog = {
            key: AttributeDefinition(
                key, key.replace("_", " ").title(), "boolean", "exact",
                hierarchy_level=level,
            )
            for key, level in (
                ("chilled", 1), ("tablet", 2), ("flammable", 2)
            )
        }
        profiles = (
            (True, True, False),
            (True, False, False),
            (True, False, True),
        )
        local = {}
        rows, summary = self.slotting.generate_basic(
            building,
            [{
                "sku": f"SKU_{index}",
                "pick_frequency": 100 - index,
                "velocity_class": "A",
                "sku_requirements": dict(zip(catalog, profile)),
            } for index, profile in enumerate(profiles, start=1)],
            1, 1, "AMR shelf", "Z01", zones, catalog, local,
            auto_plan_oversize=True,
        )
        self.assertEqual(
            {row["generated_attribute_zone_id"] for row in rows},
            {"Z01"},
        )
        for row in rows:
            self.assertEqual(row["zone_id"], row["generated_attribute_zone_id"])
            self.assertEqual(
                row["static_address"].split("/", 1)[0], row["zone_id"]
            )
            self.assertNotIn("parent_zone_id", row)
            self.assertEqual(
                summary["zone_assignments"][row["rack_waypoint"]],
                row["zone_id"],
            )
        _level, loaded_racks, _workstations, _unreachable = (
            self.slotting.rack_distances(building)
        )
        self.slotting.apply_zone_local_aisles(
            building, loaded_racks, summary["zone_assignments"], "Z01"
        )
        reloaded_paths = self.attributes.hierarchy_paths(loaded_racks, 1, 1)
        self.attributes.validate_location_attributes(
            local, catalog, reloaded_paths
        )
        self.assertEqual(summary["attribute_hierarchy"], [])
        self.assertEqual(summary["generated_attribute_zones"], {})
        self.assertEqual(local, {})

    def test_missing_core_location_values_are_ignored_without_mutation(self):
        local = {}
        rows, summary = self.slotting.generate_basic(
            copy.deepcopy(self.building),
            [
                self.sku("CHILLED", 100, {"chilled": True}),
                self.sku("AMBIENT", 90, {"chilled": False}),
            ],
            1, 1, "AMR shelf", "Z01", self.zones, self.catalog, local,
        )
        self.assertEqual(summary["assigned_count"], 2)
        self.assertEqual(summary["auto_overridden_slot_count"], 0)
        self.assertEqual(summary["generated_attribute_zones"], {})
        self.assertEqual(local, {})
        self.assertEqual({row["planned_zone_id"] for row in rows}, {"Z01"})

    def test_explicit_custom_boolean_is_never_overwritten(self):
        catalog = {
            "flammable": AttributeDefinition(
                "flammable", "Flammable", "boolean", "exact",
                hierarchy_level=1,
            )
        }
        local = {"Z01": {"flammable": False}}
        rows, _summary = self.slotting.generate_basic(
            copy.deepcopy(self.building),
            [{
                "sku": "HIGH_SECURITY", "pick_frequency": 1,
                "velocity_class": "A",
                "sku_requirements": {"flammable": True},
            }],
            1, 1, "AMR shelf", "Z01", self.zones, catalog, local,
            auto_plan_oversize=False,
        )
        self.assertEqual(
            rows[0]["assignment_status"],
            "UNASSIGNED_NO_COMPATIBLE_LOCATION",
        )
        self.assertEqual(local, {"Z01": {"flammable": False}})

    def test_attribute_configured_in_one_zone_is_enforced_across_all_zones(self):
        catalog = {
            "tablet": AttributeDefinition(
                "tablet", "Tablet", "boolean", "exact", hierarchy_level=1
            )
        }
        zones = {
            self.racks[0]["waypoint"]: "Z01",
            self.racks[1]["waypoint"]: "Z02",
        }
        local = {"Z01": {"tablet": True}}
        rows, summary = self.slotting.generate_basic(
            copy.deepcopy(self.building),
            [
                {
                    "sku": "TABLET", "pick_frequency": 100,
                    "velocity_class": "A",
                    "sku_requirements": {"tablet": True},
                },
                {
                    "sku": "NON_TABLET", "pick_frequency": 90,
                    "velocity_class": "A",
                    "sku_requirements": {"tablet": False},
                },
            ],
            1, 1, "AMR shelf", "Z01", zones, catalog, local,
        )
        by_sku = {row["sku"]: row for row in rows}
        self.assertEqual(by_sku["TABLET"]["assignment_status"], "ASSIGNED")
        self.assertEqual(by_sku["TABLET"]["zone_id"], "Z01")
        self.assertEqual(
            by_sku["NON_TABLET"]["assignment_status"],
            "ASSIGNED",
        )
        self.assertEqual(by_sku["NON_TABLET"]["zone_id"], "Z02")
        self.assertEqual(summary["zone_assignments"], zones)
        self.assertEqual(summary["generated_attribute_zones"], {})
        self.assertEqual(local, {"Z01": {"tablet": True}})

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

    def test_sku_swap_cannot_move_overweight_item_to_standard_segment(self):
        local = copy.deepcopy(self.base_local)
        rows, _summary = self.slotting.generate_basic(
            copy.deepcopy(self.building),
            [
                self.sku("HEAVY", 2, {"max_item_weight": 500}),
                self.sku("NORMAL", 1),
            ],
            1, 1, "AMR shelf", "Z01", self.zones, self.catalog, local,
        )
        heavy = self.inventory.find_sku(rows, "HEAVY")
        normal = self.inventory.find_sku(rows, "NORMAL")
        target = normal["static_address"]
        with self.assertRaisesRegex(ValueError, "not predefined|reserved"):
            self.inventory.swap_sku_slots(
                rows, "HEAVY", "NORMAL", self.catalog, local
            )
        self.assertNotIn("max_item_weight", local.get(target, {}))

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
