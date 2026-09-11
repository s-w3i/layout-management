"""Shared physical slot-allocation engine used by slotting strategies."""

from __future__ import annotations

import math
from collections import Counter, defaultdict

import numpy as np

from .zone_workload import balanced_zone_candidates

from ..affinity import AffinityAnalysis
from ..attributes import (
    OVERSIZE_CAPABLE_KEY,
    PHYSICAL_ATTRIBUTE_KEYS,
    PHYSICAL_DIMENSION_KEYS,
    PHYSICAL_WEIGHT_KEY,
    STANDARD_STORAGE_DEFAULTS,
    requires_oversize_capable,
)
from ..storage_planning import (
    build_dynamic_address,
    combined_occupied_dynamic_address,
    derive_zone_storage_types,
)
from ..slotting_rules import (
    allocation_candidate_key,
    apply_rack_frequency_ranks,
    physical_allocation_bucket,
    overweight_storage_level,
    required_slot_footprint,
)
from .affinity_support import (
    affinity_layout_metrics,
    affinity_physical_signature,
    affinity_placement_order,
    build_affinity_neighbors,
    build_order_membership_masks,
)


SHARED_HARD_RULE_PROFILE = "map_authoritative_warehouse_feasibility/v4"
SHARED_HARD_RULES = (
    "configured_handling_unit_and_buffer_capacity",
    "unique_storage_position_occupancy",
    "amr_cumulative_whole_rack_weight_capacity",
    "configured_map_attribute_compatibility",
    "known_oversize_contiguous_footprint",
    "known_overweight_required_level",
    "oversize_zone_weight_capacity_from_compatible_sku_data",
    "warehouse_wide_constrained_inventory_footprint_reservation",
    "no_automatic_zone_split_or_non_weight_attribute_mutation",
)


def expand_quantity_slot_loads(sku_rows: list[dict]) -> tuple[list[dict], dict]:
    """Expand stock targets into independently placeable slot loads.

    Quantity loads are independent records so they can be spread across racks.
    ``slots_per_unit`` remains a physical footprint on each load and is not
    expanded a second time; the allocator still reserves that footprint as one
    contiguous block.
    """
    expanded: list[dict] = []
    legacy_load_counts: dict[str, int] = defaultdict(int)
    quantity_enabled_skus = 0
    zero_required_skus = 0
    for source in sku_rows:
        row = dict(source)
        raw_required_slots = row.get("required_slots")
        if raw_required_slots in (None, ""):
            sku = str(row.get("sku", ""))
            legacy_load_counts[sku] += 1
            legacy_id = (
                sku if legacy_load_counts[sku] == 1
                else f"{sku}#LEGACY{legacy_load_counts[sku]:03d}"
            )
            row.setdefault("inventory_load_id", legacy_id)
            row.setdefault("quantity_load_index", 1)
            row.setdefault("quantity_load_count", 1)
            row.setdefault("quantity_ea", row.get("total_required_ea", ""))
            expanded.append(row)
            continue
        try:
            required_slots = max(0, int(math.ceil(float(raw_required_slots))))
            slots_per_unit = max(
                1, int(math.ceil(float(row.get("slots_per_unit") or 1)))
            )
            total_required = max(
                0, int(math.ceil(float(row.get("total_required_ea") or 0)))
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"SKU {row.get('sku', '')} has invalid stock slot quantities"
            ) from exc
        if required_slots == 0 or total_required == 0:
            zero_required_skus += 1
            continue
        load_count = max(1, math.ceil(required_slots / slots_per_unit))
        quantity_enabled_skus += 1
        base_quantity, remainder = divmod(total_required, load_count)
        width = max(3, len(str(load_count)))
        for index in range(1, load_count + 1):
            load = dict(row)
            load["inventory_load_id"] = (
                f"{row.get('sku', '')}#Q{index:0{width}d}"
            )
            load["quantity_load_index"] = index
            load["quantity_load_count"] = load_count
            load["quantity_ea"] = base_quantity + (index <= remainder)
            load["required_slot_count"] = required_slots
            expanded.append(load)
    return expanded, {
        "source_sku_count": len(sku_rows),
        "inventory_load_count": len(expanded),
        "quantity_enabled_sku_count": quantity_enabled_skus,
        "zero_required_sku_count": zero_required_skus,
    }


