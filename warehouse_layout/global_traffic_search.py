"""Automatic parameter portfolio for global traffic optimization."""

from __future__ import annotations

import copy
import csv
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .global_traffic import (
    GlobalTrafficCancelledError,
    GlobalTrafficResult,
)


@dataclass(frozen=True, slots=True)
class GlobalTrafficSearchScenario:
    """One travel/relocation constraint combination."""

    scenario_id: str
    maximum_travel_increase: float
    maximum_relocation_fraction: float


@dataclass(slots=True)
class GlobalTrafficSearchTrial:
    """One completed or failed screening/final solve."""

    phase: str
    scenario: GlobalTrafficSearchScenario
    time_limit_seconds: float
    result: GlobalTrafficResult | None = None
    error: str = ""

    @property
    def accepted(self) -> bool:
        return self.result is not None


@dataclass(slots=True)
class GlobalTrafficSearchResult:
    """Best accepted layout and the complete parameter comparison."""

    best_result: GlobalTrafficResult
    best_trial: GlobalTrafficSearchTrial
    trials: list[GlobalTrafficSearchTrial]
    ranking_policy: tuple[str, ...]


SearchProgress = Callable[[int, int, str], None]
ScenarioOptimizer = Callable[
    [GlobalTrafficSearchScenario, float, SearchProgress | None],
    GlobalTrafficResult,
]


