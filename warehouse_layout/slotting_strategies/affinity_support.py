"""Affinity-specific ordering, tuning, and layout metrics."""

from __future__ import annotations

import math

import numpy as np

from ..affinity import AffinityAnalysis


def build_order_membership_masks(
    analysis: AffinityAnalysis, sku_names: set[str] | None = None
) -> tuple[dict[str, int], int]:
    """Encode each SKU's store-day fulfillment groups as a compact bit mask."""
    dataset = analysis.dataset
    selected = (
        (dataset.dates >= analysis.start_date.toordinal())
        & (dataset.dates <= analysis.end_date.toordinal())
    )
    dates = dataset.dates[selected]
    stores = dataset.store_indices[selected]
    sku_indices = dataset.sku_indices[selected]
    order = np.lexsort((sku_indices, stores, dates))
    dates = dates[order]
    stores = stores[order]
    sku_indices = sku_indices[order]
    unique_presence = np.ones(len(order), dtype=bool)
    unique_presence[1:] = (
        (dates[1:] != dates[:-1])
        | (stores[1:] != stores[:-1])
        | (sku_indices[1:] != sku_indices[:-1])
    )
    dates = dates[unique_presence]
    stores = stores[unique_presence]
    sku_indices = sku_indices[unique_presence]
    group_start = np.ones(len(sku_indices), dtype=bool)
    group_start[1:] = (
        (dates[1:] != dates[:-1]) | (stores[1:] != stores[:-1])
    )
    group_indices = np.cumsum(group_start, dtype=np.int32) - 1
    group_count = int(group_indices[-1]) + 1 if len(group_indices) else 0
    masks = [0] * len(dataset.skus)
    for sku_index, group_index in zip(sku_indices, group_indices):
        masks[int(sku_index)] |= 1 << int(group_index)
    allowed = sku_names if sku_names is not None else set(dataset.skus)
    return {
        str(sku): masks[index]
        for index, sku in enumerate(dataset.skus)
        if str(sku) in allowed and masks[index]
    }, group_count


def order_rack_touch_metrics(
    analysis: AffinityAnalysis,
    rows: list[dict],
    order_masks: dict[str, int] | None = None,
) -> dict:
    """Measure distinct handling-unit rack touches per store-day group."""
    if order_masks is None:
        order_masks, group_count = build_order_membership_masks(analysis)
    else:
        group_count = analysis.store_day_count
    sku_rack_weights: dict[str, dict[str, float]] = {}
    for row in rows:
        if row.get("assignment_status") != "ASSIGNED":
            continue
        rack_id = str(row.get("rack_id", ""))
        sku = str(row.get("sku", ""))
        if rack_id and sku in order_masks:
            try:
                weight = float(row.get("quantity_ea") or 1)
            except (TypeError, ValueError):
                weight = 1.0
            sku_rack_weights.setdefault(sku, {})[rack_id] = (
                sku_rack_weights.setdefault(sku, {}).get(rack_id, 0.0)
                + max(0.0, weight)
            )
    rack_masks: dict[str, int] = {}
    for sku, rack_weights in sorted(sku_rack_weights.items()):
        assigned = {rack_id: 0 for rack_id in rack_weights}
        remaining = order_masks.get(sku, 0)
        while remaining:
            bit = remaining & -remaining
            rack_id = min(
                rack_weights,
                key=lambda value: (
                    (assigned[value] + 1) / (rack_weights[value] or 1.0),
                    value,
                ),
            )
            rack_masks[rack_id] = rack_masks.get(rack_id, 0) | bit
            assigned[rack_id] += 1
            remaining ^= bit
    touches = np.zeros(group_count, dtype=np.int32)
    for mask in rack_masks.values():
        remaining = mask
        while remaining:
            least_bit = remaining & -remaining
            touches[least_bit.bit_length() - 1] += 1
            remaining ^= least_bit
    observed = touches[touches > 0]
    total = int(observed.sum()) if len(observed) else 0
    return {
        "fulfillment_group_count": int(len(observed)),
        "total_rack_touches": total,
        "average_racks_per_group": (
            float(observed.mean()) if len(observed) else 0.0
        ),
        "median_racks_per_group": (
            float(np.median(observed)) if len(observed) else 0.0
        ),
        "p95_racks_per_group": (
            float(np.percentile(observed, 95)) if len(observed) else 0.0
        ),
        "one_rack_group_fraction": (
            float(np.mean(observed == 1)) if len(observed) else 0.0
        ),
    }


