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
from generate_medicine_sku_attributes import generate as generate_medicine_attributes
from warehouse_layout.attributes import STANDARD_STORAGE_DEFAULTS, StorageAttributeService
from warehouse_layout.config import (
    DEFAULT_MACHINE_CAPACITY_BY_SYSTEM,
    DEFAULT_SKU_ATTRIBUTES_INPUT,
)
from warehouse_layout.slotting import SlottingService


class SkuVelocityPhysicalTests(unittest.TestCase):
    def test_metric_defaults_fit_every_complete_sku_within_one_rack(self):
        with DEFAULT_SKU_ATTRIBUTES_INPUT.open(
            encoding="utf-8-sig", newline=""
        ) as stream:
            source_rows = list(csv.DictReader(stream))
        service = StorageAttributeService()
        service.set_machine_carrying_capacity(
            DEFAULT_MACHINE_CAPACITY_BY_SYSTEM["AMR"], "AMR shelf"
        )
        profiles = [service.physical_profile(row) for row in source_rows]
        complete = [
            profile for profile in profiles
            if profile["data_status"] == "COMPLETE"
        ]
        footprints = [
            SlottingService.required_slot_footprint(
                profile["values"], STANDARD_STORAGE_DEFAULTS, 3, 4
            )
            for profile in complete
        ]
        self.assertTrue(all(footprint is not None for footprint in footprints))
        self.assertTrue(all(footprint == (1, 1) for footprint in footprints))
        self.assertFalse(any(
            profile["machine_overweight"] for profile in complete
        ))

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
        standard = dict(STANDARD_STORAGE_DEFAULTS)
        self.assertEqual(classify_physical(standard), ("COMPLETE", "STANDARD"))
        self.assertEqual(
            classify_physical({
                **standard,
                "max_item_length": standard["max_item_length"] + 0.1,
            })[1],
            "OVERSIZE",
        )
        self.assertEqual(
            classify_physical({
                **standard,
                "max_item_weight": standard["max_item_weight"] + 0.001,
            })[1],
            "OVERWEIGHT",
        )
        self.assertEqual(
            classify_physical({
                **standard,
                "max_item_length": standard["max_item_length"] + 0.1,
                "max_item_weight": standard["max_item_weight"] + 0.001,
            })[1],
            "OVERSIZE_AND_OVERWEIGHT",
        )
        self.assertEqual(
            classify_physical({**standard, "max_item_weight": None}),
            ("MISSING", "UNKNOWN_WEIGHT"),
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

    def test_medicine_attribute_generator_uses_workbook_sizes(self):
        with tempfile.TemporaryDirectory() as directory:
            workbook_path = Path(directory) / "medicine.xlsx"
            output_path = Path(directory) / "medicine_attributes.csv"
            workbook = Workbook()
            sheet = workbook.active
            sheet.append([
                "Date", "Item or SKU", "Quantity (in EA)",
                "Length", "Width", "Height", "Weight",
            ])
            sheet.append([datetime(2026, 1, 1), "MED_1", 1, 10, 4, 6, 20])
            sheet.append([datetime(2026, 1, 2), "MED_1", 1, 12, 3, 8, 25])
            workbook.save(workbook_path)
            rows = generate_medicine_attributes(
                workbook_path, output_path, seed=42
            )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["max_item_length"], 12)
        self.assertEqual(rows[0]["max_item_width"], 4)
        self.assertEqual(rows[0]["max_item_height"], 8)
        self.assertEqual(rows[0]["max_item_weight"], 25)
        self.assertIn(rows[0]["tablet"], {"true", "false"})
        self.assertIn(rows[0]["flammable"], {"true", "false"})
        self.assertNotIn("dosage_form", rows[0])


if __name__ == "__main__":
    unittest.main()