class GlobalTrafficParameterSearch:
    """Screen a deterministic portfolio and refine its best scenarios."""

    RANKING_POLICY = (
        "controllable_resource_peak",
        "controllable_resource_p95",
        "shared_neighbourhood_peak",
        "controllable_top_5_percent_mean",
        "zone_peak",
        "expected_travel",
        "relocation_count",
    )

    @staticmethod
    def default_scenarios() -> tuple[GlobalTrafficSearchScenario, ...]:
        return tuple(
            GlobalTrafficSearchScenario(
                f"T{travel:02d}_R{relocation:03d}",
                travel / 100.0,
                relocation / 100.0,
            )
            for travel in (0, 5, 10)
            for relocation in (50, 75, 100)
        )

    @staticmethod
    def ranking_key(result: GlobalTrafficResult) -> tuple[float, ...]:
        balance = result.balance_metrics
        controllable = balance["controllable_resources_after"]
        return (
            float(controllable["peak_load"]),
            float(controllable["p95_load"]),
            float(
                balance["neighbourhood_after"][
                    "peak_normalized_load"
                ]
            ),
            float(controllable["top_5_percent_mean"]),
            float(balance["zone_after"]["peak_normalized_load"]),
            float(result.after.metrics["expected_travel"]),
            float(len(result.relocations)),
        )

    def search(
        self,
        optimize: ScenarioOptimizer,
        *,
        screening_seconds: float = 90.0,
        final_seconds: float = 300.0,
        finalist_count: int = 3,
        scenarios: tuple[GlobalTrafficSearchScenario, ...] | None = None,
        progress: SearchProgress | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> GlobalTrafficSearchResult:
        if screening_seconds <= 0 or final_seconds <= 0:
            raise ValueError("search solve times must be positive")
        scenarios = scenarios or self.default_scenarios()
        if not scenarios:
            raise ValueError("parameter search needs at least one scenario")
        finalist_count = min(max(1, int(finalist_count)), len(scenarios))
        total_trials = len(scenarios) + finalist_count
        trials: list[GlobalTrafficSearchTrial] = []

        def run_trial(
            phase: str,
            scenario: GlobalTrafficSearchScenario,
            seconds: float,
            trial_index: int,
        ) -> GlobalTrafficSearchTrial:
            if cancelled and cancelled():
                raise GlobalTrafficCancelledError(
                    "Global parameter search was cancelled"
                )

            def shifted(current: int, total: int, message: str) -> None:
                if progress:
                    fraction = current / max(1, total)
                    progress(
                        trial_index - 1 + fraction,
                        total_trials,
                        (
                            f"{phase.title()} {trial_index}/{total_trials} · "
                            f"{scenario.scenario_id} · {message}"
                        ),
                    )

            if progress:
                progress(
                    trial_index - 1,
                    total_trials,
                    (
                        f"{phase.title()} {trial_index}/{total_trials} · "
                        f"{scenario.scenario_id}"
                    ),
                )
            trial = GlobalTrafficSearchTrial(
                phase, scenario, seconds
            )
            try:
                trial.result = optimize(scenario, seconds, shifted)
            except GlobalTrafficCancelledError:
                raise
            except Exception as exc:  # Keep searching after one bad portfolio run.
                trial.error = str(exc)
            return trial

        for index, scenario in enumerate(scenarios, start=1):
            trials.append(
                run_trial(
                    "screening",
                    scenario,
                    screening_seconds,
                    index,
                )
            )
        accepted_screening = [
            trial for trial in trials if trial.accepted
        ]
        if not accepted_screening:
            details = "; ".join(
                f"{trial.scenario.scenario_id}: {trial.error}"
                for trial in trials
            )
            raise RuntimeError(
                "No screening scenario produced a valid layout. " + details
            )
        finalist_count = min(finalist_count, len(accepted_screening))
        total_trials = len(scenarios) + finalist_count
        finalists = sorted(
            accepted_screening,
            key=lambda trial: self.ranking_key(trial.result),
        )[:finalist_count]
        for offset, finalist in enumerate(finalists, start=1):
            trials.append(
                run_trial(
                    "final",
                    finalist.scenario,
                    final_seconds,
                    len(scenarios) + offset,
                )
            )
        accepted = [trial for trial in trials if trial.accepted]
        best = min(
            accepted,
            key=lambda trial: self.ranking_key(trial.result),
        )
        result = best.result
        result.output_payload.setdefault(
            "global_traffic_configuration", {}
        )["parameter_search"] = {
            "status": "COMPLETED",
            "selected_scenario": best.scenario.scenario_id,
            "selected_phase": best.phase,
            "screening_seconds": screening_seconds,
            "final_seconds": final_seconds,
            "scenario_count": len(scenarios),
            "finalist_count": len(finalists),
            "accepted_trial_count": len(accepted),
            "failed_trial_count": len(trials) - len(accepted),
            "ranking_policy": list(self.RANKING_POLICY),
        }
        result.output_payload.setdefault("operation_log", []).append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "operation": "global_traffic_parameter_search",
            "selected_scenario": best.scenario.scenario_id,
            "accepted_trial_count": len(accepted),
            "failed_trial_count": len(trials) - len(accepted),
        })
        return GlobalTrafficSearchResult(
            result, best, trials, self.RANKING_POLICY
        )

    @classmethod
    def trial_row(
        cls,
        trial: GlobalTrafficSearchTrial,
        selected: GlobalTrafficSearchTrial,
    ) -> dict:
        row = {
            "phase": trial.phase,
            "scenario": trial.scenario.scenario_id,
            "selected": (
                trial is selected
                or (
                    trial.phase == selected.phase
                    and trial.scenario == selected.scenario
                )
            ),
            "accepted": trial.accepted,
            "maximum_travel_increase_percent": (
                trial.scenario.maximum_travel_increase * 100
            ),
            "maximum_relocated_percent": (
                trial.scenario.maximum_relocation_fraction * 100
            ),
            "time_limit_seconds": trial.time_limit_seconds,
            "error": trial.error,
        }
        if not trial.result:
            return row
        result = trial.result
        balance = result.balance_metrics
        controllable = balance["controllable_resources_after"]
        row.update({
            "solver_status": result.solver["status"],
            "relative_gap": result.solver["relative_gap"],
            "controllable_peak": controllable["peak_load"],
            "controllable_p95": controllable["p95_load"],
            "controllable_top_5_percent_mean": (
                controllable["top_5_percent_mean"]
            ),
            "neighbourhood_peak": balance["neighbourhood_after"][
                "peak_normalized_load"
            ],
            "zone_peak": balance["zone_after"][
                "peak_normalized_load"
            ],
            "expected_travel": result.after.metrics["expected_travel"],
            "relocation_count": len(result.relocations),
        })
        return row

    @classmethod
    def save(
        cls,
        search_result: GlobalTrafficSearchResult,
        output_path: Path,
        selected_trial: GlobalTrafficSearchTrial | None = None,
    ) -> tuple[Path, Path, Path]:
        selected = selected_trial or search_result.best_trial
        if not selected.accepted:
            raise ValueError("cannot save a failed auto-search trial")
        output = Path(output_path).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        payload = copy.deepcopy(selected.result.output_payload)
        best_metadata = (
            search_result.best_result.output_payload.get(
                "global_traffic_configuration", {}
            ).get("parameter_search", {})
        )
        metadata = dict(best_metadata)
        metadata.update({
            "status": "COMPLETED",
            "selected_scenario": selected.scenario.scenario_id,
            "selected_phase": selected.phase,
            "auto_recommended_scenario": (
                search_result.best_trial.scenario.scenario_id
            ),
            "auto_recommended_phase": search_result.best_trial.phase,
            "user_selected_trial": selected is not search_result.best_trial,
        })
        payload.setdefault(
            "global_traffic_configuration", {}
        )["parameter_search"] = metadata
        payload.setdefault("operation_log", []).append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "operation": "save_selected_global_traffic_search_trial",
            "selected_scenario": selected.scenario.scenario_id,
            "selected_phase": selected.phase,
            "auto_recommended_scenario": (
                search_result.best_trial.scenario.scenario_id
            ),
        })
        output.write_text(
            json.dumps(payload, indent=2) + "\n",
            encoding="utf-8",
        )
        report_dir = output.with_name(f"{output.stem}_search")
        report_dir.mkdir(parents=True, exist_ok=True)
        rows = [
            cls.trial_row(trial, selected)
            for trial in search_result.trials
        ]
        comparison = report_dir / "parameter_comparison.csv"
        fieldnames = list(dict.fromkeys(
            key for row in rows for key in row
        ))
        with comparison.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(
                stream, fieldnames=fieldnames, lineterminator="\n"
            )
            writer.writeheader()
            writer.writerows(rows)
        summary = report_dir / "search_summary.json"
        summary.write_text(
            json.dumps({
                "best_layout": str(output),
                "selected_scenario": (
                    selected.scenario.scenario_id
                ),
                "selected_phase": selected.phase,
                "auto_recommended_scenario": (
                    search_result.best_trial.scenario.scenario_id
                ),
                "auto_recommended_phase": search_result.best_trial.phase,
                "ranking_policy": list(search_result.ranking_policy),
                "trials": rows,
            }, indent=2) + "\n",
            encoding="utf-8",
        )
        return output, comparison, summary
