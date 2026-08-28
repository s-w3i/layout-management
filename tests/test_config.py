"""Tests for application default paths."""

from __future__ import annotations

import unittest

from warehouse_layout.config import (
    DEFAULT_GLOBAL_TRAFFIC_OUTPUT,
    DEFAULT_MAP_DIR,
    DEFAULT_SLOTTING_OUTPUT,
    DEFAULT_TRAFFIC_OUTPUT,
)


class DefaultPathTests(unittest.TestCase):
    def test_all_layout_outputs_default_to_map_directory(self):
        self.assertEqual(DEFAULT_SLOTTING_OUTPUT.parent, DEFAULT_MAP_DIR)
        self.assertEqual(DEFAULT_TRAFFIC_OUTPUT.parent, DEFAULT_MAP_DIR)
        self.assertEqual(DEFAULT_GLOBAL_TRAFFIC_OUTPUT.parent, DEFAULT_MAP_DIR)


if __name__ == "__main__":
    unittest.main()
