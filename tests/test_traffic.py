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
    AttributeDefinition,
    GridProject,
    GridSpec,
    Marker,
    StorageAttributeService,
    TrafficAwareSlottingService,
)
from warehouse_layout.affinity import AffinityService
from warehouse_layout.ctbsa import CtbsaParameters, CtbsaPlacementPlanner
from warehouse_layout.traffic import NETWORK_SCHEMA, TRAFFIC_EXPORT_SCHEMA


class TrafficAwareSlottingTests(unittest.TestCase):
    def setUp(self):
        self.attributes = StorageAttributeService()
        self.service = TrafficAwareSlottingService(
            self.attributes,
            ctbsa_parameters=CtbsaParameters(
                population_size=10,
                generations=5,
                random_seed=1,
            ),
        )
        self.catalog = self.attributes.serialize_catalog(
            self.attributes.starter_catalog()
        )
        self.requirements = {
            "chilled": False,
            "max_item_length": 0.1,
            "max_item_width": 0.1,
            "max_item_height": 0.1,
            "max_item_weight": 0.1,
        }
        self.capacity = {
            "chilled": False,
            "max_item_length": 1,
            "max_item_width": 1,
            "max_item_height": 1,
            "max_item_weight": 1,
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

    def run_small_ctbsa(
        self, directory: Path, *, cancelled=None, slots_per_level=2,
        initial_strategy="basic", workflow_mode="full_pipeline",
        zone_workload_enabled=False, with_zones=None,
    ):
        project = GridProject(
            GridSpec(3, 1, 1, "ctbsa", "L1"),
            {
                (0, 0): Marker("rack", "RACK_01"),
                (1, 0): Marker("rack", "RACK_02"),
                (3, 1): Marker("workstation", "PACK_01"),
            },
        )
        building = project.to_building_dict()
        order_path = directory / "orders.xlsx"
        self.write_orders(order_path)
        affinity_service = AffinityService(directory / "cache")
        analysis = affinity_service.analyze(
            affinity_service.load_orders(order_path)
        )
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
        network = self.service.network_from_rmf(building)
        with_zones = zone_workload_enabled if with_zones is None else with_zones
        pipeline = self.service.run_full_pipeline(
            building,
            skus,
            analysis,
            network,
            initial_strategy=initial_strategy,
            levels_per_rack=1,
            slots_per_level=slots_per_level,
            handling_unit_type="AMR shelf",
            zone_assignments=(
                {"G0_0": "Z01", "G1_0": "Z02"}
                if with_zones else None
            ),
            attribute_catalog=self.catalog,
            location_attributes=(
                {"Z01": dict(self.capacity), "Z02": dict(self.capacity)}
                if with_zones else {"Z01": dict(self.capacity)}
            ),
            zone_workload_enabled=zone_workload_enabled,
            workflow_mode=workflow_mode,
            cancelled=cancelled,
        )
        return pipeline, network, order_path

    def test_direct_pipeline_uses_physical_feasibility_not_abc(self):
        with tempfile.TemporaryDirectory() as directory:
            pipeline, _network, _orders = self.run_small_ctbsa(
                Path(directory),
                initial_strategy="physical_feasibility",
                workflow_mode="direct_ctbsa",
            )
        self.assertEqual(pipeline.workflow_mode, "direct_ctbsa")
        self.assertEqual(pipeline.initial_strategy, "physical_feasibility")
        self.assertTrue(
            pipeline.pretraffic_payload["feasibility_seed_only"]
        )
        self.assertEqual(
            pipeline.output_payload["pipeline"]["stages"][0],
            "physical_feasibility_seed",
        )
        self.assertEqual(
            pipeline.optimization.parameters["rack_budget_policy"],
            "paper_ctbsa_selected_solution",
        )
        self.assertEqual(
            pipeline.optimization.parameters["route_metrics_role"],
            "post_assignment_evaluation_only",
        )
        self.assertTrue(
            pipeline.optimization.parameters["rack_budget_trials"][-1]["selected"]
        )
        self.assertFalse(
            pipeline.optimization.parameters["zone_workload_enabled"]
        )

    def test_zone_workload_mode_reports_capacity_demand_and_traffic(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            baseline, _network, _orders = self.run_small_ctbsa(
                directory, with_zones=True
            )
            pipeline, _network, _orders = self.run_small_ctbsa(
                directory, zone_workload_enabled=True, with_zones=True
            )
            repeated, _network, _orders = self.run_small_ctbsa(
                directory, zone_workload_enabled=True, with_zones=True
            )
        result = pipeline.optimization
        self.assertTrue(result.parameters["zone_workload_enabled"])
        self.assertEqual(result.parameters["zone_optimization_status"], "ENABLED")
        self.assertEqual(
            {row["zone_id"] for row in result.after.zone_analysis["zones"]},
            {"Z01", "Z02"},
        )
        self.assertTrue(all(
            row["usable_slots"] > 0
            for row in result.after.zone_analysis["zones"]
        ))
        self.assertIn(
            "peak_normalized_zone_traffic",
            result.after.zone_analysis["metrics"],
        )
        exported = pipeline.output_payload["traffic_analysis"]["zone_analysis"]
        self.assertEqual(exported["after"], result.after.zone_analysis)
        before_clusters = sorted(
            sorted(row["inventory_load_ids"])
            for row in baseline.optimization.parameters["clusters"]
        )
        after_clusters = sorted(
            sorted(row["inventory_load_ids"])
            for row in result.parameters["clusters"]
        )
        self.assertEqual(
            sorted(load for cluster in before_clusters for load in cluster),
            sorted(load for cluster in after_clusters for load in cluster),
        )
        self.assertNotEqual(before_clusters, after_clusters)
        rack_zones = pipeline.pretraffic_payload["zone_assignments"]
        self.assertTrue(all(
            row["selected_zone"] == rack_zones.get(row["rack_id"], "Z01")
            for row in result.parameters["clusters"]
        ))
        selected_zone_by_load = {
            load_id: row["selected_zone"]
            for row in result.parameters["clusters"]
            for load_id in row["inventory_load_ids"]
        }
        self.assertTrue(all(
            selected_zone_by_load.get(row.get("inventory_load_id"))
            == rack_zones.get(row.get("rack_id"), "Z01")
            for row in result.assignments
            if row.get("inventory_load_id") in selected_zone_by_load
        ))
        self.assertEqual(
            result.parameters["objective_mode"],
            "three_objective_affinity_shelf_zone",
        )
        self.assertEqual(
            result.parameters["extended_selection"][0]["mode"],
            "automatic_knee",
        )
        self.assertTrue(result.parameters["pareto_frontier"])
        self.assertTrue(any(
            row["selected"] for row in result.parameters["pareto_frontier"]
        ))
        baseline_metrics = baseline.optimization.after.zone_analysis["metrics"]
        result_metrics = result.after.zone_analysis["metrics"]
        self.assertLessEqual(
            result_metrics["peak_normalized_zone_demand"],
            baseline_metrics["peak_normalized_zone_demand"],
        )
        positions = lambda value: sorted(
            (
                row.get("inventory_load_id"), row.get("rack_id"),
                row.get("storage_level"), row.get("storage_slot"),
            )
            for row in value.optimization.assignments
        )
        self.assertEqual(positions(pipeline), positions(repeated))

    def test_saved_run_restores_expected_resource_view_without_slotting(self):
        with tempfile.TemporaryDirectory() as directory:
            pipeline, network, _orders = self.run_small_ctbsa(
                Path(directory), zone_workload_enabled=True, with_zones=True
            )
        restored = self.service.restore_saved_result(
            pipeline.output_payload, network
        )
        self.assertTrue(restored.parameters["restored_saved_run"])
        self.assertEqual(
            restored.after.demand.unit_visits,
            pipeline.optimization.after.demand.unit_visits,
        )
        self.assertEqual(
            restored.after.metrics,
            pipeline.optimization.after.metrics,
        )
        self.assertEqual(
            restored.after.zone_analysis,
            pipeline.optimization.after.zone_analysis,
        )
        self.assertEqual(
            restored.assignments, pipeline.output_payload["assignments"]
        )
        self.assertTrue(restored.after.resources)

    def test_saved_run_requires_traffic_metadata(self):
        with self.assertRaisesRegex(ValueError, "no traffic analysis"):
            self.service.restore_saved_result(
                {"assignments": [{"sku": "A"}]},
                self.service.network_from_rmf(
                    GridProject(
                        GridSpec(1, 1, 1, "restore-invalid", "L1"),
                        {
                            (0, 0): Marker("rack", "RACK_01"),
                            (1, 1): Marker("workstation", "PACK_01"),
                        },
                    ).to_building_dict()
                ),
            )

    def test_ctbsa_uses_all_map_active_csv_attribute_profiles(self):
        project = GridProject(
            GridSpec(2, 1, 1, "ctbsa-attributes", "L1"),
            {
                (0, 0): Marker("rack", "RACK_FISH"),
                (1, 0): Marker("rack", "RACK_MEAT"),
                (2, 1): Marker("workstation", "PACK_01"),
            },
        )
        catalog = self.attributes.normalize_catalog(self.catalog)
        catalog["fish_area"] = AttributeDefinition(
            "fish_area", "Fish area", "boolean", "exact",
            hierarchy_level=1,
        )
        catalog["tablet"] = AttributeDefinition(
            "tablet", "Tablet", "boolean", "exact",
            hierarchy_level=2,
        )
        locations = {
            "Z_FISH": {**self.capacity, "fish_area": True},
            "Z_MEAT": dict(self.capacity),
        }
        skus = [
            {
                "sku": "SKU_H", "pick_frequency": 10,
                "velocity_class": "A",
                "sku_requirements": {
                    **self.requirements, "fish_area": True, "tablet": True,
                },
            },
            {
                "sku": "SKU_L", "pick_frequency": 1,
                "velocity_class": "C",
                "sku_requirements": {
                    **self.requirements, "fish_area": False, "tablet": False,
                },
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            order_path = directory / "orders.xlsx"
            self.write_orders(order_path)
            affinity_service = AffinityService(directory / "cache")
            analysis = affinity_service.analyze(
                affinity_service.load_orders(order_path)
            )
            building = project.to_building_dict()
            pipeline = self.service.run_full_pipeline(
                building,
                skus,
                analysis,
                self.service.network_from_rmf(building),
                initial_strategy="physical_feasibility",
                levels_per_rack=1,
                slots_per_level=1,
                handling_unit_type="AMR shelf",
                zone_assignments={"G0_0": "Z_FISH", "G1_0": "Z_MEAT"},
                attribute_catalog=self.attributes.serialize_catalog(catalog),
                location_attributes=locations,
                workflow_mode="direct_ctbsa",
            )

        parameters = pipeline.optimization.parameters
        self.assertIn("fish_area", parameters["active_zone_attribute_keys"])
        self.assertNotIn("tablet", parameters["active_zone_attribute_keys"])
        self.assertEqual(parameters["attribute_profile_count"], 2)
        profiles = {
            row["attribute_profile"]["fish_area"]
            for row in parameters["clusters"]
        }
        self.assertEqual(profiles, {False, True})
        for row in pipeline.optimization.assignments:
            if row.get("assignment_status") != "ASSIGNED":
                continue
            expected = locations[row["zone_id"]].get("fish_area", False)
            self.assertIs(row["sku_requirements"]["fish_area"], expected)

    def test_ctbsa_targets_quantity_inventory_loads_independently(self):
        project = GridProject(
            GridSpec(3, 1, 1, "ctbsa-quantity", "L1"),
            {
                (0, 0): Marker("rack", "RACK_01"),
                (1, 0): Marker("rack", "RACK_02"),
                (2, 0): Marker("rack", "RACK_03"),
                (3, 1): Marker("workstation", "PACK_01"),
            },
        )
        building = project.to_building_dict()
        rows, compact_summary = self.service.slotting.generate_basic(
            copy.deepcopy(building),
            [
                {
                    "sku": "SKU_H", "pick_frequency": 10,
                    "velocity_class": "A", "total_required_ea": 5,
                    "required_slots": 5, "slots_per_unit": 1,
                    "sku_requirements": dict(self.requirements),
                },
                {
                    "sku": "SKU_L", "pick_frequency": 1,
                    "velocity_class": "C", "total_required_ea": 1,
                    "required_slots": 1, "slots_per_unit": 1,
                    "sku_requirements": dict(self.requirements),
                },
            ],
            1, 3, "AMR shelf",
            attribute_catalog=self.catalog,
            location_attributes={"Z01": dict(self.capacity)},
        )
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            order_path = directory / "orders.xlsx"
            self.write_orders(order_path)
            affinity = AffinityService(directory / "cache")
            analysis = affinity.analyze(affinity.load_orders(order_path))
            plan = CtbsaPlacementPlanner(self.service.slotting).build(
                rows, copy.deepcopy(building), analysis,
                levels_per_rack=1, slots_per_level=3,
                attribute_catalog=self.catalog,
                location_attributes={"Z01": dict(self.capacity)},
                parameters=CtbsaParameters(
                    population_size=10, generations=5, random_seed=1,
                ),
            )

        hot_loads = {
            row["inventory_load_id"] for row in rows if row["sku"] == "SKU_H"
        }
        self.assertEqual(len(hot_loads), 5)
        self.assertTrue(hot_loads.issubset(plan.target_racks))
        self.assertGreater(
            len({plan.target_racks[load_id] for load_id in hot_loads}), 1
        )
        self.assertGreater(
            len(set(plan.target_racks.values())),
            compact_summary["final_occupied_rack_count"],
        )

    def test_unverified_oversize_one_slot_baseline_passes_validation(self):
        project = GridProject(
            GridSpec(2, 1, 1, "unverified-traffic", "L1"),
            {
                (0, 0): Marker("rack", "RACK_01"),
                (2, 1): Marker("workstation", "PACK_01"),
            },
        )
        locations = {
            "Z01": {
                **self.capacity,
                "oversize_capable": True,
            }
        }
        rows, _summary = self.service.slotting.generate_basic(
            project.to_building_dict(),
            [{
                "sku": "NO_VOLUME",
                "pick_frequency": 1,
                "velocity_class": "A",
                "sku_requirements": {"chilled": False},
            }],
            1,
            1,
            "AMR shelf",
            attribute_catalog=self.catalog,
            location_attributes=locations,
        )
        validation = self.service.validate_traffic_baseline({
            "assignments": rows,
            "rack_capacity": {"levels": 1, "slots_per_level": 1},
            "attribute_catalog": self.catalog,
            "location_attributes": locations,
        })

        self.assertEqual(rows[0]["assignment_status"], "ASSIGNED")
        self.assertEqual(rows[0]["compatibility_status"], "UNVERIFIED")
        self.assertEqual(validation["unverified_physical_sku_count"], 1)

    def test_store_day_demand_counts_each_unit_once(self):
        with tempfile.TemporaryDirectory() as directory:
            order_path = Path(directory) / "orders.xlsx"
            self.write_orders(order_path)
            dataset = AffinityService(Path(directory) / "cache").load_orders(order_path)
            demand = self.service.build_demand(dataset, self.payload()["assignments"])
        self.assertEqual(demand.fulfillment_groups, 10)
        self.assertEqual(demand.unit_visits, {"UNIT_H": 10, "UNIT_L": 1})
        self.assertEqual(demand.handling_unit_visits, 11)

    def test_replicated_sku_event_uses_one_quantity_balanced_source(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            order_path = directory / "one-order.xlsx"
            workbook = Workbook()
            sheet = workbook.active
            sheet.append(["Date", "Store ID", "Item or SKU", "Quantity (in EA)"])
            sheet.append([date(2026, 1, 1), "STORE_1", "SKU_H", 1])
            workbook.save(order_path)
            dataset = AffinityService(directory / "cache").load_orders(order_path)
            first = self.row("SKU_H", "UNIT_H_1", "BAY_HOT", 0)
            second = self.row("SKU_H", "UNIT_H_2", "BAY_COOL", 1)
            first.update({"inventory_load_id": "SKU_H#Q001", "quantity_ea": 3})
            second.update({"inventory_load_id": "SKU_H#Q002", "quantity_ea": 2})

            demand = self.service.build_demand(dataset, [first, second])

        self.assertEqual(demand.handling_unit_visits, 1)
        self.assertEqual(sum(demand.unit_visits.values()), 1)
        self.assertEqual(
            sum(demand.replica_visits["SKU_H"].values()), 1
        )
        self.assertEqual(
            demand.replica_assignment_policy,
            "traffic_balanced_alternative_source",
        )

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

    def test_generic_routes_and_capacities_are_analyzed(self):
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
        self.assertEqual(before.metrics["peak_load"], 10)
        self.assertTrue(before.metrics["capacity_mode"])
        self.assertEqual(before.metrics["expected_travel"], 11)

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
                location_attributes={"Z01": dict(self.capacity)},
                optimize_traffic=True, source_orders=str(order_path),
                source_grid_project="/input/warehouse.grid.json",
            )
        self.assertEqual(result.basic_summary, {})
        self.assertEqual(result.affinity_summary["assigned_count"], 2)
        self.assertEqual(result.workflow_mode, "full_pipeline")
        self.assertEqual(result.initial_strategy, "abc_affinity")
        self.assertEqual(result.grouping_metrics["hard_validation_status"], "PASSED")
        self.assertIsNotNone(result.optimization)
        self.assertEqual(result.output_payload["strategy"], "ctbsa")
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
                location_attributes={"Z01": dict(self.capacity)},
                source_orders=str(order_path),
            )
        self.assertEqual(result.initial_strategy, "basic")
        self.assertEqual(result.basic_summary["assigned_count"], 2)
        self.assertEqual(result.affinity_summary, {})
        self.assertEqual(result.output_payload["strategy"], "ctbsa")
        self.assertEqual(
            result.output_payload["traffic_configuration"]["initial_strategy"],
            "basic",
        )

    def test_existing_layout_is_reclustered_with_ctbsa(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            initial, network, order_path = self.run_small_ctbsa(directory)
            payload = initial.pretraffic_payload
            dataset = AffinityService(directory / "cache").load_orders(
                order_path
            )
            result = self.service.run_existing_layout(
                payload,
                dataset,
                network,
                baseline_path="/input/baseline.slotting.json",
                source_orders=str(order_path),
            )
        self.assertEqual(result.workflow_mode, "existing_layout")
        self.assertEqual(result.initial_strategy, "basic")
        self.assertEqual(result.output_payload["strategy"], "ctbsa")
        self.assertEqual(
            result.optimization.parameters["method"],
            "Lee_Chung_Yoon_2020_CTBSA",
        )
        self.assertEqual(
            result.output_payload["sources"]["traffic_baseline_layout"],
            "/input/baseline.slotting.json",
        )
        self.assertEqual(
            result.output_payload["traffic_configuration"]["workflow_mode"],
            "existing_layout",
        )

    def test_existing_layout_excludes_and_retains_unassigned_skus(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            initial, network, order_path = self.run_small_ctbsa(directory)
            payload = copy.deepcopy(initial.pretraffic_payload)
            target = next(row for row in payload["assignments"] if row["sku"] == "SKU_L")
            target["assignment_status"] = "UNASSIGNED_NO_COMPATIBLE_LOCATION"
            dataset = AffinityService(directory / "cache").load_orders(
                order_path
            )
            result = self.service.run_existing_layout(
                payload,
                dataset,
                network,
            )
        retained = next(
            row for row in result.optimization.assignments
            if row["sku"] == "SKU_L"
        )
        self.assertEqual(
            retained["assignment_status"],
            "UNASSIGNED_NO_COMPATIBLE_LOCATION",
        )
        self.assertEqual(
            result.grouping_metrics["excluded_unassigned_sku_count"], 1
        )

    def test_existing_layout_retains_multislot_physical_exception_rack(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            initial, network, order_path = self.run_small_ctbsa(
                directory, slots_per_level=1
            )
            payload = copy.deepcopy(initial.pretraffic_payload)
            fixed = next(
                row for row in payload["assignments"] if row["sku"] == "SKU_L"
            )
            original_address = fixed["storage_location_address"]
            fixed["occupied_slot_count"] = 2
            dataset = AffinityService(directory / "cache").load_orders(
                order_path
            )
            result = self.service.run_existing_layout(payload, dataset, network)
        retained = next(
            row for row in result.optimization.assignments
            if row["sku"] == "SKU_L"
        )
        self.assertEqual(retained["storage_location_address"], original_address)
        self.assertIn(
            "SKU_L",
            {row["handling_unit_id"] for row in result.optimization.rejected_units},
        )
        rejected = next(
            row for row in result.optimization.rejected_units
            if row["handling_unit_id"] == "SKU_L"
        )
        self.assertEqual(rejected["sku"], "SKU_L")
        self.assertIn("physical_storage_class", rejected)
        self.assertIn("fixed", rejected["reason"])
        self.assertEqual(
            result.optimization.parameters["hard_rule_profile"],
            "map_authoritative_warehouse_feasibility/v4",
        )
        self.assertEqual(
            result.optimization.parameters["hard_rule_validation"][
                "hard_validation_status"
            ],
            "PASSED",
        )
        self.assertEqual(retained["hard_rule_status"], "PASSED")
        self.assertIn("CONTIGUOUS_FOOTPRINT", retained["hard_rule_labels"])

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
                "oversize_capable": True,
                "max_item_length": 0.10,
                # The 3.2 m-wide SKU is automatically spread across two slots.
                "max_item_width": 1.6,
                "max_item_height": 0.10,
                "max_item_weight": 0.600,
            }
        }
        skus = [
            {
                "sku": "SKU_OVERSIZE", "pick_frequency": 100,
                "velocity_class": "A",
                "sku_requirements": {
                    "chilled": False, "max_item_length": 0.10,
                    "max_item_width": 3.2, "max_item_height": 0.10,
                    "max_item_weight": 0.020,
                },
            },
            {
                "sku": "SKU_HEAVY", "pick_frequency": 90,
                "velocity_class": "A",
                "sku_requirements": {
                    "chilled": False, "max_item_length": 0.05,
                    "max_item_width": 0.05, "max_item_height": 0.05,
                    "max_item_weight": 13.0,
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
        self.assertEqual(heavy["assignment_status"], "ASSIGNED")
        self.assertEqual(heavy["storage_level"], 2)
        self.assertEqual(summary["occupied_slot_count"], 3)

    def test_strict_oversize_is_rejected_from_standard_rack(self):
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
                "chilled": False, "max_item_length": 0.10,
                "max_item_width": 3.2, "max_item_height": 0.10,
                "max_item_weight": 0.020,
            },
        }
        rows, summary = self.service.slotting.generate_basic(
            project.to_building_dict(), [oversize], 1, 4, "AMR shelf",
            attribute_catalog=self.catalog,
            location_attributes={"Z01": {
                "chilled": False, "max_item_length": 0.10,
                "max_item_width": 1.6, "max_item_height": 0.10,
                "max_item_weight": 0.100,
            }},
            strict_compatibility=True,
            auto_plan_oversize=False,
        )
        self.assertEqual(
            rows[0]["assignment_status"],
            "UNASSIGNED_NO_COMPATIBLE_LOCATION",
        )
        self.assertEqual(rows[0]["occupied_slot_count"], 0)
        self.assertEqual(summary["unassigned_count"], 1)

    def test_quantity_copies_of_oversize_sku_use_separate_contiguous_racks(self):
        project = GridProject(
            GridSpec(3, 1, 1, "quantity-oversize", "L1"),
            {
                (0, 0): Marker("rack", "RACK_01"),
                (1, 0): Marker("rack", "RACK_02"),
                (2, 0): Marker("rack", "RACK_03"),
                (3, 1): Marker("workstation", "PACK_01"),
            },
        )
        sku = {
            "sku": "SKU_OVERSIZE",
            "pick_frequency": 100,
            "velocity_class": "A",
            "total_required_ea": 2,
            "units_per_slot": "",
            "slots_per_unit": 2,
            "required_slots": 4,
            "required_racks": 2,
            "sku_requirements": {
                "chilled": False,
                "max_item_length": 0.10,
                "max_item_width": 3.2,
                "max_item_height": 0.10,
                "max_item_weight": 0.020,
            },
        }
        rows, summary = self.service.slotting.generate_basic(
            project.to_building_dict(),
            [sku],
            1,
            3,
            "AMR shelf",
            attribute_catalog=self.catalog,
            location_attributes={
                "Z01": {
                    "chilled": False,
                    "oversize_capable": True,
                    "max_item_length": 0.10,
                    "max_item_width": 1.6,
                    "max_item_height": 0.10,
                    "max_item_weight": 0.100,
                }
            },
        )

        self.assertEqual(summary["inventory_load_count"], 2)
        self.assertEqual(len(rows), 2)
        self.assertEqual(len({row["rack_id"] for row in rows}), 2)
        self.assertTrue(all(row["occupied_slot_count"] == 2 for row in rows))
        for row in rows:
            slots = sorted(
                int(address.rsplit("S", 1)[-1])
                for address in row["occupied_static_addresses"]
            )
            self.assertEqual(slots[1] - slots[0], 1)

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
                "chilled": False, "max_item_length": 0.14,
                "max_item_width": 0.14, "max_item_height": 0.20,
                "max_item_weight": 0.500,
            },
        }
        rows, summary = self.service.slotting.generate_basic(
            project.to_building_dict(), [sku], 3, 4, "AMR shelf",
            attribute_catalog=self.catalog,
            location_attributes={"Z01": {
                "chilled": False, "max_item_length": 0.15,
                "max_item_width": 0.16, "max_item_height": 0.13,
                "max_item_weight": 0.250,
            }},
            strict_compatibility=True,
            auto_plan_oversize=False,
        )
        self.assertEqual(
            rows[0]["assignment_status"], "UNASSIGNED_NO_COMPATIBLE_LOCATION"
        )
        self.assertEqual(rows[0]["occupied_slot_count"], 0)
        self.assertEqual(summary["occupied_slot_count"], 0)

    def test_full_pipeline_proceeds_with_partial_layout(self):
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
            result = self.service.run_full_pipeline(
                building, skus, affinity,
                self.service.network_from_rmf(building),
                levels_per_rack=1, slots_per_level=1,
                attribute_catalog=self.catalog,
                location_attributes={"Z01": dict(self.capacity)},
                optimize_traffic=False,
            )
        self.assertEqual(result.baseline_summary["assigned_count"], 1)
        self.assertEqual(result.baseline_summary["unassigned_count"], 1)
        self.assertEqual(
            result.grouping_metrics["excluded_unassigned_sku_count"], 1
        )
        self.assertEqual(
            sum(
                row["assignment_status"] != "ASSIGNED"
                for row in result.pretraffic_payload["assignments"]
            ),
            1,
        )

    def test_full_pipeline_preserves_ambient_map_zones(self):
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
            "chilled": False, "max_item_length": 0.10,
            "max_item_width": 3.2, "max_item_height": 0.10,
            "max_item_weight": 0.020,
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
                    "Z01": {
                        "chilled": False, "max_item_length": 0.10,
                        "max_item_width": 1.6, "max_item_height": 0.13,
                        "max_item_weight": 0.100,
                    },
                    "Z02": {
                        "chilled": False, "oversize_capable": True,
                        "max_item_length": 0.10, "max_item_width": 1.6,
                        "max_item_height": 0.13, "max_item_weight": 0.100,
                    },
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
        self.assertEqual(oversize["occupied_slot_count"], 2)
        self.assertEqual(oversize["occupied_horizontal_slot_span"], 2)
        self.assertEqual(oversize["planned_storage_type"], "OVERSIZE")
        self.assertEqual(standard["planned_storage_type"], "STANDARD")
        self.assertEqual(result.affinity_summary["generated_attribute_zones"], {})
        self.assertEqual(
            set(result.affinity_summary["zone_assignments"].values()),
            {"Z01", "Z02"},
        )

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
                    "max_item_weight": 13.0,
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
        self.assertEqual(
            heavy["assignment_status"], "UNASSIGNED_NO_COMPATIBLE_LOCATION"
        )
        self.assertEqual(heavy["planned_storage_type"], "")
        unknown = next(
            row for row in result.affinity_assignments
            if row["sku"] == "SKU_UNKNOWN"
        )
        self.assertEqual(unknown["physical_missing_data_type"], "UNKNOWN_WEIGHT")
        self.assertEqual(unknown["physical_storage_class"], "UNKNOWN_WEIGHT")
        self.assertEqual(unknown["planned_storage_type"], "STANDARD")
        self.assertEqual(unknown["assignment_status"], "ASSIGNED")

    def test_full_pipeline_preserves_chilled_zone_without_splitting(self):
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
                    "max_item_length": 3.2,
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
                levels_per_rack=1, slots_per_level=4,
                attribute_catalog=self.catalog,
                location_attributes={
                    "Z01": {
                        **self.capacity,
                        "chilled": True,
                        "oversize_capable": True,
                    },
                },
                optimize_traffic=False,
            )
        by_sku = {row["sku"]: row for row in result.affinity_assignments}
        standard = by_sku["SKU_L"]
        oversize = by_sku["SKU_H"]
        self.assertEqual(standard["planned_storage_type"], "OVERSIZE")
        self.assertEqual(oversize["planned_storage_type"], "OVERSIZE")
        self.assertEqual(standard["zone_id"], oversize["zone_id"])
        self.assertEqual(result.affinity_summary["generated_attribute_zones"], {})
        self.assertTrue(standard["static_address"].startswith(standard["zone_id"] + "/"))
        self.assertTrue(oversize["static_address"].startswith(oversize["zone_id"] + "/"))

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
                location_attributes={"Z01": dict(self.capacity)},
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
        payload["location_attributes"]["Z01"] = dict(self.capacity)
        first, second = payload["assignments"]
        second_capacity = payload["location_attributes"][second["static_address"]]
        second_capacity["chilled"] = True
        compatible, reason = self.service._strict_unit_compatibility([first], [second], payload)
        self.assertFalse(compatible)
        self.assertIn("Chilled", reason)
        second_capacity["chilled"] = False
        second_capacity["max_item_weight"] = 0.05
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
            pipeline, network, order_path = self.run_small_ctbsa(directory)
            payload = pipeline.pretraffic_payload
            result = pipeline.optimization
            paths = self.service.export(result, directory / "review.traffic.json", network)
            saved = self.service.result_payload(
                payload, result, baseline_path="baseline.slotting.json",
                order_path=str(order_path), network=network,
            )
            self.assertTrue(all(path.exists() for path in paths))
            self.assertEqual(json.loads(paths[0].read_text())["schema"], TRAFFIC_EXPORT_SCHEMA)
            self.assertEqual(saved["schema"], "inventory_slotting_layout/v2")
            self.assertEqual(saved["traffic_configuration"]["network_type"], "embedded_rmf")
            self.assertEqual(saved["operation_log"][-1]["operation"], "ctbsa_slotting")

    def test_cancellation_stops_optimization(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                self.run_small_ctbsa(Path(directory), cancelled=lambda: True)


if __name__ == "__main__":
    unittest.main()
