"""Exact/bounded global congestion-balanced handling-unit assignment."""

from __future__ import annotations

import copy
import heapq
import math
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from .affinity import AffinityAnalysis, AffinityDataset
from .attributes import (
    OVERSIZE_CAPABLE_KEY,
    PHYSICAL_DIMENSION_KEYS,
    PHYSICAL_WEIGHT_KEY,
    requires_oversize_capable,
)
from .traffic import (
    MovementNetwork,
    TrafficAnalysis,
    TrafficAwareSlottingService,
    TrafficDemand,
)
from .storage_planning import combined_occupied_dynamic_address

try:
    from ortools.sat.python import cp_model
except ImportError:  # pragma: no cover - exercised by deployment environments.
    cp_model = None


ProgressCallback = Callable[[int, int, str], None]
CancelCallback = Callable[[], bool]

UTILIZATION_SCALE = 100_000
TRAVEL_SCALE = 1_000
CVaR_TAIL_MULTIPLIER = 20  # 1 / (1 - 0.95)


class GlobalTrafficCancelledError(RuntimeError):
    """Raised when a global optimization run is cancelled."""


class GlobalTrafficOptimizationError(RuntimeError):
    """Raised when no valid global assignment can be produced."""


@dataclass(slots=True)
class GlobalTrafficResult:
    """Complete result of the independent global traffic pipeline."""

    baseline_payload: dict
    assignments: list[dict]
    demand: TrafficDemand
    before: TrafficAnalysis
    after: TrafficAnalysis
    relocations: list[dict]
    solver: dict
    balance_metrics: dict
    output_payload: dict
    workflow_mode: str
    initial_strategy: str


@dataclass(slots=True)
class _CandidateSpace:
    candidates: dict[str, list[str]]
    nodes: dict[str, str | None]
    zones: dict[str, str]
    neighbourhoods: dict[str, str]
    templates: dict[tuple[str, str], list[dict]]
    location_buffers: dict[str, tuple[str, ...]]
    original_locations: set[str]
    empty_locations: set[str]


class _CancellationCallback(cp_model.CpSolverSolutionCallback if cp_model else object):
    def __init__(self, cancelled: CancelCallback | None):
        if cp_model:
            super().__init__()
        self.cancelled = cancelled

    def on_solution_callback(self):
        if self.cancelled and self.cancelled():
            self.StopSearch()


