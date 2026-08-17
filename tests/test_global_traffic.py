"""Independent global congestion-balanced slotting tests."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from openpyxl import Workbook

from warehouse_layout.affinity import AffinityService
from warehouse_layout.attributes import StorageAttributeService
from warehouse_layout.global_traffic import GlobalTrafficSlottingService
from warehouse_layout.traffic import (
    NETWORK_SCHEMA,
    MovementLink,
    MovementNetwork,
    MovementNode,
    ServiceEndpoint,
    TrafficAwareSlottingService,
)


class GlobalTrafficSlottingTests(unittest.TestCase):
    def setUp(self):
        self.attributes = StorageAttributeService()
        self.traffic = TrafficAwareSlottingService(self.attributes)
        self.service = GlobalTrafficSlottingService(self.traffic)
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

    def row(self, sku: str, unit: str, bay: str, vertex: int, zone: str):
        address = f"{zone}/A01/{bay}/L01/S01"
        return {
            "sku": sku,
            "velocity_class": "A" if sku == "SKU_H" else "C",
            "assignment_status": "ASSIGNED",
            "handling_unit_type": "AMR shelf",
            "handling_unit_id": unit,
            "dynamic_address_level": "shelf",
            "dynamic_address": f"SHELF-{unit}/L01/S01",
            "static_address": address,
            "storage_location_address": address,
            "buffer_id": bay,
            "buffer_level": "grid",
            "occupied_buffer_ids": [bay],
            "occupied_static_addresses": [address.rsplit("/L", 1)[0]],
            "occupied_storage_location_addresses": [address],
            "zone_id": zone,
            "planned_zone_id": zone,
            "aisle_id": "A01",
            "static_bay_id": bay,
            "rack_id": bay,
            "rack_waypoint": bay,
            "pickup_dispenser_id": bay,
            "rack_vertex_index": vertex,
            "rack_rank": vertex + 1,
            "storage_level": 1,
            "storage_slot": 1,
            "occupied_slot_count": 1,
            "occupied_level_span": 1,
            "occupied_horizontal_slot_span": 1,
            "occupied_handling_units": [{
                "handling_unit_id": unit,
                "rack_id": bay,
                "storage_level": 1,
                "storage_slot": 1,
                "static_address": address,
                "storage_location_address": address,
            }],
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
            self.row("SKU_H", "UNIT_H", "BAY_HOT_1", 0, "Z_HOT"),
            self.row("SKU_L1", "UNIT_L1", "BAY_HOT_2", 1, "Z_HOT"),
            self.row("SKU_L2", "UNIT_L2", "BAY_COOL", 2, "Z_COOL"),
        ]
        return {
            "schema": "inventory_slotting_layout/v2",
            "strategy": "basic",
            "handling_unit_type": "AMR shelf",
            "rack_capacity": {"levels": 1, "slots_per_level": 1},
            "assignments": rows,
            "building": {"levels": {}},
            "attribute_catalog": self.catalog,
            "location_attributes": {
                row["static_address"]: dict(self.capacity) for row in rows
            },
            "operation_log": [],
            "sources": {},
            "summary": {},
            "buffers": [],
        }

    @staticmethod
    def write_orders(path: Path):
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["Date", "Store ID", "Item or SKU", "Quantity (in EA)"])
        start = date(2026, 1, 1)
        for index in range(12):
            sheet.append([start + timedelta(days=index), "STORE_1", "SKU_H", 1])
        sheet.append([start, "STORE_1", "SKU_L1", 1])
        sheet.append([start, "STORE_2", "SKU_L2", 1])
        workbook.save(path)

    @staticmethod
    def write_network(path: Path):
        payload = {
            "schema": NETWORK_SCHEMA,
            "nodes": [
                {
                    "id": "hot_1", "x": 0, "y": 0, "kind": "storage",
                    "location_ids": ["BAY_HOT_1"],
                },
                {
                    "id": "hot_2", "x": 0, "y": 1, "kind": "storage",
                    "location_ids": ["BAY_HOT_2"],
                },
                {
                    "id": "cool", "x": 3, "y": 0, "kind": "storage",
                    "location_ids": ["BAY_COOL"],
                },
                {"id": "service", "x": 1, "y": 0, "kind": "endpoint"},
            ],
            "resources": [
                {"id": "narrow", "capacity": 1},
                {"id": "wide", "capacity": 20},
            ],
            "links": [
                {
                    "id": "hot1", "from": "hot_1", "to": "service",
                    "distance": 1, "resource_id": "narrow",
                },
                {
                    "id": "hot2", "from": "hot_2", "to": "service",
                    "distance": 1, "resource_id": "narrow",
                },
                {
                    "id": "cool_service", "from": "cool", "to": "service",
                    "distance": 1, "resource_id": "wide",
                },
            ],
            "endpoints": [
                {"id": "PACK", "node_id": "service", "weight": 1}
            ],
        }
        path.write_text(json.dumps(payload), encoding="utf-8")

    def run_case(self):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        orders, network_path = root / "orders.xlsx", root / "network.json"
        self.write_orders(orders)
        self.write_network(network_path)
        dataset = AffinityService(root / "cache").load_orders(orders)
        network = self.traffic.load_network(network_path)
        result = self.service.optimize_existing_layout(
            self.payload(),
            dataset,
            network,
            time_limit_seconds=10,
            relative_gap_limit=0,
            maximum_travel_increase=1.0,
            source_orders=str(orders),
        )
        return temporary, result

    def test_exact_global_assignment_balances_hot_location_without_empty_buffer(self):
        temporary, result = self.run_case()
        self.addCleanup(temporary.cleanup)
        placement = {
            row["handling_unit_id"]: row["static_bay_id"]
            for row in result.assignments
        }
        self.assertEqual(placement["UNIT_H"], "BAY_COOL")
        self.assertEqual(len(set(placement.values())), 3)
        self.assertEqual(len(result.relocations), 2)
        self.assertLess(
            result.after.metrics["peak_load"],
            result.before.metrics["peak_load"],
        )
        self.assertEqual(result.solver["status"], "OPTIMAL")
        self.assertTrue(result.solver["global_optimum_proven"])
        self.assertEqual(result.solver["relative_gap"], 0)

    def test_output_records_independent_workflow_and_solver_proof(self):
        temporary, result = self.run_case()
        self.addCleanup(temporary.cleanup)
        output = result.output_payload
        self.assertEqual(output["strategy"], "global_congestion_balanced")
        configuration = output["global_traffic_configuration"]
        self.assertEqual(configuration["workflow_mode"], "existing_layout")
        self.assertEqual(configuration["backend"], "OR-Tools CP-SAT")
        self.assertTrue(configuration["global_optimum_proven"])
        self.assertIn("global_traffic_analysis", output)
        self.assertEqual(
            output["operation_log"][-1]["operation"],
            "global_congestion_balanced_slotting",
        )

    def test_global_optimizer_excludes_and_retains_unassigned_sku(self):
        payload = self.payload()
        excluded = payload["assignments"][-1]
        excluded["assignment_status"] = (
            "UNASSIGNED_NO_COMPATIBLE_LOCATION"
        )
        excluded.update({
            "handling_unit_id": "",
            "static_address": "",
            "storage_location_address": "",
            "occupied_buffer_ids": [],
            "occupied_static_addresses": [],
            "occupied_storage_location_addresses": [],
            "occupied_handling_units": [],
        })
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            orders, network_path = root / "orders.xlsx", root / "network.json"
            self.write_orders(orders)
            self.write_network(network_path)
            dataset = AffinityService(root / "cache").load_orders(orders)
            result = self.service.optimize_existing_layout(
                payload,
                dataset,
                self.traffic.load_network(network_path),
                time_limit_seconds=10,
                maximum_travel_increase=1.0,
            )

        retained = next(
            row for row in result.assignments
            if row["sku"] == excluded["sku"]
        )
        self.assertEqual(
            retained["assignment_status"],
            "UNASSIGNED_NO_COMPATIBLE_LOCATION",
        )
        validation = result.solver["hard_validation"]
        self.assertEqual(validation["excluded_unassigned_sku_count"], 1)
        self.assertIn(
            excluded["sku"],
            validation["excluded_unassigned_sku_details"][0],
        )

    def test_unknown_physical_data_amr_shelf_can_move_as_complete_unit(self):
        payload = self.payload()
        high = payload["assignments"][0]
        high["sku_requirements"]["max_item_weight"] = None
        original = copy.deepcopy(high)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            orders, network_path = root / "orders.xlsx", root / "network.json"
            self.write_orders(orders)
            self.write_network(network_path)
            dataset = AffinityService(root / "cache").load_orders(orders)
            network = self.traffic.load_network(network_path)
            result = self.service.optimize_existing_layout(
                payload,
                dataset,
                network,
                time_limit_seconds=10,
                maximum_travel_increase=1.0,
            )
        optimized = next(
            row for row in result.assignments
            if row["handling_unit_id"] == "UNIT_H"
        )
        self.assertNotEqual(
            optimized["static_bay_id"], original["static_bay_id"]
        )

    def test_unknown_physical_data_non_amr_unit_remains_fixed(self):
        payload = self.payload()
        high = payload["assignments"][0]
        high["handling_unit_type"] = "Tote"
        high["sku_requirements"]["max_item_weight"] = None
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            orders, network_path = root / "orders.xlsx", root / "network.json"
            self.write_orders(orders)
            self.write_network(network_path)
            dataset = AffinityService(root / "cache").load_orders(orders)
            network = self.traffic.load_network(network_path)
            result = self.service.optimize_existing_layout(
                payload,
                dataset,
                network,
                time_limit_seconds=10,
                maximum_travel_increase=1.0,
            )
        optimized = next(
            row for row in result.assignments
            if row["handling_unit_id"] == "UNIT_H"
        )
        self.assertEqual(optimized["static_bay_id"], "BAY_HOT_1")

    def test_global_routes_do_not_cross_another_rack_grid(self):
        network = MovementNetwork(
            nodes={
                "rack_start": MovementNode(
                    "rack_start", 0, 0, "storage"
                ),
                "rack_block": MovementNode(
                    "rack_block", 1, 0, "storage"
                ),
                "detour": MovementNode("detour", 0, 1, "transit"),
                "service": MovementNode("service", 2, 0, "endpoint"),
            },
            links=(
                MovementLink(
                    "into_rack", "rack_start", "rack_block",
                    1, "rack_entry",
                ),
                MovementLink(
                    "through_rack", "rack_block", "service",
                    1, "rack_exit",
                ),
                MovementLink(
                    "detour_1", "rack_start", "detour",
                    2, "open_aisle_1",
                ),
                MovementLink(
                    "detour_2", "detour", "service",
                    2, "open_aisle_2",
                ),
            ),
            storage_nodes={
                "START": "rack_start",
                "BLOCK": "rack_block",
            },
            endpoints=(ServiceEndpoint("service", 1, "PACK"),),
            source_type="test",
        )

        routes = self.service._terminal_only_rack_routes(network)

        self.assertEqual(
            routes[("rack_start", "service")],
            (
                4,
                ("detour_1", "detour_2"),
                ("open_aisle_1", "open_aisle_2"),
            ),
        )
        self.assertEqual(
            routes[("rack_block", "service")],
            (1, ("through_rack",), ("rack_exit",)),
        )

    def test_global_route_may_end_at_a_rack_grid(self):
        network = MovementNetwork(
            nodes={
                "rack_start": MovementNode(
                    "rack_start", 0, 0, "storage"
                ),
                "rack_end": MovementNode(
                    "rack_end", 1, 0, "storage"
                ),
            },
            links=(
                MovementLink(
                    "rack_to_rack", "rack_start", "rack_end",
                    1, "terminal_approach",
                ),
            ),
            storage_nodes={
                "START": "rack_start",
                "END": "rack_end",
            },
            endpoints=(ServiceEndpoint("rack_end", 1, "END_TASK"),),
            source_type="test",
        )

        routes = self.service._terminal_only_rack_routes(network)

        self.assertEqual(
            routes[("rack_start", "rack_end")],
            (1, ("rack_to_rack",), ("terminal_approach",)),
        )

    def test_first_solver_stage_receives_feasibility_budget(self):
        self.assertEqual(
            self.service._stage_time_budget(90, 1, 8), 60
        )
        self.assertEqual(
            self.service._stage_time_budget(180, 1, 8), 72
        )
        self.assertEqual(
            self.service._stage_time_budget(480, 1, 8), 120
        )
        self.assertAlmostEqual(
            self.service._stage_time_budget(108, 2, 8),
            108 / 7,
        )

    def test_compatible_empty_amr_buffer_is_a_candidate_destination(self):
        payload = self.payload()
        payload["building"] = {
            "coordinate_system": "cartesian_meters",
            "levels": {
                "L1": {
                    "vertices": [
                        [0, 0, 0, "BAY_HOT_1", {
                            "pickup_dispenser": "BAY_HOT_1"
                        }],
                        [0, 1, 0, "BAY_HOT_2", {
                            "pickup_dispenser": "BAY_HOT_2"
                        }],
                        [3, 0, 0, "BAY_COOL", {
                            "pickup_dispenser": "BAY_COOL"
                        }],
                        [3, 1, 0, "BAY_EMPTY", {
                            "pickup_dispenser": "BAY_EMPTY"
                        }],
                        [1, 0, 0, "service", {
                            "dropoff_ingestor": "PACK"
                        }],
                    ],
                    "lanes": [
                        [0, 4, {"bidirectional": True}],
                        [1, 4, {"bidirectional": True}],
                        [2, 4, {"bidirectional": True}],
                        [3, 4, {"bidirectional": True}],
                    ],
                }
            },
        }
        payload["zone_assignments"] = {
            "BAY_HOT_1": "Z_HOT",
            "BAY_HOT_2": "Z_HOT",
            "BAY_COOL": "Z_COOL",
            "BAY_EMPTY": "Z_COOL",
        }
        payload["storage_layout"] = {
            "system_type": "AMR",
            "buffer_level": "grid",
            "handling_unit_type": "AMR shelf",
            "levels_per_rack": 1,
            "slots_per_level": 1,
            "buffers": [{
                "buffer_id": "BAY_EMPTY",
                "grid_waypoint": "BAY_EMPTY",
                "rack_endpoint_id": "BAY_EMPTY",
                "buffer_level": "grid",
                "status": "EMPTY",
            }],
        }
        payload["buffers"] = copy.deepcopy(
            payload["storage_layout"]["buffers"]
        )
        payload["location_attributes"][
            "Z_COOL/A01/BAY_EMPTY/L01/S01"
        ] = dict(self.capacity)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            orders, network_path = root / "orders.xlsx", root / "network.json"
            self.write_orders(orders)
            self.write_network(network_path)
            network_payload = json.loads(
                network_path.read_text(encoding="utf-8")
            )
            network_payload["nodes"].insert(3, {
                "id": "empty",
                "x": 3,
                "y": 1,
                "kind": "storage",
                "location_ids": ["BAY_EMPTY"],
            })
            network_payload["links"].append({
                "id": "empty_service",
                "from": "empty",
                "to": "service",
                "distance": 1,
                "resource_id": "wide_aisle",
            })
            network_path.write_text(
                json.dumps(network_payload), encoding="utf-8"
            )
            dataset = AffinityService(root / "cache").load_orders(orders)
            network = self.traffic.load_network(network_path)
            result = self.service.optimize_existing_layout(
                payload,
                dataset,
                network,
                time_limit_seconds=10,
                maximum_travel_increase=1.0,
            )
        self.assertIn(
            "BAY_EMPTY",
            {
                row["static_bay_id"]
                for row in result.assignments
                if row["assignment_status"] == "ASSIGNED"
            },
        )
        self.assertEqual(
            result.solver["empty_candidate_location_count"], 1
        )
        self.assertGreaterEqual(len(result.relocations), 1)
        self.assertEqual(
            result.solver["acceptance_guard"]["status"], "PASSED"
        )

    def test_standalone_existing_layout_cli_writes_global_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            orders = root / "orders.xlsx"
            network_path = root / "network.json"
            layout_path = root / "baseline.slotting.json"
            output_path = root / "global.slotting.json"
            self.write_orders(orders)
            self.write_network(network_path)
            layout_path.write_text(
                json.dumps(self.payload()), encoding="utf-8"
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    "global_traffic_slotting.py",
                    "--mode", "existing",
                    "--layout", str(layout_path),
                    "--orders", str(orders),
                    "--network", str(network_path),
                    "--output", str(output_path),
                    "--time-limit", "10",
                    "--max-travel-increase-percent", "100",
                ],
                cwd=Path(__file__).resolve().parent.parent,
                check=True,
                text=True,
                capture_output=True,
            )
            summary = json.loads(completed.stdout)
            output = json.loads(output_path.read_text(encoding="utf-8"))
        self.assertEqual(summary["solver_status"], "OPTIMAL")
        self.assertTrue(summary["global_optimum_proven"])
        self.assertEqual(output["strategy"], "global_congestion_balanced")


if __name__ == "__main__":
    unittest.main()
