"""Grid-map domain objects independent of persistence and user interfaces."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Dict, Iterable, Tuple

from .config import PROJECT_SCHEMA


GridPosition = Tuple[int, int]


@dataclass
class GridSpec:
    width_m: float = 20.0
    length_m: float = 15.0
    spacing_m: float = 1.0
    map_name: str = "warehouse_grid"
    level_name: str = "L1"

    def validate(self) -> None:
        for label, value in (
            ("width", self.width_m),
            ("length", self.length_m),
            ("grid spacing", self.spacing_m),
        ):
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{label} must be greater than zero")
        if not self.map_name.strip() or not self.level_name.strip():
            raise ValueError("map and level names cannot be blank")
        for label, value in (("width", self.width_m), ("length", self.length_m)):
            cells = value / self.spacing_m
            if not math.isclose(cells, round(cells), abs_tol=1e-7):
                raise ValueError(f"{label} must be an exact multiple of grid spacing")
        if self.vertex_count > 10_000:
            raise ValueError(f"grid contains {self.vertex_count:,} vertices; maximum is 10,000")

    @property
    def columns(self) -> int:
        return round(self.width_m / self.spacing_m)

    @property
    def rows(self) -> int:
        return round(self.length_m / self.spacing_m)

    @property
    def vertex_count(self) -> int:
        return (self.columns + 1) * (self.rows + 1)

    @property
    def edge_count(self) -> int:
        return self.columns * (self.rows + 1) + self.rows * (self.columns + 1)


@dataclass
class Marker:
    role: str
    endpoint_id: str

    def validate(self) -> None:
        if self.role not in {"rack", "workstation"}:
            raise ValueError(f"unsupported marker role: {self.role}")
        if not self.endpoint_id.strip():
            raise ValueError("endpoint ID cannot be blank")


@dataclass
class GridProject:
    grid: GridSpec = field(default_factory=GridSpec)
    markers: Dict[GridPosition, Marker] = field(default_factory=dict)

    def validate(self) -> None:
        self.grid.validate()
        endpoint_ids = []
        for (column, row), marker in self.markers.items():
            if not (0 <= column <= self.grid.columns and 0 <= row <= self.grid.rows):
                raise ValueError(f"marker ({column}, {row}) is outside the grid")
            marker.validate()
            endpoint_ids.append(marker.endpoint_id)
        duplicates = sorted({item for item in endpoint_ids if endpoint_ids.count(item) > 1})
        if duplicates:
            raise ValueError(f"duplicate endpoint IDs: {', '.join(duplicates)}")

    def vertex_index(self, column: int, row: int) -> int:
        return row * (self.grid.columns + 1) + column

    def vertex_name(self, column: int, row: int) -> str:
        return f"G{column}_{row}"

    def iter_positions(self) -> Iterable[GridPosition]:
        for row in range(self.grid.rows + 1):
            for column in range(self.grid.columns + 1):
                yield column, row

    def to_project_dict(self) -> dict:
        return {
            "schema": PROJECT_SCHEMA,
            "grid": asdict(self.grid),
            "markers": [
                {"column": column, "row": row, **asdict(marker)}
                for (column, row), marker in sorted(
                    self.markers.items(), key=lambda item: (item[0][1], item[0][0])
                )
            ],
        }

    @classmethod
    def from_project_dict(cls, data: dict) -> "GridProject":
        if data.get("schema") != PROJECT_SCHEMA:
            raise ValueError("not a supported RMF grid project file")
        project = cls(grid=GridSpec(**data["grid"]))
        for item in data.get("markers", []):
            position = (int(item["column"]), int(item["row"]))
            project.markers[position] = Marker(item["role"], item["endpoint_id"])
        project.validate()
        return project

    def to_building_dict(self) -> dict:
        self.validate()
        vertices = []
        for column, row in self.iter_positions():
            x = round(column * self.grid.spacing_m, 9)
            y = round(row * self.grid.spacing_m, 9)
            vertex = [x, y, 0, self.vertex_name(column, row)]
            marker = self.markers.get((column, row))
            if marker:
                key = "pickup_dispenser" if marker.role == "rack" else "dropoff_ingestor"
                vertex.append({key: [1, marker.endpoint_id]})
            vertices.append(vertex)

        lane_parameters = {
            "bidirectional": [4, True],
            "demo_mock_floor_name": [1, ""],
            "demo_mock_lift_name": [1, ""],
            "graph_idx": [2, 0],
            "mutex": [1, ""],
            "orientation": [1, ""],
            "speed_limit": [3, 0],
        }
        lanes = []
        for row in range(self.grid.rows + 1):
            for column in range(self.grid.columns):
                lanes.append([
                    self.vertex_index(column, row),
                    self.vertex_index(column + 1, row),
                    dict(lane_parameters),
                ])
        for column in range(self.grid.columns + 1):
            for row in range(self.grid.rows):
                lanes.append([
                    self.vertex_index(column, row),
                    self.vertex_index(column, row + 1),
                    dict(lane_parameters),
                ])

        bottom_left = self.vertex_index(0, 0)
        bottom_right = self.vertex_index(self.grid.columns, 0)
        top_right = self.vertex_index(self.grid.columns, self.grid.rows)
        top_left = self.vertex_index(0, self.grid.rows)
        boundary = [bottom_left, bottom_right, top_right, top_left]
        floor_parameters = {
            "ceiling_scale": [3, 1],
            "ceiling_texture": [1, "blue_linoleum"],
            "indoor": [2, 0],
            "texture_name": [1, "blue_linoleum"],
            "texture_rotation": [3, 0],
            "texture_scale": [3, 1],
        }
        wall_parameters = {
            "alpha": [3, 1],
            "texture_height": [3, 2.5],
            "texture_name": [1, "default"],
            "texture_scale": [3, 1],
            "texture_width": [3, 1],
        }
        walls = [
            [bottom_left, bottom_right, dict(wall_parameters)],
            [bottom_right, top_right, dict(wall_parameters)],
            [top_right, top_left, dict(wall_parameters)],
            [top_left, bottom_left, dict(wall_parameters)],
        ]
        level = {
            "elevation": 0,
            "fiducials": [],
            "floors": [{"parameters": floor_parameters, "vertices": boundary}],
            "lanes": lanes,
            "layers": {},
            "measurements": [],
            "models": [],
            "vertices": vertices,
            "walls": walls,
        }
        return {
            "coordinate_system": "cartesian_meters",
            "graphs": {},
            "levels": {self.grid.level_name: level},
            "lifts": {},
            "name": self.grid.map_name,
        }
