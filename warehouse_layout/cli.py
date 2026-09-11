"""Command-line entry point for the warehouse grid map application."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from .config import DEFAULT_BUILDING_OUTPUT
from .domain import GridPosition, GridProject, GridSpec, Marker
from .rmf import RmfMapService


class GridMapEditorCommand:
    """Parse CLI arguments and start map generation or the desktop UI."""

    def __init__(self, rmf_maps: RmfMapService | None = None):
        self.rmf_maps = rmf_maps or RmfMapService()
        self.parser = self._build_parser()

    def _build_parser(self) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(
            description=(
                "Create an image-free, grid-based Open-RMF building map. "
                "Run without --generate to open the desktop editor."
            )
        )
        parser.add_argument(
            "--generate",
            action="store_true",
            help="Generate YAML without opening the GUI",
        )
        parser.add_argument("--width", type=float, default=20.0, help="Total warehouse width in metres")
        parser.add_argument("--length", type=float, default=15.0, help="Total warehouse length in metres")
        parser.add_argument("--spacing", type=float, default=1.0, help="Distance between grid points in metres")
        parser.add_argument(
            "--x-spacing", type=float,
            help="X-axis grid distance; defaults to --spacing",
        )
        parser.add_argument(
            "--y-spacing", type=float,
            help="Y-axis grid distance; defaults to --spacing",
        )
        parser.add_argument("--name", default="warehouse_grid", help="Building map name")
        parser.add_argument("--level", default="L1", help="RMF level name")
        parser.add_argument(
            "--marker",
            action="append",
            type=self.parse_marker,
            default=[],
            metavar="ROLE,COL,ROW,ID",
            help="Add rack or workstation marker; may be repeated",
        )
        parser.add_argument(
            "--output",
            type=Path,
            default=DEFAULT_BUILDING_OUTPUT,
            help="Output .building.yaml path",
        )
        return parser

    @staticmethod
    def parse_marker(value: str) -> tuple[GridPosition, Marker]:
        try:
            role, column, row, endpoint_id = value.split(",", 3)
            return (int(column), int(row)), Marker(role.strip(), endpoint_id.strip())
        except ValueError as exc:
            raise argparse.ArgumentTypeError(
                "marker must be ROLE,COLUMN,ROW,ENDPOINT_ID"
            ) from exc

    def run(self, argv: Sequence[str] | None = None) -> None:
        args = self.parser.parse_args(argv)
        project = GridProject(
            GridSpec(
                args.width,
                args.length,
                args.x_spacing if args.x_spacing is not None else args.spacing,
                args.name,
                args.level,
                args.y_spacing if args.y_spacing is not None else args.spacing,
            ),
            dict(args.marker),
        )
        try:
            project.validate()
        except ValueError as exc:
            self.parser.error(str(exc))

        if args.generate:
            self.rmf_maps.export_building(project, args.output)
            print(
                f"Generated {project.grid.vertex_count:,} vertices and "
                f"{project.grid.edge_count:,} bidirectional edges."
            )
            print("Bottom-left: G0_0 at (0, 0) metres")
            print(f"Output: {args.output}")
            return

        from .gui import run_gui

        run_gui(project)


def main(argv: Sequence[str] | None = None) -> None:
    GridMapEditorCommand().run(argv)
