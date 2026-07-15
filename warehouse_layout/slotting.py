"""ABC slotting, route scoring, inventory addressing, and layout persistence."""

from __future__ import annotations

import csv
import heapq
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path

from .config import SLOTTING_SCHEMA
from .rmf import RmfMapService


class SlottingService:
    """Generate warehouse slotting recommendations from RMF and ABC inputs."""

    def __init__(self, rmf_maps: RmfMapService | None = None):
        self.rmf_maps = rmf_maps or RmfMapService()

    @staticmethod
    def next_zone_id(zone_id: str) -> str:
        value = zone_id.strip()
        match = re.match(r"^(.*?)(\d+)$", value)
        if match:
            prefix, number = match.groups()
            return f"{prefix}{int(number) + 1:0{len(number)}d}"
        return f"{value}_02"

    @staticmethod
    def typed_value(value, default=None):
        if isinstance(value, list) and len(value) >= 2:
            return value[1]
        return default

    def _distance_scale(self, building: dict, level: dict) -> float:
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
            drawing_distance = math.hypot(
                float(vertices[start][0]) - float(vertices[end][0]),
                float(vertices[start][1]) - float(vertices[end][1]),
            )
            real_distance = float(self.typed_value(measurement[2]["distance"], 0) or 0)
            if drawing_distance > 0 and real_distance > 0:
                ratios.append(real_distance / drawing_distance)
        return sorted(ratios)[len(ratios) // 2] if ratios else 1.0

    def load_velocity(self, path: Path) -> list[dict]:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        required = {"sku", "pick_frequency", "velocity_class"}
        if not rows or not required.issubset(rows[0]):
            raise ValueError(f"SKU CSV must contain: {', '.join(sorted(required))}")
        return rows

    def load_inputs(self, building_path: Path, velocity_path: Path) -> tuple[dict, list[dict]]:
        return self.rmf_maps.load_building(building_path), self.load_velocity(velocity_path)

    def rack_distances(self, building: dict) -> tuple[str, list[dict], int, int]:
        level_name, level = next(iter(building["levels"].items()))
        vertices = level.get("vertices", [])
        scale = self._distance_scale(building, level)
        pickups, dropoffs = [], {}
        for index, vertex in enumerate(vertices):
            params = vertex[4] if len(vertex) > 4 and isinstance(vertex[4], dict) else {}
            if "pickup_dispenser" in params:
                waypoint = str(vertex[3]) or f"vertex_{index}"
                pickups.append({
                    "vertex_index": index,
                    "rack_id": waypoint,
                    "waypoint": waypoint,
                    "pickup_dispenser_id": str(
                        self.typed_value(params["pickup_dispenser"], waypoint)
                    ),
                    "x": float(vertex[0]),
                    "y": float(vertex[1]),
                    "static_bay_id": f"BAY-{waypoint}",
                })
            if "dropoff_ingestor" in params:
                dropoffs[index] = str(
                    self.typed_value(params["dropoff_ingestor"], vertex[3])
                )
        if not pickups:
            raise ValueError("building YAML contains no pickup_dispenser rack points")
        if not dropoffs:
            raise ValueError("building YAML contains no dropoff_ingestor workstations")

        reverse_graph = [[] for _ in vertices]
        for lane in level.get("lanes", []):
            if len(lane) < 3:
                continue
            start, end, params = int(lane[0]), int(lane[1]), lane[2]
            if not (0 <= start < len(vertices) and 0 <= end < len(vertices)):
                continue
            weight = math.hypot(
                float(vertices[start][0]) - float(vertices[end][0]),
                float(vertices[start][1]) - float(vertices[end][1]),
            ) * scale
            reverse_graph[end].append((start, weight))
            if bool(self.typed_value(params.get("bidirectional"), False)):
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
            routes = rack_routes[rack["vertex_index"]]
            rack["workstations"] = [endpoint for endpoint, _distance in routes]
            rack["workstation_count"] = len(routes)
            rack["distance_m"] = (
                sum(distance for _endpoint, distance in routes) / len(routes)
                if len(routes) == len(dropoffs)
                else math.inf
            )
        self.apply_zone_local_aisles(building, pickups, {}, "Z01")
        pickups.sort(
            key=lambda rack: (
                math.isinf(rack["distance_m"]),
                rack["distance_m"],
                rack["rack_id"],
            )
        )
        return (
            level_name,
            pickups,
            len(dropoffs),
            sum(math.isinf(rack["distance_m"]) for rack in pickups),
        )

    @staticmethod
    def apply_zone_local_aisles(
        building: dict,
        racks: list[dict],
        zone_assignments: dict[str, str],
        default_zone: str = "Z01",
    ) -> None:
        """Assign one aisle ID to each distinct rack column in a zone."""
        x_key = (
            (lambda value: round(value))
            if building.get("coordinate_system") == "reference_image"
            else (lambda value: round(value, 9))
        )
        racks_by_zone = {}
        for rack in racks:
            zone = zone_assignments.get(rack["waypoint"], default_zone).strip() or default_zone
            rack["zone_id"] = zone
            racks_by_zone.setdefault(zone, []).append(rack)
        for zone_racks in racks_by_zone.values():
            columns = {
                value: index
                for index, value in enumerate(
                    sorted({x_key(rack["x"]) for rack in zone_racks}), start=1
                )
            }
            racks_by_aisle = {}
            for rack in zone_racks:
                rack["aisle_id"] = f"A{columns[x_key(rack['x'])]:02d}"
                racks_by_aisle.setdefault(rack["aisle_id"], []).append(rack)
            for aisle_racks in racks_by_aisle.values():
                for bay_order, rack in enumerate(
                    sorted(aisle_racks, key=lambda item: (item["y"], item["waypoint"])),
                    start=1,
                ):
                    rack["bay_order"] = bay_order

    @staticmethod
    def build_dynamic_address(
        zone_id: str,
        aisle_id: str,
        static_bay_id: str,
        level: int,
        slot: int,
        handling_unit_type: str,
        handling_unit_id: str,
    ) -> tuple[str, str]:
        if handling_unit_type == "AMR shelf":
            return (
                f"{zone_id}/{aisle_id}/BAY-{handling_unit_id}/L{level:02d}/S{slot:02d}",
                "bay",
            )
        if handling_unit_type in {"Tote", "Pallet"}:
            return (
                f"{zone_id}/{aisle_id}/{static_bay_id}/L{level:02d}/SLOT-{handling_unit_id}",
                "slot",
            )
        raise ValueError(f"unsupported handling unit type: {handling_unit_type}")

    def generate_basic(
        self,
        building: dict,
        sku_rows: list[dict],
        levels_per_rack: int = 1,
        slots_per_level: int = 6,
        handling_unit_type: str = "AMR shelf",
        zone_id: str = "Z01",
        zone_assignments: dict[str, str] | None = None,
    ) -> tuple[list[dict], dict]:
        if levels_per_rack < 1 or slots_per_level < 1:
            raise ValueError("levels and slots per level must be at least 1")
        level_name, racks, workstation_count, unreachable_count = self.rack_distances(building)
        usable_racks = [rack for rack in racks if not math.isinf(rack["distance_m"])]
        unit_prefix = {
            "AMR shelf": "SHELF",
            "Tote": "TOTE",
            "Pallet": "PALLET",
        }.get(handling_unit_type)
        if unit_prefix is None:
            raise ValueError(f"unsupported handling unit type: {handling_unit_type}")
        zone_id = zone_id.strip()
        if not zone_id:
            raise ValueError("zone ID cannot be blank")
        zone_assignments = zone_assignments or {}
        self.apply_zone_local_aisles(building, racks, zone_assignments, zone_id)

        positions = []
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
                    static_address = (
                        f"{rack_zone}/{rack['aisle_id']}/{rack['static_bay_id']}"
                        f"/L{level_number:02d}/S{slot_number:02d}"
                    )
                    dynamic_address, dynamic_address_level = self.build_dynamic_address(
                        rack_zone,
                        rack["aisle_id"],
                        rack["static_bay_id"],
                        level_number,
                        slot_number,
                        handling_unit_type,
                        handling_unit_id,
                    )
                    positions.append({
                        **rack,
                        "zone_id": rack_zone,
                        "rack_rank": rack_rank,
                        "handling_unit_id": handling_unit_id,
                        "handling_unit_type": handling_unit_type,
                        "dynamic_address_level": dynamic_address_level,
                        "level": level_number,
                        "slot": slot_number,
                        "static_address": static_address,
                        "dynamic_address": dynamic_address,
                    })

        class_rank = {"A": 0, "B": 1, "C": 2}
        sorted_skus = sorted(
            sku_rows,
            key=lambda row: (
                class_rank.get(str(row.get("velocity_class", "")).upper(), 9),
                -float(row.get("pick_frequency") or 0),
                str(row.get("sku", "")),
            ),
        )
        output = []
        empty_location_fields = (
            "static_address", "rmf_grid_address", "zone_id", "aisle_id",
            "static_bay_id", "rack_id", "rack_waypoint", "pickup_dispenser_id",
            "rack_vertex_index", "rack_rank", "handling_unit_type",
            "handling_unit_id", "dynamic_address_level", "dynamic_address",
            "storage_level", "storage_slot", "workstations_evaluated",
            "average_workstation_distance_m",
        )
        for sku_rank, sku in enumerate(sorted_skus, start=1):
            position = positions[sku_rank - 1] if sku_rank <= len(positions) else None
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
                row.update({key: "" for key in empty_location_fields})
            output.append(row)

        summary = {
            "sku_count": len(sorted_skus),
            "assigned_count": min(len(sorted_skus), len(positions)),
            "unassigned_count": max(0, len(sorted_skus) - len(positions)),
            "rack_count": len(racks),
            "usable_rack_count": len(usable_racks),
            "unreachable_rack_count": unreachable_count,
            "workstation_count": workstation_count,
            "capacity": len(positions),
            "level_name": level_name,
            "zone_count": len({position["zone_id"] for position in positions}),
        }
        return output, summary


class SlottingLayoutRepository:
    """Read and write self-contained inventory slotting layout documents."""

    def save(
        self,
        rows: list[dict],
        building: dict,
        summary: dict,
        output_path: Path,
        *,
        strategy: str,
        handling_unit_type: str,
        levels_per_rack: int,
        slots_per_level: int,
        zone_assignments: dict[str, str],
        source_building: str = "",
        source_velocity: str = "",
    ) -> None:
        payload = {
            "schema": SLOTTING_SCHEMA,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "strategy": strategy,
            "handling_unit_type": handling_unit_type,
            "rack_capacity": {
                "levels": levels_per_rack,
                "slots_per_level": slots_per_level,
            },
            "sources": {
                "building_yaml": source_building,
                "sku_velocity_csv": source_velocity,
            },
            "zone_assignments": zone_assignments,
            "summary": summary,
            "building": building,
            "assignments": rows,
            "operation_log": [],
        }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def load(self, path: Path) -> dict:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("schema") != SLOTTING_SCHEMA:
            raise ValueError("not a supported inventory slotting layout")
        if not isinstance(payload.get("building"), dict) or not isinstance(
            payload.get("assignments"), list
        ):
            raise ValueError("slotting layout is missing building or assignment data")
        return payload

    @staticmethod
    def save_payload(payload: dict, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    @staticmethod
    def save_csv(rows: list[dict], path: Path) -> None:
        if not rows:
            raise ValueError("there are no slotting rows to write")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