class GlobalTrafficSlottingService:
    """Globally assign complete units while balancing network and spatial load."""

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
        traffic: TrafficAwareSlottingService | None = None,
    ):
        self.traffic = traffic or TrafficAwareSlottingService()
        self.slotting = self.traffic.slotting

    @staticmethod
    def _shape(row: dict) -> tuple[int, int, int, int, int]:
        return (
            int(row.get("storage_level") or 1),
            int(row.get("storage_slot") or 1),
            int(row.get("occupied_slot_count") or 1),
            int(row.get("occupied_level_span") or 1),
            int(row.get("occupied_horizontal_slot_span") or 1),
        )

    @staticmethod
    def _groups(rows: list[dict]) -> dict[str, list[dict]]:
        groups: dict[str, list[dict]] = {}
        for row in rows:
            if (
                row.get("assignment_status") == "ASSIGNED"
                and row.get("handling_unit_id")
            ):
                groups.setdefault(str(row["handling_unit_id"]), []).append(row)
        for values in groups.values():
            values.sort(key=GlobalTrafficSlottingService._shape)
        return groups

    @staticmethod
    def _occupied_buffers(rows: list[dict]) -> tuple[str, ...]:
        values = {
            str(buffer_id)
            for row in rows
            for buffer_id in (
                row.get("occupied_buffer_ids")
                or ([row.get("buffer_id")] if row.get("buffer_id") else [])
            )
            if str(buffer_id)
        }
        return tuple(sorted(values))

    @staticmethod
    def _is_amr(rows: list[dict]) -> bool:
        return bool(rows) and all(
            str(row.get("handling_unit_type") or "").strip().casefold()
            == "amr shelf"
            for row in rows
        )

    def _amr_unknown_compatibility(
        self,
        source_rows: list[dict],
        target_rows: list[dict],
        payload: dict,
    ) -> tuple[bool, str]:
        """Validate complete-shelf movement without re-fitting each SKU."""
        if not self._is_amr(source_rows) or not self._is_amr(target_rows):
            return False, "unknown physical data remains fixed for non-AMR units"
        source_shapes = sorted(self._shape(row) for row in source_rows)
        target_by_shape = {
            self._shape(row): row for row in target_rows
        }
        if source_shapes != sorted(target_by_shape):
            return False, "AMR shelves have different internal slot shapes"
        catalog = payload.get("attribute_catalog", [])
        configured_map_attribute_keys = (
            self.traffic.attributes.configured_zone_attribute_keys(
                payload.get("location_attributes", {})
            )
        )
        for source in source_rows:
            target = target_by_shape[self._shape(source)]
            requirements = dict(source.get("sku_requirements") or {})
            generic = {
                key: value for key, value in requirements.items()
                if key not in {
                    *PHYSICAL_DIMENSION_KEYS,
                    PHYSICAL_WEIGHT_KEY,
                }
            }
            effective = target.get("effective_location_attributes") or {}
            profile = self.traffic.attributes.physical_profile(requirements)
            if (
                requires_oversize_capable(profile)
                and effective.get("oversize_capable") is not True
            ):
                return False, (
                    f"SKU {source.get('sku', '')}: oversize or overweight "
                    "inventory requires an oversize-capable zone"
                )
            generic = {
                key: value for key, value in generic.items()
                if key in configured_map_attribute_keys
            }
            issues = self.traffic.attributes.compatibility_issues(
                generic, effective, catalog
            )
            if issues:
                return False, (
                    f"SKU {source.get('sku', '')}: " + "; ".join(issues)
                )
        return True, ""

    def _unit_compatibility(
        self,
        source_rows: list[dict],
        target_rows: list[dict],
        payload: dict,
    ) -> tuple[bool, str]:
        incomplete = any(
            self.traffic.attributes.physical_profile(
                row.get("sku_requirements") or {}
            )["data_status"] != "COMPLETE"
            for row in source_rows
        )
        if incomplete:
            return self._amr_unknown_compatibility(
                source_rows, target_rows, payload
            )
        return self.traffic._strict_unit_compatibility(
            source_rows, target_rows, payload
        )

    @staticmethod
    def _planned_zone(
        zone: str,
        storage_type: str,
        chilled_split: bool,
    ) -> str:
        if chilled_split:
            suffix = (
                "chill_oversize"
                if storage_type == "OVERSIZE"
                else "chill_normal"
            )
        else:
            suffix = storage_type
        return f"{zone}_{suffix}"

    def _empty_buffer_descriptors(
        self,
        payload: dict,
        groups: dict[str, list[dict]],
    ) -> list[dict]:
        storage = payload.get("storage_layout") or {}
        buffers = payload.get("buffers") or storage.get("buffers") or []
        occupied = {
            value
            for rows in groups.values()
            for value in self._occupied_buffers(rows)
        }
        if not buffers or not payload.get("building", {}).get("levels"):
            return []
        try:
            level_name, racks, _workstations, _unreachable = (
                self.slotting.rack_distances(payload["building"])
            )
        except (ValueError, KeyError, StopIteration):
            return []
        zones = dict(payload.get("zone_assignments") or {})
        default_zone = next(iter(zones.values()), "Z01")
        self.slotting.apply_zone_local_aisles(
            payload["building"], racks, zones, default_zone
        )
        rack_by_waypoint = {
            str(rack["waypoint"]): rack for rack in racks
        }
        split_zones = {
            str(row.get("zone_id") or "")
            for rows in groups.values()
            for row in rows
            if "_chill_" in str(row.get("planned_zone_id") or "")
        }
        descriptors = []
        for record in buffers:
            buffer_id = str(record.get("buffer_id") or "")
            if not buffer_id or buffer_id in occupied:
                continue
            waypoint = str(record.get("grid_waypoint") or "")
            rack = rack_by_waypoint.get(waypoint)
            if rack is None:
                continue
            descriptors.append({
                **dict(record),
                "location_id": f"EMPTY::{buffer_id}",
                "level_name": level_name,
                "rack": rack,
                "zone_id": str(
                    zones.get(waypoint)
                    or rack.get("zone_id")
                    or default_zone
                ),
                "chilled_split": str(
                    zones.get(waypoint)
                    or rack.get("zone_id")
                    or default_zone
                ) in split_zones,
            })
        return descriptors

    def _empty_target_rows(
        self,
        source_rows: list[dict],
        descriptor: dict,
        payload: dict,
    ) -> list[dict] | None:
        """Build a compatible location template for an empty physical buffer."""
        buffer_id = str(descriptor["buffer_id"])
        buffer_level = str(descriptor.get("buffer_level") or "")
        rack = descriptor["rack"]
        zone = descriptor["zone_id"]
        aisle = str(rack.get("aisle_id") or "A01")
        waypoint = str(rack["waypoint"])
        is_amr = self._is_amr(source_rows)
        if is_amr and buffer_level != "grid":
            return None
        if not is_amr:
            # Single-slot ASRS moves are supported. Multi-buffer footprints
            # remain on compatible occupied footprints until a packing model
            # can reserve every target slot atomically.
            if (
                buffer_level != "slot"
                or len(source_rows) != 1
                or len(self._occupied_buffers(source_rows)) != 1
                or int(source_rows[0].get("occupied_slot_count") or 1) != 1
            ):
                return None

        root_buffer = buffer_id.split("/L", 1)[0]
        target_rows = []
        for source in source_rows:
            level = int(source.get("storage_level") or 1)
            slot = int(source.get("storage_slot") or 1)
            if not is_amr:
                level_match = re.search(r"/L(\d+)", buffer_id)
                slot_match = re.search(r"/S(\d+)", buffer_id)
                if not level_match or not slot_match:
                    return None
                level, slot = int(level_match.group(1)), int(slot_match.group(1))
            physical_prefix = f"{zone}/{aisle}/{root_buffer}"
            storage_address = (
                f"{physical_prefix}/L{level:02d}/S{slot:02d}"
                if is_amr else f"{zone}/{aisle}/{buffer_id}"
            )
            effective, _sources = self.traffic.attributes.effective_attributes(
                storage_address,
                payload.get("location_attributes") or {},
            )
            storage_type = (
                "OVERSIZE"
                if effective.get(OVERSIZE_CAPABLE_KEY) is True
                else "STANDARD"
            )
            planned_zone = self._planned_zone(
                zone, storage_type, bool(descriptor["chilled_split"])
            )
            static_prefix = (
                f"{planned_zone}/{aisle}/{root_buffer}"
                if descriptor["chilled_split"] else physical_prefix
            )
            static_address = (
                static_prefix
                if is_amr
                else f"{planned_zone if descriptor['chilled_split'] else zone}"
                f"/{aisle}/{buffer_id}"
            )
            occupied_storage = []
            for address in (
                source.get("occupied_storage_location_addresses")
                or [source.get("storage_location_address")]
            ):
                suffix = (
                    str(address).split("/L", 1)[1]
                    if "/L" in str(address) else f"{level:02d}/S{slot:02d}"
                )
                occupied_storage.append(
                    f"{static_prefix}/L{suffix}"
                    if is_amr else static_address
                )
            target = copy.deepcopy(source)
            target.update({
                "static_address": static_address,
                "storage_location_address": storage_address,
                "buffer_id": buffer_id,
                "buffer_level": buffer_level,
                "rmf_grid_address": (
                    f"{descriptor['level_name']}/{waypoint}"
                ),
                "zone_id": zone,
                "aisle_id": aisle,
                "static_bay_id": root_buffer,
                "rack_id": waypoint,
                "rack_waypoint": waypoint,
                "pickup_dispenser_id": str(
                    descriptor.get("rack_endpoint_id")
                    or rack.get("pickup_dispenser_id")
                    or waypoint
                ),
                "rack_vertex_index": int(rack["vertex_index"]),
                "rack_rank": int(rack.get("rack_rank") or 0),
                "storage_level": level,
                "storage_slot": slot,
                "storage_area_type": storage_type,
                "planned_zone_id": planned_zone,
                "planned_storage_type": storage_type,
                "zone_storage_type": storage_type,
                "effective_location_attributes": effective,
                "workstations_evaluated": "|".join(
                    str(value) for value in rack.get("workstations", [])
                ),
                "average_workstation_distance_m": rack.get("distance_m"),
                "routing_status": (
                    "REACHABLE"
                    if math.isfinite(float(rack.get("distance_m", math.inf)))
                    else "UNREACHABLE_LAST_RESORT"
                ),
                "occupied_static_addresses": [static_address],
                "occupied_storage_location_addresses": occupied_storage,
                "occupied_buffer_ids": [buffer_id],
                "occupied_handling_units": [{
                    "handling_unit_id": str(
                        source.get("handling_unit_id") or ""
                    ),
                    "rack_id": waypoint,
                    "storage_level": level,
                    "storage_slot": slot,
                    "buffer_id": buffer_id,
                    "static_address": static_address,
                    "storage_location_address": storage_address,
                }],
            })
            target_rows.append(target)
        target_rows.sort(key=self._shape)
        return target_rows

    @staticmethod
    def _shared_neighbourhoods(
        nodes: dict[str, str | None],
        zones: dict[str, str],
        network: MovementNetwork,
        route_cache,
    ) -> dict[str, str]:
        routes: dict[str, tuple[str, ...]] = {}
        counts: dict[str, int] = {}
        for location, node in nodes.items():
            if node is None:
                routes[location] = ()
                continue
            reachable = [
                endpoint for endpoint in network.endpoints
                if (node, endpoint.node_id) in route_cache
            ]
            if not reachable:
                routes[location] = ()
                continue
            endpoint = min(
                reachable,
                key=lambda value: (
                    -value.weight, value.endpoint_id, value.node_id
                ),
            )
            resources = tuple(
                route_cache[(node, endpoint.node_id)][2]
            )
            routes[location] = resources
            for resource in set(resources):
                counts[resource] = counts.get(resource, 0) + 1
        location_count = len(nodes)
        minimum_shared = max(
            3, math.ceil(math.sqrt(max(1, location_count)) / 2)
        )
        maximum_shared = max(
            minimum_shared, math.floor(0.5 * location_count)
        )
        output = {}
        for location, resources in routes.items():
            shared = next(
                (
                    resource for resource in resources
                    if (
                        minimum_shared
                        <= counts.get(resource, 0)
                        <= maximum_shared
                    )
                ),
                None,
            )
            output[location] = (
                shared
                if shared is not None
                else "UNCLUSTERED"
            )
        return output

    @staticmethod
    def _terminal_only_rack_routes(
        network: MovementNetwork,
    ) -> dict[
        tuple[str, str],
        tuple[float, tuple[str, ...], tuple[str, ...]],
    ]:
        """Find shortest routes without using rack grids as transit nodes."""
        graph = {node: [] for node in network.nodes}
        for link in network.links:
            graph[link.start].append(link)
        for links in graph.values():
            links.sort(key=lambda link: (link.end, link.link_id))

        rack_nodes = set(network.storage_nodes.values()) | {
            node_id
            for node_id, node in network.nodes.items()
            if str(node.kind).strip().casefold() == "storage"
        }
        sources = sorted(set(network.storage_nodes.values()))
        endpoint_nodes = sorted({
            endpoint.node_id for endpoint in network.endpoints
        })
        routes = {}
        for source in sources:
            distance = {source: 0.0}
            signature = {source: ()}
            previous = {}
            queue = [(0.0, (), source)]
            while queue:
                cost, path_signature, node = heapq.heappop(queue)
                if (
                    cost > distance.get(node, math.inf) + 1e-9
                    or path_signature != signature.get(node)
                ):
                    continue
                # A rack grid may be entered when it is the route destination,
                # but it may not be expanded as an intermediate transit node.
                if node in rack_nodes and node != source:
                    continue
                for link in graph.get(node, []):
                    candidate = cost + link.distance
                    candidate_signature = path_signature + (link.link_id,)
                    current = distance.get(link.end, math.inf)
                    if (
                        candidate < current - 1e-9
                        or (
                            abs(candidate - current) <= 1e-9
                            and candidate_signature
                            < signature.get(link.end, ("~",))
                        )
                    ):
                        distance[link.end] = candidate
                        signature[link.end] = candidate_signature
                        previous[link.end] = (node, link)
                        heapq.heappush(
                            queue,
                            (
                                candidate,
                                candidate_signature,
                                link.end,
                            ),
                        )
            for endpoint in endpoint_nodes:
                if endpoint not in distance:
                    continue
                node = endpoint
                path_links = []
                resources = []
                while node != source:
                    prior, link = previous[node]
                    path_links.append(link.link_id)
                    resources.append(link.resource_id)
                    node = prior
                routes[(source, endpoint)] = (
                    distance[endpoint],
                    tuple(reversed(path_links)),
                    tuple(reversed(resources)),
                )
        return routes

    def _candidate_locations(
        self,
        payload: dict,
        demand: TrafficDemand,
        network: MovementNetwork,
        groups: dict[str, list[dict]],
        route_cache,
        progress: ProgressCallback | None,
        cancelled: CancelCallback | None,
    ) -> _CandidateSpace:
        units = sorted(groups)
        templates: dict[tuple[str, str], list[dict]] = {}
        location_buffers = {
            location: self._occupied_buffers(rows)
            for location, rows in groups.items()
        }
        nodes = {
            location: self.traffic._resolve_node(rows[0], network)
            for location, rows in groups.items()
        }
        zones = {
            location: str(
                rows[0].get("planned_zone_id")
                or rows[0].get("zone_id")
                or "UNASSIGNED"
            )
            for location, rows in groups.items()
        }
        empty_descriptors = self._empty_buffer_descriptors(payload, groups)
        descriptor_by_location = {
            value["location_id"]: value for value in empty_descriptors
        }
        for location, descriptor in descriptor_by_location.items():
            fake = {
                "rack_waypoint": descriptor["rack"]["waypoint"],
                "rack_id": descriptor["rack"]["waypoint"],
                "pickup_dispenser_id": (
                    descriptor.get("rack_endpoint_id")
                    or descriptor["rack"].get("pickup_dispenser_id")
                ),
                "rack_vertex_index": descriptor["rack"]["vertex_index"],
            }
            nodes[location] = self.traffic._resolve_node(fake, network)
            zones[location] = self._planned_zone(
                descriptor["zone_id"],
                "OVERSIZE"
                if (
                    self.traffic.attributes.effective_attributes(
                        f"{descriptor['zone_id']}/"
                        f"{descriptor['rack'].get('aisle_id', 'A01')}/"
                        f"{descriptor['buffer_id'].split('/L', 1)[0]}",
                        payload.get("location_attributes") or {},
                    )[0].get(OVERSIZE_CAPABLE_KEY) is True
                )
                else "STANDARD",
                bool(descriptor["chilled_split"]),
            )
            location_buffers[location] = (
                str(descriptor["buffer_id"]),
            )
        reachable_locations = {
            location
            for location, node in nodes.items()
            if node is not None
            and any(
                (node, endpoint.node_id) in route_cache
                for endpoint in network.endpoints
            )
        }

        candidates: dict[str, list[str]] = {}
        for index, unit in enumerate(units, start=1):
            if cancelled and cancelled():
                raise GlobalTrafficCancelledError(
                    "Global traffic optimization was cancelled"
                )
            rows = groups[unit]
            if nodes.get(unit) is None or unit not in reachable_locations:
                candidates[unit] = [unit]
                templates[(unit, unit)] = groups[unit]
            else:
                compatible = []
                for location in units:
                    if location not in reachable_locations:
                        continue
                    forward, _reason = self._unit_compatibility(
                        rows, groups[location], payload
                    )
                    if forward:
                        compatible.append(location)
                        templates[(unit, location)] = groups[location]
                for location, descriptor in descriptor_by_location.items():
                    if location not in reachable_locations:
                        continue
                    target_rows = self._empty_target_rows(
                        rows, descriptor, payload
                    )
                    if target_rows is None:
                        continue
                    forward, _reason = self._unit_compatibility(
                        rows, target_rows, payload
                    )
                    if forward:
                        compatible.append(location)
                        templates[(unit, location)] = target_rows
                        zones[location] = str(
                            target_rows[0].get("planned_zone_id")
                            or target_rows[0].get("zone_id")
                            or zones[location]
                        )
                if unit not in compatible:
                    compatible.append(unit)
                    templates[(unit, unit)] = groups[unit]
                candidates[unit] = sorted(set(compatible))
            if progress and (
                index == len(units)
                or index % max(1, len(units) // 10) == 0
            ):
                progress(
                    2,
                    8,
                    f"Validated global candidates for {index:,}/{len(units):,} units",
                )
        used_locations = {
            location for values in candidates.values() for location in values
        }
        nodes = {
            location: node for location, node in nodes.items()
            if location in used_locations
        }
        zones = {
            location: zone for location, zone in zones.items()
            if location in used_locations
        }
        neighbourhoods = self._shared_neighbourhoods(
            nodes, zones, network, route_cache
        )
        return _CandidateSpace(
            candidates,
            nodes,
            zones,
            neighbourhoods,
            templates,
            {
                location: location_buffers[location]
                for location in used_locations
            },
            set(groups),
            used_locations - set(groups),
        )

    @staticmethod
    def _route_contribution(
        visits: int,
        node: str,
        network: MovementNetwork,
        route_cache,
    ) -> tuple[dict[str, float], float] | None:
        reachable = [
            endpoint for endpoint in network.endpoints
            if (node, endpoint.node_id) in route_cache
        ]
        if not reachable:
            return None
        total_weight = sum(endpoint.weight for endpoint in reachable)
        resources: dict[str, float] = {}
        travel = 0.0
        for endpoint in reachable:
            share = endpoint.weight / total_weight
            distance, _links, route_resources = route_cache[
                (node, endpoint.node_id)
            ]
            flow = visits * share
            travel += flow * distance
            for resource in route_resources:
                resources[resource] = resources.get(resource, 0.0) + flow
        return resources, travel

    @staticmethod
    def _objective_gap(solver, value: float) -> float:
        bound = float(solver.BestObjectiveBound())
        return abs(value - bound) / max(1.0, abs(value))

    @staticmethod
    def _stage_time_budget(
        remaining_seconds: float,
        stage_index: int,
        stage_count: int,
    ) -> float:
        """Reserve enough time to establish the first feasible incumbent."""
        remaining = max(0.05, float(remaining_seconds))
        stages_remaining = max(1, stage_count - stage_index + 1)
        if stage_index == 1:
            first_stage = max(60.0, remaining * 0.40)
            return min(remaining, min(120.0, first_stage))
        return max(0.05, remaining / stages_remaining)

    def _apply_assignment(
        self,
        payload: dict,
        groups: dict[str, list[dict]],
        placement: dict[str, str],
        templates: dict[tuple[str, str], list[dict]],
    ) -> tuple[list[dict], list[dict]]:
        rows = copy.deepcopy(payload["assignments"])
        output_groups = self._groups(rows)
        relocations = []
        for unit, target in sorted(placement.items()):
            source_rows = output_groups[unit]
            old_location = str(
                source_rows[0].get("static_bay_id")
                or source_rows[0].get("static_address")
                or unit
            )
            target_rows = templates[(unit, target)]
            for row, target_row in zip(
                sorted(source_rows, key=self._shape),
                sorted(target_rows, key=self._shape),
            ):
                source_units = copy.deepcopy(
                    row.get("occupied_handling_units") or []
                )
                row.update({
                    field: copy.deepcopy(target_row.get(field))
                    for field in self.LOCATION_FIELDS
                })
                target_units = row.get("occupied_handling_units") or []
                relocated_units = []
                for index, location in enumerate(target_units):
                    record = dict(location)
                    record["handling_unit_id"] = (
                        source_units[index].get("handling_unit_id")
                        if index < len(source_units)
                        else unit
                    )
                    relocated_units.append(record)
                row["occupied_handling_units"] = relocated_units or [{
                    "handling_unit_id": unit,
                    "rack_id": row.get("rack_id", ""),
                    "storage_level": int(row.get("storage_level") or 1),
                    "storage_slot": int(row.get("storage_slot") or 1),
                    "buffer_id": row.get("buffer_id", ""),
                    "static_address": row.get("static_address", ""),
                    "storage_location_address": row.get(
                        "storage_location_address", ""
                    ),
                }]
                row["dynamic_address"], row["dynamic_address_level"] = (
                    self.slotting.build_dynamic_address(
                        str(row.get("zone_id", "")),
                        str(row.get("aisle_id", "")),
                        str(row.get("static_bay_id", "")),
                        int(row.get("storage_level") or 1),
                        int(row.get("storage_slot") or 1),
                        str(row.get("handling_unit_type", "")),
                        unit,
                        buffer_model=bool(payload.get("storage_layout")),
                    )
                )
                row["occupied_dynamic_address"] = (
                    combined_occupied_dynamic_address(row)
                )
                row["compatibility_status"] = "COMPATIBLE"
                row["compatibility_issues"] = []
            new_location = str(
                source_rows[0].get("static_bay_id")
                or source_rows[0].get("static_address")
                or target
            )
            if target != unit:
                relocations.append({
                    "handling_unit_id": unit,
                    "from": old_location,
                    "to": new_location,
                    "location_replaced": (
                        "" if target.startswith("EMPTY::") else target
                    ),
                })
        return rows, relocations

    @staticmethod
    def _balance_values(
        placement: dict[str, str],
        visits: dict[str, int],
        labels: dict[str, str],
    ) -> dict:
        location_counts: dict[str, int] = {}
        for label in labels.values():
            location_counts[label] = location_counts.get(label, 0) + 1
        loads: dict[str, int] = {label: 0 for label in location_counts}
        for unit, location in placement.items():
            label = labels[location]
            loads[label] += int(visits.get(unit, 0))
        average = sum(visits.values()) / max(1, len(placement))
        normalized = {
            label: value / max(1.0, location_counts[label] * average)
            for label, value in loads.items()
        }
        return {
            "loads": dict(sorted(loads.items())),
            "normalized_loads": dict(sorted(normalized.items())),
            "peak_normalized_load": max(normalized.values(), default=0.0),
        }

    @staticmethod
    def _percentile(values: list[float], percentile: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(float(value) for value in values)
        position = (len(ordered) - 1) * percentile / 100.0
        lower = math.floor(position)
        upper = math.ceil(position)
        if lower == upper:
            return ordered[lower]
        weight = position - lower
        return (
            ordered[lower] * (1.0 - weight)
            + ordered[upper] * weight
        )

    @staticmethod
    def _nearest_rank_percentile(
        values: list[float], percentile: float
    ) -> float:
        if not values:
            return 0.0
        ordered = sorted(float(value) for value in values)
        rank = max(
            1, math.ceil(percentile / 100.0 * len(ordered))
        )
        return ordered[rank - 1]

    @classmethod
    def _resource_subset_metrics(
        cls,
        analysis: TrafficAnalysis,
        resource_ids: set[str],
    ) -> dict:
        rows = [
            row for row in analysis.resources
            if row["resource_id"] in resource_ids
        ]
        normalized = [
            float(row["normalized_load"]) for row in rows
        ]
        raw = [float(row["load"]) for row in rows]
        tail_count = max(1, math.ceil(0.05 * len(normalized)))
        return {
            "resource_count": len(rows),
            "peak_load": max(normalized, default=0.0),
            "p90_load": cls._percentile(normalized, 90),
            "p95_load": cls._nearest_rank_percentile(normalized, 95),
            "interpolated_p95_load": cls._percentile(normalized, 95),
            "top_5_percent_mean": (
                sum(sorted(normalized)[-tail_count:]) / tail_count
                if normalized else 0.0
            ),
            "mean_load": (
                sum(normalized) / len(normalized)
                if normalized else 0.0
            ),
            "raw_peak_load": max(raw, default=0.0),
            "raw_p95_load": cls._nearest_rank_percentile(raw, 95),
            "interpolated_raw_p95_load": cls._percentile(raw, 95),
        }

    def optimize_existing_layout(
        self,
        payload: dict,
        dataset: AffinityDataset,
        network: MovementNetwork,
        *,
        start_date=None,
        end_date=None,
        maximum_travel_increase: float = 0.0,
        time_limit_seconds: float = 180.0,
        relative_gap_limit: float = 0.0,
        neighbourhood_mode: str = "shared_resource",
        maximum_controllable_p95_increase: float = 0.0,
        maximum_relocation_fraction: float = 0.50,
        baseline_path: str = "",
        source_orders: str = "",
        progress: ProgressCallback | None = None,
        cancelled: CancelCallback | None = None,
        workflow_mode: str = "existing_layout",
        initial_strategy: str | None = None,
    ) -> GlobalTrafficResult:
        """Globally optimize a complete baseline through simultaneous assignment."""
        if cp_model is None:
            raise GlobalTrafficOptimizationError(
                "OR-Tools is required for global congestion optimization"
            )
        if maximum_travel_increase < 0:
            raise ValueError("maximum travel increase cannot be negative")
        if time_limit_seconds < 0:
            raise ValueError("solve time cannot be negative")
        if not 0 <= relative_gap_limit <= 1:
            raise ValueError("relative gap limit must be between 0 and 1")
        if maximum_controllable_p95_increase < 0:
            raise ValueError(
                "maximum controllable P95 increase cannot be negative"
            )
        if not 0 <= maximum_relocation_fraction <= 1:
            raise ValueError(
                "maximum relocation fraction must be between 0 and 1"
            )
        if neighbourhood_mode not in {"shared_resource", "zone"}:
            raise ValueError(
                "neighbourhood mode must be shared_resource or zone"
            )
        if cancelled and cancelled():
            raise GlobalTrafficCancelledError(
                "Global traffic optimization was cancelled"
            )

        baseline = copy.deepcopy(payload)
        validation = self.traffic.validate_traffic_baseline(baseline)
        rows = baseline["assignments"]
        groups = self._groups(rows)
        if not groups:
            raise GlobalTrafficOptimizationError(
                "The baseline contains no assigned handling units"
            )
        if progress:
            progress(1, 8, "Building Store ID + Date global demand")
        demand = self.traffic.build_demand(
            dataset, rows, start_date, end_date
        )
        route_cache = self._terminal_only_rack_routes(network)
        before = self.traffic.analyze(
            rows, network, demand, route_cache=route_cache
        )
        space = self._candidate_locations(
            baseline, demand, network, groups, route_cache,
            progress, cancelled,
        )
        candidates = space.candidates
        nodes = space.nodes
        zones = space.zones
        neighbourhoods = (
            zones if neighbourhood_mode == "zone"
            else space.neighbourhoods
        )
        units = sorted(groups)
        locations = sorted({
            location
            for values in candidates.values()
            for location in values
        })
        resources = list(network.resources)
        total_visits = sum(demand.unit_visits.values())
        average_visits = total_visits / max(1, len(units))

        if progress:
            progress(3, 8, "Precomputing global traffic contribution matrix")
        contributions: dict[tuple[str, str], tuple[dict[str, float], float]] = {}
        for unit in units:
            visits = int(demand.unit_visits.get(unit, 0))
            for location in candidates[unit]:
                node = nodes.get(location)
                value = (
                    self._route_contribution(
                        visits, node, network, route_cache
                    )
                    if node is not None else None
                )
                if value is None:
                    if location == unit:
                        value = ({}, 0.0)
                    else:
                        continue
                contributions[(unit, location)] = value

        controllable_resources = []
        invariant_resources = []
        for resource in resources:
            controllable = any(
                len({
                    round(
                        contributions[(unit, location)][0].get(
                            resource, 0.0
                        ),
                        9,
                    )
                    for location in candidates[unit]
                    if (unit, location) in contributions
                }) > 1
                for unit in units
            )
            (
                controllable_resources
                if controllable else invariant_resources
            ).append(resource)
        objective_resources = (
            controllable_resources or resources
        )

        model = cp_model.CpModel()
        variables = {}
        for unit in units:
            valid_locations = [
                location for location in candidates[unit]
                if (unit, location) in contributions
            ]
            if not valid_locations:
                raise GlobalTrafficOptimizationError(
                    f"Handling unit {unit} has no valid global location"
                )
            for location in valid_locations:
                variables[(unit, location)] = model.NewBoolVar(
                    f"x__{unit}__{location}"
                )
            model.AddExactlyOne(
                variables[(unit, location)]
                for location in valid_locations
            )
        for location in locations:
            occupants = [
                variables[(unit, location)]
                for unit in units
                if (unit, location) in variables
            ]
            if occupants:
                model.AddAtMostOne(occupants)
        physical_buffers = sorted({
            buffer_id
            for values in space.location_buffers.values()
            for buffer_id in values
        })
        for buffer_id in physical_buffers:
            occupants = [
                variable
                for (unit, location), variable in variables.items()
                if buffer_id in space.location_buffers[location]
            ]
            if occupants:
                model.AddAtMostOne(occupants)
        for (unit, location), variable in variables.items():
            model.AddHint(variable, int(unit == location))

        baseline_reference = max(
            float(before.metrics.get("relative_reference") or 1.0), 1e-9
        )
        resource_expressions = {}
        resource_upper = 0
        for resource in objective_resources:
            terms = []
            for key, variable in variables.items():
                raw = contributions[key][0].get(resource, 0.0)
                capacity = network.resource_capacities.get(
                    resource, baseline_reference
                )
                coefficient = max(
                    0, round(raw * UTILIZATION_SCALE / capacity)
                )
                if coefficient:
                    terms.append(coefficient * variable)
                    resource_upper += coefficient
            expression = model.NewIntVar(
                0, max(1, resource_upper), f"resource__{resource}"
            )
            model.Add(expression == sum(terms))
            resource_expressions[resource] = expression
        max_bound = max(1, resource_upper)
        peak_resource = model.NewIntVar(0, max_bound, "peak_resource")
        if resource_expressions:
            model.AddMaxEquality(
                peak_resource, list(resource_expressions.values())
            )
        else:
            model.Add(peak_resource == 0)
        baseline_scaled_loads = {}
        for resource in objective_resources:
            capacity = network.resource_capacities.get(
                resource, baseline_reference
            )
            baseline_scaled_loads[resource] = sum(
                max(
                    0,
                    round(
                        contributions[(unit, unit)][0].get(
                            resource, 0.0
                        )
                        * UTILIZATION_SCALE
                        / capacity
                    ),
                )
                for unit in units
                if (unit, unit) in contributions
            )
        baseline_guard_p95 = self._nearest_rank_percentile(
            list(baseline_scaled_loads.values()), 95
        )
        guard_limit = math.ceil(
            baseline_guard_p95
            * (1.0 + maximum_controllable_p95_increase)
        )
        guard_allowed_above = max(
            0,
            len(objective_resources)
            - math.ceil(0.95 * len(objective_resources)),
        )
        guard_exceedances = []
        for resource, expression in resource_expressions.items():
            exceeds = model.NewBoolVar(
                f"baseline_p95_guard_exceeds__{resource}"
            )
            model.Add(
                expression <= guard_limit + max_bound * exceeds
            )
            guard_exceedances.append(exceeds)
        model.Add(sum(guard_exceedances) <= guard_allowed_above)
        p95_threshold = model.NewIntVar(
            0, max_bound, "controllable_resource_p95"
        )
        p95_exceedances = []
        for resource, expression in resource_expressions.items():
            exceeds = model.NewBoolVar(f"p95_exceeds__{resource}")
            model.Add(
                expression <= p95_threshold + max_bound * exceeds
            )
            p95_exceedances.append(exceeds)
        # Nearest-rank P95: at least ceil(95% of resources) must be at or
        # below the threshold. This stage runs before spatial objectives so a
        # bounded solve cannot silently trade a worse tail for zone balance.
        allowed_above_p95 = max(
            0,
            len(objective_resources)
            - math.ceil(0.95 * len(objective_resources)),
        )
        model.Add(sum(p95_exceedances) <= allowed_above_p95)

        def spatial_peak(
            name: str,
            labels: dict[str, str],
            *,
            enforce_baseline_guard: bool,
        ) -> tuple[object, dict[str, object], int]:
            counts: dict[str, int] = {}
            for label in labels.values():
                counts[label] = counts.get(label, 0) + 1
            expressions = {}
            upper = 0
            changing_labels = {
                label
                for unit in units
                if len({
                    labels[location]
                    for location in candidates[unit]
                    if (unit, location) in variables
                }) > 1
                for label in {
                    labels[location]
                    for location in candidates[unit]
                    if (unit, location) in variables
                }
            }
            active_labels = changing_labels or set(counts)
            for label, count in sorted(counts.items()):
                if label not in active_labels:
                    continue
                terms = []
                capacity = max(1.0, count * average_visits)
                for (unit, location), variable in variables.items():
                    if labels[location] != label:
                        continue
                    coefficient = max(
                        0,
                        round(
                            int(demand.unit_visits.get(unit, 0))
                            * UTILIZATION_SCALE / capacity
                        ),
                    )
                    if coefficient:
                        terms.append(coefficient * variable)
                        upper += coefficient
                value = model.NewIntVar(
                    0, max(1, upper), f"{name}__{label}"
                )
                model.Add(value == sum(terms))
                expressions[label] = value
            peak = model.NewIntVar(0, max(1, upper), f"peak_{name}")
            model.AddMaxEquality(peak, list(expressions.values()))
            baseline_loads = {
                label: sum(
                    max(
                        0,
                        round(
                            int(demand.unit_visits.get(unit, 0))
                            * UTILIZATION_SCALE
                            / max(1.0, counts[label] * average_visits)
                        ),
                    )
                    for unit in units
                    if (
                        (unit, unit) in variables
                        and labels[unit] == label
                    )
                )
                for label in active_labels
            }
            baseline_peak = max(baseline_loads.values(), default=0)
            if enforce_baseline_guard:
                model.Add(peak <= baseline_peak)
            return peak, expressions, baseline_peak

        (
            peak_neighbourhood,
            neighbourhood_expressions,
            baseline_neighbourhood_peak,
        ) = spatial_peak(
            "neighbourhood",
            neighbourhoods,
            enforce_baseline_guard=True,
        )
        peak_zone, zone_expressions, baseline_zone_peak = spatial_peak(
            "zone", zones, enforce_baseline_guard=False
        )

        eta = model.NewIntVar(0, max_bound, "cvar95_eta")
        excesses = []
        for resource, expression in resource_expressions.items():
            excess = model.NewIntVar(
                0, max_bound, f"cvar95_excess__{resource}"
            )
            model.Add(excess >= expression - eta)
            excesses.append(excess)
        cvar95 = model.NewIntVar(
            0,
            max(
                1,
                len(objective_resources)
                * max_bound
                * CVaR_TAIL_MULTIPLIER,
            ),
            "cvar95",
        )
        model.Add(
            cvar95
            == len(objective_resources) * eta
            + CVaR_TAIL_MULTIPLIER * sum(excesses)
        )
        queue_penalties = []
        for resource, expression in resource_expressions.items():
            # Convex queue-risk proxy. Marginal cost rises at 60%, 75%,
            # and 90% utilization to discourage unstable bottlenecks.
            penalty = model.NewIntVar(
                0, max(1, 25 * max_bound),
                f"queue_risk__{resource}",
            )
            model.Add(penalty >= expression)
            model.Add(penalty >= 4 * expression - 180_000)
            model.Add(penalty >= 10 * expression - 630_000)
            model.Add(penalty >= 25 * expression - 1_980_000)
            queue_penalties.append(penalty)
        queue_risk = model.NewIntVar(
            0,
            max(1, len(objective_resources) * 25 * max_bound),
            "queue_risk_penalty",
        )
        model.Add(queue_risk == sum(queue_penalties))

        travel_terms = []
        baseline_travel = 0
        for key, variable in variables.items():
            coefficient = max(
                0, round(contributions[key][1] * TRAVEL_SCALE)
            )
            if coefficient:
                travel_terms.append(coefficient * variable)
            if key[0] == key[1]:
                baseline_travel += coefficient
        travel_upper = max(
            baseline_travel,
            sum(
                max(
                    (
                        max(
                            0,
                            round(
                                contributions[(unit, location)][1]
                                * TRAVEL_SCALE
                            ),
                        )
                        for location in candidates[unit]
                        if (unit, location) in contributions
                    ),
                    default=0,
                )
                for unit in units
            ),
        )
        total_travel = model.NewIntVar(
            0, max(1, travel_upper), "total_travel"
        )
        model.Add(total_travel == sum(travel_terms))
        model.Add(
            total_travel
            <= math.floor(
                baseline_travel * (1.0 + maximum_travel_increase)
                + 1e-9
            )
        )
        relocation_count = model.NewIntVar(0, len(units), "relocations")
        model.Add(
            relocation_count
            == sum(
                variable
                for (unit, location), variable in variables.items()
                if unit != location
            )
        )
        relocation_limit = (
            0
            if maximum_relocation_fraction == 0
            else min(
                len(units),
                max(
                    2,
                    math.floor(
                        len(units) * maximum_relocation_fraction
                    ),
                ),
            )
        )
        model.Add(
            relocation_count <= relocation_limit
        )

        stages = [
            ("peak_resource_utilization", peak_resource),
            ("p95_resource_utilization", p95_threshold),
            ("cvar95_resource_utilization", cvar95),
            ("queue_risk_penalty", queue_risk),
            ("peak_neighbourhood_concentration", peak_neighbourhood),
            ("peak_zone_utilization", peak_zone),
            ("expected_travel", total_travel),
            ("relocation_count", relocation_count),
        ]
        deadline = (
            time.monotonic() + time_limit_seconds
            if time_limit_seconds > 0 else None
        )
        stage_records = []
        final_solver = None
        bounded_stage_seen = False
        for stage_index, (stage_name, objective) in enumerate(
            stages, start=1
        ):
            if cancelled and cancelled():
                raise GlobalTrafficCancelledError(
                    "Global traffic optimization was cancelled"
                )
            model.Minimize(objective)
            solver = cp_model.CpSolver()
            solver.parameters.num_search_workers = 8
            solver.parameters.random_seed = 0
            solver.parameters.relative_gap_limit = relative_gap_limit
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                solver.parameters.max_time_in_seconds = (
                    self._stage_time_budget(
                        remaining, stage_index, len(stages)
                    )
                )
            callback = _CancellationCallback(cancelled)
            if progress:
                progress(
                    min(7, 3 + stage_index),
                    8,
                    f"Solving global objective {stage_index}/{len(stages)}: "
                    f"{stage_name}",
                )
            status = solver.Solve(model, callback)
            if cancelled and cancelled():
                raise GlobalTrafficCancelledError(
                    "Global traffic optimization was cancelled"
                )
            if status not in {cp_model.OPTIMAL, cp_model.FEASIBLE}:
                if final_solver is None:
                    raise GlobalTrafficOptimizationError(
                        f"Global solver returned {solver.StatusName(status)} "
                        f"during {stage_name}"
                    )
                break
            value = int(solver.Value(objective))
            gap = self._objective_gap(solver, float(value))
            stage_records.append({
                "stage": stage_name,
                "status": solver.StatusName(status),
                "objective_value": value,
                "best_bound": float(solver.BestObjectiveBound()),
                "relative_gap": gap,
                "wall_time_seconds": float(solver.WallTime()),
            })
            final_solver = solver
            if status != cp_model.OPTIMAL or gap > 1e-9:
                bounded_stage_seen = True
                if deadline is None:
                    break
            model.Add(objective == value)
            model.ClearHints()
            for variable in variables.values():
                model.AddHint(variable, solver.Value(variable))

        if final_solver is None:
            raise GlobalTrafficOptimizationError(
                "Global solver did not produce a feasible layout"
            )
        placement = {}
        for unit in units:
            placement[unit] = next(
                location
                for location in candidates[unit]
                if (unit, location) in variables
                and final_solver.Value(variables[(unit, location)])
            )
        optimized_rows, relocations = self._apply_assignment(
            baseline, groups, placement, space.templates
        )
        moved_units = {
            row["handling_unit_id"] for row in relocations
        }
        complete_moved_units = {
            unit for unit in moved_units
            if all(
                self.traffic.attributes.physical_profile(
                    row.get("sku_requirements") or {}
                )["data_status"] == "COMPLETE"
                for row in groups[unit]
            )
        }
        self.traffic.validate_result(
            rows, optimized_rows, baseline, complete_moved_units
        )
        optimized_groups = self._groups(optimized_rows)
        for unit in moved_units - complete_moved_units:
            compatible, reason = self._amr_unknown_compatibility(
                groups[unit], optimized_groups[unit], baseline
            )
            if not compatible:
                raise GlobalTrafficOptimizationError(
                    f"Moved AMR shelf {unit} failed validation: {reason}"
                )
        after = self.traffic.analyze(
            optimized_rows,
            network,
            demand,
            route_cache=route_cache,
            relative_reference=before.metrics["relative_reference"],
        )
        controllable_ids = set(controllable_resources)
        invariant_ids = set(invariant_resources)
        controllable_before = self._resource_subset_metrics(
            before, controllable_ids
        )
        controllable_after = self._resource_subset_metrics(
            after, controllable_ids
        )
        invariant_before = self._resource_subset_metrics(
            before, invariant_ids
        )
        invariant_after = self._resource_subset_metrics(
            after, invariant_ids
        )
        allowed_p95 = (
            controllable_before["p95_load"]
            * (1.0 + maximum_controllable_p95_increase)
        )
        if (
            controllable_resources
            and controllable_after["p95_load"]
            > allowed_p95 + 2.0 / UTILIZATION_SCALE
        ):
            raise GlobalTrafficOptimizationError(
                "Global candidate rejected: controllable-resource P95 "
                f"increased from {controllable_before['p95_load']:.6f} "
                f"to {controllable_after['p95_load']:.6f}; allowed maximum "
                f"is {allowed_p95:.6f}"
            )
        if progress:
            progress(8, 8, "Global congestion-balanced layout validated")

        all_stages_optimal = (
            len(stage_records) == len(stages)
            and not bounded_stage_seen
            and all(
                row["status"] == "OPTIMAL"
                and row["relative_gap"] <= 1e-9
                for row in stage_records
            )
        )
        solver_status = "OPTIMAL" if all_stages_optimal else "FEASIBLE"
        final_gap = 0.0 if solver_status == "OPTIMAL" else (
            max(
                (
                    row["relative_gap"]
                    for row in stage_records
                    if row["status"] == "FEASIBLE"
                    or row["relative_gap"] > 1e-9
                ),
                default=0.0,
            )
            if stage_records else None
        )
        zone_before = self._balance_values(
            {unit: unit for unit in units},
            demand.unit_visits,
            zones,
        )
        zone_after = self._balance_values(
            placement, demand.unit_visits, zones
        )
        neighbourhood_before = self._balance_values(
            {unit: unit for unit in units},
            demand.unit_visits,
            neighbourhoods,
        )
        neighbourhood_after = self._balance_values(
            placement, demand.unit_visits, neighbourhoods
        )
        balance_metrics = {
            "zone_before": zone_before,
            "zone_after": zone_after,
            "neighbourhood_before": neighbourhood_before,
            "neighbourhood_after": neighbourhood_after,
            "controllable_resources_before": controllable_before,
            "controllable_resources_after": controllable_after,
            "invariant_resources_before": invariant_before,
            "invariant_resources_after": invariant_after,
        }
        strategy = initial_strategy or str(
            baseline.get("strategy") or "basic"
        )
        solver_metadata = {
            "backend": "OR-Tools CP-SAT",
            "status": solver_status,
            "global_optimum_proven": solver_status == "OPTIMAL",
            "proof_scope": (
                "fixed_routes_and_all_compatible_single_buffer_locations"
            ),
            "routing_policy": "shortest_path_rack_nodes_terminal_only",
            "relative_gap": final_gap,
            "requested_gap_limit": relative_gap_limit,
            "time_limit_seconds": time_limit_seconds,
            "maximum_travel_increase": maximum_travel_increase,
            "maximum_controllable_p95_increase": (
                maximum_controllable_p95_increase
            ),
            "maximum_relocation_fraction": maximum_relocation_fraction,
            "neighbourhood_mode": neighbourhood_mode,
            "demand_model": "unique_handling_unit_per_store_day",
            "start_date": demand.start_date,
            "end_date": demand.end_date,
            "capacity_mode": bool(network.resource_capacities),
            "simultaneous_permutations": True,
            "handling_unit_count": len(units),
            "candidate_location_count": len(locations),
            "occupied_candidate_location_count": len(
                space.original_locations & set(locations)
            ),
            "empty_candidate_location_count": len(
                space.empty_locations & set(locations)
            ),
            "candidate_variable_count": len(variables),
            "controllable_resource_count": len(controllable_resources),
            "invariant_resource_count": len(invariant_resources),
            "controllable_resources": controllable_resources,
            "invariant_resources": invariant_resources,
            "capacity_warning": (
                ""
                if network.resource_capacities
                else (
                    "Network has no resource capacities; congestion values "
                    "are relative expected loads, not utilization."
                )
            ),
            "acceptance_guard": {
                "status": "PASSED",
                "metric": "controllable_resource_p95",
                "before": controllable_before["p95_load"],
                "after": controllable_after["p95_load"],
                "allowed_increase": maximum_controllable_p95_increase,
                "model_scaled_limit": guard_limit,
                "model_allowed_resources_above_limit": (
                    guard_allowed_above
                ),
                "baseline_neighbourhood_peak_scaled": (
                    baseline_neighbourhood_peak
                ),
                "baseline_zone_peak_scaled": baseline_zone_peak,
                "maximum_relocation_count": relocation_limit,
            },
            "hard_validation": validation,
            "stages": stage_records,
        }
        output = copy.deepcopy(baseline)
        output["generated_at"] = datetime.now(timezone.utc).isoformat()
        output["strategy"] = "global_congestion_balanced"
        output["assignments"] = optimized_rows
        output.setdefault("sources", {})[
            "global_traffic_baseline_layout"
        ] = baseline_path
        output["sources"]["global_traffic_order_workbook"] = source_orders
        output["sources"]["global_traffic_network"] = (
            network.source_path or "embedded_rmf"
        )
        output["global_traffic_configuration"] = {
            "workflow_mode": workflow_mode,
            "initial_strategy": strategy,
            **solver_metadata,
        }
        output["global_traffic_analysis"] = {
            "before": before.metrics,
            "after": after.metrics,
            "fulfillment_groups": demand.fulfillment_groups,
            "handling_unit_visits": demand.handling_unit_visits,
            "unit_visits": dict(sorted(demand.unit_visits.items())),
            "balance": balance_metrics,
            "relocations": relocations,
        }
        output.setdefault("operation_log", []).append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "operation": "global_congestion_balanced_slotting",
            "solver_status": solver_status,
            "relative_gap": final_gap,
            "relocation_count": len(relocations),
            "excluded_unassigned_sku_count": validation[
                "excluded_unassigned_sku_count"
            ],
        })
        output["buffers"] = self.traffic._buffer_records(
            output, optimized_rows
        )
        return GlobalTrafficResult(
            baseline,
            optimized_rows,
            demand,
            before,
            after,
            relocations,
            solver_metadata,
            balance_metrics,
            output,
            workflow_mode,
            strategy,
        )

    def run_full_pipeline(
        self,
        building: dict,
        sku_rows: list[dict],
        affinity_source: AffinityAnalysis | AffinityDataset,
        network: MovementNetwork,
        *,
        initial_strategy: str = "basic",
        affinity_weight: float = 0.5,
        levels_per_rack: int = 1,
        slots_per_level: int = 6,
        handling_unit_type: str = "AMR shelf",
        zone_assignments: dict[str, str] | None = None,
        attribute_catalog=None,
        location_attributes: dict[str, dict] | None = None,
        storage_layout=None,
        start_date=None,
        end_date=None,
        maximum_travel_increase: float = 0.0,
        time_limit_seconds: float = 180.0,
        relative_gap_limit: float = 0.0,
        neighbourhood_mode: str = "shared_resource",
        maximum_controllable_p95_increase: float = 0.0,
        maximum_relocation_fraction: float = 0.50,
        source_grid_project: str = "",
        source_velocity: str = "",
        source_chilled: str = "",
        source_orders: str = "",
        progress: ProgressCallback | None = None,
        cancelled: CancelCallback | None = None,
    ) -> GlobalTrafficResult:
        """Generate the selected baseline, then run independent global placement."""
        if progress:
            progress(1, 10, "Generating initial layout for global optimization")
        baseline = self.traffic.run_full_pipeline(
            building,
            sku_rows,
            affinity_source,
            network,
            initial_strategy=initial_strategy,
            affinity_weight=affinity_weight,
            levels_per_rack=levels_per_rack,
            slots_per_level=slots_per_level,
            handling_unit_type=handling_unit_type,
            zone_assignments=zone_assignments,
            attribute_catalog=attribute_catalog,
            location_attributes=location_attributes,
            storage_layout=storage_layout,
            start_date=start_date,
            end_date=end_date,
            optimize_traffic=False,
            source_grid_project=source_grid_project,
            source_velocity=source_velocity,
            source_chilled=source_chilled,
            source_orders=source_orders,
            progress=None,
            cancelled=cancelled,
        ).pretraffic_payload

        def shifted(current: int, total: int, message: str) -> None:
            if progress:
                progress(2 + current, 10, message)

        return self.optimize_existing_layout(
            baseline,
            (
                affinity_source.dataset
                if isinstance(affinity_source, AffinityAnalysis)
                else affinity_source
            ),
            network,
            start_date=start_date,
            end_date=end_date,
            maximum_travel_increase=maximum_travel_increase,
            time_limit_seconds=time_limit_seconds,
            relative_gap_limit=relative_gap_limit,
            neighbourhood_mode=neighbourhood_mode,
            maximum_controllable_p95_increase=(
                maximum_controllable_p95_increase
            ),
            maximum_relocation_fraction=maximum_relocation_fraction,
            baseline_path="generated_in_global_pipeline",
            source_orders=source_orders,
            progress=shifted,
            cancelled=cancelled,
            workflow_mode="full_pipeline",
            initial_strategy=initial_strategy,
        )
