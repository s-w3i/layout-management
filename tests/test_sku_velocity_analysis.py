"""Tests for physical-profile extraction and deterministic chilled demo data."""

from __future__ import annotations

import csv
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook

from sku_velocity_analysis import (
    chilled_demo_skus,
    classify_physical,
    read_transactions,
    write_chilled_demo,
)


class SkuVelocityPhysicalTests(unittest.TestCase):
    def test_workbook_physical_values_preserve_source_units_and_ignore_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.xlsx"
            workbook = Workbook()
            sheet = workbook.active
            sheet.append([
                "Date", "Item or SKU", "Quantity (in EA)",
                "Length", "Width", "Height", "Weight",
            ])
            sheet.append([datetime(2026, 1, 1), "SKU_1", 2, 10, 4, 6, 20])
            sheet.append([datetime(2026, 1, 2), "SKU_1", 3, 12, 0, 6, 25])
            workbook.save(path)
            rows = list(read_transactions(path))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][3]["max_item_length"], 10)
        self.assertIsNone(rows[1][3]["max_item_width"])
        self.assertEqual(rows[1][3]["max_item_weight"], 25)

    def test_physical_classification_boundaries(self):
        standard = {
            "max_item_length": 15,
            "max_item_width": 16,
            "max_item_height": 13,
            "max_item_weight": 250,
        }
        self.assertEqual(classify_physical(standard), ("COMPLETE", "STANDARD"))
        self.assertEqual(
            classify_physical({**standard, "max_item_length": 17})[1],
            "OVERSIZE",
        )
        self.assertEqual(
            classify_physical({**standard, "max_item_weight": 251})[1],
            "OVERWEIGHT",
        )
        self.assertEqual(
            classify_physical({
                **standard, "max_item_length": 17, "max_item_weight": 251,
            })[1],
            "OVERSIZE_AND_OVERWEIGHT",
        )
        self.assertEqual(
            classify_physical({**standard, "max_item_weight": None}),
            ("MISSING", "UNVERIFIED_OVERSIZE"),
        )

    def test_chilled_selection_is_deterministic_and_writes_selected_only(self):
        skus = [f"SKU_{index:04d}" for index in range(1524)]
        first = chilled_demo_skus(skus, 0.10, 42)
        second = chilled_demo_skus(reversed(skus), 0.10, 42)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 152)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "chilled.csv"
            count = write_chilled_demo(path, skus, 0.10, 42)
            with path.open(encoding="utf-8", newline="") as stream:
                rows = list(csv.DictReader(stream))
        self.assertEqual(count, 152)
        self.assertEqual(len(rows), 152)
        self.assertTrue(all(row["chilled_required"] == "true" for row in rows))


if __name__ == "__main__":
    unittest.main()