def handling_unit_visit_metrics(
    analysis: AffinityAnalysis,
    rows: list[dict],
    handling_unit_type: str,
) -> dict:
    """Rank deliverable units by distinct Store ID + Date task visits."""
    sku_names = {
        str(row.get("sku", "")) for row in rows
        if row.get("assignment_status") == "ASSIGNED"
    }
    order_masks, _group_count = build_order_membership_masks(
        analysis, sku_names
    )
    unit_masks: dict[str, int] = {}
    unit_locations: dict[str, dict] = {}
    sku_sources: dict[str, dict[str, dict]] = {}
    for row in rows:
        if row.get("assignment_status") != "ASSIGNED":
            continue
        sku = str(row.get("sku", ""))
        sku_mask = order_masks.get(sku, 0)
        if not sku_mask:
            continue
        occupied_units = row.get("occupied_handling_units") or []
        if handling_unit_type == "AMR shelf" or not occupied_units:
            occupied_units = [{
                "handling_unit_id": row.get("handling_unit_id", ""),
                "rack_id": row.get("rack_id", ""),
                "storage_level": row.get("storage_level", ""),
                "storage_slot": row.get("storage_slot", ""),
            }]
        physical_units = set()
        for location in occupied_units:
            unit_id = str(location.get("handling_unit_id", "")).strip()
            if not unit_id:
                continue
            physical_units.add(unit_id)
            unit_locations.setdefault(unit_id, dict(location))
        primary = str(row.get("handling_unit_id", "")).strip()
        if not primary or not physical_units:
            continue
        try:
            weight = float(row.get("quantity_ea") or 1)
        except (TypeError, ValueError):
            weight = 1.0
        source = sku_sources.setdefault(sku, {}).setdefault(
            primary, {"units": set(), "weight": 0.0}
        )
        source["units"].update(physical_units)
        source["weight"] += max(0.0, weight)
    for sku, sources in sorted(sku_sources.items()):
        assigned = {primary: 0 for primary in sources}
        remaining = order_masks.get(sku, 0)
        while remaining:
            bit = remaining & -remaining
            primary = min(
                sources,
                key=lambda value: (
                    (assigned[value] + 1) / (sources[value]["weight"] or 1.0),
                    value,
                ),
            )
            for unit_id in sources[primary]["units"]:
                unit_masks[unit_id] = unit_masks.get(unit_id, 0) | bit
            assigned[primary] += 1
            remaining ^= bit
    ranked = sorted(
        unit_masks,
        key=lambda unit_id: (-unit_masks[unit_id].bit_count(), unit_id),
    )
    total_visits = sum(unit_masks[unit_id].bit_count() for unit_id in ranked)
    cumulative = 0
    units = []
    for rank, unit_id in enumerate(ranked, start=1):
        visits = unit_masks[unit_id].bit_count()
        previous_share = cumulative / total_visits if total_visits else 0.0
        cumulative += visits
        cumulative_share = cumulative / total_visits if total_visits else 0.0
        movement_class = (
            "A" if previous_share < 0.80
            else "B" if previous_share < 0.95
            else "C"
        )
        units.append({
            **unit_locations[unit_id],
            "handling_unit_id": unit_id,
            "visit_rank": rank,
            "visit_count": visits,
            "visit_rate": visits / analysis.store_day_count,
            "visit_share": visits / total_visits if total_visits else 0.0,
            "cumulative_visit_share": cumulative_share,
            "movement_class": movement_class,
        })
    return {
        "grouping": "Store ID + Date",
        "fulfillment_group_count": analysis.store_day_count,
        "handling_unit_type": handling_unit_type,
        "ranking_level": (
            "shelf" if handling_unit_type == "AMR shelf" else "slot"
        ),
        "total_handling_unit_visits": total_visits,
        "unit_count": len(units),
        "units": units,
    }


