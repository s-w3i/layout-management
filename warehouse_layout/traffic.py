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

from .affinity import AffinityAnalysis, AffinityDataset, AffinityService
from .attributes import (
    PHYSICAL_ATTRIBUTE_KEYS,
    PHYSICAL_DIMENSION_KEYS,
    STANDARD_STORAGE_DEFAULTS,
    StorageAttributeService,
    requires_oversize_capable,
)
from .config import (
    LEGACY_PROJECT_SCHEMA,
    PROJECT_SCHEMA,
    SLOTTING_SCHEMA,
)
from .ctbsa import CtbsaParameters, CtbsaPlacementPlanner
from .domain import GridProject, StorageLayout
from .slotting import SlottingService
from .slotting_rules import overweight_storage_level


NETWORK_SCHEMA = "warehouse_movement_network/v1"
TRAFFIC_EXPORT_SCHEMA = "traffic_aware_slotting_analysis/v1"

ProgressCallback = Callable[[int, int, str], None]
CancelCallback = Callable[[], bool]
AssignmentCallback = Callable[[dict], None]


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
        sku_details = summary.get("unassigned_sku_details") or []
        sku_text = (
            "; " + " | ".join(str(value) for value in sku_details[:5])
            if sku_details else ""
        )
        super().__init__(
            f"{summary.get('unassigned_count', 0)} SKU(s) do not fit the configured "
            f"storage areas ({detail}){sku_text}"
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
    replica_visits: dict[str, dict[str, int]] = field(default_factory=dict)
    replica_assignment_policy: str = "traffic_balanced_alternative_source"


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
    zone_analysis: dict = field(default_factory=dict)


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
    """Run C&TBSA SKU clustering and static expected-flow analysis."""

    def __init__(
        self,
        attributes: StorageAttributeService | None = None,
        slotting: SlottingService | None = None,
        ctbsa_parameters: CtbsaParameters | None = None,
    ):
        self.attributes = attributes or StorageAttributeService()
        self.slotting = slotting or SlottingService(attributes=self.attributes)
        self.default_ctbsa_parameters = ctbsa_parameters or CtbsaParameters()

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

    @classmethod
    def build_demand(
        cls,
        dataset: AffinityDataset,
        assignments: list[dict],
        start_date=None,
        end_date=None,
        network: MovementNetwork | None = None,
    ) -> TrafficDemand:
        """Route each SKU event to one alternative inventory replica."""
        start = start_date or dataset.min_date
        end = end_date or dataset.max_date
        if start > end:
            raise ValueError("Start date must be on or before end date")
        sku_units: dict[str, dict[str, set[str]]] = {}
        replica_weights: dict[str, dict[str, float]] = {}
        primary_rows: dict[str, dict] = {}
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
                    try:
                        quantity = float(row.get("quantity_ea") or 1)
                    except (TypeError, ValueError):
                        quantity = 1.0
                    replica_weights.setdefault(sku, {})[primary] = (
                        replica_weights.setdefault(sku, {}).get(primary, 0.0)
                        + max(0.0, quantity)
                    )
                    primary_rows.setdefault(primary, row)
        mask = (dataset.dates >= start.toordinal()) & (dataset.dates <= end.toordinal())
        indices = np.flatnonzero(mask)
        if not len(indices):
            raise ValueError("No valid order events fall within the selected date range")
        groups: dict[tuple[int, int], dict[str, set[str]]] = {}
        unmatched: set[str] = set()
        matched_events = 0
        assigned_events: dict[str, dict[str, int]] = {
            sku: {primary: 0 for primary in units}
            for sku, units in sku_units.items()
        }
        replica_visits: dict[str, dict[str, int]] = {
            sku: {primary: 0 for primary in units}
            for sku, units in sku_units.items()
        }
        route_profiles: dict[str, tuple[float, dict[str, float]]] = {}
        projected_resources: dict[str, float] = (
            {resource: 0.0 for resource in network.resources}
            if network is not None else {}
        )
        if network is not None:
            route_cache = cls._shortest_routes(network)
            for primary, row in primary_rows.items():
                node = cls._resolve_node(row, network)
                reachable = [
                    endpoint for endpoint in network.endpoints
                    if node is not None and (node, endpoint.node_id) in route_cache
                ]
                if not reachable:
                    route_profiles[primary] = (math.inf, {})
                    continue
                total_weight = sum(endpoint.weight for endpoint in reachable)
                distance = 0.0
                resources: dict[str, float] = {}
                for endpoint in reachable:
                    share = endpoint.weight / total_weight
                    route_distance, _links, route_resources = route_cache[
                        (node, endpoint.node_id)
                    ]
                    distance += share * route_distance
                    for resource in route_resources:
                        resources[resource] = resources.get(resource, 0.0) + share
                route_profiles[primary] = (distance, resources)
        for event in indices:
            sku = dataset.skus[int(dataset.sku_indices[event])]
            unit_groups = sku_units.get(sku)
            if not unit_groups:
                unmatched.add(sku)
                continue
            key = (int(dataset.dates[event]), int(dataset.store_indices[event]))
            task_units = groups.setdefault(key, {})
            weights = replica_weights.get(sku, {})

            def replica_score(primary: str) -> tuple:
                weight = weights.get(primary, 0.0) or 1.0
                share_pressure = (
                    assigned_events[sku].get(primary, 0) + 1
                ) / weight
                distance, resources = route_profiles.get(primary, (0.0, {}))
                projected_peak = max(
                    (
                        projected_resources.get(resource, 0.0) + increment
                    ) / (
                        network.resource_capacities.get(resource, 1.0)
                        if network is not None
                        and network.resource_capacities.get(resource)
                        else 1.0
                    )
                    for resource, increment in resources.items()
                ) if resources else 0.0
                return (share_pressure, projected_peak, distance, primary)

            primary = min(sorted(unit_groups), key=replica_score)
            units = unit_groups[primary]
            task_units.setdefault(primary, set()).update(units)
            assigned_events[sku][primary] += 1
            replica_visits[sku][primary] += 1
            _distance, resources = route_profiles.get(primary, (0.0, {}))
            for resource, increment in resources.items():
                projected_resources[resource] = (
                    projected_resources.get(resource, 0.0) + increment
                )
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
            replica_visits={
                sku: dict(sorted(visits.items()))
                for sku, visits in sorted(replica_visits.items())
            },
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

    def rack_traffic_costs(
        self, building: dict, network: MovementNetwork
    ) -> dict[str, dict[str, float]]:
        """Return deterministic expected route cost for every mapped rack."""
        _level, racks, _workstations, _unreachable = self.slotting.rack_distances(
            building
        )
        routes = self._shortest_routes(network)
        result = {}
        for rack in racks:
            rack_id = str(rack["rack_id"])
            node = self._resolve_node(
                {"rack_id": rack_id, "static_bay_id": rack_id}, network
            )
            reachable = [
                endpoint for endpoint in network.endpoints
                if node is not None and (node, endpoint.node_id) in routes
            ]
            weight_total = sum(endpoint.weight for endpoint in reachable)
            if not reachable or weight_total <= 0:
                result[rack_id] = {
                    "resource_flow_per_visit": 0.0,
                    "travel_per_visit": float(rack.get("distance_m", 0.0)),
                }
                continue
            resource_flow = travel = 0.0
            for endpoint in reachable:
                share = endpoint.weight / weight_total
                distance, _links, resources = routes[(node, endpoint.node_id)]
                resource_flow += share * len(resources)
                travel += share * distance
            result[rack_id] = {
                "resource_flow_per_visit": resource_flow,
                "travel_per_visit": travel,
            }
        return result

    def analyze_zones(
        self, assignments: list[dict], analysis: TrafficAnalysis, payload: dict
    ) -> dict:
        """Aggregate demand and attributed route flow by destination rack zone."""
        capacity_per_rack = (
            int((payload.get("rack_capacity") or {}).get("levels") or 1)
            * int((payload.get("rack_capacity") or {}).get("slots_per_level") or 1)
        )
        _level, racks, _workstations, _unreachable = self.slotting.rack_distances(
            payload["building"]
        )
        self.slotting.apply_zone_local_aisles(
            payload["building"], racks, payload.get("zone_assignments") or {}, "Z01"
        )
        rack_zones = {
            str(rack["rack_id"]): str(rack.get("zone_id") or "Z01")
            for rack in racks
        }
        records: dict[str, dict] = {}
        for zone in sorted(set(rack_zones.values()) or {"Z01"}):
            rack_count = sum(value == zone for value in rack_zones.values())
            records[zone] = {
                "zone_id": zone,
                "usable_racks": rack_count,
                "usable_slots": rack_count * capacity_per_rack,
                "normalized_capacity": rack_count * capacity_per_rack,
                "expected_visits": 0.0,
                "attributed_resource_flow": 0.0,
                "occupied_racks": set(),
                "inventory_load_ids": set(),
                "skus": set(),
                "quantity_ea": 0.0,
            }
        unit_zone: dict[str, str] = {}
        counted_loads = set()
        for row in assignments:
            if row.get("assignment_status") != "ASSIGNED":
                continue
            rack_id = str(row.get("rack_id") or row.get("static_bay_id") or "")
            zone = rack_zones.get(
                rack_id,
                str((payload.get("zone_assignments") or {}).get(rack_id) or "Z01"),
            )
            record = records.setdefault(zone, {
                "zone_id": zone, "usable_racks": 0, "usable_slots": 0,
                "normalized_capacity": 0, "expected_visits": 0.0,
                "attributed_resource_flow": 0.0, "occupied_racks": set(),
                "inventory_load_ids": set(), "skus": set(), "quantity_ea": 0.0,
            })
            unit = str(row.get("handling_unit_id") or "")
            if unit:
                unit_zone[unit] = zone
            record["occupied_racks"].add(rack_id)
            load_id = str(row.get("inventory_load_id", row.get("sku", "")))
            record["inventory_load_ids"].add(load_id)
            record["skus"].add(str(row.get("sku") or ""))
            if load_id not in counted_loads:
                try:
                    record["quantity_ea"] += float(row.get("quantity_ea") or 0.0)
                except (TypeError, ValueError):
                    pass
                counted_loads.add(load_id)
        for unit, visits in analysis.demand.unit_visits.items():
            zone = unit_zone.get(str(unit))
            if zone in records:
                records[zone]["expected_visits"] += float(visits)
        for unit, route in analysis.unit_routes.items():
            zone = unit_zone.get(str(unit))
            if zone in records:
                records[zone]["attributed_resource_flow"] += sum(
                    float(value) for value in route.get("resource_flows", {}).values()
                )
        rows = []
        for zone in sorted(records):
            record = records[zone]
            capacity = float(record["normalized_capacity"])
            record["normalized_demand_workload"] = (
                record["expected_visits"] / capacity if capacity > 0 else 0.0
            )
            record["normalized_traffic_workload"] = (
                record["attributed_resource_flow"] / capacity if capacity > 0 else 0.0
            )
            for key in ("occupied_racks", "inventory_load_ids", "skus"):
                values = record[key]
                record[key.replace("inventory_load_ids", "inventory_load_count").replace("occupied_racks", "occupied_rack_count").replace("skus", "sku_count")] = len(values)
                del record[key]
            rows.append(record)
        demand_values = np.array([
            row["normalized_demand_workload"] for row in rows
            if row["normalized_capacity"] > 0
        ])
        traffic_values = np.array([
            row["normalized_traffic_workload"] for row in rows
            if row["normalized_capacity"] > 0
        ])
        most_loaded = max(
            rows,
            key=lambda row: (
                row["normalized_demand_workload"],
                row["normalized_traffic_workload"],
                row["zone_id"],
            ),
            default={"zone_id": ""},
        )
        return {
            "normalization": "compatible_usable_rack_slot_capacity",
            "traffic_attribution": "destination_rack_zone",
            "metrics": {
                "peak_normalized_zone_demand": float(demand_values.max()) if len(demand_values) else 0.0,
                "p95_normalized_zone_demand": float(np.percentile(demand_values, 95)) if len(demand_values) else 0.0,
                "peak_normalized_zone_traffic": float(traffic_values.max()) if len(traffic_values) else 0.0,
                "p95_normalized_zone_traffic": float(np.percentile(traffic_values, 95)) if len(traffic_values) else 0.0,
                "expected_travel": float(analysis.metrics.get("expected_travel", 0.0)),
                "most_loaded_zone": most_loaded.get("zone_id", ""),
            },
            "zones": rows,
        }

    def restore_saved_result(
        self, payload: dict, network: MovementNetwork
    ) -> TrafficOptimizationResult:
        """Rebuild final expected traffic from a saved traffic-aware layout."""
        traffic = payload.get("traffic_analysis") or {}
        configuration = copy.deepcopy(payload.get("traffic_configuration") or {})
        if not traffic or not configuration:
            raise ValueError(
                "saved layout has no traffic analysis/configuration to restore"
            )
        assignments = copy.deepcopy(payload.get("assignments") or [])
        if not assignments:
            raise ValueError("saved traffic-aware layout has no assignments")
        unit_visits = {
            str(unit): int(visits)
            for unit, visits in (
                traffic.get("after_unit_visits") or traffic.get("unit_visits") or {}
            ).items()
        }
        demand = TrafficDemand(
            unit_visits=unit_visits,
            fulfillment_groups=int(traffic.get("fulfillment_groups") or 0),
            handling_unit_visits=int(
                traffic.get("after_handling_unit_visits")
                or sum(unit_visits.values())
            ),
            matched_events=0,
            unmatched_skus=tuple(str(value) for value in traffic.get("unmatched_skus", [])),
            start_date=str(configuration.get("start_date") or ""),
            end_date=str(configuration.get("end_date") or ""),
            replica_visits=copy.deepcopy(
                traffic.get("after_replica_visits")
                or traffic.get("replica_visits") or {}
            ),
            replica_assignment_policy=str(
                traffic.get("replica_assignment_policy")
                or "traffic_balanced_alternative_source"
            ),
        )
        saved_after_metrics = traffic.get("after") or {}
        analysis = self.analyze(
            assignments,
            network,
            demand,
            relative_reference=(
                float(saved_after_metrics["relative_reference"])
                if saved_after_metrics.get("relative_reference") is not None
                else None
            ),
        )
        saved_zone = (traffic.get("zone_analysis") or {}).get("after")
        analysis.zone_analysis = (
            copy.deepcopy(saved_zone)
            if saved_zone else self.analyze_zones(assignments, analysis, payload)
        )
        parameters = {
            **configuration,
            "restored_saved_run": True,
            "restored_resource_view": "rerouted_saved_final_unit_visits",
        }
        return TrafficOptimizationResult(
            assignments=assignments,
            before=analysis,
            after=analysis,
            relocations=copy.deepcopy(traffic.get("relocations") or []),
            rejected_units=copy.deepcopy(traffic.get("rejected_units") or []),
            parameters=parameters,
            trials=copy.deepcopy(
                traffic.get("trials")
                or configuration.get("pareto_frontier") or []
            ),
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
        configured_map_attribute_keys = (
            self.attributes.configured_zone_attribute_keys(local)
        )
        if not self.attributes.has_physical_catalog(catalog):
            return False, "physical capacity definitions are unavailable"
        for row in source_rows:
            requirements = row.get("sku_requirements") or {}
            missing = [key for key in PHYSICAL_ATTRIBUTE_KEYS if key not in requirements]
            if missing:
                return False, f"SKU {row.get('sku', '')} has incomplete physical requirements"
            profile = self.attributes.physical_profile(requirements)
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
                    and key in configured_map_attribute_keys
                }
                issues = self.attributes.compatibility_issues(
                    generic_requirements, effective, catalog
                )
                if (
                    requires_oversize_capable(profile)
                    and effective.get("oversize_capable") is not True
                ):
                    issues.append(
                        "oversize or overweight inventory requires an "
                        "oversize-capable zone"
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


    def optimize_ctbsa(
        self,
        payload: dict,
        analysis: AffinityAnalysis,
        network: MovementNetwork,
        *,
        parameters: CtbsaParameters | None = None,
        zone_workload_enabled: bool = False,
        progress: ProgressCallback | None = None,
        cancelled: CancelCallback | None = None,
    ) -> TrafficOptimizationResult:
        """Run the paper's two-stage C&TBSA method."""
        baseline_rows = copy.deepcopy(payload.get("assignments") or [])
        if str(payload.get("handling_unit_type", "")) != "AMR shelf":
            raise ValueError(
                "paper-replication C&TBSA currently requires AMR shelf storage"
            )
        rack_capacity = payload.get("rack_capacity") or {}
        levels = int(rack_capacity.get("levels") or 1)
        slots = int(rack_capacity.get("slots_per_level") or 1)
        parameters = parameters or self.default_ctbsa_parameters
        planner = CtbsaPlacementPlanner(self.slotting)
        plan = planner.build(
            baseline_rows,
            payload["building"],
            analysis,
            levels_per_rack=levels,
            slots_per_level=slots,
            zone_assignments=payload.get("zone_assignments") or {},
            location_attributes=payload.get("location_attributes") or {},
            attribute_catalog=payload.get("attribute_catalog"),
            zone_workload_enabled=zone_workload_enabled,
            rack_traffic_costs=(
                self.rack_traffic_costs(payload["building"], network)
                if zone_workload_enabled else None
            ),
            parameters=parameters,
            progress=progress,
            cancelled=cancelled,
        )
        optimized_loads = set(plan.optimized_loads)
        source_rows = [
            copy.deepcopy(row) for row in baseline_rows
            if (
                row.get("assignment_status") == "ASSIGNED"
                and str(row.get("inventory_load_id", row.get("sku", "")))
                in optimized_loads
            )
        ]
        storage_layout = (
            StorageLayout.from_dict(payload["storage_layout"])
            if payload.get("storage_layout") else None
        )
        regenerated, regenerated_summary = self.slotting.generate_basic(
            copy.deepcopy(payload["building"]),
            source_rows,
            levels,
            slots,
            "AMR shelf",
            zone_assignments=payload.get("zone_assignments") or {},
            attribute_catalog=payload.get("attribute_catalog"),
            location_attributes=copy.deepcopy(
                payload.get("location_attributes") or {}
            ),
            strategy="ctbsa",
            strict_compatibility=False,
            storage_layout=storage_layout,
            # Paper Stage 2 randomly orders SKUs within the selected area;
            # ergonomic level preferences would alter that assignment rule.
            ergonomic_weight_heuristic=False,
            auto_plan_oversize=True,
            ctbsa_target_racks=plan.target_racks,
            ctbsa_rank_by_sku=plan.rank_by_sku,
        )
        failed = [
            row for row in regenerated
            if row.get("assignment_status") != "ASSIGNED"
        ]
        if failed:
            sample = ", ".join(str(row.get("sku", "")) for row in failed[:5])
            raise ValueError(
                f"C&TBSA Stage 2 could not place {len(failed)} SKU(s) in their "
                f"assigned storage areas: {sample}"
            )
        retained_fixed = [
            copy.deepcopy(row) for row in baseline_rows
            if (
                row.get("assignment_status") != "ASSIGNED"
                or str(row.get("inventory_load_id", row.get("sku", "")))
                not in optimized_loads
            )
        ]
        result_rows = regenerated + retained_fixed
        optimized_units = {
            str(row.get("handling_unit_id", ""))
            for row in regenerated
            if row.get("assignment_status") == "ASSIGNED"
        }
        self.validate_result(
            baseline_rows, result_rows, payload, optimized_units
        )
        # Audit the complete final layout, including physical-exception shelves
        # that C&TBSA deliberately keeps fixed.  Previously only movable units
        # passed through the post-optimization compatibility check.
        validation_payload = copy.deepcopy(payload)
        validation_payload["assignments"] = result_rows
        final_hard_rule_validation = self.validate_traffic_baseline(
            validation_payload
        )
        for row in result_rows:
            physical_class = str(
                row.get("physical_storage_class") or "NOT_EVALUATED"
            ).upper()
            missing_data = str(
                row.get("physical_missing_data_type") or ""
            ).upper()
            labels = []
            if (row.get("sku_requirements") or {}).get("chilled") is True:
                labels.append("CHILLED")
            labels.append(missing_data or physical_class)
            if (
                physical_class in {"OVERSIZE", "OVERSIZE_AND_OVERWEIGHT"}
                or int(row.get("occupied_slot_count") or 1) > 1
            ):
                labels.append("CONTIGUOUS_FOOTPRINT")
            if physical_class in {"OVERWEIGHT", "OVERSIZE_AND_OVERWEIGHT"}:
                labels.append(
                    f"REQUIRED_LEVEL_{overweight_storage_level(levels)}"
                )
            row["hard_rule_labels"] = list(dict.fromkeys(labels))
            row["hard_rule_status"] = (
                "PASSED" if row.get("assignment_status") == "ASSIGNED"
                else "EXCLUDED_UNASSIGNED"
            )
        before_demand = self.build_demand(
            analysis.dataset, baseline_rows,
            analysis.start_date, analysis.end_date, network,
        )
        after_demand = self.build_demand(
            analysis.dataset, result_rows,
            analysis.start_date, analysis.end_date, network,
        )
        route_cache = self._shortest_routes(network)
        before = self.analyze(
            baseline_rows, network, before_demand, route_cache=route_cache
        )
        after = self.analyze(
            result_rows,
            network,
            after_demand,
            route_cache=route_cache,
            relative_reference=float(before.metrics["relative_reference"]),
        )
        before.zone_analysis = self.analyze_zones(baseline_rows, before, payload)
        after.zone_analysis = self.analyze_zones(result_rows, after, payload)
        compact_rack_count = len({
            str(row.get("rack_id", "")) for row in baseline_rows
            if row.get("assignment_status") == "ASSIGNED" and row.get("rack_id")
        })
        candidate_rack_count = len({
            str(row.get("rack_id", "")) for row in result_rows
            if row.get("assignment_status") == "ASSIGNED" and row.get("rack_id")
        })

        candidate_metrics = dict(after.metrics)
        candidate_replica_visits = copy.deepcopy(after.demand.replica_visits)
        rack_budget_trials = [
            {
                "rack_budget": compact_rack_count,
                "source": "compact_feasible_baseline",
                "selected": False,
                **{
                    key: before.metrics.get(key)
                    for key in ("peak_load", "p95_load", "expected_travel")
                },
            },
            {
                "rack_budget": candidate_rack_count,
                "source": "paper_ctbsa_selected_representative",
                "selected": True,
                **{
                    key: candidate_metrics.get(key)
                    for key in ("peak_load", "p95_load", "expected_travel")
                },
            },
        ]
        old_by_load = {
            str(row.get("inventory_load_id", row.get("sku", ""))): row
            for row in baseline_rows
            if row.get("assignment_status") == "ASSIGNED"
        }
        new_by_load = {
            str(row.get("inventory_load_id", row.get("sku", ""))): row
            for row in result_rows
            if row.get("assignment_status") == "ASSIGNED"
        }
        relocations = []
        for load_id in sorted(set(old_by_load) & set(new_by_load)):
            old, new = old_by_load[load_id], new_by_load[load_id]
            if (
                str(old.get("rack_id", "")) == str(new.get("rack_id", ""))
                and int(old.get("storage_level") or 1)
                == int(new.get("storage_level") or 1)
                and int(old.get("storage_slot") or 1)
                == int(new.get("storage_slot") or 1)
            ):
                continue
            relocations.append({
                "sku": str(new.get("sku", "")),
                "inventory_load_id": load_id,
                "handling_unit_id": str(new.get("handling_unit_id", "")),
                "from_rack": str(old.get("rack_id", "")),
                "to_rack": str(new.get("rack_id", "")),
                "from": str(old.get("storage_location_address") or old.get("static_address", "")),
                "to": str(new.get("storage_location_address") or new.get("static_address", "")),
            })
        result_parameters = {
            **plan.parameters,
            "optimized_sku_count": len(plan.optimized_skus),
            "fixed_sku_count": len(plan.fixed_skus),
            "optimized_load_count": len(plan.optimized_loads),
            "fixed_load_count": len(plan.fixed_loads),
            "hard_rule_profile": regenerated_summary.get(
                "hard_rule_profile", "shared_warehouse_feasibility/v1"
            ),
            "hard_rules": regenerated_summary.get("hard_rules", []),
            "hard_rule_validation": final_hard_rule_validation,
            "paper_reference": "10.1016/j.cie.2019.106129",
            "validation_mode": "static_expected_flow_only",
            "regenerated_summary": regenerated_summary,
            "clusters": plan.cluster_rows,
            "rack_budget_policy": (
                "extended_ctbsa_selected_solution"
                if zone_workload_enabled else "paper_ctbsa_selected_solution"
            ),
            "compact_rack_count": compact_rack_count,
            "selected_rack_count": candidate_rack_count,
            "rack_budget_trials": rack_budget_trials,
            "rack_budget_selection_reason": (
                "selected C&TBSA Pareto representative applied without a "
                "post-simulation route-metric veto"
            ),
            "route_metrics_role": (
                "within_selected_zone_assignment_then_post_evaluation"
                if zone_workload_enabled else "post_assignment_evaluation_only"
            ),
            "candidate_replica_visit_allocation": candidate_replica_visits,
            "selected_replica_visit_allocation": copy.deepcopy(
                after.demand.replica_visits
            ),
            "zone_workload_enabled": bool(zone_workload_enabled),
            "zone_analysis_before": copy.deepcopy(before.zone_analysis),
            "zone_analysis_after": copy.deepcopy(after.zone_analysis),
        }
        baseline_by_sku = {
            str(row.get("sku", "")): row for row in baseline_rows
        }
        rejected = []
        for sku in plan.fixed_skus:
            source = baseline_by_sku.get(str(sku), {})
            physical_class = str(
                source.get("physical_storage_class") or "NOT_EVALUATED"
            ).upper()
            missing_data = str(
                source.get("physical_missing_data_type") or ""
            ).upper()
            status = str(source.get("assignment_status") or "UNKNOWN")
            occupied = int(source.get("occupied_slot_count") or 1)
            if missing_data:
                display_class = missing_data
                reason = (
                    f"{missing_data} · incomplete physical data; fixed outside "
                    "C&TBSA movement"
                )
            elif physical_class == "OVERSIZE":
                display_class = "OVERSIZE"
                reason = (
                    f"OVERSIZE · fixed contiguous footprint ({occupied} slots)"
                )
            elif physical_class == "OVERWEIGHT":
                display_class = "OVERWEIGHT"
                reason = "OVERWEIGHT · fixed at its hard-rule storage level"
            elif physical_class == "OVERSIZE_AND_OVERWEIGHT":
                display_class = "OVERSIZE + OVERWEIGHT"
                reason = (
                    f"OVERSIZE + OVERWEIGHT · fixed footprint ({occupied} slots) "
                    "and required level"
                )
            else:
                display_class = physical_class
                reason = "fixed because its shelf contains a physical exception"
            if status != "ASSIGNED":
                reason = f"{reason} · {status}"
            occupied_address = str(
                source.get("occupied_dynamic_address")
                or source.get("storage_location_address")
                or source.get("static_address")
                or ""
            )
            rejected.append({
                # Keep this compatibility field as the SKU because older
                # exported traffic reports used it as their row identifier.
                "handling_unit_id": str(sku),
                "sku": str(sku),
                "shelf_id": str(source.get("handling_unit_id") or ""),
                "physical_storage_class": display_class,
                "physical_missing_data_type": missing_data,
                "assignment_status": status,
                "location": occupied_address,
                "reason": reason,
            })
        return TrafficOptimizationResult(
            result_rows, before, after, relocations, rejected,
            result_parameters, plan.pareto_rows,
        )

    def optimize(
        self,
        payload: dict,
        analysis: AffinityAnalysis,
        network: MovementNetwork,
        *,
        parameters: CtbsaParameters | None = None,
        zone_workload_enabled: bool = False,
        progress: ProgressCallback | None = None,
        cancelled: CancelCallback | None = None,
    ) -> TrafficOptimizationResult:
        """Run the replacement paper-replication C&TBSA optimizer."""
        return self.optimize_ctbsa(
            payload,
            analysis,
            network,
            parameters=parameters,
            zone_workload_enabled=zone_workload_enabled,
            progress=progress,
            cancelled=cancelled,
        )

    def validate_result(
        self,
        original: list[dict],
        result: list[dict],
        payload: dict,
        moved_units: set[str] | None = None,
    ) -> None:
        if len(original) != len(result):
            raise ValueError("traffic result changed the assignment row count")
        identity = lambda row: str(
            row.get("inventory_load_id", row.get("sku", ""))
        )
        if sorted(map(identity, original)) != sorted(map(identity, result)):
            raise ValueError("C&TBSA result changed the SKU membership")
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
        """Validate assigned inventory and report unassigned rows as exclusions."""
        rows = payload.get("assignments", [])
        unassigned = [
            row for row in rows if row.get("assignment_status") != "ASSIGNED"
        ]
        unassigned_counts: dict[str, int] = {}
        unassigned_details = []
        if unassigned:
            for row in unassigned:
                status = str(row.get("assignment_status", "UNASSIGNED"))
                unassigned_counts[status] = (
                    unassigned_counts.get(status, 0) + 1
                )
            unassigned_details = [
                (
                    f"{row.get('sku', '')}: "
                    f"{row.get('assignment_status', 'UNASSIGNED')}"
                    + (
                        " · " + "; ".join(
                            str(issue)
                            for issue in row.get("compatibility_issues", [])
                        )
                        if row.get("compatibility_issues") else ""
                    )
                )
                for row in unassigned
            ]
        if not rows:
            raise ValueError("slotting layout contains no assignments")
        assigned = [
            row for row in rows if row.get("assignment_status") == "ASSIGNED"
        ]
        if not assigned:
            raise ValueError(
                "slotting layout contains no assigned inventory to optimize"
            )

        levels_per_rack = int(
            payload.get("rack_capacity", {}).get("levels") or 1
        )
        catalog = self.attributes.normalize_catalog(
            payload.get("attribute_catalog")
        )
        locations = payload.get("location_attributes", {})
        configured_map_attribute_keys = (
            self.attributes.configured_zone_attribute_keys(locations)
        )
        invalid = []
        unverified = 0
        for row in assigned:
            requirements = row.get("sku_requirements") or {}
            profile = self.attributes.physical_profile(requirements)
            if profile["data_status"] != "COMPLETE":
                unverified += 1
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
                configured_requirements = {
                    key: value for key, value in generic_requirements.items()
                    if key in configured_map_attribute_keys
                }
                issues.extend(self.attributes.compatibility_issues(
                    configured_requirements, effective, catalog
                ))
                if (
                    requires_oversize_capable(profile)
                    and effective.get("oversize_capable") is not True
                ):
                    issues.append(
                        "oversize or overweight inventory requires an "
                        "oversize-capable zone"
                    )
            level_span = int(row.get("occupied_level_span") or 1)
            slot_span = int(
                row.get("occupied_horizontal_slot_span")
                or len(occupied_addresses)
            )
            if (
                profile["data_status"] == "COMPLETE"
                and self.slotting.required_slot_footprint(
                    requirements,
                    primary_effective or {},
                    level_span,
                    slot_span,
                ) is None
            ):
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
            "hard_validation_status": (
                "PASSED_WITH_UNASSIGNED_EXCLUSIONS"
                if unassigned else "PASSED"
            ),
            "unverified_physical_sku_count": unverified,
            "assigned_sku_count": len(assigned),
            "excluded_unassigned_sku_count": len(unassigned),
            "excluded_unassigned_status_counts": unassigned_counts,
            "excluded_unassigned_sku_details": unassigned_details,
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
        affinity_source: AffinityAnalysis | AffinityDataset,
        network: MovementNetwork,
        *,
        workflow_mode: str,
        initial_strategy: str,
        start_date=None,
        end_date=None,
        baseline_path: str = "",
        source_orders: str = "",
        progress: ProgressCallback | None = None,
        cancelled: CancelCallback | None = None,
        progress_offset: int = 0,
        progress_total: int = 5,
        generation_summary: dict | None = None,
        optimize_traffic: bool = True,
        ctbsa_parameters: CtbsaParameters | None = None,
        zone_workload_enabled: bool = False,
    ) -> TrafficPipelineResult:
        def stage(current: int, message: str) -> None:
            if cancelled and cancelled():
                raise TrafficCancelledError("Traffic pipeline was cancelled")
            if progress:
                progress(progress_offset + current, progress_total, message)

        analysis_source = (
            affinity_source
            if isinstance(affinity_source, AffinityAnalysis)
            else AffinityService.analyze(
                affinity_source, start_date, end_date
            )
        )
        dataset = analysis_source.dataset
        stage(1, "Validating hard storage compatibility")
        validation = self.validate_traffic_baseline(payload)
        stage(2, "Calculating Store ID + Date handling-unit visits")
        demand = self.build_demand(
            dataset, payload["assignments"], start_date, end_date, network
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
        pretraffic_analysis.zone_analysis = self.analyze_zones(
            payload["assignments"], pretraffic_analysis, payload
        )
        optimization = output_payload = None
        if optimize_traffic:
            stage(4, "Running paper-replication C&TBSA clustering and assignment")
            optimization = self.optimize_ctbsa(
                payload, analysis_source, network,
                parameters=ctbsa_parameters,
                zone_workload_enabled=zone_workload_enabled,
                progress=progress,
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
                    (
                        "physical_feasibility_seed"
                        if initial_strategy == "physical_feasibility"
                        else "initial_layout_generation"
                        if workflow_mode == "full_pipeline"
                        else "existing_layout_load"
                    ),
                    "hard_constraint_validation",
                    "handling_unit_visit_calculation",
                    "movement_resource_routing",
                    "ctbsa_nsga2_sku_clustering",
                    "ctbsa_demand_ranked_storage_area_assignment",
                    (
                        "ctbsa_zone_workload_rack_assignment"
                        if zone_workload_enabled
                        else "ctbsa_zone_workload_disabled"
                    ),
                    "final_validation_and_comparison",
                ],
                "grouping_metrics": grouping_metrics,
                "generation_summary": generation_summary or {},
            }
        stage(5, "Final traffic validation and comparison complete")
        basic_rows = (
            payload["assignments"]
            if initial_strategy in {"basic", "physical_feasibility"} else []
        )
        affinity_rows = (
            payload["assignments"]
            if initial_strategy == "abc_affinity" else []
        )
        basic_summary = (
            payload["summary"]
            if initial_strategy in {"basic", "physical_feasibility"} else {}
        )
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
        ctbsa_parameters: CtbsaParameters | None = None,
        zone_workload_enabled: bool = False,
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
            baseline_path=baseline_path,
            source_orders=source_orders,
            progress=progress,
            cancelled=cancelled,
            progress_total=5,
            ctbsa_parameters=ctbsa_parameters,
            zone_workload_enabled=zone_workload_enabled,
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
        ctbsa_parameters: CtbsaParameters | None = None,
        zone_workload_enabled: bool = False,
        source_grid_project: str = "",
        source_velocity: str = "",
        source_chilled: str = "",
        source_orders: str = "",
        workflow_mode: str = "full_pipeline",
        progress: ProgressCallback | None = None,
        cancelled: CancelCallback | None = None,
        assignment_progress: AssignmentCallback | None = None,
    ) -> TrafficPipelineResult:
        """Build a feasibility seed, then run the paper C&TBSA method."""
        if initial_strategy not in {
            "physical_feasibility", "basic", "abc_affinity"
        }:
            raise ValueError(
                "initial strategy must be physical_feasibility, basic, or "
                "abc_affinity"
            )
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

        catalog = self.attributes.normalize_catalog(attribute_catalog)
        zones = dict(zone_assignments or {})
        configured_locations = copy.deepcopy(location_attributes or {})
        if progress:
            progress(
                2, 7,
                "Building physical-feasibility seed"
                if initial_strategy == "physical_feasibility"
                else "Generating ABC layout"
                if initial_strategy == "basic"
                else "Generating ABC + affinity layout",
            )
        baseline_locations = copy.deepcopy(configured_locations)
        if initial_strategy in {"physical_feasibility", "basic"}:
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
        zones = dict(summary.get("zone_assignments", zones))
        if assignment_progress:
            assignment_progress(copy.deepcopy(summary))
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
                "sku_attributes_csv": source_chilled,
                "ctbsa_order_workbook": source_orders,
            },
            "affinity_configuration": affinity_configuration,
            "feasibility_seed_only": initial_strategy == "physical_feasibility",
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
            workflow_mode=workflow_mode,
            initial_strategy=initial_strategy,
            start_date=start_date,
            end_date=end_date,
            baseline_path=(
                "generated_physical_feasibility_seed"
                if initial_strategy == "physical_feasibility"
                else "generated_in_full_pipeline"
            ),
            source_orders=source_orders,
            progress=progress,
            cancelled=cancelled,
            progress_offset=2,
            progress_total=7,
            generation_summary=summary,
            optimize_traffic=optimize_traffic,
            ctbsa_parameters=ctbsa_parameters,
            zone_workload_enabled=zone_workload_enabled,
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
        payload["strategy"] = "ctbsa"
        payload["assignments"] = result.assignments
        final_summary = copy.deepcopy(payload.get("summary") or {})
        final_summary.update({
            "strategy": "ctbsa",
            "hard_rule_profile": result.parameters.get(
                "hard_rule_profile", "shared_warehouse_feasibility/v1"
            ),
            "hard_rules": result.parameters.get("hard_rules", []),
            "hard_rule_validation": result.parameters.get(
                "hard_rule_validation", {}
            ),
            "optimized_sku_count": result.parameters.get(
                "optimized_sku_count", 0
            ),
            "fixed_sku_count": result.parameters.get("fixed_sku_count", 0),
            "optimized_load_count": result.parameters.get("optimized_load_count", 0),
            "fixed_load_count": result.parameters.get("fixed_load_count", 0),
            "compact_rack_count": result.parameters.get("compact_rack_count", 0),
            "final_occupied_rack_count": result.parameters.get("selected_rack_count", 0),
        })
        payload["summary"] = final_summary
        payload.setdefault("sources", {})["traffic_baseline_layout"] = baseline_path
        payload["sources"]["traffic_order_workbook"] = order_path
        payload["sources"]["traffic_network"] = network.source_path or "embedded_rmf"
        payload["traffic_configuration"] = {
            **result.parameters,
            "workflow_mode": workflow_mode,
            "initial_strategy": initial_strategy,
            "optimization_strategy": "ctbsa",
            "network_type": network.source_type,
            "demand_model": "traffic_balanced_alternative_sku_replica",
            "start_date": result.before.demand.start_date,
            "end_date": result.before.demand.end_date,
        }
        payload["traffic_analysis"] = {
            "before": result.before.metrics,
            "after": result.after.metrics,
            "zone_workload_enabled": bool(
                result.parameters.get("zone_workload_enabled", False)
            ),
            "zone_analysis": {
                "before": result.before.zone_analysis,
                "after": result.after.zone_analysis,
            },
            "fulfillment_groups": result.before.demand.fulfillment_groups,
            "handling_unit_visits": result.before.demand.handling_unit_visits,
            "unit_visits": dict(sorted(result.before.demand.unit_visits.items())),
            "after_handling_unit_visits": result.after.demand.handling_unit_visits,
            "after_unit_visits": dict(sorted(result.after.demand.unit_visits.items())),
            "replica_assignment_policy": result.before.demand.replica_assignment_policy,
            "replica_visits": result.before.demand.replica_visits,
            "after_replica_visits": result.after.demand.replica_visits,
            "mapped_units": len(result.before.mapped_units),
            "unreachable_units": list(result.before.unreachable_units),
            "unmapped_units": list(result.before.unmapped_units),
            "relocations": result.relocations,
            "rejected_units": result.rejected_units,
            "trials": result.trials,
        }
        payload.setdefault("operation_log", []).append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "operation": "ctbsa_slotting",
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
                "replica_assignment_policy": result.before.demand.replica_assignment_policy,
                "replica_visits": result.before.demand.replica_visits,
            },
            "parameters": result.parameters,
            "before": {"metrics": result.before.metrics, "resources": result.before.resources},
            "after": {"metrics": result.after.metrics, "resources": result.after.resources},
            "zone_analysis": {
                "before": result.before.zone_analysis,
                "after": result.after.zone_analysis,
            },
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
            writer = csv.DictWriter(
                stream,
                fieldnames=(
                    "sku", "handling_unit_id", "from_rack", "to_rack",
                    "from", "to",
                ),
                extrasaction="ignore",
            )
            writer.writeheader()
            writer.writerows(result.relocations)
        return json_path, resource_path, relocation_path
