"""Reusable placement constraints, capacity rules, and ranking functions."""

from __future__ import annotations

import itertools
import math

from .attributes import (
    OVERSIZE_CAPABLE_KEY,
    PHYSICAL_DIMENSION_KEYS,
    PHYSICAL_WEIGHT_KEY,
    StorageAttributeService,
)


def required_slot_footprint(
    requirements: dict,
    effective: dict,
    maximum_levels: int | None = None,
    maximum_horizontal_slots: int | None = None,
) -> tuple[int, int] | None:
    """Return (vertical levels, horizontal slots), allowing item rotation.

    Location length is treated as depth, width as the repeatable horizontal
    slot dimension, and height as the repeatable vertical level dimension.
    """
    try:
        item = [float(requirements[key]) for key in PHYSICAL_DIMENSION_KEYS]
        depth = StorageAttributeService.physical_capacity(
            effective, "max_item_length"
        )
        width = StorageAttributeService.physical_capacity(
            effective, "max_item_width"
        )
        height = StorageAttributeService.physical_capacity(
            effective, "max_item_height"
        )
    except (KeyError, TypeError, ValueError):
        return None
    if any(not math.isfinite(value) or value <= 0 for value in item) or any(
        math.isnan(value) or value <= 0 for value in (depth, width, height)
    ):
        return None
    footprints = [
        (
            max(1, int(math.ceil(oriented[2] / height - 1e-12))),
            max(1, int(math.ceil(oriented[1] / width - 1e-12))),
        )
        for oriented in set(itertools.permutations(item))
        if oriented[0] <= depth + 1e-9
    ]
    if maximum_levels is not None:
        footprints = [
            value for value in footprints if value[0] <= maximum_levels
        ]
    if maximum_horizontal_slots is not None:
        footprints = [
            value for value in footprints
            if value[1] <= maximum_horizontal_slots
        ]
    return min(
        footprints,
        key=lambda value: (value[0] * value[1], value[0], value[1]),
    ) if footprints else None


def required_horizontal_slot_span(
    requirements: dict, effective: dict
) -> int | None:
    """Backward-compatible horizontal component of the slot footprint."""
    footprint = required_slot_footprint(requirements, effective)
    return footprint[1] if footprint else None


def candidate_sort_key(
    candidate: dict, profile: dict, physical_enabled: bool
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
        float(candidate["distance_m"]),
        int(candidate["rack_rank"]),
        waste,
        int(candidate["level"]),
        int(candidate["slot"]),
    )


def rack_frequency_class(cumulative_share: float) -> str:
    if cumulative_share <= 0.80:
        return "A"
    if cumulative_share <= 0.95:
        return "B"
    return "C"


def apply_rack_frequency_ranks(rows: list[dict]) -> list[dict]:
    rack_frequency: dict[str, float] = {}
    rack_rows: dict[str, list[dict]] = {}
    rack_order: dict[str, int] = {}
    for row in rows:
        if row.get("assignment_status") != "ASSIGNED":
            continue
        rack_id = str(row.get("rack_id", ""))
        if not rack_id:
            continue
        try:
            frequency = float(row.get("pick_frequency") or 0)
        except (TypeError, ValueError):
            frequency = 0.0
        rack_frequency[rack_id] = rack_frequency.get(rack_id, 0.0) + frequency
        rack_rows.setdefault(rack_id, []).append(row)
        try:
            rack_order.setdefault(rack_id, int(row.get("rack_rank") or 10**9))
        except (TypeError, ValueError):
            rack_order.setdefault(rack_id, 10**9)

    total_frequency = sum(rack_frequency.values())
    ranking = []
    cumulative = 0.0
    ranked_racks = sorted(
        rack_frequency,
        key=lambda rack_id: (
            -rack_frequency[rack_id],
            rack_order.get(rack_id, 10**9),
            rack_id,
        ),
    )
    for rank, rack_id in enumerate(ranked_racks, start=1):
        frequency = rack_frequency[rack_id]
        share = frequency / total_frequency if total_frequency else 0.0
        cumulative += share
        rack_class = rack_frequency_class(cumulative)
        record = {
            "rack_id": rack_id,
            "rack_frequency_rank": rank,
            "rack_pick_frequency": frequency,
            "rack_frequency_share": share,
            "rack_cumulative_frequency_share": cumulative,
            "rack_velocity_class": rack_class,
        }
        ranking.append(record)
        for row in rack_rows[rack_id]:
            row.update(record)

    for row in rows:
        row.setdefault("rack_frequency_rank", "")
        row.setdefault("rack_pick_frequency", "")
        row.setdefault("rack_frequency_share", "")
        row.setdefault("rack_cumulative_frequency_share", "")
        row.setdefault("rack_velocity_class", "")
    return ranking


def physical_allocation_bucket(profile: dict, physical_enabled: bool) -> str:
    if not physical_enabled:
        return "STANDARD"
    storage_class = str(profile.get("storage_class", "")).upper()
    if storage_class == "STANDARD":
        return "STANDARD"
    return storage_class or "EXCEPTION"


