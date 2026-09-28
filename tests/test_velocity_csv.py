import csv
from datetime import date
from pathlib import Path
import tempfile
import unittest

from openpyxl import Workbook

from warehouse_layout.sku_velocity_analysis import generate_velocity_csv
from warehouse_layout.slotting import SlottingService


class VelocityCsvTests(unittest.TestCase):
    def test_workbook_to_slotting_csv_and_invalid_input(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "orders.xlsx"
            output = Path(directory) / "new" / "velocity.csv"
            workbook = Workbook()
            sheet = workbook.active
            sheet.append(["Date", "Item or SKU", "Quantity (in EA)", "Weight (kg)"])
            for sku, count in (("A-SKU", 8), ("B-SKU", 1), ("C-SKU", 1)):
                for _ in range(count):
                    sheet.append([date(2026, 1, 1), sku, 2, 0.2])
            workbook.save(source)
            rows, _ = generate_velocity_csv(source, output, 0.8, 0.9)
            self.assertEqual([row["velocity_class"] for row in rows], ["A", "B", "C"])
            with output.open() as stream:
                saved = list(csv.DictReader(stream))
            self.assertEqual(saved[0]["total_quantity_ea"], "16.0")
            self.assertEqual(saved[0]["req_max_item_weight"], "0.2")
            self.assertEqual(len(SlottingService().load_velocity(output)), 3)
            before = output.read_bytes()
            with self.assertRaises(ValueError):
                generate_velocity_csv(source, output, 0.95, 0.8)
            sheet.delete_rows(2, 10)
            workbook.save(source)
            with self.assertRaisesRegex(ValueError, "No valid transactions"):
                generate_velocity_csv(source, output)
            self.assertEqual(output.read_bytes(), before)
