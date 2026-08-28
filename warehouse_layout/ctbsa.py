"""Paper-faithful C&TBSA clustering with quantity-load SKU replication.

The implementation follows Lee, Chung, and Yoon (2020): a permutation
chromosome is divided into fixed-capacity clusters, NSGA-II maximizes
within-cluster SKU co-appearance while minimizing the maximum cluster demand,
PMX performs crossover, and a two-position (2-opt) swap performs mutation.
Each stock quantity load is an item in that original formulation, allowing one
logical SKU to occupy multiple clusters without changing either objective.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np

from .affinity import AffinityAnalysis


CancelCallback = Callable[[], bool]
ProgressCallback = Callable[[int, int, str], None]


class CtbsaCancelledError(RuntimeError):
    """Raised when the C&TBSA search is cancelled."""


@dataclass(frozen=True, slots=True)
class CtbsaParameters:
    """NSGA-II settings used by the paper's final C&TBSA search."""

    population_size: int = 100
    crossover_probability: float = 0.9
    mutation_probability: float = 0.1
    generations: int = 50_000
    random_seed: int = 0
    selected_solution: int = 3
    extended_selected_solution: int | None = None

    def validate(self) -> None:
        if self.population_size < 2:
            raise ValueError("C&TBSA population size must be at least 2")
        if self.generations < 1:
            raise ValueError("C&TBSA generations must be at least 1")
        if not 0 <= self.crossover_probability <= 1:
            raise ValueError("C&TBSA crossover probability must be between 0 and 1")
        if not 0 <= self.mutation_probability <= 1:
            raise ValueError("C&TBSA mutation probability must be between 0 and 1")
        if not 1 <= self.selected_solution <= 5:
            raise ValueError("C&TBSA selected solution must be between 1 and 5")
        if (
            self.extended_selected_solution is not None
            and not 1 <= self.extended_selected_solution <= 5
        ):
            raise ValueError(
                "extended C&TBSA selected solution must be Auto or between 1 and 5"
            )


@dataclass(slots=True)
class CtbsaSearchResult:
    """Pareto solutions and the paper-style five representative layouts."""

    selected_chromosome: np.ndarray
    selected_clusters: list[list[int]]
    pareto: list[dict]
    representative_solutions: list[dict]
    parameters: CtbsaParameters
    selection: dict


@dataclass(slots=True)
class CtbsaPlacementPlan:
    """Paper Stage-2 targets for the warehouse slot allocator."""

    # Targets and ranks are keyed by inventory-load ID. The legacy field names
    # remain in the serialized API because older layouts contain one load/SKU.
    target_racks: dict[str, str]
    rank_by_sku: dict[str, int]
    optimized_skus: tuple[str, ...]
    fixed_skus: tuple[str, ...]
    optimized_loads: tuple[str, ...]
    fixed_loads: tuple[str, ...]
    cluster_rows: list[dict]
    pareto_rows: list[dict]
    parameters: dict


