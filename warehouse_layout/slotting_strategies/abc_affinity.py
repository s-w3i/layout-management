"""ABC/affinity strategy orchestration and empirical tuning."""

from __future__ import annotations

import copy
import math

from ..affinity import AffinityAnalysis
from .affinity_support import (
    build_affinity_neighbors,
    empirical_service_cap_candidates,
    normalized_metric,
)




class AbcAffinitySlottingStrategy:
    """Blend affinity grouping with ABC while retaining shared hard rules."""

    name = "abc_affinity"

    def generate(
        self,
        service,
        building: dict,
        sku_rows: list[dict],
        affinity_analysis: AffinityAnalysis,
        affinity_weight: float,
        levels_per_rack: int = 1,
        slots_per_level: int = 6,
        handling_unit_type: str = "AMR shelf",
        zone_id: str = "Z01",
        zone_assignments: dict[str, str] | None = None,
        attribute_catalog=None,
        location_attributes: dict[str, dict] | None = None,
        tuning_parameters: dict | None = None,
        strict_compatibility: bool = False,
        storage_layout=None,
        ergonomic_weight_heuristic: bool = True,
        auto_plan_oversize: bool = True,
    ) -> tuple[list[dict], dict]:
        """Generate affinity/ABC balanced bay slotting with empirical tuning."""
        if not 0.0 <= affinity_weight <= 1.0:
            raise ValueError("affinity weight must be between 0 and 1")
        known_skus = {str(row.get("sku", "")) for row in sku_rows}
        automatic = tuning_parameters is None
        threshold_recommendation = affinity_analysis.suggest_slotting_thresholds(
            known_skus, affinity_weight
        )
        if automatic:
            minimum_shared_store_days = int(
                threshold_recommendation["minimum_shared_store_days"]
            )
            minimum_affinity_score = float(
                threshold_recommendation["minimum_affinity_score"]
            )
        else:
            try:
                minimum_shared_store_days = int(
                    tuning_parameters["minimum_shared_store_days"]
                )
                minimum_affinity_score = float(
                    tuning_parameters["minimum_affinity_score"]
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    "adjusted tuning requires minimum shared store-days and "
                    "minimum affinity score"
                ) from exc

        affinity_neighbors = build_affinity_neighbors(
            affinity_analysis,
            known_skus,
            minimum_shared_store_days,
            minimum_affinity_score,
        )

        baseline_rows, baseline_summary = service.generate_basic(
            building,
            copy.deepcopy(sku_rows),
            levels_per_rack,
            slots_per_level,
            handling_unit_type,
            zone_id,
            zone_assignments,
            attribute_catalog,
            copy.deepcopy(location_attributes or {}),
            strategy="basic",
            affinity_analysis=affinity_analysis,
            minimum_shared_store_days=minimum_shared_store_days,
            minimum_affinity_score=minimum_affinity_score,
            strict_compatibility=strict_compatibility,
            storage_layout=storage_layout,
            ergonomic_weight_heuristic=ergonomic_weight_heuristic,
            auto_plan_oversize=auto_plan_oversize,
        )
        _level, racks, _workstations, _unreachable = service.rack_distances(building)
        service.apply_zone_local_aisles(
            building, racks, zone_assignments or {}, zone_id
        )
        if automatic:
            empirical_service_caps = empirical_service_cap_candidates(racks)
            relationship_confidence = math.sqrt(
                float(threshold_recommendation["retained_weight_fraction"])
                * float(threshold_recommendation["sku_coverage_fraction"])
            )
            affinity_pressure = affinity_weight * relationship_confidence
            service_cap_index = int(
                round(affinity_pressure * (len(empirical_service_caps) - 1))
            )
            suggested_service_cap = float(
                empirical_service_caps[service_cap_index]
            )
            cap_candidates = [suggested_service_cap]
            service_cap_recommendation = {
                "method": "affinity_weighted_empirical_map_distance_quantile",
                "relationship_confidence": relationship_confidence,
                "affinity_pressure": affinity_pressure,
                "empirical_candidate_count": len(empirical_service_caps),
                "selected_candidate_index": service_cap_index,
                "selected_service_distance_increase": suggested_service_cap,
                "empirical_candidates": empirical_service_caps,
            }
        else:
            try:
                cap_candidates = [
                    float(tuning_parameters["maximum_service_distance_increase"])
                ]
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    "adjusted tuning requires maximum service-distance increase"
                ) from exc
            empirical_service_caps = cap_candidates
            service_cap_recommendation = {
                "method": "user_adjusted",
                "selected_service_distance_increase": cap_candidates[0],
            }
        if any(value < 0 for value in cap_candidates):
            raise ValueError("maximum service-distance increase cannot be negative")

        trials = []
        for service_cap in cap_candidates:
            trial_location_attributes = copy.deepcopy(location_attributes or {})
            trial_rows, trial_summary = service.generate_basic(
                building,
                copy.deepcopy(sku_rows),
                levels_per_rack,
                slots_per_level,
                handling_unit_type,
                zone_id,
                zone_assignments,
                attribute_catalog,
                trial_location_attributes,
                strategy="abc_affinity",
                affinity_analysis=affinity_analysis,
                affinity_weight=affinity_weight,
                minimum_shared_store_days=minimum_shared_store_days,
                minimum_affinity_score=minimum_affinity_score,
                maximum_service_distance_increase=service_cap,
                precomputed_affinity_neighbors=affinity_neighbors,
                strict_compatibility=strict_compatibility,
                storage_layout=storage_layout,
                ergonomic_weight_heuristic=ergonomic_weight_heuristic,
                auto_plan_oversize=auto_plan_oversize,
            )
            if trial_summary["unassigned_count"] > baseline_summary["unassigned_count"]:
                continue
            metrics = trial_summary["affinity_metrics"]
            trials.append({
                "maximum_service_distance_increase": service_cap,
                "weighted_pair_distance_m": float(
                    metrics["weighted_pair_distance_m"]
                ),
                "weighted_service_distance_m": float(
                    metrics["weighted_service_distance_m"]
                ),
                "same_rack_affinity_fraction": float(
                    metrics["same_rack_affinity_fraction"]
                ),
                "mixed_abc_rack_count": int(metrics["mixed_abc_rack_count"]),
                "summary": trial_summary,
                "rows": trial_rows,
                "location_attributes": trial_location_attributes,
            })
        if not trials:
            raise ValueError(
                "no affinity parameter candidate preserved the ABC baseline assignment count"
            )
        pair_values = [row["weighted_pair_distance_m"] for row in trials]
        service_values = [row["weighted_service_distance_m"] for row in trials]
        for trial in trials:
            pair_normalized = normalized_metric(
                pair_values, trial["weighted_pair_distance_m"]
            )
            service_normalized = normalized_metric(
                service_values, trial["weighted_service_distance_m"]
            )
            trial["selection_loss"] = math.sqrt(
                affinity_weight * pair_normalized**2
                + (1.0 - affinity_weight) * service_normalized**2
            )
        selected_trial = min(
            trials,
            key=lambda row: (
                row["selection_loss"],
                row["weighted_service_distance_m"],
                row["weighted_pair_distance_m"],
                row["maximum_service_distance_increase"],
            ),
        )
        selected_cap = float(
            selected_trial["maximum_service_distance_increase"]
        )
        final_rows = selected_trial["rows"]
        final_summary = selected_trial["summary"]
        if location_attributes is not None:
            location_attributes.clear()
            location_attributes.update(selected_trial["location_attributes"])
        baseline_metrics = baseline_summary["affinity_metrics"]
        final_metrics = final_summary["affinity_metrics"]

        def relative_change(current, baseline):
            return (
                (float(current) - float(baseline)) / float(baseline)
                if baseline
                else 0.0
            )

        final_summary["affinity_tuning"] = {
            "parameter_status": "AUTO_SUGGESTED" if automatic else "USER_ADJUSTED",
            "method": "data_driven_relationship_pareto_and_map_quantile",
            "weight_semantics": (
                "affinity_weight balances same-bay affinity consolidation "
                "against ABC bay purity"
            ),
            "affinity_weight": affinity_weight,
            "minimum_shared_store_days": minimum_shared_store_days,
            "minimum_affinity_score": minimum_affinity_score,
            "maximum_service_distance_increase": selected_cap,
            "relationship_recommendation": threshold_recommendation,
            "service_cap_recommendation": service_cap_recommendation,
            "service_cap_candidate_count": len(empirical_service_caps),
            "service_cap_evaluated_count": len(cap_candidates),
            "valid_layout_candidate_count": len(trials),
            "layout_candidates": [
                {
                    key: value for key, value in trial.items()
                    if key not in {"rows", "summary", "location_attributes"}
                }
                for trial in trials
            ],
            "source_store_day_count": affinity_analysis.store_day_count,
            "source_line_order_count": affinity_analysis.event_count,
        }
        final_summary["baseline_comparison"] = {
            "basic": baseline_metrics,
            "abc_affinity": final_metrics,
            "weighted_pair_distance_change_fraction": relative_change(
                final_metrics["weighted_pair_distance_m"],
                baseline_metrics["weighted_pair_distance_m"],
            ),
            "weighted_service_distance_change_fraction": relative_change(
                final_metrics["weighted_service_distance_m"],
                baseline_metrics["weighted_service_distance_m"],
            ),
            "same_bay_affinity_change_fraction_points": (
                final_metrics["affinity_pair_same_bay_fraction"]
                - baseline_metrics["affinity_pair_same_bay_fraction"]
            ),
            "mixed_abc_rack_count_change": (
                final_metrics["mixed_abc_rack_count"]
                - baseline_metrics["mixed_abc_rack_count"]
            ),
            "assigned_count_change": (
                final_summary["assigned_count"] - baseline_summary["assigned_count"]
            ),
        }
        return final_rows, final_summary
