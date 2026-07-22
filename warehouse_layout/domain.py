"""Grid-map domain objects independent of persistence and user interfaces."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, Tuple

from .config import PROJECT_SCHEMA


GridPosition = Tuple[int, int]


@dataclass
class GridSpec:
    width_m: float = 20.0
    length_m: float = 15.0
    spacing_m: float = 1.0
    map_name: str = "warehouse_grid"
    level_name: str = "L1"
    spacing_y_m: float | None = None

    def __post_init__(self) -> None:
        if self.spacing_y_m is None:
            self.spacing_y_m = self.spacing_m

    def validate(self) -> None:
        for label, value in (
            ("width", self.width_m),
            ("length", self.length_m),
            ("X grid spacing", self.spacing_m),
            ("Y grid spacing", self.spacing_y_m),
        ):
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{label} must be greater than zero")
        if not self.map_name.strip() or not self.level_name.strip():
            raise ValueError("map and level names cannot be blank")
        if self.vertex_count > 10_000:
            raise ValueError(f"grid contains {self.vertex_count:,} vertices; maximum is 10,000")

    @property
    def columns(self) -> int:
        return max(1, math.ceil(self.width_m / self.spacing_m - 1e-12))

    @property
    def rows(self) -> int:
        return max(1, math.ceil(self.length_m / self.spacing_y_m - 1e-12))

    def x_coordinate(self, column: int) -> float:
        if not 0 <= column <= self.columns:
            raise ValueError(f"column {column} is outside the grid")
        return min(column * self.spacing_m, self.width_m)

    def y_coordinate(self, row: int) -> float:
        if not 0 <= row <= self.rows:
            raise ValueError(f"row {row} is outside the grid")
        return min(row * self.spacing_y_m, self.length_m)

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


STORAGE_SYSTEMS = {
    "AMR": ("grid", "AMR shelf"),
    "Mini-load ASRS": ("slot", "Tote"),
    "Pallet ASRS": ("slot", "Pallet"),
}


@dataclass
class StorageLayout:
    """Empty static buffers generated from rack markers in an editable project."""

    system_type: str = "AMR"
    levels_per_rack: int = 1
    slots_per_level: int = 6
    buffers: list[dict[str, Any]] = field(default_factory=list)

    @property
    def buffer_level(self) -> str:
        return STORAGE_SYSTEMS.get(self.system_type, ("", ""))[0]

    @property
    def handling_unit_type(self) -> str:
        return STORAGE_SYSTEMS.get(self.system_type, ("", ""))[1]

    def validate(self) -> None:
        if self.system_type not in STORAGE_SYSTEMS:
            raise ValueError(f"unsupported storage system: {self.system_type}")
        if self.levels_per_rack < 1 or self.slots_per_level < 1:
            raise ValueError("storage levels and slots per level must be at least 1")
        identifiers: set[str] = set()
        for item in self.buffers:
            if not isinstance(item, dict):
                raise ValueError("storage buffer must be an object")
            buffer_id = str(item.get("buffer_id", "")).strip()
            if not buffer_id:
                raise ValueError("storage buffer ID cannot be blank")
            if buffer_id in identifiers:
                raise ValueError(f"duplicate storage buffer ID: {buffer_id}")
            identifiers.add(buffer_id)
            if item.get("buffer_level") != self.buffer_level:
                raise ValueError(
                    f"buffer {buffer_id} must use {self.buffer_level} level for "
                    f"{self.system_type}"
                )
            if item.get("status") != "EMPTY":
                raise ValueError(f"grid-project buffer {buffer_id} must be EMPTY")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "system_type": self.system_type,
            "buffer_level": self.buffer_level,
            "handling_unit_type": self.handling_unit_type,
            "levels_per_rack": self.levels_per_rack,
            "slots_per_level": self.slots_per_level,
            "buffers": [dict(item) for item in self.buffers],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "StorageLayout":
        layout = cls(
            system_type=str(value.get("system_type", "AMR")),
            levels_per_rack=int(value.get("levels_per_rack", 1)),
            slots_per_level=int(value.get("slots_per_level", 6)),
            buffers=[dict(item) for item in value.get("buffers", [])],
        )
        layout.validate()
        return layout


@dataclass
class GridProject:
    grid: GridSpec = field(default_factory=GridSpec)
    markers: Dict[GridPosition, Marker] = field(default_factory=dict)
    storage_layout: StorageLayout | None = None

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
        if self.storage_layout is not None:
            self.storage_layout.validate()
            rack_positions = {
                position for position, marker in self.markers.items()
                if marker.role == "rack"
            }
            buffer_positions = {
                (int(item["column"]), int(item["row"]))
                for item in self.storage_layout.buffers
            }
            if buffer_positions != rack_positions:
                raise ValueError(
                    "every rack marker must have generated storage buffers and "
                    "buffers cannot exist at non-rack points"
                )
            expected_per_rack = (
                1 if self.storage_layout.buffer_level == "grid"
                else self.storage_layout.levels_per_rack
                * self.storage_layout.slots_per_level
            )
            if len(self.storage_layout.buffers) != len(rack_positions) * expected_per_rack:
                raise ValueError("storage buffer catalog is incomplete for its capacity")
            for item in self.storage_layout.buffers:
                column, row = int(item["column"]), int(item["row"])
                waypoint = self.vertex_name(column, row)
                if item.get("grid_waypoint") != waypoint:
                    raise ValueError(
                        f"storage buffer grid waypoint must be {waypoint}"
                    )
                root = f"B-{waypoint}"
                expected_id = root
                if self.storage_layout.buffer_level == "slot":
                    expected_id += (
                        f"/L{int(item.get('level', 0)):02d}"
                        f"/S{int(item.get('slot', 0)):02d}"
                    )
                if item.get("buffer_id") != expected_id:
                    raise ValueError(f"invalid storage buffer ID: {item.get('buffer_id')}")

    def vertex_index(self, column: int, row: int) -> int:
        return row * (self.grid.columns + 1) + column

    def vertex_name(self, column: int, row: int) -> str:
        return f"G{column}_{row}"

    def iter_positions(self) -> Iterable[GridPosition]:
        for row in range(self.grid.rows + 1):
            for column in range(self.grid.columns + 1):
                yield column, row

    def to_project_dict(self) -> dict:
        result = {
            "schema": PROJECT_SCHEMA,
            "grid": asdict(self.grid),
            "markers": [
                {"column": column, "row": row, **asdict(marker)}
                for (column, row), marker in sorted(
                    self.markers.items(), key=lambda item: (item[0][1], item[0][0])
                )
            ],
        }
        if self.storage_layout is not None:
            result["storage_layout"] = self.storage_layout.to_dict()
        return result

    @classmethod
    def from_project_dict(cls, data: dict) -> "GridProject":
        from .config import LEGACY_PROJECT_SCHEMA

        if data.get("schema") not in {PROJECT_SCHEMA, LEGACY_PROJECT_SCHEMA}:
            raise ValueError("not a supported RMF grid project file")
        project = cls(grid=GridSpec(**data["grid"]))
        for item in data.get("markers", []):
            position = (int(item["column"]), int(item["row"]))
            project.markers[position] = Marker(item["role"], item["endpoint_id"])
        if data.get("storage_layout") is not None:
            project.storage_layout = StorageLayout.from_dict(data["storage_layout"])
        project.validate()
        return project

    def assign_storage_buffers(
        self, system_type: str, levels_per_rack: int, slots_per_level: int
    ) -> StorageLayout:
        """Replace the project buffer catalog using the current rack markers."""
        layout = StorageLayout(system_type, levels_per_rack, slots_per_level)
        for (column, row), marker in sorted(
            self.markers.items(), key=lambda item: (item[0][1], item[0][0])
        ):
            if marker.role != "rack":
                continue
            waypoint = self.vertex_name(column, row)
            root_id = f"B-{waypoint}"
            common = {
                "column": column,
                "row": row,
                "grid_waypoint": waypoint,
                "rack_endpoint_id": marker.endpoint_id,
                "buffer_level": layout.buffer_level,
                "status": "EMPTY",
            }
            if layout.buffer_level == "grid":
                layout.buffers.append({"buffer_id": root_id, **common})
                continue
            for level in range(1, levels_per_rack + 1):
                for slot in range(1, slots_per_level + 1):
                    layout.buffers.append({
                        "buffer_id": f"{root_id}/L{level:02d}/S{slot:02d}",
                        **common,
                        "level": level,
                        "slot": slot,
                    })
        layout.validate()
        self.storage_layout = layout
        return layout

    def to_building_dict(self) -> dict:
        self.validate()
        vertices = []
        for column, row in self.iter_positions():
            x = round(self.grid.x_coordinate(column), 9)
            y = round(self.grid.y_coordinate(row), 9)
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
