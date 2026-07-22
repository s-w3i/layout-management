"""Storage-zone planning and inventory address construction."""

from __future__ import annotations

import itertools
import math

from .attributes import OVERSIZE_CAPABLE_KEY, StorageAttributeService


def plan_storage_zones(
    positions: list[dict],
    sku_rows: list[dict],
    catalog,
    attributes: StorageAttributeService,
    levels_per_rack: int,
    local_attributes: dict[str, dict],
) -> None:
    """Assign every position to exactly one STANDARD or OVERSIZE zone.

    Ambient user zones remain atomic. Chilled user zones may be split into
    generated standard/oversize subzones because temperature containment is
    retained by both children.
    """
    def planned_address(address: str, planned_zone: str) -> str:
        parts = str(address).split("/", 1)
        return (
            f"{planned_zone}/{parts[1]}"
            if len(parts) == 2 and planned_zone
            else str(address)
        )

    demand = {
        False: {"STANDARD": 0, "OVERSIZE": 0},
        True: {"STANDARD": 0, "OVERSIZE": 0},
    }
    for row in sku_rows:
        requirements = row.get("sku_requirements")
        if not isinstance(requirements, dict):
            requirements = attributes.requirements_from_row(row, catalog)
        profile = attributes.physical_profile(requirements)
        storage_type = (
            "STANDARD"
            if profile["storage_class"] == "STANDARD"
            else "OVERSIZE"
        )
        demand[requirements.get("chilled") is True][storage_type] += 1

    for chilled in (False, True):
        pool = [
            position for position in positions
            if position["effective_location_attributes"].get("chilled") is chilled
        ]
        exception_required = demand[chilled]["OVERSIZE"]
        standard_required = demand[chilled]["STANDARD"]
        chilled_split_required = bool(chilled and exception_required and standard_required)
        oversize_ids: set[int] = set()
        if chilled:
            # Chilled zones split by whole rack. Standard chilled inventory
            # keeps first claim on the nearest racks; oversize/unknown
            # physical inventory can use only different chilled racks.
            by_rack: dict[str, list[dict]] = {}
            for position in pool:
                by_rack.setdefault(str(position["rack_id"]), []).append(position)
            ranked_racks = sorted(
                by_rack,
                key=lambda rack_id: (
                    math.isinf(
                        min(float(item["distance_m"]) for item in by_rack[rack_id])
                    ),
                    min(float(item["distance_m"]) for item in by_rack[rack_id]),
                    min(int(item["rack_rank"]) for item in by_rack[rack_id]),
                    rack_id,
                ),
            )
            standard_racks: set[str] = set()
            standard_capacity = 0
            for rack_id in ranked_racks:
                if standard_capacity >= standard_required:
                    break
                standard_racks.add(rack_id)
                standard_capacity += len(by_rack[rack_id])
            remaining_for_oversize = [
                item
                for rack_id in ranked_racks
                if rack_id not in standard_racks
                for item in sorted(
                    by_rack[rack_id],
                    key=lambda value: (
                        abs(float(value["level"]) - (levels_per_rack + 1) / 2.0),
                        int(value["level"]),
                        int(value["slot"]),
                    ),
                )
            ]
            oversize_ids = {
                id(item)
                for item in remaining_for_oversize[:exception_required]
            }
        else:
            # Ambient zones are atomic: standard demand keeps first claim on
            # the nearest whole zones; exception inventory is planned from
            # the remaining whole-zone capacity.
            by_zone: dict[str, list[dict]] = {}
            for position in pool:
                by_zone.setdefault(str(position["zone_id"]), []).append(position)
            zones = sorted(by_zone)
            explicit_oversize_ids = {
                id(position)
                for position in pool
                if attributes.is_oversize_location(
                    position["effective_location_attributes"]
                )
            } if exception_required else set()
            zone_distance = {
                zone: (
                    sum(float(item["distance_m"]) for item in by_zone[zone])
                    / max(len(by_zone[zone]), 1)
                )
                for zone in zones
            }
            standard_capacity_by_zone = {
                zone: sum(
                    id(item) not in explicit_oversize_ids
                    for item in by_zone[zone]
                )
                for zone in zones
            }
            nearest_zones = sorted(
                zones,
                key=lambda zone: (
                    math.isinf(zone_distance[zone]),
                    zone_distance[zone],
                    zone,
                ),
            )
            standard_zones = set()
            standard_capacity = 0
            for zone in nearest_zones:
                if standard_capacity >= standard_required:
                    break
                if standard_capacity_by_zone[zone] <= 0:
                    continue
                standard_zones.add(zone)
                standard_capacity += standard_capacity_by_zone[zone]
            oversize_candidate_zones = [
                zone for zone in zones if zone not in standard_zones
            ]
            remaining_exception_required = max(
                0, exception_required - len(explicit_oversize_ids)
            )
            feasible_subsets = []
            if (
                remaining_exception_required
                and len(oversize_candidate_zones) <= 16
            ):
                for count in range(1, len(oversize_candidate_zones) + 1):
                    for subset in itertools.combinations(
                        oversize_candidate_zones, count
                    ):
                        capacity = sum(len(by_zone[zone]) for zone in subset)
                        if capacity < remaining_exception_required:
                            continue
                        weighted_distance = sum(
                            sum(float(item["distance_m"]) for item in by_zone[zone])
                            for zone in subset
                        ) / max(capacity, 1)
                        feasible_subsets.append((
                            capacity - remaining_exception_required,
                            weighted_distance,
                            count,
                            subset,
                        ))
                selected_zones = set(min(feasible_subsets)[-1]) if feasible_subsets else set()
            elif remaining_exception_required:
                ranked_zones = sorted(
                    oversize_candidate_zones,
                    key=lambda zone: (
                        abs(len(by_zone[zone]) - remaining_exception_required),
                        zone_distance[zone],
                        zone,
                    ),
                )
                selected_zones = set()
                capacity = 0
                for zone in ranked_zones:
                    selected_zones.add(zone)
                    capacity += len(by_zone[zone])
                    if capacity >= remaining_exception_required:
                        break
            else:
                selected_zones = set()
            oversize_ids = {
                id(item)
                for zone in selected_zones
                for item in by_zone[zone]
            } | explicit_oversize_ids

        for position in pool:
            storage_type = (
                "OVERSIZE" if id(position) in oversize_ids else "STANDARD"
            )
            position["planned_storage_type"] = storage_type
            zone_suffix = (
                "chill_oversize"
                if chilled_split_required and storage_type == "OVERSIZE"
                else "chill_normal"
                if chilled_split_required
                else storage_type
            )
            position["planned_zone_id"] = f"{position['zone_id']}_{zone_suffix}"
            if chilled_split_required:
                position["static_address"] = planned_address(
                    position["static_address"], position["planned_zone_id"]
                )
            expected_flag = storage_type == "OVERSIZE"
            effective = dict(position["effective_location_attributes"])
            if effective.get(OVERSIZE_CAPABLE_KEY) is not expected_flag:
                local_attributes.setdefault(
                    position["storage_location_address"], {}
                )[OVERSIZE_CAPABLE_KEY] = expected_flag
            effective[OVERSIZE_CAPABLE_KEY] = expected_flag
            position["effective_location_attributes"] = effective
            position["storage_area_type"] = storage_type