def allocate(
    service,
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
    if not isinstance(zone_workload_enabled, bool):
        raise ValueError("zone_workload_enabled must be a boolean")
    if zone_workload_enabled and strategy == "ctbsa":
        raise ValueError("C&TBSA zone balancing belongs in the traffic planner")
    if levels_per_rack < 1 or slots_per_level < 1:
        raise ValueError("levels and slots per level must be at least 1")
    if maximum_same_sku_slots_per_rack is None:
        maximum_same_sku_slots_per_rack = levels_per_rack * slots_per_level
    if (
        isinstance(maximum_same_sku_slots_per_rack, bool)
        or not isinstance(maximum_same_sku_slots_per_rack, int)
        or maximum_same_sku_slots_per_rack < 1
    ):
        raise ValueError("maximum same-SKU slots per rack must be a positive integer")
    if strategy not in {"basic", "abc_affinity", "ctbsa"}:
        raise ValueError(f"unsupported slotting strategy: {strategy}")
    if not 0.0 <= affinity_weight <= 1.0:
        raise ValueError("affinity weight must be between 0 and 1")
    if minimum_shared_store_days < 0:
        raise ValueError("minimum shared store-days cannot be negative")
    if not 0.0 <= minimum_affinity_score <= 1.0:
        raise ValueError("minimum affinity score must be between 0 and 1")
    if maximum_service_distance_increase < 0:
        raise ValueError("maximum service-distance increase cannot be negative")
    sku_rows, quantity_summary = expand_quantity_slot_loads(sku_rows)
    affinity_enabled = strategy == "abc_affinity"
    if affinity_enabled and affinity_analysis is None:
        raise ValueError("ABC + affinity strategy requires affinity analysis")
    level_name, racks, workstation_count, unreachable_count = service.rack_distances(building)
    level = building["levels"][level_name]
    coordinate_scale = service._distance_scale(building, level)
    # Route reachability affects preference, not whether physical storage exists.
    # Unreachable racks sort last but remain usable when capacity is needed.
    usable_racks = list(racks)
    buffer_model = storage_layout is not None
    if buffer_model:
        expected_handling_unit = str(storage_layout.handling_unit_type)
        if handling_unit_type != expected_handling_unit:
            raise ValueError(
                f"grid project requires handling unit {expected_handling_unit}, "
                f"not {handling_unit_type}"
            )
        if levels_per_rack != int(storage_layout.levels_per_rack) or (
            slots_per_level != int(storage_layout.slots_per_level)
        ):
            raise ValueError(
                "rack capacity must match the buffers generated in the grid project"
            )
        service.attributes.set_machine_carrying_capacity(
            getattr(storage_layout, "machine_carrying_capacity", None),
            handling_unit_type,
        )
        roots_by_waypoint = {}
        for buffer in storage_layout.buffers:
            waypoint = str(buffer.get("grid_waypoint", ""))
            root = str(buffer.get("buffer_id", "")).split("/L", 1)[0]
            if waypoint and root:
                roots_by_waypoint[waypoint] = root
        for rack in racks:
            if rack["waypoint"] not in roots_by_waypoint:
                raise ValueError(
                    f"rack {rack['waypoint']} has no generated storage buffer"
                )
            rack["static_bay_id"] = roots_by_waypoint[rack["waypoint"]]
    else:
        # Calls without a grid project retain the legacy unbounded machine
        # envelope and continue to classify against slot capacities only.
        service.attributes.set_machine_carrying_capacity(None, handling_unit_type)
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
    zone_assignments = dict(zone_assignments or {})
    service.apply_zone_local_aisles(building, racks, zone_assignments, zone_id)
    catalog = service.attributes.normalize_catalog(attribute_catalog)
    # Map-authoritative mode: zone profiles come only from the saved map.
    # SKU requirements never create grouping keys or generated subzones.
    exact_grouping_keys = ()
    valid_paths = service.attributes.hierarchy_paths(
        racks, levels_per_rack, slots_per_level
    )
    local_attributes = service.attributes.validate_location_attributes(
        location_attributes, catalog, valid_paths
    )
    configured_map_attribute_keys: set[str] = set()
    configured_zones = {str(rack["zone_id"]) for rack in racks}
    for configured_zone in configured_zones:
        effective_zone, _sources = service.attributes.effective_attributes(
            configured_zone, local_attributes
        )
        configured_map_attribute_keys.update(effective_zone)

    auto_adjusted_oversize_zone_weight_capacities: dict[str, dict] = {}
    if PHYSICAL_WEIGHT_KEY in configured_map_attribute_keys:
        normalized_sku_requirements = []
        for sku in sku_rows:
            requirements = sku.get("sku_requirements")
            if not isinstance(requirements, dict):
                requirements = service.attributes.requirements_from_row(
                    sku, catalog
                )
            else:
                requirements = service.attributes.validate_requirements(
                    requirements, catalog
                )
            normalized_sku_requirements.append(requirements)
        for configured_zone in sorted(configured_zones):
            effective_zone, _sources = service.attributes.effective_attributes(
                configured_zone, local_attributes
            )
            if effective_zone.get(OVERSIZE_CAPABLE_KEY) is not True:
                continue
            compatible_weights = []
            for requirements in normalized_sku_requirements:
                zone_requirements = {
                    key: value
                    for key, value in requirements.items()
                    if key in configured_map_attribute_keys
                    and key not in PHYSICAL_ATTRIBUTE_KEYS
                    and key != OVERSIZE_CAPABLE_KEY
                }
                if service.attributes.compatibility_issues(
                    zone_requirements, effective_zone, catalog
                ):
                    continue
                try:
                    weight = float(requirements.get(PHYSICAL_WEIGHT_KEY, 0))
                except (TypeError, ValueError):
                    continue
                if math.isfinite(weight) and weight > 0:
                    compatible_weights.append(weight)
            if not compatible_weights:
                continue
            required_weight = max(compatible_weights)
            current_weight = service.attributes.physical_capacity(
                effective_zone, PHYSICAL_WEIGHT_KEY
            )
            if required_weight <= current_weight:
                continue
            local_attributes.setdefault(configured_zone, {})[
                PHYSICAL_WEIGHT_KEY
            ] = required_weight
            auto_adjusted_oversize_zone_weight_capacities[configured_zone] = {
                "previous_max_item_weight": current_weight,
                "updated_max_item_weight": required_weight,
            }

    asrs_buffer_coordinates = {
        (
            str(item.get("grid_waypoint", "")),
            int(item.get("level", 0)),
            int(item.get("slot", 0)),
        )
        for item in (storage_layout.buffers if buffer_model else [])
        if item.get("buffer_level") == "slot"
    }
    positions = []
    movable_unit_number = 0
    for rack_rank, rack in enumerate(usable_racks, start=1):
        shelf_unit = f"SHELF_{rack_rank:03d}"
        for level_number in range(1, levels_per_rack + 1):
            for slot_number in range(1, slots_per_level + 1):
                if buffer_model and storage_layout.buffer_level == "slot" and (
                    rack["waypoint"], level_number, slot_number
                ) not in asrs_buffer_coordinates:
                    continue
                if handling_unit_type == "AMR shelf":
                    handling_unit_id = shelf_unit
                else:
                    movable_unit_number += 1
                    handling_unit_id = f"{unit_prefix}_{movable_unit_number:03d}"
                rack_zone = rack["zone_id"]
                storage_location_address = (
                    f"{rack_zone}/{rack['aisle_id']}/{rack['static_bay_id']}"
                    f"/L{level_number:02d}/S{slot_number:02d}"
                )
                buffer_id = (
                    rack["static_bay_id"]
                    if handling_unit_type == "AMR shelf"
                    else f"{rack['static_bay_id']}/L{level_number:02d}/S{slot_number:02d}"
                )
                static_address = (
                    f"{rack_zone}/{rack['aisle_id']}/{buffer_id}"
                    if buffer_model else storage_location_address
                )
                dynamic_address, dynamic_address_level = build_dynamic_address(
                    rack_zone,
                    rack["aisle_id"],
                    rack["static_bay_id"],
                    level_number,
                    slot_number,
                    handling_unit_type,
                    handling_unit_id,
                    buffer_model=buffer_model,
                )
                effective_attributes = service.attributes.effective_attributes(
                    storage_location_address, local_attributes
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
                    "buffer_id": buffer_id,
                    "buffer_level": (
                        storage_layout.buffer_level if buffer_model
                        else ("grid" if handling_unit_type == "AMR shelf" else "slot")
                    ),
                    "static_address": static_address,
                    "storage_location_address": storage_location_address,
                    "dynamic_address": dynamic_address,
                    "effective_location_attributes": effective_attributes,
                    "storage_area_type": (
                        "OVERSIZE"
                        if service.attributes.is_oversize_location(effective_attributes)
                        else "STANDARD"
                    ),
                })

    physical_enabled = service.attributes.has_physical_catalog(catalog)
    auto_plan_oversize = False
    for position in positions:
        storage_type = (
            "OVERSIZE"
            if service.attributes.is_oversize_location(
                position["effective_location_attributes"]
            )
            else "STANDARD"
        )
        position["planned_storage_type"] = storage_type
        position["planned_zone_id"] = position["zone_id"]
    for position in positions:
        position["planned_zone_base_id"] = position["planned_zone_id"]
    # The legacy strict compatibility mode models oversize as a contiguous
    # occupancy requirement instead of a separately planned placement class.
    physical_grouping_enabled = physical_enabled and not strict_compatibility

    def physical_group_rank(row):
        if not physical_grouping_enabled:
            return 0
        requirements = row.get("sku_requirements")
        if not isinstance(requirements, dict):
            requirements = service.attributes.requirements_from_row(row, catalog)
        profile = service.attributes.physical_profile(requirements)
        return int(profile["storage_class"] != "STANDARD")

    class_rank = {"A": 0, "B": 1, "C": 2}
    ctbsa_target_racks = dict(ctbsa_target_racks or {})
    ctbsa_rank_by_sku = dict(ctbsa_rank_by_sku or {})
    if strategy == "ctbsa":
        base_sorted_skus = sorted(
            sku_rows,
            key=lambda row: (
                ctbsa_rank_by_sku.get(
                    str(row.get("inventory_load_id", "")),
                    ctbsa_rank_by_sku.get(str(row.get("sku", "")), 10**12),
                ),
                str(row.get("sku", "")),
                str(row.get("inventory_load_id", "")),
            ),
        )
    else:
        base_sorted_skus = sorted(
            sku_rows,
            key=lambda row: (
                # Place one load of every SKU before replenishment loads. This
                # discovers the compact system rack pool early enough for hot
                # quantity loads to spread without opening dedicated racks.
                class_rank.get(str(row.get("velocity_class", "")).upper(), 9),
                physical_group_rank(row),
                -float(row.get("pick_frequency") or 0),
                str(row.get("sku", "")),
                int(row.get("quantity_load_index") or 1),
            ),
        )
    abc_rank_by_id = {
        id(row): rank for rank, row in enumerate(base_sorted_skus, start=1)
    }
    affinity_neighbors: dict[str, list[tuple[str, float, float, int]]] = {}
    if affinity_enabled and affinity_analysis is not None:
        affinity_neighbors = precomputed_affinity_neighbors or (
            build_affinity_neighbors(
                affinity_analysis,
                {str(row.get("sku", "")) for row in base_sorted_skus},
                minimum_shared_store_days,
                minimum_affinity_score,
            )
        )
    order_membership_masks = {}
    if affinity_enabled and affinity_analysis is not None:
        order_membership_masks, _order_group_count = build_order_membership_masks(
            affinity_analysis,
            {str(row.get("sku", "")) for row in sku_rows},
        )
    logical_sorted_skus = (
        affinity_placement_order(
            base_sorted_skus,
            affinity_neighbors,
            affinity_weight,
            order_membership_masks,
            levels_per_rack * slots_per_level,
        )
        if affinity_enabled
        else base_sorted_skus
    )
    affinity_rank_by_id = {
        id(row): rank for rank, row in enumerate(logical_sorted_skus, start=1)
    }

    def strict_packing_priority(row):
        requirements = row.get("sku_requirements")
        if not isinstance(requirements, dict):
            requirements = service.attributes.requirements_from_row(row, catalog)
        profile = service.attributes.physical_profile(requirements)
        if profile["data_status"] != "COMPLETE":
            return (2, 0)
        footprint = required_slot_footprint(
            requirements,
            {"chilled": requirements.get("chilled", False),
             **STANDARD_STORAGE_DEFAULTS},
            levels_per_rack,
            slots_per_level,
        )
        area = footprint[0] * footprint[1] if footprint else 10**9
        overweight = profile["storage_class"] in {
            "OVERWEIGHT", "OVERSIZE_AND_OVERWEIGHT"
        }
        return (0 if overweight else 1 if area > 1 else 2, -area)

    if strict_compatibility:
        sorted_skus = sorted(logical_sorted_skus, key=strict_packing_priority)
    elif auto_plan_oversize and physical_enabled:
        def physical_invariant_order(row):
            requirements = row.get("sku_requirements")
            if not isinstance(requirements, dict):
                requirements = service.attributes.requirements_from_row(
                    row, catalog
                )
            is_exception = (
                service.attributes.physical_profile(requirements)[
                    "storage_class"
                ] != "STANDARD"
            )
            # Soft affinity ordering applies only to standard inventory.
            # Exceptions keep the shared deterministic ABC/physical ordering
            # used by Basic and by Traffic's feasibility seed.
            rank = (
                abc_rank_by_id[id(row)]
                if is_exception else affinity_rank_by_id[id(row)]
            )
            return (is_exception, rank)

        sorted_skus = sorted(
            logical_sorted_skus,
            key=physical_invariant_order,
        )
    else:
        sorted_skus = logical_sorted_skus

    sku_group_order = {}
    for row in sorted_skus:
        sku_group_order.setdefault(str(row.get("sku", "")), len(sku_group_order))
    sorted_skus = sorted(
        sorted_skus,
        key=lambda row: (
            sku_group_order[str(row.get("sku", ""))],
            int(row.get("quantity_load_index") or 1),
            str(row.get("inventory_load_id", "")),
        ),
    )
    sku_load_counts = Counter(str(row.get("sku", "")) for row in sorted_skus)

    def position_key(position: dict) -> tuple[str, int, int]:
        return (
            str(position["rack_id"]),
            int(position["level"]),
            int(position["slot"]),
        )

    # Reserve physically constrained inventory before strategy placement.  The
    # strategy still controls the order and preferred location of ordinary
    # one-slot SKUs, but cannot consume cells required by an oversize,
    # overweight, or physically unverified SKU.  This avoids a greedy early
    # placement making a feasible warehouse layout appear infeasible.
    position_by_key = {position_key(item): item for item in positions}
    constrained_records = []
    constrained_inventory_total = 0
    for strategy_rank, sku in enumerate(sorted_skus, start=1):
        requirements = sku.get("sku_requirements")
        if not isinstance(requirements, dict):
            requirements = service.attributes.requirements_from_row(sku, catalog)
        else:
            requirements = service.attributes.validate_requirements(
                requirements, catalog
            )
        profile = (
            service.attributes.physical_profile(requirements)
            if physical_enabled else None
        )
        if not profile or not requires_oversize_capable(profile):
            continue
        constrained_inventory_total += 1
        if len(constrained_records) >= len(positions):
            # At most one constrained load can anchor per physical position.
            # Additional loads are still emitted as unassigned records below,
            # but duplicating every candidate footprint for them wastes memory.
            continue
        physical_class = str(profile.get("storage_class", "")).upper()
        overweight = physical_class in {
            "OVERWEIGHT", "OVERSIZE_AND_OVERWEIGHT"
        }
        known_volumetric_oversize = bool(
            all(key in profile.get("values", {}) for key in PHYSICAL_DIMENSION_KEYS)
            and service.attributes.is_volumetric_oversize(profile)
        )
        placements = []
        seen_footprints = set()
        for candidate in positions:
            target_rack = ctbsa_target_racks.get(
                str(sku.get("inventory_load_id", "")),
                ctbsa_target_racks.get(str(sku.get("sku", ""))),
            )
            if target_rack and str(candidate.get("rack_id", "")) != target_rack:
                continue
            effective = candidate["effective_location_attributes"]
            if effective.get(OVERSIZE_CAPABLE_KEY) is not True:
                continue
            if (
                overweight
                and not profile.get("weight_heuristic_disabled", False)
                and int(candidate["level"])
                != overweight_storage_level(levels_per_rack)
            ):
                continue
            scoped_requirements = {
                key: value for key, value in requirements.items()
                if key in configured_map_attribute_keys
            }
            if service.attributes.hard_compatibility_issues(
                scoped_requirements, effective, catalog
            ):
                continue
            if PHYSICAL_WEIGHT_KEY in configured_map_attribute_keys:
                raw_weight = requirements.get(PHYSICAL_WEIGHT_KEY)
                if raw_weight not in (None, ""):
                    try:
                        if float(raw_weight) > service.attributes.physical_capacity(
                            effective, PHYSICAL_WEIGHT_KEY
                        ):
                            continue
                    except (TypeError, ValueError):
                        continue
            missing_physical = [
                key for key in PHYSICAL_ATTRIBUTE_KEYS
                if key in requirements
                and key in configured_map_attribute_keys
                and key not in effective
            ]
            if missing_physical:
                continue
            footprint = (1, 1)
            if known_volumetric_oversize and all(
                key in effective for key in PHYSICAL_DIMENSION_KEYS
            ):
                footprint = required_slot_footprint(
                    requirements,
                    effective,
                    levels_per_rack - int(candidate["level"]) + 1,
                    slots_per_level - int(candidate["slot"]) + 1,
                )
                if footprint is None:
                    continue
            level_span, slot_span = footprint
            footprint_keys = tuple(
                (
                    str(candidate["rack_id"]),
                    level_number,
                    slot_number,
                )
                for level_number in range(
                    int(candidate["level"]),
                    int(candidate["level"]) + level_span,
                )
                for slot_number in range(
                    int(candidate["slot"]),
                    int(candidate["slot"]) + slot_span,
                )
            )
            if footprint_keys in seen_footprints or any(
                key not in position_by_key for key in footprint_keys
            ):
                continue
            occupied = [position_by_key[key] for key in footprint_keys]
            if any(
                service.attributes.hard_compatibility_issues(
                    scoped_requirements,
                    item["effective_location_attributes"],
                    catalog,
                )
                for item in occupied
            ):
                continue
            if any(
                service.attributes.physical_capacity(
                    item["effective_location_attributes"], key
                )
                < service.attributes.physical_capacity(effective, key)
                for item in occupied
                for key in PHYSICAL_DIMENSION_KEYS
            ):
                continue
            seen_footprints.add(footprint_keys)
            placements.append(footprint_keys)
        placements.sort(key=lambda footprint_keys: (
            float(position_by_key[footprint_keys[0]]["distance_m"]),
            int(position_by_key[footprint_keys[0]]["rack_rank"]),
            int(position_by_key[footprint_keys[0]]["level"]),
            int(position_by_key[footprint_keys[0]]["slot"]),
        ))
        constrained_records.append({
            "row_id": id(sku),
            "sku": str(sku.get("sku", "")),
            "spread_quantity": sku_load_counts[str(sku.get("sku", ""))] > 1,
            "strategy_rank": strategy_rank,
            "placements": placements,
            "largest_footprint": max(
                (len(value) for value in placements), default=0
            ),
        })

    plannable_records = [
        record for record in constrained_records if record["placements"]
    ]
    reserved_footprints_by_row_id: dict[int, tuple] = {}
    occupied_reservations: set[tuple[str, int, int]] = set()
    quantity_reservations_by_sku: dict[str, list[tuple]] = defaultdict(list)
    search_nodes = 0
    search_node_limit = 250_000

    def reservation_spread_key(record: dict, footprint: tuple) -> tuple:
        rack_id = footprint[0][0]
        opened_racks = {position[0] for position in occupied_reservations}
        new_rack_penalty = int(rack_id not in opened_racks)
        if not record["spread_quantity"]:
            return (new_rack_penalty, 0, 0)
        sku_footprints = quantity_reservations_by_sku[record["sku"]]
        rack_count = sum(
            len(prior) for prior in sku_footprints if prior and prior[0][0] == rack_id
        )
        prior_positions = {
            position
            for prior in sku_footprints
            for position in prior
            if position[0] == rack_id
        }
        adjacent = any(
            existing[0] == position[0]
            and abs(existing[1] - position[1])
            + abs(existing[2] - position[2]) == 1
            for existing in prior_positions
            for position in footprint
        )
        same_level_adjacent = any(
            existing[0] == position[0]
            and existing[1] == position[1]
            and abs(existing[2] - position[2]) == 1
            for existing in prior_positions
            for position in footprint
        )
        return (new_rack_penalty, -rack_count, -int(same_level_adjacent), -int(adjacent))

    def reserve_all(remaining: tuple[dict, ...]) -> bool:
        nonlocal search_nodes
        search_nodes += 1
        if search_nodes > search_node_limit:
            return False
        if not remaining:
            return True
        viable_by_record = []
        for record in remaining:
            viable = [
                footprint for footprint in record["placements"]
                if not occupied_reservations.intersection(footprint)
                and (
                    sum(
                        len(prior)
                        for prior in quantity_reservations_by_sku[record["sku"]]
                        if prior and prior[0][0] == footprint[0][0]
                    ) + len(footprint)
                    <= maximum_same_sku_slots_per_rack
                )
            ]
            if not viable:
                return False
            viable_by_record.append((len(viable), record, viable))
        _count, selected, viable = min(
            viable_by_record,
            key=lambda value: (
                value[0],
                -value[1]["largest_footprint"],
                value[1]["strategy_rank"],
            ),
        )
        next_remaining = tuple(
            record for record in remaining if record is not selected
        )
        for footprint in sorted(
            viable, key=lambda value: reservation_spread_key(selected, value)
        ):
            occupied_reservations.update(footprint)
            reserved_footprints_by_row_id[selected["row_id"]] = footprint
            if selected["spread_quantity"]:
                quantity_reservations_by_sku[selected["sku"]].append(footprint)
            if reserve_all(next_remaining):
                return True
            if selected["spread_quantity"]:
                quantity_reservations_by_sku[selected["sku"]].pop()
            reserved_footprints_by_row_id.pop(selected["row_id"], None)
            occupied_reservations.difference_update(footprint)
        return False

    minimum_constrained_slots = sum(
        min(len(footprint) for footprint in record["placements"])
        for record in plannable_records
    )
    provably_insufficient_constrained_capacity = (
        constrained_inventory_total > len(constrained_records)
        or minimum_constrained_slots > len(positions)
    )
    complete_constrained_plan = (
        False
        if provably_insufficient_constrained_capacity
        else reserve_all(tuple(plannable_records))
    )
    if not complete_constrained_plan:
        # Deterministic best-effort fallback for genuinely insufficient maps or
        # unusually large searches: reserve the hardest feasible items first.
        reserved_footprints_by_row_id.clear()
        occupied_reservations.clear()
        quantity_reservations_by_sku.clear()
        fallback_order = sorted(
            plannable_records,
            key=lambda record: (
                len(record["placements"]),
                -record["largest_footprint"],
                record["strategy_rank"],
            ),
        )
        for record in fallback_order:
            viable = [
                value for value in record["placements"]
                if not occupied_reservations.intersection(value)
                and (
                    sum(
                        len(prior)
                        for prior in quantity_reservations_by_sku[record["sku"]]
                        if prior and prior[0][0] == value[0][0]
                    ) + len(value)
                    <= maximum_same_sku_slots_per_rack
                )
            ]
            footprint = min(
                viable,
                key=lambda value: reservation_spread_key(record, value),
                default=None,
            )
            if footprint is None:
                continue
            reserved_footprints_by_row_id[record["row_id"]] = footprint
            occupied_reservations.update(footprint)
            if record["spread_quantity"]:
                quantity_reservations_by_sku[record["sku"]].append(footprint)
    reserved_owner_by_position = {
        key: row_id
        for row_id, footprint in reserved_footprints_by_row_id.items()
        for key in footprint
    }
    generated_attribute_zones = {}
    finite_service_distances = [
        float(rack["distance_m"])
        for rack in racks
        if math.isfinite(float(rack["distance_m"]))
    ]
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
        "static_address", "storage_location_address", "buffer_id",
        "buffer_level", "rmf_grid_address", "zone_id", "aisle_id",
        "static_bay_id", "rack_id", "rack_waypoint", "pickup_dispenser_id",
        "rack_vertex_index", "rack_rank", "handling_unit_type",
        "handling_unit_id", "dynamic_address_level", "dynamic_address",
        "storage_level", "storage_slot", "workstations_evaluated",
        "average_workstation_distance_m", "routing_status",
        "storage_area_type",
        "planned_zone_id", "planned_storage_type",
        "generated_attribute_zone_id",
        "rack_frequency_rank", "rack_pick_frequency",
        "rack_frequency_share", "rack_cumulative_frequency_share",
        "rack_velocity_class",
    )
    available_positions = list(positions)
    zone_capacities = defaultdict(int)
    zone_workloads = defaultdict(float)
    for position in positions:
        zone_capacities[str(position["zone_id"])] += 1
    # Each replica carries its quantity share of logical SKU demand.
    quantity_totals = defaultdict(float)
    quantity_counts = defaultdict(int)
    quantity_weights = {}
    for row in sorted_skus:
        raw_quantity = row.get("quantity_ea")
        quantity = max(0.0, float(raw_quantity if raw_quantity not in (None, "") else 1))
        quantity_weights[id(row)] = quantity
        quantity_totals[str(row.get("sku", ""))] += quantity
        quantity_counts[str(row.get("sku", ""))] += 1
    rack_state: dict[str, dict] = {}
    rack_max_weight = None
    if handling_unit_type == "AMR shelf":
        raw_rack_weight = (
            getattr(storage_layout, "machine_carrying_capacity", {}).get(
                PHYSICAL_WEIGHT_KEY
            )
            if storage_layout is not None else None
        )
        if raw_rack_weight in (None, ""):
            raw_rack_weight = service.attributes.machine_carrying_capacity.get(
                PHYSICAL_WEIGHT_KEY
            )
        try:
            rack_max_weight = float(raw_rack_weight)
        except (TypeError, ValueError):
            rack_max_weight = None
        if rack_max_weight is not None and (
            not math.isfinite(rack_max_weight) or rack_max_weight <= 0
        ):
            rack_max_weight = None
    assigned_affinity_positions: dict[str, dict] = {}
    quantity_rack_counts: dict[str, dict[str, int]] = defaultdict(
        lambda: defaultdict(int)
    )
    quantity_positions_by_sku: dict[str, list[tuple[str, int, int]]] = (
        defaultdict(list)
    )
    for placement_rank, sku in enumerate(sorted_skus, start=1):
        planned_footprint = reserved_footprints_by_row_id.get(id(sku))
        planned_footprint_set = set(planned_footprint or ())
        planned_anchor = planned_footprint[0] if planned_footprint else None
        abc_frequency_rank = abc_rank_by_id.get(id(sku), placement_rank)
        affinity_placement_rank = affinity_rank_by_id.get(
            id(sku), placement_rank
        )
        sku_rank = abc_frequency_rank
        current_sku = str(sku.get("sku", ""))
        zone_demand = max(0.0, float(sku.get("pick_frequency") or 0)) * (
            quantity_weights[id(sku)] / quantity_totals[current_sku]
            if quantity_totals[current_sku] else 1 / quantity_counts[current_sku]
        )
        current_order_mask = order_membership_masks.get(current_sku, 0)
        requirements = sku.get("sku_requirements")
        if not isinstance(requirements, dict):
            requirements = service.attributes.requirements_from_row(sku, catalog)
        else:
            requirements = service.attributes.validate_requirements(
                requirements, catalog
            )
        profile = (
            service.attributes.physical_profile(requirements)
            if physical_enabled
            else {
                "data_status": str(sku.get("physical_data_status", "NOT_EVALUATED")),
                "storage_class": str(sku.get("physical_storage_class", "NOT_EVALUATED")),
                "missing_fields": [],
                "values": {},
            }
        )
        try:
            ergonomic_sku_weight = float(
                profile.get("values", {}).get(PHYSICAL_WEIGHT_KEY, 0)
            )
        except (TypeError, ValueError):
            ergonomic_sku_weight = 0.0
        try:
            load_quantity = float(sku.get("quantity_ea") or 1)
        except (TypeError, ValueError):
            load_quantity = 1.0
        inventory_load_weight = (
            ergonomic_sku_weight * load_quantity
            if math.isfinite(ergonomic_sku_weight)
            and ergonomic_sku_weight > 0
            and math.isfinite(load_quantity)
            and load_quantity > 0
            else None
        )
        ergonomic_preference_enabled = bool(
            ergonomic_weight_heuristic
            and ergonomic_sku_weight > 0
        )
        ergonomic_preferred_level = (
            min(2, levels_per_rack)
            if physical_grouping_enabled
            and str(profile.get("storage_class", "")).upper()
            in {"OVERWEIGHT", "OVERSIZE_AND_OVERWEIGHT"}
            and not profile.get("weight_heuristic_disabled", False)
            else (levels_per_rack + 1) / 2.0
        )
        exception_inventory = bool(
            physical_enabled
            and str(profile.get("storage_class", "STANDARD")).upper()
            != "STANDARD"
        )
        known_volumetric_oversize = bool(
            physical_enabled
            and all(
                key in profile.get("values", {})
                for key in PHYSICAL_DIMENSION_KEYS
            )
            and service.attributes.is_volumetric_oversize(profile)
        )
        position = None
        occupied_positions: list[dict] = []
        compatibility_status = "NOT_EVALUATED"
        mismatch_details: list[str] = []
        auto_overrides: dict = {}
        affinity_neighbors_used: list[str] = []
        affinity_weight_sum = 0.0
        weighted_affinity_distance = 0.0
        affinity_same_bay_fraction = 0.0
        abc_grouping_cost = 0.0
        affinity_bay_cost = 0.0
        combined_location_score = None
        if available_positions:
            available_lookup = {
                (
                    position["rack_id"],
                    int(position["level"]),
                    int(position["slot"]),
                ): position
                for position in available_positions
            }
            hard_candidates = []
            hard_issues: list[str] = []
            for index, candidate in enumerate(available_positions):
                candidate_position_key = position_key(candidate)
                reservation_owner = reserved_owner_by_position.get(
                    candidate_position_key
                )
                if reservation_owner not in (None, id(sku)):
                    continue
                if planned_anchor and candidate_position_key != planned_anchor:
                    continue
                effective = dict(candidate["effective_location_attributes"])
                target_rack = ctbsa_target_racks.get(
                    str(sku.get("inventory_load_id", "")),
                    ctbsa_target_racks.get(current_sku),
                )
                if target_rack and str(candidate.get("rack_id", "")) != target_rack:
                    continue
                candidate_rack_state = rack_state.get(
                    str(candidate.get("rack_id", "")), {}
                )
                if (
                    rack_max_weight is not None
                    and inventory_load_weight is not None
                    and float(candidate_rack_state.get("cumulative_weight_kg", 0.0))
                    + inventory_load_weight
                    > rack_max_weight + 1e-9
                ):
                    issue = (
                        "AMR whole-rack cumulative weight would exceed "
                        f"{rack_max_weight:g} kg"
                    )
                    if issue not in hard_issues:
                        hard_issues.append(issue)
                    continue
                physical_class = str(profile.get("storage_class", "")).upper()
                overweight = physical_class in {
                    "OVERWEIGHT", "OVERSIZE_AND_OVERWEIGHT"
                }
                overweight_level_required = bool(
                    physical_grouping_enabled
                    and overweight
                    and not profile.get("weight_heuristic_disabled", False)
                )
                if (
                    overweight_level_required
                    and int(candidate["level"]) != overweight_storage_level(
                        levels_per_rack
                    )
                ):
                    issue = (
                        f"overweight inventory requires level "
                        f"{overweight_storage_level(levels_per_rack)}"
                    )
                    if issue not in hard_issues:
                        hard_issues.append(issue)
                    continue
                if (
                    strict_compatibility
                    and overweight
                    and candidate["level"]
                    != overweight_storage_level(levels_per_rack)
                ):
                    continue
                if (
                    requires_oversize_capable(profile)
                    and candidate["effective_location_attributes"].get(
                        OVERSIZE_CAPABLE_KEY
                    ) is not True
                ):
                    issue = (
                        "oversize or overweight inventory requires an "
                        "oversize-capable zone"
                    )
                    if issue not in hard_issues:
                        hard_issues.append(issue)
                    continue
                occupied_positions = [candidate]
                if (
                    auto_plan_oversize
                    and candidate.get("planned_storage_type")
                    != ("OVERSIZE" if exception_inventory else "STANDARD")
                ):
                    issue = (
                        f"{candidate.get('planned_storage_type')} zone cannot "
                        f"store {'exception' if exception_inventory else 'standard'} "
                        "inventory"
                    )
                    if issue not in hard_issues:
                        hard_issues.append(issue)
                    continue
                footprint_required = bool(
                    (
                        strict_compatibility
                        and profile["data_status"] == "COMPLETE"
                    )
                    or (
                        known_volumetric_oversize
                        and all(
                            key in candidate["effective_location_attributes"]
                            for key in PHYSICAL_DIMENSION_KEYS
                        )
                    )
                )
                if footprint_required:
                    issues = []
                    footprint = required_slot_footprint(
                        requirements,
                        candidate["effective_location_attributes"],
                        levels_per_rack - int(candidate["level"]) + 1,
                        slots_per_level - int(candidate["slot"]) + 1,
                    )
                    if footprint is None:
                        issues = [
                            "item dimensions do not fit this rack footprint "
                            "in any rotation"
                        ]
                    else:
                        level_span, slot_span = footprint
                        expected_coordinates = [
                            (level_number, slot_number)
                            for level_number in range(
                                candidate["level"],
                                candidate["level"] + level_span,
                            )
                            for slot_number in range(
                                candidate["slot"],
                                candidate["slot"] + slot_span,
                            )
                        ]
                        occupied_positions = [
                            available_lookup.get((
                                candidate["rack_id"],
                                level_number,
                                slot_number,
                            ))
                            for level_number, slot_number
                            in expected_coordinates
                        ]
                        actual_coordinates = [
                            (position["level"], position["slot"])
                            for position in occupied_positions
                            if position is not None
                        ]
                        if actual_coordinates != expected_coordinates:
                            issues = [
                                f"requires a contiguous {level_span} level × "
                                f"{slot_span} slot footprint"
                            ]
                        else:
                            if auto_plan_oversize:
                                expected_storage_type = (
                                    "OVERSIZE"
                                    if exception_inventory else "STANDARD"
                                )
                                if any(
                                    occupied.get("planned_storage_type")
                                    != expected_storage_type
                                    for occupied in occupied_positions
                                ):
                                    issues.append(
                                        f"requires a contiguous {level_span} level × "
                                        f"{slot_span} slot footprint entirely inside "
                                        f"the {expected_storage_type} zone"
                                    )
                                if any(
                                    occupied.get("planned_zone_id")
                                    != candidate.get("planned_zone_id")
                                    for occupied in occupied_positions
                                ):
                                    issues.append(
                                        "contiguous footprint cannot cross a "
                                        "planned-zone boundary"
                                    )
                            anchor_effective = candidate[
                                "effective_location_attributes"
                            ]
                            if any(
                                any(
                                    service.attributes.physical_capacity(
                                        occupied[
                                            "effective_location_attributes"
                                        ],
                                        key,
                                    )
                                    < service.attributes.physical_capacity(
                                        anchor_effective,
                                        key,
                                    )
                                    for key in PHYSICAL_DIMENSION_KEYS
                                )
                                for occupied in occupied_positions
                            ):
                                issues.append(
                                    "one or more positions in the contiguous "
                                    "footprint are smaller than the selected "
                                    "anchor slot"
                                )
                            missing_overrides = {}
                            for occupied in occupied_positions:
                                effective = dict(
                                    occupied["effective_location_attributes"]
                                )
                                scoped_requirements = {
                                    key: value
                                    for key, value in requirements.items()
                                    if key in configured_map_attribute_keys
                                }
                                issues.extend(
                                    service.attributes.hard_compatibility_issues(
                                        scoped_requirements,
                                        effective,
                                        catalog,
                                    )
                                )
                            if strict_compatibility:
                                generic_requirements = {
                                    key: value
                                    for key, value in requirements.items()
                                    if key not in PHYSICAL_DIMENSION_KEYS
                                    and not (
                                        overweight
                                        and key == PHYSICAL_WEIGHT_KEY
                                    )
                                }
                                for occupied in occupied_positions:
                                    effective = dict(
                                        occupied["effective_location_attributes"]
                                    )
                                    occupied_requirements = {
                                        key: value
                                        for key, value in generic_requirements.items()
                                        if key in configured_map_attribute_keys
                                    }
                                    issues.extend(
                                        service.attributes.compatibility_issues(
                                            occupied_requirements,
                                            effective,
                                            catalog,
                                        )
                                    )
                elif strict_compatibility:
                    missing_overrides = {}
                    effective = dict(candidate["effective_location_attributes"])
                    generic_requirements = {
                        key: value for key, value in requirements.items()
                        if key not in PHYSICAL_ATTRIBUTE_KEYS
                        and key in configured_map_attribute_keys
                    }
                    issues = service.attributes.compatibility_issues(
                        generic_requirements,
                        effective,
                        catalog,
                    )
                else:
                    missing_overrides = {}
                    effective = dict(candidate["effective_location_attributes"])
                    scoped_requirements = {
                        key: value
                        for key, value in requirements.items()
                        if key in configured_map_attribute_keys
                    }
                    issues = service.attributes.hard_compatibility_issues(
                        scoped_requirements,
                        effective,
                        catalog,
                    )
                occupied_position_keys = {
                    position_key(item) for item in occupied_positions
                    if item is not None
                }
                if any(
                    reserved_owner_by_position.get(key) not in (None, id(sku))
                    for key in occupied_position_keys
                ):
                    continue
                if planned_footprint_set and (
                    occupied_position_keys != planned_footprint_set
                ):
                    continue
                if (
                    PHYSICAL_WEIGHT_KEY in requirements
                    and PHYSICAL_WEIGHT_KEY in configured_map_attribute_keys
                    and PHYSICAL_WEIGHT_KEY in effective
                    and effective.get(PHYSICAL_WEIGHT_KEY) not in (None, "")
                ):
                    try:
                        weight_exceeds_capacity = (
                            float(requirements[PHYSICAL_WEIGHT_KEY])
                            > float(effective[PHYSICAL_WEIGHT_KEY])
                        )
                    except (TypeError, ValueError):
                        weight_exceeds_capacity = True
                    if weight_exceeds_capacity:
                        issues.append(
                            "Maximum item weight exceeds the configured zone capacity"
                        )
                missing_physical = [
                    key for key in PHYSICAL_ATTRIBUTE_KEYS
                    if key in requirements
                    and key in configured_map_attribute_keys
                    and key not in effective
                ]
                if missing_physical:
                    issues.extend(
                        f"{catalog[key].label}: location value is not defined"
                        for key in missing_physical
                    )
                if issues:
                    for issue in issues:
                        if issue not in hard_issues:
                            hard_issues.append(issue)
                    continue
                overrides = dict(missing_overrides)
                if known_volumetric_oversize:
                    # The contiguous footprint satisfies the dimension
                    # requirement. Do not disguise that occupancy by enlarging
                    # a single slot's configured dimensions.
                    for key in PHYSICAL_DIMENSION_KEYS:
                        overrides.pop(key, None)
                missing_override_keys = set(missing_overrides) & set(overrides)
                if auto_plan_oversize and exception_inventory:
                    if candidate["effective_location_attributes"].get(
                        OVERSIZE_CAPABLE_KEY
                    ) is not True:
                        overrides[OVERSIZE_CAPABLE_KEY] = True
                    for key in PHYSICAL_ATTRIBUTE_KEYS:
                        raw = requirements.get(key)
                        try:
                            value = float(raw)
                        except (TypeError, ValueError):
                            value = 0.0
                        if not math.isfinite(value) or value <= 0:
                            # Unknown physical properties become unbounded
                            # planning assumptions on this generated segment.
                            overrides[key] = None
                candidate_key = allocation_candidate_key(
                    candidate,
                    profile,
                    physical_grouping_enabled,
                    str(sku.get("velocity_class", "")).upper(),
                    overrides,
                    rack_state,
                    levels_per_rack,
                    occupied_positions,
                    ergonomic_weight_heuristic,
                    strategy != "ctbsa",
                )
                exception_inventory = (
                    profile["storage_class"] != "STANDARD"
                )
                soft_affinity_enabled = (
                    affinity_enabled and not exception_inventory
                )
                if soft_affinity_enabled and affinity_weight >= 1.0:
                    # At the pure-affinity endpoint ABC class must not affect
                    # placement, even for isolated SKUs or affinity ties.
                    candidate_key = (
                        *candidate_key[:4], 0, *candidate_key[5:]
                    )
                if (
                    sku_load_counts[current_sku] > 1
                    and quantity_rack_counts[current_sku][
                        str(candidate["rack_id"])
                    ] + len(occupied_positions)
                    > maximum_same_sku_slots_per_rack
                ):
                    continue
                hard_candidates.append((
                    candidate_key,
                    index,
                    candidate,
                    overrides,
                    occupied_positions,
                    missing_override_keys,
                ))
            if hard_candidates:
                spread_candidates = hard_candidates
                if sku_load_counts[current_sku] > 1:
                    rack_counts = quantity_rack_counts[current_sku]
                    spread_candidates = [
                        record for record in spread_candidates
                        if rack_counts[str(record[2]["rack_id"])]
                        + len(record[4]) <= maximum_same_sku_slots_per_rack
                    ]
                    same_sku_racks = [
                        record for record in spread_candidates
                        if rack_counts[str(record[2]["rack_id"])] > 0
                    ]
                    if same_sku_racks:
                        spread_candidates = same_sku_racks
                    prior_positions = quantity_positions_by_sku[current_sku]

                    def same_level_adjacent_to_same_sku(record) -> bool:
                        return any(
                            prior[0] == str(position["rack_id"])
                            and prior[1] == int(position["level"])
                            and abs(prior[2] - int(position["slot"])) == 1
                            for prior in prior_positions
                            for position in record[4]
                        )
                    selection_key = lambda record: (
                        *record[0][:9],
                        -int(same_level_adjacent_to_same_sku(record)),
                        *record[0][9:],
                    )
                else:
                    selection_key = lambda record: record[0]
                if zone_workload_enabled and not any(
                    quantity_rack_counts[current_sku][str(record[2]["rack_id"])] > 0
                    for record in spread_candidates
                ):
                    spread_candidates = balanced_zone_candidates(
                        spread_candidates, zone_workloads, zone_capacities, zone_demand,
                    )
                baseline_candidate = min(
                    spread_candidates, key=selection_key
                )
                selected_candidate = baseline_candidate
                related_assigned = [
                    (related_sku, weight, assigned_affinity_positions[related_sku])
                    for related_sku, weight, _score, _shared
                    in affinity_neighbors.get(current_sku, [])
                    if related_sku in assigned_affinity_positions
                ]
                if (
                    soft_affinity_enabled
                    and affinity_weight > 0
                    and current_order_mask
                ):
                    baseline_key = baseline_candidate[0]
                    physical_signature = affinity_physical_signature(
                        baseline_key
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
                        math.inf
                        if affinity_weight >= 1.0
                        else baseline_service
                        + maximum_service_distance_increase * service_reference
                        if math.isfinite(baseline_service)
                        else math.inf
                    )
                    eligible = []
                    for candidate_record in spread_candidates:
                        (
                            key, _index, candidate, _overrides, _occupied,
                            _missing_override_keys,
                        ) = candidate_record
                        candidate_service = float(candidate["distance_m"])
                        if (
                            affinity_physical_signature(key)
                            != physical_signature
                        ):
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
                        pair_distances = (
                            np.linalg.norm(
                                candidate_coordinates[:, None, :]
                                - related_coordinates[None, :, :],
                                axis=2,
                            ) * coordinate_scale
                            if related_assigned
                            else np.zeros((len(eligible), 0), dtype=np.float64)
                        )
                        affinity_distances = (
                            (pair_distances @ relationship_weights)
                            / float(relationship_weights.sum())
                            if related_assigned else np.zeros(len(eligible))
                        )
                        candidate_rack_ids = np.array(
                            [str(item[2]["rack_id"]) for item in eligible]
                        )
                        related_rack_ids = np.array(
                            [str(item[2]["rack_id"]) for item in related_assigned]
                        )
                        different_bay = (
                            candidate_rack_ids[:, None]
                            != related_rack_ids[None, :]
                            if related_assigned
                            else np.zeros((len(eligible), 0), dtype=bool)
                        )
                        normalized_affinity_distances = (
                            affinity_distances / maximum_rack_distance
                        )
                        different_bay_fractions = (
                            (different_bay.astype(np.float64) @ relationship_weights)
                            / float(relationship_weights.sum())
                            if related_assigned else np.ones(len(eligible))
                        )
                        same_bay_fractions = 1.0 - different_bay_fractions
                        scores = []
                        for candidate_number, candidate_record in enumerate(eligible):
                            service_distance = float(
                                candidate_record[2]["distance_m"]
                            )
                            candidate_abc_cost = float(candidate_record[0][3])
                            rack_order_mask = rack_state.get(
                                str(candidate_record[2]["rack_id"]), {}
                            ).get("order_mask", 0)
                            covered_orders = (
                                current_order_mask & rack_order_mask
                            ).bit_count()
                            candidate_affinity_cost = 1.0 - (
                                covered_orders / current_order_mask.bit_count()
                            )
                            combined = (
                                (1.0 - affinity_weight) * candidate_abc_cost
                                + affinity_weight * candidate_affinity_cost
                            )
                            abc_tiebreak_cost = (
                                candidate_abc_cost
                                if affinity_weight < 1.0 else 0.0
                            )
                            scores.append((
                                combined,
                                candidate_affinity_cost,
                                float(
                                    normalized_affinity_distances[
                                        candidate_number
                                    ]
                                ),
                                abc_tiebreak_cost,
                                service_distance,
                                candidate_record[0],
                                candidate_number,
                                candidate_record,
                            ))
                        (
                            combined_location_score,
                            affinity_bay_cost,
                            _affinity_distance_score,
                            abc_grouping_cost,
                            _service,
                            _candidate_key,
                            selected_number,
                            selected_candidate,
                        ) = min(scores, key=lambda item: item[:7])
                        affinity_neighbors_used = [
                            item[0] for item in related_assigned
                        ]
                        affinity_weight_sum = float(
                            relationship_weights.sum()
                        )
                        weighted_affinity_distance = float(
                            affinity_distances[selected_number]
                        )
                        affinity_same_bay_fraction = float(
                            same_bay_fractions[selected_number]
                        )
                (
                    _key, selected_index, position, auto_overrides,
                    occupied_positions, missing_override_keys,
                ) = selected_candidate
                zone_workloads[str(position["zone_id"])] += zone_demand
                occupied_ids = {id(item) for item in occupied_positions}
                available_positions = [
                    item for item in available_positions
                    if id(item) not in occupied_ids
                ]
                if auto_overrides:
                    rack_group_overrides = {
                        key: value for key, value in auto_overrides.items()
                        if key in missing_override_keys
                        and key in exact_grouping_keys
                    }
                    if rack_group_overrides:
                        rack_path = "/".join(
                            position["storage_location_address"].split("/")[:3]
                        )
                        local_attributes.setdefault(rack_path, {}).update(
                            rack_group_overrides
                        )
                        rack_positions = [
                            item for item in positions
                            if item["rack_id"] == position["rack_id"]
                        ]
                        for rack_position in rack_positions:
                            address = rack_position[
                                "storage_location_address"
                            ]
                            rack_position[
                                "effective_location_attributes"
                            ] = service.attributes.effective_attributes(
                                address, local_attributes
                            )[0]
                        profile_values = {
                            key: position[
                                "effective_location_attributes"
                            ][key]
                            for key in sorted(exact_grouping_keys)
                            if key in position[
                                "effective_location_attributes"
                            ]
                        }
                        parent_zone = str(position["zone_id"])
                        generated_zone_id = next((
                            zone_key
                            for zone_key, definition
                            in generated_attribute_zones.items()
                            if definition["parent_zone_id"] == parent_zone
                            and definition["attributes"] == profile_values
                        ), "")
                        if not generated_zone_id:
                            generated_zone_id = parent_zone + "".join(
                                f"__L{(catalog[key].hierarchy_level or index):02d}"
                                f"_{key}_"
                                + (
                                    "T" if value is True
                                    else "F" if value is False else "U"
                                )
                                for index, (key, value) in enumerate(
                                    profile_values.items(), start=1
                                )
                            )
                            generated_attribute_zones[generated_zone_id] = {
                                "parent_zone_id": parent_zone,
                                "attributes": dict(profile_values),
                                "rack_ids": [],
                                "hierarchy_path": [
                                    {
                                        "level": catalog[key].hierarchy_level or index,
                                        "attribute": key,
                                        "value": value,
                                        "zone_id": generated_zone_id,
                                    }
                                    for index, (key, value) in enumerate(
                                        profile_values.items(), start=1
                                    )
                                ],
                            }
                        generated_racks = generated_attribute_zones[
                            generated_zone_id
                        ]["rack_ids"]
                        if position["rack_id"] not in generated_racks:
                            generated_racks.append(position["rack_id"])
                        subzone_suffix = (
                            generated_zone_id.split(parent_zone, 1)[1]
                            if generated_zone_id != parent_zone else ""
                        )
                        for rack_position in rack_positions:
                            base_zone = rack_position["planned_zone_base_id"]
                            rack_position["planned_zone_id"] = (
                                f"{base_zone}{subzone_suffix}"
                                if subzone_suffix else base_zone
                            )
                            rack_position["generated_attribute_zone_id"] = (
                                generated_zone_id
                            )
                            rack_position["parent_zone_id"] = parent_zone
                        for assigned_row in output:
                            if assigned_row.get("rack_id") != position["rack_id"]:
                                continue
                            assigned_row["planned_zone_id"] = position[
                                "planned_zone_id"
                            ]
                            assigned_row[
                                "effective_location_attributes"
                            ] = service.attributes.effective_attributes(
                                assigned_row["storage_location_address"],
                                local_attributes,
                            )[0]
                    for occupied in occupied_positions:
                        override_path = occupied["storage_location_address"]
                        effective_before = occupied[
                            "effective_location_attributes"
                        ]
                        safe_overrides = {
                            key: value
                            for key, value in auto_overrides.items()
                            if key not in rack_group_overrides
                            if key not in missing_override_keys
                            or key not in effective_before
                        }
                        local_attributes.setdefault(override_path, {}).update(
                            safe_overrides
                        )
                        occupied["effective_location_attributes"] = (
                            service.attributes.effective_attributes(
                                override_path, local_attributes
                            )[0]
                        )
                        occupied["storage_area_type"] = (
                            "OVERSIZE"
                            if service.attributes.is_oversize_location(
                                occupied["effective_location_attributes"]
                            )
                            else "STANDARD"
                        )
                selected_state = rack_state.setdefault(
                    position["rack_id"],
                    {
                        "velocity_classes": set(),
                        "physical_buckets": set(),
                        "has_oversize": False,
                        "order_mask": 0,
                        "cumulative_weight_kg": 0.0,
                    },
                )
                selected_state["order_mask"] |= current_order_mask
                if inventory_load_weight is not None:
                    selected_state["cumulative_weight_kg"] += inventory_load_weight
                selected_state["velocity_classes"].add(
                    str(sku.get("velocity_class", "")).upper()
                )
                selected_state["physical_buckets"].add(
                    physical_allocation_bucket(
                        profile, physical_grouping_enabled
                    )
                )
                selected_state["has_oversize"] = (
                    selected_state["has_oversize"]
                    or (
                        physical_grouping_enabled
                        and str(profile.get("storage_class", "")).upper()
                        in {
                            "OVERSIZE",
                            "OVERSIZE_AND_OVERWEIGHT",
                            "UNVERIFIED_OVERSIZE",
                        }
                    )
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
                if sku_load_counts[current_sku] > 1:
                    quantity_rack_counts[current_sku][
                        str(position["rack_id"])
                    ] += len(occupied_positions)
                    quantity_positions_by_sku[current_sku].extend(
                        (
                            str(item["rack_id"]),
                            int(item["level"]),
                            int(item["slot"]),
                        )
                        for item in occupied_positions
                    )
            else:
                mismatch_details = hard_issues
        if position:
            assignment_status = "ASSIGNED"
        elif available_positions:
            required_chilled = requirements.get("chilled")
            temperature_capacity_exists = any(
                candidate["effective_location_attributes"].get("chilled")
                is required_chilled
                for candidate in available_positions
            ) if isinstance(required_chilled, bool) else True
            only_temperature_mismatch = bool(hard_issues) and all(
                str(issue).startswith("Chilled:") for issue in hard_issues
            )
            if not temperature_capacity_exists:
                assignment_status = (
                    "UNASSIGNED_NO_CHILLED_LOCATION"
                    if required_chilled is True
                    else "UNASSIGNED_NO_AMBIENT_LOCATION"
                )
            elif not only_temperature_mismatch:
                assignment_status = "UNASSIGNED_NO_COMPATIBLE_LOCATION"
            else:
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
            "abc_frequency_rank": abc_frequency_rank,
            "affinity_placement_rank": affinity_placement_rank,
            "placement_rank": placement_rank,
            "placement_priority_group": (
                "STANDARD_FIRST" if not exception_inventory else "OVERSIZE_LAST"
            ),
            "sku": sku.get("sku", ""),
            "inventory_load_id": sku.get(
                "inventory_load_id", sku.get("sku", "")
            ),
            "quantity_load_index": sku.get("quantity_load_index", 1),
            "quantity_load_count": sku.get("quantity_load_count", 1),
            "quantity_ea": sku.get("quantity_ea", ""),
            "inventory_load_weight_kg": (
                round(inventory_load_weight, 9)
                if inventory_load_weight is not None else ""
            ),
            "rack_cumulative_weight_kg": (
                round(
                    float(rack_state.get(str(position["rack_id"]), {}).get(
                        "cumulative_weight_kg", 0.0
                    )),
                    9,
                )
                if position else ""
            ),
            "total_required_ea": sku.get("total_required_ea", ""),
            "units_per_slot": sku.get("units_per_slot", ""),
            "slots_per_unit": sku.get("slots_per_unit", ""),
            "required_slot_count": sku.get(
                "required_slot_count", sku.get("required_slots", "")
            ),
            "required_racks": sku.get("required_racks", ""),
            "velocity_class": sku.get("velocity_class", ""),
            "pick_frequency": sku.get("pick_frequency", ""),
            "total_quantity_ea": sku.get("total_quantity_ea", ""),
            "active_days": sku.get("active_days", ""),
            "strategy": strategy,
            "assignment_status": assignment_status,
            "sku_requirements": requirements,
            "physical_data_status": profile["data_status"],
            "physical_missing_data_type": profile.get("missing_data_type", ""),
            "physical_storage_class": profile["storage_class"],
            "physical_missing_fields": profile["missing_fields"],
            "constrained_feasibility_reserved": bool(planned_footprint),
            "ergonomic_weight_heuristic": ergonomic_preference_enabled,
            "ergonomic_preferred_level": (
                ergonomic_preferred_level
                if ergonomic_preference_enabled else ""
            ),
            "compatibility_status": compatibility_status,
            "compatibility_issues": mismatch_details,
            "auto_attribute_overrides": auto_overrides,
            "affinity_neighbors_used": affinity_neighbors_used,
            "affinity_weight_sum": round(affinity_weight_sum, 6),
            "weighted_affinity_distance_m": round(
                weighted_affinity_distance, 6
            ),
            "affinity_same_bay_fraction": round(
                affinity_same_bay_fraction, 6
            ),
            "abc_grouping_cost": round(abc_grouping_cost, 6),
            "affinity_bay_cost": round(affinity_bay_cost, 6),
            "combined_location_score": (
                round(float(combined_location_score), 9)
                if combined_location_score is not None
                else ""
            ),
            "affinity_weight": affinity_weight if affinity_enabled else 0.0,
            "occupied_slot_count": len(occupied_positions) if position else 0,
            "occupied_level_span": (
                len({item["level"] for item in occupied_positions})
                if position else 0
            ),
            "occupied_horizontal_slot_span": (
                len({item["slot"] for item in occupied_positions})
                if position else 0
            ),
            "occupied_static_addresses": (
                sorted({item["static_address"] for item in occupied_positions})
                if position else []
            ),
            "occupied_buffer_ids": (
                sorted({item["buffer_id"] for item in occupied_positions})
                if position else []
            ),
            "occupied_storage_location_addresses": (
                [item["storage_location_address"] for item in occupied_positions]
                if position else []
            ),
            "occupied_handling_units": (
                [
                    {
                        "handling_unit_id": item["handling_unit_id"],
                        "rack_id": item["rack_id"],
                        "storage_level": item["level"],
                        "storage_slot": item["slot"],
                        "buffer_id": item["buffer_id"],
                        "static_address": item["static_address"],
                        "storage_location_address": item[
                            "storage_location_address"
                        ],
                    }
                    for item in occupied_positions
                ]
                if position else []
            ),
        }
        if position:
            row.update({
                "static_address": position["static_address"],
                "storage_location_address": position["storage_location_address"],
                "buffer_id": position["buffer_id"],
                "buffer_level": position["buffer_level"],
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
                "planned_zone_id": position["planned_zone_id"],
                "planned_storage_type": position["planned_storage_type"],
                "generated_attribute_zone_id": position.get(
                    "generated_attribute_zone_id", position["zone_id"]
                ),
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
        row["occupied_dynamic_address"] = (
            combined_occupied_dynamic_address(row)
            if position else ""
        )
        output.append(row)

    # Preserve the map's zone IDs and boundaries exactly. Slotting may assign
    # inventory, but it does not materialize new attribute/storage zones.
    generated_attribute_zones = {}
    old_aisle_by_waypoint = {
        str(position["waypoint"]): str(position["aisle_id"])
        for position in positions
    }
    service.apply_zone_local_aisles(
        building, racks, zone_assignments, zone_id
    )
    aisle_by_waypoint = {
        str(rack["waypoint"]): str(rack["aisle_id"])
        for rack in racks
    }
    rack_prefix_changes = {}
    for position in positions:
        waypoint = str(position["waypoint"])
        old_aisle = old_aisle_by_waypoint[waypoint]
        new_aisle = aisle_by_waypoint[waypoint]
        if old_aisle == new_aisle:
            continue
        old_prefix = (
            f"{position['zone_id']}/{old_aisle}/{position['static_bay_id']}"
        )
        new_prefix = (
            f"{position['zone_id']}/{new_aisle}/{position['static_bay_id']}"
        )
        rack_prefix_changes[old_prefix] = new_prefix
        position["aisle_id"] = new_aisle
        for field in ("static_address", "storage_location_address"):
            value = str(position.get(field, ""))
            if value == old_prefix or value.startswith(old_prefix + "/"):
                position[field] = new_prefix + value[len(old_prefix):]
    for old_prefix, new_prefix in rack_prefix_changes.items():
        for path, values in list(local_attributes.items()):
            if path == old_prefix or path.startswith(old_prefix + "/"):
                new_path = new_prefix + path[len(old_prefix):]
                local_attributes[new_path] = local_attributes.pop(path)
    for row in output:
        waypoint = str(row.get("rack_waypoint", ""))
        if not waypoint or waypoint not in aisle_by_waypoint:
            continue
        old_aisle = str(row.get("aisle_id", ""))
        new_aisle = aisle_by_waypoint[waypoint]
        if old_aisle == new_aisle:
            continue
        old_prefix = f"{row['zone_id']}/{old_aisle}/{row['static_bay_id']}"
        new_prefix = f"{row['zone_id']}/{new_aisle}/{row['static_bay_id']}"

        def rename_aisle(value):
            if isinstance(value, str) and (
                value == old_prefix or value.startswith(old_prefix + "/")
            ):
                return new_prefix + value[len(old_prefix):]
            return value

        row["aisle_id"] = new_aisle
        for field in ("static_address", "storage_location_address"):
            row[field] = rename_aisle(row.get(field, ""))
        for field in (
            "occupied_static_addresses", "occupied_storage_location_addresses"
        ):
            row[field] = [rename_aisle(value) for value in row.get(field, [])]
        for unit in row.get("occupied_handling_units", []):
            for field in ("static_address", "storage_location_address"):
                if field in unit:
                    unit[field] = rename_aisle(unit[field])
    output.sort(key=lambda row: int(row.get("sku_rank") or 10**9))
    rack_frequency_ranking = apply_rack_frequency_ranks(output)

    if location_attributes is not None:
        location_attributes.clear()
        location_attributes.update(local_attributes)

    zone_storage_types = derive_zone_storage_types(
        output, {position["planned_zone_id"] for position in positions}
    )
    planned_zone_types = {
        position["planned_zone_id"]: position["planned_storage_type"]
        for position in positions
    }
    zone_storage_types = dict(sorted(planned_zone_types.items()))
    for row in output:
        if row.get("assignment_status") == "ASSIGNED":
            row["zone_storage_type"] = row.get("planned_storage_type", "")

    status_counts = {
        status: sum(row["assignment_status"] == status for row in output)
        for status in {
            "UNASSIGNED_NO_CAPACITY",
            "UNASSIGNED_NO_CHILLED_LOCATION",
            "UNASSIGNED_NO_AMBIENT_LOCATION",
            "UNASSIGNED_NO_COMPATIBLE_LOCATION",
            "UNASSIGNED_NO_OVERSIZE_LOCATION",
        }
    }
    occupied_buffer_ids = {
        buffer_id
        for row in output
        if row["assignment_status"] == "ASSIGNED"
        for buffer_id in row.get("occupied_buffer_ids", [])
    }
    buffer_count = (
        len(storage_layout.buffers)
        if buffer_model
        else len(racks) if handling_unit_type == "AMR shelf" else len(positions)
    )
    loads_by_sku: dict[str, list[dict]] = defaultdict(list)
    for row in output:
        loads_by_sku[str(row.get("sku", ""))].append(row)
    fully_assigned_sku_count = sum(
        all(row["assignment_status"] == "ASSIGNED" for row in rows)
        for rows in loads_by_sku.values()
    )
    partially_assigned_sku_count = sum(
        any(row["assignment_status"] == "ASSIGNED" for row in rows)
        and any(row["assignment_status"] != "ASSIGNED" for row in rows)
        for rows in loads_by_sku.values()
    )
    unassigned_sku_count = sum(
        all(row["assignment_status"] != "ASSIGNED" for row in rows)
        for rows in loads_by_sku.values()
    )
    assigned_load_count = sum(
        row["assignment_status"] == "ASSIGNED" for row in output
    )
    unassigned_load_count = len(output) - assigned_load_count
    occupied_racks = sorted({
        str(row.get("rack_id", "")) for row in output
        if row.get("assignment_status") == "ASSIGNED" and row.get("rack_id")
    })

    def assigned_quantity(rows: list[dict], rack_id: str) -> float:
        total = 0.0
        for row in rows:
            if (
                row.get("assignment_status") != "ASSIGNED"
                or str(row.get("rack_id", "")) != rack_id
            ):
                continue
            try:
                total += float(row.get("quantity_ea") or 0)
            except (TypeError, ValueError):
                continue
        return total

    sku_rack_distribution = {
        sku: {
            rack_id: assigned_quantity(rows, rack_id)
            for rack_id in sorted({
                str(row.get("rack_id", "")) for row in rows
                if row.get("assignment_status") == "ASSIGNED"
                and row.get("rack_id")
            })
        }
        for sku, rows in sorted(loads_by_sku.items())
    }
    summary = {
        "strategy": strategy,
        "zone_workload_enabled": zone_workload_enabled,
        "maximum_same_sku_slots_per_rack": maximum_same_sku_slots_per_rack,
        "zone_workload_model": "quantity_weighted_pick_frequency_per_zone_slot",
        "zone_workload": {
            zone: {
                "slot_capacity": capacity,
                "demand": zone_workloads[zone],
                "normalized_demand": zone_workloads[zone] / capacity,
            }
            for zone, capacity in sorted(zone_capacities.items())
        },
        "hard_rule_profile": SHARED_HARD_RULE_PROFILE,
        "hard_rules": list(SHARED_HARD_RULES),
        "sku_count": quantity_summary["source_sku_count"],
        "inventory_load_count": quantity_summary["inventory_load_count"],
        "quantity_enabled_sku_count": quantity_summary[
            "quantity_enabled_sku_count"
        ],
        "zero_required_sku_count": quantity_summary[
            "zero_required_sku_count"
        ],
        "fully_assigned_sku_count": fully_assigned_sku_count,
        "partially_assigned_sku_count": partially_assigned_sku_count,
        "unassigned_sku_count": unassigned_sku_count,
        "assigned_load_count": assigned_load_count,
        "unassigned_load_count": unassigned_load_count,
        "assigned_count": assigned_load_count,
        "occupied_slot_count": sum(
            int(row.get("occupied_slot_count", 0)) for row in output
        ),
        "unassigned_count": unassigned_load_count,
        "unassigned_no_capacity_count": status_counts["UNASSIGNED_NO_CAPACITY"],
        "unassigned_no_compatible_location_count": status_counts[
            "UNASSIGNED_NO_COMPATIBLE_LOCATION"
        ],
        "unassigned_no_oversize_location_count": status_counts[
            "UNASSIGNED_NO_OVERSIZE_LOCATION"
        ],
        "unassigned_status_counts": status_counts,
        "unverified_oversize_count": sum(
            row["physical_data_status"] == "MISSING"
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
        "auto_adjusted_oversize_zone_weight_capacity_count": len(
            auto_adjusted_oversize_zone_weight_capacities
        ),
        "auto_adjusted_oversize_zone_weight_capacities": (
            auto_adjusted_oversize_zone_weight_capacities
        ),
        "constrained_inventory_count": constrained_inventory_total,
        "reserved_constrained_inventory_count": len(
            reserved_footprints_by_row_id
        ),
        "reserved_constrained_slot_count": len(reserved_owner_by_position),
        "unreservable_constrained_inventory_count": (
            constrained_inventory_total - len(reserved_footprints_by_row_id)
        ),
        "constrained_feasibility_plan_complete": bool(
            complete_constrained_plan
            and len(plannable_records) == len(constrained_records)
        ),
        "constrained_feasibility_search_nodes": search_nodes,
        "provably_insufficient_constrained_capacity": (
            provably_insufficient_constrained_capacity
        ),
        "auto_planned_oversize_segment_count": sum(
            storage_type == "OVERSIZE"
            for storage_type in planned_zone_types.values()
        ),
        "rack_count": len(racks),
        "usable_rack_count": len(usable_racks),
        "compact_rack_count": len(occupied_racks),
        "final_occupied_rack_count": len(occupied_racks),
        "occupied_rack_ids": occupied_racks,
        "sku_rack_distribution": sku_rack_distribution,
        "consolidation_status": (
            "COMPACT_POOL_ALLOCATED" if not unassigned_load_count
            else "PARTIAL_CAPACITY_OR_COMPATIBILITY"
        ),
        "unreachable_rack_count": unreachable_count,
        "workstation_count": workstation_count,
        "capacity": len(positions),
        "buffer_count": buffer_count,
        "occupied_buffer_count": len(occupied_buffer_ids),
        "empty_buffer_count": max(0, buffer_count - len(occupied_buffer_ids)),
        "buffer_occupancy_rate": (
            len(occupied_buffer_ids) / buffer_count if buffer_count else 0.0
        ),
        "level_name": level_name,
        "zone_count": len({position["planned_zone_id"] for position in positions}),
        "zone_storage_types": zone_storage_types,
        "generated_attribute_zones": generated_attribute_zones,
        "zone_assignments": dict(zone_assignments),
        "attribute_hierarchy": [
            {
                "level": catalog[key].hierarchy_level or index,
                "attribute": key,
            }
            for index, key in enumerate(exact_grouping_keys, start=1)
        ],
        "ergonomic_weight_heuristic": ergonomic_weight_heuristic,
        "rack_frequency_ranking": rack_frequency_ranking,
    }
    if affinity_analysis is not None:
        affinity_metrics = affinity_layout_metrics(
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
