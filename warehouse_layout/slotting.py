"""Stable facade for slotting inputs, routing, and strategy dispatch."""

from __future__ import annotations

import csv
import heapq
import math
import re
from pathlib import Path

from .affinity import AffinityAnalysis
from .attributes import (
    PHYSICAL_ATTRIBUTE_KEYS,
    PHYSICAL_WEIGHT_KEY,
    StorageAttributeService,
)
from .rmf import RmfMapService
from .slotting_strategies import create_strategy
from .slotting_strategies.allocation import allocate
from .slotting_strategies.affinity_support import (
    affinity_layout_metrics,
    affinity_physical_signature,
    affinity_placement_order,
    build_affinity_neighbors,
    empirical_service_cap_candidates,
    normalized_metric,
)
from .slotting_repository import SlottingLayoutRepository as SlottingLayoutRepository
from .slotting_rules import (
    allocation_candidate_key,
    apply_rack_frequency_ranks,
    candidate_sort_key,
    physical_allocation_bucket,
    rack_frequency_class,
    required_horizontal_slot_span,
    required_slot_footprint,
)
from .storage_planning import (
    build_dynamic_address,
    derive_zone_storage_types,
    plan_storage_zones,
)


class SlottingService:
    """Generate warehouse slotting recommendations from RMF and ABC inputs."""

    def __init__(
        self,
        rmf_maps: RmfMapService | None = None,
        attributes: StorageAttributeService | None = None,
    ):
        self.rmf_maps = rmf_maps or RmfMapService()
        self.attributes = attributes or StorageAttributeService()

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

    required_slot_footprint = staticmethod(required_slot_footprint)
    required_horizontal_slot_span = staticmethod(required_horizontal_slot_span)

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

    def load_chilled_requirements(
        self, path: Path, known_skus: set[str]
    ) -> dict[str, bool]:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        required = {"sku", "chilled_required"}
        if not rows or not required.issubset(rows[0]):
            raise ValueError(
                "chilled SKU CSV must contain: sku, chilled_required"
            )
        chilled_definition = self.attributes.starter_catalog()["chilled"]
        values: dict[str, bool] = {}
        for row_number, row in enumerate(rows, start=2):
            sku = str(row.get("sku", "")).strip()
            if not sku:
                raise ValueError(f"chilled SKU CSV row {row_number}: SKU is blank")
            if sku in values:
                raise ValueError(f"chilled SKU CSV contains duplicate SKU: {sku}")
            if sku not in known_skus:
                raise ValueError(f"chilled SKU CSV references unknown SKU: {sku}")
            try:
                values[sku] = self.attributes.parse_value(
                    chilled_definition, row.get("chilled_required")
                )
            except ValueError as exc:
                raise ValueError(f"chilled SKU CSV row {row_number}: {exc}") from exc
        return values

    def load_velocity(
        self,
        path: Path,
        attribute_catalog=None,
        chilled_path: Path | None = None,
    ) -> list[dict]:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        required = {"sku", "pick_frequency", "velocity_class"}
        if not rows or not required.issubset(rows[0]):
            raise ValueError(f"SKU CSV must contain: {', '.join(sorted(required))}")
        catalog = self.attributes.normalize_catalog(attribute_catalog)
        unknown_columns = sorted(
            column
            for column in rows[0]
            if column.startswith("req_") and column[4:] not in catalog
        )
        if unknown_columns:
            raise ValueError(
                "unknown SKU requirement column(s): " + ", ".join(unknown_columns)
            )
        sku_ids = [str(row.get("sku", "")).strip() for row in rows]
        if any(not sku for sku in sku_ids):
            raise ValueError("SKU CSV contains a blank SKU")
        if len(set(sku_ids)) != len(sku_ids):
            raise ValueError("SKU CSV contains duplicate SKU values")
        chilled_values = (
            self.load_chilled_requirements(chilled_path, set(sku_ids))
            if chilled_path is not None
            else None
        )
        for row_number, row in enumerate(rows, start=2):
            try:
                requirements = self.attributes.requirements_from_row(row, catalog)
                for key in PHYSICAL_ATTRIBUTE_KEYS:
                    minimum = 0 if key == PHYSICAL_WEIGHT_KEY else 1e-300
                    if key in requirements and float(requirements[key]) < minimum:
                        raise ValueError(
                            f"req_{key} must be "
                            + (
                                "zero or greater"
                                if key == PHYSICAL_WEIGHT_KEY
                                else "greater than zero"
                            )
                            + " when provided"
                        )
                if "chilled" in catalog:
                    raw_chilled = row.get("req_chilled")
                    velocity_has_chilled = (
                        raw_chilled is not None and str(raw_chilled).strip() != ""
                    )
                    file_value = (
                        chilled_values.get(sku_ids[row_number - 2], False)
                        if chilled_values is not None
                        else None
                    )
                    if (
                        file_value is not None
                        and velocity_has_chilled
                        and requirements.get("chilled") != file_value
                    ):
                        raise ValueError(
                            "conflicting chilled requirement between velocity and "
                            "chilled CSV"
                        )
                    if file_value is not None:
                        requirements["chilled"] = file_value
                    elif not velocity_has_chilled:
                        requirements["chilled"] = False
                row["sku_requirements"] = requirements
                if self.attributes.has_physical_catalog(catalog):
                    profile = self.attributes.physical_profile(requirements)
                    row["physical_data_status"] = profile["data_status"]
                    row["physical_storage_class"] = profile["storage_class"]
            except ValueError as exc:
                raise ValueError(f"SKU CSV row {row_number}: {exc}") from exc
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

    _candidate_sort_key = staticmethod(candidate_sort_key)
    _rack_frequency_class = staticmethod(rack_frequency_class)
    _apply_rack_frequency_ranks = staticmethod(apply_rack_frequency_ranks)
    _physical_allocation_bucket = staticmethod(physical_allocation_bucket)
    _allocation_candidate_key = staticmethod(allocation_candidate_key)

    _build_affinity_neighbors = staticmethod(build_affinity_neighbors)
    _affinity_placement_order = staticmethod(affinity_placement_order)
    _affinity_physical_signature = staticmethod(affinity_physical_signature)
    _empirical_service_cap_candidates = staticmethod(empirical_service_cap_candidates)
    _normalized_metric = staticmethod(normalized_metric)
    _affinity_layout_metrics = staticmethod(affinity_layout_metrics)

    _plan_storage_zones = staticmethod(plan_storage_zones)
    derive_zone_storage_types = staticmethod(derive_zone_storage_types)
    build_dynamic_address = staticmethod(build_dynamic_address)

    def generate_basic(
        self,
        building: dict,
        sku_rows: list[dict],
        levels_per_rack: int = 1,
        slots_per_level: int = 6,
        handling_unit_type: str = "AMR shelf",
        zone_id: str = "Z01",
        zone_assignments: dict[str, str] | None = None,
        attribute_catalog=None,
        location_attributes: dict[str, dict] | None = None,
        strategy: str = "basic",
        affinity_analysis: AffinityAnalysis | None = None,
        affinity_weight: float = 0.0,
        minimum_shared_store_days: int = 0,
        minimum_affinity_score: float = 0.0,
        maximum_service_distance_increase: float = 0.0,
        precomputed_affinity_neighbors: (
            dict[str, list[tuple[str, float, float, int]]] | None
        ) = None,
        strict_compatibility: bool = False,
        storage_layout=None,
        ergonomic_weight_heuristic: bool = True,
        auto_plan_oversize: bool = True,
    ) -> tuple[list[dict], dict]:
        parameters = locals()
        service = parameters.pop("self")
        strategy_name = parameters.pop("strategy")
        if strategy_name == "basic":
            return create_strategy("basic").generate(service, **parameters)
        parameters["strategy"] = strategy_name
        return allocate(service, **parameters)

    def generate_abc_affinity(
        self,
        building: dict,
        sku_rows: list[dict],
        affinity_analysis: AffinityAnalysis,
        affinity_weight: float,
        levels_per_rack: int = 1,
        slots_per_level: int = 6,
        handling_unit_type: str = "AMR shelf",
        zone_id: str = "Z01",
        zone_assignments: dict[str, str] | None = None,
        attribute_catalog=None,
        location_attributes: dict[str, dict] | None = None,
        tuning_parameters: dict | None = None,
        strict_compatibility: bool = False,
        storage_layout=None,
        ergonomic_weight_heuristic: bool = True,
        auto_plan_oversize: bool = True,
    ) -> tuple[list[dict], dict]:
        parameters = locals()
        service = parameters.pop("self")
        return create_strategy("abc_affinity").generate(service, **parameters)
