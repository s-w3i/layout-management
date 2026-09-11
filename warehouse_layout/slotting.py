"""Stable facade for slotting inputs, routing, and strategy dispatch."""

from __future__ import annotations

import csv
from collections import Counter
from dataclasses import replace
import heapq
import math
import re
from pathlib import Path

from .affinity import AffinityAnalysis
from .attributes import (
    AttributeDefinition,
    DERIVED_OVERSIZE_KEY,
    OVERSIZE_CAPABLE_KEY,
    PHYSICAL_ATTRIBUTE_KEYS,
    PHYSICAL_WEIGHT_KEY,
    StorageAttributeService,
    requires_oversize_capable,
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
    handling_unit_visit_metrics,
    normalized_metric,
)
from .slotting_repository import SlottingLayoutRepository as SlottingLayoutRepository
from .slotting_rules import (
    allocation_candidate_key,
    apply_rack_frequency_ranks,
    candidate_sort_key,
    physical_allocation_bucket,
    planned_storage_type,
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

    SKU_ATTRIBUTE_COLUMN_ALIASES = {
        "length": "max_item_length",
        "width": "max_item_width",
        "height": "max_item_height",
        "weight": "max_item_weight",
        "chilled_required": "chilled",
    }

    @classmethod
    def sku_attribute_key(cls, column: str) -> str:
        key = str(column).strip().lower()
        if key.startswith("req_"):
            key = key[4:]
        return cls.SKU_ATTRIBUTE_COLUMN_ALIASES.get(key, key)

    @staticmethod
    def infer_attribute_definition(
        key: str, raw_values: list[str], hierarchy_level: int | None = None
    ) -> AttributeDefinition:
        boolean_tokens = {"true", "false", "yes", "no", "y", "n", "1", "0"}
        lowered = {value.lower() for value in raw_values}
        label = key.replace("_", " ").title()
        if key in PHYSICAL_ATTRIBUTE_KEYS:
            definition = AttributeDefinition(
                key, label, "number", "capacity",
                (
                    "kg" if key == PHYSICAL_WEIGHT_KEY else "m"
                ),
            )
        elif raw_values and lowered.issubset(boolean_tokens):
            definition = AttributeDefinition(
                key, label, "boolean", "exact",
                hierarchy_level=hierarchy_level,
            )
        else:
            if not raw_values:
                raise ValueError(
                    f"cannot infer attribute '{key}' because its column is empty"
                )
            raise ValueError(
                f"attribute '{key}' must contain Boolean true/false values; "
                "only length, width, height, and weight may be numeric"
            )
        definition.validate()
        return definition

    def inspect_sku_attribute_csv(
        self,
        path: Path,
        attribute_catalog=None,
        combination_attribute_keys: list[str] | tuple[str, ...] | None = None,
    ) -> tuple[dict[str, AttributeDefinition], dict]:
        """Infer custom requirement definitions and summarize their values."""
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        if not rows or "sku" not in rows[0]:
            raise ValueError("SKU attributes CSV must contain a sku column")
        catalog = self.attributes.normalize_catalog(attribute_catalog)
        columns: dict[str, str] = {}
        for column in rows[0]:
            if not column or str(column).strip().lower() == "sku":
                continue
            key = self.sku_attribute_key(column)
            if key in columns.values():
                raise ValueError(
                    f"SKU attributes CSV maps more than one column to '{key}'"
                )
            columns[str(column)] = key
        if not columns:
            raise ValueError("SKU attributes CSV contains no attribute columns")

        hierarchy_level = 0
        for column, key in columns.items():
            level = None
            if key not in PHYSICAL_ATTRIBUTE_KEYS and key != OVERSIZE_CAPABLE_KEY:
                hierarchy_level += 1
                level = min(hierarchy_level, 2)
            if key in catalog:
                if catalog[key].hierarchy_level is None and level is not None:
                    catalog[key] = replace(
                        catalog[key], hierarchy_level=level
                    )
                continue
            raw_values = [
                str(row.get(column, "")).strip()
                for row in rows
                if str(row.get(column, "")).strip()
            ]
            catalog[key] = self.infer_attribute_definition(
                key, raw_values, level
            )

        sku_ids = [str(row.get("sku", "")).strip() for row in rows]
        if any(not sku for sku in sku_ids):
            raise ValueError("SKU attributes CSV contains a blank SKU")
        if len(set(sku_ids)) != len(sku_ids):
            raise ValueError("SKU attributes CSV contains duplicate SKU values")
        summary_attributes = {}
        parsed_rows: list[dict] = [dict() for _row in rows]
        for column, key in columns.items():
            definition = catalog[key]
            parsed_values = []
            for row_number, row in enumerate(rows, start=2):
                raw = row.get(column)
                if raw is None or str(raw).strip() == "":
                    continue
                try:
                    parsed = self.attributes.parse_value(definition, raw)
                    parsed_values.append(parsed)
                    parsed_rows[row_number - 2][key] = parsed
                except ValueError as exc:
                    raise ValueError(
                        f"SKU attributes CSV row {row_number}: {exc}"
                    ) from exc
            unique_values = sorted({str(value) for value in parsed_values})
            summary_attributes[key] = {
                "label": definition.label,
                "value_type": definition.value_type,
                "match_rule": definition.match_rule,
                "hierarchy_level": definition.hierarchy_level,
                "values": unique_values[:20],
                "distinct_count": len(unique_values),
                "populated_count": len(parsed_values),
            }
            if definition.value_type == "number" and parsed_values:
                numeric = [float(value) for value in parsed_values]
                summary_attributes[key]["minimum"] = min(numeric)
                summary_attributes[key]["maximum"] = max(numeric)

        physical_enabled = self.attributes.has_physical_catalog(catalog)
        available_boolean_keys = sorted(
            (
                key for key in columns.values()
                if key not in {OVERSIZE_CAPABLE_KEY, DERIVED_OVERSIZE_KEY}
                and catalog[key].value_type == "boolean"
            ),
            key=lambda key: (
                catalog[key].hierarchy_level is None,
                catalog[key].hierarchy_level or 10**9,
                list(columns.values()).index(key),
            ),
        )
        if physical_enabled:
            available_boolean_keys.append(DERIVED_OVERSIZE_KEY)
        if combination_attribute_keys is None:
            boolean_keys = available_boolean_keys
        else:
            requested = list(dict.fromkeys(combination_attribute_keys))
            unavailable = sorted(set(requested) - set(available_boolean_keys))
            if unavailable:
                raise ValueError(
                    "overlay grouping attributes are unavailable or not Boolean: "
                    + ", ".join(unavailable)
                )
            requested_set = set(requested)
            boolean_keys = [
                key for key in available_boolean_keys if key in requested_set
            ]
        combinations = Counter()
        for requirements in parsed_rows:
            profile = self.attributes.physical_profile(requirements)
            storage_type = planned_storage_type(profile, physical_enabled)
            signature = tuple(
                requires_oversize_capable(profile)
                if key == DERIVED_OVERSIZE_KEY
                else requirements.get(key)
                for key in boolean_keys
            )
            combinations[(storage_type, signature)] += 1
        if physical_enabled:
            derived_values = [
                requires_oversize_capable(
                    self.attributes.physical_profile(requirements)
                )
                for requirements in parsed_rows
            ]
            summary_attributes[DERIVED_OVERSIZE_KEY] = {
                "label": "Oversize",
                "value_type": "boolean",
                "match_rule": "exact",
                "hierarchy_level": None,
                "values": [str(value) for value in sorted(set(derived_values))],
                "distinct_count": len(set(derived_values)),
                "populated_count": len(derived_values),
                "derived": True,
            }
        combination_summary = []
        for (storage_type, signature), count in sorted(
            combinations.items(),
            key=lambda item: (item[0][0], tuple(str(value) for value in item[0][1])),
        ):
            combination_summary.append({
                "storage_type": storage_type,
                "attributes": dict(zip(boolean_keys, signature)),
                "sku_count": count,
            })
        return catalog, {
            "sku_count": len(rows),
            "attributes": summary_attributes,
            "available_combination_attributes": available_boolean_keys,
            "combination_attributes": boolean_keys,
            "attribute_combinations": combination_summary,
        }

    def load_sku_attribute_requirements(
        self,
        path: Path,
        known_skus: set[str],
        attribute_catalog=None,
        *,
        include_derived_grouping: bool = False,
    ) -> dict[str, dict]:
        catalog, _summary = self.inspect_sku_attribute_csv(
            path, attribute_catalog
        )
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        columns = {
            column: self.sku_attribute_key(column)
            for column in rows[0]
            if column and str(column).strip().lower() != "sku"
        }
        values: dict[str, dict] = {}
        for row_number, row in enumerate(rows, start=2):
            sku = str(row.get("sku", "")).strip()
            if sku not in known_skus:
                raise ValueError(f"SKU attributes CSV references unknown SKU: {sku}")
            requirements = {}
            for column, key in columns.items():
                raw = row.get(column)
                if raw is None or str(raw).strip() == "":
                    continue
                try:
                    requirements[key] = self.attributes.parse_value(
                        catalog[key], raw
                    )
                except ValueError as exc:
                    raise ValueError(
                        f"SKU attributes CSV row {row_number}: {exc}"
                    ) from exc
            if (
                include_derived_grouping
                and self.attributes.has_physical_catalog(catalog)
            ):
                requirements[DERIVED_OVERSIZE_KEY] = requires_oversize_capable(
                    self.attributes.physical_profile(requirements)
                )
            values[sku] = requirements
        return values

    def load_chilled_requirements(
        self, path: Path, known_skus: set[str]
    ) -> dict[str, bool]:
        """Backward-compatible reader for the former chilled-only CSV."""
        values = self.load_sku_attribute_requirements(
            path, known_skus, None
        )
        return {
            sku: requirements["chilled"]
            for sku, requirements in values.items()
            if "chilled" in requirements
        }

    @staticmethod
    def apply_stock_requirements(
        sku_rows: list[dict], stock_rows: list[dict], *, require_complete: bool = False,
    ) -> list[dict]:
        """Attach calculated stock targets to velocity rows by SKU."""
        stock_by_sku: dict[str, dict] = {}
        for row in stock_rows:
            sku = str(row.get("sku", "")).strip()
            if not sku:
                raise ValueError("stock requirements contain a blank SKU")
            if sku in stock_by_sku:
                raise ValueError(f"stock requirements contain duplicate SKU: {sku}")
            stock_by_sku[sku] = row
        result = []
        for source in sku_rows:
            row = dict(source)
            stock = stock_by_sku.get(str(row.get("sku", "")).strip())
            if stock is not None:
                for key in (
                    "total_required_ea",
                    "units_per_slot",
                    "slots_per_unit",
                    "required_slots",
                    "required_racks",
                    "rack_calculation_status",
                ):
                    if key in stock:
                        row[key] = stock[key]
            result.append(row)
        if require_complete:
            invalid = []
            for row in result:
                try:
                    quantity = float(row["total_required_ea"])
                    slots = float(row["required_slots"])
                    valid = (
                        math.isfinite(quantity) and math.isfinite(slots)
                        and quantity >= 0 and slots >= 0
                        and (quantity == 0) == (slots == 0)
                    )
                except (KeyError, TypeError, ValueError):
                    valid = False
                if not valid:
                    invalid.append(str(row.get("sku", "")))
            if invalid:
                raise ValueError(
                    f"Minimum stock quantities or required slots are missing or "
                    f"unresolved for {len(invalid):,} SKU(s): {', '.join(invalid[:5])}. "
                    "Calculate Stock Requirements for the selected SKUs before "
                    "generating a layout. Slotting cannot assume one slot per SKU."
                )
        return result

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
        requirement_columns = [
            column for column in rows[0] if column.startswith("req_")
        ]
        hierarchy_level = max(
            (
                definition.hierarchy_level or 0
                for definition in catalog.values()
            ),
            default=0,
        )
        for column in requirement_columns:
            key = self.sku_attribute_key(column)
            level = None
            if key not in PHYSICAL_ATTRIBUTE_KEYS and key != OVERSIZE_CAPABLE_KEY:
                hierarchy_level += 1
                level = hierarchy_level
            if key in catalog:
                if catalog[key].hierarchy_level is None and level is not None:
                    catalog[key] = replace(
                        catalog[key], hierarchy_level=level
                    )
                continue
            raw_values = [
                str(row.get(column, "")).strip()
                for row in rows
                if str(row.get(column, "")).strip()
            ]
            catalog[key] = self.infer_attribute_definition(
                key, raw_values, level
            )
        if chilled_path is not None:
            catalog, _attribute_summary = self.inspect_sku_attribute_csv(
                chilled_path, catalog
            )
        if isinstance(attribute_catalog, dict) and catalog is not attribute_catalog:
            attribute_catalog.clear()
            attribute_catalog.update(catalog)
        sku_ids = [str(row.get("sku", "")).strip() for row in rows]
        if any(not sku for sku in sku_ids):
            raise ValueError("SKU CSV contains a blank SKU")
        if len(set(sku_ids)) != len(sku_ids):
            raise ValueError("SKU CSV contains duplicate SKU values")
        attribute_values = (
            self.load_sku_attribute_requirements(
                chilled_path, set(sku_ids), catalog
            )
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
                external = (
                    attribute_values.get(sku_ids[row_number - 2], {})
                    if attribute_values is not None else {}
                )
                for key, file_value in external.items():
                    conflicting = (
                        key in requirements and requirements[key] != file_value
                    )
                    if conflicting and key in PHYSICAL_ATTRIBUTE_KEYS:
                        try:
                            conflicting = not math.isclose(
                                float(requirements[key]),
                                float(file_value),
                                rel_tol=1e-9,
                                abs_tol=1e-12,
                            )
                        except (TypeError, ValueError):
                            pass
                    if conflicting:
                        raise ValueError(
                            f"conflicting {key} requirement between velocity and "
                            "SKU attributes CSV"
                        )
                    requirements[key] = file_value
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
    handling_unit_visit_metrics = staticmethod(handling_unit_visit_metrics)

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
        auto_plan_oversize: bool = False,
        ctbsa_target_racks: dict[str, str] | None = None,
        ctbsa_rank_by_sku: dict[str, int] | None = None,
        zone_workload_enabled: bool = False,
        maximum_same_sku_slots_per_rack: int | None = None,
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
        auto_plan_oversize: bool = False,
        zone_workload_enabled: bool = False,
        maximum_same_sku_slots_per_rack: int | None = None,
    ) -> tuple[list[dict], dict]:
        parameters = locals()
        service = parameters.pop("self")
        return create_strategy("abc_affinity").generate(service, **parameters)
