"""Traffic-aware slotting service tests."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from openpyxl import Workbook

from warehouse_layout import (
    GridProject,
    GridSpec,
    InsufficientStorageError,
    Marker,
    StorageAttributeService,
    TrafficAwareSlottingService,
)
from warehouse_layout.affinity import AffinityService
from warehouse_layout.traffic import NETWORK_SCHEMA, TRAFFIC_EXPORT_SCHEMA


class TrafficAwareSlottingTests(unittest.TestCase):
    def setUp(self):
        self.attributes = StorageAttributeService()
        self.service = TrafficAwareSlottingService(self.attributes)
        self.catalog = self.attributes.serialize_catalog(
            self.attributes.starter_catalog()
        )
        self.requirements = {
            "chilled": False,
            "max_item_length": 1,
            "max_item_width": 1,
            "max_item_height": 1,
            "max_item_weight": 1,
        }
        self.capacity = {
            "chilled": False,
            "max_item_length": 10,
            "max_item_width": 10,
            "max_item_height": 10,
            "max_item_weight": 10,
        }

    def row(self, sku, unit, bay, vertex):
        address = f"Z01/A01/{bay}/L01/S01"
        return {
            "sku": sku,
            "velocity_class": "A" if sku == "SKU_H" else "C",
            "assignment_status": "ASSIGNED",
            "handling_unit_type": "Tote",
            "handling_unit_id": unit,
            "dynamic_address_level": "slot",
            "dynamic_address": f"Z01/A01/{bay}/L01/SLOT-{unit}",
            "static_address": address,
            "rmf_grid_address": "",
            "zone_id": "Z01",
            "aisle_id": "A01",
            "static_bay_id": bay,
            "rack_id": bay,
            "rack_waypoint": bay,
            "pickup_dispenser_id": bay,
            "rack_vertex_index": vertex,
            "rack_rank": vertex + 1,
            "storage_level": 1,
            "storage_slot": 1,
            "workstations_evaluated": 1,
            "average_workstation_distance_m": 1,
            "routing_status": "ROUTED",
            "sku_requirements": dict(self.requirements),
            "effective_location_attributes": dict(self.capacity),
            "auto_attribute_overrides": {},
            "compatibility_status": "COMPATIBLE",
            "compatibility_issues": [],
        }

    def payload(self):
        rows = [
            self.row("SKU_H", "UNIT_H", "BAY_HOT", 0),
            self.row("SKU_L", "UNIT_L", "BAY_COOL", 1),
        ]
        return {
            "schema": "inventory_slotting_layout/v2",
            "assignments": rows,
            "building": {"levels": {}},
            "attribute_catalog": self.catalog,
            "location_attributes": {
                row["static_address"]: dict(self.capacity) for row in rows
            },
            "operation_log": [],
            "sources": {},
            "summary": {},
        }

    @staticmethod
    def write_orders(path: Path):
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["Date", "Store ID", "Item or SKU", "Quantity (in EA)"])
        start = date(2026, 1, 1)
        for index in range(10):
            # Duplicate high-SKU lines in one store-day still require one unit visit.
            sheet.append([start + timedelta(days=index), "STORE_1", "SKU_H", 100])
            if index == 0:
                sheet.append([start, "STORE_1", "SKU_H", 1])
                sheet.append([start, "STORE_1", "SKU_L", 1])
        workbook.save(path)

    @staticmethod
    def write_network(path: Path):
        payload = {
            "schema": NETWORK_SCHEMA,
            "nodes": [
                {"id": "hot", "x": 0, "y": 0, "kind": "storage", "location_ids": ["BAY_HOT"]},
                {"id": "cool", "x": 0, "y": 1, "kind": "storage", "location_ids": ["BAY_COOL"]},
                {"id": "service", "x": 1, "y": 0, "kind": "endpoint"},
            ],
            "resources": [
                {"id": "narrow_aisle", "capacity": 1},
                {"id": "wide_aisle", "capacity": 10},
            ],
            "links": [
                {"id": "hot_service", "from": "hot", "to": "service", "distance": 1, "resource_id": "narrow_aisle"},
                {"id": "cool_service", "from": "cool", "to": "service", "distance": 1, "resource_id": "wide_aisle"},
            ],
            "endpoints": [{"id": "PACK", "node_id": "service", "weight": 1}],
        }
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_store_day_demand_counts_each_unit_once(self):
        with tempfile.TemporaryDirectory() as directory:
            order_path = Path(directory) / "orders.xlsx"
            self.write_orders(order_path)
            dataset = AffinityService(Path(directory) / "cache").load_orders(order_path)
            demand = self.service.build_demand(dataset, self.payload()["assignments"])
        self.assertEqual(demand.fulfillment_groups, 10)
        self.assertEqual(demand.unit_visits, {"UNIT_H": 10, "UNIT_L": 1})
        self.assertEqual(demand.handling_unit_visits, 11)

    def test_asrs_demand_counts_each_occupied_slot_retrieval(self):
        payload = self.payload()
        high = payload["assignments"][0]
        high["occupied_buffer_ids"] = ["BUFFER_1", "BUFFER_2"]
        high["occupied_handling_units"] = [
            {
                "handling_unit_id": "UNIT_H",
                "buffer_id": "BUFFER_1",
            },
            {
                "handling_unit_id": "UNIT_H_2",
                "buffer_id": "BUFFER_2",
            },
        ]
        payload["buffers"] = [
            {"buffer_id": "BUFFER_1"},
            {"buffer_id": "BUFFER_2"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            order_path = Path(directory) / "orders.xlsx"
            self.write_orders(order_path)
            dataset = AffinityService(Path(directory) / "cache").load_orders(
                order_path
            )
            demand = self.service.build_demand(dataset, payload["assignments"])
        self.assertEqual(
            demand.unit_visits,
            {"UNIT_H": 20, "UNIT_L": 1},
        )
        self.assertEqual(demand.handling_unit_visits, 21)
        buffers = self.service._buffer_records(
            payload, payload["assignments"]
        )
        self.assertEqual(
            [row["handling_unit_ids"] for row in buffers],
            [["UNIT_H"], ["UNIT_H_2"]],
        )

    def test_generic_routes_capacities_and_deterministic_optimization(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            order_path, network_path = directory / "orders.xlsx", directory / "network.json"
            self.write_orders(order_path)
            self.write_network(network_path)
            dataset = AffinityService(directory / "cache").load_orders(order_path)
            payload = self.payload()
            demand = self.service.build_demand(dataset, payload["assignments"])
            network = self.service.load_network(network_path)
            before = self.service.analyze(payload["assignments"], network, demand)
            result = self.service.optimize(payload, network, demand)
        self.assertEqual(before.metrics["peak_load"], 10)
        self.assertLess(result.after.metrics["peak_load"], before.metrics["peak_load"])
        self.assertEqual(
            result.before.metrics["relative_reference"],
            result.after.metrics["relative_reference"],
        )
        self.assertEqual({row["sku"] for row in result.assignments}, {"SKU_H", "SKU_L"})
        high = next(row for row in result.assignments if row["sku"] == "SKU_H")
        self.assertEqual(high["static_bay_id"], "BAY_COOL")
        self.assertTrue(result.relocations)

    def test_rmf_adapter_builds_generic_directed_resources(self):
        project = GridProject(
            GridSpec(2, 1, 1, "traffic", "L1"),
            {
                (0, 0): Marker("rack", "RACK_01"),
                (2, 1): Marker("workstation", "PACK_01"),
            },
        )
        network = self.service.network_from_rmf(project.to_building_dict())
        self.assertEqual(network.source_type, "embedded_rmf")
        self.assertIn("RACK_01", network.storage_nodes)
        self.assertEqual(network.endpoints[0].endpoint_id, "PACK_01")
        self.assertTrue(network.links)
        self.assertFalse(network.resource_capacities)

    def test_grid_project_json_can_be_loaded_as_movement_network(self):
        project = GridProject(
            GridSpec(2, 1, 1, "traffic-grid-json", "L1"),
            {
                (0, 0): Marker("rack", "RACK_01"),
                (2, 1): Marker("workstation", "PACK_01"),
            },
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "warehouse.grid.json"
            path.write_text(
                json.dumps(project.to_project_dict()),
                encoding="utf-8",
            )
            network = self.service.load_network(path)
        self.assertEqual(network.source_type, "grid_project_json")
        self.assertEqual(network.source_path, str(path.resolve()))
        self.assertIn("RACK_01", network.storage_nodes)
        self.assertEqual(network.endpoints[0].endpoint_id, "PACK_01")
        self.assertTrue(network.links)

    def test_hotspot_suggestion_changes_with_resource_distribution(self):
        first = self.service._suggest_hotspot_percentile([1, 1, 1, 10])
        second = self.service._suggest_hotspot_percentile([1, 1, 5, 5, 5])
        self.assertNotEqual(first, second)
        self.assertEqual(first, 75)
        self.assertEqual(second, 40)

    def test_full_pipeline_starts_from_raw_skus_and_building(self):
        project = GridProject(
            GridSpec(3, 1, 1, "pipeline", "L1"),
            {
                (0, 0): Marker("rack", "RACK_01"),
                (1, 0): Marker("rack", "RACK_02"),
                (3, 1): Marker("workstation", "PACK_01"),
            },
        )
        building = project.to_building_dict()
        skus = [
            {
                "sku": "SKU_H", "pick_frequency": 10,
                "velocity_class": "A", "sku_requirements": dict(self.requirements),
            },
            {
                "sku": "SKU_L", "pick_frequency": 1,
                "velocity_class": "C", "sku_requirements": dict(self.requirements),
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            order_path = directory / "orders.xlsx"
            self.write_orders(order_path)
            affinity_service = AffinityService(directory / "cache")
            affinity = affinity_service.analyze(
                affinity_service.load_orders(order_path)
            )
            result = self.service.run_full_pipeline(
                building, skus, affinity, self.service.network_from_rmf(building),
                affinity_weight=1.0, levels_per_rack=1, slots_per_level=2,
                attribute_catalog=self.catalog,
                optimize_traffic=True, source_orders=str(order_path),
                source_grid_project="/input/warehouse.grid.json",
            )
        self.assertEqual(result.basic_summary, {})
        self.assertEqual(result.affinity_summary["assigned_count"], 2)
        self.assertEqual(result.workflow_mode, "full_pipeline")
        self.assertEqual(result.initial_strategy, "abc_affinity")
        self.assertEqual(result.grouping_metrics["hard_validation_status"], "PASSED")
        self.assertIsNotNone(result.optimization)
        self.assertEqual(result.output_payload["strategy"], "abc_affinity")
        self.assertEqual(
            result.output_payload["traffic_analysis"]["unit_visits"],
            result.baseline_demand.unit_visits,
        )
        self.assertEqual(
            result.output_payload["traffic_configuration"]["workflow_mode"],
            "full_pipeline",
        )
        self.assertIn("pipeline", result.output_payload)
        self.assertEqual(
            result.pretraffic_payload["sources"]["grid_project_json"],
            "/input/warehouse.grid.json",
        )
        self.assertNotIn(
            "building_yaml", result.pretraffic_payload["sources"]
        )

    def test_full_pipeline_can_start_from_pure_abc(self):
        project = GridProject(
            GridSpec(3, 1, 1, "abc-pipeline", "L1"),
            {
                (0, 0): Marker("rack", "RACK_01"),
                (1, 0): Marker("rack", "RACK_02"),
                (3, 1): Marker("workstation", "PACK_01"),
            },
        )
        building = project.to_building_dict()
        skus = [
            {
                "sku": "SKU_H", "pick_frequency": 10,
                "velocity_class": "A",
                "sku_requirements": dict(self.requirements),
            },
            {
                "sku": "SKU_L", "pick_frequency": 1,
                "velocity_class": "C",
                "sku_requirements": dict(self.requirements),
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            order_path = directory / "orders.xlsx"
            self.write_orders(order_path)
            dataset = AffinityService(directory / "cache").load_orders(
                order_path
            )
            result = self.service.run_full_pipeline(
                building, skus, dataset,
                self.service.network_from_rmf(building),
                initial_strategy="basic",
                levels_per_rack=1,
                slots_per_level=2,
                attribute_catalog=self.catalog,
                source_orders=str(order_path),
            )
        self.assertEqual(result.initial_strategy, "basic")
        self.assertEqual(result.basic_summary["assigned_count"], 2)
        self.assertEqual(result.affinity_summary, {})
        self.assertEqual(result.output_payload["strategy"], "basic")
        self.assertEqual(
            result.output_payload["traffic_configuration"]["initial_strategy"],
            "basic",
        )

    def test_existing_layout_is_optimized_without_regeneration(self):
        payload = self.payload()
        payload.update({
            "strategy": "basic",
            "handling_unit_type": "Tote",
            "rack_capacity": {"levels": 1, "slots_per_level": 1},
        })
        baseline_membership = [
            (row["sku"], row["handling_unit_id"])
            for row in payload["assignments"]
        ]
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            order_path = directory / "orders.xlsx"
            network_path = directory / "network.json"
            self.write_orders(order_path)
            self.write_network(network_path)
            dataset = AffinityService(directory / "cache").load_orders(
                order_path
            )
            result = self.service.run_existing_layout(
                payload,
                dataset,
                self.service.load_network(network_path),
                baseline_path="/input/baseline.slotting.json",
                source_orders=str(order_path),
            )
        self.assertEqual(result.workflow_mode, "existing_layout")
        self.assertEqual(result.initial_strategy, "basic")
        self.assertEqual(
            [
                (row["sku"], row["handling_unit_id"])
                for row in result.pretraffic_payload["assignments"]
            ],
            baseline_membership,
        )
        self.assertEqual(
            result.output_payload["sources"]["traffic_baseline_layout"],
            "/input/baseline.slotting.json",
        )
        self.assertEqual(
            result.output_payload["traffic_configuration"]["workflow_mode"],
            "existing_layout",
        )

    def test_existing_layout_rejects_unassigned_skus(self):
        payload = self.payload()
        payload["assignments"][1]["assignment_status"] = (
            "UNASSIGNED_NO_COMPATIBLE_LOCATION"
        )
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            order_path = directory / "orders.xlsx"
            network_path = directory / "network.json"
            self.write_orders(order_path)
            self.write_network(network_path)
            dataset = AffinityService(directory / "cache").load_orders(
                order_path
            )
            with self.assertRaises(InsufficientStorageError):
                self.service.run_existing_layout(
                    payload,
                    dataset,
                    self.service.load_network(network_path),
                )

    def test_strict_oversize_uses_contiguous_slots_and_overweight_uses_level_two(self):
        project = GridProject(
            GridSpec(2, 1, 1, "physical", "L1"),
            {
                (0, 0): Marker("rack", "RACK_01"),
                (2, 1): Marker("workstation", "PACK_01"),
            },
        )
        location_capacity = {
            "Z01": {
                "chilled": False,
                "max_item_length": 10,
                # The 20-wide SKU is automatically spread across two slots.
                "max_item_width": 16,
                "max_item_height": 10,
                "max_item_weight": 400,
            }
        }
        skus = [
            {
                "sku": "SKU_OVERSIZE", "pick_frequency": 100,
                "velocity_class": "A",
                "sku_requirements": {
                    "chilled": False, "max_item_length": 10,
                    "max_item_width": 26, "max_item_height": 10,
                    "max_item_weight": 20,
                },
            },
            {
                "sku": "SKU_HEAVY", "pick_frequency": 90,
                "velocity_class": "A",
                "sku_requirements": {
                    "chilled": False, "max_item_length": 5,
                    "max_item_width": 5, "max_item_height": 5,
                    "max_item_weight": 500,
                },
            },
        ]
        rows, summary = self.service.slotting.generate_basic(
            project.to_building_dict(), skus, 2, 4, "AMR shelf",
            attribute_catalog=self.catalog,
            location_attributes=location_capacity,
            strict_compatibility=True,
            auto_plan_oversize=False,
        )
        oversize = next(row for row in rows if row["sku"] == "SKU_OVERSIZE")
        heavy = next(row for row in rows if row["sku"] == "SKU_HEAVY")
        self.assertEqual(oversize["occupied_slot_count"], 2)
        occupied_slots = [
            int(address.rsplit("S", 1)[-1])
            for address in oversize["occupied_static_addresses"]
        ]
        self.assertEqual(occupied_slots[1] - occupied_slots[0], 1)
        self.assertEqual(heavy["storage_level"], 2)
        self.assertEqual(summary["occupied_slot_count"], 3)

    def test_strict_oversize_is_automatically_slotted_in_standard_rack(self):
        project = GridProject(
            GridSpec(2, 1, 1, "physical", "L1"),
            {
                (0, 0): Marker("rack", "RACK_01"),
                (2, 1): Marker("workstation", "PACK_01"),
            },
        )
        oversize = {
            "sku": "SKU_OVERSIZE", "pick_frequency": 100,
            "velocity_class": "A",
            "sku_requirements": {
                "chilled": False, "max_item_length": 10,
                "max_item_width": 26, "max_item_height": 10,
                "max_item_weight": 20,
            },
        }
        rows, summary = self.service.slotting.generate_basic(
            project.to_building_dict(), [oversize], 1, 4, "AMR shelf",
            attribute_catalog=self.catalog,
            location_attributes={"Z01": {
                "chilled": False, "max_item_length": 10,
                "max_item_width": 16, "max_item_height": 10,
                "max_item_weight": 100,
            }},
            strict_compatibility=True,
            auto_plan_oversize=False,
        )
        self.assertEqual(rows[0]["assignment_status"], "ASSIGNED")
        self.assertEqual(rows[0]["occupied_slot_count"], 2)
        self.assertEqual(summary["unassigned_count"], 0)

    def test_oversize_can_span_levels_and_overweight_starts_at_level_two(self):
        project = GridProject(
            GridSpec(2, 1, 1, "vertical-oversize", "L1"),
            {
                (0, 0): Marker("rack", "RACK_01"),
                (2, 1): Marker("workstation", "PACK_01"),
            },
        )
        sku = {
            "sku": "SKU_TALL_HEAVY", "pick_frequency": 100,
            "velocity_class": "A",
            "sku_requirements": {
                "chilled": False, "max_item_length": 14,
                "max_item_width": 14, "max_item_height": 20,
                "max_item_weight": 500,
            },
        }
        rows, summary = self.service.slotting.generate_basic(
            project.to_building_dict(), [sku], 3, 4, "AMR shelf",
            attribute_catalog=self.catalog,
            location_attributes={"Z01": {
                "chilled": False, "max_item_length": 15,
                "max_item_width": 16, "max_item_height": 13,
                "max_item_weight": 250,
            }},
            strict_compatibility=True,
            auto_plan_oversize=False,
        )
        self.assertEqual(rows[0]["assignment_status"], "ASSIGNED")
        self.assertEqual(rows[0]["storage_level"], 2)
        self.assertEqual(rows[0]["occupied_level_span"], 2)
        self.assertEqual(rows[0]["occupied_horizontal_slot_span"], 1)
        self.assertEqual(rows[0]["occupied_slot_count"], 2)
        self.assertEqual(summary["occupied_slot_count"], 2)

    def test_full_pipeline_rejects_partial_layout_when_capacity_is_insufficient(self):
        project = GridProject(
            GridSpec(2, 1, 1, "capacity", "L1"),
            {
                (0, 0): Marker("rack", "RACK_01"),
                (2, 1): Marker("workstation", "PACK_01"),
            },
        )
        building = project.to_building_dict()
        skus = [
            {"sku": "SKU_H", "pick_frequency": 10, "velocity_class": "A",
             "sku_requirements": dict(self.requirements)},
            {"sku": "SKU_L", "pick_frequency": 1, "velocity_class": "C",
             "sku_requirements": dict(self.requirements)},
        ]
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            order_path = directory / "orders.xlsx"
            self.write_orders(order_path)
            affinity_service = AffinityService(directory / "cache")
            affinity = affinity_service.analyze(affinity_service.load_orders(order_path))
            with self.assertRaises(InsufficientStorageError) as caught:
                self.service.run_full_pipeline(
                    building, skus, affinity,
                    self.service.network_from_rmf(building),
                    levels_per_rack=1, slots_per_level=1,
                    attribute_catalog=self.catalog,
                    optimize_traffic=False,
                )
        self.assertEqual(caught.exception.summary["assigned_count"], 1)
        self.assertEqual(caught.exception.summary["unassigned_count"], 1)

    def test_full_pipeline_uses_dedicated_ambient_oversize_zone(self):
        project = GridProject(
            GridSpec(2, 1, 1, "oversize-pipeline", "L1"),
            {
                (0, 0): Marker("rack", "RACK_01"),
                (1, 0): Marker("rack", "RACK_02"),
                (2, 1): Marker("workstation", "PACK_01"),
            },
        )
        building = project.to_building_dict()
        oversize_requirements = {
            "chilled": False, "max_item_length": 10,
            "max_item_width": 26, "max_item_height": 10,
            "max_item_weight": 20,
        }
        skus = [
            {"sku": "SKU_H", "pick_frequency": 10, "velocity_class": "A",
             "sku_requirements": oversize_requirements},
            {"sku": "SKU_L", "pick_frequency": 1, "velocity_class": "C",
             "sku_requirements": dict(self.requirements)},
        ]
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            order_path = directory / "orders.xlsx"
            self.write_orders(order_path)
            affinity_service = AffinityService(directory / "cache")
            affinity = affinity_service.analyze(affinity_service.load_orders(order_path))
            result = self.service.run_full_pipeline(
                building, skus, affinity,
                self.service.network_from_rmf(building),
                levels_per_rack=1, slots_per_level=4,
                attribute_catalog=self.catalog,
                zone_assignments={"G0_0": "Z01", "G1_0": "Z02"},
                location_attributes={
                    zone: {
                        "chilled": False, "max_item_length": 10,
                        "max_item_width": 16, "max_item_height": 13,
                        "max_item_weight": 100,
                    }
                    for zone in ("Z01", "Z02")
                },
                optimize_traffic=False,
            )
        oversize = next(
            row for row in result.affinity_assignments if row["sku"] == "SKU_H"
        )
        standard = next(
            row for row in result.affinity_assignments if row["sku"] == "SKU_L"
        )
        self.assertEqual(result.affinity_summary["assigned_count"], 2)
        self.assertEqual(oversize["occupied_slot_count"], 1)
        self.assertEqual(oversize["planned_storage_type"], "OVERSIZE")
        self.assertEqual(standard["planned_storage_type"], "STANDARD")
        self.assertNotEqual(oversize["rack_id"], standard["rack_id"])

    def test_full_pipeline_places_known_overweight_inventory_on_level_two(self):
        project = GridProject(
            GridSpec(2, 1, 1, "overweight-pipeline", "L1"),
            {
                (0, 0): Marker("rack", "RACK_01"),
                (1, 0): Marker("rack", "RACK_02"),
                (2, 1): Marker("workstation", "PACK_01"),
            },
        )
        building = project.to_building_dict()
        skus = [
            {
                "sku": "SKU_H", "pick_frequency": 10, "velocity_class": "A",
                "sku_requirements": {
                    **self.requirements,
                    "max_item_weight": 500,
                },
            },
            {
                "sku": "SKU_L", "pick_frequency": 1, "velocity_class": "C",
                "sku_requirements": dict(self.requirements),
            },
            {
                "sku": "SKU_UNKNOWN", "pick_frequency": 0,
                "velocity_class": "C",
                "sku_requirements": {
                    **self.requirements,
                    "max_item_weight": 0,
                },
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            order_path = directory / "orders.xlsx"
            self.write_orders(order_path)
            affinity_service = AffinityService(directory / "cache")
            affinity = affinity_service.analyze(
                affinity_service.load_orders(order_path)
            )
            result = self.service.run_full_pipeline(
                building, skus, affinity,
                self.service.network_from_rmf(building),
                levels_per_rack=3, slots_per_level=2,
                attribute_catalog=self.catalog,
                zone_assignments={"G0_0": "Z01", "G1_0": "Z02"},
                location_attributes={
                    zone: dict(self.capacity) for zone in ("Z01", "Z02")
                },
                optimize_traffic=False,
            )
        heavy = next(
            row for row in result.affinity_assignments if row["sku"] == "SKU_H"
        )
        self.assertEqual(heavy["physical_storage_class"], "OVERWEIGHT")
        self.assertEqual(heavy["planned_storage_type"], "OVERSIZE")
        self.assertEqual(heavy["storage_level"], 2)
        unknown = next(
            row for row in result.affinity_assignments
            if row["sku"] == "SKU_UNKNOWN"
        )
        self.assertEqual(unknown["physical_missing_data_type"], "UNKNOWN_WEIGHT")
        self.assertEqual(unknown["physical_storage_class"], "UNKNOWN_WEIGHT")
        self.assertEqual(unknown["planned_storage_type"], "OVERSIZE")

    def test_full_pipeline_splits_chilled_standard_and_oversize_racks(self):
        project = GridProject(
            GridSpec(2, 1, 1, "chilled-pipeline", "L1"),
            {
                (0, 0): Marker("rack", "RACK_01"),
                (1, 0): Marker("rack", "RACK_02"),
                (2, 1): Marker("workstation", "PACK_01"),
            },
        )
        building = project.to_building_dict()
        skus = [
            {
                "sku": "SKU_H", "pick_frequency": 10, "velocity_class": "A",
                "sku_requirements": {
                    **self.requirements,
                    "chilled": True,
                    "max_item_length": 26,
                },
            },
            {
                "sku": "SKU_L", "pick_frequency": 1, "velocity_class": "C",
                "sku_requirements": {
                    **self.requirements,
                    "chilled": True,
                },
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            order_path = directory / "orders.xlsx"
            self.write_orders(order_path)
            affinity_service = AffinityService(directory / "cache")
            affinity = affinity_service.analyze(
                affinity_service.load_orders(order_path)
            )
            result = self.service.run_full_pipeline(
                building, skus, affinity,
                self.service.network_from_rmf(building),
                levels_per_rack=1, slots_per_level=2,
                attribute_catalog=self.catalog,
                location_attributes={
                    "Z01": {**self.capacity, "chilled": True},
                },
                optimize_traffic=False,
            )
        by_sku = {row["sku"]: row for row in result.affinity_assignments}
        standard = by_sku["SKU_L"]
        oversize = by_sku["SKU_H"]
        self.assertEqual(standard["planned_zone_id"], "Z01_chill_normal")
        self.assertEqual(oversize["planned_zone_id"], "Z01_chill_oversize")
        self.assertNotEqual(standard["rack_id"], oversize["rack_id"])
        self.assertTrue(standard["static_address"].startswith("Z01_chill_normal/"))
        self.assertTrue(oversize["static_address"].startswith("Z01_chill_oversize/"))

    def test_full_pipeline_uses_generated_buffers_and_reports_occupancy(self):
        project = GridProject(
            GridSpec(2, 1, 1, "buffer-pipeline", "L1"),
            {
                (0, 0): Marker("rack", "RACK_01"),
                (1, 0): Marker("rack", "RACK_02"),
                (2, 1): Marker("workstation", "PACK_01"),
            },
        )
        storage_layout = project.assign_storage_buffers("AMR", 1, 2)
        building = project.to_building_dict()
        skus = [
            {
                "sku": "SKU_H", "pick_frequency": 10, "velocity_class": "A",
                "sku_requirements": dict(self.requirements),
            },
            {
                "sku": "SKU_L", "pick_frequency": 1, "velocity_class": "C",
                "sku_requirements": dict(self.requirements),
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            order_path = directory / "orders.xlsx"
            self.write_orders(order_path)
            affinity_service = AffinityService(directory / "cache")
            affinity = affinity_service.analyze(
                affinity_service.load_orders(order_path)
            )
            result = self.service.run_full_pipeline(
                building, skus, affinity,
                self.service.network_from_rmf(building),
                levels_per_rack=1, slots_per_level=2,
                handling_unit_type="AMR shelf",
                attribute_catalog=self.catalog,
                storage_layout=storage_layout,
                optimize_traffic=False,
            )
        self.assertEqual(result.affinity_summary["buffer_count"], 2)
        self.assertEqual(result.affinity_summary["occupied_buffer_count"], 1)
        self.assertEqual(result.affinity_summary["buffer_occupancy_rate"], 0.5)
        self.assertEqual(
            sum(row["status"] == "OCCUPIED" for row in result.pretraffic_payload["buffers"]),
            1,
        )
        self.assertEqual(
            result.pretraffic_payload["storage_layout"], storage_layout.to_dict()
        )

    def test_strict_chilled_physical_and_missing_data_rules(self):
        payload = self.payload()
        first, second = payload["assignments"]
        second_capacity = payload["location_attributes"][second["static_address"]]
        second_capacity["chilled"] = True
        compatible, reason = self.service._strict_unit_compatibility([first], [second], payload)
        self.assertFalse(compatible)
        self.assertIn("Chilled", reason)
        second_capacity["chilled"] = False
        second_capacity["max_item_weight"] = 0.5
        compatible, reason = self.service._strict_unit_compatibility([first], [second], payload)
        self.assertFalse(compatible)
        self.assertIn("weight", reason.lower())
        del first["sku_requirements"]["max_item_height"]
        compatible, reason = self.service._strict_unit_compatibility([first], [second], payload)
        self.assertFalse(compatible)
        self.assertIn("incomplete", reason)

    def test_unmapped_and_unreachable_units_are_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            order_path, network_path = directory / "orders.xlsx", directory / "network.json"
            self.write_orders(order_path)
            self.write_network(network_path)
            payload = self.payload()
            payload["assignments"][1]["rack_id"] = "UNKNOWN"
            payload["assignments"][1]["rack_waypoint"] = "UNKNOWN"
            payload["assignments"][1]["pickup_dispenser_id"] = "UNKNOWN"
            payload["assignments"][1]["rack_vertex_index"] = 99
            dataset = AffinityService(directory / "cache").load_orders(order_path)
            demand = self.service.build_demand(dataset, payload["assignments"])
            analysis = self.service.analyze(
                payload["assignments"], self.service.load_network(network_path), demand
            )
        self.assertIn("UNIT_L", analysis.unmapped_units)

    def test_exports_and_layout_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            order_path, network_path = directory / "orders.xlsx", directory / "network.json"
            self.write_orders(order_path)
            self.write_network(network_path)
            payload = self.payload()
            dataset = AffinityService(directory / "cache").load_orders(order_path)
            demand = self.service.build_demand(dataset, payload["assignments"])
            network = self.service.load_network(network_path)
            result = self.service.optimize(payload, network, demand)
            paths = self.service.export(result, directory / "review.traffic.json", network)
            saved = self.service.result_payload(
                payload, result, baseline_path="baseline.slotting.json",
                order_path=str(order_path), network=network,
            )
            self.assertTrue(all(path.exists() for path in paths))
            self.assertEqual(json.loads(paths[0].read_text())["schema"], TRAFFIC_EXPORT_SCHEMA)
            self.assertEqual(saved["schema"], "inventory_slotting_layout/v2")
            self.assertEqual(saved["traffic_configuration"]["network_type"], "generic_json")
            self.assertEqual(saved["operation_log"][-1]["operation"], "traffic_aware_slotting")

    def test_cancellation_stops_optimization(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            order_path, network_path = directory / "orders.xlsx", directory / "network.json"
            self.write_orders(order_path)
            self.write_network(network_path)
            dataset = AffinityService(directory / "cache").load_orders(order_path)
            payload = self.payload()
            demand = self.service.build_demand(dataset, payload["assignments"])
            network = self.service.load_network(network_path)
            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                self.service.optimize(payload, network, demand, cancelled=lambda: True)


if __name__ == "__main__":
    unittest.main()