def build_affinity_neighbors(
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


def affinity_placement_order(
    base_order: list[dict],
    affinity_neighbors: dict[str, list[tuple[str, float, float, int]]],
    affinity_weight: float,
    order_masks: dict[str, int] | None = None,
    rack_capacity: int | None = None,
) -> list[dict]:
    """Blend affinity-cluster traversal with the existing ABC order.

    ``affinity_weight`` is the affinity ratio and ``1 - affinity_weight`` is
    the ABC ratio. At 1.0, ABC class/order does not influence the traversal
    except for deterministic ties; at 0.0, the incoming ABC/frequency order
    is kept unchanged.
    """
    if affinity_weight <= 0 or len(base_order) < 2:
        return list(base_order)
    rows_by_sku: dict[str, list[dict]] = {}
    base_position: dict[str, int] = {}
    for position, row in enumerate(base_order):
        sku = str(row.get("sku", ""))
        rows_by_sku.setdefault(sku, []).append(row)
        base_position.setdefault(sku, position)
    representative = {sku: rows[0] for sku, rows in rows_by_sku.items()}

    def expand_load_order(sku_order: list[str]) -> list[dict]:
        """Preserve every quantity load after ordering logical SKUs."""
        load_indices = sorted({
            int(row.get("quantity_load_index") or 1)
            for rows in rows_by_sku.values() for row in rows
        })
        expanded = []
        for load_index in load_indices:
            for sku in sku_order:
                expanded.extend(sorted(
                    (
                        row for row in rows_by_sku[sku]
                        if int(row.get("quantity_load_index") or 1) == load_index
                    ),
                    key=lambda row: str(row.get("inventory_load_id", "")),
                ))
        return expanded
    class_rank = {"A": 0.0, "B": 0.5, "C": 1.0}
    abc_weight = 1.0 - affinity_weight
    if order_masks:
        remaining = set(rows_by_sku)
        ordered = []
        cluster_mask = 0
        cluster_size = 0
        capacity = max(1, int(rack_capacity or len(base_order)))
        while remaining:
            if cluster_size == 0:
                maximum_signal = max(
                    order_masks.get(sku, 0).bit_count() for sku in remaining
                ) or 1
                affinity_cost = {
                    sku: 1.0
                    - order_masks.get(sku, 0).bit_count() / maximum_signal
                    for sku in remaining
                }
            else:
                overlaps = {
                    sku: (
                        order_masks.get(sku, 0) & cluster_mask
                    ).bit_count()
                    for sku in remaining
                }
                maximum_signal = max(overlaps.values(), default=0) or 1
                affinity_cost = {
                    sku: 1.0 - overlaps[sku] / maximum_signal
                    for sku in remaining
                }
            selected = min(
                remaining,
                key=lambda sku: (
                    abc_weight * class_rank.get(
                        str(representative[sku].get("velocity_class", "")).upper(),
                        1.0,
                    )
                    + affinity_weight * affinity_cost[sku],
                    affinity_cost[sku],
                    base_position[sku] if abc_weight > 0 else 0,
                    sku,
                ),
            )
            remaining.remove(selected)
            ordered.append(selected)
            cluster_mask |= order_masks.get(selected, 0)
            cluster_size += 1
            if cluster_size >= capacity:
                cluster_mask = 0
                cluster_size = 0
        return expand_load_order(ordered)
    strongest_edge = max(
        (
            float(edge[1])
            for edges in affinity_neighbors.values()
            for edge in edges
        ),
        default=1.0,
    ) or 1.0
    remaining = set(rows_by_sku)
    connection_strength = {sku: 0.0 for sku in remaining}
    seed_strength = {
        sku: max(
            (float(edge[1]) for edge in affinity_neighbors.get(sku, [])),
            default=0.0,
        )
        for sku in remaining
    }
    ordered = []
    while remaining:
        selected = min(
            remaining,
            key=lambda sku: (
                abc_weight
                * class_rank.get(
                    str(representative[sku].get("velocity_class", "")).upper(),
                    1.0,
                )
                + affinity_weight
                * (
                    1.0
                    - (
                        connection_strength[sku]
                        if connection_strength[sku] > 0
                        else seed_strength[sku]
                    )
                    / strongest_edge
                ),
                -connection_strength[sku],
                -seed_strength[sku],
                base_position[sku] if abc_weight > 0 else 0,
                sku,
            ),
        )
        remaining.remove(selected)
        ordered.append(selected)
        for related_sku, relationship_weight, _score, _shared in (
            affinity_neighbors.get(selected, [])
        ):
            if related_sku in remaining:
                connection_strength[related_sku] = max(
                    connection_strength[related_sku],
                    float(relationship_weight),
                )
    return expand_load_order(ordered)


def affinity_physical_signature(candidate_key: tuple) -> tuple:
    """Keep physical-fit preferences fixed while ABC competes with affinity."""
    return (
        candidate_key[0],
        candidate_key[1],
        candidate_key[2],
        candidate_key[5],
        candidate_key[6],
        candidate_key[7],
        candidate_key[8],
    )


def empirical_service_cap_candidates(racks: list[dict]) -> list[float]:
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


def normalized_metric(values: list[float], value: float) -> float:
    finite = [item for item in values if math.isfinite(item)]
    if not finite:
        return 0.0
    low, high = min(finite), max(finite)
    return 0.0 if high <= low else (value - low) / (high - low)


def affinity_layout_metrics(
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
    classes_by_rack: dict[str, set[str]] = {}
    for row in assigned.values():
        rack_id = str(row.get("rack_id", ""))
        if rack_id:
            classes_by_rack.setdefault(rack_id, set()).add(
                str(row.get("velocity_class", "")).upper()
            )
    mixed_abc_racks = sum(
        len(classes - {""}) > 1 for classes in classes_by_rack.values()
    )
    return {
        "weighted_pair_distance_m": (
            pair_distance / pair_weight if pair_weight else 0.0
        ),
        "pair_weight": pair_weight,
        "same_rack_affinity_fraction": (
            same_rack_weight / pair_weight if pair_weight else 0.0
        ),
        "affinity_pair_same_bay_fraction": (
            same_rack_weight / pair_weight if pair_weight else 0.0
        ),
        "occupied_rack_count": len(classes_by_rack),
        "mixed_abc_rack_count": mixed_abc_racks,
        "abc_pure_rack_fraction": (
            (len(classes_by_rack) - mixed_abc_racks) / len(classes_by_rack)
            if classes_by_rack else 0.0
        ),
        "retained_relationship_count": retained_pairs,
        "weighted_service_distance_m": (
            service_distance / service_weight if service_weight else 0.0
        ),
        "service_weight": service_weight,
    }
