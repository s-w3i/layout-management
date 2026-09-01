from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

Node = tuple[int, int]


def node_name(node: Node) -> str:
    return f"G{node[0]}_{node[1]}"


def parse_node(value: str) -> Node:
    try:
        x, y = value.removeprefix("G").split("_")
        return int(x), int(y)
    except Exception as exc:
        raise ValueError(f"invalid grid node {value!r}") from exc


@dataclass(frozen=True, slots=True)
class Motion:
    max_linear_speed_mps: float = 1.5
    linear_acceleration_mps2: float = 0.75
    max_angular_speed_degps: float = 90.0
    angular_acceleration_degps2: float = 90.0


@dataclass(frozen=True, slots=True)
class Config:
    amr_count: int
    spawn_nodes: tuple[str, ...]
    workstations: tuple[str, ...]
    jack_up_seconds: float = 4.0
    jack_down_seconds: float = 4.0
    service_seconds: float = 10.0
    initial_heading_degrees: float = 0.0
    planner_timeout_seconds: float = 5.0
    planner_seed: int = 0
    no_progress_ticks: int = 100
    station_queue_capacity: int = 1
    workstation_entries: dict[str, str] = field(default_factory=dict)
    workstation_exits: dict[str, str] = field(default_factory=dict)
    workstation_holding_paths: dict[str, tuple[str, ...]] = field(default_factory=dict)
    detailed_trace: bool = False
    motion: Motion = Motion()
    schema: str = "lacam_dte_config/v1"

    @classmethod
    def load(cls, path: Path) -> "Config":
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("schema") != "lacam_dte_config/v1":
            raise ValueError("configuration schema must be 'lacam_dte_config/v1'")
        cfg = cls(
            amr_count=int(raw["amr_count"]),
            spawn_nodes=tuple(map(str, raw["spawn_nodes"])),
            workstations=tuple(map(str, raw["workstations"])),
            jack_up_seconds=float(raw.get("jack_up_seconds", 4)),
            jack_down_seconds=float(raw.get("jack_down_seconds", 4)),
            service_seconds=float(raw.get("service_seconds", 10)),
            initial_heading_degrees=float(raw.get("initial_heading_degrees", 0)),
            planner_timeout_seconds=float(raw.get("planner_timeout_seconds", 5)),
            planner_seed=int(raw.get("planner_seed", 0)),
            no_progress_ticks=int(raw.get("no_progress_ticks", 100)),
            station_queue_capacity=int(raw.get("station_queue_capacity", 1)),
            workstation_entries={str(k): str(v) for k, v in raw.get("workstation_entries", {}).items()},
            workstation_exits={str(k): str(v) for k, v in raw.get("workstation_exits", {}).items()},
            workstation_holding_paths={
                str(k): tuple(map(str, v)) for k, v in raw.get("workstation_holding_paths", {}).items()
            },
            detailed_trace=bool(raw.get("detailed_trace", False)),
            motion=Motion(**raw.get("motion", {})),
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if self.amr_count < 1 or len(self.spawn_nodes) < self.amr_count:
            raise ValueError("spawn_nodes must contain at least amr_count entries")
        if len(set(self.spawn_nodes[: self.amr_count])) != self.amr_count:
            raise ValueError("active spawn nodes must be unique")
        if not self.workstations or len(set(self.workstations)) != len(self.workstations):
            raise ValueError("workstations must be non-empty and unique")
        values = tuple(getattr(self.motion, k) for k in self.motion.__slots__) + (self.planner_timeout_seconds,)
        if any(not math.isfinite(v) or v <= 0 for v in values):
            raise ValueError("motion and timeout values must be positive and finite")
        if self.no_progress_ticks < 1:
            raise ValueError("no_progress_ticks must be positive")
        if self.station_queue_capacity < 1:
            raise ValueError("station_queue_capacity must be positive")

    def with_amrs(self, count: int) -> "Config":
        values = self.snapshot(); values["amr_count"] = count
        values["motion"] = Motion(**values["motion"])
        return Config(**values)

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema": self.schema, "amr_count": self.amr_count,
            "spawn_nodes": list(self.spawn_nodes), "workstations": list(self.workstations),
            "jack_up_seconds": self.jack_up_seconds, "jack_down_seconds": self.jack_down_seconds,
            "service_seconds": self.service_seconds,
            "initial_heading_degrees": self.initial_heading_degrees,
            "planner_timeout_seconds": self.planner_timeout_seconds,
            "planner_seed": self.planner_seed, "no_progress_ticks": self.no_progress_ticks,
            "station_queue_capacity": self.station_queue_capacity,
            "workstation_entries": dict(self.workstation_entries),
            "workstation_exits": dict(self.workstation_exits),
            "workstation_holding_paths": {
                station: list(path) for station, path in self.workstation_holding_paths.items()
            },
            "detailed_trace": self.detailed_trace,
            "motion": {k: getattr(self.motion, k) for k in self.motion.__slots__},
        }


@dataclass(slots=True)
class Grid:
    width: int
    height: int
    spacing_x: float
    spacing_y: float
    nodes: set[Node]
    edges: set[tuple[Node, Node]]
    racks: dict[str, Node]
    workstations: dict[str, Node]

    def coordinate(self, node: Node) -> tuple[float, float]:
        return node[0] * self.spacing_x, node[1] * self.spacing_y


@dataclass(frozen=True, slots=True)
class Task:
    task_id: str
    date: date
    store: str
    release_seconds: float
    lines: dict[str, int]


@dataclass(slots=True)
class Job:
    job_id: str
    task_id: str
    store: str
    rack_id: str
    rack_node: Node
    station_id: str
    station_node: Node
    lines: int
    release_tick: int
    station_queue_node: Node | None = None
    fifo_index: int = -1
    stage: str = "queued"
    amr_id: str | None = None
    dispatch_tick: int | None = None
    completion_tick: int | None = None


@dataclass(slots=True)
class Amr:
    amr_id: str
    node: Node
    goal: Node
    stage: str = "idle"
    job: Job | None = None
    busy_until: int = 0
    loaded: bool = False
    distance_m: float = 0.0
    wait_ticks: int = 0
    busy_ticks: int = 0
    heading_radians: float = 0.0


@dataclass(slots=True)
class DayResult:
    metrics: dict[str, Any]
    events: list[dict[str, Any]] = field(default_factory=list)
    trajectory: list[dict[str, Any]] = field(default_factory=list)
    jobs: list[dict[str, Any]] = field(default_factory=list)
