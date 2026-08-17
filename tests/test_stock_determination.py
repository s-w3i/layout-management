"""Tests for reusable minimum-stock and buffer-stock determination."""

from __future__ import annotations

import csv
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from openpyxl import Workbook

from warehouse_layout.config import (
    DEFAULT_MACHINE_CAPACITY_BY_SYSTEM,
    DEFAULT_SLOT_CAPACITY,
)
from warehouse_layout.domain import StorageLayout
from warehouse_layout.stock_determination import (
    COMBINATION_CSV_FIELDS,
    CSV_FIELDS,
    calculate_rack_requirements,
    calculate_stock_requirements,
    determine_stock_requirements,
    write_attribute_combination_csv,
    write_stock_requirements_csv,
)


class StockDeterminationTests(unittest.TestCase):
    def test_requested_amr_and_slot_defaults(self):
        self.assertEqual(StorageLayout().levels_per_rack, 3)
        self.assertEqual(StorageLayout().slots_per_level, 4)
        self.assertEqual(DEFAULT_SLOT_CAPACITY, {
            "max_item_length": 1.9,
            "max_item_width": 0.5,
            "max_item_height": 1.0,
            "max_item_weight": 12.5,
        })
        self.assertEqual(
            DEFAULT_MACHINE_CAPACITY_BY_SYSTEM["AMR"]["max_item_weight"],
            150.0,
        )

    def test_calculates_combined_coverage_over_inclusive_calendar_span(self):
        rows = calculate_stock_requirements(
            [
                ("SKU_B", date(2026, 1, 1), 3),
                ("SKU_A", date(2026, 1, 1), 2),
                ("SKU_A", date(2026, 1, 3), 3),
            ],
            minimum_stock_days=2,
            buffer_stock_days=1,
        )

        self.assertEqual([row["sku"] for row in rows], ["SKU_A", "SKU_B"])
        sku_a = rows[0]
        self.assertEqual(sku_a["observation_days"], 3)
        self.assertEqual(sku_a["total_demand_ea"], 5)
        self.assertEqual(sku_a["average_daily_demand_ea"], 1.6667)
        self.assertEqual(sku_a["minimum_stock_ea"], 4)
        self.assertEqual(sku_a["minimum_buffer_stock_ea"], 1)
        self.assertEqual(sku_a["total_required_ea"], 5)

    def test_keeps_zero_demand_sku_and_rejects_invalid_days(self):
        rows = calculate_stock_requirements(
            [("SKU_ZERO", date(2026, 2, 1), -4)], 1, 0
        )
        self.assertEqual(rows[0]["minimum_stock_ea"], 0)
        self.assertEqual(rows[0]["minimum_buffer_stock_ea"], 0)
        with self.assertRaisesRegex(ValueError, "at least 1"):
            calculate_stock_requirements([], 0, 2)
        with self.assertRaisesRegex(ValueError, "cannot be negative"):
            calculate_stock_requirements([], 2, -1)

    def test_workbook_entry_point_and_csv_export(self):
        with tempfile.TemporaryDirectory() as directory:
            workbook_path = Path(directory) / "orders.xlsx"
            csv_path = Path(directory) / "requirements.csv"
            workbook = Workbook()
            sheet = workbook.active
            sheet.append(["Report title"])
            sheet.append(["Date", "Item or SKU", "Quantity (in EA)"])
            sheet.append([datetime(2026, 3, 1), 1001, 4])
            sheet.append([datetime(2026, 3, 2), 1001, 2])
            workbook.save(workbook_path)

            rows = determine_stock_requirements(workbook_path, 3, 2)
            output = write_stock_requirements_csv(rows, csv_path)
            with output.open(encoding="utf-8", newline="") as stream:
                exported = list(csv.DictReader(stream))

        self.assertEqual(rows[0]["sku"], "1001")
        self.assertEqual(rows[0]["minimum_stock_ea"], 9)
        self.assertEqual(rows[0]["minimum_buffer_stock_ea"], 6)
        self.assertEqual(tuple(exported[0]), CSV_FIELDS)
        self.assertEqual(exported[0]["total_required_ea"], "15")

    def test_rack_requirements_use_best_orientation_and_share_group_slots(self):
        stock_rows = [
            {"sku": "A", "total_required_ea": 17},
            {"sku": "B", "total_required_ea": 9},
        ]
        attributes = {
            "A": {
                "max_item_length": 4,
                "max_item_width": 2,
                "max_item_height": 5,
                "chilled": True,
            },
            "B": {
                "max_item_length": 5,
                "max_item_width": 5,
                "max_item_height": 5,
                "chilled": True,
            },
        }
        rows, groups = calculate_rack_requirements(
            stock_rows, attributes, (10, 8, 5), 2, 3, ("chilled",)
        )

        self.assertEqual(rows[0]["units_per_slot"], 10)
        self.assertEqual(rows[0]["required_slots"], 2)
        self.assertEqual(rows[0]["required_racks"], 1)
        self.assertEqual(rows[1]["units_per_slot"], 2)
        self.assertEqual(rows[1]["required_slots"], 5)
        self.assertEqual(groups[0]["required_slots"], 7)
        self.assertEqual(groups[0]["required_racks"], 2)
        self.assertEqual(groups[0]["attribute_combination"], "chilled=T")

    def test_rack_requirements_report_missing_or_non_fitting_dimensions(self):
        rows, groups = calculate_rack_requirements(
            [
                {"sku": "MISSING", "total_required_ea": 2},
                {"sku": "LARGE", "total_required_ea": 2},
            ],
            {
                "MISSING": {"max_item_length": 1},
                "LARGE": {
                    "max_item_length": 20,
                    "max_item_width": 20,
                    "max_item_height": 20,
                },
            },
            (10, 10, 10),
            1,
            1,
        )
        self.assertEqual(
            rows[0]["rack_calculation_status"],
            "CALCULATED_UNVERIFIED_DIMENSIONS",
        )
        self.assertEqual(rows[0]["required_slots"], 2)
        self.assertEqual(rows[1]["rack_calculation_status"], "ITEM_DOES_NOT_FIT")
        self.assertEqual(groups[0]["unresolved_skus"], 1)
        self.assertEqual(groups[0]["required_racks"], 2)

    def test_multi_slot_items_and_large_sku_quantities_can_use_multiple_racks(self):
        rows, groups = calculate_rack_requirements(
            [{"sku": "BULKY", "total_required_ea": 10}],
            {"BULKY": {
                "max_item_length": 0.4,
                "max_item_width": 0.6,
                "max_item_height": 0.2,
            }},
            (0.5, 0.3, 0.2),
            2,
            3,
        )
        self.assertEqual(rows[0]["rack_calculation_status"], "CALCULATED_MULTI_SLOT")
        self.assertEqual(rows[0]["slots_per_unit"], 2)
        self.assertEqual(rows[0]["required_slots"], 20)
        self.assertEqual(rows[0]["required_racks"], 5)
        self.assertEqual(groups[0]["required_racks"], 5)

    def test_slot_and_amr_rack_weight_raise_space_based_rack_requirement(self):
        rows, groups = calculate_rack_requirements(
            [{"sku": "HEAVY", "total_required_ea": 100}],
            {"HEAVY": {
                "max_item_length": 0.1,
                "max_item_width": 0.1,
                "max_item_height": 0.1,
                "max_item_weight": 2.0,
            }},
            (1.0, 1.0, 1.0),
            2,
            3,
            slot_max_weight=10.0,
            rack_max_weight=30.0,
        )

        self.assertEqual(rows[0]["units_per_slot"], 5)
        self.assertEqual(rows[0]["required_slots"], 20)
        self.assertEqual(rows[0]["required_racks"], 7)
        self.assertEqual(groups[0]["required_racks"], 7)
        self.assertNotIn("required_racks_by_slots", rows[0])
        self.assertNotIn("required_racks_by_weight", rows[0])
        self.assertNotIn("required_racks_by_slots", groups[0])
        self.assertNotIn("required_racks_by_weight", groups[0])

    def test_rack_packing_respects_indivisible_slot_load_weights(self):
        rows, groups = calculate_rack_requirements(
            [{"sku": "DENSE", "total_required_ea": 4}],
            {"DENSE": {
                "max_item_length": 1.0,
                "max_item_width": 1.0,
                "max_item_height": 1.0,
                "max_item_weight": 51.0,
            }},
            (1.0, 1.0, 1.0),
            1,
            6,
            slot_max_weight=60.0,
            rack_max_weight=100.0,
        )

        # Aggregate weight gives a lower bound of three racks, but each 51 kg
        # slot load needs a separate 100 kg rack.
        self.assertEqual(rows[0]["required_slots"], 4)
        self.assertEqual(rows[0]["required_racks"], 4)
        self.assertEqual(groups[0]["required_racks"], 4)

    def test_attribute_combination_csv_export(self):
        with tempfile.TemporaryDirectory() as directory:
            output = write_attribute_combination_csv(
                [{
                    "attribute_combination": "chilled=T",
                    "sku_count": 2,
                    "total_required_ea": 26,
                    "required_slots": 7,
                    "required_racks": 2,
                    "unresolved_skus": 0,
                }],
                Path(directory) / "groups.csv",
            )
            with output.open(encoding="utf-8", newline="") as stream:
                rows = list(csv.DictReader(stream))
        self.assertEqual(tuple(rows[0]), COMBINATION_CSV_FIELDS)
        self.assertEqual(rows[0]["required_racks"], "2")


if __name__ == "__main__":
    unittest.main()
