"""Generic expected-flow analysis and traffic-aware handling-unit slotting."""

from __future__ import annotations

import copy
import csv
import heapq
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import numpy as np

from .affinity import AffinityAnalysis, AffinityDataset
from .attributes import (
    PHYSICAL_ATTRIBUTE_KEYS,
    PHYSICAL_DIMENSION_KEYS,
    STANDARD_STORAGE_DEFAULTS,
    StorageAttributeService,
)
from .config import (
    LEGACY_PROJECT_SCHEMA,
    PROJECT_SCHEMA,
    SLOTTING_SCHEMA,
)
from .domain import GridProject
from .slotting import SlottingService
from .slotting_rules import overweight_storage_level, planned_storage_type


NETWORK_SCHEMA = "warehouse_movement_network/v1"
TRAFFIC_EXPORT_SCHEMA = "traffic_aware_slotting_analysis/v1"

ProgressCallback = Callable[[int, int, str], None]
CancelCallback = Callable[[], bool]


class TrafficCancelledError(RuntimeError):
    """Raised when traffic analysis or optimization is cancelled."""


class InsufficientStorageError(ValueError):
    """Raised when the full pipeline cannot place every SKU."""

    def __init__(self, summary: dict):
        self.summary = summary
        counts = summary.get("unassigned_status_counts", {})
        detail = ", ".join(
            f"{status}={count}"
            for status, count in sorted(counts.items()) if count
        ) or "no compatible contiguous capacity"
        super().__init__(
            f"{summary.get('unassigned_count', 0)} SKU(s) do not fit the configured "
            f"storage areas ({detail})"
        )


@dataclass(frozen=True, slots=True)
class MovementNode:
    node_id: str
    x: float
    y: float
    kind: str = "transit"


@dataclass(frozen=True, slots=True)
class MovementLink:
    link_id: str
    start: str
    end: str
    distance: float
    resource_id: str
    capacity: float | None = None


@dataclass(frozen=True, slots=True)
class ServiceEndpoint:
    node_id: str
    weight: float = 1.0
    endpoint_id: str = ""


@dataclass(slots=True)
class MovementNetwork:
    nodes: dict[str, MovementNode]
    links: tuple[MovementLink, ...]
    storage_nodes: dict[str, str]
    endpoints: tuple[ServiceEndpoint, ...]
    source_type: str
    source_path: str = ""
    resource_capacities: dict[str, float] = field(default_factory=dict)

    @property
    def resources(self) -> tuple[str, ...]:
        return tuple(sorted({link.resource_id for link in self.links}))


@dataclass(slots=True)
class TrafficDemand:
    unit_visits: dict[str, int]
    fulfillment_groups: int
    handling_unit_visits: int
    matched_events: int
    unmatched_skus: tuple[str, ...]
    start_date: str
    end_date: str


@dataclass(slots=True)
class TrafficAnalysis:
    demand: TrafficDemand
    metrics: dict
    resources: list[dict]
    unit_routes: dict[str, dict]
    mapped_units: tuple[str, ...]
    unreachable_units: tuple[str, ...]
    unmapped_units: tuple[str, ...]
    placements: dict[str, str]


@dataclass(slots=True)
class TrafficOptimizationResult:
    assignments: list[dict]
    before: TrafficAnalysis
    after: TrafficAnalysis
    relocations: list[dict]
    rejected_units: list[dict]
    parameters: dict
    trials: list[dict]


@dataclass(slots=True)
class TrafficPipelineResult:
    """One traffic workflow, optionally including initial layout generation."""

    basic_assignments: list[dict]
    basic_summary: dict
    affinity_assignments: list[dict]
    affinity_summary: dict
    basic_demand: TrafficDemand
    affinity_demand: TrafficDemand
    grouping_metrics: dict
    pretraffic_payload: dict
    pretraffic_analysis: TrafficAnalysis
    optimization: TrafficOptimizationResult | None
    output_payload: dict | None
    workflow_mode: str = "full_pipeline"
    initial_strategy: str = "abc_affinity"

    @property
    def baseline_assignments(self) -> list[dict]:
        return (
            self.basic_assignments
            if self.initial_strategy == "basic"
            else self.affinity_assignments
        )

    @property
    def baseline_summary(self) -> dict:
        return (
            self.basic_summary
            if self.initial_strategy == "basic"
            else self.affinity_summary
        )

    @property
    def baseline_demand(self) -> TrafficDemand:
        return (
            self.basic_demand
            if self.initial_strategy == "basic"
            else self.affinity_demand
        )


