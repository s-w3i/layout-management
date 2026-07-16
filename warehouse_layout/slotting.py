"""ABC slotting, route scoring, inventory addressing, and layout persistence."""

from __future__ import annotations

import csv
import copy
import heapq
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .affinity import AffinityAnalysis
from .attributes import (
    OVERSIZE_STORAGE_DEFAULTS,
    PHYSICAL_ATTRIBUTE_KEYS,
    PHYSICAL_DIMENSION_KEYS,
    PHYSICAL_WEIGHT_KEY,
    STANDARD_STORAGE_DEFAULTS,
    StorageAttributeService,
)
from .config import LEGACY_SLOTTING_SCHEMA, SLOTTING_SCHEMA
from .rmf import RmfMapService


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
                    if key in requirements and float(requirements[key]) <= 0:
                        raise ValueError(
                            f"req_{key} must be greater than zero when provided"
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

    def _candidate_sort_key(
        self, candidate: dict, profile: dict, physical_enabled: bool
    ) -> tuple:
        effective = candidate["effective_location_attributes"]
        waste = 0.0
        if physical_enabled and profile["data_status"] == "COMPLETE":
            try:
                item_dimensions = sorted(
                    float(profile["values"][key]) for key in PHYSICAL_DIMENSION_KEYS
                )
                location_dimensions = sorted(
                    float(effective[key]) for key in PHYSICAL_DIMENSION_KEYS
                )
                dimension_waste = sum(
                    max(0.0, capacity - item) / max(capacity, 1.0)
                    for item, capacity in zip(item_dimensions, location_dimensions)
                )
                weight_capacity = float(effective[PHYSICAL_WEIGHT_KEY])
                weight_waste = max(
                    0.0,
                    weight_capacity - float(profile["values"][PHYSICAL_WEIGHT_KEY]),
                ) / max(weight_capacity, 1.0)
                waste = dimension_waste + weight_waste
            except (KeyError, TypeError, ValueError):
                waste = 0.0
        return (
            waste,
            int(candidate["level"]),
            float(candidate["distance_m"]),
            int(candidate["rack_rank"]),
            int(candidate["slot"]),
        )

    @staticmethod
    def _physical_allocation_bucket(profile: dict, physical_enabled: bool) -> str:
        if not physical_enabled:
            return "STANDARD"
        return (
            "STANDARD"
            if str(profile.get("storage_class", "")).upper() == "STANDARD"
            else "EXCEPTION"
        )

    def _allocation_candidate_key(
        self,
        candidate: dict,
        profile: dict,
        physical_enabled: bool,
        velocity_class: str,
        overrides: dict,
        rack_state: dict[str, dict],
        levels_per_rack: int,
    ) -> tuple:
        """Rank a slot with ABC rack grouping ahead of physical preferences."""
        state = rack_state.get(candidate["rack_id"], {})
        rack_classes = state.get("velocity_classes", set())
        rack_physical_buckets = state.get("physical_buckets", set())
        rack_has_oversize = bool(state.get("has_oversize"))
        physical_bucket = self._physical_allocation_bucket(
            profile, physical_enabled
        )
        physical_class = str(profile.get("storage_class", "")).upper()
        is_oversize = physical_class in {
            "OVERSIZE", "OVERSIZE_AND_OVERWEIGHT", "UNVERIFIED_OVERSIZE",
        }

        # Empty racks and racks already holding this ABC class are preferred.
        # A rack containing another class is used only when class-dedicated
        # capacity has been exhausted.
        class_mix_penalty = int(
            bool(rack_classes) and velocity_class not in rack_classes
        )

        # Prefer a physically homogeneous rack, but keep this behind ABC class
        # affinity. Standard inventory is especially discouraged from entering
        # an oversize rack because an existing oversize SKU may not be on L03.
        if not rack_physical_buckets or physical_bucket in rack_physical_buckets:
            physical_mix_penalty = 0
        elif physical_bucket == "EXCEPTION":
            physical_mix_penalty = 1
        else:
            physical_mix_penalty = 2

        preferred_oversize_level = min(3, levels_per_rack)
        oversize_level_penalty = 0
        if is_oversize and "STANDARD" in rack_physical_buckets:
            oversize_level_penalty = int(
                int(candidate["level"]) != preferred_oversize_level
            )
        elif physical_bucket == "STANDARD" and rack_has_oversize:
            oversize_level_penalty = int(
                int(candidate["level"]) == preferred_oversize_level
            )

        # Once a suitable class/physical rack is open, fill it before opening
        # another rack. This prevents level-first allocation from spreading A,
        # B and C inventory over every rack.
        if rack_classes and velocity_class in rack_classes:
            rack_reuse_penalty = 0
        elif not rack_classes:
            rack_reuse_penalty = 1
        else:
            rack_reuse_penalty = 2

        return (
            # The L03 rule is a placement constraint whenever physical classes
            # must share a rack; it therefore wins over opening another ABC
            # transition rack.
            oversize_level_penalty,
            class_mix_penalty,
            bool(overrides),
            len(overrides),
            physical_mix_penalty,
            rack_reuse_penalty,
            self._candidate_sort_key(candidate, profile, physical_enabled),
        )

    @staticmethod
    def _build_affinity_neighbors(
        analysis: AffinityAnalysis,
        sku_names: set[str],
        minimum_shared_store_days: int,
        minimum_affinity_score: float,
    ) -> dict[str, list[tuple[str, float, float, int]]]:
        affinity_index = {
            sku: index
            for index, sku in enumerate(analysis.dataset.skus)
            if sku in sku_names
        }
        index_to_sku = {index: sku for sku, index in affinity_index.items()}
        neighbors: dict[str, list[tuple[str, float, float, int]]] = {}
        for sku, index in affinity_index.items():
            shared_row = analysis.shared_store_days[index]
            score_row = analysis.similarity[index]
            related_indices = np.flatnonzero(
                (shared_row >= minimum_shared_store_days)
                & (score_row >= minimum_affinity_score)
                & (score_row > 0)
            )
            neighbors[sku] = [
                (
                    index_to_sku[int(related_index)],
                    int(shared_row[related_index]) * float(score_row[related_index]),
                    float(score_row[related_index]),
                    int(shared_row[related_index]),
                )
                for related_index in related_indices
                if int(related_index) in index_to_sku
                and int(related_index) != index
            ]
        return neighbors

    @staticmethod
    def _empirical_service_cap_candidates(racks: list[dict]) -> list[float]:
        """Derive service-distance allowance candidates from the current map."""
        distances = np.array(
            sorted({
                float(rack["distance_m"])
                for rack in racks
                if math.isfinite(float(rack["distance_m"]))
            }),
            dtype=np.float64,
        )
        if len(distances) < 2:
            return [0.0]
        positive = distances[distances > 0]
        reference = float(positive.min()) if len(positive) else 1.0
        first, second = np.triu_indices(len(distances), 1)
        increases = (distances[second] - distances[first]) / np.maximum(
            distances[first], reference
        )
        increases = increases[np.isfinite(increases) & (increases >= 0)]
        if not len(increases):
            return [0.0]
        candidate_count = max(2, int(math.ceil(math.log2(len(increases)) + 1)))
        candidates = np.unique(
            np.quantile(
                increases,
                np.linspace(0.0, 1.0, candidate_count),
                method="nearest",
            )
        )
        return sorted({0.0, *(float(value) for value in candidates)})

    @staticmethod
    def _normalized_metric(values: list[float], value: float) -> float:
        finite = [item for item in values if math.isfinite(item)]
        if not finite:
            return 0.0
        low, high = min(finite), max(finite)
        return 0.0 if high <= low else (value - low) / (high - low)

    @staticmethod
    def _affinity_layout_metrics(
        rows: list[dict],
        racks: list[dict],
        analysis: AffinityAnalysis,
        minimum_shared_store_days: int,
        minimum_affinity_score: float,
        coordinate_scale: float,
    ) -> dict:
        assigned = {
            str(row.get("sku", "")): row
            for row in rows
            if row.get("assignment_status") == "ASSIGNED"
        }
        rack_by_id = {rack["rack_id"]: rack for rack in racks}
        dataset_index = {
            sku: index for index, sku in enumerate(analysis.dataset.skus)
        }
        skus = [
            sku for sku in assigned
            if sku in dataset_index
            and assigned[sku].get("rack_id") in rack_by_id
        ]
        pair_weight = 0.0
        pair_distance = 0.0
        same_rack_weight = 0.0
        retained_pairs = 0
        if len(skus) >= 2:
            indices = np.array([dataset_index[sku] for sku in skus], dtype=np.int32)
            coordinates = np.array(
                [
                    [
                        float(rack_by_id[assigned[sku]["rack_id"]]["x"]),
                        float(rack_by_id[assigned[sku]["rack_id"]]["y"]),
                    ]
                    for sku in skus
                ],
                dtype=np.float64,
            )
            rack_ids = np.array(
                [str(assigned[sku]["rack_id"]) for sku in skus]
            )
            shared = analysis.shared_store_days[np.ix_(indices, indices)]
            scores = analysis.similarity[np.ix_(indices, indices)]
            first, second = np.triu_indices(len(skus), 1)
            retained = (
                (shared[first, second] >= minimum_shared_store_days)
                & (scores[first, second] >= minimum_affinity_score)
                & (scores[first, second] > 0)
            )
            first = first[retained]
            second = second[retained]
            weights = (
                shared[first, second].astype(np.float64)
                * scores[first, second].astype(np.float64)
            )
            distances = np.linalg.norm(
                coordinates[first] - coordinates[second], axis=1
            ) * coordinate_scale
            pair_weight = float(weights.sum())
            pair_distance = float(weights @ distances)
            same_rack_weight = float(weights[rack_ids[first] == rack_ids[second]].sum())
            retained_pairs = len(weights)
        service_weight = 0.0
        service_distance = 0.0
        for row in assigned.values():
            raw_distance = row.get("average_workstation_distance_m")
            if raw_distance in (None, ""):
                continue
            weight = float(row.get("pick_frequency") or 0)
            service_weight += weight
            service_distance += weight * float(raw_distance)
        return {
            "weighted_pair_distance_m": (
                pair_distance / pair_weight if pair_weight else 0.0
            ),
            "pair_weight": pair_weight,
            "same_rack_affinity_fraction": (
                same_rack_weight / pair_weight if pair_weight else 0.0
            ),
            "retained_relationship_count": retained_pairs,
            "weighted_service_distance_m": (
                service_distance / service_weight if service_weight else 0.0
            ),
            "service_weight": service_weight,
        }

    @staticmethod
    def derive_zone_storage_types(
        rows: list[dict], zones: set[str] | None = None
    ) -> dict[str, str]:
        """Classify zones from their current assigned SKU mix, never from input roles."""
        exception_classes = {
            "OVERSIZE", "OVERWEIGHT", "OVERSIZE_AND_OVERWEIGHT",
            "UNVERIFIED_OVERSIZE",
        }
        known_zones = set(zones or ())
        known_zones.update(
            str(row.get("zone_id", ""))
            for row in rows
            if row.get("assignment_status") == "ASSIGNED" and row.get("zone_id")
        )
        zone_mix = {
            zone: {"standard": 0, "oversize": 0}
            for zone in sorted(known_zones)
        }
        for row in rows:
            if row.get("assignment_status") != "ASSIGNED":
                continue
            zone = str(row.get("zone_id", ""))
            if not zone:
                continue
            bucket = (
                "oversize"
                if row.get("physical_storage_class") in exception_classes
                else "standard"
            )
            zone_mix[zone][bucket] += 1
        zone_storage_types = {}
        for zone, counts in zone_mix.items():
            if counts["standard"] and counts["oversize"]:
                zone_storage_types[zone] = "MIXED"
            elif counts["oversize"]:
                zone_storage_types[zone] = "OVERSIZE"
            elif counts["standard"]:
                zone_storage_types[zone] = "STANDARD"
            else:
                zone_storage_types[zone] = "UNUSED"
        for row in rows:
            row["zone_storage_type"] = (
                zone_storage_types.get(str(row.get("zone_id", "")), "")
                if row.get("assignment_status") == "ASSIGNED"
                else ""
            )
        return zone_storage_types

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
    ) -> tuple[list[dict], dict]:
        if levels_per_rack < 1 or slots_per_level < 1:
            raise ValueError("levels and slots per level must be at least 1")
        if strategy not in {"basic", "abc_affinity"}:
            raise ValueError(f"unsupported slotting strategy: {strategy}")
        if not 0.0 <= affinity_weight <= 1.0:
            raise ValueError("affinity weight must be between 0 and 1")
        if minimum_shared_store_days < 0:
            raise ValueError("minimum shared store-days cannot be negative")
        if not 0.0 <= minimum_affinity_score <= 1.0:
            raise ValueError("minimum affinity score must be between 0 and 1")
        if maximum_service_distance_increase < 0:
            raise ValueError("maximum service-distance increase cannot be negative")
        affinity_enabled = strategy == "abc_affinity"
        if affinity_enabled and affinity_analysis is None:
            raise ValueError("ABC + affinity strategy requires affinity analysis")
        level_name, racks, workstation_count, unreachable_count = self.rack_distances(building)
        level = building["levels"][level_name]
        coordinate_scale = self._distance_scale(building, level)
        # Route reachability affects preference, not whether physical storage exists.
        # Unreachable racks sort last but remain usable when capacity is needed.
        usable_racks = list(racks)
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
        catalog = self.attributes.normalize_catalog(attribute_catalog)
        valid_paths = self.attributes.hierarchy_paths(
            racks, levels_per_rack, slots_per_level
        )
        local_attributes = self.attributes.validate_location_attributes(
            location_attributes, catalog, valid_paths
        )

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
                    effective_attributes = self.attributes.effective_attributes(
                        static_address, local_attributes
                    )[0]
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
                        "effective_location_attributes": effective_attributes,
                        "storage_area_type": (
                            "OVERSIZE"
                            if self.attributes.is_oversize_location(effective_attributes)
                            else "STANDARD"
                        ),
                    })

        physical_enabled = self.attributes.has_physical_catalog(catalog)

        def physical_group_rank(row):
            if not physical_enabled:
                return 0
            requirements = row.get("sku_requirements")
            if not isinstance(requirements, dict):
                requirements = self.attributes.requirements_from_row(row, catalog)
            profile = self.attributes.physical_profile(requirements)
            return int(profile["storage_class"] != "STANDARD")

        class_rank = {"A": 0, "B": 1, "C": 2}
        sorted_skus = sorted(
            sku_rows,
            key=lambda row: (
                class_rank.get(str(row.get("velocity_class", "")).upper(), 9),
                physical_group_rank(row),
                -float(row.get("pick_frequency") or 0),
                str(row.get("sku", "")),
            ),
        )
        affinity_neighbors: dict[str, list[tuple[str, float, float, int]]] = {}
        if affinity_enabled and affinity_analysis is not None:
            affinity_neighbors = precomputed_affinity_neighbors or (
                self._build_affinity_neighbors(
                    affinity_analysis,
                    {str(row.get("sku", "")) for row in sorted_skus},
                    minimum_shared_store_days,
                    minimum_affinity_score,
                )
            )
        finite_service_distances = [
            float(rack["distance_m"])
            for rack in racks
            if math.isfinite(float(rack["distance_m"]))
        ]
        max_service_distance = max(finite_service_distances, default=1.0) or 1.0
        positive_service_distances = [
            value for value in finite_service_distances if value > 0
        ]
        minimum_service_reference = (
            min(positive_service_distances) if positive_service_distances else 1.0
        )
        xs = [float(rack["x"]) for rack in racks]
        ys = [float(rack["y"]) for rack in racks]
        maximum_rack_distance = (
            math.hypot(max(xs) - min(xs), max(ys) - min(ys)) * coordinate_scale
            if xs and ys
            else 1.0
        ) or 1.0
        output = []
        empty_location_fields = (
            "static_address", "rmf_grid_address", "zone_id", "aisle_id",
            "static_bay_id", "rack_id", "rack_waypoint", "pickup_dispenser_id",
            "rack_vertex_index", "rack_rank", "handling_unit_type",
            "handling_unit_id", "dynamic_address_level", "dynamic_address",
            "storage_level", "storage_slot", "workstations_evaluated",
            "average_workstation_distance_m", "routing_status",
            "storage_area_type",
        )
        available_positions = list(positions)
        rack_state: dict[str, dict] = {}
        assigned_affinity_positions: dict[str, dict] = {}
        for sku_rank, sku in enumerate(sorted_skus, start=1):
            requirements = sku.get("sku_requirements")
            if not isinstance(requirements, dict):
                requirements = self.attributes.requirements_from_row(sku, catalog)
            else:
                requirements = self.attributes.validate_requirements(
                    requirements, catalog
                )
            profile = (
                self.attributes.physical_profile(requirements)
                if physical_enabled
                else {
                    "data_status": str(sku.get("physical_data_status", "NOT_EVALUATED")),
                    "storage_class": str(sku.get("physical_storage_class", "NOT_EVALUATED")),
                    "missing_fields": [],
                    "values": {},
                }
            )
            position = None
            compatibility_status = "NOT_EVALUATED"
            mismatch_details: list[str] = []
            auto_overrides: dict = {}
            affinity_neighbors_used: list[str] = []
            affinity_weight_sum = 0.0
            weighted_affinity_distance = 0.0
            combined_location_score = None
            if available_positions:
                hard_candidates = []
                hard_issues: list[str] = []
                for index, candidate in enumerate(available_positions):
                    issues = self.attributes.hard_compatibility_issues(
                        requirements, candidate["effective_location_attributes"]
                    )
                    if issues:
                        for issue in issues:
                            if issue not in hard_issues:
                                hard_issues.append(issue)
                        continue
                    overrides = self.attributes.required_local_overrides(
                        requirements,
                        candidate["effective_location_attributes"],
                        catalog,
                    )
                    hard_candidates.append((
                        self._allocation_candidate_key(
                            candidate,
                            profile,
                            physical_enabled,
                            str(sku.get("velocity_class", "")).upper(),
                            overrides,
                            rack_state,
                            levels_per_rack,
                        ),
                        index,
                        candidate,
                        overrides,
                    ))
                if hard_candidates:
                    baseline_candidate = min(
                        hard_candidates, key=lambda item: item[0]
                    )
                    selected_candidate = baseline_candidate
                    current_sku = str(sku.get("sku", ""))
                    related_assigned = [
                        (related_sku, weight, assigned_affinity_positions[related_sku])
                        for related_sku, weight, _score, _shared
                        in affinity_neighbors.get(current_sku, [])
                        if related_sku in assigned_affinity_positions
                    ]
                    if affinity_enabled and related_assigned:
                        baseline_key = baseline_candidate[0]
                        fixed_signature = (
                            baseline_key[:-1] + baseline_key[-1][:2]
                        )
                        baseline_service = float(
                            baseline_candidate[2]["distance_m"]
                        )
                        service_reference = max(
                            baseline_service
                            if math.isfinite(baseline_service)
                            else 0.0,
                            minimum_service_reference,
                        )
                        service_limit = (
                            baseline_service
                            + maximum_service_distance_increase * service_reference
                            if math.isfinite(baseline_service)
                            else math.inf
                        )
                        eligible = []
                        for candidate_record in hard_candidates:
                            key, _index, candidate, _overrides = candidate_record
                            signature = key[:-1] + key[-1][:2]
                            candidate_service = float(candidate["distance_m"])
                            if signature != fixed_signature:
                                continue
                            if (
                                math.isfinite(baseline_service)
                                and (
                                    not math.isfinite(candidate_service)
                                    or candidate_service > service_limit + 1e-9
                                )
                            ):
                                continue
                            eligible.append(candidate_record)
                        if eligible:
                            candidate_coordinates = np.array(
                                [
                                    [float(item[2]["x"]), float(item[2]["y"])]
                                    for item in eligible
                                ],
                                dtype=np.float64,
                            )
                            related_coordinates = np.array(
                                [
                                    [float(item[2]["x"]), float(item[2]["y"])]
                                    for item in related_assigned
                                ],
                                dtype=np.float64,
                            )
                            relationship_weights = np.array(
                                [float(item[1]) for item in related_assigned],
                                dtype=np.float64,
                            )
                            pair_distances = np.linalg.norm(
                                candidate_coordinates[:, None, :]
                                - related_coordinates[None, :, :],
                                axis=2,
                            ) * coordinate_scale
                            affinity_distances = (
                                pair_distances @ relationship_weights
                            ) / float(relationship_weights.sum())
                            scores = []
                            for candidate_number, candidate_record in enumerate(eligible):
                                service_distance = float(
                                    candidate_record[2]["distance_m"]
                                )
                                service_score = (
                                    service_distance / max_service_distance
                                    if math.isfinite(service_distance)
                                    else 1.0
                                )
                                affinity_score = (
                                    float(affinity_distances[candidate_number])
                                    / maximum_rack_distance
                                )
                                combined = (
                                    (1.0 - affinity_weight) * service_score
                                    + affinity_weight * affinity_score
                                )
                                scores.append((
                                    combined,
                                    service_distance,
                                    candidate_record[0],
                                    candidate_number,
                                    candidate_record,
                                ))
                            (
                                combined_location_score,
                                _service,
                                _candidate_key,
                                selected_number,
                                selected_candidate,
                            ) = min(scores, key=lambda item: item[:4])
                            affinity_neighbors_used = [
                                item[0] for item in related_assigned
                            ]
                            affinity_weight_sum = float(
                                relationship_weights.sum()
                            )
                            weighted_affinity_distance = float(
                                affinity_distances[selected_number]
                            )
                    _key, selected_index, position, auto_overrides = (
                        selected_candidate
                    )
                    available_positions.pop(selected_index)
                    if auto_overrides:
                        local_attributes.setdefault(
                            position["static_address"], {}
                        ).update(auto_overrides)
                        position["effective_location_attributes"] = (
                            self.attributes.effective_attributes(
                                position["static_address"], local_attributes
                            )[0]
                        )
                        position["storage_area_type"] = (
                            "OVERSIZE"
                            if self.attributes.is_oversize_location(
                                position["effective_location_attributes"]
                            )
                            else "STANDARD"
                        )
                    selected_state = rack_state.setdefault(
                        position["rack_id"],
                        {
                            "velocity_classes": set(),
                            "physical_buckets": set(),
                            "has_oversize": False,
                        },
                    )
                    selected_state["velocity_classes"].add(
                        str(sku.get("velocity_class", "")).upper()
                    )
                    selected_state["physical_buckets"].add(
                        self._physical_allocation_bucket(profile, physical_enabled)
                    )
                    selected_state["has_oversize"] = (
                        selected_state["has_oversize"]
                        or str(profile.get("storage_class", "")).upper()
                        in {
                            "OVERSIZE",
                            "OVERSIZE_AND_OVERWEIGHT",
                            "UNVERIFIED_OVERSIZE",
                        }
                    )
                    if profile["data_status"] == "MISSING":
                        compatibility_status = "UNVERIFIED"
                        mismatch_details.append(
                            "physical fit is unverified; missing "
                            + ", ".join(profile["missing_fields"])
                        )
                    elif auto_overrides:
                        compatibility_status = "COMPATIBLE_AUTO_OVERRIDE"
                    else:
                        compatibility_status = "COMPATIBLE"
                    mismatch_details.extend(
                        f"Auto slot override: {key}={value}"
                        for key, value in sorted(auto_overrides.items())
                    )
                    assigned_affinity_positions[current_sku] = position
                else:
                    mismatch_details = hard_issues
            if position:
                assignment_status = "ASSIGNED"
            elif available_positions:
                assignment_status = (
                    "UNASSIGNED_NO_CHILLED_LOCATION"
                    if requirements.get("chilled") is True
                    else "UNASSIGNED_NO_AMBIENT_LOCATION"
                    if requirements.get("chilled") is False
                    else "UNASSIGNED_NO_COMPATIBLE_LOCATION"
                )
                compatibility_status = "INCOMPATIBLE"
            else:
                assignment_status = "UNASSIGNED_NO_CAPACITY"
                compatibility_status = "NOT_EVALUATED"
            row = {
                "sku_rank": sku_rank,
                "sku": sku.get("sku", ""),
                "velocity_class": sku.get("velocity_class", ""),
                "pick_frequency": sku.get("pick_frequency", ""),
                "total_quantity_ea": sku.get("total_quantity_ea", ""),
                "active_days": sku.get("active_days", ""),
                "strategy": strategy,
                "assignment_status": assignment_status,
                "sku_requirements": requirements,
                "physical_data_status": profile["data_status"],
                "physical_storage_class": profile["storage_class"],
                "physical_missing_fields": profile["missing_fields"],
                "compatibility_status": compatibility_status,
                "compatibility_issues": mismatch_details,
                "auto_attribute_overrides": auto_overrides,
                "affinity_neighbors_used": affinity_neighbors_used,
                "affinity_weight_sum": round(affinity_weight_sum, 6),
                "weighted_affinity_distance_m": round(
                    weighted_affinity_distance, 6
                ),
                "combined_location_score": (
                    round(float(combined_location_score), 9)
                    if combined_location_score is not None
                    else ""
                ),
                "affinity_weight": affinity_weight if affinity_enabled else 0.0,
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
                    "storage_area_type": position["storage_area_type"],
                    "effective_location_attributes": position[
                        "effective_location_attributes"
                    ],
                    "workstations_evaluated": "|".join(position["workstations"]),
                    "average_workstation_distance_m": (
                        round(position["distance_m"], 3)
                        if math.isfinite(position["distance_m"])
                        else ""
                    ),
                    "routing_status": (
                        "REACHABLE"
                        if math.isfinite(position["distance_m"])
                        else "UNREACHABLE_LAST_RESORT"
                    ),
                })
            else:
                row.update({key: "" for key in empty_location_fields})
                row["effective_location_attributes"] = {}
            output.append(row)

        if location_attributes is not None:
            location_attributes.clear()
            location_attributes.update(local_attributes)

        zone_storage_types = self.derive_zone_storage_types(
            output, {position["zone_id"] for position in positions}
        )

        status_counts = {
            status: sum(row["assignment_status"] == status for row in output)
            for status in {
                "UNASSIGNED_NO_CAPACITY",
                "UNASSIGNED_NO_CHILLED_LOCATION",
                "UNASSIGNED_NO_AMBIENT_LOCATION",
                "UNASSIGNED_NO_COMPATIBLE_LOCATION",
            }
        }
        summary = {
            "strategy": strategy,
            "sku_count": len(sorted_skus),
            "assigned_count": sum(
                row["assignment_status"] == "ASSIGNED" for row in output
            ),
            "unassigned_count": sum(
                row["assignment_status"] != "ASSIGNED" for row in output
            ),
            "unassigned_no_capacity_count": status_counts["UNASSIGNED_NO_CAPACITY"],
            "unassigned_no_compatible_location_count": status_counts[
                "UNASSIGNED_NO_COMPATIBLE_LOCATION"
            ],
            "unassigned_status_counts": status_counts,
            "unverified_oversize_count": sum(
                row["physical_storage_class"] == "UNVERIFIED_OVERSIZE"
                for row in output
            ),
            "assigned_unverified_count": sum(
                row["assignment_status"] == "ASSIGNED"
                and row["compatibility_status"] == "UNVERIFIED"
                for row in output
            ),
            "auto_overridden_slot_count": sum(
                bool(row["auto_attribute_overrides"]) for row in output
            ),
            "rack_count": len(racks),
            "usable_rack_count": len(usable_racks),
            "unreachable_rack_count": unreachable_count,
            "workstation_count": workstation_count,
            "capacity": len(positions),
            "level_name": level_name,
            "zone_count": len({position["zone_id"] for position in positions}),
            "zone_storage_types": zone_storage_types,
        }
        if affinity_analysis is not None:
            affinity_metrics = self._affinity_layout_metrics(
                output,
                racks,
                affinity_analysis,
                minimum_shared_store_days,
                minimum_affinity_score,
                coordinate_scale,
            )
            summary["affinity_metrics"] = affinity_metrics
            summary["affinity_configuration"] = {
                "affinity_weight": affinity_weight if affinity_enabled else 0.0,
                "minimum_shared_store_days": minimum_shared_store_days,
                "minimum_affinity_score": minimum_affinity_score,
                "maximum_service_distance_increase": (
                    maximum_service_distance_increase if affinity_enabled else 0.0
                ),
            }
        return output, summary

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
    ) -> tuple[list[dict], dict]:
        """Generate ABC-first affinity slotting with empirical auto-tuning."""
        if not 0.0 <= affinity_weight <= 1.0:
            raise ValueError("affinity weight must be between 0 and 1")
        known_skus = {str(row.get("sku", "")) for row in sku_rows}
        automatic = tuning_parameters is None
        threshold_recommendation = affinity_analysis.suggest_slotting_thresholds(
            known_skus, affinity_weight
        )
        if automatic:
            minimum_shared_store_days = int(
                threshold_recommendation["minimum_shared_store_days"]
            )
            minimum_affinity_score = float(
                threshold_recommendation["minimum_affinity_score"]
            )
        else:
            try:
                minimum_shared_store_days = int(
                    tuning_parameters["minimum_shared_store_days"]
                )
                minimum_affinity_score = float(
                    tuning_parameters["minimum_affinity_score"]
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    "adjusted tuning requires minimum shared store-days and "
                    "minimum affinity score"
                ) from exc

        affinity_neighbors = self._build_affinity_neighbors(
            affinity_analysis,
            known_skus,
            minimum_shared_store_days,
            minimum_affinity_score,
        )

        baseline_rows, baseline_summary = self.generate_basic(
            building,
            copy.deepcopy(sku_rows),
            levels_per_rack,
            slots_per_level,
            handling_unit_type,
            zone_id,
            zone_assignments,
            attribute_catalog,
            copy.deepcopy(location_attributes or {}),
            strategy="basic",
            affinity_analysis=affinity_analysis,
            minimum_shared_store_days=minimum_shared_store_days,
            minimum_affinity_score=minimum_affinity_score,
        )
        _level, racks, _workstations, _unreachable = self.rack_distances(building)
        self.apply_zone_local_aisles(
            building, racks, zone_assignments or {}, zone_id
        )
        if automatic:
            empirical_service_caps = self._empirical_service_cap_candidates(racks)
            relationship_confidence = math.sqrt(
                float(threshold_recommendation["retained_weight_fraction"])
                * float(threshold_recommendation["sku_coverage_fraction"])
            )
            affinity_pressure = affinity_weight * relationship_confidence
            service_cap_index = int(
                round(affinity_pressure * (len(empirical_service_caps) - 1))
            )
            suggested_service_cap = float(
                empirical_service_caps[service_cap_index]
            )
            cap_candidates = [suggested_service_cap]
            service_cap_recommendation = {
                "method": "affinity_weighted_empirical_map_distance_quantile",
                "relationship_confidence": relationship_confidence,
                "affinity_pressure": affinity_pressure,
                "empirical_candidate_count": len(empirical_service_caps),
                "selected_candidate_index": service_cap_index,
                "selected_service_distance_increase": suggested_service_cap,
                "empirical_candidates": empirical_service_caps,
            }
        else:
            try:
                cap_candidates = [
                    float(tuning_parameters["maximum_service_distance_increase"])
                ]
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    "adjusted tuning requires maximum service-distance increase"
                ) from exc
            empirical_service_caps = cap_candidates
            service_cap_recommendation = {
                "method": "user_adjusted",
                "selected_service_distance_increase": cap_candidates[0],
            }
        if any(value < 0 for value in cap_candidates):
            raise ValueError("maximum service-distance increase cannot be negative")

        trials = []
        for service_cap in cap_candidates:
            trial_location_attributes = copy.deepcopy(location_attributes or {})
            trial_rows, trial_summary = self.generate_basic(
                building,
                copy.deepcopy(sku_rows),
                levels_per_rack,
                slots_per_level,
                handling_unit_type,
                zone_id,
                zone_assignments,
                attribute_catalog,
                trial_location_attributes,
                strategy="abc_affinity",
                affinity_analysis=affinity_analysis,
                affinity_weight=affinity_weight,
                minimum_shared_store_days=minimum_shared_store_days,
                minimum_affinity_score=minimum_affinity_score,
                maximum_service_distance_increase=service_cap,
                precomputed_affinity_neighbors=affinity_neighbors,
            )
            if trial_summary["unassigned_count"] > baseline_summary["unassigned_count"]:
                continue
            metrics = trial_summary["affinity_metrics"]
            trials.append({
                "maximum_service_distance_increase": service_cap,
                "weighted_pair_distance_m": float(
                    metrics["weighted_pair_distance_m"]
                ),
                "weighted_service_distance_m": float(
                    metrics["weighted_service_distance_m"]
                ),
                "same_rack_affinity_fraction": float(
                    metrics["same_rack_affinity_fraction"]
                ),
                "summary": trial_summary,
                "rows": trial_rows,
                "location_attributes": trial_location_attributes,
            })
        if not trials:
            raise ValueError(
                "no affinity parameter candidate preserved the ABC baseline assignment count"
            )
        pair_values = [row["weighted_pair_distance_m"] for row in trials]
        service_values = [row["weighted_service_distance_m"] for row in trials]
        for trial in trials:
            pair_normalized = self._normalized_metric(
                pair_values, trial["weighted_pair_distance_m"]
            )
            service_normalized = self._normalized_metric(
                service_values, trial["weighted_service_distance_m"]
            )
            trial["selection_loss"] = math.sqrt(
                affinity_weight * pair_normalized**2
                + (1.0 - affinity_weight) * service_normalized**2
            )
        selected_trial = min(
            trials,
            key=lambda row: (
                row["selection_loss"],
                row["weighted_service_distance_m"],
                row["weighted_pair_distance_m"],
                row["maximum_service_distance_increase"],
            ),
        )
        selected_cap = float(
            selected_trial["maximum_service_distance_increase"]
        )
        final_rows = selected_trial["rows"]
        final_summary = selected_trial["summary"]
        if location_attributes is not None:
            location_attributes.clear()
            location_attributes.update(selected_trial["location_attributes"])
        baseline_metrics = baseline_summary["affinity_metrics"]
        final_metrics = final_summary["affinity_metrics"]

        def relative_change(current, baseline):
            return (
                (float(current) - float(baseline)) / float(baseline)
                if baseline
                else 0.0
            )

        final_summary["affinity_tuning"] = {
            "parameter_status": "AUTO_SUGGESTED" if automatic else "USER_ADJUSTED",
            "method": "data_driven_relationship_pareto_and_map_quantile",
            "affinity_weight": affinity_weight,
            "minimum_shared_store_days": minimum_shared_store_days,
            "minimum_affinity_score": minimum_affinity_score,
            "maximum_service_distance_increase": selected_cap,
            "relationship_recommendation": threshold_recommendation,
            "service_cap_recommendation": service_cap_recommendation,
            "service_cap_candidate_count": len(empirical_service_caps),
            "service_cap_evaluated_count": len(cap_candidates),
            "valid_layout_candidate_count": len(trials),
            "layout_candidates": [
                {
                    key: value for key, value in trial.items()
                    if key not in {"rows", "summary", "location_attributes"}
                }
                for trial in trials
            ],
            "source_store_day_count": affinity_analysis.store_day_count,
            "source_line_order_count": affinity_analysis.event_count,
        }
        final_summary["baseline_comparison"] = {
            "basic": baseline_metrics,
            "abc_affinity": final_metrics,
            "weighted_pair_distance_change_fraction": relative_change(
                final_metrics["weighted_pair_distance_m"],
                baseline_metrics["weighted_pair_distance_m"],
            ),
            "weighted_service_distance_change_fraction": relative_change(
                final_metrics["weighted_service_distance_m"],
                baseline_metrics["weighted_service_distance_m"],
            ),
            "assigned_count_change": (
                final_summary["assigned_count"] - baseline_summary["assigned_count"]
            ),
        }
        return final_rows, final_summary


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
        attribute_catalog=None,
        location_attributes: dict[str, dict] | None = None,
        source_building: str = "",
        source_velocity: str = "",
        source_chilled: str = "",
        source_affinity: str = "",
        affinity_configuration: dict | None = None,
        standard_storage_defaults: dict | None = None,
        oversize_storage_defaults: dict | None = None,
        chilled_demo_rate: float = 0.10,
        chilled_demo_seed: int = 42,
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
                "chilled_requirements_csv": source_chilled,
                "affinity_order_workbook": source_affinity,
            },
            "affinity_configuration": affinity_configuration or {},
            "storage_defaults": {
                "standard": standard_storage_defaults or STANDARD_STORAGE_DEFAULTS,
                "oversize": oversize_storage_defaults or OVERSIZE_STORAGE_DEFAULTS,
            },
            "chilled_requirements": {
                "missing_sku_is_ambient": True,
                "demo_rate": chilled_demo_rate,
                "demo_seed": chilled_demo_seed,
            },
            "zone_assignments": zone_assignments,
            "attribute_catalog": StorageAttributeService.serialize_catalog(
                attribute_catalog
            ),
            "location_attributes": location_attributes or {},
            "summary": summary,
            "building": building,
            "assignments": rows,
            "operation_log": [],
        }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def load(self, path: Path) -> dict:
        payload = json.loads(path.read_text(encoding="utf-8"))
        schema = payload.get("schema")
        if schema not in {SLOTTING_SCHEMA, LEGACY_SLOTTING_SCHEMA}:
            raise ValueError("not a supported inventory slotting layout")
        if not isinstance(payload.get("building"), dict) or not isinstance(
            payload.get("assignments"), list
        ):
            raise ValueError("slotting layout is missing building or assignment data")
        if schema == LEGACY_SLOTTING_SCHEMA:
            payload["source_schema"] = LEGACY_SLOTTING_SCHEMA
            payload["schema"] = SLOTTING_SCHEMA
            payload.setdefault("attribute_catalog", [])
            payload.setdefault("location_attributes", {})
        payload.setdefault("sources", {})
        payload["sources"].setdefault("chilled_requirements_csv", "")
        payload["sources"].setdefault("affinity_order_workbook", "")
        payload.setdefault("affinity_configuration", {})
        payload.setdefault("storage_defaults", {
            "standard": STANDARD_STORAGE_DEFAULTS,
            "oversize": {},
        })
        payload.setdefault("chilled_requirements", {
            "missing_sku_is_ambient": True,
            "demo_rate": 0.10,
            "demo_seed": 42,
        })
        attributes = StorageAttributeService()
        catalog = attributes.normalize_catalog(payload.get("attribute_catalog"))
        payload["attribute_catalog"] = attributes.serialize_catalog(catalog)
        payload["location_attributes"] = attributes.validate_location_attributes(
            payload.get("location_attributes"), catalog
        )
        for row in payload["assignments"]:
            requirements = row.setdefault("sku_requirements", {})
            if not isinstance(requirements, dict):
                raise ValueError("assignment sku_requirements must be an object")
            row["sku_requirements"] = attributes.validate_requirements(
                requirements, catalog
            )
            row.setdefault(
                "compatibility_status",
                "COMPATIBLE"
                if row.get("assignment_status") == "ASSIGNED"
                else "NOT_EVALUATED",
            )
            row.setdefault("compatibility_issues", [])
            row.setdefault("auto_attribute_overrides", {})
            row.setdefault("routing_status", "NOT_EVALUATED")
            if self._physical_requirements_present(row["sku_requirements"]):
                profile = attributes.physical_profile(row["sku_requirements"])
                row.setdefault("physical_data_status", profile["data_status"])
                row.setdefault("physical_storage_class", profile["storage_class"])
                row.setdefault("physical_missing_fields", profile["missing_fields"])
            else:
                row.setdefault("physical_data_status", "NOT_EVALUATED")
                row.setdefault("physical_storage_class", "NOT_EVALUATED")
                row.setdefault("physical_missing_fields", [])
            row.setdefault("effective_location_attributes", {})
        zones = set(
            payload.get("summary", {}).get("zone_storage_types", {}).keys()
        )
        payload.setdefault("summary", {})["zone_storage_types"] = (
            SlottingService.derive_zone_storage_types(payload["assignments"], zones)
        )
        return payload

    @staticmethod
    def _physical_requirements_present(requirements: dict) -> bool:
        return any(key in requirements for key in PHYSICAL_ATTRIBUTE_KEYS)

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
