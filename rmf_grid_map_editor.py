#!/usr/bin/env python3
"""Create an image-free, grid-based Open-RMF building map.

Run without arguments to open the Tkinter editor. The command-line mode can
generate a fully connected empty grid for automation or testing.
"""

from __future__ import annotations

import argparse
import csv
import heapq
import json
import math
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, Tuple

try:
    import yaml
except ImportError as exc:
    raise SystemExit("PyYAML is required: pip install pyyaml") from exc


PROJECT_SCHEMA = "rmf_grid_map_editor/v1"
SLOTTING_SCHEMA = "inventory_slotting_layout/v1"
DEFAULT_OUTPUT = Path(__file__).resolve().parent / "resources/map/v6.building.yaml"
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
                for (column, row), marker in sorted(self.markers.items(), key=lambda item: (item[0][1], item[0][0]))
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
                lanes.append([self.vertex_index(column, row), self.vertex_index(column + 1, row), dict(lane_parameters)])
        for column in range(self.grid.columns + 1):
            for row in range(self.grid.rows):
                lanes.append([self.vertex_index(column, row), self.vertex_index(column, row + 1), dict(lane_parameters)])

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


class FlowStyleDumper(yaml.SafeDumper):
    """Dumper that keeps RMF's short typed parameter lists compact."""

    def ignore_aliases(self, data):
        return True


def _represent_sequence(dumper, data):
    flow = len(data) <= 4 and not any(isinstance(item, (dict, list)) for item in data)
    return dumper.represent_sequence("tag:yaml.org,2002:seq", data, flow_style=flow)


FlowStyleDumper.add_representer(list, _represent_sequence)


