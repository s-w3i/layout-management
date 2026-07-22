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

    def test_non_divisible_spacing_keeps_exact_outer_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "odd-spacing.building.yaml"
            GridMapEditorCommand().run([
                "--generate",
                "--width", "20",
                "--length", "15",
                "--spacing", "3",
                "--output", str(path),
            ])
            building = RmfMapService().load_building(path)
        vertices = next(iter(building["levels"].values()))["vertices"]
        self.assertEqual(len(vertices), 48)
        self.assertEqual(sorted({vertex[0] for vertex in vertices}), [0, 3, 6, 9, 12, 15, 18, 20])
        self.assertEqual(sorted({vertex[1] for vertex in vertices}), [0, 3, 6, 9, 12, 15])

    def test_independent_x_and_y_spacing_generate_rectangular_cells(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rectangular-grid.building.yaml"
            GridMapEditorCommand().run([
                "--generate",
                "--width", "8",
                "--length", "6",
                "--x-spacing", "2",
                "--y-spacing", "3",
                "--output", str(path),
            ])
            building = RmfMapService().load_building(path)
        vertices = next(iter(building["levels"].values()))["vertices"]
        self.assertEqual(sorted({vertex[0] for vertex in vertices}), [0, 2, 4, 6, 8])
        self.assertEqual(sorted({vertex[1] for vertex in vertices}), [0, 3, 6])


if __name__ == "__main__":
    unittest.main()
