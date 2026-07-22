"""Affinity-specific ordering, tuning, and layout metrics."""

from __future__ import annotations

import math

import numpy as np

from ..affinity import AffinityAnalysis


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
) -> list[dict]:
    """Blend affinity-cluster traversal with the existing ABC order.

    ``affinity_weight`` is the affinity ratio and ``1 - affinity_weight`` is
    the ABC ratio. At 1.0, ABC class/order does not influence the traversal
    except for deterministic ties; at 0.0, the incoming ABC/frequency order
    is kept unchanged.
    """
    if affinity_weight <= 0 or len(base_order) < 2:
        return list(base_order)
    rows_by_sku = {
        str(row.get("sku", "")): row for row in base_order
    }
    base_position = {
        str(row.get("sku", "")): position
        for position, row in enumerate(base_order)
    }
    class_rank = {"A": 0.0, "B": 0.5, "C": 1.0}
    abc_weight = 1.0 - affinity_weight
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
                    str(rows_by_sku[sku].get("velocity_class", "")).upper(),
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
        ordered.append(rows_by_sku[selected])
        for related_sku, relationship_weight, _score, _shared in (
            affinity_neighbors.get(selected, [])
        ):
            if related_sku in remaining:
                connection_strength[related_sku] = max(
                    connection_strength[related_sku],
                    float(relationship_weight),
                )
    return ordered


def affinity_physical_signature(candidate_key: tuple) -> tuple:
    """Keep physical-fit preferences fixed while ABC competes with affinity."""
    candidate_sort_key = candidate_key[-1]
    return (
        candidate_key[0],
        candidate_key[1],
        candidate_key[2],
        candidate_key[5],
        candidate_key[6],
        candidate_key[7],
        candidate_key[8],
        candidate_sort_key[0],
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