class CtbsaNsga2:
    """NSGA-II implementation of the paper's clustering model."""

    def __init__(
        self,
        demands: np.ndarray,
        correlations: np.ndarray,
        cluster_capacities: list[int] | tuple[int, ...],
        *,
        real_item_count: int | None = None,
        cluster_zone_ids: list[str] | tuple[str, ...] | None = None,
        zone_capacities: dict[str, int] | None = None,
    ):
        self.demands = np.asarray(demands, dtype=np.int64)
        self.correlations = np.asarray(correlations, dtype=np.int64)
        self.capacities = tuple(int(value) for value in cluster_capacities)
        if not self.capacities or any(value < 1 for value in self.capacities):
            raise ValueError("C&TBSA cluster capacities must be positive")
        self.chromosome_size = sum(self.capacities)
        self.real_item_count = (
            len(self.demands) if real_item_count is None else int(real_item_count)
        )
        if self.real_item_count < 0 or self.real_item_count > self.chromosome_size:
            raise ValueError("invalid C&TBSA real item count")
        if len(self.demands) != self.real_item_count:
            raise ValueError("C&TBSA demand vector must contain every real SKU")
        if self.correlations.shape != (self.real_item_count, self.real_item_count):
            raise ValueError("C&TBSA correlation matrix has the wrong shape")
        self.boundaries = np.cumsum((0, *self.capacities), dtype=np.int64)
        self.cluster_zone_ids = (
            tuple(str(value or "Z01") for value in cluster_zone_ids)
            if cluster_zone_ids is not None else None
        )
        if (
            self.cluster_zone_ids is not None
            and len(self.cluster_zone_ids) != len(self.capacities)
        ):
            raise ValueError("C&TBSA cluster zone count must match cluster count")
        self.zone_capacities = {
            str(zone): int(value) for zone, value in (zone_capacities or {}).items()
        }
        if self.cluster_zone_ids is not None:
            for zone in self.cluster_zone_ids:
                if self.zone_capacities.get(zone, 0) <= 0:
                    raise ValueError(
                        f"C&TBSA zone {zone} must have positive compatible capacity"
                    )
        first, second = np.triu_indices(self.real_item_count, 1)
        weights = self.correlations[first, second]
        positive = weights > 0
        self.edge_first = first[positive]
        self.edge_second = second[positive]
        self.edge_weights = weights[positive]

    def clusters(self, chromosome: np.ndarray) -> list[list[int]]:
        return [
            [int(item) for item in chromosome[start:end] if item < self.real_item_count]
            for start, end in zip(self.boundaries[:-1], self.boundaries[1:])
        ]

    def evaluate(self, chromosome: np.ndarray) -> tuple[int, int] | tuple[int, int, float]:
        cluster_by_item = np.empty(self.real_item_count, dtype=np.int32)
        cluster_demands = np.zeros(len(self.capacities), dtype=np.int64)
        for cluster, (start, end) in enumerate(
            zip(self.boundaries[:-1], self.boundaries[1:])
        ):
            items = chromosome[start:end]
            items = items[items < self.real_item_count]
            cluster_by_item[items] = cluster
            cluster_demands[cluster] = self.demands[items].sum(dtype=np.int64)
        correlation = int(self.edge_weights[
            cluster_by_item[self.edge_first] == cluster_by_item[self.edge_second]
        ].sum(dtype=np.int64))
        maximum_demand = int(cluster_demands.max(initial=0))
        if self.cluster_zone_ids is None:
            return correlation, maximum_demand
        zone_demands = {zone: 0 for zone in self.zone_capacities}
        for index, demand in enumerate(cluster_demands):
            zone = self.cluster_zone_ids[index]
            zone_demands[zone] = zone_demands.get(zone, 0) + int(demand)
        maximum_zone_demand = max(
            (
                zone_demands.get(zone, 0) / capacity
                for zone, capacity in self.zone_capacities.items()
                if capacity > 0
            ),
            default=0.0,
        )
        return correlation, maximum_demand, float(maximum_zone_demand)

    @staticmethod
    def _dominates(first: tuple, second: tuple) -> bool:
        # Affinity is maximized; shelf and optional zone workload are minimized.
        no_worse = first[0] >= second[0] and all(
            left <= right for left, right in zip(first[1:], second[1:])
        )
        strictly_better = first[0] > second[0] or any(
            left < right for left, right in zip(first[1:], second[1:])
        )
        return no_worse and strictly_better

    def _fronts(self, objectives: list[tuple]) -> list[list[int]]:
        dominates = [[] for _ in objectives]
        dominated_count = [0] * len(objectives)
        first_front = []
        for index, value in enumerate(objectives):
            for other, other_value in enumerate(objectives):
                if index == other:
                    continue
                if self._dominates(value, other_value):
                    dominates[index].append(other)
                elif self._dominates(other_value, value):
                    dominated_count[index] += 1
            if dominated_count[index] == 0:
                first_front.append(index)
        fronts = [first_front]
        while fronts[-1]:
            following = []
            for index in fronts[-1]:
                for other in dominates[index]:
                    dominated_count[other] -= 1
                    if dominated_count[other] == 0:
                        following.append(other)
            if following:
                fronts.append(following)
            else:
                break
        return fronts

    @staticmethod
    def _crowding(front: list[int], objectives: list[tuple]) -> dict[int, float]:
        distances = {index: 0.0 for index in front}
        if len(front) <= 2:
            return {index: float("inf") for index in front}
        for objective_index in range(len(objectives[0])):
            ordered = sorted(front, key=lambda index: objectives[index][objective_index])
            distances[ordered[0]] = distances[ordered[-1]] = float("inf")
            low = objectives[ordered[0]][objective_index]
            high = objectives[ordered[-1]][objective_index]
            if high == low:
                continue
            for position in range(1, len(ordered) - 1):
                before = objectives[ordered[position - 1]][objective_index]
                after = objectives[ordered[position + 1]][objective_index]
                distances[ordered[position]] += (after - before) / (high - low)
        return distances

    @staticmethod
    def _pmx(
        first: np.ndarray, second: np.ndarray, rng: np.random.Generator
    ) -> tuple[np.ndarray, np.ndarray]:
        size = len(first)
        if size < 2:
            return first.copy(), second.copy()
        cut1, cut2 = sorted(rng.choice(size, size=2, replace=False))
        cut2 += 1

        def child(parent_a: np.ndarray, parent_b: np.ndarray) -> np.ndarray:
            result = np.full(size, -1, dtype=np.int32)
            result[cut1:cut2] = parent_a[cut1:cut2]
            positions = {int(value): index for index, value in enumerate(parent_b)}
            segment = set(int(value) for value in result[cut1:cut2])
            for index in range(cut1, cut2):
                value = int(parent_b[index])
                if value in segment:
                    continue
                target = index
                while cut1 <= target < cut2:
                    target = positions[int(parent_a[target])]
                result[target] = value
            missing = result < 0
            result[missing] = parent_b[missing]
            return result

        return child(first, second), child(second, first)

    @staticmethod
    def _mutate(chromosome: np.ndarray, rng: np.random.Generator) -> np.ndarray:
        result = chromosome.copy()
        if len(result) >= 2:
            first, second = rng.choice(len(result), size=2, replace=False)
            result[first], result[second] = result[second], result[first]
        return result

    @staticmethod
    def _five_representatives(pareto: list[dict]) -> list[dict]:
        ordered = sorted(
            pareto,
            key=lambda row: (row["maximum_cluster_demand"], row["correlation"]),
        )
        if len(ordered) <= 5:
            return ordered
        targets = np.linspace(0, len(ordered) - 1, 5)
        selected = []
        used = set()
        for target in targets:
            index = int(round(float(target)))
            while index in used and index + 1 < len(ordered):
                index += 1
            used.add(index)
            selected.append(ordered[index])
        return selected

    @staticmethod
    def _extended_selection(pareto: list[dict]) -> tuple[list[dict], dict]:
        """Return five stable 3-D representatives and normalized knee metadata."""
        correlation_values = [float(row["correlation"]) for row in pareto]
        shelf_values = [float(row["maximum_cluster_demand"]) for row in pareto]
        zone_values = [float(row["maximum_zone_demand"]) for row in pareto]
        best_correlation = max(correlation_values, default=0.0)
        lowest_correlation = min(correlation_values, default=best_correlation)
        best_shelf = min(shelf_values, default=0.0)
        worst_shelf = max(shelf_values, default=best_shelf)
        best_zone = min(zone_values, default=0.0)
        worst_zone = max(zone_values, default=best_zone)

        def normalized(row):
            correlation_range = best_correlation - lowest_correlation
            shelf_range = worst_shelf - best_shelf
            zone_range = worst_zone - best_zone
            return (
                (best_correlation - row["correlation"]) / correlation_range
                if correlation_range else 0.0,
                (row["maximum_cluster_demand"] - best_shelf) / shelf_range
                if shelf_range else 0.0,
                (row["maximum_zone_demand"] - best_zone) / zone_range
                if zone_range else 0.0,
            )

        def signature(row):
            return tuple(int(value) for value in row["chromosome"])

        for row in pareto:
            vector = normalized(row)
            row["normalized_objectives"] = vector
            row["normalized_knee_score"] = float(
                np.sqrt(sum(value * value for value in vector))
            )
            row["degradation_from_ideal"] = {
                "affinity_percent": (
                    100.0 * (best_correlation - row["correlation"]) / best_correlation
                    if best_correlation else 0.0
                ),
                "shelf_workload_percent": (
                    100.0 * (row["maximum_cluster_demand"] - best_shelf) / best_shelf
                    if best_shelf else 0.0
                ),
                "zone_workload_percent": (
                    100.0 * (row["maximum_zone_demand"] - best_zone) / best_zone
                    if best_zone else 0.0
                ),
            }
        affinity = min(
            pareto,
            key=lambda row: (-row["correlation"], row["maximum_cluster_demand"],
                             row["maximum_zone_demand"], signature(row)),
        )
        shelf = min(
            pareto,
            key=lambda row: (row["maximum_cluster_demand"],
                             row["maximum_zone_demand"], -row["correlation"],
                             signature(row)),
        )
        knee = min(
            pareto,
            key=lambda row: (row["normalized_knee_score"],
                             row["maximum_cluster_demand"],
                             row["maximum_zone_demand"], -row["correlation"],
                             signature(row)),
        )
        zone = min(
            pareto,
            key=lambda row: (row["maximum_zone_demand"],
                             row["maximum_cluster_demand"], -row["correlation"],
                             signature(row)),
        )
        representatives = []
        roles = []
        for role, row in (
            ("affinity_extreme", affinity), ("shelf_extreme", shelf),
            ("normalized_knee", knee), ("zone_extreme", zone),
        ):
            if row not in representatives:
                representatives.append(row)
                roles.append(role)
        if len(representatives) < min(5, len(pareto)):
            remaining = [row for row in pareto if row not in representatives]

            def separation(row):
                vector = row["normalized_objectives"]
                return min(
                    np.sqrt(sum(
                        (left - right) ** 2
                        for left, right in zip(
                            vector, selected["normalized_objectives"]
                        )
                    ))
                    for selected in representatives
                )

            diverse = max(
                remaining,
                key=lambda row: (separation(row), -row["normalized_knee_score"],
                                 -row["maximum_cluster_demand"],
                                 -row["maximum_zone_demand"], row["correlation"],
                                 tuple(-value for value in signature(row))),
            )
            representatives.append(diverse)
            roles.append("maximum_spread")
        for role, row in zip(roles, representatives):
            row["representative_role"] = role
        return representatives, {
            "automatic_rule": "equal_weight_normalized_euclidean_knee",
            "ideal": {
                "maximum_correlation": best_correlation,
                "minimum_shelf_workload": best_shelf,
                "minimum_zone_workload": best_zone,
            },
            "knee_score": knee["normalized_knee_score"],
            "knee_objectives": {
                "correlation": knee["correlation"],
                "maximum_cluster_demand": knee["maximum_cluster_demand"],
                "maximum_zone_demand": knee["maximum_zone_demand"],
            },
        }

    def run(
        self,
        parameters: CtbsaParameters | None = None,
        *,
        progress: ProgressCallback | None = None,
        cancelled: CancelCallback | None = None,
    ) -> CtbsaSearchResult:
        parameters = parameters or CtbsaParameters()
        parameters.validate()
        rng = np.random.default_rng(parameters.random_seed)
        base = np.arange(self.chromosome_size, dtype=np.int32)
        population = [rng.permutation(base) for _ in range(parameters.population_size)]

        for generation in range(parameters.generations):
            if cancelled and cancelled():
                raise CtbsaCancelledError("C&TBSA optimization was cancelled")
            objectives = [self.evaluate(chromosome) for chromosome in population]
            fronts = self._fronts(objectives)
            ranks = {
                index: rank for rank, front in enumerate(fronts) for index in front
            }
            crowding = {}
            for front in fronts:
                crowding.update(self._crowding(front, objectives))

            def tournament() -> np.ndarray:
                first, second = rng.choice(len(population), size=2, replace=False)
                key_first = (ranks[first], -crowding[first], first)
                key_second = (ranks[second], -crowding[second], second)
                return population[first if key_first <= key_second else second]

            offspring = []
            while len(offspring) < parameters.population_size:
                first, second = tournament(), tournament()
                if rng.random() < parameters.crossover_probability:
                    child1, child2 = self._pmx(first, second, rng)
                else:
                    child1, child2 = first.copy(), second.copy()
                if rng.random() < parameters.mutation_probability:
                    child1 = self._mutate(child1, rng)
                if rng.random() < parameters.mutation_probability:
                    child2 = self._mutate(child2, rng)
                offspring.extend((child1, child2))
            combined = population + offspring[: parameters.population_size]
            combined_objectives = [self.evaluate(value) for value in combined]
            next_population = []
            for front in self._fronts(combined_objectives):
                crowd = self._crowding(front, combined_objectives)
                ordered = sorted(front, key=lambda index: (-crowd[index], index))
                remaining = parameters.population_size - len(next_population)
                next_population.extend(combined[index] for index in ordered[:remaining])
                if len(next_population) == parameters.population_size:
                    break
            population = next_population
            if progress and (
                generation == 0
                or generation + 1 == parameters.generations
                or (generation + 1) % max(1, parameters.generations // 100) == 0
            ):
                progress(
                    generation + 1,
                    parameters.generations,
                    f"C&TBSA NSGA-II generation {generation + 1:,}/{parameters.generations:,}",
                )

        objectives = [self.evaluate(chromosome) for chromosome in population]
        front = self._fronts(objectives)[0]
        unique = {}
        for index in front:
            objective = objectives[index]
            unique.setdefault(objective, population[index].copy())
        pareto = []
        for objective, chromosome in unique.items():
            row = {
                "correlation": objective[0],
                "maximum_cluster_demand": objective[1],
                "chromosome": chromosome,
            }
            if len(objective) == 3:
                row["maximum_zone_demand"] = objective[2]
            pareto.append(row)
        if self.cluster_zone_ids is None:
            representatives = self._five_representatives(pareto)
            selected_index = min(parameters.selected_solution, len(representatives)) - 1
            selected = representatives[selected_index]
            selection = {
                "mode": "legacy_representative",
                "selected_representative": selected_index + 1,
                "selection_reason": "paper two-objective representative",
            }
        else:
            representatives, selection = self._extended_selection(pareto)
            override = parameters.extended_selected_solution
            if override is None:
                selected = min(
                    pareto,
                    key=lambda row: (
                        row["normalized_knee_score"],
                        row["maximum_cluster_demand"],
                        row["maximum_zone_demand"], -row["correlation"],
                        tuple(int(value) for value in row["chromosome"]),
                    ),
                )
                selection.update({
                    "mode": "automatic_knee",
                    "selected_representative": None,
                    "selection_reason": "minimum equal-weight normalized distance to ideal",
                })
            else:
                selected_index = min(override, len(representatives)) - 1
                selected = representatives[selected_index]
                selection.update({
                    "mode": "manual_representative",
                    "selected_representative": selected_index + 1,
                    "selection_reason": f"manual extended representative {selected_index + 1}",
                })
            selection["selected_knee_score"] = selected["normalized_knee_score"]
            selection["selected_degradation_from_ideal"] = dict(
                selected["degradation_from_ideal"]
            )
        return CtbsaSearchResult(
            selected_chromosome=selected["chromosome"].copy(),
            selected_clusters=self.clusters(selected["chromosome"]),
            pareto=sorted(
                pareto,
                key=lambda row: (
                    row["maximum_cluster_demand"],
                    row.get("maximum_zone_demand", 0.0), -row["correlation"]
                ),
            ),
            representative_solutions=representatives,
            parameters=parameters,
            selection=selection,
        )


class CtbsaPlacementPlanner:
    """Adapt the paper's equal-location storage areas to complete AMR shelves."""

    def __init__(self, slotting_service):
        self.slotting = slotting_service

    @staticmethod
    def _serializable_solution(row: dict, solution_number: int) -> dict:
        result = {
            "solution": solution_number,
            "correlation": int(row["correlation"]),
            "maximum_cluster_demand": int(row["maximum_cluster_demand"]),
        }
        if "maximum_zone_demand" in row:
            result.update({
                "maximum_zone_demand": float(row["maximum_zone_demand"]),
                "normalized_knee_score": float(row["normalized_knee_score"]),
                "degradation_from_ideal": dict(row["degradation_from_ideal"]),
                "representative_role": row.get("representative_role", "tradeoff"),
            })
        return result

    def build(
        self,
        baseline_rows: list[dict],
        building: dict,
        analysis: AffinityAnalysis,
        *,
        levels_per_rack: int,
        slots_per_level: int,
        zone_assignments: dict[str, str] | None = None,
        location_attributes: dict[str, dict] | None = None,
        attribute_catalog=None,
        zone_workload_enabled: bool = False,
        rack_traffic_costs: dict[str, dict[str, float]] | None = None,
        parameters: CtbsaParameters | None = None,
        progress: ProgressCallback | None = None,
        cancelled: CancelCallback | None = None,
    ) -> CtbsaPlacementPlan:
        parameters = parameters or CtbsaParameters()
        parameters.validate()
        if levels_per_rack < 1 or slots_per_level < 1:
            raise ValueError("C&TBSA rack capacity must be positive")
        capacity = levels_per_rack * slots_per_level
        rack_traffic_costs = rack_traffic_costs or {}
        _level, racks, _workstations, _unreachable = self.slotting.rack_distances(
            building
        )
        self.slotting.apply_zone_local_aisles(
            building, racks, zone_assignments or {}, "Z01"
        )
        rack_by_id = {str(rack["rack_id"]): rack for rack in racks}
        rows_by_rack: dict[str, list[dict]] = {}
        assigned_by_load = {}
        fixed_loads = set()
        legacy_counts: dict[str, int] = {}
        for row in baseline_rows:
            sku = str(row.get("sku", ""))
            legacy_counts[sku] = legacy_counts.get(sku, 0) + 1
            load_id = str(row.get("inventory_load_id", "")).strip()
            if not load_id:
                load_id = (
                    sku if legacy_counts[sku] == 1
                    else f"{sku}#LEGACY{legacy_counts[sku]:03d}"
                )
                row["inventory_load_id"] = load_id
            if row.get("assignment_status") != "ASSIGNED":
                fixed_loads.add(load_id)
                continue
            assigned_by_load[load_id] = row
            rack_id = str(row.get("rack_id", ""))
            rows_by_rack.setdefault(rack_id, []).append(row)

        clean_racks = []
        for rack_id, rack in rack_by_id.items():
            rows = rows_by_rack.get(rack_id, [])
            clean = True
            for row in rows:
                requirements = row.get("sku_requirements") or {}
                profile = self.slotting.attributes.physical_profile(requirements)
                if (
                    profile["data_status"] != "COMPLETE"
                    or profile["storage_class"] != "STANDARD"
                    or int(row.get("occupied_slot_count") or 1) != 1
                    or str(row.get("handling_unit_type", "")) != "AMR shelf"
                ):
                    clean = False
                    break
            if clean:
                clean_racks.append(rack)
            else:
                fixed_loads.update(
                    str(row.get("inventory_load_id", "")) for row in rows
                )

        optimized_loads = sorted({
            str(row.get("inventory_load_id", ""))
            for rack in clean_racks
            for row in rows_by_rack.get(str(rack["rack_id"]), [])
            if str(row.get("inventory_load_id", ""))
        })
        if not optimized_loads:
            raise ValueError("C&TBSA found no complete standard AMR-shelf SKUs to cluster")
        optimized_skus = sorted({
            str(assigned_by_load[load_id].get("sku", ""))
            for load_id in optimized_loads
        })
        fixed_skus = sorted({
            str(row.get("sku", "")) for load_id, row in assigned_by_load.items()
            if load_id in fixed_loads
        })

        zone_attributes = dict(location_attributes or {})
        configured_attribute_keys = (
            self.slotting.attributes.configured_zone_attribute_keys(
                zone_attributes
            )
        )
        sku_attribute_keys = {
            str(key)
            for row in baseline_rows
            for key in (row.get("sku_requirements") or {})
        }
        active_attribute_keys = sorted(
            configured_attribute_keys & sku_attribute_keys
        )
        definitions = self.slotting.attributes.normalize_catalog(
            attribute_catalog
        )

        def rack_attribute_profile(rack: dict) -> tuple:
            zone = str(rack.get("zone_id", ""))
            effective, _sources = (
                self.slotting.attributes.effective_attributes(
                    zone, zone_attributes
                )
            )
            return tuple(
                (
                    key,
                    key in effective
                    or definitions.get(key) is not None
                    and definitions[key].value_type == "boolean",
                    (
                        effective.get(key, False)
                        if definitions.get(key) is not None
                        and definitions[key].value_type == "boolean"
                        else effective.get(key)
                    ),
                )
                for key in active_attribute_keys
            )

        groups: dict[tuple, list[dict]] = {}
        rack_profiles = {}
        for rack in clean_racks:
            profile = rack_attribute_profile(rack)
            groups.setdefault(profile, []).append(rack)
            rack_profiles[str(rack["rack_id"])] = profile

        dataset_index = {
            sku: index for index, sku in enumerate(analysis.dataset.skus)
        }
        target_racks = {
            load_id: str(row.get("rack_id", ""))
            for load_id, row in assigned_by_load.items()
            if load_id in fixed_loads and row.get("rack_id")
        }
        rank_by_sku = {}
        cluster_rows = []
        pareto_rows = []
        pareto_frontier_rows = []
        extended_selection_rows = []
        rank = 0
        active_groups = sorted(
            (
                (profile, values)
                for profile, values in groups.items()
                if values
            ),
            key=lambda item: repr(item[0]),
        )
        for group_number, (profile, group_racks) in enumerate(
            active_groups, start=1
        ):
            attribute_profile = {
                key: value if defined else None
                for key, defined, value in profile
            }
            profile_label = (
                " · ".join(
                    f"{key}={value!s}"
                    for key, value in attribute_profile.items()
                )
                or "no active zone attributes"
            )
            group_loads = [
                load_id for load_id in optimized_loads
                if rack_profiles.get(
                    str(assigned_by_load[load_id].get("rack_id", ""))
                ) == profile
            ]
            if not group_loads:
                continue
            group_racks = sorted(
                group_racks,
                key=lambda rack: (
                    *((str(rack.get("zone_id") or "Z01"),)
                      if zone_workload_enabled else ()),
                    not np.isfinite(float(rack["distance_m"])),
                    float(rack["distance_m"]),
                    str(rack["rack_id"]),
                ),
            )
            available = len(group_racks) * capacity
            if len(group_loads) > available:
                raise ValueError(
                    f"C&TBSA profile {profile_label} requires "
                    f"{len(group_loads):,} locations but only {available:,} are available"
                )
            group_skus = [
                str(assigned_by_load[load_id].get("sku", ""))
                for load_id in group_loads
            ]
            source_indices = [dataset_index.get(sku) for sku in group_skus]
            demands = np.zeros(len(group_loads), dtype=np.int64)
            for sku in sorted(set(group_skus)):
                members = [
                    index for index, value in enumerate(group_skus)
                    if value == sku
                ]
                dataset_position = dataset_index.get(sku)
                total_demand = (
                    int(analysis.sku_store_day_totals[dataset_position])
                    if dataset_position is not None else 0
                )
                weights = []
                for index in members:
                    try:
                        weight = float(
                            assigned_by_load[group_loads[index]].get("quantity_ea")
                            or 1
                        )
                    except (TypeError, ValueError):
                        weight = 1.0
                    weights.append(max(0.0, weight))
                if not sum(weights):
                    weights = [1.0] * len(members)
                raw = [total_demand * value / sum(weights) for value in weights]
                allocated = [int(np.floor(value)) for value in raw]
                remainder = total_demand - sum(allocated)
                order = sorted(
                    range(len(members)),
                    key=lambda index: (-(raw[index] - allocated[index]), group_loads[members[index]]),
                )
                for index in order[:remainder]:
                    allocated[index] += 1
                for member, value in zip(members, allocated):
                    demands[member] = value
            correlations = np.zeros((len(group_loads), len(group_loads)), dtype=np.int64)
            known_positions = [
                position for position, index in enumerate(source_indices)
                if index is not None
            ]
            if known_positions:
                known_indices = [source_indices[position] for position in known_positions]
                correlations[np.ix_(known_positions, known_positions)] = (
                    analysis.shared_store_days[np.ix_(known_indices, known_indices)]
                )
            for first in range(len(group_loads)):
                for second in range(first + 1, len(group_loads)):
                    if group_skus[first] == group_skus[second]:
                        correlations[first, second] = 0
                        correlations[second, first] = 0
            zone_capacities: dict[str, int] = {}
            for rack in group_racks:
                zone = str(rack.get("zone_id") or "Z01")
                zone_capacities[zone] = zone_capacities.get(zone, 0) + capacity
            optimizer = CtbsaNsga2(
                demands,
                correlations,
                [capacity] * len(group_racks),
                real_item_count=len(group_loads),
                cluster_zone_ids=(
                    [str(rack.get("zone_id") or "Z01") for rack in group_racks]
                    if zone_workload_enabled else None
                ),
                zone_capacities=(zone_capacities if zone_workload_enabled else None),
            )

            def group_progress(current: int, total: int, message: str) -> None:
                if progress:
                    overall = (group_number - 1) * total + current
                    progress(overall, len(active_groups) * total, message)

            result = optimizer.run(
                parameters, progress=group_progress, cancelled=cancelled
            )
            representatives = [
                self._serializable_solution(row, index)
                for index, row in enumerate(result.representative_solutions, start=1)
            ]
            for row in representatives:
                pareto_rows.append({
                    **row,
                    "storage_class": profile_label,
                    "attribute_profile": dict(attribute_profile),
                })
            representative_by_objective = {
                (
                    row["correlation"], row["maximum_cluster_demand"],
                    row.get("maximum_zone_demand"),
                ): index
                for index, row in enumerate(
                    result.representative_solutions, start=1
                )
            }
            for row in result.pareto:
                objective = (
                    row["correlation"], row["maximum_cluster_demand"],
                    row.get("maximum_zone_demand"),
                )
                pareto_frontier_rows.append({
                    "storage_class": profile_label,
                    "attribute_profile": dict(attribute_profile),
                    "correlation": int(row["correlation"]),
                    "maximum_cluster_demand": int(
                        row["maximum_cluster_demand"]
                    ),
                    **({
                        "maximum_zone_demand": float(row["maximum_zone_demand"]),
                        "normalized_knee_score": float(
                            row["normalized_knee_score"]
                        ),
                        "degradation_from_ideal": dict(
                            row["degradation_from_ideal"]
                        ),
                    } if "maximum_zone_demand" in row else {}),
                    "representative_solution": representative_by_objective.get(
                        objective
                    ),
                    "representative_role": row.get("representative_role"),
                    "selected": bool(np.array_equal(
                        row["chromosome"], result.selected_chromosome
                    )),
                })
            if zone_workload_enabled:
                extended_selection_rows.append({
                    "storage_class": profile_label,
                    "attribute_profile": dict(attribute_profile),
                    **result.selection,
                })
            cluster_records = []
            for index, items in enumerate(result.selected_clusters):
                cluster_demand = int(demands[items].sum(dtype=np.int64)) if items else 0
                cluster_records.append((cluster_demand, index, items))
            if zone_workload_enabled:
                # The chromosome fixes each cluster's zone. Within that zone,
                # pair hotter clusters with lower-flow racks; never cross zones.
                selected_pairs = []
                for zone in sorted(zone_capacities):
                    zone_records = sorted(
                        (
                            record for record in cluster_records
                            if str(group_racks[record[1]].get("zone_id") or "Z01")
                            == zone
                        ),
                        key=lambda record: (-record[0], record[1]),
                    )
                    zone_racks = sorted(
                        (
                            rack for rack in group_racks
                            if str(rack.get("zone_id") or "Z01") == zone
                        ),
                        key=lambda rack: (
                            float(rack_traffic_costs.get(
                                str(rack["rack_id"]), {}
                            ).get("resource_flow_per_visit", 0.0)),
                            float(rack_traffic_costs.get(
                                str(rack["rack_id"]), {}
                            ).get("travel_per_visit", rack.get("distance_m", 0.0))),
                            str(rack["rack_id"]),
                        ),
                    )
                    selected_pairs.extend(zip(zone_records, zone_racks))
                cluster_records = [record for record, _rack in selected_pairs]
                rack_targets = [rack for _record, rack in selected_pairs]
            else:
                cluster_records.sort(key=lambda value: (-value[0], value[1]))
                rack_targets = list(group_racks)
            rng = np.random.default_rng(parameters.random_seed + group_number)
            for accessible_rank, ((cluster_demand, source_cluster, items), rack) in enumerate(
                zip(cluster_records, rack_targets), start=1
            ):
                ordered_items = list(rng.permutation(items)) if items else []
                load_ids = [group_loads[int(item)] for item in ordered_items]
                skus = [
                    str(assigned_by_load[load_id].get("sku", ""))
                    for load_id in load_ids
                ]
                for load_id in load_ids:
                    rank += 1
                    rank_by_sku[load_id] = rank
                    target_racks[load_id] = str(rack["rack_id"])
                cluster_rows.append({
                    "storage_class": profile_label,
                    "attribute_profile": dict(attribute_profile),
                    "source_cluster": source_cluster + 1,
                    "accessible_rank": accessible_rank,
                    "rack_id": str(rack["rack_id"]),
                    "selected_zone": str(rack.get("zone_id") or "Z01"),
                    "rack_distance": float(rack["distance_m"]),
                    "sku_count": len(skus),
                    "cluster_demand": cluster_demand,
                    "skus": skus,
                    "inventory_load_ids": load_ids,
                })

        for load_id in sorted(fixed_loads):
            rank += 1
            rank_by_sku[load_id] = rank
        return CtbsaPlacementPlan(
            target_racks=target_racks,
            rank_by_sku=rank_by_sku,
            optimized_skus=tuple(sorted(optimized_skus)),
            fixed_skus=tuple(sorted(fixed_skus)),
            optimized_loads=tuple(sorted(optimized_loads)),
            fixed_loads=tuple(sorted(fixed_loads)),
            cluster_rows=cluster_rows,
            pareto_rows=pareto_rows,
            parameters={
                "method": "Lee_Chung_Yoon_2020_CTBSA",
                "population_size": parameters.population_size,
                "crossover_probability": parameters.crossover_probability,
                "mutation_probability": parameters.mutation_probability,
                "generations": parameters.generations,
                "random_seed": parameters.random_seed,
                "selected_solution": parameters.selected_solution,
                "extended_selected_solution": parameters.extended_selected_solution,
                "cluster_unit": "AMR shelf",
                "storage_locations_per_cluster": capacity,
                "active_zone_attribute_keys": active_attribute_keys,
                "attribute_profile_count": len(active_groups),
                "paper_hard_constraints": (
                    "each inventory load assigned to exactly one cluster; "
                    "load count in cluster k does not exceed Z_k"
                ),
                "multi_rack_sku_extension": (
                    "logical SKU demand is quantity-weighted across independent "
                    "inventory loads; original correlation and maximum-cluster-"
                    "demand objectives are preserved, with optional normalized "
                    "zone demand as objective three"
                ),
                "warehouse_hard_constraints": (
                    "AMR shelf only; unique occupied addresses; map-active SKU "
                    "attribute profiles separated; physical exceptions and "
                    "incomplete-data racks fixed"
                ),
                "simulation_validation": False,
                "zone_workload_enabled": bool(zone_workload_enabled),
                "zone_workload_objective_order": (
                    "pareto: maximize affinity, minimize maximum shelf demand, "
                    "minimize maximum normalized zone demand; post-assignment: "
                    "minimize attributed zone traffic, expected travel, stable rack ID"
                ),
                "zone_workload_normalization": "compatible_usable_rack_slot_capacity",
                "zone_traffic_attribution": "destination_rack_zone",
                "zone_optimization_status": (
                    "ENABLED" if zone_workload_enabled else "DISABLED_COMPATIBILITY_PATH"
                ),
                "objective_mode": (
                    "three_objective_affinity_shelf_zone"
                    if zone_workload_enabled else "paper_two_objective"
                ),
                "extended_selection": extended_selection_rows,
                "pareto_frontier": pareto_frontier_rows,
                "post_assignment_policy": (
                    "preserve_pareto_zone_then_minimize_zone_traffic_and_travel"
                    if zone_workload_enabled
                    else "demand_ranked_accessible_racks"
                ),
            },
        )
