"""Storage-zone planning and inventory address construction."""

from __future__ import annotations

from collections import Counter, defaultdict
import itertools
import math

from .attributes import (
    OVERSIZE_CAPABLE_KEY,
    PHYSICAL_ATTRIBUTE_KEYS,
    StorageAttributeService,
)
from .slotting_rules import required_slot_footprint


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

    has_configured_temperature = any(
        isinstance(
            position["effective_location_attributes"].get("chilled"), bool
        )
        for position in positions
    )
    planning_groups = (
        [
            (
                chilled,
                [
                    position for position in positions
                    if position["effective_location_attributes"].get("chilled")
                    is chilled
                ],
            )
            for chilled in (False, True)
        ]
        if has_configured_temperature
        else [(None, list(positions))]
    )

    for chilled, pool in planning_groups:
        maximum_horizontal_slots = max(
            (int(position["slot"]) for position in pool),
            default=0,
        )
        exception_required = 0
        standard_required = 0
        for row in sku_rows:
            requirements = row.get("sku_requirements")
            if not isinstance(requirements, dict):
                requirements = attributes.requirements_from_row(row, catalog)
            required_chilled = requirements.get("chilled")
            if chilled is not None and (
                (required_chilled if isinstance(required_chilled, bool) else False)
                is not chilled
            ):
                continue
            profile = attributes.physical_profile(requirements)
            if profile["storage_class"] == "STANDARD":
                standard_required += 1
                continue

            required_positions = 1
            if (
                all(
                    key in profile.get("values", {})
                    for key in (
                        "max_item_length",
                        "max_item_width",
                        "max_item_height",
                    )
                )
                and attributes.is_volumetric_oversize(profile)
            ):
                feasible_footprints = [
                    footprint
                    for position in pool
                    if (
                        footprint := required_slot_footprint(
                            requirements,
                            position["effective_location_attributes"],
                            levels_per_rack,
                            maximum_horizontal_slots,
                        )
                    ) is not None
                ]
                if feasible_footprints:
                    required_positions = min(
                        level_span * slot_span
                        for level_span, slot_span in feasible_footprints
                    )
            exception_required += required_positions

        chilled_split_required = bool(
            chilled is True and exception_required and standard_required
        )
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
            columns = sorted({
                float(items[0].get("x", 0)) for items in by_rack.values()
            })
            column_index = {value: index for index, value in enumerate(columns)}
            spatial_racks = sorted(
                by_rack,
                key=lambda rack_id: (
                    column_index[float(by_rack[rack_id][0].get("x", 0))],
                    (
                        float(by_rack[rack_id][0].get("y", 0))
                        if column_index[
                            float(by_rack[rack_id][0].get("x", 0))
                        ] % 2 == 0
                        else -float(by_rack[rack_id][0].get("y", 0))
                    ),
                    rack_id,
                ),
            )
            oversize_racks: set[str] = set()
            oversize_capacity = 0
            # Reserve exception racks from one physical edge. Selecting the
            # next rack by travel rank can put an OVERSIZE island inside an
            # otherwise identical STANDARD attribute block.
            for rack_id in reversed(spatial_racks):
                if oversize_capacity >= exception_required:
                    break
                oversize_racks.add(rack_id)
                oversize_capacity += len(by_rack[rack_id])
            remaining_standard_capacity = sum(
                len(by_rack[rack_id])
                for rack_id in spatial_racks
                if rack_id not in oversize_racks
            )
            if remaining_standard_capacity < standard_required:
                # Preserve feasibility when the zone is too small to keep a
                # clean edge partition.
                oversize_racks.clear()
                oversize_capacity = 0
                standard_racks: set[str] = set()
                standard_capacity = 0
                for rack_id in ranked_racks:
                    if standard_capacity >= standard_required:
                        break
                    standard_racks.add(rack_id)
                    standard_capacity += len(by_rack[rack_id])
                for rack_id in ranked_racks:
                    if rack_id in standard_racks:
                        continue
                    if oversize_capacity >= exception_required:
                        break
                    oversize_racks.add(rack_id)
                    oversize_capacity += len(by_rack[rack_id])
            oversize_ids = {
                id(item)
                for rack_id in oversize_racks
                for item in by_rack[rack_id]
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


def plan_exact_attribute_subzones(
    positions: list[dict],
    sku_rows: list[dict],
    catalog,
    attributes: StorageAttributeService,
    local_attributes: dict[str, dict],
) -> dict[str, dict]:
    """Dedicate zones first, then rack-based subzones, to exact SKU profiles.

    Explicit hierarchy values remain hard constraints. A source zone is kept
    whole when one profile consumes all of its racks. If another profile needs
    the remaining capacity, those racks receive a generated subzone ID and the
    same exact attributes at every rack root in that subzone.
    """
    definitions = attributes.normalize_catalog(catalog)
    grouping_keys = tuple(
        key for key, definition in sorted(
            definitions.items(),
            key=lambda item: (
                item[1].hierarchy_level is None,
                item[1].hierarchy_level or 10**9,
            ),
        )
        if definition.match_rule == "exact"
        and key not in PHYSICAL_ATTRIBUTE_KEYS
        and key != OVERSIZE_CAPABLE_KEY
    )
    if not grouping_keys or not positions:
        return {}
    hierarchy_levels = {
        key: definitions[key].hierarchy_level or fallback_level
        for fallback_level, key in enumerate(grouping_keys, start=1)
    }
    physical_enabled = attributes.has_physical_catalog(definitions)

    def requirements_for(row: dict) -> dict:
        requirements = row.get("sku_requirements")
        return (
            requirements
            if isinstance(requirements, dict)
            else attributes.requirements_from_row(row, definitions)
        )

    def profile_for(requirements: dict) -> tuple[tuple[str, object], ...]:
        return tuple(
            (key, requirements[key])
            for key in grouping_keys
            if key in requirements
        )

    profile_order = []
    demand: dict[tuple[tuple[str, object], ...], Counter] = defaultdict(Counter)
    for row in sku_rows:
        requirements = requirements_for(row)
        profile = profile_for(requirements)
        if not profile:
            continue
        if profile not in demand:
            profile_order.append(profile)
        storage_type = (
            "OVERSIZE"
            if physical_enabled
            and attributes.physical_profile(requirements)["storage_class"]
            != "STANDARD"
            else "STANDARD"
        )
        required_positions = 1
        if physical_enabled:
            maximum_levels = max(
                (int(position.get("level", 1)) for position in positions),
                default=1,
            )
            maximum_slots = max(
                (int(position.get("slot", 1)) for position in positions),
                default=1,
            )
            footprints = [
                footprint
                for position in positions
                if str(position.get("planned_storage_type")) == storage_type
                if (
                    footprint := required_slot_footprint(
                        requirements,
                        position.get("effective_location_attributes", {}),
                        maximum_levels,
                        maximum_slots,
                    )
                ) is not None
            ]
            if footprints:
                required_positions = min(
                    vertical * horizontal
                    for vertical, horizontal in footprints
                )
        demand[profile][storage_type] += required_positions

    racks: dict[str, list[dict]] = defaultdict(list)
    for position in positions:
        racks[str(position["rack_id"])].append(position)
    rack_order = sorted(
        racks,
        key=lambda rack_id: (
            math.isinf(min(float(p["distance_m"]) for p in racks[rack_id])),
            min(float(p["distance_m"]) for p in racks[rack_id]),
            min(int(p["rack_rank"]) for p in racks[rack_id]),
            rack_id,
        ),
    )
    rack_profile: dict[str, tuple[tuple[str, object], ...]] = {}

    def spatial_rack_order(rack_ids):
        rack_ids = list(rack_ids)
        columns = sorted({
            float(racks[rack_id][0].get("x", 0)) for rack_id in rack_ids
        })
        column_index = {value: index for index, value in enumerate(columns)}
        return sorted(
            rack_ids,
            key=lambda rack_id: (
                column_index[float(racks[rack_id][0].get("x", 0))],
                (
                    float(racks[rack_id][0].get("y", 0))
                    if column_index[
                        float(racks[rack_id][0].get("x", 0))
                    ] % 2 == 0
                    else -float(racks[rack_id][0].get("y", 0))
                ),
                rack_id,
            ),
        )

    def eligible(rack_id: str, profile: tuple[tuple[str, object], ...]) -> bool:
        for position in racks[rack_id]:
            effective = position["effective_location_attributes"]
            for key, required in profile:
                if key in effective and effective[key] != required:
                    return False
        return True

    for profile in profile_order:
        remaining = Counter(demand[profile])
        candidates = [
            rack_id for rack_id in rack_order
            if rack_id not in rack_profile and eligible(rack_id, profile)
        ]
        zones = []
        racks_by_zone: dict[str, list[str]] = defaultdict(list)
        for rack_id in candidates:
            zone = str(racks[rack_id][0]["zone_id"])
            if zone not in racks_by_zone:
                zones.append(zone)
            racks_by_zone[zone].append(rack_id)
        for zone in zones:
            for rack_id in spatial_rack_order(racks_by_zone[zone]):
                capacities = Counter(
                    str(position["planned_storage_type"])
                    for position in racks[rack_id]
                )
                useful = sum(
                    min(remaining[storage_type], capacity)
                    for storage_type, capacity in capacities.items()
                )
                if useful <= 0:
                    continue
                rack_profile[rack_id] = profile
                for storage_type, capacity in capacities.items():
                    remaining[storage_type] = max(
                        0, remaining[storage_type] - capacity
                    )
                if not any(remaining.values()):
                    break
            if not any(remaining.values()):
                break

    # A split source zone must remain a set of contiguous rack blocks. Fill
    # unused racks from the previous assigned block (and leading racks from the
    # first block) so the map never draws one zone through another zone.
    source_zones = {
        str(racks[rack_id][0]["zone_id"]) for rack_id in rack_order
    }
    for zone in source_zones:
        zone_racks = spatial_rack_order(
            (
                rack_id for rack_id in rack_order
                if str(racks[rack_id][0]["zone_id"]) == zone
            )
        )
        assigned_profiles = {
            rack_profile[rack_id]
            for rack_id in zone_racks if rack_id in rack_profile
        }
        if len(assigned_profiles) < 2:
            continue
        last_profile = None
        for rack_id in zone_racks:
            if rack_id in rack_profile:
                last_profile = rack_profile[rack_id]
            elif last_profile is not None:
                rack_profile[rack_id] = last_profile
        first_profile = next(
            (rack_profile[rack_id] for rack_id in zone_racks
             if rack_id in rack_profile),
            None,
        )
        if first_profile is not None:
            for rack_id in zone_racks:
                if rack_id in rack_profile:
                    break
                rack_profile[rack_id] = first_profile

    profiles_by_zone: dict[str, list[tuple[tuple[str, object], ...]]] = defaultdict(list)
    for rack_id in rack_order:
        profile = rack_profile.get(rack_id)
        if profile is None:
            continue
        zone = str(racks[rack_id][0]["zone_id"])
        if profile not in profiles_by_zone[zone]:
            profiles_by_zone[zone].append(profile)

    generated: dict[str, dict] = {}
    for zone, zone_profiles in profiles_by_zone.items():
        zone_racks = [rack_id for rack_id in rack_order if racks[rack_id][0]["zone_id"] == zone]
        assigned_zone_racks = [rack_id for rack_id in zone_racks if rack_id in rack_profile]
        whole_zone = len(zone_profiles) == 1
        for profile in zone_profiles:
            profile_values = dict(profile)
            profile_racks = [
                rack_id for rack_id in assigned_zone_racks
                if rack_profile[rack_id] == profile
            ]
            hierarchy_path = []
            subzone_id = zone
            for key in grouping_keys:
                if key not in profile_values:
                    continue
                level = hierarchy_levels[key]
                lower_level_prefix = {
                    prefix_key: profile_values[prefix_key]
                    for prefix_key in grouping_keys
                    if prefix_key in profile_values
                    and hierarchy_levels[prefix_key] < level
                }
                peer_profiles = [
                    dict(candidate) for candidate in zone_profiles
                    if all(
                        dict(candidate).get(parent_key) == parent_value
                        for parent_key, parent_value
                        in lower_level_prefix.items()
                    )
                ]
                distinct_values = {
                    candidate.get(key) for candidate in peer_profiles
                }
                value = profile_values[key]
                if not whole_zone and len(distinct_values) > 1:
                    value_code = (
                        "T" if value is True else "F" if value is False else "U"
                    )
                    subzone_id += f"__L{level:02d}_{key}_{value_code}"
                hierarchy_path.append({
                    "level": level,
                    "attribute": key,
                    "value": value,
                    "zone_id": subzone_id,
                })
            generated[subzone_id] = {
                "parent_zone_id": zone,
                "attributes": profile_values,
                "rack_ids": list(profile_racks),
                "hierarchy_path": hierarchy_path,
            }
            for rack_id in profile_racks:
                rack_path = "/".join(
                    racks[rack_id][0]["storage_location_address"].split("/")[:3]
                )
                rack_effective = racks[rack_id][0][
                    "effective_location_attributes"
                ]
                missing = {
                    key: value for key, value in profile
                    if key not in rack_effective
                }
                if whole_zone:
                    zone_effective = attributes.effective_attributes(
                        zone, local_attributes
                    )[0]
                    missing = {
                        key: value for key, value in profile
                        if key not in zone_effective
                    }
                    if missing:
                        local_attributes.setdefault(zone, {}).update(missing)
                else:
                    if missing:
                        local_attributes.setdefault(rack_path, {}).update(missing)
                for position in racks[rack_id]:
                    address = position["storage_location_address"]
                    position["effective_location_attributes"] = (
                        attributes.effective_attributes(address, local_attributes)[0]
                    )
                    base_zone = position["planned_zone_base_id"]
                    subzone_suffix = (
                        subzone_id[len(zone):]
                        if subzone_id != zone else ""
                    )
                    position["planned_zone_id"] = (
                        base_zone if whole_zone
                        else f"{base_zone}{subzone_suffix}"
                    )
                    position["generated_attribute_zone_id"] = subzone_id
                    position["parent_zone_id"] = zone
                    position["auto_zone_attributes"] = dict(missing)
    return generated


def materialize_independent_attribute_zones(
    positions: list[dict],
    rows: list[dict],
    generated_zones: dict[str, dict],
    local_attributes: dict[str, dict],
    zone_assignments: dict[str, str],
) -> dict[str, dict]:
    """Promote planned attribute partitions to standalone warehouse zones."""
    used_zone_ids = {
        str(position.get("zone_id", "")) for position in positions
        if str(position.get("zone_id", ""))
    }
    next_suffix_by_source: dict[str, int] = defaultdict(lambda: 1)

    def next_zone_id(source_zone):
        while True:
            suffix = next_suffix_by_source[source_zone]
            next_suffix_by_source[source_zone] += 1
            candidate = f"{source_zone}_{suffix}"
            if candidate not in used_zone_ids:
                used_zone_ids.add(candidate)
                return candidate

    if not generated_zones:
        rack_ids_by_zone: dict[str, list[str]] = defaultdict(list)
        for position in positions:
            zone = str(position.get("zone_id", ""))
            rack_id = str(position.get("rack_id", ""))
            if rack_id and rack_id not in rack_ids_by_zone[zone]:
                rack_ids_by_zone[zone].append(rack_id)
        generated_zones = {
            zone: {
                "parent_zone_id": zone,
                "attributes": {},
                "rack_ids": rack_ids,
                "hierarchy_path": [],
            }
            for zone, rack_ids in rack_ids_by_zone.items()
        }

    rack_storage_types: dict[str, set[str]] = defaultdict(set)
    rack_coordinates = {}
    for position in positions:
        rack_id = str(position.get("rack_id", ""))
        rack_storage_types[rack_id].add(
            str(position.get("planned_storage_type", "STANDARD"))
        )
        rack_coordinates.setdefault(
            rack_id,
            (float(position.get("x", 0)), float(position.get("y", 0))),
        )
    for rack_id, storage_types in rack_storage_types.items():
        if len(storage_types) < 2:
            continue
        # A rack is the smallest zone-visible unit. If legacy configuration
        # mixes storage types inside it, expose the complete rack as the more
        # restrictive exception type while retaining slot-level numeric
        # capacities for compatibility checks.
        rack_type = "OVERSIZE" if "OVERSIZE" in storage_types else sorted(
            storage_types
        )[0]
        for position in positions:
            if str(position.get("rack_id", "")) == rack_id:
                position["planned_storage_type"] = rack_type
        rack_storage_types[rack_id] = {rack_type}

    physical_neighbors: dict[str, set[str]] = defaultdict(set)
    racks_by_x: dict[float, list[tuple[float, str]]] = defaultdict(list)
    racks_by_y: dict[float, list[tuple[float, str]]] = defaultdict(list)
    for rack_id, (x, y) in rack_coordinates.items():
        racks_by_x[x].append((y, rack_id))
        racks_by_y[y].append((x, rack_id))
    for line in (*racks_by_x.values(), *racks_by_y.values()):
        ordered = [rack_id for _coordinate, rack_id in sorted(line)]
        for left, right in zip(ordered, ordered[1:]):
            physical_neighbors[left].add(right)
            physical_neighbors[right].add(left)

    def connected_components(rack_ids):
        remaining = set(rack_ids)
        components = []
        while remaining:
            start = min(
                remaining,
                key=lambda rack_id: (*rack_coordinates[rack_id], rack_id),
            )
            component = []
            pending = [start]
            remaining.remove(start)
            while pending:
                rack_id = pending.pop()
                component.append(rack_id)
                for neighbor in sorted(physical_neighbors[rack_id]):
                    if neighbor in remaining:
                        remaining.remove(neighbor)
                        pending.append(neighbor)
            components.append(sorted(
                component,
                key=lambda rack_id: (*rack_coordinates[rack_id], rack_id),
            ))
        return components

    expanded_definitions = []
    for planned_zone, definition in generated_zones.items():
        by_storage_type: dict[str, list[str]] = defaultdict(list)
        for rack_id in definition.get("rack_ids", []):
            storage_types = rack_storage_types[str(rack_id)]
            by_storage_type[next(iter(storage_types))].append(str(rack_id))
        split_by_storage = len(by_storage_type) > 1
        for storage_type, rack_ids in sorted(by_storage_type.items()):
            components = connected_components(rack_ids)
            for component_index, component in enumerate(components, start=1):
                partition_key = (
                    f"{planned_zone}__storage_{storage_type}"
                    f"__component_{component_index}"
                    if split_by_storage or len(components) > 1
                    else planned_zone
                )
                expanded_definitions.append((
                    partition_key,
                    {
                        **definition,
                        "rack_ids": component,
                        "storage_type": storage_type,
                    },
                ))

    promoted = {}
    for planned_zone, definition in expanded_definitions:
        source_zone = str(definition.get("parent_zone_id", planned_zone))
        is_partition = planned_zone != source_zone
        independent_zone = (
            next_zone_id(source_zone) if is_partition else planned_zone
        )
        rack_ids = set(definition.get("rack_ids", []))
        if not is_partition:
            for position in positions:
                if str(position.get("rack_id", "")) in rack_ids:
                    position["planned_storage_type"] = definition.get(
                        "storage_type", position.get(
                            "planned_storage_type", "STANDARD"
                        )
                    )
                    position["planned_zone_id"] = (
                        f"{independent_zone}_{position['planned_storage_type']}"
                    )
                    position.pop("parent_zone_id", None)
            for row in rows:
                if str(row.get("rack_id", "")) in rack_ids:
                    row["planned_storage_type"] = definition.get(
                        "storage_type", row.get(
                            "planned_storage_type", "STANDARD"
                        )
                    )
                    row["planned_zone_id"] = (
                        f"{independent_zone}_{row['planned_storage_type']}"
                    )
                    row.pop("parent_zone_id", None)
            promoted[independent_zone] = {
                "attributes": dict(definition.get("attributes", {})),
                "storage_type": definition.get("storage_type", "STANDARD"),
                "rack_ids": list(definition.get("rack_ids", [])),
                "hierarchy_path": [
                    {**step, "zone_id": independent_zone}
                    for step in definition.get("hierarchy_path", [])
                ],
            }
            continue
        zone_attributes = {
            **local_attributes.get(source_zone, {}),
            **definition.get("attributes", {}),
        }
        if zone_attributes:
            local_attributes.setdefault(independent_zone, {}).update(
                zone_attributes
            )
        old_rack_prefixes = set()
        for position in positions:
            if str(position.get("rack_id", "")) not in rack_ids:
                continue
            old_rack_prefix = "/".join(
                str(position.get("storage_location_address", "")).split("/")[:3]
            )
            if old_rack_prefix:
                old_rack_prefixes.add(old_rack_prefix)
            for field in ("static_address", "storage_location_address"):
                value = position.get(field)
                if isinstance(value, str) and "/" in value:
                    position[field] = independent_zone + value[value.find("/"):]
            position["zone_id"] = independent_zone
            position["planned_zone_id"] = (
                f"{independent_zone}_{position.get('planned_storage_type', 'STANDARD')}"
            )
            position["generated_attribute_zone_id"] = independent_zone
            position.pop("parent_zone_id", None)
            zone_assignments[str(position.get("waypoint", ""))] = independent_zone

        for old_prefix in old_rack_prefixes:
            new_prefix = independent_zone + old_prefix[old_prefix.find("/"):]
            for path, values in list(local_attributes.items()):
                if path == old_prefix or path.startswith(old_prefix + "/"):
                    new_path = new_prefix + path[len(old_prefix):]
                    local_attributes[new_path] = local_attributes.pop(path)

        for row in rows:
            if str(row.get("rack_id", "")) not in rack_ids:
                continue
            def rename_address(value):
                if isinstance(value, str) and "/" in value:
                    return independent_zone + value[value.find("/"):]
                return value

            row["zone_id"] = independent_zone
            row["planned_storage_type"] = definition.get(
                "storage_type", row.get("planned_storage_type", "STANDARD")
            )
            row["planned_zone_id"] = (
                f"{independent_zone}_{row.get('planned_storage_type', 'STANDARD')}"
            )
            row["generated_attribute_zone_id"] = independent_zone
            row.pop("parent_zone_id", None)
            for field in ("static_address", "storage_location_address"):
                row[field] = rename_address(row.get(field, ""))
            for field in (
                "occupied_static_addresses",
                "occupied_storage_location_addresses",
            ):
                row[field] = [
                    rename_address(value) for value in row.get(field, [])
                ]
            for unit in row.get("occupied_handling_units", []):
                for field in ("static_address", "storage_location_address"):
                    if field in unit:
                        unit[field] = rename_address(unit[field])

        promoted[independent_zone] = {
            "attributes": dict(definition.get("attributes", {})),
            "storage_type": definition.get("storage_type", "STANDARD"),
            "rack_ids": list(definition.get("rack_ids", [])),
            "hierarchy_path": [
                {**step, "zone_id": independent_zone}
                for step in definition.get("hierarchy_path", [])
            ],
        }

    for position in positions:
        waypoint = str(position.get("waypoint", ""))
        if waypoint:
            zone_assignments[waypoint] = str(position.get("zone_id", ""))
    active_zones = {
        str(position.get("zone_id", "")) for position in positions
        if str(position.get("zone_id", ""))
    }
    for path in list(local_attributes):
        if path.split("/", 1)[0] not in active_zones:
            local_attributes.pop(path)
    return promoted


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


def combined_occupied_dynamic_address(row: dict) -> str:
    """Return one compact display address for every position occupied by a SKU."""
    canonical = str(row.get("dynamic_address", ""))
    occupied = row.get("occupied_handling_units") or []
    if len(occupied) <= 1:
        return canonical

    dynamic_level = str(row.get("dynamic_address_level", ""))
    buffer_model = dynamic_level in {"shelf_slot", "handling_unit"}
    if str(row.get("handling_unit_type", "")) == "AMR shelf":
        unit_ids = {
            str(
                location.get("handling_unit_id")
                or row.get("handling_unit_id", "")
            )
            for location in occupied
        }
        coordinates = {
            (
                int(location.get("storage_level") or 1),
                int(location.get("storage_slot") or 1),
            )
            for location in occupied
        }
        levels = sorted({level for level, _slot in coordinates})
        slots = sorted({slot for _level, slot in coordinates})
        rectangle = {
            (level, slot)
            for level in levels
            for slot in slots
        }
        if len(unit_ids) == 1 and coordinates == rectangle:
            anchor, _level = build_dynamic_address(
                str(row.get("zone_id", "")),
                str(row.get("aisle_id", "")),
                str(row.get("static_bay_id", "")),
                levels[0],
                slots[0],
                "AMR shelf",
                next(iter(unit_ids)),
                buffer_model=buffer_model,
            )
            base = anchor.rsplit("/L", 1)[0]
            level_label = f"L{levels[0]:02d}" + "".join(
                f",{level:02d}" for level in levels[1:]
            )
            slot_label = f"S{slots[0]:02d}" + "".join(
                f",{slot:02d}" for slot in slots[1:]
            )
            return f"{base}/{level_label}/{slot_label}"

    addresses = []
    for location in occupied:
        try:
            address, _level = build_dynamic_address(
                str(row.get("zone_id", "")),
                str(row.get("aisle_id", "")),
                str(row.get("static_bay_id", "")),
                int(location.get("storage_level") or 1),
                int(location.get("storage_slot") or 1),
                str(row.get("handling_unit_type", "")),
                str(
                    location.get("handling_unit_id")
                    or row.get("handling_unit_id", "")
                ),
                buffer_model=buffer_model,
            )
        except (TypeError, ValueError):
            return canonical
        if address and address not in addresses:
            addresses.append(address)

    if len(addresses) <= 1:
        return addresses[0] if addresses else canonical

    split_addresses = [address.split("/") for address in addresses]
    common_length = 0
    for segments in zip(*split_addresses):
        if len(set(segments)) != 1:
            break
        common_length += 1
    if common_length:
        prefix = "/".join(split_addresses[0][:common_length])
        suffixes = [
            "/".join(parts[common_length:])
            for parts in split_addresses
        ]
        return f"{prefix}/[{', '.join(suffixes)}]"
    return " | ".join(addresses)
