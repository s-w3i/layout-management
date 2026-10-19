"""Regression cases for replica-aware affinity and complete-rack balancing."""

from types import SimpleNamespace
import unittest

import numpy as np

from warehouse_layout.slotting_strategies.affinity_support import (
    affinity_layout_metrics, affinity_placement_order, order_rack_touch_metrics,
)
from warehouse_layout.slotting_strategies.quantity_feasibility import QuantityFeasibilityPlan
from warehouse_layout.slotting_strategies.rack_balance import balance_rack_groups
import test_slotting_zone_workload as zone_fixture


def assigned(sku, rack, distance=1):
    return dict(sku=sku, rack_id=rack, assignment_status="ASSIGNED", quantity_ea=1,
                pick_frequency=1, velocity_class="A", average_workstation_distance_m=distance)


class AffinityRackGroupTests(unittest.TestCase):
    def test_joint_selection_ignores_redundant_single_sku_copy(self):
        analysis = SimpleNamespace(store_day_count=1)
        rows = [assigned("A", "R1"), assigned("A", "R2"), assigned("B", "R2")]
        metrics = order_rack_touch_metrics(analysis, rows, {"A": 1, "B": 1})
        self.assertEqual(metrics["total_rack_touches"], 1)
        self.assertEqual(metrics["rack_visit_counts"], {"R1": 0, "R2": 1})

    def test_missing_sku_is_reported_and_not_counted_as_fulfilled(self):
        metrics = order_rack_touch_metrics(SimpleNamespace(store_day_count=1), [assigned("A", "R1")], {"A": 1, "B": 1})
        self.assertEqual(metrics["uncovered_group_count"], 1)

    def test_replica_metrics_are_invariant_to_row_order(self):
        analysis = SimpleNamespace(dataset=SimpleNamespace(skus=("A", "B")),
                                   shared_store_days=np.ones((2, 2)), similarity=np.ones((2, 2)))
        rows = [assigned("A", "R1"), assigned("A", "R2"), assigned("B", "R1")]
        racks = [dict(rack_id="R1", x=0, y=0), dict(rack_id="R2", x=10, y=0)]
        metrics = affinity_layout_metrics(rows, racks, analysis, 1, 0, 1)
        self.assertEqual(metrics, affinity_layout_metrics(list(reversed(rows)), racks, analysis, 1, 0, 1))
        self.assertEqual(metrics["same_rack_affinity_fraction"], 1)
        self.assertEqual(metrics["occupied_rack_count"], 2)

    def test_logical_sku_grouping_precedes_extra_inventory_copies(self):
        rows = [dict(sku=sku, quantity_load_index=copy, inventory_load_id=f"{sku}{copy}")
                for sku in ("A", "B") for copy in (1, 2)]
        result = affinity_placement_order(rows, {}, 1, {"A": 1, "B": 1}, 2)
        self.assertEqual([(row["sku"], row["quantity_load_index"]) for row in result],
                         [("A", 1), ("B", 1), ("A", 2), ("B", 2)])

    def test_allocator_preserves_affinity_load_order_and_same_sku_limit(self):
        fixture = zone_fixture.ZoneWorkloadTests(); fixture.setUp()
        fixture.skus = [dict(row, required_slots=2, total_slotted_ea=2, total_required_ea=2)
                        for row in fixture.skus]
        rows, summary = fixture.generate("abc_affinity", zone_workload_enabled=True,
                                         maximum_same_sku_slots_per_rack=1)
        placed = sorted(rows, key=lambda row: row["placement_rank"])
        self.assertEqual([row["quantity_load_index"] for row in placed], [1] * 4 + [2] * 4)
        contents = {}
        for row in rows:
            contents.setdefault(row["rack_id"], []).append(row["sku"])
        self.assertTrue(all(len(skus) == len(set(skus)) for skus in contents.values()))
        self.assertEqual(summary["unassigned_count"], 0)
        self.assertEqual(summary["rack_workload"]["total_presentations"], 1)

    def test_balance_moves_complete_groups_and_reduces_zone_peak(self):
        fixture = zone_fixture.ZoneWorkloadTests(); fixture.setUp()
        level = fixture.building["levels"]["L1"]
        level["vertices"][2][0] = 2
        level["vertices"].extend([
            [2.1, 0, 0, "R3", {"pickup_dispenser": [1, "R3"]}],
            [3.1, 0, 0, "R4", {"pickup_dispenser": [1, "R4"]}],
        ])
        level["lanes"].extend([[0, 3, {"bidirectional": [4, True]}], [0, 4, {"bidirectional": [4, True]}]])
        fixture.zones = {"R1": "Z1", "R2": "Z1", "R3": "Z2", "R4": "Z2"}
        fixture.analysis.dataset.store_indices = np.array([0, 0, 1, 1])
        fixture.analysis.dataset.stores = ("first", "second")
        fixture.analysis = zone_fixture.AffinityService.analyze(fixture.analysis.dataset)
        rows, summary = fixture.generate("abc_affinity", zone_workload_enabled=True, slots_per_level=2)
        balance = summary["rack_zone_balance"]
        self.assertTrue(balance["accepted"])
        self.assertLess(balance["after"]["maximum_normalized_zone_work_seconds"],
                        balance["before"]["maximum_normalized_zone_work_seconds"])
        contents = {}
        for row in rows:
            contents.setdefault(row["rack_id"], set()).add(row["sku"])
            self.assertTrue(all(unit["rack_id"] == row["rack_id"] for unit in row["occupied_handling_units"]))
            self.assertTrue(row["storage_location_address"].startswith(row["zone_id"] + "/"))
        self.assertEqual({frozenset(skus) for skus in contents.values()}, {frozenset({"0", "1"}), frozenset({"2", "3"})})
        self.assertEqual(len({row["storage_location_address"] for row in rows}), 4)

    def test_single_oversize_load_cannot_bypass_same_sku_cell_limit(self):
        fixture = zone_fixture.ZoneWorkloadTests(); fixture.setUp()
        fixture.catalog["oversize_capable"] = zone_fixture.AttributeDefinition("oversize_capable", "Oversize capable", "boolean")
        for attributes in fixture.attributes.values():
            attributes["oversize_capable"] = True
        requirements = {key: .5 for key in zone_fixture.PHYSICAL_ATTRIBUTE_KEYS}
        requirements["max_item_width"] = 3
        fixture.skus = [dict(fixture.skus[0], required_slots=3, total_required_ea=1,
                             total_slotted_ea=1, slots_per_unit=3, sku_requirements=requirements)]
        rows, summary = fixture.generate("basic", maximum_same_sku_slots_per_rack=2)
        self.assertEqual(summary["unassigned_count"], 1)
        self.assertEqual(rows[0]["occupied_slot_count"], 0)

    def test_grouping_preserves_space_for_high_replica_inventory(self):
        fixture = zone_fixture.ZoneWorkloadTests(); fixture.setUp()
        level = fixture.building["levels"]["L1"]
        level["vertices"].extend([
            [20, 0, 0, "R3", {"pickup_dispenser": [1, "R3"]}],
            [30, 0, 0, "R4", {"pickup_dispenser": [1, "R4"]}],
        ])
        level["lanes"].extend([[0, 3, {"bidirectional": [4, True]}], [0, 4, {"bidirectional": [4, True]}]])
        fixture.zones.update(R3="Z1", R4="Z2")
        fixture.skus = [dict(fixture.skus[0], sku=str(index), required_slots=count,
                             total_slotted_ea=count, total_required_ea=count)
                        for index, count in enumerate((4, 1, 1, 1, 1))]
        dataset = fixture.analysis.dataset
        dataset.skus = tuple(str(index) for index in range(5))
        dataset.sku_indices = np.arange(5)
        dataset.dates = np.full(5, dataset.min_date.toordinal())
        dataset.store_indices = np.zeros(5, dtype=int)
        dataset.valid_rows = 5
        fixture.analysis = zone_fixture.AffinityService.analyze(dataset)
        rows, summary = fixture.generate("abc_affinity", slots_per_level=2,
                                         maximum_same_sku_slots_per_rack=1, zone_workload_enabled=True)
        self.assertEqual(summary["unassigned_count"], 0)
        self.assertEqual(len({row["rack_id"] for row in rows if row["sku"] == "0"}), 4)

    def test_grouping_preserves_combined_restricted_rack_capacity(self):
        fixture = zone_fixture.ZoneWorkloadTests(); fixture.setUp()
        level = fixture.building["levels"]["L1"]
        level["vertices"].extend([
            [20, 0, 0, "R3", {"pickup_dispenser": [1, "R3"]}],
            [30, 0, 0, "R4", {"pickup_dispenser": [1, "R4"]}],
        ])
        level["lanes"].extend([[0, 3, {"bidirectional": [4, True]}], [0, 4, {"bidirectional": [4, True]}]])
        fixture.zones = dict(R1="Z1", R2="Z1", R3="Z2", R4="Z2")
        fixture.catalog["restricted"] = zone_fixture.AttributeDefinition("restricted", "Restricted", "boolean")
        fixture.attributes["Z1"]["restricted"] = True
        fixture.attributes["Z2"]["restricted"] = False
        fixture.skus = [dict(fixture.skus[0], sku=str(index), pick_frequency=1000 if index >= 4 else 100,
                             required_slots=count, total_required_ea=count, total_slotted_ea=count,
                             sku_requirements={**fixture.skus[0]["sku_requirements"],
                                               **({"restricted": True} if index >= 4 else {})})
                        for index, count in enumerate((1, 1, 1, 1, 2, 2))]
        dataset = fixture.analysis.dataset
        dataset.skus = tuple(str(index) for index in range(6))
        dataset.sku_indices = np.array([0, 1, 2, 3, 4, 5, 0, 1, 2, 3])
        dataset.store_indices = np.array([0] * 6 + [1] * 4)
        dataset.stores = ("first", "second")
        dataset.dates = np.full(10, dataset.min_date.toordinal())
        dataset.valid_rows = 10
        fixture.analysis = zone_fixture.AffinityService.analyze(dataset)
        rows, summary = fixture.generate("abc_affinity", slots_per_level=2,
                                         maximum_same_sku_slots_per_rack=1, zone_workload_enabled=True)
        self.assertEqual(min(rows, key=lambda row: row["placement_rank"])["sku"], "0")
        self.assertEqual(summary["unassigned_count"], 0)
        self.assertEqual({row["rack_id"] for row in rows if row["sku"] in {"4", "5"}}, {"R1", "R2"})
        self.assertEqual({row["rack_id"] for row in rows if row["sku"] not in {"4", "5"}}, {"R3", "R4"})

    def test_future_reservation_exchange_preserves_whole_rack_weight(self):
        positions = [dict(rack_id=rack, level=1, slot=slot, distance_m=index, rack_rank=index,
                          effective_location_attributes={}, planned_storage_type="STANDARD")
                     for index, rack in enumerate(("R1", "R2"), 1) for slot in (1, 2)]
        rows = [dict(assigned(sku, position["rack_id"]), inventory_load_id=sku,
                     physical_storage_class="STANDARD", inventory_load_weight_kg=weight,
                     occupied_handling_units=[dict(rack_id=position["rack_id"], storage_level=1,
                                                   storage_slot=position["slot"])])
                for sku, weight, position in zip(("A", "D", "B", "C"), (9, 1, 1, 9), positions)]
        plan = QuantityFeasibilityPlan(rows, positions, 1, 10)
        self.assertIsNone(plan.propose("A", [positions[2]]))
        self.assertEqual(plan.propose("A", [positions[0]]), {})

    def test_verified_swap_balances_when_full_remap_would_add_a_visit(self):
        fixture = zone_fixture.ZoneWorkloadTests(); fixture.setUp()
        level = fixture.building["levels"]["L1"]
        level["vertices"][2][0] = 2
        for number in range(3, 7):
            level["vertices"].append([number, 0, 0, f"R{number}", {"pickup_dispenser": [1, f"R{number}"]}])
            level["lanes"].append([0, number, {"bidirectional": [4, True]}])
        fixture.zones = {f"R{number}": "Z1" if number <= 3 else "Z2" for number in range(1, 7)}
        fixture.skus = [dict(fixture.skus[0], sku=str(number)) for number in range(18)]
        rows, _summary = fixture.generate("basic", slots_per_level=3)
        positions = [dict(row, level=row["storage_level"], slot=row["storage_slot"],
                          waypoint=row["rack_waypoint"], vertex_index=row["rack_vertex_index"],
                          distance_m=row["average_workstation_distance_m"],
                          workstations=row["workstations_evaluated"].split("|")) for row in rows]
        skus = ("6", "3", "6", "2", "4", "1", "4", "0", "7", "0", "6", "6", "7", "1", "7", "2", "3", "1")
        rows = [dict(row, sku=sku) for row, sku in zip(rows, skus)]
        masks = {"0": 907, "1": 1017, "2": 502, "3": 196, "4": 973, "6": 959, "7": 706}
        final, summary = balance_rack_groups(rows, positions, masks, 10, "L1")
        self.assertEqual(summary["proposed"]["total_presentations"], 25)
        self.assertEqual(summary["before"]["total_presentations"], 24)
        self.assertEqual(summary["after"]["total_presentations"], 24)
        self.assertEqual(summary["selected_proposal"], "verified_pair_swap")
        self.assertLess(summary["after"]["maximum_normalized_zone_work_seconds"],
                        summary["before"]["maximum_normalized_zone_work_seconds"])
        groups = lambda data: sorted(sorted(row["inventory_load_id"] for row in data if row["rack_id"] == rack)
                                     for rack in {row["rack_id"] for row in data})
        self.assertEqual(groups(rows), groups(final))
        self.assertEqual(len({row["storage_location_address"] for row in final}), len(rows))


if __name__ == "__main__":
    unittest.main()
