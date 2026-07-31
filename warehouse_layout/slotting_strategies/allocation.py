"""Shared physical slot-allocation engine used by slotting strategies."""

from __future__ import annotations

import math

import numpy as np

from ..affinity import AffinityAnalysis
from ..attributes import (
    OVERSIZE_CAPABLE_KEY,
    PHYSICAL_ATTRIBUTE_KEYS,
    PHYSICAL_DIMENSION_KEYS,
    PHYSICAL_WEIGHT_KEY,
    STANDARD_STORAGE_DEFAULTS,
)
from ..storage_planning import (
    build_dynamic_address,
    combined_occupied_dynamic_address,
    derive_zone_storage_types,
    plan_storage_zones,
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
    auto_plan_oversize: bool = True,
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
    service.apply_zone_local_aisles(building, racks, zone_assignments, zone_id)
    catalog = service.attributes.normalize_catalog(attribute_catalog)
    valid_paths = service.attributes.hierarchy_paths(
        racks, levels_per_rack, slots_per_level
    )
    local_attributes = service.attributes.validate_location_attributes(
        location_attributes, catalog, valid_paths
    )

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
    if auto_plan_oversize and physical_enabled:
        plan_storage_zones(
            positions,
            sku_rows,
            catalog,
            service.attributes,
            levels_per_rack,
            local_attributes,
        )
    else:
        for position in positions:
            storage_type = (
                "OVERSIZE"
                if service.attributes.is_oversize_location(
                    position["effective_location_attributes"]
                )
                else "STANDARD"
            )
            position["planned_storage_type"] = storage_type
            position["planned_zone_id"] = (
                f"{position['zone_id']}_{storage_type}"
            )
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
    base_sorted_skus = sorted(
        sku_rows,
        key=lambda row: (
            class_rank.get(str(row.get("velocity_class", "")).upper(), 9),
            physical_group_rank(row),
            -float(row.get("pick_frequency") or 0),
            str(row.get("sku", "")),
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
        sorted_skus = sorted(
            logical_sorted_skus,
            key=lambda row: (
                service.attributes.physical_profile(
                    row.get("sku_requirements")
                    if isinstance(row.get("sku_requirements"), dict)
                    else service.attributes.requirements_from_row(row, catalog)
                )["storage_class"] != "STANDARD",
                affinity_rank_by_id[id(row)],
            ),
        )
    else:
        sorted_skus = logical_sorted_skus
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
        "rack_frequency_rank", "rack_pick_frequency",
        "rack_frequency_share", "rack_cumulative_frequency_share",
        "rack_velocity_class",
    )
    available_positions = list(positions)
    rack_state: dict[str, dict] = {}
    assigned_affinity_positions: dict[str, dict] = {}
    for placement_rank, sku in enumerate(sorted_skus, start=1):
        abc_frequency_rank = abc_rank_by_id.get(id(sku), placement_rank)
        affinity_placement_rank = affinity_rank_by_id.get(
            id(sku), placement_rank
        )
        sku_rank = abc_frequency_rank
        current_sku = str(sku.get("sku", ""))
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
                    or known_volumetric_oversize
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
                            for occupied in occupied_positions:
                                issues.extend(
                                    service.attributes.hard_compatibility_issues(
                                        requirements,
                                        occupied[
                                            "effective_location_attributes"
                                        ],
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
                                    issues.extend(
                                        service.attributes.compatibility_issues(
                                            generic_requirements,
                                            occupied[
                                                "effective_location_attributes"
                                            ],
                                            catalog,
                                        )
                                    )
                elif strict_compatibility:
                    generic_requirements = {
                        key: value for key, value in requirements.items()
                        if key not in PHYSICAL_ATTRIBUTE_KEYS
                    }
                    issues = service.attributes.compatibility_issues(
                        generic_requirements,
                        candidate["effective_location_attributes"],
                        catalog,
                    )
                else:
                    issues = service.attributes.hard_compatibility_issues(
                        requirements, candidate["effective_location_attributes"]
                    )
                if issues:
                    for issue in issues:
                        if issue not in hard_issues:
                            hard_issues.append(issue)
                    continue
                overrides = (
                    {}
                    if strict_compatibility
                    else service.attributes.required_local_overrides(
                        requirements,
                        candidate["effective_location_attributes"],
                        catalog,
                    )
                )
                if known_volumetric_oversize:
                    # The contiguous footprint satisfies the dimension
                    # requirement. Do not disguise that occupancy by enlarging
                    # a single slot's configured dimensions.
                    for key in PHYSICAL_DIMENSION_KEYS:
                        overrides.pop(key, None)
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
                    auto_plan_oversize,
                )
                if affinity_enabled and affinity_weight >= 1.0:
                    # At the pure-affinity endpoint ABC class must not affect
                    # placement, even for isolated SKUs or affinity ties.
                    candidate_key = (
                        *candidate_key[:4], 0, *candidate_key[5:]
                    )
                hard_candidates.append((
                    candidate_key,
                    index,
                    candidate,
                    overrides,
                    occupied_positions,
                ))
            if hard_candidates:
                baseline_candidate = min(
                    hard_candidates, key=lambda item: item[0]
                )
                selected_candidate = baseline_candidate
                related_assigned = [
                    (related_sku, weight, assigned_affinity_positions[related_sku])
                    for related_sku, weight, _score, _shared
                    in affinity_neighbors.get(current_sku, [])
                    if related_sku in assigned_affinity_positions
                ]
                if (
                    affinity_enabled
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
                    for candidate_record in hard_candidates:
                        key, _index, candidate, _overrides, _occupied = candidate_record
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
                _key, selected_index, position, auto_overrides, occupied_positions = (
                    selected_candidate
                )
                occupied_ids = {id(item) for item in occupied_positions}
                available_positions = [
                    item for item in available_positions
                    if id(item) not in occupied_ids
                ]
                if auto_overrides:
                    for occupied in occupied_positions:
                        override_path = occupied["storage_location_address"]
                        local_attributes.setdefault(
                            override_path, {}
                        ).update(auto_overrides)
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
                    },
                )
                selected_state["order_mask"] |= current_order_mask
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
    summary = {
        "strategy": strategy,
        "sku_count": len(sorted_skus),
        "assigned_count": sum(
            row["assignment_status"] == "ASSIGNED" for row in output
        ),
        "occupied_slot_count": sum(
            int(row.get("occupied_slot_count", 0)) for row in output
        ),
        "unassigned_count": sum(
            row["assignment_status"] != "ASSIGNED" for row in output
        ),
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
        "auto_planned_oversize_segment_count": sum(
            storage_type == "OVERSIZE"
            for storage_type in planned_zone_types.values()
        ),
        "rack_count": len(racks),
        "usable_rack_count": len(usable_racks),
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