def write_building_yaml(project: GridProject, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as stream:
        yaml.dump(project.to_building_dict(), stream, Dumper=FlowStyleDumper, sort_keys=False, width=160)


def write_project(project: GridProject, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(project.to_project_dict(), indent=2) + "\n", encoding="utf-8")


def _typed_value(value, default=None):
    if isinstance(value, list) and len(value) >= 2:
        return value[1]
    return default


def next_zone_id(zone_id: str) -> str:
    value = zone_id.strip()
    match = re.match(r"^(.*?)(\d+)$", value)
    if match:
        prefix, number = match.groups()
        return f"{prefix}{int(number)+1:0{len(number)}d}"
    return f"{value}_02"


def _distance_scale(building: dict, level: dict) -> float:
    if building.get("coordinate_system") == "cartesian_meters":
        return 1.0
    vertices = level.get("vertices", [])
    ratios = []
    for measurement in level.get("measurements", []):
        if len(measurement) < 3 or "distance" not in measurement[2]:
            continue
        start, end = int(measurement[0]), int(measurement[1])
        if not (0 <= start < len(vertices) and 0 <= end < len(vertices)):
            continue
        drawing_distance = math.hypot(float(vertices[start][0])-float(vertices[end][0]), float(vertices[start][1])-float(vertices[end][1]))
        real_distance = float(_typed_value(measurement[2]["distance"], 0) or 0)
        if drawing_distance > 0 and real_distance > 0:
            ratios.append(real_distance / drawing_distance)
    return sorted(ratios)[len(ratios)//2] if ratios else 1.0


def load_building_yaml(building_path: Path) -> dict:
    with building_path.open("r", encoding="utf-8") as stream:
        building = yaml.safe_load(stream)
    if not isinstance(building, dict) or not building.get("levels"):
        raise ValueError("building YAML has no RMF levels")
    return building


def load_velocity_csv(velocity_path: Path) -> list[dict]:
    with velocity_path.open("r", encoding="utf-8-sig", newline="") as stream:
        sku_rows = list(csv.DictReader(stream))
    required = {"sku", "pick_frequency", "velocity_class"}
    if not sku_rows or not required.issubset(sku_rows[0]):
        raise ValueError(f"SKU CSV must contain: {', '.join(sorted(required))}")
    return sku_rows


def load_slotting_inputs(building_path: Path, velocity_path: Path) -> tuple[dict, list[dict]]:
    return load_building_yaml(building_path), load_velocity_csv(velocity_path)


def _rack_distances(building: dict) -> tuple[str, list[dict], int, int]:
    level_name, level = next(iter(building["levels"].items()))
    vertices = level.get("vertices", [])
    scale = _distance_scale(building, level)
    pickups, dropoffs = [], {}
    for index, vertex in enumerate(vertices):
        params = vertex[4] if len(vertex) > 4 and isinstance(vertex[4], dict) else {}
        if "pickup_dispenser" in params:
            waypoint = str(vertex[3]) or f"vertex_{index}"
            pickups.append({"vertex_index": index, "rack_id": waypoint, "waypoint": waypoint, "pickup_dispenser_id": str(_typed_value(params["pickup_dispenser"], waypoint)), "x": float(vertex[0]), "y": float(vertex[1])})
        if "dropoff_ingestor" in params:
            dropoffs[index] = str(_typed_value(params["dropoff_ingestor"], vertex[3]))
    if not pickups:
        raise ValueError("building YAML contains no pickup_dispenser rack points")
    if not dropoffs:
        raise ValueError("building YAML contains no dropoff_ingestor workstations")

    for rack in pickups:
        rack["static_bay_id"] = f"BAY-{rack['waypoint']}"

    reverse_graph = [[] for _ in vertices]
    for lane in level.get("lanes", []):
        if len(lane) < 3:
            continue
        start, end, params = int(lane[0]), int(lane[1]), lane[2]
        if not (0 <= start < len(vertices) and 0 <= end < len(vertices)):
            continue
        weight = math.hypot(float(vertices[start][0])-float(vertices[end][0]), float(vertices[start][1])-float(vertices[end][1])) * scale
        reverse_graph[end].append((start, weight))
        if bool(_typed_value(params.get("bidirectional"), False)):
            reverse_graph[start].append((end, weight))

    rack_routes = {rack["vertex_index"]: [] for rack in pickups}
    for dropoff_index, endpoint in sorted(dropoffs.items(), key=lambda item: item[1]):
        distances = [math.inf] * len(vertices)
        distances[dropoff_index] = 0.0
        queue = [(0.0, dropoff_index)]
        while queue:
            distance, node = heapq.heappop(queue)
            if distance > distances[node] + 1e-9:
                continue
            for previous, weight in reverse_graph[node]:
                candidate = distance + weight
                if candidate + 1e-9 < distances[previous]:
                    distances[previous] = candidate
                    heapq.heappush(queue, (candidate, previous))
        for rack in pickups:
            rack_index = rack["vertex_index"]
            if math.isfinite(distances[rack_index]):
                rack_routes[rack_index].append((endpoint, distances[rack_index]))
    for rack in pickups:
        index = rack["vertex_index"]
        routes = rack_routes[index]
        rack["workstations"] = [endpoint for endpoint, _distance in routes]
        rack["workstation_count"] = len(routes)
        rack["distance_m"] = sum(distance for _endpoint, distance in routes) / len(routes) if len(routes) == len(dropoffs) else math.inf
    apply_zone_local_aisles(building, pickups, {}, "Z01")
    pickups.sort(key=lambda rack: (math.isinf(rack["distance_m"]), rack["distance_m"], rack["rack_id"]))
    return level_name, pickups, len(dropoffs), sum(math.isinf(rack["distance_m"]) for rack in pickups)


def apply_zone_local_aisles(building: dict, racks: list[dict], zone_assignments: dict[str, str], default_zone: str = "Z01") -> None:
    """Assign one aisle ID to each distinct rack column in a zone."""
    x_key = (lambda value: round(value)) if building.get("coordinate_system") == "reference_image" else (lambda value: round(value, 9))
    racks_by_zone = {}
    for rack in racks:
        zone = zone_assignments.get(rack["waypoint"], default_zone).strip() or default_zone
        rack["zone_id"] = zone
        racks_by_zone.setdefault(zone, []).append(rack)
    for zone_racks in racks_by_zone.values():
        columns = {value: index for index, value in enumerate(sorted({x_key(rack["x"]) for rack in zone_racks}), start=1)}
        racks_by_aisle = {}
        for rack in zone_racks:
            rack["aisle_id"] = f"A{columns[x_key(rack['x'])]:02d}"
            racks_by_aisle.setdefault(rack["aisle_id"], []).append(rack)
        for aisle_racks in racks_by_aisle.values():
            for bay_order, rack in enumerate(sorted(aisle_racks, key=lambda item: (item["y"], item["waypoint"])), start=1):
                rack["bay_order"] = bay_order


def build_dynamic_address(zone_id: str, aisle_id: str, static_bay_id: str, level: int, slot: int, handling_unit_type: str, handling_unit_id: str) -> tuple[str, str]:
    """Return the dynamic address and the hierarchy layer holding the unit."""
    if handling_unit_type == "AMR shelf":
        return f"{zone_id}/{aisle_id}/BAY-{handling_unit_id}/L{level:02d}/S{slot:02d}", "bay"
    if handling_unit_type in {"Tote", "Pallet"}:
        return f"{zone_id}/{aisle_id}/{static_bay_id}/L{level:02d}/SLOT-{handling_unit_id}", "slot"
    raise ValueError(f"unsupported handling unit type: {handling_unit_type}")


def generate_basic_slotting(building: dict, sku_rows: list[dict], levels_per_rack: int = 1, slots_per_level: int = 6, handling_unit_type: str = "AMR shelf", zone_id: str = "Z01", zone_assignments: dict[str, str] | None = None) -> tuple[list[dict], dict]:
    if levels_per_rack < 1 or slots_per_level < 1:
        raise ValueError("levels and slots per level must be at least 1")
    level_name, racks, workstation_count, unreachable_count = _rack_distances(building)
    usable_racks = [rack for rack in racks if not math.isinf(rack["distance_m"])]
    positions = []
    unit_prefix = {"AMR shelf": "SHELF", "Tote": "TOTE", "Pallet": "PALLET"}.get(handling_unit_type)
    if unit_prefix is None:
        raise ValueError(f"unsupported handling unit type: {handling_unit_type}")
    zone_id = zone_id.strip()
    if not zone_id:
        raise ValueError("zone ID cannot be blank")
    zone_assignments = zone_assignments or {}
    apply_zone_local_aisles(building, racks, zone_assignments, zone_id)
    movable_unit_number = 0
    for rack_rank, rack in enumerate(usable_racks, start=1):
        shelf_unit = f"SHELF_{rack_rank:03d}"
        for level_number in range(1, levels_per_rack + 1):
            for slot_number in range(1, slots_per_level + 1):
                if handling_unit_type == "AMR shelf":
                    handling_unit_id = shelf_unit
                else:
                    movable_unit_number += 1
                    handling_unit_id = f"{unit_prefix}_{movable_unit_number:03d}"
                rack_zone = rack["zone_id"]
                static_address = f"{rack_zone}/{rack['aisle_id']}/{rack['static_bay_id']}/L{level_number:02d}/S{slot_number:02d}"
                dynamic_address, dynamic_address_level = build_dynamic_address(rack_zone, rack["aisle_id"], rack["static_bay_id"], level_number, slot_number, handling_unit_type, handling_unit_id)
                positions.append({**rack, "zone_id": rack_zone, "rack_rank": rack_rank, "handling_unit_id": handling_unit_id, "handling_unit_type": handling_unit_type, "dynamic_address_level": dynamic_address_level, "level": level_number, "slot": slot_number, "static_address": static_address, "dynamic_address": dynamic_address})

    class_rank = {"A": 0, "B": 1, "C": 2}
    sorted_skus = sorted(sku_rows, key=lambda row: (class_rank.get(str(row.get("velocity_class", "")).upper(), 9), -float(row.get("pick_frequency") or 0), str(row.get("sku", ""))))
    output = []
    for sku_rank, sku in enumerate(sorted_skus, start=1):
        position = positions[sku_rank-1] if sku_rank <= len(positions) else None
        row = {
            "sku_rank": sku_rank,
            "sku": sku.get("sku", ""),
            "velocity_class": sku.get("velocity_class", ""),
            "pick_frequency": sku.get("pick_frequency", ""),
            "total_quantity_ea": sku.get("total_quantity_ea", ""),
            "active_days": sku.get("active_days", ""),
            "strategy": "basic",
            "assignment_status": "ASSIGNED" if position else "UNASSIGNED_NO_CAPACITY",
        }
        if position:
            row.update({
                "static_address": position["static_address"],
                "rmf_grid_address": f"{level_name}/{position['waypoint']}",
                "zone_id": position["zone_id"],
                "aisle_id": position["aisle_id"],
                "static_bay_id": position["static_bay_id"],
                "rack_id": position["rack_id"],
                "rack_waypoint": position["waypoint"],
                "pickup_dispenser_id": position["pickup_dispenser_id"],
                "rack_vertex_index": position["vertex_index"],
                "rack_rank": position["rack_rank"],
                "handling_unit_type": position["handling_unit_type"],
                "handling_unit_id": position["handling_unit_id"],
                "dynamic_address_level": position["dynamic_address_level"],
                "dynamic_address": position["dynamic_address"],
                "storage_level": position["level"],
                "storage_slot": position["slot"],
                "workstations_evaluated": "|".join(position["workstations"]),
                "average_workstation_distance_m": round(position["distance_m"], 3),
            })
        else:
            row.update({key: "" for key in ("static_address", "rmf_grid_address", "zone_id", "aisle_id", "static_bay_id", "rack_id", "rack_waypoint", "pickup_dispenser_id", "rack_vertex_index", "rack_rank", "handling_unit_type", "handling_unit_id", "dynamic_address_level", "dynamic_address", "storage_level", "storage_slot", "workstations_evaluated", "average_workstation_distance_m")})
        output.append(row)
    summary = {
        "sku_count": len(sorted_skus), "assigned_count": min(len(sorted_skus), len(positions)),
        "unassigned_count": max(0, len(sorted_skus)-len(positions)), "rack_count": len(racks),
        "usable_rack_count": len(usable_racks), "unreachable_rack_count": unreachable_count,
        "workstation_count": workstation_count, "capacity": len(positions), "level_name": level_name,
        "zone_count": len({position["zone_id"] for position in positions}),
    }
    return output, summary


def write_slotting_csv(rows: list[dict], output_path: Path) -> None:
    if not rows:
        raise ValueError("there are no slotting rows to write")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader(); writer.writerows(rows)


def write_slotting_layout_json(rows: list[dict], building: dict, summary: dict, output_path: Path, *, strategy: str, handling_unit_type: str, levels_per_rack: int, slots_per_level: int, zone_assignments: dict[str, str], source_building: str = "", source_velocity: str = "") -> None:
    payload = {
        "schema": SLOTTING_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "strategy": strategy,
        "handling_unit_type": handling_unit_type,
        "rack_capacity": {"levels": levels_per_rack, "slots_per_level": slots_per_level},
        "sources": {"building_yaml": source_building, "sku_velocity_csv": source_velocity},
        "zone_assignments": zone_assignments,
        "summary": summary,
        "building": building,
        "assignments": rows,
        "operation_log": [],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def load_slotting_layout_json(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != SLOTTING_SCHEMA:
        raise ValueError("not a supported inventory slotting layout")
    if not isinstance(payload.get("building"), dict) or not isinstance(payload.get("assignments"), list):
        raise ValueError("slotting layout is missing building or assignment data")
    return payload


SLOT_LOCATION_FIELDS = (
    "static_address", "rmf_grid_address", "zone_id", "aisle_id", "static_bay_id",
    "rack_id", "rack_waypoint", "pickup_dispenser_id", "rack_vertex_index", "rack_rank",
    "handling_unit_type", "handling_unit_id", "dynamic_address_level", "dynamic_address", "storage_level", "storage_slot",
    "workstations_evaluated", "average_workstation_distance_m",
)


def _find_sku_assignment(rows: list[dict], sku: str) -> dict:
    wanted = sku.strip().lower()
    exact = next((row for row in rows if str(row.get("sku", "")).lower() == wanted), None)
    if exact:
        return exact
    matches = [row for row in rows if wanted and wanted in str(row.get("sku", "")).lower()]
    if not matches:
        raise ValueError(f"SKU not found: {sku}")
    return matches[0]


def swap_sku_slots(rows: list[dict], first_sku: str, second_sku: str) -> tuple[dict, dict]:
    first = _find_sku_assignment(rows, first_sku)
    second = _find_sku_assignment(rows, second_sku)
    if first is second:
        raise ValueError("select two different SKUs")
    first_location = {field: first.get(field, "") for field in SLOT_LOCATION_FIELDS}
    second_location = {field: second.get(field, "") for field in SLOT_LOCATION_FIELDS}
    for field in SLOT_LOCATION_FIELDS:
        first[field] = second_location[field]
        second[field] = first_location[field]
    return first, second


def _static_profile(row: dict) -> dict:
    fields = ("rmf_grid_address", "zone_id", "aisle_id", "static_bay_id", "rack_id", "rack_waypoint", "pickup_dispenser_id", "rack_vertex_index", "rack_rank", "workstations_evaluated", "average_workstation_distance_m")
    return {field: row.get(field, "") for field in fields}


def _apply_shelf_profile(row: dict, profile: dict) -> None:
    for field, value in profile.items():
        row[field] = value
    level = int(row.get("storage_level") or 1)
    slot = int(row.get("storage_slot") or 1)
    row["static_address"] = f"{row['zone_id']}/{row['aisle_id']}/{row['static_bay_id']}/L{level:02d}/S{slot:02d}"
    row["dynamic_address"], row["dynamic_address_level"] = build_dynamic_address(row["zone_id"], row["aisle_id"], row["static_bay_id"], level, slot, row.get("handling_unit_type", "AMR shelf"), row["handling_unit_id"])


def swap_whole_shelves(rows: list[dict], first_sku: str, second_sku: str) -> tuple[str, str, int, int]:
    first = _find_sku_assignment(rows, first_sku)
    second = _find_sku_assignment(rows, second_sku)
    first_unit, second_unit = first.get("handling_unit_id", ""), second.get("handling_unit_id", "")
    if not first_unit or not second_unit:
        raise ValueError("the two SKUs must belong to different handling units")
    return swap_whole_shelf_units(rows, first_unit, second_unit)


def swap_whole_shelf_units(rows: list[dict], first_unit: str, second_unit: str) -> tuple[str, str, int, int]:
    first_unit, second_unit = first_unit.strip(), second_unit.strip()
    if not first_unit or not second_unit or first_unit == second_unit:
        raise ValueError("select two different shelves")
    first_rows = [row for row in rows if row.get("handling_unit_id") == first_unit]
    second_rows = [row for row in rows if row.get("handling_unit_id") == second_unit]
    if not first_rows:
        raise ValueError(f"shelf not found: {first_unit}")
    if not second_rows:
        raise ValueError(f"shelf not found: {second_unit}")
    if any(row.get("handling_unit_type") != "AMR shelf" for row in first_rows + second_rows):
        raise ValueError("whole-shelf swap is only available for AMR shelf layouts")
    first_profile, second_profile = _static_profile(first_rows[0]), _static_profile(second_rows[0])
    for row in first_rows:
        _apply_shelf_profile(row, second_profile)
    for row in second_rows:
        _apply_shelf_profile(row, first_profile)
    return first_unit, second_unit, len(first_rows), len(second_rows)


def run_gui(initial_project: GridProject | None = None) -> None:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    class Editor:
        def __init__(self, root):
            self.root = root
            self.root.title("RMF Grid Map Editor")
            self.root.geometry("1220x820")
            self.project = initial_project or GridProject()
            self.selected: GridPosition | None = None
            self.bulk_anchor: GridPosition | None = None
            self.undo_stack: list[dict] = []
            self.redo_stack: list[dict] = []
            self.drag_undo_started = False
            self.tool = tk.StringVar(value="select")
            self.rack_prefix = tk.StringVar(value="RACK")
            self.map_name = tk.StringVar(value=self.project.grid.map_name)
            self.level_name = tk.StringVar(value=self.project.grid.level_name)
            self.width = tk.StringVar(value=str(self.project.grid.width_m))
            self.length = tk.StringVar(value=str(self.project.grid.length_m))
            self.spacing = tk.StringVar(value=str(self.project.grid.spacing_m))
            self.selected_coordinate = tk.StringVar(value="No grid point selected")
            self.role = tk.StringVar(value="none")
            self.endpoint_id = tk.StringVar()
            self.summary = tk.StringVar()
            self.status = tk.StringVar(value="Bottom-left grid point is (0, 0)")
            self._build_ui()
            self.root.bind_all("<Control-z>", self.undo)
            self.root.bind_all("<Control-y>", self.redo)
            self.root.bind_all("<Control-Shift-Z>", self.redo)
            self.redraw()

        def _build_ui(self):
            self.root.columnconfigure(0, weight=1)
            self.root.rowconfigure(0, weight=1)
            notebook = ttk.Notebook(self.root)
            notebook.grid(row=0, column=0, sticky="nsew")
            map_tab = ttk.Frame(notebook)
            slotting_tab = ttk.Frame(notebook)
            operations_tab = ttk.Frame(notebook)
            notebook.add(map_tab, text="Grid Map Editor")
            notebook.add(slotting_tab, text="Inventory Slotting")
            notebook.add(operations_tab, text="Inventory Operations Demo")
            map_tab.columnconfigure(1, weight=1)
            map_tab.rowconfigure(0, weight=1)
            left = ttk.Frame(map_tab, padding=12)
            left.grid(row=0, column=0, sticky="ns")
            canvas_frame = ttk.Frame(map_tab, padding=(0, 12, 12, 12))
            canvas_frame.grid(row=0, column=1, sticky="nsew")
            canvas_frame.columnconfigure(0, weight=1)
            canvas_frame.rowconfigure(0, weight=1)

            ttk.Label(left, text="WAREHOUSE GRID", font=("TkDefaultFont", 10, "bold")).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
            fields = [
                ("Map name", self.map_name), ("Level name", self.level_name),
                ("Total width (m)", self.width), ("Total length (m)", self.length),
                ("Distance per grid (m)", self.spacing),
            ]
            for row, (label, variable) in enumerate(fields, start=1):
                ttk.Label(left, text=label).grid(row=row, column=0, sticky="w", pady=3)
                ttk.Entry(left, textvariable=variable, width=19).grid(row=row, column=1, sticky="ew", pady=3)
            ttk.Button(left, text="Generate / reset grid", command=self.generate_grid).grid(row=6, column=0, columnspan=2, sticky="ew", pady=(8, 4))
            ttk.Label(left, textvariable=self.summary, foreground="#4d646d").grid(row=7, column=0, columnspan=2, sticky="w", pady=(0, 14))

            ttk.Separator(left).grid(row=8, column=0, columnspan=2, sticky="ew", pady=4)
            ttk.Label(left, text="CLICK TOOL", font=("TkDefaultFont", 10, "bold")).grid(row=9, column=0, columnspan=2, sticky="w", pady=(8, 5))
            tools = [
                ("Select / edit", "select"),
                ("Paint rack pickups (drag)", "rack"),
                ("Fill rack rectangle (2 clicks)", "rack_rectangle"),
                ("Place workstation drop-off", "workstation"),
                ("Clear markers (drag)", "clear"),
            ]
            for row, (label, value) in enumerate(tools, start=10):
                ttk.Radiobutton(left, text=label, variable=self.tool, value=value).grid(row=row, column=0, columnspan=2, sticky="w", pady=2)

            ttk.Label(left, text="Rack ID prefix").grid(row=15, column=0, sticky="w", pady=(7, 3))
            ttk.Entry(left, textvariable=self.rack_prefix, width=19).grid(row=15, column=1, sticky="ew", pady=(7, 3))

            ttk.Separator(left).grid(row=16, column=0, columnspan=2, sticky="ew", pady=10)
            ttk.Label(left, text="SELECTED GRID POINT", font=("TkDefaultFont", 10, "bold")).grid(row=17, column=0, columnspan=2, sticky="w")
            ttk.Label(left, textvariable=self.selected_coordinate).grid(row=18, column=0, columnspan=2, sticky="w", pady=(3, 6))
            ttk.Label(left, text="Role").grid(row=19, column=0, sticky="w", pady=3)
            role_box = ttk.Combobox(left, textvariable=self.role, state="readonly", values=("none", "rack", "workstation"), width=16)
            role_box.grid(row=19, column=1, sticky="ew", pady=3)
            ttk.Label(left, text="Endpoint ID").grid(row=20, column=0, sticky="w", pady=3)
            ttk.Entry(left, textvariable=self.endpoint_id, width=19).grid(row=20, column=1, sticky="ew", pady=3)
            ttk.Button(left, text="Apply point edit", command=self.apply_edit).grid(row=21, column=0, columnspan=2, sticky="ew", pady=(6, 12))

            ttk.Separator(left).grid(row=22, column=0, columnspan=2, sticky="ew", pady=5)
            ttk.Button(left, text="Save editable project…", command=self.save_project_dialog).grid(row=23, column=0, columnspan=2, sticky="ew", pady=3)
            ttk.Button(left, text="Load editable project…", command=self.load_project_dialog).grid(row=24, column=0, columnspan=2, sticky="ew", pady=3)
            ttk.Button(left, text="Export RMF building YAML…", command=self.export_yaml_dialog).grid(row=25, column=0, columnspan=2, sticky="ew", pady=(10, 3))

            self.canvas = tk.Canvas(canvas_frame, background="white", highlightthickness=1, highlightbackground="#9aa8ae")
            self.canvas.grid(row=0, column=0, sticky="nsew")
            self.canvas.bind("<Button-1>", self.canvas_click)
            self.canvas.bind("<B1-Motion>", self.canvas_drag)
            self.canvas.bind("<ButtonRelease-1>", self.canvas_release)
            self.canvas.bind("<Configure>", lambda _event: self.redraw())
            ttk.Label(canvas_frame, textvariable=self.status).grid(row=1, column=0, sticky="ew", pady=(6, 0))
            self._build_slotting_tab(slotting_tab)
            self._build_operations_tab(operations_tab)

        def _build_slotting_tab(self, parent):
            base = Path(__file__).resolve().parent
            self.slot_building_path = tk.StringVar(value=str(base / "resources/map/demo.building.yaml"))
            self.slot_velocity_path = tk.StringVar(value=str(base / "resources/data/sku_velocity_output/sku_velocity_summary.csv"))
            self.slot_output_path = tk.StringVar(value=str(base / "resources/data/basic_slotting_layout.slotting.json"))
            self.slot_strategy = tk.StringVar(value="basic")
            self.slot_handling_unit = tk.StringVar(value="AMR shelf")
            self.slot_zone = tk.StringVar(value="Z01")
            self.slot_levels = tk.StringVar(value="1")
            self.slot_slots = tk.StringVar(value="6")
            self.slot_summary = tk.StringVar(value="Choose the inputs and generate a slotting layout.")
            self.slot_rack_detail = tk.StringVar(value="Generate a layout, then click a rack to inspect it.")
            self.slot_building = None
            self.slot_rows = []
            self.slot_racks = []
            self.slot_selected_rack = None
            self.slot_loaded_path = None
            self.slot_zone_assignments = {}
            self.slot_zone_mode = tk.BooleanVar(value=True)
            self.slot_zone_auto = tk.BooleanVar(value=True)
            self.slot_zone_drag_start = None
            self.slot_zone_drag_current = None
            self.slot_legend = tk.StringVar(value="Load the building map, then drag a rectangle to assign rack zones.")

            parent.columnconfigure(0, weight=1)
            parent.rowconfigure(1, weight=1)
            form = ttk.LabelFrame(parent, text="Slotting inputs", padding=12)
            form.grid(row=0, column=0, sticky="ew", padx=12, pady=12)
            form.columnconfigure(1, weight=1)
            ttk.Label(form, text="Building YAML").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
            ttk.Entry(form, textvariable=self.slot_building_path).grid(row=0, column=1, sticky="ew", pady=4)
            ttk.Button(form, text="Browse…", command=lambda: self.browse_slot_input(self.slot_building_path, [("RMF building YAML", "*.building.yaml"), ("YAML", "*.yaml"), ("All files", "*")])).grid(row=0, column=2, padx=(8, 0), pady=4)
            ttk.Button(form, text="Load map", command=self.load_slot_building).grid(row=0, column=3, padx=(6, 0), pady=4)
            ttk.Label(form, text="ABC SKU velocity CSV").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=4)
            ttk.Entry(form, textvariable=self.slot_velocity_path).grid(row=1, column=1, sticky="ew", pady=4)
            ttk.Button(form, text="Browse…", command=lambda: self.browse_slot_input(self.slot_velocity_path, [("CSV", "*.csv"), ("All files", "*")])).grid(row=1, column=2, padx=(8, 0), pady=4)

            ttk.Label(form, text="Strategy").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=4)
            ttk.Combobox(form, textvariable=self.slot_strategy, state="readonly", values=("basic",), width=18).grid(row=2, column=1, sticky="w", pady=4)
            ttk.Label(form, text="Handling unit").grid(row=3, column=0, sticky="w", padx=(0, 8), pady=4)
            ttk.Combobox(form, textvariable=self.slot_handling_unit, state="readonly", values=("AMR shelf", "Tote", "Pallet"), width=18).grid(row=3, column=1, sticky="w", pady=4)
            ttk.Label(form, text="Zone ID").grid(row=4, column=0, sticky="w", padx=(0, 8), pady=4)
            ttk.Entry(form, textvariable=self.slot_zone, width=20).grid(row=4, column=1, sticky="w", pady=4)
            zone_actions = ttk.Frame(form)
            zone_actions.grid(row=4, column=2, columnspan=2, sticky="w")
            ttk.Checkbutton(zone_actions, text="Rectangle zone selection", variable=self.slot_zone_mode, command=self.zone_mode_changed).pack(side="left")
            ttk.Checkbutton(zone_actions, text="Auto next ID", variable=self.slot_zone_auto).pack(side="left", padx=(6,0))
            ttk.Button(zone_actions, text="Clear zones", command=self.clear_slot_zones).pack(side="left", padx=(6,0))

            capacity = ttk.Frame(form)
            capacity.grid(row=5, column=1, sticky="w", pady=4)
            ttk.Label(form, text="Rack capacity").grid(row=5, column=0, sticky="w", padx=(0, 8), pady=4)
            ttk.Label(capacity, text="Levels").pack(side="left")
            ttk.Spinbox(capacity, from_=1, to=100, textvariable=self.slot_levels, width=5).pack(side="left", padx=(5, 14))
            ttk.Label(capacity, text="Slots per level").pack(side="left")
            ttk.Spinbox(capacity, from_=1, to=100, textvariable=self.slot_slots, width=5).pack(side="left", padx=5)

            ttk.Label(form, text="Output layout JSON").grid(row=6, column=0, sticky="w", padx=(0, 8), pady=4)
            ttk.Entry(form, textvariable=self.slot_output_path).grid(row=6, column=1, sticky="ew", pady=4)
            ttk.Button(form, text="Browse…", command=self.browse_slot_output).grid(row=6, column=2, padx=(8, 0), pady=4)
            ttk.Button(form, text="Generate slotting layout", command=self.run_slotting, style="Accent.TButton").grid(row=7, column=1, sticky="w", pady=(10, 4))
            ttk.Label(form, textvariable=self.slot_summary, foreground="#315b66").grid(row=8, column=0, columnspan=3, sticky="w", pady=(8, 0))

            result = ttk.LabelFrame(parent, text="Interactive slotting layout", padding=8)
            result.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))
            result.columnconfigure(0, weight=1); result.rowconfigure(0, weight=1)
            paned = ttk.Panedwindow(result, orient="horizontal")
            paned.grid(row=0, column=0, sticky="nsew")
            layout_view = ttk.Frame(paned)
            rack_view = ttk.Frame(paned)
            paned.add(layout_view, weight=3); paned.add(rack_view, weight=2)
            layout_view.columnconfigure(0, weight=1); layout_view.rowconfigure(0, weight=1)
            rack_view.columnconfigure(0, weight=1); rack_view.rowconfigure(2, weight=1)

            self.slot_canvas = tk.Canvas(layout_view, background="white", highlightthickness=1, highlightbackground="#9aa8ae")
            self.slot_canvas.grid(row=0, column=0, sticky="nsew")
            self.slot_canvas.bind("<Configure>", lambda _event: self.draw_slotting_layout())
            self.slot_canvas.bind("<ButtonPress-1>", self.slot_canvas_press)
            self.slot_canvas.bind("<B1-Motion>", self.slot_canvas_drag)
            self.slot_canvas.bind("<ButtonRelease-1>", self.slot_canvas_release)
            self.slot_canvas.tag_bind("rack", "<Button-1>", self.slot_rack_click)
            ttk.Label(layout_view, textvariable=self.slot_legend, foreground="#4d646d").grid(row=1, column=0, sticky="w", pady=(5, 0))

            ttk.Label(rack_view, text="RACK DETAILS", font=("TkDefaultFont", 10, "bold")).grid(row=0, column=0, sticky="w", padx=8)
            ttk.Label(rack_view, textvariable=self.slot_rack_detail, justify="left", wraplength=450).grid(row=1, column=0, sticky="ew", padx=8, pady=(4, 8))
            columns = ("rank", "sku", "class", "static", "dynamic", "unit_type", "unit_id", "status")
            tree_frame = ttk.Frame(rack_view)
            tree_frame.grid(row=2, column=0, sticky="nsew", padx=8)
            tree_frame.columnconfigure(0, weight=1); tree_frame.rowconfigure(0, weight=1)
            self.slot_tree = ttk.Treeview(tree_frame, columns=columns, show="headings")
            headings = {"rank":"Rank", "sku":"SKU", "class":"ABC", "static":"Current static address", "dynamic":"Current dynamic address", "unit_type":"Unit type", "unit_id":"Handling unit ID", "status":"Status"}
            widths = {"rank":55, "sku":95, "class":50, "static":150, "dynamic":230, "unit_type":90, "unit_id":120, "status":90}
            for column in columns:
                self.slot_tree.heading(column, text=headings[column]); self.slot_tree.column(column, width=widths[column], anchor="center" if column not in {"static","dynamic"} else "w")
            yscroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.slot_tree.yview)
            xscroll = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.slot_tree.xview)
            self.slot_tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
            self.slot_tree.grid(row=0, column=0, sticky="nsew"); yscroll.grid(row=0, column=1, sticky="ns"); xscroll.grid(row=1, column=0, sticky="ew")
            ttk.Button(rack_view, text="Show all assignments", command=lambda: self.show_slotting_rows(self.slot_rows)).grid(row=3, column=0, sticky="w", padx=8, pady=(7, 0))

        def _build_operations_tab(self,parent):
            base=Path(__file__).resolve().parent
            self.ops_layout_path=tk.StringVar(value=str(base/"resources/data/basic_slotting_layout.slotting.json"))
            self.ops_search=tk.StringVar(); self.ops_source_sku=tk.StringVar(); self.ops_target_sku=tk.StringVar()
            self.ops_source_label=tk.StringVar(value="Source SKU");self.ops_target_label=tk.StringVar(value="Target SKU")
            self.ops_swap_mode=tk.StringVar(value="SKU slot")
            self.ops_status=tk.StringVar(value="Load a generated slotting layout to begin.")
            self.ops_details=tk.StringVar(value="Search for a SKU to show its current addresses and map position.")
            self.ops_payload=None; self.ops_building=None; self.ops_rows=[]; self.ops_racks=[]; self.ops_highlight_rack=None
            self.ops_shelf_selection=[];self.ops_sku_selection=[];self.ops_inventory_rows={}
            parent.columnconfigure(0,weight=1); parent.rowconfigure(1,weight=1)
            top=ttk.LabelFrame(parent,text="Slotting layout",padding=10); top.grid(row=0,column=0,sticky="ew",padx=12,pady=12); top.columnconfigure(1,weight=1)
            ttk.Label(top,text="Layout JSON").grid(row=0,column=0,sticky="w",padx=(0,8))
            ttk.Entry(top,textvariable=self.ops_layout_path).grid(row=0,column=1,sticky="ew")
            ttk.Button(top,text="Browse…",command=self.browse_ops_layout).grid(row=0,column=2,padx=(8,0))
            ttk.Button(top,text="Load layout",command=self.load_ops_layout).grid(row=0,column=3,padx=(6,0))
            ttk.Button(top,text="Save changes as…",command=self.save_ops_layout).grid(row=0,column=4,padx=(6,0))
            ttk.Label(top,textvariable=self.ops_status,foreground="#315b66").grid(row=1,column=0,columnspan=5,sticky="w",pady=(7,0))

            paned=ttk.Panedwindow(parent,orient="horizontal"); paned.grid(row=1,column=0,sticky="nsew",padx=12,pady=(0,12))
            map_frame=ttk.LabelFrame(paned,text="Current inventory layout",padding=8)
            control=ttk.Frame(paned,padding=8); paned.add(map_frame,weight=3); paned.add(control,weight=2)
            map_frame.columnconfigure(0,weight=1); map_frame.rowconfigure(0,weight=1)
            control.columnconfigure(0,weight=1); control.rowconfigure(2,weight=2); control.rowconfigure(5,weight=1)
            self.ops_canvas=tk.Canvas(map_frame,background="white",highlightthickness=1,highlightbackground="#9aa8ae")
            self.ops_canvas.grid(row=0,column=0,sticky="nsew"); self.ops_canvas.bind("<Configure>",lambda _event:self.draw_ops_layout())
            self.ops_canvas.tag_bind("ops_rack","<Button-1>",self.ops_rack_click)
            ttk.Label(map_frame,text="Highlighted ring = searched SKU position · click a rack to inspect its contents",foreground="#4d646d").grid(row=1,column=0,sticky="w",pady=(5,0))

            search=ttk.LabelFrame(control,text="1. Find SKU",padding=10); search.grid(row=0,column=0,sticky="ew",pady=(0,8)); search.columnconfigure(0,weight=1)
            ttk.Entry(search,textvariable=self.ops_search).grid(row=0,column=0,sticky="ew")
            ttk.Button(search,text="Search",command=self.search_ops_sku).grid(row=0,column=1,padx=(6,0))
            ttk.Label(control,textvariable=self.ops_details,justify="left",wraplength=470).grid(row=1,column=0,sticky="ew",pady=(0,10))

            inventory=ttk.LabelFrame(control,text="SELECTED RACK INVENTORY",padding=6); inventory.grid(row=2,column=0,sticky="nsew",pady=(0,8)); inventory.columnconfigure(0,weight=1); inventory.rowconfigure(0,weight=1)
            columns=("sku","class","static","dynamic","unit")
            self.ops_inventory_tree=ttk.Treeview(inventory,columns=columns,show="headings",height=8)
            headings={"sku":"SKU","class":"ABC","static":"Static address","dynamic":"Dynamic address","unit":"Shelf / unit"}
            widths={"sku":100,"class":45,"static":190,"dynamic":220,"unit":110}
            for column in columns:
                self.ops_inventory_tree.heading(column,text=headings[column]);self.ops_inventory_tree.column(column,width=widths[column],anchor="center" if column in {"class","unit"} else "w")
            inventory_y=ttk.Scrollbar(inventory,orient="vertical",command=self.ops_inventory_tree.yview)
            inventory_x=ttk.Scrollbar(inventory,orient="horizontal",command=self.ops_inventory_tree.xview)
            self.ops_inventory_tree.configure(yscrollcommand=inventory_y.set,xscrollcommand=inventory_x.set)
            self.ops_inventory_tree.grid(row=0,column=0,sticky="nsew");inventory_y.grid(row=0,column=1,sticky="ns");inventory_x.grid(row=1,column=0,sticky="ew")
            self.ops_inventory_tree.bind("<<TreeviewSelect>>",self.ops_inventory_select)

            swap=ttk.LabelFrame(control,text="2. Mock position swap",padding=10); swap.grid(row=3,column=0,sticky="ew",pady=(0,8)); swap.columnconfigure(1,weight=1)
            ttk.Label(swap,textvariable=self.ops_source_label).grid(row=0,column=0,sticky="w",padx=(0,8),pady=3); ttk.Entry(swap,textvariable=self.ops_source_sku).grid(row=0,column=1,sticky="ew",pady=3)
            ttk.Label(swap,textvariable=self.ops_target_label).grid(row=1,column=0,sticky="w",padx=(0,8),pady=3); ttk.Entry(swap,textvariable=self.ops_target_sku).grid(row=1,column=1,sticky="ew",pady=3)
            ttk.Label(swap,text="Swap type").grid(row=2,column=0,sticky="w",padx=(0,8),pady=3)
            self.ops_swap_box=ttk.Combobox(swap,textvariable=self.ops_swap_mode,state="readonly",values=("SKU slot","Whole shelf"),width=18)
            self.ops_swap_box.grid(row=2,column=1,sticky="w",pady=3);self.ops_swap_box.bind("<<ComboboxSelected>>",self.ops_swap_mode_changed)
            ttk.Button(swap,text="Execute mock swap",command=self.execute_ops_swap).grid(row=3,column=1,sticky="w",pady=(8,2))
            ttk.Label(swap,text="SKU slot: click two SKU rows. Whole shelf: click two occupied rack points. Review the fields, then execute.",foreground="#4d646d",wraplength=390).grid(row=4,column=0,columnspan=2,sticky="w",pady=(6,0))

            ttk.Label(control,text="OPERATION LOG",font=("TkDefaultFont",10,"bold")).grid(row=4,column=0,sticky="w",pady=(6,4))
            self.ops_log=tk.Listbox(control,height=8); self.ops_log.grid(row=5,column=0,sticky="nsew")

        def browse_ops_layout(self):
            path=filedialog.askopenfilename(filetypes=[("Slotting layout","*.slotting.json"),("JSON","*.json"),("All files","*")],initialdir=str(Path(self.ops_layout_path.get()).expanduser().parent))
            if path:self.ops_layout_path.set(path)

        def load_ops_layout(self):
            try:
                payload=load_slotting_layout_json(Path(self.ops_layout_path.get()).expanduser())
                _,racks,workstations,unreachable=_rack_distances(payload["building"])
            except (OSError,ValueError,TypeError,json.JSONDecodeError,yaml.YAMLError) as exc:
                messagebox.showerror("Layout load failed",str(exc));return
            self.ops_payload=payload;self.ops_building=payload["building"];self.ops_rows=payload["assignments"];self.ops_racks=racks;self.ops_highlight_rack=None;self.ops_shelf_selection=[];self.ops_sku_selection=[]
            self.ops_source_sku.set("");self.ops_target_sku.set("")
            self.show_ops_rack_inventory(None)
            self.ops_log.delete(0,"end")
            for event in payload.get("operation_log",[]):self.ops_log.insert("end",event.get("message",str(event)))
            self.ops_status.set(f"Loaded {len(self.ops_rows):,} SKU assignments · {len(racks)} racks · {workstations} workstations · {unreachable} unreachable racks")
            self.ops_details.set("Search for a SKU or click a rack to inspect current inventory addresses.");self.draw_ops_layout()

        def save_ops_layout(self):
            if not self.ops_payload:
                messagebox.showinfo("Load layout","Load a slotting layout first.");return
            path=filedialog.asksaveasfilename(defaultextension=".slotting.json",filetypes=[("Slotting layout","*.slotting.json"),("JSON","*.json")],initialdir=str(Path(self.ops_layout_path.get()).expanduser().parent),initialfile=Path(self.ops_layout_path.get()).name)
            if not path:return
            self.ops_payload["assignments"]=self.ops_rows;self.ops_payload["modified_at"]=datetime.now(timezone.utc).isoformat()
            Path(path).write_text(json.dumps(self.ops_payload,indent=2)+"\n",encoding="utf-8");self.ops_layout_path.set(path);self.ops_status.set(f"Saved modified layout: {path}")

        def ops_geometry(self,vertices):
            xs=[float(v[0]) for v in vertices];ys=[float(v[1]) for v in vertices];min_x,max_x,min_y,max_y=min(xs),max(xs),min(ys),max(ys)
            width=max(300,self.ops_canvas.winfo_width());height=max(300,self.ops_canvas.winfo_height());padding=28
            scale=min((width-2*padding)/max(1e-9,max_x-min_x),(height-2*padding)/max(1e-9,max_y-min_y));return min_x,max_x,min_y,max_y,width,height,padding,scale

        def ops_screen_point(self,x,y,geometry):
            min_x,max_x,min_y,max_y,width,height,padding,scale=geometry;sx=padding+(float(x)-min_x)*scale
            sy=padding+(float(y)-min_y)*scale if self.ops_building.get("coordinate_system")=="reference_image" else height-padding-(float(y)-min_y)*scale
            return sx,sy

        def draw_ops_layout(self):
            if not hasattr(self,"ops_canvas"):return
            self.ops_canvas.delete("all")
            if not self.ops_building:return
            _,level=next(iter(self.ops_building["levels"].items()));vertices=level.get("vertices",[])
            if not vertices:return
            geometry=self.ops_geometry(vertices)
            for lane in level.get("lanes",[]):
                if len(lane)<2:continue
                a,b=vertices[lane[0]],vertices[lane[1]];x1,y1=self.ops_screen_point(a[0],a[1],geometry);x2,y2=self.ops_screen_point(b[0],b[1],geometry);self.ops_canvas.create_line(x1,y1,x2,y2,fill="#d9e0e3")
            grouped={}
            for row in self.ops_rows:
                if row.get("assignment_status")=="ASSIGNED":grouped.setdefault(row.get("rack_id",""),[]).append(row)
            colors={"A":"#d1495b","B":"#f3a712","C":"#4c9f70"}
            shelf_racks={selection["rack_id"] for selection in self.ops_shelf_selection}
            for rack in self.ops_racks:
                x,y=self.ops_screen_point(rack["x"],rack["y"],geometry);rows=grouped.get(rack["rack_id"],[]);hot=sorted((r.get("velocity_class","") for r in rows),key=lambda c:{"A":0,"B":1,"C":2}.get(c,9));fill=colors.get(hot[0] if hot else "","#aeb8bc")
                shelf_selected=rack["rack_id"] in shelf_racks;selected=rack["rack_id"]==self.ops_highlight_rack;radius=11 if shelf_selected else (9 if selected else 4)
                outline="#e07a1f" if shelf_selected else ("#087f8c" if selected else "white")
                self.ops_canvas.create_oval(x-radius,y-radius,x+radius,y+radius,fill=fill,outline=outline,width=4 if shelf_selected else (3 if selected else 1),tags=("ops_rack",f"opsrack:{rack['rack_id']}"))
                if selected:
                    self.ops_canvas.create_text(x,y-16,text=rack["rack_id"],fill="#065f69",font=("TkDefaultFont",9,"bold"))

        def show_ops_rack_inventory(self,rack_id):
            if not hasattr(self,"ops_inventory_tree"):return
            self.ops_inventory_rows={}
            self.ops_inventory_tree.delete(*self.ops_inventory_tree.get_children())
            if not rack_id:return
            rows=[row for row in self.ops_rows if row.get("assignment_status")=="ASSIGNED" and row.get("rack_id")==rack_id]
            rows.sort(key=lambda row:(int(row.get("storage_level") or 0),int(row.get("storage_slot") or 0),str(row.get("sku",""))))
            for row in rows:
                item=self.ops_inventory_tree.insert("","end",values=(row.get("sku",""),row.get("velocity_class",""),row.get("static_address",""),row.get("dynamic_address",""),row.get("handling_unit_id","")))
                self.ops_inventory_rows[item]=row

        def ops_inventory_select(self,_event=None):
            selected=self.ops_inventory_tree.selection()
            if not selected:return
            row=self.ops_inventory_rows.get(selected[0])
            if row and self.ops_swap_mode.get()=="SKU slot":self.select_ops_sku_for_swap(row)

        def select_ops_sku_for_swap(self,row):
            sku=str(row.get("sku",""))
            if not sku:return
            if len(self.ops_sku_selection)>=2:self.ops_sku_selection=[]
            if sku in self.ops_sku_selection:return
            self.ops_sku_selection.append(sku)
            self.ops_source_sku.set(self.ops_sku_selection[0])
            self.ops_target_sku.set(self.ops_sku_selection[1] if len(self.ops_sku_selection)>1 else "")
            if len(self.ops_sku_selection)==1:self.ops_status.set(f"Selected source SKU {sku} · select the target SKU.")
            else:self.ops_status.set(f"Selected SKU swap: {self.ops_sku_selection[0]} ↔ {self.ops_sku_selection[1]} · click Execute mock swap.")

        def show_ops_assignment(self,row):
            self.ops_highlight_rack=row.get("rack_id","")
            self.show_ops_rack_inventory(self.ops_highlight_rack)
            self.ops_details.set(
                f"SKU: {row.get('sku','')} · ABC class {row.get('velocity_class','')} · quantity {row.get('total_quantity_ea','')} EA\n"
                f"Current static address: {row.get('static_address','')}\nCurrent dynamic address: {row.get('dynamic_address','')}\n"
                f"Handling unit: {row.get('handling_unit_id','')} ({row.get('handling_unit_type','')})\nRMF grid position: {row.get('rmf_grid_address','')}"
            );self.draw_ops_layout()

        def search_ops_sku(self):
            if not self.ops_rows:
                messagebox.showinfo("Load layout","Load a slotting layout first.");return
            try:row=_find_sku_assignment(self.ops_rows,self.ops_search.get())
            except ValueError as exc:messagebox.showerror("SKU search",str(exc));return
            self.show_ops_assignment(row)
            if self.ops_swap_mode.get()=="SKU slot":self.select_ops_sku_for_swap(row)
            else:self.ops_status.set(f"Located SKU {row['sku']} at {row['static_address']}")

        def ops_rack_click(self,event):
            item=self.ops_canvas.find_withtag("current")
            if not item:return
            tags=self.ops_canvas.gettags(item[0]);found=[tag.split(":",1)[1] for tag in tags if tag.startswith("opsrack:")]
            if not found:return
            rack_id=found[0];rows=[row for row in self.ops_rows if row.get("assignment_status")=="ASSIGNED" and row.get("rack_id")==rack_id]
            self.ops_highlight_rack=rack_id
            self.show_ops_rack_inventory(rack_id)
            units=sorted({str(row.get("handling_unit_id","")) for row in rows if row.get("handling_unit_id")})
            self.ops_details.set(f"Rack {rack_id} · {len(rows)} assigned SKU(s)\nShelf / handling unit: "+(", ".join(units) if units else "empty"))
            if self.ops_swap_mode.get()=="Whole shelf":
                self.select_ops_shelf_for_swap(rack_id,rows)
            else:self.draw_ops_layout()

        def ops_swap_mode_changed(self,_event=None):
            self.ops_shelf_selection=[];self.ops_sku_selection=[];self.ops_source_sku.set("");self.ops_target_sku.set("");self.draw_ops_layout()
            if self.ops_swap_mode.get()=="Whole shelf":
                if self.ops_payload and self.ops_payload.get("handling_unit_type") != "AMR shelf":
                    messagebox.showinfo("AMR shelf only","Whole-shelf swap is only available for AMR shelf layouts. Tote and pallet units are addressed at slot level.")
                    self.ops_swap_mode.set("SKU slot");self.ops_source_label.set("Source SKU");self.ops_target_label.set("Target SKU");self.ops_status.set("SKU slot mode: click two SKU rows, then click Execute mock swap.");return
                self.ops_source_label.set("Source shelf");self.ops_target_label.set("Target shelf");self.ops_status.set("Whole shelf mode: click two occupied rack points, then click Execute mock swap.")
            else:
                self.ops_source_label.set("Source SKU");self.ops_target_label.set("Target SKU");self.ops_status.set("SKU slot mode: click two SKU rows, then click Execute mock swap.")

        def select_ops_shelf_for_swap(self,rack_id,rows):
            units=sorted({str(row.get("handling_unit_id","")) for row in rows if row.get("handling_unit_id")})
            if not units:
                messagebox.showinfo("Empty rack","This rack has no shelf to swap.");self.draw_ops_layout();return
            if len(units)>1:
                messagebox.showerror("Shelf selection",f"Rack {rack_id} contains multiple handling units; select a SKU from the required shelf instead.");self.draw_ops_layout();return
            unit=units[0]
            if len(self.ops_shelf_selection)>=2:self.ops_shelf_selection=[]
            if any(selection["unit_id"]==unit for selection in self.ops_shelf_selection):
                messagebox.showinfo("Select another shelf","Click a different shelf for the swap.");self.draw_ops_layout();return
            self.ops_shelf_selection.append({"rack_id":rack_id,"unit_id":unit})
            self.ops_source_sku.set(self.ops_shelf_selection[0]["unit_id"])
            self.ops_target_sku.set(self.ops_shelf_selection[1]["unit_id"] if len(self.ops_shelf_selection)>1 else "")
            if len(self.ops_shelf_selection)==1:self.ops_status.set(f"Selected source shelf {unit} at {rack_id} · click the target shelf.")
            else:self.ops_status.set(f"Selected shelf swap: {self.ops_shelf_selection[0]['unit_id']} ↔ {unit} · click Execute mock swap.")
            self.draw_ops_layout()

        def perform_ops_swap(self,source,target,mode):
            timestamp=datetime.now(timezone.utc).isoformat()
            try:
                if mode=="SKU slot":
                    first,second=swap_sku_slots(self.ops_rows,source,target);message=f"Swapped SKU slots: {first['sku']} ↔ {second['sku']}"
                    row=_find_sku_assignment(self.ops_rows,source)
                else:
                    first_unit,second_unit,first_count,second_count=swap_whole_shelf_units(self.ops_rows,source,target);message=f"Swapped shelves: {first_unit} ({first_count} SKUs) ↔ {second_unit} ({second_count} SKUs)"
                    row=next(row for row in self.ops_rows if row.get("handling_unit_id")==first_unit)
            except ValueError as exc:messagebox.showerror("Swap failed",str(exc));return False
            event={"timestamp":timestamp,"type":mode,"source":source,"target":target,"message":message};self.ops_payload.setdefault("operation_log",[]).append(event);self.ops_log.insert("end",f"{timestamp[:19]}  {message}");self.ops_log.see("end")
            self.ops_shelf_selection=[];self.ops_sku_selection=[];self.show_ops_assignment(row);self.ops_status.set(message+" · save changes to persist the demo result")
            return True

        def execute_ops_swap(self):
            if not self.ops_payload:
                messagebox.showinfo("Load layout","Load a slotting layout first.");return
            source,target=self.ops_source_sku.get().strip(),self.ops_target_sku.get().strip();self.perform_ops_swap(source,target,self.ops_swap_mode.get())

        def browse_slot_input(self, variable, filetypes):
            path = filedialog.askopenfilename(filetypes=filetypes, initialdir=str(Path(variable.get()).expanduser().parent))
            if path: variable.set(path)

        def browse_slot_output(self):
            path = filedialog.asksaveasfilename(defaultextension=".slotting.json", filetypes=[("Slotting layout", "*.slotting.json"), ("JSON", "*.json")], initialdir=str(Path(self.slot_output_path.get()).expanduser().parent), initialfile=Path(self.slot_output_path.get()).name)
            if path: self.slot_output_path.set(path)

        def load_slot_building(self):
            try:
                path=Path(self.slot_building_path.get()).expanduser().resolve()
                building=load_building_yaml(path)
                _,racks,workstations,unreachable=_rack_distances(building)
            except (OSError,ValueError,TypeError,yaml.YAMLError) as exc:
                messagebox.showerror("Building map load failed",str(exc)); return
            self.slot_building=building; self.slot_racks=racks; self.slot_loaded_path=path
            self.slot_zone_assignments={}; self.slot_rows=[]; self.slot_selected_rack=None
            self.slot_zone.set("Z01")
            self.slot_zone_drag_start=None; self.slot_zone_drag_current=None; self.slot_zone_mode.set(True)
            self.show_slotting_rows([]); self.draw_slotting_layout()
            self.slot_summary.set(f"Loaded {len(racks)} racks and {workstations} workstations · {unreachable} unreachable · assign zones by dragging rectangles")
            self.slot_rack_detail.set("Zone grouping mode is active. Enter a zone ID and drag a rectangle over a group of racks.")

        def zone_mode_changed(self):
            self.slot_zone_drag_start=None; self.slot_zone_drag_current=None
            self.draw_slotting_layout()

        def clear_slot_zones(self):
            if not self.slot_building: return
            self.slot_zone_assignments={}; self.slot_rows=[]; self.slot_selected_rack=None; self.slot_zone_mode.set(True)
            self.slot_zone.set("Z01")
            self.show_slotting_rows([]); self.draw_slotting_layout()
            self.slot_summary.set(f"Cleared zone assignments for {len(self.slot_racks)} racks.")

        def slot_canvas_press(self,event):
            if not self.slot_building or not self.slot_zone_mode.get(): return
            self.slot_zone_drag_start=(event.x,event.y); self.slot_zone_drag_current=(event.x,event.y)
            self.draw_slotting_layout()

        def slot_canvas_drag(self,event):
            if self.slot_zone_drag_start is None or not self.slot_zone_mode.get(): return
            self.slot_zone_drag_current=(event.x,event.y); self.draw_slotting_layout()

        def slot_canvas_release(self,event):
            if self.slot_zone_drag_start is None or not self.slot_zone_mode.get(): return
            self.slot_zone_drag_current=(event.x,event.y)
            zone=self.slot_zone.get().strip()
            if not zone:
                self.slot_zone_drag_start=None; self.slot_zone_drag_current=None
                messagebox.showerror("Zone ID","Enter a zone ID before selecting racks."); return
            x1,y1=self.slot_zone_drag_start; x2,y2=self.slot_zone_drag_current
            left,right=sorted((x1,x2)); top,bottom=sorted((y1,y2))
            _,level=next(iter(self.slot_building["levels"].items())); geometry=self.slotting_geometry(level["vertices"])
            selected=[]
            for rack in self.slot_racks:
                x,y=self.slotting_screen_point(rack["x"],rack["y"],geometry)
                if left-6<=x<=right+6 and top-6<=y<=bottom+6:
                    self.slot_zone_assignments[rack["waypoint"]]=zone; rack["zone_id"]=zone; selected.append(rack)
            apply_zone_local_aisles(self.slot_building,self.slot_racks,self.slot_zone_assignments,"UNASSIGNED")
            self.slot_zone_drag_start=None; self.slot_zone_drag_current=None
            zone_counts={}
            for value in self.slot_zone_assignments.values(): zone_counts[value]=zone_counts.get(value,0)+1
            remaining=len(self.slot_racks)-len(self.slot_zone_assignments)
            if selected and self.slot_zone_auto.get(): self.slot_zone.set(next_zone_id(zone))
            self.slot_summary.set(f"Assigned {len(selected)} rack(s) to {zone} · zones: "+", ".join(f"{key}={value}" for key,value in sorted(zone_counts.items()))+f" · {remaining} unassigned · next ID {self.slot_zone.get()}")
            self.slot_rack_detail.set(f"Zone {zone}: selected {len(selected)} rack(s). The next rectangle will use {self.slot_zone.get()}.")
            self.draw_slotting_layout()

        def run_slotting(self):
            try:
                if self.slot_strategy.get() != "basic":
                    raise ValueError("only the basic strategy is available in this demo")
                current_path=Path(self.slot_building_path.get()).expanduser().resolve()
                if self.slot_building is None or self.slot_loaded_path!=current_path:
                    raise ValueError("load the selected building YAML before generating")
                unassigned=[rack["waypoint"] for rack in self.slot_racks if rack["waypoint"] not in self.slot_zone_assignments]
                if unassigned:
                    raise ValueError(f"assign a zone to all racks first; {len(unassigned)} remain unassigned")
                building=self.slot_building
                skus=load_velocity_csv(Path(self.slot_velocity_path.get()).expanduser())
                levels=int(self.slot_levels.get()); slots=int(self.slot_slots.get())
                rows, summary = generate_basic_slotting(building, skus, levels, slots, self.slot_handling_unit.get(), self.slot_zone.get(), self.slot_zone_assignments)
                write_slotting_layout_json(rows,building,summary,Path(self.slot_output_path.get()).expanduser(),strategy=self.slot_strategy.get(),handling_unit_type=self.slot_handling_unit.get(),levels_per_rack=levels,slots_per_level=slots,zone_assignments=self.slot_zone_assignments,source_building=str(current_path),source_velocity=str(Path(self.slot_velocity_path.get()).expanduser().resolve()))
            except (OSError, ValueError, TypeError, yaml.YAMLError) as exc:
                messagebox.showerror("Slotting generation failed", str(exc)); return
            self.slot_rows = rows
            apply_zone_local_aisles(building,self.slot_racks,self.slot_zone_assignments,self.slot_zone.get())
            self.slot_selected_rack = None
            self.slot_zone_mode.set(False)
            self.show_slotting_rows(rows)
            self.draw_slotting_layout()
            self.slot_rack_detail.set("Click a coloured rack point on the map to inspect its assignments.")
            self.slot_summary.set(
                f"Assigned {summary['assigned_count']:,}/{summary['sku_count']:,} SKUs · "
                f"{summary['rack_count']} racks ({summary['unreachable_rack_count']} unreachable) · "
                f"{summary['workstation_count']} workstations · {summary['zone_count']} zones · capacity {summary['capacity']:,} · "
                f"saved to {self.slot_output_path.get()}"
            )

        def show_slotting_rows(self, rows):
            self.slot_tree.delete(*self.slot_tree.get_children())
            for row in rows[:1000]:
                self.slot_tree.insert("", "end", values=(row["sku_rank"], row["sku"], row["velocity_class"], row["static_address"], row["dynamic_address"], row["handling_unit_type"], row["handling_unit_id"], row["assignment_status"]))

        def slotting_geometry(self, vertices):
            xs=[float(v[0]) for v in vertices]; ys=[float(v[1]) for v in vertices]
            min_x,max_x,min_y,max_y=min(xs),max(xs),min(ys),max(ys)
            width=max(300,self.slot_canvas.winfo_width()); height=max(300,self.slot_canvas.winfo_height()); padding=28
            scale=min((width-2*padding)/max(1e-9,max_x-min_x),(height-2*padding)/max(1e-9,max_y-min_y))
            return min_x,max_x,min_y,max_y,width,height,padding,scale

        def slotting_screen_point(self, x, y, geometry):
            min_x,max_x,min_y,max_y,width,height,padding,scale=geometry
            screen_x=padding+(float(x)-min_x)*scale
            if self.slot_building.get("coordinate_system")=="reference_image": screen_y=padding+(float(y)-min_y)*scale
            else: screen_y=height-padding-(float(y)-min_y)*scale
            return screen_x,screen_y

        def draw_slotting_layout(self):
            if not hasattr(self,"slot_canvas"): return
            self.slot_canvas.delete("all")
            if not self.slot_building: return
            _,level=next(iter(self.slot_building["levels"].items())); vertices=level.get("vertices",[])
            if not vertices: return
            geometry=self.slotting_geometry(vertices)
            for lane in level.get("lanes",[]):
                if len(lane)<2 or lane[0]>=len(vertices) or lane[1]>=len(vertices): continue
                a,b=vertices[lane[0]],vertices[lane[1]]; x1,y1=self.slotting_screen_point(a[0],a[1],geometry); x2,y2=self.slotting_screen_point(b[0],b[1],geometry)
                self.slot_canvas.create_line(x1,y1,x2,y2,fill="#d9e0e3",width=1)
            assignments={}
            for row in self.slot_rows:
                if row["assignment_status"]=="ASSIGNED": assignments.setdefault(row["rack_id"],[]).append(row)
            class_colors={"A":"#d1495b","B":"#f3a712","C":"#4c9f70"}
            zone_palette=("#6c8cd5","#9b70c7","#31a6a0","#d47b4c","#8ca63c","#c75d8b","#81756e","#3d8fbe")
            zones=sorted(set(self.slot_zone_assignments.values()))
            zone_colors={zone:zone_palette[index%len(zone_palette)] for index,zone in enumerate(zones)}
            zone_view=self.slot_zone_mode.get() or not self.slot_rows
            for rack in self.slot_racks:
                x,y=self.slotting_screen_point(rack["x"],rack["y"],geometry); rack_rows=assignments.get(rack["rack_id"],[])
                if zone_view: fill=zone_colors.get(self.slot_zone_assignments.get(rack["waypoint"],""),"#aeb8bc")
                else:
                    hottest=rack_rows[0]["velocity_class"] if rack_rows else ""; fill=class_colors.get(hottest,"#7b8b92")
                radius=7 if rack["rack_id"]==self.slot_selected_rack else 4
                self.slot_canvas.create_oval(x-radius,y-radius,x+radius,y+radius,fill=fill,outline="#087f8c" if radius==7 else "white",width=3 if radius==7 else 1,tags=("rack",f"rack:{rack['rack_id']}"))
            for index,vertex in enumerate(vertices):
                params=vertex[4] if len(vertex)>4 and isinstance(vertex[4],dict) else {}
                if "dropoff_ingestor" not in params: continue
                endpoint=str(_typed_value(params["dropoff_ingestor"],vertex[3])); x,y=self.slotting_screen_point(vertex[0],vertex[1],geometry); r=6
                self.slot_canvas.create_polygon(x,y-r,x+r,y,x,y+r,x-r,y,fill="#277da1",outline="white")
                self.slot_canvas.create_text(x,y-11,text=endpoint,fill="#1d5d78",font=("TkDefaultFont",8,"bold"))
            if self.slot_zone_drag_start is not None and self.slot_zone_drag_current is not None:
                x1,y1=self.slot_zone_drag_start; x2,y2=self.slot_zone_drag_current
                self.slot_canvas.create_rectangle(x1,y1,x2,y2,outline="#7b2cbf",width=2,dash=(5,3))
            if zone_view:
                counts={zone:sum(value==zone for value in self.slot_zone_assignments.values()) for zone in zones}
                self.slot_legend.set("Zone grouping · drag rectangle · "+(" · ".join(f"{zone}: {counts[zone]} racks" for zone in zones) if zones else "all racks unassigned"))
            else:
                self.slot_legend.set("Rack colour: A = red · B = orange · C = green · Empty = grey · ◆ Workstation = blue")

        def slot_rack_click(self, event):
            if self.slot_zone_mode.get(): return
            item=self.slot_canvas.find_withtag("current")
            if not item: return
            tags=self.slot_canvas.gettags(item[0]); rack_tags=[tag for tag in tags if tag.startswith("rack:")]
            if not rack_tags: return
            rack_id=rack_tags[0].split(":",1)[1]
            self.slot_selected_rack=rack_id
            rack=next((item for item in self.slot_racks if item["rack_id"]==rack_id),None)
            rows=[row for row in self.slot_rows if row["rack_id"]==rack_id and row["assignment_status"]=="ASSIGNED"]
            self.show_slotting_rows(rows); self.draw_slotting_layout()
            if not rack: return
            classes={label:sum(row["velocity_class"]==label for row in rows) for label in ("A","B","C")}
            unit_ids=sorted({row["handling_unit_id"] for row in rows})
            self.slot_rack_detail.set(
                f"Static grid rack: {rack_id}\nPickup dispenser: {rack['pickup_dispenser_id']} · vertex {rack['vertex_index']}\n"
                f"Current static address: {rows[0]['static_address'] if rows else rack.get('zone_id','UNASSIGNED')+'/'+rack['aisle_id']+'/'+rack['static_bay_id']+'/L--/S--'}\n"
                f"Assigned SKUs: {len(rows)} · A {classes['A']} / B {classes['B']} / C {classes['C']}\n"
                f"Handling unit(s): {', '.join(unit_ids) if unit_ids else 'none'}"
            )

        def spec_from_inputs(self) -> GridSpec:
            spec = GridSpec(float(self.width.get()), float(self.length.get()), float(self.spacing.get()), self.map_name.get().strip(), self.level_name.get().strip())
            spec.validate()
            return spec

        def generate_grid(self):
            try:
                spec = self.spec_from_inputs()
            except ValueError as exc:
                messagebox.showerror("Invalid grid", str(exc)); return
            if self.project.markers and not messagebox.askyesno("Reset grid", "Generating a new grid removes all rack and workstation markers. Continue?"):
                return
            self.push_undo()
            self.project = GridProject(spec)
            self.selected = None
            self.bulk_anchor = None
            self.update_selected_editor()
            self.redraw()
            self.status.set("Grid generated. All neighbouring points are connected bidirectionally.")

        def geometry(self):
            width = max(200, self.canvas.winfo_width())
            height = max(200, self.canvas.winfo_height())
            padding = 45
            scale = min((width - 2 * padding) / self.project.grid.width_m, (height - 2 * padding) / self.project.grid.length_m)
            return padding, scale, height

        def screen_point(self, column, row):
            padding, scale, height = self.geometry()
            x = padding + column * self.project.grid.spacing_m * scale
            y = height - padding - row * self.project.grid.spacing_m * scale
            return x, y

        def redraw(self):
            if not hasattr(self, "canvas"): return
            self.canvas.delete("all")
            spec = self.project.grid
            for row in range(spec.rows + 1):
                x1, y1 = self.screen_point(0, row); x2, y2 = self.screen_point(spec.columns, row)
                self.canvas.create_line(x1, y1, x2, y2, fill="#d7dfe2")
            for column in range(spec.columns + 1):
                x1, y1 = self.screen_point(column, 0); x2, y2 = self.screen_point(column, spec.rows)
                self.canvas.create_line(x1, y1, x2, y2, fill="#d7dfe2")
            radius = max(2, min(5, self.geometry()[1] * spec.spacing_m * 0.10))
            for column, row in self.project.iter_positions():
                x, y = self.screen_point(column, row)
                marker = self.project.markers.get((column, row))
                fill = "#d1495b" if marker and marker.role == "workstation" else "#f3a712" if marker else "#50656e"
                r = radius + 3 if self.selected == (column, row) else radius
                outline = "#087f8c" if self.selected == (column, row) else fill
                self.canvas.create_oval(x-r, y-r, x+r, y+r, fill=fill, outline=outline, width=3 if self.selected == (column, row) else 1)
                if marker:
                    self.canvas.create_text(x, y-12, text=marker.endpoint_id, fill=fill, font=("TkDefaultFont", 8, "bold"))
            if self.bulk_anchor is not None:
                x, y = self.screen_point(*self.bulk_anchor)
                self.canvas.create_oval(x-9, y-9, x+9, y+9, outline="#7b2cbf", width=3)
            x0, y0 = self.screen_point(0, 0)
            self.canvas.create_text(x0, y0+20, text="(0, 0)", anchor="n", fill="#087f8c", font=("TkDefaultFont", 9, "bold"))
            self.summary.set(f"{spec.columns} columns × {spec.rows} rows\n{spec.vertex_count:,} vertices · {spec.edge_count:,} edges")

        def nearest_position(self, event) -> GridPosition | None:
            padding, scale, height = self.geometry()
            column = round((event.x - padding) / (self.project.grid.spacing_m * scale))
            row = round((height - padding - event.y) / (self.project.grid.spacing_m * scale))
            if 0 <= column <= self.project.grid.columns and 0 <= row <= self.project.grid.rows:
                x, y = self.screen_point(column, row)
                if math.hypot(event.x-x, event.y-y) <= max(12, self.project.grid.spacing_m*scale*.35):
                    return column, row
            return None

        def canvas_click(self, event):
            position = self.nearest_position(event)
            if position is None: return
            action = self.tool.get()
            if action == "clear":
                self.push_undo(); self.drag_undo_started = True
                self.project.markers.pop(position, None)
            elif action == "rack":
                self.push_undo(); self.drag_undo_started = True
                self.place_rack(position)
            elif action == "rack_rectangle":
                if self.bulk_anchor is None:
                    self.bulk_anchor = position
                    self.status.set(f"Rack rectangle starts at {position}. Click the opposite corner.")
                else:
                    self.push_undo()
                    c1, r1 = self.bulk_anchor; c2, r2 = position
                    count = 0
                    for row in range(min(r1,r2), max(r1,r2)+1):
                        for column in range(min(c1,c2), max(c1,c2)+1):
                            self.place_rack((column,row), redraw=False); count += 1
                    self.bulk_anchor = None
                    self.status.set(f"Placed {count} rack pickup points.")
            elif action == "workstation":
                self.push_undo()
                self.project.markers[position] = Marker("workstation", f"WS_{position[0]}_{position[1]}")
            self.selected = position
            self.update_selected_editor()
            self.redraw()

        def canvas_drag(self, event):
            action = self.tool.get()
            if action not in {"rack", "clear"}: return
            position = self.nearest_position(event)
            if position is None or position == self.selected: return
            if not self.drag_undo_started:
                self.push_undo(); self.drag_undo_started = True
            if action == "rack": self.place_rack(position, redraw=False)
            else: self.project.markers.pop(position, None)
            self.selected = position
            self.update_selected_editor()
            self.redraw()

        def canvas_release(self, _event):
            self.drag_undo_started = False

        def place_rack(self, position: GridPosition, redraw=True):
            prefix = self.rack_prefix.get().strip() or "RACK"
            self.project.markers[position] = Marker("rack", f"{prefix}_{position[0]}_{position[1]}")
            if redraw: self.redraw()

        def snapshot(self) -> dict:
            return self.project.to_project_dict()

        def restore_snapshot(self, snapshot: dict):
            self.project = GridProject.from_project_dict(snapshot)
            self.selected = None
            self.bulk_anchor = None
            self.map_name.set(self.project.grid.map_name)
            self.level_name.set(self.project.grid.level_name)
            self.width.set(str(self.project.grid.width_m))
            self.length.set(str(self.project.grid.length_m))
            self.spacing.set(str(self.project.grid.spacing_m))
            self.update_selected_editor()
            self.redraw()

        def push_undo(self):
            self.undo_stack.append(self.snapshot())
            if len(self.undo_stack) > 100:
                self.undo_stack.pop(0)
            self.redo_stack.clear()

        def undo(self, _event=None):
            if not self.undo_stack:
                self.status.set("Nothing to undo.")
                return "break"
            self.redo_stack.append(self.snapshot())
            self.restore_snapshot(self.undo_stack.pop())
            self.status.set(f"Undo complete · {len(self.undo_stack)} earlier action(s)")
            return "break"

        def redo(self, _event=None):
            if not self.redo_stack:
                self.status.set("Nothing to redo.")
                return "break"
            self.undo_stack.append(self.snapshot())
            self.restore_snapshot(self.redo_stack.pop())
            self.status.set(f"Redo complete · {len(self.redo_stack)} later action(s)")
            return "break"

        def update_selected_editor(self):
            if self.selected is None:
                self.selected_coordinate.set("No grid point selected")
                self.role.set("none"); self.endpoint_id.set(""); return
            column, row = self.selected
            self.selected_coordinate.set(f"Column {column}, row {row}  →  ({column*self.project.grid.spacing_m:g}, {row*self.project.grid.spacing_m:g}) m")
            marker = self.project.markers.get(self.selected)
            self.role.set(marker.role if marker else "none")
            self.endpoint_id.set(marker.endpoint_id if marker else "")

        def apply_edit(self):
            if self.selected is None:
                messagebox.showinfo("Select a point", "Select a grid point first."); return
            role = self.role.get()
            before = self.snapshot()
            if role == "none": self.project.markers.pop(self.selected, None)
            else:
                endpoint = self.endpoint_id.get().strip()
                if not endpoint:
                    messagebox.showerror("Endpoint ID", "Endpoint ID cannot be blank."); return
                self.project.markers[self.selected] = Marker(role, endpoint)
            try: self.project.validate()
            except ValueError as exc:
                self.restore_snapshot(before); messagebox.showerror("Invalid edit", str(exc)); return
            self.undo_stack.append(before)
            if len(self.undo_stack) > 100: self.undo_stack.pop(0)
            self.redo_stack.clear()
            self.redraw(); self.status.set("Point edit applied.")

        def save_project_dialog(self):
            path = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("Grid project", "*.json")], initialfile=f"{self.project.grid.map_name}.grid.json")
            if path:
                try: self.project.validate(); write_project(self.project, Path(path)); self.status.set(f"Project saved: {path}")
                except (OSError, ValueError) as exc: messagebox.showerror("Save failed", str(exc))

        def load_project_dialog(self):
            path = filedialog.askopenfilename(filetypes=[("Grid project", "*.json"), ("All files", "*")])
            if not path: return
            try:
                data = json.loads(Path(path).read_text(encoding="utf-8")); loaded_project = GridProject.from_project_dict(data)
                self.push_undo(); self.project = loaded_project
                self.selected = None; self.map_name.set(self.project.grid.map_name); self.level_name.set(self.project.grid.level_name)
                self.width.set(str(self.project.grid.width_m)); self.length.set(str(self.project.grid.length_m)); self.spacing.set(str(self.project.grid.spacing_m))
                self.update_selected_editor(); self.redraw(); self.status.set(f"Project loaded: {path}")
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc: messagebox.showerror("Load failed", str(exc))

        def export_yaml_dialog(self):
            path = filedialog.asksaveasfilename(defaultextension=".building.yaml", filetypes=[("RMF building map", "*.building.yaml"), ("YAML", "*.yaml")], initialdir=str(DEFAULT_OUTPUT.parent), initialfile=DEFAULT_OUTPUT.name)
            if path:
                try:
                    self.project.validate(); write_building_yaml(self.project, Path(path))
                    self.status.set(f"RMF map exported: {path}")
                    messagebox.showinfo("Export complete", f"Generated {self.project.grid.vertex_count:,} vertices and {self.project.grid.edge_count:,} bidirectional edges.\n\n{path}")
                except (OSError, ValueError) as exc: messagebox.showerror("Export failed", str(exc))

    root = tk.Tk()
    Editor(root)
    root.mainloop()