class TrafficAwareSlottingService:
    """Analyze expected traffic and reposition complete handling units."""

    LOCATION_FIELDS = (
        "static_address", "storage_location_address", "buffer_id", "buffer_level",
        "rmf_grid_address", "zone_id", "aisle_id", "static_bay_id", "rack_id",
        "rack_waypoint", "pickup_dispenser_id", "rack_vertex_index", "rack_rank",
        "workstations_evaluated", "average_workstation_distance_m", "routing_status",
        "storage_area_type", "planned_zone_id", "planned_storage_type",
        "zone_storage_type", "effective_location_attributes",
        "auto_attribute_overrides", "occupied_static_addresses",
        "occupied_storage_location_addresses", "occupied_buffer_ids",
        "occupied_handling_units",
    )

    def __init__(
        self,
        attributes: StorageAttributeService | None = None,
        slotting: SlottingService | None = None,
    ):
        self.attributes = attributes or StorageAttributeService()
        self.slotting = slotting or SlottingService(attributes=self.attributes)

    @staticmethod
    def _typed(value, default=None):
        return SlottingService.typed_value(value, default)

    def network_from_rmf(self, building: dict) -> MovementNetwork:
        """Adapt the first RMF level into the canonical movement network."""
        if not isinstance(building, dict) or not building.get("levels"):
            raise ValueError("slotting layout has no embedded RMF level")
        _level_name, level = next(iter(building["levels"].items()))
        vertices = level.get("vertices", [])
        if not vertices:
            raise ValueError("embedded RMF level contains no vertices")
        scale = self.slotting._distance_scale(building, level)
        nodes: dict[str, MovementNode] = {}
        storage_nodes: dict[str, str] = {}
        endpoints: list[ServiceEndpoint] = []
        for index, vertex in enumerate(vertices):
            node_id = f"v:{index}"
            params = vertex[4] if len(vertex) > 4 and isinstance(vertex[4], dict) else {}
            kind = "transit"
            if "pickup_dispenser" in params:
                kind = "storage"
            if "dropoff_ingestor" in params:
                kind = "endpoint"
            nodes[node_id] = MovementNode(
                node_id, float(vertex[0]), float(vertex[1]), kind
            )
            label = str(vertex[3]).strip() if len(vertex) > 3 else ""
            if "pickup_dispenser" in params:
                pickup = str(self._typed(params["pickup_dispenser"], label)).strip()
                for key in (label, pickup, str(index), f"vertex:{index}"):
                    if key:
                        storage_nodes[key] = node_id
            if "dropoff_ingestor" in params:
                endpoint_id = str(self._typed(params["dropoff_ingestor"], label)).strip()
                endpoints.append(ServiceEndpoint(node_id, 1.0, endpoint_id or label))
        links: list[MovementLink] = []
        capacities: dict[str, float] = {}
        for index, lane in enumerate(level.get("lanes", [])):
            if len(lane) < 3:
                continue
            start, end = int(lane[0]), int(lane[1])
            if not (0 <= start < len(vertices) and 0 <= end < len(vertices)):
                continue
            params = lane[2] if isinstance(lane[2], dict) else {}
            distance = math.hypot(
                float(vertices[start][0]) - float(vertices[end][0]),
                float(vertices[start][1]) - float(vertices[end][1]),
            ) * scale
            resource_id = str(self._typed(params.get("traffic_resource"), f"lane:{index}"))
            raw_capacity = self._typed(params.get("traffic_capacity"))
            capacity = None
            if raw_capacity not in (None, ""):
                capacity = float(raw_capacity)
                if not math.isfinite(capacity) or capacity <= 0:
                    raise ValueError(f"RMF lane {index} traffic capacity must be positive")
                capacities[resource_id] = capacity
            links.append(MovementLink(
                f"lane:{index}:forward", f"v:{start}", f"v:{end}",
                distance, resource_id, capacity,
            ))
            if bool(self._typed(params.get("bidirectional"), False)):
                links.append(MovementLink(
                    f"lane:{index}:reverse", f"v:{end}", f"v:{start}",
                    distance, resource_id, capacity,
                ))
        if not endpoints:
            raise ValueError("embedded RMF map contains no drop-off endpoint")
        return MovementNetwork(
            nodes, tuple(links), storage_nodes, tuple(endpoints), "embedded_rmf",
            resource_capacities=capacities,
        )

    def load_network(self, path: Path) -> MovementNetwork:
        """Load a generic movement network or adapt an editable grid project."""
        source = Path(path).expanduser().resolve()
        payload = json.loads(source.read_text(encoding="utf-8"))
        if payload.get("schema") in {
            PROJECT_SCHEMA,
            LEGACY_PROJECT_SCHEMA,
        }:
            project = GridProject.from_project_dict(payload)
            network = self.network_from_rmf(project.to_building_dict())
            network.source_type = "grid_project_json"
            network.source_path = str(source)
            return network
        if payload.get("schema") != NETWORK_SCHEMA:
            raise ValueError(
                "network JSON must be a warehouse movement network "
                f"({NETWORK_SCHEMA}) or RMF grid project "
                f"({PROJECT_SCHEMA})"
            )
        nodes: dict[str, MovementNode] = {}
        storage_nodes: dict[str, str] = {}
        for value in payload.get("nodes", []):
            node_id = str(value.get("id", "")).strip()
            if not node_id or node_id in nodes:
                raise ValueError("movement network node IDs must be non-blank and unique")
            node = MovementNode(
                node_id, float(value.get("x", 0)), float(value.get("y", 0)),
                str(value.get("kind", "transit")),
            )
            nodes[node_id] = node
            for key in value.get("location_ids", []):
                key = str(key).strip()
                if key:
                    storage_nodes[key] = node_id
        if not nodes:
            raise ValueError("movement network contains no nodes")
        capacities: dict[str, float] = {}
        for value in payload.get("resources", []):
            resource_id = str(value.get("id", "")).strip()
            if not resource_id:
                raise ValueError("movement resource ID cannot be blank")
            raw = value.get("capacity")
            if raw not in (None, ""):
                capacity = float(raw)
                if not math.isfinite(capacity) or capacity <= 0:
                    raise ValueError(f"resource {resource_id} capacity must be positive")
                capacities[resource_id] = capacity
        links: list[MovementLink] = []
        seen_links: set[str] = set()
        for index, value in enumerate(payload.get("links", [])):
            link_id = str(value.get("id", f"link:{index}")).strip()
            start, end = str(value.get("from", "")).strip(), str(value.get("to", "")).strip()
            if not link_id or link_id in seen_links:
                raise ValueError("movement link IDs must be non-blank and unique")
            if start not in nodes or end not in nodes:
                raise ValueError(f"link {link_id} references an unknown node")
            distance = float(value.get("travel_time", value.get("distance", 0)))
            if not math.isfinite(distance) or distance <= 0:
                raise ValueError(f"link {link_id} distance/travel_time must be positive")
            resource_id = str(value.get("resource_id", link_id)).strip()
            capacity = capacities.get(resource_id)
            links.append(MovementLink(link_id, start, end, distance, resource_id, capacity))
            seen_links.add(link_id)
            if bool(value.get("bidirectional", False)):
                reverse_id = f"{link_id}:reverse"
                links.append(MovementLink(reverse_id, end, start, distance, resource_id, capacity))
                seen_links.add(reverse_id)
        if not links:
            raise ValueError("movement network contains no links")
        endpoints = []
        for value in payload.get("endpoints", []):
            node_id = str(value.get("node_id", "")).strip()
            if node_id not in nodes:
                raise ValueError("movement endpoint references an unknown node")
            weight = float(value.get("weight", 1.0))
            if not math.isfinite(weight) or weight <= 0:
                raise ValueError("movement endpoint weight must be positive")
            endpoints.append(ServiceEndpoint(
                node_id, weight, str(value.get("id", node_id)).strip()
            ))
        if not endpoints:
            raise ValueError("movement network contains no service endpoints")
        for key, node_id in payload.get("storage_locations", {}).items():
            if node_id not in nodes:
                raise ValueError(f"storage location {key} references an unknown node")
            storage_nodes[str(key)] = str(node_id)
        return MovementNetwork(
            nodes, tuple(links), storage_nodes, tuple(endpoints), "generic_json",
            str(source), capacities,
        )

    @staticmethod
    def build_demand(
        dataset: AffinityDataset,
        assignments: list[dict],
        start_date=None,
        end_date=None,
    ) -> TrafficDemand:
        """Convert Store ID + Date groups into one visit per handling unit."""
        start = start_date or dataset.min_date
        end = end_date or dataset.max_date
        if start > end:
            raise ValueError("Start date must be on or before end date")
        sku_units: dict[str, dict[str, set[str]]] = {}
        for row in assignments:
            if row.get("assignment_status") != "ASSIGNED":
                continue
            sku = str(row.get("sku", "")).strip()
            if not sku:
                continue
            occupied_units = row.get("occupied_handling_units") or []
            if row.get("handling_unit_type") == "AMR shelf" or not occupied_units:
                occupied_units = [{
                    "handling_unit_id": row.get("handling_unit_id", "")
                }]
            units = {
                str(item.get("handling_unit_id", "")).strip()
                for item in occupied_units
                if str(item.get("handling_unit_id", "")).strip()
            }
            if units:
                primary = str(row.get("handling_unit_id", "")).strip()
                if primary:
                    sku_units.setdefault(sku, {}).setdefault(
                        primary, set()
                    ).update(units)
        mask = (dataset.dates >= start.toordinal()) & (dataset.dates <= end.toordinal())
        indices = np.flatnonzero(mask)
        if not len(indices):
            raise ValueError("No valid order events fall within the selected date range")
        groups: dict[tuple[int, int], dict[str, set[str]]] = {}
        unmatched: set[str] = set()
        matched_events = 0
        for event in indices:
            sku = dataset.skus[int(dataset.sku_indices[event])]
            unit_groups = sku_units.get(sku)
            if not unit_groups:
                unmatched.add(sku)
                continue
            key = (int(dataset.dates[event]), int(dataset.store_indices[event]))
            task_units = groups.setdefault(key, {})
            for primary, units in unit_groups.items():
                task_units.setdefault(primary, set()).update(units)
            matched_events += 1
        if not groups:
            raise ValueError("No order SKU is assigned in the selected slotting layout")
        unit_visits: dict[str, int] = {}
        for units in groups.values():
            for primary, physical_units in units.items():
                unit_visits[primary] = (
                    unit_visits.get(primary, 0) + len(physical_units)
                )
        return TrafficDemand(
            unit_visits=unit_visits,
            fulfillment_groups=len(groups),
            handling_unit_visits=sum(unit_visits.values()),
            matched_events=matched_events,
            unmatched_skus=tuple(sorted(unmatched)),
            start_date=start.isoformat(),
            end_date=end.isoformat(),
        )

    @staticmethod
    def _resolve_node(row: dict, network: MovementNetwork) -> str | None:
        keys = (
            row.get("static_address"), row.get("rack_id"), row.get("rack_waypoint"),
            row.get("pickup_dispenser_id"),
            f"vertex:{row.get('rack_vertex_index')}" if row.get("rack_vertex_index") not in (None, "") else "",
            str(row.get("rack_vertex_index", "")),
        )
        return next((network.storage_nodes[str(key)] for key in keys if str(key) in network.storage_nodes), None)

    @staticmethod
    def _shortest_routes(network: MovementNetwork) -> dict[tuple[str, str], tuple[float, tuple[str, ...], tuple[str, ...]]]:
        graph: dict[str, list[MovementLink]] = {node: [] for node in network.nodes}
        for link in network.links:
            graph[link.start].append(link)
        for links in graph.values():
            links.sort(key=lambda link: (link.end, link.link_id))
        routes = {}
        sources = sorted(set(network.storage_nodes.values()))
        endpoint_nodes = sorted({endpoint.node_id for endpoint in network.endpoints})
        for source in sources:
            distance = {source: 0.0}
            signature = {source: ()}
            previous: dict[str, tuple[str, MovementLink]] = {}
            queue = [(0.0, (), source)]
            while queue:
                cost, path_signature, node = heapq.heappop(queue)
                if cost > distance.get(node, math.inf) + 1e-9 or path_signature != signature.get(node):
                    continue
                for link in graph.get(node, []):
                    candidate = cost + link.distance
                    candidate_signature = path_signature + (link.link_id,)
                    current = distance.get(link.end, math.inf)
                    if candidate < current - 1e-9 or (
                        abs(candidate - current) <= 1e-9
                        and candidate_signature < signature.get(link.end, ("~",))
                    ):
                        distance[link.end] = candidate
                        signature[link.end] = candidate_signature
                        previous[link.end] = (node, link)
                        heapq.heappush(queue, (candidate, candidate_signature, link.end))
            for endpoint in endpoint_nodes:
                if endpoint not in distance:
                    continue
                node = endpoint
                path_links: list[str] = []
                resources: list[str] = []
                while node != source:
                    prior, link = previous[node]
                    path_links.append(link.link_id)
                    resources.append(link.resource_id)
                    node = prior
                routes[(source, endpoint)] = (
                    distance[endpoint], tuple(reversed(path_links)), tuple(reversed(resources))
                )
        return routes

    def analyze(
        self,
        assignments: list[dict],
        network: MovementNetwork,
        demand: TrafficDemand,
        *,
        placements: dict[str, str] | None = None,
        route_cache=None,
        relative_reference: float | None = None,
    ) -> TrafficAnalysis:
        groups: dict[str, list[dict]] = {}
        for row in assignments:
            if row.get("assignment_status") == "ASSIGNED" and row.get("handling_unit_id"):
                groups.setdefault(str(row["handling_unit_id"]), []).append(row)
        resolved = dict(placements or {})
        unmapped: list[str] = []
        for unit, rows in sorted(groups.items()):
            if unit in resolved:
                continue
            node = self._resolve_node(rows[0], network)
            if node is None:
                unmapped.append(unit)
            else:
                resolved[unit] = node
        route_cache = route_cache or self._shortest_routes(network)
        loads = {resource: 0.0 for resource in network.resources}
        contributors: dict[str, dict[str, float]] = {resource: {} for resource in network.resources}
        unit_routes: dict[str, dict] = {}
        unreachable: list[str] = []
        total_travel = 0.0
        for unit, visits in sorted(demand.unit_visits.items()):
            node = resolved.get(unit)
            if node is None:
                if unit not in unmapped:
                    unmapped.append(unit)
                continue
            reachable = [
                endpoint for endpoint in network.endpoints
                if (node, endpoint.node_id) in route_cache
            ]
            if not reachable:
                unreachable.append(unit)
                continue
            weight_total = sum(endpoint.weight for endpoint in reachable)
            route_rows = []
            unit_distance = 0.0
            resource_weights: dict[str, float] = {}
            for endpoint in reachable:
                share = endpoint.weight / weight_total
                distance, link_ids, resources = route_cache[(node, endpoint.node_id)]
                flow = visits * share
                unit_distance += flow * distance
                total_travel += flow * distance
                for resource in resources:
                    loads[resource] += flow
                    contributors[resource][unit] = contributors[resource].get(unit, 0.0) + flow
                    resource_weights[resource] = resource_weights.get(resource, 0.0) + flow
                route_rows.append({
                    "endpoint": endpoint.endpoint_id or endpoint.node_id,
                    "weight": share,
                    "distance": distance,
                    "links": list(link_ids),
                    "resources": list(resources),
                })
            unit_routes[unit] = {
                "node_id": node, "visits": visits, "weighted_travel": unit_distance,
                "routes": route_rows, "resource_flows": resource_weights,
            }
        positive_raw = [value for value in loads.values() if value > 0]
        relative_reference = (
            float(relative_reference)
            if relative_reference is not None
            else float(np.mean(positive_raw)) if positive_raw else 1.0
        )
        resource_rows = []
        for resource in sorted(loads):
            raw = loads[resource]
            capacity = network.resource_capacities.get(resource)
            normalized = raw / capacity if capacity else raw / relative_reference
            resource_rows.append({
                "resource_id": resource,
                "load": raw,
                "capacity": capacity,
                "utilization": raw / capacity if capacity else None,
                "normalized_load": normalized,
                "contributors": [
                    {"handling_unit_id": unit, "flow": flow}
                    for unit, flow in sorted(
                        contributors[resource].items(), key=lambda item: (-item[1], item[0])
                    )
                ],
            })
        normalized_values = np.array(
            [row["normalized_load"] for row in resource_rows], dtype=float
        )
        peak = float(normalized_values.max()) if len(normalized_values) else 0.0
        p95 = float(np.percentile(normalized_values, 95)) if len(normalized_values) else 0.0
        return TrafficAnalysis(
            demand=demand,
            metrics={
                "peak_load": peak,
                "p95_load": p95,
                "raw_peak_load": max(positive_raw, default=0.0),
                "raw_p95_load": (
                    float(np.percentile(positive_raw, 95)) if positive_raw else 0.0
                ),
                "relative_reference": relative_reference,
                "expected_travel": total_travel,
                "resource_count": len(resource_rows),
                "capacity_mode": bool(network.resource_capacities),
            },
            resources=resource_rows,
            unit_routes=unit_routes,
            mapped_units=tuple(sorted(unit_routes)),
            unreachable_units=tuple(sorted(unreachable)),
            unmapped_units=tuple(sorted(set(unmapped))),
            placements=resolved,
        )

    def _strict_unit_compatibility(
        self, source_rows: list[dict], target_rows: list[dict], payload: dict
    ) -> tuple[bool, str]:
        source_shape = sorted(
            (
                int(row.get("storage_level") or 1),
                int(row.get("storage_slot") or 1),
                int(row.get("occupied_slot_count") or 1),
                int(row.get("occupied_level_span") or 1),
                int(row.get("occupied_horizontal_slot_span") or 1),
            )
            for row in source_rows
        )
        target_by_shape = {
            (
                int(row.get("storage_level") or 1),
                int(row.get("storage_slot") or 1),
                int(row.get("occupied_slot_count") or 1),
                int(row.get("occupied_level_span") or 1),
                int(row.get("occupied_horizontal_slot_span") or 1),
            ): row
            for row in target_rows
        }
        if source_shape != sorted(target_by_shape):
            return False, "handling units have different slot capacities or shapes"
        catalog = payload.get("attribute_catalog", [])
        local = payload.get("location_attributes", {})
        if not self.attributes.has_physical_catalog(catalog):
            return False, "physical capacity definitions are unavailable"
        for row in source_rows:
            requirements = row.get("sku_requirements") or {}
            missing = [key for key in PHYSICAL_ATTRIBUTE_KEYS if key not in requirements]
            if missing:
                return False, f"SKU {row.get('sku', '')} has incomplete physical requirements"
            shape = (
                int(row.get("storage_level") or 1),
                int(row.get("storage_slot") or 1),
                int(row.get("occupied_slot_count") or 1),
                int(row.get("occupied_level_span") or 1),
                int(row.get("occupied_horizontal_slot_span") or 1),
            )
            target = target_by_shape[shape]
            target_addresses = target.get("occupied_static_addresses") or [
                str(target.get("static_address", ""))
            ]
            if len(target_addresses) != shape[2]:
                return False, "target multi-slot reservation is incomplete"
            profile = self.attributes.physical_profile(requirements)
            required_storage_type = planned_storage_type(profile)
            target_storage_type = str(
                target.get("planned_storage_type")
                or target.get("zone_storage_type")
                or target.get("storage_area_type")
                or ""
            ).upper()
            if (
                target_storage_type
                and target_storage_type != required_storage_type
            ):
                return False, (
                    f"SKU {row.get('sku', '')}: {required_storage_type} inventory "
                    f"cannot move into a {target_storage_type} segment"
                )
            for target_address in target_addresses:
                effective, _sources = self.attributes.effective_attributes(
                    str(
                        target.get("storage_location_address")
                        or target_address
                    ),
                    local,
                )
                generic_requirements = {
                    key: value for key, value in requirements.items()
                    if key not in PHYSICAL_DIMENSION_KEYS
                }
                issues = self.attributes.compatibility_issues(
                    generic_requirements, effective, catalog
                )
                target_level_span = int(
                    target.get("occupied_level_span") or shape[3]
                )
                target_slot_span = int(
                    target.get("occupied_horizontal_slot_span") or shape[4]
                )
                required_footprint = self.slotting.required_slot_footprint(
                    requirements, effective,
                    target_level_span, target_slot_span,
                )
                if required_footprint is None:
                    issues.append(
                        "requires a larger contiguous level/slot footprint"
                    )
                if issues:
                    return False, f"SKU {row.get('sku', '')}: " + "; ".join(issues)
            if profile["storage_class"] in {"OVERWEIGHT", "OVERSIZE_AND_OVERWEIGHT"}:
                levels_per_rack = int(
                    payload.get("rack_capacity", {}).get("levels") or 1
                )
                required_level = overweight_storage_level(levels_per_rack)
                if shape[0] != required_level:
                    return False, (
                        f"SKU {row.get('sku', '')}: overweight inventory requires "
                        f"level {required_level}"
                    )
        return True, ""

    @staticmethod
    def _objective(analysis: TrafficAnalysis) -> tuple[float, float, float]:
        return (
            float(analysis.metrics["peak_load"]),
            float(analysis.metrics["p95_load"]),
            float(analysis.metrics["expected_travel"]),
        )

    @staticmethod
    def _load_objective(
        raw_loads: np.ndarray,
        capacities: np.ndarray,
        travel: float,
        relative_reference: float,
    ) -> tuple[float, float, float]:
        normalized = np.divide(
            raw_loads,
            capacities,
            out=raw_loads / max(float(relative_reference), 1e-12),
            where=np.isfinite(capacities),
        )
        return (
            float(normalized.max()) if len(normalized) else 0.0,
            float(np.percentile(normalized, 95)) if len(normalized) else 0.0,
            float(travel),
        )

    @staticmethod
    def _unit_contribution(
        unit: str,
        node: str,
        visits: int,
        network: MovementNetwork,
        route_cache,
        resource_indices: dict[str, int],
        cache: dict,
    ) -> tuple[np.ndarray, float] | None:
        key = (unit, node)
        if key in cache:
            return cache[key]
        reachable = [
            endpoint for endpoint in network.endpoints
            if (node, endpoint.node_id) in route_cache
        ]
        if not reachable:
            cache[key] = None
            return None
        total_weight = sum(endpoint.weight for endpoint in reachable)
        loads = np.zeros(len(resource_indices), dtype=float)
        travel = 0.0
        for endpoint in reachable:
            share = endpoint.weight / total_weight
            distance, _links, resources = route_cache[(node, endpoint.node_id)]
            flow = visits * share
            travel += flow * distance
            for resource in resources:
                loads[resource_indices[resource]] += flow
        cache[key] = (loads, travel)
        return loads, travel

    def _apply_unit_swap(self, rows: list[dict], first: str, second: str, payload: dict) -> None:
        grouped = {
            unit: {
                (int(row.get("storage_level") or 1), int(row.get("storage_slot") or 1)): row
                for row in rows if str(row.get("handling_unit_id", "")) == unit
            }
            for unit in (first, second)
        }
        snapshots = {
            unit: {
                shape: {field: row.get(field) for field in self.LOCATION_FIELDS}
                for shape, row in group.items()
            }
            for unit, group in grouped.items()
        }
        for source, target in ((first, second), (second, first)):
            for shape, row in grouped[source].items():
                row.update(snapshots[target][shape])
                level, slot = shape
                source_units = (
                    snapshots[source][shape].get("occupied_handling_units") or []
                )
                target_units = (
                    snapshots[target][shape].get("occupied_handling_units") or []
                )
                relocated_units = []
                for index, target_location in enumerate(target_units):
                    location = dict(target_location)
                    source_unit_id = (
                        source_units[index].get("handling_unit_id")
                        if index < len(source_units)
                        else source
                    )
                    location["handling_unit_id"] = source_unit_id
                    relocated_units.append(location)
                row["occupied_handling_units"] = relocated_units or [{
                    "handling_unit_id": source,
                    "rack_id": row.get("rack_id", ""),
                    "storage_level": level,
                    "storage_slot": slot,
                }]
                row["dynamic_address"], row["dynamic_address_level"] = self.slotting.build_dynamic_address(
                    str(row["zone_id"]), str(row["aisle_id"]), str(row["static_bay_id"]),
                    level, slot, str(row.get("handling_unit_type", "")), source,
                    buffer_model=bool(payload.get("storage_layout")),
                )
                row["compatibility_status"] = "COMPATIBLE"
                row["compatibility_issues"] = []

    def _candidate_pairs(
        self, analysis: TrafficAnalysis, units: list[str], percentile: float
    ) -> list[tuple[str, str]]:
        if len(units) < 2:
            return []
        threshold = float(np.percentile(
            [row["normalized_load"] for row in analysis.resources], percentile
        )) if analysis.resources else 0.0
        hot_scores = {unit: 0.0 for unit in units}
        for resource in analysis.resources:
            if resource["normalized_load"] + 1e-12 < threshold:
                continue
            for contributor in resource["contributors"]:
                unit = contributor["handling_unit_id"]
                if unit in hot_scores:
                    hot_scores[unit] += contributor["flow"] * resource["normalized_load"]
        hot = sorted(units, key=lambda unit: (-hot_scores[unit], unit))
        sample_size = min(len(units), max(2, int(math.ceil(math.sqrt(len(units)) * 4))))
        hot = hot[:sample_size]
        pairs = {
            tuple(sorted((first, second)))
            for first in hot for second in units if first != second
        }
        return sorted(pairs)

    def _feasible_pairs(
        self,
        payload: dict,
        demand: TrafficDemand,
        candidate_pairs: list[tuple[str, str]] | None = None,
    ) -> tuple[list[tuple[str, str]], list[dict]]:
        groups: dict[str, list[dict]] = {}
        for row in payload["assignments"]:
            if row.get("assignment_status") == "ASSIGNED" and row.get("handling_unit_id"):
                groups.setdefault(str(row["handling_unit_id"]), []).append(row)
        active = sorted(unit for unit in demand.unit_visits if unit in groups)
        feasible, rejected, eligible = [], [], []
        for unit in active:
            incomplete = any(
                self.attributes.physical_profile(row.get("sku_requirements") or {})[
                    "data_status"
                ] != "COMPLETE"
                for row in groups[unit]
            )
            if incomplete:
                rejected.append({
                    "handling_unit_id": unit,
                    "reason": "incomplete physical requirements",
                })
            else:
                eligible.append(unit)
        eligible_set = set(eligible)
        pairs = (
            [pair for pair in candidate_pairs or [] if set(pair) <= eligible_set]
            if candidate_pairs is not None
            else [
                (first, second)
                for index, first in enumerate(eligible)
                for second in eligible[index + 1:]
            ]
        )
        for first, second in pairs:
                forward, reason = self._strict_unit_compatibility(groups[first], groups[second], payload)
                if not forward:
                    continue
                reverse, _reason = self._strict_unit_compatibility(groups[second], groups[first], payload)
                if reverse:
                    feasible.append((first, second))
        return feasible, rejected

    @staticmethod
    def _suggest_hotspot_percentile(resource_values: list[float]) -> float:
        """Find the empirical knee separating ordinary and high resource load."""
        values = np.array(sorted(value for value in resource_values if value > 0), dtype=float)
        if len(values) <= 1 or abs(float(values[-1] - values[0])) <= 1e-12:
            return 0.0
        gaps = np.diff(values) / max(float(values[-1] - values[0]), 1e-12)
        index = int(np.argmax(gaps))
        return 100.0 * (index + 1) / len(values)

    @staticmethod
    def _pareto_knee(results):
        """Select the geometric knee of the non-dominated peak/travel frontier."""
        ordered = sorted(
            results,
            key=lambda result: (
                result[3]["travel_change_fraction"],
                result[3]["peak_load"],
                result[3]["p95_load"],
                result[3]["relocation_count"],
            ),
        )
        frontier = []
        best_peak = math.inf
        for result in ordered:
            peak = float(result[3]["peak_load"])
            if peak < best_peak - 1e-12:
                frontier.append(result)
                best_peak = peak
        if len(frontier) <= 2:
            return min(
                frontier or ordered,
                key=lambda result: (
                    result[3]["peak_load"], result[3]["p95_load"],
                    result[3]["travel_change_fraction"], result[3]["relocation_count"],
                ),
            )
        travels = np.array([row[3]["travel_change_fraction"] for row in frontier], dtype=float)
        peaks = np.array([row[3]["peak_load"] for row in frontier], dtype=float)
        x = (travels - travels.min()) / max(float(np.ptp(travels)), 1e-12)
        y = (peaks - peaks.min()) / max(float(np.ptp(peaks)), 1e-12)
        start = np.array([x[0], y[0]])
        end = np.array([x[-1], y[-1]])
        vector = end - start
        denominator = max(float(np.linalg.norm(vector)), 1e-12)
        distances = [
            abs(float(np.cross(vector, np.array([x[index], y[index]]) - start))) / denominator
            for index in range(len(frontier))
        ]
        maximum = max(distances)
        candidates = [
            frontier[index] for index, distance in enumerate(distances)
            if abs(distance - maximum) <= 1e-12
        ]
        return min(
            candidates,
            key=lambda result: (
                result[3]["peak_load"], result[3]["p95_load"],
                result[3]["travel_change_fraction"], result[3]["relocation_count"],
            ),
        )

    def _optimize_for_cap(
        self,
        payload: dict,
        network: MovementNetwork,
        demand: TrafficDemand,
        baseline: TrafficAnalysis,
        feasible_pairs: list[tuple[str, str]],
        travel_cap: float,
        hotspot_percentile: float,
        route_cache,
        contribution_cache,
        cancelled: CancelCallback | None,
    ) -> tuple[list[dict], TrafficAnalysis, list[dict]]:
        rows = copy.deepcopy(payload["assignments"])
        placements = dict(baseline.placements)
        relative_reference = float(baseline.metrics["relative_reference"])
        current = self.analyze(
            rows, network, demand, placements=placements,
            route_cache=route_cache, relative_reference=relative_reference,
        )
        moves: list[dict] = []
        baseline_travel = float(baseline.metrics["expected_travel"])
        limit = baseline_travel * (1.0 + max(0.0, travel_cap))
        units = sorted(demand.unit_visits)
        resources = network.resources
        resource_indices = {resource: index for index, resource in enumerate(resources)}
        capacities = np.array([
            network.resource_capacities.get(resource, math.nan)
            for resource in resources
        ], dtype=float)
        current_raw = np.array([
            next(
                row["load"] for row in current.resources
                if row["resource_id"] == resource
            )
            for resource in resources
        ], dtype=float)
        current_travel = float(current.metrics["expected_travel"])
        for _iteration in range(max(1, int(math.ceil(math.sqrt(len(units)))))):
            if cancelled and cancelled():
                raise TrafficCancelledError("Traffic optimization was cancelled")
            candidate_scope = set(self._candidate_pairs(current, units, hotspot_percentile))
            best = None
            for first, second in feasible_pairs:
                if (first, second) not in candidate_scope:
                    continue
                candidate_placements = dict(placements)
                candidate_placements[first], candidate_placements[second] = (
                    candidate_placements.get(second), candidate_placements.get(first)
                )
                if candidate_placements[first] is None or candidate_placements[second] is None:
                    continue
                first_old = self._unit_contribution(
                    first, placements[first], demand.unit_visits[first], network,
                    route_cache, resource_indices, contribution_cache,
                )
                second_old = self._unit_contribution(
                    second, placements[second], demand.unit_visits[second], network,
                    route_cache, resource_indices, contribution_cache,
                )
                first_new = self._unit_contribution(
                    first, candidate_placements[first], demand.unit_visits[first], network,
                    route_cache, resource_indices, contribution_cache,
                )
                second_new = self._unit_contribution(
                    second, candidate_placements[second], demand.unit_visits[second], network,
                    route_cache, resource_indices, contribution_cache,
                )
                if any(
                    value is None
                    for value in (first_old, second_old, first_new, second_new)
                ):
                    continue
                candidate_raw = (
                    current_raw - first_old[0] - second_old[0]
                    + first_new[0] + second_new[0]
                )
                candidate_travel = (
                    current_travel - first_old[1] - second_old[1]
                    + first_new[1] + second_new[1]
                )
                if candidate_travel > limit + 1e-9:
                    continue
                objective = self._load_objective(
                    candidate_raw, capacities, candidate_travel,
                    relative_reference,
                )
                if objective >= self._load_objective(
                    current_raw, capacities, current_travel,
                    relative_reference,
                ):
                    continue
                key = (*objective, first, second)
                if best is None or key < best[0]:
                    best = (
                        key, first, second, candidate_placements,
                        candidate_raw, candidate_travel,
                    )
            if best is None:
                break
            _key, first, second, placements, current_raw, current_travel = best
            first_rows = [row for row in rows if str(row.get("handling_unit_id", "")) == first]
            second_rows = [row for row in rows if str(row.get("handling_unit_id", "")) == second]
            old_first = str(first_rows[0].get("static_bay_id", first_rows[0].get("static_address", "")))
            old_second = str(second_rows[0].get("static_bay_id", second_rows[0].get("static_address", "")))
            self._apply_unit_swap(rows, first, second, payload)
            current = self.analyze(
                rows, network, demand, placements=placements,
                route_cache=route_cache, relative_reference=relative_reference,
            )
            moves.extend((
                {"handling_unit_id": first, "from": old_first, "to": old_second, "swap_with": second},
                {"handling_unit_id": second, "from": old_second, "to": old_first, "swap_with": first},
            ))
        final = self.analyze(
            rows, network, demand, placements=placements,
            route_cache=route_cache, relative_reference=relative_reference,
        )
        return rows, final, moves

    def optimize(
        self,
        payload: dict,
        network: MovementNetwork,
        demand: TrafficDemand,
        *,
        maximum_travel_increase: float | None = None,
        hotspot_percentile: float | None = None,
        progress: ProgressCallback | None = None,
        cancelled: CancelCallback | None = None,
    ) -> TrafficOptimizationResult:
        """Select a deterministic traffic/travel Pareto solution."""
        route_cache = self._shortest_routes(network)
        contribution_cache: dict = {}
        baseline = self.analyze(
            payload["assignments"], network, demand, route_cache=route_cache
        )
        resource_values = [row["normalized_load"] for row in baseline.resources]
        suggested_percentile = self._suggest_hotspot_percentile(resource_values)
        selected_percentile = float(
            suggested_percentile if hotspot_percentile is None else hotspot_percentile
        )
        if not 0 <= selected_percentile <= 100:
            raise ValueError("hotspot percentile must be between 0 and 100")
        active_units = sorted(demand.unit_visits)
        # Exhaustive compatibility remains practical for smaller layouts. For
        # large bin/tote systems, validate the data-derived hotspot candidate
        # set to avoid quadratic setup time.
        candidate_pairs = None
        if len(active_units) > 256:
            candidate_pairs = self._candidate_pairs(
                baseline, active_units, selected_percentile
            )
        feasible, rejected = self._feasible_pairs(
            payload, demand, candidate_pairs
        )
        if progress:
            progress(1, 3, f"Validated {len(feasible):,} feasible unit swaps")
        baseline_travel = float(baseline.metrics["expected_travel"])
        empirical_deltas = []
        resources = network.resources
        resource_indices = {resource: index for index, resource in enumerate(resources)}
        for first, second in feasible:
            placements = dict(baseline.placements)
            if first not in placements or second not in placements:
                continue
            first_old = self._unit_contribution(
                first, placements[first], demand.unit_visits[first], network,
                route_cache, resource_indices, contribution_cache,
            )
            second_old = self._unit_contribution(
                second, placements[second], demand.unit_visits[second], network,
                route_cache, resource_indices, contribution_cache,
            )
            first_new = self._unit_contribution(
                first, placements[second], demand.unit_visits[first], network,
                route_cache, resource_indices, contribution_cache,
            )
            second_new = self._unit_contribution(
                second, placements[first], demand.unit_visits[second], network,
                route_cache, resource_indices, contribution_cache,
            )
            if any(
                value is None
                for value in (first_old, second_old, first_new, second_new)
            ):
                continue
            candidate_travel = (
                baseline_travel - first_old[1] - second_old[1]
                + first_new[1] + second_new[1]
            )
            empirical_deltas.append(max(
                0.0,
                (candidate_travel - baseline_travel) / max(baseline_travel, 1e-9),
            ))
        if maximum_travel_increase is None:
            positive = sorted(set(round(value, 8) for value in empirical_deltas if value > 1e-12))
            if positive:
                caps = sorted(set(
                    [0.0] + [float(np.percentile(positive, q)) for q in (25, 50, 75)]
                ))
            else:
                caps = [0.0]
            parameter_status = "AUTO_SUGGESTED"
        else:
            if maximum_travel_increase < 0:
                raise ValueError("maximum travel increase cannot be negative")
            caps = [float(maximum_travel_increase)]
            parameter_status = "USER_ADJUSTED"
        trials = []
        results = []
        for index, cap in enumerate(caps, start=1):
            if cancelled and cancelled():
                raise TrafficCancelledError("Traffic optimization was cancelled")
            rows, analysis, moves = self._optimize_for_cap(
                payload, network, demand, baseline, feasible, cap,
                selected_percentile, route_cache, contribution_cache, cancelled,
            )
            peak_reduction = baseline.metrics["peak_load"] - analysis.metrics["peak_load"]
            travel_change = (
                (analysis.metrics["expected_travel"] - baseline_travel)
                / max(baseline_travel, 1e-9)
            )
            trial = {
                "maximum_travel_increase": cap,
                "peak_load": analysis.metrics["peak_load"],
                "p95_load": analysis.metrics["p95_load"],
                "travel_change_fraction": travel_change,
                "peak_reduction": peak_reduction,
                "relocation_count": len({move["handling_unit_id"] for move in moves}),
            }
            trials.append(trial)
            results.append((rows, analysis, moves, trial))
            if progress:
                progress(1 + index, 2 + len(caps), f"Evaluated traffic/travel candidate {index}/{len(caps)}")
        chosen = self._pareto_knee(results)
        rows, after, moves, selected_trial = chosen
        moved_units = {move["handling_unit_id"] for move in moves}
        self.validate_result(payload["assignments"], rows, payload, moved_units)
        threshold = float(np.percentile(resource_values, selected_percentile)) if resource_values else 0.0
        parameters = {
            "parameter_status": parameter_status,
            "maximum_travel_increase": selected_trial["maximum_travel_increase"],
            "hotspot_percentile": selected_percentile,
            "hotspot_threshold": threshold,
            "baseline_peak_load": baseline.metrics["peak_load"],
            "feasible_swap_count": len(feasible),
            "empirical_candidate_count": len(caps),
        }
        if progress:
            progress(2 + len(caps), 2 + len(caps), "Traffic-aware layout validated")
        return TrafficOptimizationResult(rows, baseline, after, moves, rejected, parameters, trials)

    def validate_result(
        self,
        original: list[dict],
        result: list[dict],
        payload: dict,
        moved_units: set[str] | None = None,
    ) -> None:
        if len(original) != len(result):
            raise ValueError("traffic result changed the assignment row count")
        identity = lambda row: (str(row.get("sku", "")), str(row.get("handling_unit_id", "")))
        if sorted(map(identity, original)) != sorted(map(identity, result)):
            raise ValueError("traffic result changed SKU or handling-unit membership")
        addresses = [
            str(address)
            for row in result if row.get("assignment_status") == "ASSIGNED"
            for address in (
                row.get("occupied_storage_location_addresses")
                or row.get("occupied_static_addresses")
                or [row.get("static_address", "")]
            )
        ]
        if len(addresses) != len(set(addresses)):
            raise ValueError("traffic result contains duplicate occupied addresses")
        groups: dict[str, list[dict]] = {}
        for row in result:
            if row.get("assignment_status") == "ASSIGNED":
                groups.setdefault(str(row.get("handling_unit_id", "")), []).append(row)
        for unit, rows in groups.items():
            # Conservative units with incomplete source data are permitted to
            # remain exactly where the baseline placed them, but can never move.
            if moved_units is not None and unit not in moved_units:
                continue
            compatible, reason = self._strict_unit_compatibility(rows, rows, payload)
            if not compatible:
                raise ValueError(f"traffic result unit {unit} failed validation: {reason}")

    def validate_traffic_baseline(self, payload: dict) -> dict:
        """Apply the shared hard rules before any traffic relocation."""
        rows = payload.get("assignments", [])
        unassigned = [
            row for row in rows if row.get("assignment_status") != "ASSIGNED"
        ]
        if unassigned:
            summary = copy.deepcopy(payload.get("summary", {}))
            counts: dict[str, int] = {}
            for row in unassigned:
                status = str(row.get("assignment_status", "UNASSIGNED"))
                counts[status] = counts.get(status, 0) + 1
            summary["unassigned_count"] = len(unassigned)
            summary["unassigned_status_counts"] = counts
            summary.setdefault("capacity", len(rows) - len(unassigned))
            raise InsufficientStorageError(summary)
        if not rows:
            raise ValueError("slotting layout contains no assignments")

        levels_per_rack = int(
            payload.get("rack_capacity", {}).get("levels") or 1
        )
        catalog = self.attributes.normalize_catalog(
            payload.get("attribute_catalog")
        )
        locations = payload.get("location_attributes", {})
        invalid = []
        unverified = 0
        for row in rows:
            requirements = row.get("sku_requirements") or {}
            profile = self.attributes.physical_profile(requirements)
            required_storage_type = planned_storage_type(profile)
            actual_storage_type = str(
                row.get("planned_storage_type")
                or row.get("zone_storage_type")
                or row.get("storage_area_type")
                or ""
            ).upper()
            if actual_storage_type and actual_storage_type != required_storage_type:
                invalid.append(
                    f"{row.get('sku', '')}: {required_storage_type} inventory "
                    f"cannot use a {actual_storage_type} segment"
                )
                continue
            if profile["data_status"] != "COMPLETE":
                unverified += 1
                continue
            occupied_addresses = row.get("occupied_static_addresses") or [
                str(row.get("static_address", ""))
            ]
            issues = []
            primary_effective = None
            generic_requirements = {
                key: value for key, value in requirements.items()
                if key not in PHYSICAL_DIMENSION_KEYS
            }
            for address in occupied_addresses:
                effective, _sources = self.attributes.effective_attributes(
                    str(row.get("storage_location_address") or address),
                    locations,
                )
                primary_effective = primary_effective or effective
                issues.extend(self.attributes.compatibility_issues(
                    generic_requirements, effective, catalog
                ))
            level_span = int(row.get("occupied_level_span") or 1)
            slot_span = int(
                row.get("occupied_horizontal_slot_span")
                or len(occupied_addresses)
            )
            if self.slotting.required_slot_footprint(
                requirements, primary_effective or {}, level_span, slot_span
            ) is None:
                issues.append(
                    f"requires more than the reserved {level_span} × {slot_span} "
                    "level/slot footprint"
                )
            if (
                profile["storage_class"]
                in {"OVERWEIGHT", "OVERSIZE_AND_OVERWEIGHT"}
                and int(row.get("storage_level") or 1)
                != overweight_storage_level(levels_per_rack)
            ):
                issues.append(
                    "overweight inventory requires level "
                    f"{overweight_storage_level(levels_per_rack)}"
                )
            if issues:
                invalid.append(
                    f"{row.get('sku', '')}: "
                    + "; ".join(dict.fromkeys(issues))
                )
        if invalid:
            raise ValueError(
                "slotting layout failed hard compatibility validation: "
                + " | ".join(invalid[:5])
            )
        return {
            "hard_validation_status": "PASSED",
            "unverified_physical_sku_count": unverified,
        }

    @staticmethod
    def _buffer_records(payload: dict, rows: list[dict]) -> list[dict]:
        """Rebuild occupied/empty buffer records from the supplied assignments."""
        occupied: dict[str, set[str]] = {}
        for row in rows:
            if row.get("assignment_status") != "ASSIGNED":
                continue
            default_unit = str(row.get("handling_unit_id", ""))
            units = row.get("occupied_handling_units") or [{
                "handling_unit_id": default_unit
            }]
            buffer_ids = [
                str(value) for value in (row.get("occupied_buffer_ids") or [])
                if str(value)
            ]
            mapped = [
                (
                    str(unit.get("buffer_id", "")),
                    str(unit.get("handling_unit_id", "")),
                )
                for unit in units
                if str(unit.get("buffer_id", ""))
                and str(unit.get("handling_unit_id", ""))
            ]
            if not mapped and len(buffer_ids) == len(units):
                ordered_units = sorted(
                    units,
                    key=lambda unit: (
                        int(unit.get("storage_level") or 1),
                        int(unit.get("storage_slot") or 1),
                    ),
                )
                mapped = [
                    (buffer_id, str(unit.get("handling_unit_id", "")))
                    for buffer_id, unit in zip(
                        sorted(buffer_ids), ordered_units
                    )
                ]
            if mapped:
                for buffer_id, unit_id in mapped:
                    occupied.setdefault(buffer_id, set()).add(unit_id)
            else:
                unit_ids = {
                    str(unit.get("handling_unit_id", "")).strip()
                    for unit in units
                    if str(unit.get("handling_unit_id", "")).strip()
                }
                for buffer_id in buffer_ids:
                    occupied.setdefault(buffer_id, set()).update(unit_ids)
        source_records = payload.get("buffers", [])
        if not source_records:
            storage_layout = payload.get("storage_layout") or {}
            source_records = storage_layout.get("buffers", [])
        records = []
        for source in source_records:
            buffer_id = str(source.get("buffer_id", ""))
            records.append({
                **dict(source),
                "status": "OCCUPIED" if buffer_id in occupied else "EMPTY",
                "handling_unit_ids": sorted(occupied.get(buffer_id, set())),
            })
        return records

    def _complete_traffic_workflow(
        self,
        payload: dict,
        dataset: AffinityDataset,
        network: MovementNetwork,
        *,
        workflow_mode: str,
        initial_strategy: str,
        start_date=None,
        end_date=None,
        maximum_travel_increase: float | None = None,
        hotspot_percentile: float | None = None,
        baseline_path: str = "",
        source_orders: str = "",
        progress: ProgressCallback | None = None,
        cancelled: CancelCallback | None = None,
        progress_offset: int = 0,
        progress_total: int = 5,
        generation_summary: dict | None = None,
        optimize_traffic: bool = True,
    ) -> TrafficPipelineResult:
        def stage(current: int, message: str) -> None:
            if cancelled and cancelled():
                raise TrafficCancelledError("Traffic pipeline was cancelled")
            if progress:
                progress(progress_offset + current, progress_total, message)

        stage(1, "Validating hard storage compatibility")
        validation = self.validate_traffic_baseline(payload)
        stage(2, "Calculating Store ID + Date handling-unit visits")
        demand = self.build_demand(
            dataset, payload["assignments"], start_date, end_date
        )
        grouping_metrics = {
            "baseline_handling_unit_visits": demand.handling_unit_visits,
            "baseline_fulfillment_groups": demand.fulfillment_groups,
            "initial_strategy": initial_strategy,
            **validation,
        }
        payload = copy.deepcopy(payload)
        payload.setdefault("summary", {})["pipeline_grouping"] = grouping_metrics
        payload["buffers"] = self._buffer_records(payload, payload["assignments"])
        stage(3, "Routing grouped handling-unit demand")
        pretraffic_analysis = self.analyze(
            payload["assignments"], network, demand
        )
        optimization = output_payload = None
        if optimize_traffic:
            stage(4, "Optimizing complete handling-unit placement")
            optimization = self.optimize(
                payload, network, demand,
                maximum_travel_increase=maximum_travel_increase,
                hotspot_percentile=hotspot_percentile,
                progress=None,
                cancelled=cancelled,
            )
            output_payload = self.result_payload(
                payload, optimization,
                baseline_path=baseline_path,
                order_path=source_orders,
                network=network,
                workflow_mode=workflow_mode,
                initial_strategy=initial_strategy,
            )
            output_payload["pipeline"] = {
                "workflow_mode": workflow_mode,
                "initial_strategy": initial_strategy,
                "stages": [
                    "initial_layout_generation"
                    if workflow_mode == "full_pipeline"
                    else "existing_layout_load",
                    "hard_constraint_validation",
                    "handling_unit_visit_calculation",
                    "movement_resource_routing",
                    "traffic_aware_handling_unit_placement",
                    "final_validation_and_comparison",
                ],
                "grouping_metrics": grouping_metrics,
                "generation_summary": generation_summary or {},
            }
        stage(5, "Final traffic validation and comparison complete")
        basic_rows = payload["assignments"] if initial_strategy == "basic" else []
        affinity_rows = (
            payload["assignments"]
            if initial_strategy == "abc_affinity" else []
        )
        basic_summary = payload["summary"] if initial_strategy == "basic" else {}
        affinity_summary = (
            payload["summary"] if initial_strategy == "abc_affinity" else {}
        )
        return TrafficPipelineResult(
            basic_rows, basic_summary, affinity_rows, affinity_summary,
            demand, demand, grouping_metrics, payload, pretraffic_analysis,
            optimization, output_payload, workflow_mode, initial_strategy,
        )

    def run_existing_layout(
        self,
        payload: dict,
        dataset: AffinityDataset,
        network: MovementNetwork,
        *,
        start_date=None,
        end_date=None,
        maximum_travel_increase: float | None = None,
        hotspot_percentile: float | None = None,
        baseline_path: str = "",
        source_orders: str = "",
        progress: ProgressCallback | None = None,
        cancelled: CancelCallback | None = None,
    ) -> TrafficPipelineResult:
        """Optimize traffic without regenerating a supplied slotting layout."""
        baseline = copy.deepcopy(payload)
        strategy = str(baseline.get("strategy") or "basic")
        if strategy not in {"basic", "abc_affinity"}:
            strategy = (
                "abc_affinity"
                if baseline.get("affinity_configuration") else "basic"
            )
        return self._complete_traffic_workflow(
            baseline, dataset, network,
            workflow_mode="existing_layout",
            initial_strategy=strategy,
            start_date=start_date,
            end_date=end_date,
            maximum_travel_increase=maximum_travel_increase,
            hotspot_percentile=hotspot_percentile,
            baseline_path=baseline_path,
            source_orders=source_orders,
            progress=progress,
            cancelled=cancelled,
            progress_total=5,
        )

    def run_full_pipeline(
        self,
        building: dict,
        sku_rows: list[dict],
        affinity_source: AffinityAnalysis | AffinityDataset,
        network: MovementNetwork,
        *,
        initial_strategy: str = "abc_affinity",
        affinity_weight: float = 0.5,
        levels_per_rack: int = 1,
        slots_per_level: int = 6,
        handling_unit_type: str = "AMR shelf",
        zone_id: str = "Z01",
        zone_assignments: dict[str, str] | None = None,
        attribute_catalog=None,
        location_attributes: dict[str, dict] | None = None,
        storage_layout=None,
        start_date=None,
        end_date=None,
        optimize_traffic: bool = True,
        maximum_travel_increase: float | None = None,
        hotspot_percentile: float | None = None,
        source_grid_project: str = "",
        source_velocity: str = "",
        source_chilled: str = "",
        source_orders: str = "",
        progress: ProgressCallback | None = None,
        cancelled: CancelCallback | None = None,
    ) -> TrafficPipelineResult:
        """Generate the selected initial layout, then optimize its traffic."""
        if initial_strategy not in {"basic", "abc_affinity"}:
            raise ValueError("initial strategy must be basic or abc_affinity")
        affinity_analysis = (
            affinity_source
            if isinstance(affinity_source, AffinityAnalysis) else None
        )
        dataset = (
            affinity_source.dataset
            if affinity_analysis is not None else affinity_source
        )
        if initial_strategy == "abc_affinity" and affinity_analysis is None:
            raise ValueError("affinity strategy requires an affinity analysis")
        if cancelled and cancelled():
            raise TrafficCancelledError("Traffic pipeline was cancelled")
        if progress:
            progress(1, 7, "Loading warehouse configuration")

        catalog = attribute_catalog or self.attributes.starter_catalog()
        zones = dict(zone_assignments or {})
        configured_locations = copy.deepcopy(location_attributes or {})
        if not configured_locations:
            configured_locations[zone_id] = {
                "chilled": False,
                **STANDARD_STORAGE_DEFAULTS,
            }
        if progress:
            progress(
                2, 7,
                "Generating ABC layout"
                if initial_strategy == "basic"
                else "Generating ABC + affinity layout",
            )
        baseline_locations = copy.deepcopy(configured_locations)
        if initial_strategy == "basic":
            rows, summary = self.slotting.generate_basic(
                copy.deepcopy(building), copy.deepcopy(sku_rows),
                levels_per_rack, slots_per_level, handling_unit_type,
                zone_id, zones, catalog, baseline_locations,
                storage_layout=storage_layout,
                strict_compatibility=False,
                ergonomic_weight_heuristic=True,
                auto_plan_oversize=True,
            )
            affinity_configuration = {}
        else:
            rows, summary = self.slotting.generate_abc_affinity(
                copy.deepcopy(building), copy.deepcopy(sku_rows),
                affinity_analysis, affinity_weight,
                levels_per_rack, slots_per_level, handling_unit_type,
                zone_id, zones, catalog, baseline_locations,
                storage_layout=storage_layout,
                strict_compatibility=False,
                ergonomic_weight_heuristic=True,
                auto_plan_oversize=True,
            )
            affinity_configuration = summary.get("affinity_tuning", {})
        if summary.get("unassigned_count", 0):
            raise InsufficientStorageError(summary)

        storage_layout_payload = (
            storage_layout.to_dict() if storage_layout is not None else {}
        )
        payload = {
            "schema": SLOTTING_SCHEMA,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "strategy": initial_strategy,
            "handling_unit_type": handling_unit_type,
            "rack_capacity": {
                "levels": levels_per_rack,
                "slots_per_level": slots_per_level,
            },
            "sources": {
                "grid_project_json": source_grid_project,
                "sku_velocity_csv": source_velocity,
                "chilled_requirements_csv": source_chilled,
                "affinity_order_workbook": (
                    source_orders if initial_strategy == "abc_affinity" else ""
                ),
            },
            "affinity_configuration": affinity_configuration,
            "storage_defaults": {"standard": STANDARD_STORAGE_DEFAULTS},
            "zone_assignments": zones,
            "storage_layout": storage_layout_payload,
            "buffers": [],
            "attribute_catalog": self.attributes.serialize_catalog(catalog),
            "location_attributes": baseline_locations,
            "summary": summary,
            "building": copy.deepcopy(building),
            "assignments": rows,
            "operation_log": [],
        }
        payload["buffers"] = self._buffer_records(payload, rows)
        return self._complete_traffic_workflow(
            payload, dataset, network,
            workflow_mode="full_pipeline",
            initial_strategy=initial_strategy,
            start_date=start_date,
            end_date=end_date,
            maximum_travel_increase=maximum_travel_increase,
            hotspot_percentile=hotspot_percentile,
            baseline_path="generated_in_full_pipeline",
            source_orders=source_orders,
            progress=progress,
            cancelled=cancelled,
            progress_offset=2,
            progress_total=7,
            generation_summary=summary,
            optimize_traffic=optimize_traffic,
        )

    @staticmethod
    def result_payload(
        baseline_payload: dict,
        result: TrafficOptimizationResult,
        *,
        baseline_path: str,
        order_path: str,
        network: MovementNetwork,
        workflow_mode: str = "existing_layout",
        initial_strategy: str = "basic",
    ) -> dict:
        payload = copy.deepcopy(baseline_payload)
        payload["generated_at"] = datetime.now(timezone.utc).isoformat()
        payload["assignments"] = result.assignments
        payload.setdefault("sources", {})["traffic_baseline_layout"] = baseline_path
        payload["sources"]["traffic_order_workbook"] = order_path
        payload["sources"]["traffic_network"] = network.source_path or "embedded_rmf"
        payload["traffic_configuration"] = {
            **result.parameters,
            "workflow_mode": workflow_mode,
            "initial_strategy": initial_strategy,
            "network_type": network.source_type,
            "demand_model": "unique_handling_unit_per_store_day",
            "start_date": result.before.demand.start_date,
            "end_date": result.before.demand.end_date,
        }
        payload["traffic_analysis"] = {
            "before": result.before.metrics,
            "after": result.after.metrics,
            "fulfillment_groups": result.before.demand.fulfillment_groups,
            "handling_unit_visits": result.before.demand.handling_unit_visits,
            "unit_visits": dict(sorted(result.before.demand.unit_visits.items())),
            "mapped_units": len(result.before.mapped_units),
            "unreachable_units": list(result.before.unreachable_units),
            "unmapped_units": list(result.before.unmapped_units),
            "relocations": result.relocations,
            "rejected_units": result.rejected_units,
            "trials": result.trials,
        }
        payload.setdefault("operation_log", []).append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "operation": "traffic_aware_slotting",
            "relocation_count": len({row["handling_unit_id"] for row in result.relocations}),
        })
        payload["buffers"] = TrafficAwareSlottingService._buffer_records(
            payload, result.assignments
        )
        return payload

    @staticmethod
    def export(result: TrafficOptimizationResult, path: Path, network: MovementNetwork) -> tuple[Path, Path, Path]:
        destination = Path(path).expanduser()
        name = destination.name
        if name.endswith(".traffic.json"):
            stem = name[:-13]
        elif name.endswith(".json"):
            stem = name[:-5]
        else:
            stem = name
        json_path = destination.with_name(f"{stem}.traffic.json")
        resource_path = destination.with_name(f"{stem}_traffic_resources.csv")
        relocation_path = destination.with_name(f"{stem}_traffic_relocations.csv")
        json_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": TRAFFIC_EXPORT_SCHEMA,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "network": {"type": network.source_type, "source": network.source_path},
            "demand": {
                "fulfillment_groups": result.before.demand.fulfillment_groups,
                "handling_unit_visits": result.before.demand.handling_unit_visits,
                "start_date": result.before.demand.start_date,
                "end_date": result.before.demand.end_date,
                "unmatched_skus": list(result.before.demand.unmatched_skus),
            },
            "parameters": result.parameters,
            "before": {"metrics": result.before.metrics, "resources": result.before.resources},
            "after": {"metrics": result.after.metrics, "resources": result.after.resources},
            "relocations": result.relocations,
            "rejected_units": result.rejected_units,
            "trials": result.trials,
        }
        json_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        before = {row["resource_id"]: row for row in result.before.resources}
        after = {row["resource_id"]: row for row in result.after.resources}
        with resource_path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=(
                "resource_id", "capacity", "before_load", "after_load",
                "before_normalized_load", "after_normalized_load",
            ))
            writer.writeheader()
            for resource_id in sorted(set(before) | set(after)):
                first, second = before.get(resource_id, {}), after.get(resource_id, {})
                writer.writerow({
                    "resource_id": resource_id,
                    "capacity": first.get("capacity", second.get("capacity", "")),
                    "before_load": first.get("load", 0), "after_load": second.get("load", 0),
                    "before_normalized_load": first.get("normalized_load", 0),
                    "after_normalized_load": second.get("normalized_load", 0),
                })
        with relocation_path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=("handling_unit_id", "from", "to", "swap_with"))
            writer.writeheader()
            writer.writerows(result.relocations)
        return json_path, resource_path, relocation_path
