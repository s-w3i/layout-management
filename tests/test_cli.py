"""Command-line controller tests."""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from warehouse_layout.cli import GridMapEditorCommand
from warehouse_layout.rmf import RmfMapService


class GridMapEditorCommandTests(unittest.TestCase):
    def test_generate_building_yaml(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "generated.building.yaml"
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                GridMapEditorCommand().run([
                    "--generate",
                    "--width", "4",
                    "--length", "3",
                    "--spacing", "1",
                    "--marker", "rack,1,1,RACK_01",
                    "--marker", "workstation,4,2,WS_01",
                    "--output", str(path),
                ])
            building = RmfMapService().load_building(path)
        self.assertIn("Generated 20 vertices", output.getvalue())
        self.assertEqual(building["coordinate_system"], "cartesian_meters")

    def test_invalid_grid_returns_parser_error(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                GridMapEditorCommand().run([
                    "--generate",
                    "--width", "20",
                    "--length", "15",
                    "--spacing", "3",
                ])


if __name__ == "__main__":
    unittest.main()
