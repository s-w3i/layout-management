"""Grid-map domain objects independent of persistence and user interfaces."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, Tuple

from .config import PROJECT_SCHEMA


GridPosition = Tuple[int, int]
GridLane = Tuple[GridPosition, GridPosition]


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
    zone_assignments: dict[str, str] = field(default_factory=dict)
    attribute_catalog: list[dict[str, Any]] = field(default_factory=list)
    location_attributes: dict[str, dict[str, Any]] = field(default_factory=dict)
    warehouse_storage_defaults: dict[str, float] = field(
        default_factory=lambda: {
            "max_item_length": 25.0,
            "max_item_width": 19.3,
            "max_item_height": 19.2,
            "max_item_weight": 465.0,
        }
    )
    deleted_positions: set[GridPosition] = field(default_factory=set)
    coordinate_overrides: dict[GridPosition, tuple[float, float]] = field(
        default_factory=dict
    )
    deleted_lanes: set[GridLane] = field(default_factory=set)
    sku_attribute_source: str = ""
    sku_attribute_summary: dict[str, Any] = field(default_factory=dict)
    sku_overlay_attributes: list[str] = field(default_factory=list)

    def validate(self) -> None:
        self.grid.validate()
        all_positions = {
            (column, row)
            for row in range(self.grid.rows + 1)
            for column in range(self.grid.columns + 1)
        }
        invalid_deleted = sorted(self.deleted_positions - all_positions)
        if invalid_deleted:
            raise ValueError(f"deleted grid point is outside the grid: {invalid_deleted[0]}")
        invalid_overrides = sorted(set(self.coordinate_overrides) - all_positions)
        if invalid_overrides:
            raise ValueError(
                f"coordinate override is outside the grid: {invalid_overrides[0]}"
            )
        if set(self.coordinate_overrides) & self.deleted_positions:
            raise ValueError("deleted grid points cannot have coordinate overrides")
        for position, coordinates in self.coordinate_overrides.items():
            if len(coordinates) != 2 or not all(math.isfinite(value) for value in coordinates):
                raise ValueError(f"grid point {position} must have finite X and Y coordinates")
        if not isinstance(self.sku_attribute_summary, dict):
            raise ValueError("SKU attribute summary must be an object")
        if (
            not isinstance(self.sku_overlay_attributes, list)
            or any(
                not isinstance(key, str) or not key.strip()
                for key in self.sku_overlay_attributes
            )
            or len(set(self.sku_overlay_attributes))
            != len(self.sku_overlay_attributes)
        ):
            raise ValueError(
                "SKU overlay attributes must be a unique list of attribute keys"
            )
        available_lanes = set(self._iter_connected_lane_positions())
        invalid_lanes = sorted(self.deleted_lanes - available_lanes)
        if invalid_lanes:
            raise ValueError(
                f"deleted lane is not part of the current grid: {invalid_lanes[0]}"
            )
        endpoint_ids = []
        for (column, row), marker in self.markers.items():
            if not (0 <= column <= self.grid.columns and 0 <= row <= self.grid.rows):
                raise ValueError(f"marker ({column}, {row}) is outside the grid")
            if (column, row) in self.deleted_positions:
                raise ValueError(f"deleted grid point ({column}, {row}) cannot have a marker")
            marker.validate()
            endpoint_ids.append(marker.endpoint_id)
        duplicates = sorted({item for item in endpoint_ids if endpoint_ids.count(item) > 1})
        if duplicates:
            raise ValueError(f"duplicate endpoint IDs: {', '.join(duplicates)}")
        rack_waypoints = {
            self.vertex_name(column, row)
            for (column, row), marker in self.markers.items()
            if marker.role == "rack"
        }
        unknown_zone_racks = sorted(set(self.zone_assignments) - rack_waypoints)
        if unknown_zone_racks:
            raise ValueError(
                "zone assignments reference non-rack grid points: "
                + ", ".join(unknown_zone_racks)
            )
        if any(not str(zone).strip() for zone in self.zone_assignments.values()):
            raise ValueError("rack zone IDs cannot be blank")
        if not isinstance(self.attribute_catalog, list) or any(
            not isinstance(item, dict) for item in self.attribute_catalog
        ):
            raise ValueError("attribute catalog must be a list of objects")
        if not isinstance(self.location_attributes, dict) or any(
            not str(path).strip() or not isinstance(values, dict)
            for path, values in self.location_attributes.items()
        ):
            raise ValueError("location attributes must map hierarchy paths to objects")
        required_storage_defaults = {
            "max_item_length", "max_item_width", "max_item_height", "max_item_weight"
        }
        if (
            not isinstance(self.warehouse_storage_defaults, dict)
            or set(self.warehouse_storage_defaults) != required_storage_defaults
        ):
            raise ValueError(
                "warehouse storage defaults must define length, width, height, and weight"
            )
        for key, value in self.warehouse_storage_defaults.items():
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"warehouse storage default {key} must be greater than zero")
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
        position = (column, row)
        try:
            return {item: index for index, item in enumerate(self.iter_positions())}[
                position
            ]
        except KeyError as exc:
            raise ValueError(f"grid point {position} has been deleted") from exc

    def vertex_name(self, column: int, row: int) -> str:
        return f"G{column}_{row}"

    def iter_positions(self) -> Iterable[GridPosition]:
        for row in range(self.grid.rows + 1):
            for column in range(self.grid.columns + 1):
                position = (column, row)
                if position not in self.deleted_positions:
                    yield position

    def coordinates(self, column: int, row: int) -> tuple[float, float]:
        """Return the editable physical coordinates of an active grid point."""
        position = (column, row)
        if position in self.deleted_positions:
            raise ValueError(f"grid point {position} has been deleted")
        return self.coordinate_overrides.get(
            position,
            (self.grid.x_coordinate(column), self.grid.y_coordinate(row)),
        )

    @staticmethod
    def normalized_lane(start: GridPosition, end: GridPosition) -> GridLane:
        return (start, end) if start <= end else (end, start)

    def _iter_connected_lane_positions(self) -> Iterable[GridLane]:
        """Connect consecutive surviving points along every original row/column."""
        for row in range(self.grid.rows + 1):
            positions = [
                (column, row)
                for column in range(self.grid.columns + 1)
                if (column, row) not in self.deleted_positions
            ]
            for start, end in zip(positions, positions[1:]):
                yield self.normalized_lane(start, end)
        for column in range(self.grid.columns + 1):
            positions = [
                (column, row)
                for row in range(self.grid.rows + 1)
                if (column, row) not in self.deleted_positions
            ]
            for start, end in zip(positions, positions[1:]):
                yield self.normalized_lane(start, end)

    def iter_lane_positions(self) -> Iterable[GridLane]:
        for lane in self._iter_connected_lane_positions():
            if lane not in self.deleted_lanes:
                yield lane

    @property
    def vertex_count(self) -> int:
        return self.grid.vertex_count - len(self.deleted_positions)

    @property
    def edge_count(self) -> int:
        return sum(1 for _lane in self.iter_lane_positions())

    def to_project_dict(self) -> dict:
        result = {
            "schema": PROJECT_SCHEMA,
            "grid": asdict(self.grid),
            "warehouse_storage_defaults": dict(self.warehouse_storage_defaults),
            "markers": [
                {"column": column, "row": row, **asdict(marker)}
                for (column, row), marker in sorted(
                    self.markers.items(), key=lambda item: (item[0][1], item[0][0])
                )
            ],
        }
        if self.storage_layout is not None:
            result["storage_layout"] = self.storage_layout.to_dict()
        if self.zone_assignments:
            result["zone_assignments"] = dict(sorted(self.zone_assignments.items()))
        if self.attribute_catalog:
            result["attribute_catalog"] = [dict(item) for item in self.attribute_catalog]
        if self.location_attributes:
            result["location_attributes"] = {
                path: dict(values)
                for path, values in sorted(self.location_attributes.items())
            }
        if self.deleted_positions:
            result["deleted_positions"] = [
                {"column": column, "row": row}
                for column, row in sorted(
                    self.deleted_positions, key=lambda item: (item[1], item[0])
                )
            ]
        if self.coordinate_overrides:
            result["coordinate_overrides"] = [
                {"column": column, "row": row, "x": x, "y": y}
                for (column, row), (x, y) in sorted(
                    self.coordinate_overrides.items(),
                    key=lambda item: (item[0][1], item[0][0]),
                )
            ]
        if self.deleted_lanes:
            result["deleted_lanes"] = [
                {
                    "start": {"column": start[0], "row": start[1]},
                    "end": {"column": end[0], "row": end[1]},
                }
                for start, end in sorted(self.deleted_lanes)
            ]
        if self.sku_attribute_source:
            result["sku_attribute_source"] = self.sku_attribute_source
        if self.sku_attribute_summary:
            result["sku_attribute_summary"] = self.sku_attribute_summary
        if self.sku_attribute_source or self.sku_attribute_summary:
            result["sku_overlay_attributes"] = list(
                self.sku_overlay_attributes
            )
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
        project.zone_assignments = {
            str(waypoint): str(zone)
            for waypoint, zone in (data.get("zone_assignments") or {}).items()
        }
        project.attribute_catalog = [
            dict(item) for item in (data.get("attribute_catalog") or [])
        ]
        project.location_attributes = {
            str(path): dict(values)
            for path, values in (data.get("location_attributes") or {}).items()
        }
        project.warehouse_storage_defaults = {
            key: float(value)
            for key, value in (
                data.get("warehouse_storage_defaults")
                or project.warehouse_storage_defaults
            ).items()
        }
        project.deleted_positions = {
            (int(item["column"]), int(item["row"]))
            for item in data.get("deleted_positions", [])
        }
        project.coordinate_overrides = {
            (int(item["column"]), int(item["row"])): (
                float(item["x"]), float(item["y"])
            )
            for item in data.get("coordinate_overrides", [])
        }
        project.deleted_lanes = {
            project.normalized_lane(
                (int(item["start"]["column"]), int(item["start"]["row"])),
                (int(item["end"]["column"]), int(item["end"]["row"])),
            )
            for item in data.get("deleted_lanes", [])
        }
        project.sku_attribute_source = str(
            data.get("sku_attribute_source", "")
        )
        project.sku_attribute_summary = dict(
            data.get("sku_attribute_summary") or {}
        )
        if "sku_overlay_attributes" in data:
            project.sku_overlay_attributes = [
                str(key) for key in data.get("sku_overlay_attributes") or []
            ]
        else:
            project.sku_overlay_attributes = list(
                project.sku_attribute_summary.get("combination_attributes")
                or project.sku_attribute_summary.get(
                    "available_combination_attributes"
                )
                or [
                    key
                    for key, item in (
                        project.sku_attribute_summary.get("attributes") or {}
                    ).items()
                    if item.get("value_type") == "boolean"
                    and key != "oversize_capable"
                ]
            )
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
            point_x, point_y = self.coordinates(column, row)
            x = round(point_x, 9)
            y = round(point_y, 9)
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
        position_indexes = {
            position: index for index, position in enumerate(self.iter_positions())
        }
        lanes = [
            [position_indexes[start], position_indexes[end], dict(lane_parameters)]
            for start, end in self.iter_lane_positions()
        ]

        corner_positions = [
            (0, 0),
            (self.grid.columns, 0),
            (self.grid.columns, self.grid.rows),
            (0, self.grid.rows),
        ]
        boundary = [
            position_indexes[position]
            for position in corner_positions
            if position in position_indexes
        ]
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
        walls = []
        floors = []
        if len(boundary) == 4:
            floors = [{"parameters": floor_parameters, "vertices": boundary}]
            walls = [
                [boundary[index], boundary[(index + 1) % 4], dict(wall_parameters)]
                for index in range(4)
            ]
        level = {
            "elevation": 0,
            "fiducials": [],
            "floors": floors,
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
