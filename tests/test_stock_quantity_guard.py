import unittest

from warehouse_layout.slotting import SlottingService
from warehouse_layout.slotting_strategies.allocation import expand_quantity_slot_loads
from warehouse_layout.ctbsa import cluster_capacities


class StockQuantityGuardTests(unittest.TestCase):
    def test_missing_or_partial_targets_raise_alert(self):
        skus = [{"sku": "A"}, {"sku": "B"}]
        for stock in ([], [{"sku": "A", "total_required_ea": 20, "required_slots": 3}]):
            with self.assertRaisesRegex(ValueError, "Calculate Stock Requirements"):
                SlottingService.apply_stock_requirements(skus, stock, require_complete=True)

    def test_unresolved_targets_cannot_fall_back_to_one_load(self):
        for quantity, slots in [(10, ""), ("", 1), (10, 0), (-1, 1), (float("nan"), 1)]:
            with self.subTest(quantity=quantity, slots=slots):
                with self.assertRaises(ValueError):
                    SlottingService.apply_stock_requirements(
                        [{"sku": "A"}], [{"sku": "A", "total_required_ea": quantity, "required_slots": slots}],
                        require_complete=True,
                    )

    def test_quantity_expansion_drives_minimum_rack_count(self):
        rows = SlottingService.apply_stock_requirements(
            [{"sku": "A"}, {"sku": "B"}],
            [{"sku": "A", "total_required_ea": 20, "required_slots": 5, "slots_per_unit": 1},
             {"sku": "B", "total_required_ea": 0, "required_slots": 0}],
            require_complete=True,
        )
        loads, summary = expand_quantity_slot_loads(rows)
        self.assertEqual(len(loads), 5)
        self.assertEqual(sum(row["quantity_ea"] for row in loads), 20)
        self.assertEqual(summary["zero_required_sku_count"], 1)
        self.assertEqual(cluster_capacities(len(loads), 10, 2, minimize_rack_count=True), [2, 2, 2])
        # A quantity-enriched CSV is also valid without in-memory stock rows.
        self.assertEqual(SlottingService.apply_stock_requirements(rows, [], require_complete=True), rows)


if __name__ == "__main__":
    unittest.main()