def derive_zone_storage_types(
    rows: list[dict], zones: set[str] | None = None
) -> dict[str, str]:
    """Classify generated zones without ever producing a mixed zone."""
    exception_classes = {
        "OVERSIZE", "OVERWEIGHT", "OVERSIZE_AND_OVERWEIGHT",
        "UNKNOWN_SIZE", "UNKNOWN_WEIGHT", "NON_VOLUMETRIC_DATA",
        "UNVERIFIED_OVERSIZE",
    }
    for row in rows:
        if row.get("assignment_status") != "ASSIGNED" or row.get("planned_zone_id"):
            continue
        storage_type = (
            "OVERSIZE"
            if row.get("physical_storage_class") in exception_classes
            else "STANDARD"
        )
        row["planned_storage_type"] = storage_type
        row["planned_zone_id"] = f"{row.get('zone_id', '')}_{storage_type}"

    assigned_zone_ids = {
        str(row.get("planned_zone_id"))
        for row in rows
        if row.get("assignment_status") == "ASSIGNED"
        and row.get("planned_zone_id")
    }
    known_zones = assigned_zone_ids or set(zones or ())
    known_zones.update(
        str(row.get("planned_zone_id") or row.get("zone_id", ""))
        for row in rows
        if row.get("assignment_status") == "ASSIGNED"
        and (row.get("planned_zone_id") or row.get("zone_id"))
    )
    zone_mix = {
        zone: {"standard": 0, "oversize": 0}
        for zone in sorted(known_zones)
    }
    for row in rows:
        if row.get("assignment_status") != "ASSIGNED":
            continue
        zone = str(row.get("planned_zone_id") or row.get("zone_id", ""))
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
        if counts["oversize"]:
            zone_storage_types[zone] = "OVERSIZE"
        elif counts["standard"]:
            zone_storage_types[zone] = "STANDARD"
        else:
            zone_storage_types[zone] = "UNUSED"
    for row in rows:
        zone = str(row.get("planned_zone_id") or row.get("zone_id", ""))
        row["zone_storage_type"] = (
            zone_storage_types.get(zone, "")
            if row.get("assignment_status") == "ASSIGNED"
            else ""
        )
    return zone_storage_types


def build_dynamic_address(
    zone_id: str,
    aisle_id: str,
    static_bay_id: str,
    level: int,
    slot: int,
    handling_unit_type: str,
    handling_unit_id: str,
    *,
    buffer_model: bool = False,
) -> tuple[str, str]:
    if buffer_model:
        if handling_unit_type == "AMR shelf":
            return (
                f"{handling_unit_id}/L{level:02d}/S{slot:02d}",
                "shelf_slot",
            )
        if handling_unit_type in {"Tote", "Pallet"}:
            return handling_unit_id, "handling_unit"
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