def allocation_candidate_key(
    candidate: dict,
    profile: dict,
    physical_enabled: bool,
    velocity_class: str,
    overrides: dict,
    rack_state: dict[str, dict],
    levels_per_rack: int,
    occupied_positions: list[dict],
    ergonomic_weight_heuristic: bool,
    fill_rack_before_next: bool,
) -> tuple:
    """Rank a slot with ABC rack grouping ahead of physical preferences."""
    state = rack_state.get(candidate["rack_id"], {})
    rack_classes = state.get("velocity_classes", set())
    rack_physical_buckets = state.get("physical_buckets", set())
    rack_has_oversize = bool(state.get("has_oversize"))
    physical_bucket = physical_allocation_bucket(
        profile, physical_enabled
    )
    physical_class = str(profile.get("storage_class", "")).upper()
    is_oversize = physical_class in {
        "OVERSIZE", "OVERSIZE_AND_OVERWEIGHT", "UNVERIFIED_OVERSIZE",
    }

    # ABC purity is a tie-breaker after storage utilization. A rack may mix
    # classes because an already-open compatible rack is filled first.
    class_mix_penalty = int(
        bool(rack_classes) and velocity_class not in rack_classes
    )

    exception_buckets = {
        "OVERSIZE", "OVERWEIGHT", "OVERSIZE_AND_OVERWEIGHT",
        "UNVERIFIED_OVERSIZE", "UNKNOWN_SIZE", "UNKNOWN_WEIGHT",
        "NON_VOLUMETRIC_DATA", "EXCEPTION",
    }
    physical_bucket_is_exception = physical_bucket in exception_buckets
    rack_has_only_exceptions = bool(
        rack_physical_buckets
    ) and rack_physical_buckets.issubset(exception_buckets)

    # Keep standard and exception inventory separated, but allow exception
    # categories to share a rack. This permits overweight, oversize, and
    # unknown-data inventory to mix when it improves rack utilisation.
    if physical_bucket in rack_physical_buckets:
        physical_mix_penalty = 0
    elif physical_bucket_is_exception and rack_has_only_exceptions:
        physical_mix_penalty = 0
    elif not rack_physical_buckets:
        physical_mix_penalty = 1
    else:
        physical_mix_penalty = 2

    preferred_oversize_level = min(3, levels_per_rack)
    preferred_overweight_level = min(2, levels_per_rack)
    overweight_level_penalty = 0
    if (
        physical_enabled
        and physical_class in {"OVERWEIGHT", "OVERSIZE_AND_OVERWEIGHT"}
        and not profile.get("weight_heuristic_disabled", False)
    ):
        overweight_level_penalty = int(
            int(candidate["level"]) != preferred_overweight_level
        )
    oversize_level_penalty = 0
    if (
        is_oversize
        and physical_class not in {"OVERWEIGHT", "OVERSIZE_AND_OVERWEIGHT"}
        and "STANDARD" in rack_physical_buckets
    ):
        oversize_level_penalty = int(
            int(candidate["level"]) != preferred_oversize_level
        )
    elif physical_bucket == "STANDARD" and rack_has_oversize:
        oversize_level_penalty = int(
            int(candidate["level"]) == preferred_oversize_level
        )

    # Any occupied compatible rack wins over an empty rack. This deliberately
    # permits ABC mixing to avoid opening partially filled racks.
    if fill_rack_before_next:
        rack_reuse_penalty = 0 if rack_classes else 1
    elif rack_classes and velocity_class in rack_classes:
        rack_reuse_penalty = 0
    elif not rack_classes:
        rack_reuse_penalty = 1
    else:
        rack_reuse_penalty = 2

    exception_inventory = str(
        profile.get("storage_class", "STANDARD")
    ).upper() != "STANDARD"
    location_is_exception = candidate[
        "effective_location_attributes"
    ].get(OVERSIZE_CAPABLE_KEY) is True
    location_type_penalty = int(
        exception_inventory != location_is_exception
    )

    try:
        sku_weight = float(
            profile.get("values", {}).get(PHYSICAL_WEIGHT_KEY, 0)
        )
    except (TypeError, ValueError):
        sku_weight = 0.0
    if (
        ergonomic_weight_heuristic
        and sku_weight > 0
        and occupied_positions
    ):
        occupied_center = sum(
            float(item["level"]) for item in occupied_positions
        ) / len(occupied_positions)
        ergonomic_level_penalty = abs(
            occupied_center - (levels_per_rack + 1) / 2.0
        )
    else:
        ergonomic_level_penalty = 0.0

    return (
        # L02 overweight and detailed exception category preferences are
        # stronger than rack reuse; mixed racks remain a fallback because
        # these are ranking penalties, not hard filters.
        overweight_level_penalty,
        physical_mix_penalty,
        oversize_level_penalty,
        rack_reuse_penalty,
        class_mix_penalty,
        bool(overrides),
        len(overrides),
        location_type_penalty,
        ergonomic_level_penalty,
        candidate_sort_key(candidate, profile, physical_enabled),
    )
