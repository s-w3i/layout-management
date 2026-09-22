import unittest
from types import SimpleNamespace

from warehouse_layout.attributes import StorageAttributeService
from warehouse_layout.gui import GridMapEditorApp


class Value:
    def __init__(self):
        self.value = ""

    def set(self, value):
        self.value = value


class StorageDefaultsSyncTests(unittest.TestCase):
    def test_grid_defaults_update_stock_requirement_dimensions(self):
        defaults = {
            "max_item_length": 0.8,
            "max_item_width": 0.5,
            "max_item_height": 0.95,
            "max_item_weight": 12.5,
        }
        editor = SimpleNamespace(
            attributes=StorageAttributeService(),
            project=SimpleNamespace(
                warehouse_storage_defaults=defaults,
                machine_carrying_capacity={key: None for key in defaults},
                storage_layout=None,
            ),
            grid_warehouse_capacity_values={key: Value() for key in defaults},
            stock_slot_length=Value(),
            stock_slot_width=Value(),
            stock_slot_height=Value(),
            stock_slot_weight=Value(),
            grid_machine_capacity_values={key: Value() for key in defaults},
            update_machine_capacity_ui=lambda: None,
        )

        GridMapEditorApp.sync_grid_warehouse_storage_controls(editor)

        self.assertEqual(editor.stock_slot_length.value, "0.8")
        self.assertEqual(editor.stock_slot_width.value, "0.5")
        self.assertEqual(editor.stock_slot_height.value, "0.95")
        self.assertEqual(editor.stock_slot_weight.value, "12.5")


if __name__ == "__main__":
    unittest.main()