def parse_marker(value: str) -> tuple[GridPosition, Marker]:
    try:
        role, column, row, endpoint_id = value.split(",", 3)
        return (int(column), int(row)), Marker(role.strip(), endpoint_id.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError("marker must be ROLE,COLUMN,ROW,ENDPOINT_ID") from exc


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--generate", action="store_true", help="Generate YAML without opening the GUI")
    parser.add_argument("--width", type=float, default=20.0, help="Total warehouse width in metres")
    parser.add_argument("--length", type=float, default=15.0, help="Total warehouse length in metres")
    parser.add_argument("--spacing", type=float, default=1.0, help="Distance between grid points in metres")
    parser.add_argument("--name", default="warehouse_grid", help="Building map name")
    parser.add_argument("--level", default="L1", help="RMF level name")
    parser.add_argument("--marker", action="append", type=parse_marker, default=[], metavar="ROLE,COL,ROW,ID", help="Add rack or workstation marker; may be repeated")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Output .building.yaml path")
    args = parser.parse_args()
    project = GridProject(GridSpec(args.width, args.length, args.spacing, args.name, args.level), dict(args.marker))
    try: project.validate()
    except ValueError as exc: parser.error(str(exc))
    if args.generate:
        write_building_yaml(project, args.output)
        print(f"Generated {project.grid.vertex_count:,} vertices and {project.grid.edge_count:,} bidirectional edges.")
        print(f"Bottom-left: G0_0 at (0, 0) metres")
        print(f"Output: {args.output}")
    else:
        run_gui(project)


if __name__ == "__main__":
    main()
