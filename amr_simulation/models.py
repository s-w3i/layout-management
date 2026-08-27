"""Configuration and data records shared by the simulator."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any


CONFIG_SCHEMA = "amr_simulation_config/v1"


def grid_position(value: str) -> tuple[int, int]:
    try:
        column, row = str(value).removeprefix("G").split("_")
        return int(column), int(row)
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"invalid grid node: {value!r}") from exc


def grid_name(position: tuple[int, int]) -> str:
    return f"G{position[0]}_{position[1]}"


@dataclass(frozen=True, slots=True)
class MotionProfile:
    max_linear_speed_mps: float = 1.5
    linear_acceleration_mps2: float = 0.75
    max_angular_speed_degps: float = 90.0
    angular_acceleration_degps2: float = 90.0

    def validate(self) -> None:
        if any(
            not math.isfinite(value) or value <= 0
            for value in asdict(self).values()
        ):
            raise ValueError("motion profile values must be finite and positive")


@dataclass(frozen=True, slots=True)
class SimulationConfig:
    amr_count: int
    spawn_nodes: tuple[str, ...]
    initial_heading_degrees: float
    workstations: tuple[str, ...]
    store_workstation_overrides: dict[str, str]
    jack_up_seconds: float
    jack_down_seconds: float
    service_seconds: float
    motion: MotionProfile
    detailed_event_log: bool = False
    schema: str = CONFIG_SCHEMA

    @classmethod
    def load(cls, path: Path) -> "SimulationConfig":
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot read simulation configuration: {exc}") from exc
        if raw.get("schema") != CONFIG_SCHEMA:
            raise ValueError(f"configuration schema must be {CONFIG_SCHEMA!r}")
        motion = MotionProfile(**(raw.get("motion") or {}))
        config = cls(
            amr_count=int(raw.get("amr_count", 0)),
            spawn_nodes=tuple(str(value) for value in raw.get("spawn_nodes", [])),
            initial_heading_degrees=float(raw.get("initial_heading_degrees", 0.0)),
            workstations=tuple(str(value) for value in raw.get("workstations", [])),
            store_workstation_overrides={
                str(key): str(value)
                for key, value in (raw.get("store_workstation_overrides") or {}).items()
            },
            jack_up_seconds=float(raw.get("jack_up_seconds", 4.0)),
            jack_down_seconds=float(raw.get("jack_down_seconds", 4.0)),
            service_seconds=float(raw.get("service_seconds", 10.0)),
            motion=motion,
            detailed_event_log=bool(raw.get("detailed_event_log", False)),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if self.amr_count < 1 or len(self.spawn_nodes) != self.amr_count:
            raise ValueError("spawn_nodes must contain exactly one node per AMR")
        if len(set(self.spawn_nodes)) != len(self.spawn_nodes):
            raise ValueError("spawn_nodes must be distinct")
        for value in self.spawn_nodes:
            grid_position(value)
        if not self.workstations or len(set(self.workstations)) != len(self.workstations):
            raise ValueError("workstations must be a non-empty unique list")
        if not math.isfinite(self.initial_heading_degrees):
            raise ValueError("initial heading must be finite")
        if any(
            not math.isfinite(value) or value < 0
            for value in (
                self.jack_up_seconds,
                self.jack_down_seconds,
                self.service_seconds,
            )
        ):
            raise ValueError("handling and service times must be finite and non-negative")
        self.motion.validate()

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "amr_count": self.amr_count,
            "spawn_nodes": list(self.spawn_nodes),
            "initial_heading_degrees": self.initial_heading_degrees,
            "workstations": list(self.workstations),
            "store_workstation_overrides": dict(self.store_workstation_overrides),
            "jack_up_seconds": self.jack_up_seconds,
            "jack_down_seconds": self.jack_down_seconds,
            "service_seconds": self.service_seconds,
            "motion": asdict(self.motion),
            "detailed_event_log": self.detailed_event_log,
        }


@dataclass(frozen=True, slots=True)
class WorkloadTask:
    task_id: str
    task_date: date
    release_seconds: float
    store_id: str
    line_counts: dict[str, int]

    @property
    def source_lines(self) -> int:
        return sum(self.line_counts.values())


@dataclass(slots=True)
class Workload:
    tasks: list[WorkloadTask]
    min_date: date
    max_date: date
    source_path: Path
    valid_rows: int
    cache_used: bool = False

    def select(self, start: date | None = None, end: date | None = None) -> list[WorkloadTask]:
        first, last = start or self.min_date, end or self.max_date
        if first > last:
            raise ValueError("start date must be on or before end date")
        selected = [task for task in self.tasks if first <= task.task_date <= last]
        if not selected:
            raise ValueError("the selected date range contains no tasks")
        return selected


@dataclass(frozen=True, slots=True)
class Rack:
    rack_id: str
    position: tuple[int, int]
    skus: frozenset[str]


@dataclass(slots=True)
class ValidationReport:
    valid: bool = True
    errors: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)

    def error(self, code: str, message: str, **details: Any) -> None:
        self.valid = False
        self.errors.append({"code": code, "message": message, **details})

    def warning(self, code: str, message: str, **details: Any) -> None:
        self.warnings.append({"code": code, "message": message, **details})

    def to_dict(self) -> dict[str, Any]:
        return {"valid": self.valid, "errors": self.errors, "warnings": self.warnings}


@dataclass(slots=True)
class MotionSegment:
    job_id: str
    amr_id: str
    kind: str
    start_time: float
    end_time: float
    start_position: tuple[float, float]
    end_position: tuple[float, float]
    start_heading: float
    end_heading: float
    start_rate: float = 0.0
    acceleration: float = 0.0
    loaded: bool = False

    def pose_at(self, when: float) -> tuple[float, float, float]:
        elapsed = min(max(when - self.start_time, 0.0), self.end_time - self.start_time)
        if self.kind.startswith("linear_"):
            distance = self.start_rate * elapsed + 0.5 * self.acceleration * elapsed**2
            return (
                self.start_position[0] + math.cos(self.start_heading) * distance,
                self.start_position[1] + math.sin(self.start_heading) * distance,
                self.start_heading,
            )
        if self.kind.startswith("rotate_"):
            heading = self.start_heading + self.start_rate * elapsed + 0.5 * self.acceleration * elapsed**2
            return self.start_position[0], self.start_position[1], (heading + math.pi) % (2 * math.pi) - math.pi
        return (*self.start_position, self.start_heading)


@dataclass(slots=True)
class DayResult:
    metrics: dict[str, Any]
    events: list[dict[str, Any]] = field(default_factory=list)
    motion_segments: list[MotionSegment] = field(default_factory=list)
    jobs: list[dict[str, Any]] = field(default_factory=list)
    paths: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
