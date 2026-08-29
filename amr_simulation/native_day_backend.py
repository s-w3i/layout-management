"""Build and load the one-call native day simulator."""

from __future__ import annotations

import ctypes
import json
import fcntl
import hashlib
import os
import platform
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
import math

from .models import DayResult, grid_name, grid_position

ROOT = Path(__file__).resolve().parent
SOURCE_DIR = ROOT / "native"
SOURCES = (
    SOURCE_DIR / "day_engine.cpp",
    SOURCE_DIR / "routing_motion.cpp",
    SOURCE_DIR / "simulator.cpp",
)
BUILD_INPUTS = SOURCES + tuple(sorted(SOURCE_DIR.glob("*.hpp")))
CACHE = ROOT / ".cache" / "native"


@dataclass(frozen=True, slots=True)
class SimulationBackendInfo:
    requested: str
    selected: str
    build_hash: str = ""
    compile_seconds: float = 0.0
    native_simulation_seconds: float = 0.0
    decoding_seconds: float = 0.0
    fallback_reason: str = ""


def _build() -> tuple[Path, str, float]:
    compiler = subprocess.run(
        ("g++", "--version"), capture_output=True, text=True, check=True
    ).stdout.splitlines()[0]
    digest = hashlib.sha256()
    for source in BUILD_INPUTS:
        digest.update(source.name.encode())
        digest.update(source.read_bytes())
    digest.update(compiler.encode())
    digest.update(platform.platform().encode())
    digest.update(platform.machine().encode())
    build_hash = digest.hexdigest()[:16]
    CACHE.mkdir(parents=True, exist_ok=True)
    target = CACHE / f"day-engine-{build_hash}.so"
    if target.exists():
        return target, build_hash, 0.0
    with (CACHE / "day-engine.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if target.exists():
            return target, build_hash, 0.0
        started = time.monotonic()
        descriptor, temporary_name = tempfile.mkstemp(
            prefix="day-engine-", suffix=".so", dir=CACHE
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            subprocess.run(
                (
                    "g++", "-std=c++17", "-O3", "-DNDEBUG", "-shared", "-fPIC",
                    *(str(source) for source in SOURCES), "-o", str(temporary),
                ),
                check=True,
                capture_output=True,
                text=True,
            )
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return target, build_hash, time.monotonic() - started


class NativeDayEngine:
    def __init__(self, path: Path):
        self.library = ctypes.CDLL(str(path))
        self.library.amr_day_abi_version.restype = ctypes.c_char_p
        self.library.amr_day_engine_ready.restype = ctypes.c_int32
        abi = self.library.amr_day_abi_version().decode("ascii")
        if abi != "amr-day/v1":
            raise RuntimeError(f"unsupported native day ABI: {abi}")

        class Node(ctypes.Structure):
            _fields_ = (
                ("x", ctypes.c_double),
                ("y", ctypes.c_double),
                ("grid_x", ctypes.c_int32),
                ("grid_y", ctypes.c_int32),
                ("rack", ctypes.c_int32),
            )

        class Edge(ctypes.Structure):
            _fields_ = (
                ("from_node", ctypes.c_int32),
                ("to_node", ctypes.c_int32),
                ("distance", ctypes.c_double),
            )

        class Rack(ctypes.Structure):
            _fields_ = (("node", ctypes.c_int32), ("sku_offset", ctypes.c_int32), ("sku_count", ctypes.c_int32))

        class Task(ctypes.Structure):
            _fields_ = (("station", ctypes.c_int32), ("line_offset", ctypes.c_int32), ("line_count", ctypes.c_int32))

        class TaskLine(ctypes.Structure):
            _fields_ = (("sku", ctypes.c_int32), ("count", ctypes.c_int32))

        class Spawn(ctypes.Structure):
            _fields_ = (("node", ctypes.c_int32), ("heading", ctypes.c_double))

        class Config(ctypes.Structure):
            _fields_ = (
                ("jack_up_seconds", ctypes.c_double), ("jack_down_seconds", ctypes.c_double),
                ("service_seconds", ctypes.c_double), ("reservation_wait_seconds", ctypes.c_double),
                ("dram_wait_seconds", ctypes.c_double), ("linear_speed", ctypes.c_double),
                ("linear_acceleration", ctypes.c_double), ("angular_speed", ctypes.c_double),
                ("angular_acceleration", ctypes.c_double), ("loaded_priority", ctypes.c_int32),
                ("mutex_passage", ctypes.c_int32), ("corridor_coordination", ctypes.c_int32),
                ("trace", ctypes.c_int32),
            )

        class DayInput(ctypes.Structure):
            _fields_ = (
                ("nodes", ctypes.POINTER(Node)), ("node_count", ctypes.c_int32),
                ("edges", ctypes.POINTER(Edge)), ("edge_count", ctypes.c_int32),
                ("racks", ctypes.POINTER(Rack)), ("rack_count", ctypes.c_int32),
                ("rack_skus", ctypes.POINTER(ctypes.c_int32)), ("rack_sku_count", ctypes.c_int32),
                ("tasks", ctypes.POINTER(Task)), ("task_count", ctypes.c_int32),
                ("task_lines", ctypes.POINTER(TaskLine)), ("task_line_count", ctypes.c_int32),
                ("spawns", ctypes.POINTER(Spawn)), ("spawn_count", ctypes.c_int32),
                ("stations", ctypes.POINTER(ctypes.c_int32)), ("station_count", ctypes.c_int32),
                ("config", Config),
                ("day_year", ctypes.c_int32), ("day_month", ctypes.c_int32),
                ("day_day", ctypes.c_int32),
            )

        class JobResult(ctypes.Structure):
            _fields_ = (
                ("task", ctypes.c_int32), ("rack", ctypes.c_int32),
                ("robot", ctypes.c_int32), ("station", ctypes.c_int32),
                ("covered_lines", ctypes.c_int32), ("sku_offset", ctypes.c_int32),
                ("sku_count", ctypes.c_int32), ("path_offset", ctypes.c_int32),
                ("path_count", ctypes.c_int32), ("station_queue_position", ctypes.c_int32),
                ("reservation_conflicts", ctypes.c_int32), ("reroutes", ctypes.c_int32),
                ("denial_offset", ctypes.c_int32), ("denial_count", ctypes.c_int32),
                ("last_denial_reason", ctypes.c_int32), ("last_denial_node", ctypes.c_int32),
                ("dispatch_time", ctypes.c_double), ("rack_departure", ctypes.c_double),
                ("rack_return", ctypes.c_double), ("station_queue_enter", ctypes.c_double),
                ("station_admitted", ctypes.c_double), ("station_arrival", ctypes.c_double),
                ("service_start", ctypes.c_double), ("completion_time", ctypes.c_double),
                ("travel_distance", ctypes.c_double), ("travel_seconds", ctypes.c_double),
                ("node_wait_seconds", ctypes.c_double),
                ("dram_wait_seconds", ctypes.c_double),
            )

        self.Node, self.Edge, self.Rack = Node, Edge, Rack
        self.Task, self.TaskLine, self.Spawn = Task, TaskLine, Spawn
        self.Config, self.DayInput, self.JobResult = Config, DayInput, JobResult
        self.library.amr_day_route.argtypes = (
            ctypes.POINTER(Node), ctypes.c_int32,
            ctypes.POINTER(Edge), ctypes.c_int32,
            ctypes.c_int32, ctypes.c_int32,
            ctypes.POINTER(ctypes.c_int32), ctypes.c_int32,
            ctypes.POINTER(ctypes.c_uint64), ctypes.c_int32,
            ctypes.POINTER(ctypes.c_int32), ctypes.c_int32,
            ctypes.POINTER(ctypes.c_double),
        )
        self.library.amr_day_route.restype = ctypes.c_int32
        self.library.amr_day_predicted_motion.argtypes = (
            ctypes.POINTER(Node), ctypes.c_int32,
            ctypes.POINTER(ctypes.c_int32), ctypes.c_int32,
            ctypes.c_double, ctypes.c_double, ctypes.c_double,
            ctypes.c_double, ctypes.c_double,
            ctypes.POINTER(ctypes.c_double),
        )
        self.library.amr_day_predicted_motion.restype = ctypes.c_double
        self.library.amr_day_simulate.argtypes = (ctypes.POINTER(DayInput),)
        self.library.amr_day_simulate.restype = ctypes.c_void_p
        self.library.amr_day_result_error.argtypes = (ctypes.c_void_p,)
        self.library.amr_day_result_error.restype = ctypes.c_char_p
        self.library.amr_day_result_metrics_json.argtypes = (ctypes.c_void_p,)
        self.library.amr_day_result_metrics_json.restype = ctypes.c_char_p
        self.library.amr_day_result_destroy.argtypes = (ctypes.c_void_p,)
        self.library.amr_day_result_job_count.argtypes = (ctypes.c_void_p,)
        self.library.amr_day_result_job_count.restype = ctypes.c_int32
        self.library.amr_day_result_jobs.argtypes = (ctypes.c_void_p,)
        self.library.amr_day_result_jobs.restype = ctypes.POINTER(JobResult)
        self.library.amr_day_result_job_skus.argtypes = (ctypes.c_void_p,)
        self.library.amr_day_result_job_skus.restype = ctypes.POINTER(ctypes.c_int32)
        self.library.amr_day_result_path_nodes.argtypes = (ctypes.c_void_p,)
        self.library.amr_day_result_path_nodes.restype = ctypes.POINTER(ctypes.c_int32)
        self.library.amr_day_result_denial_times.argtypes = (ctypes.c_void_p,)
        self.library.amr_day_result_denial_times.restype = ctypes.POINTER(ctypes.c_double)
        self.library.amr_day_result_denial_reasons.argtypes = (ctypes.c_void_p,)
        self.library.amr_day_result_denial_reasons.restype = ctypes.POINTER(ctypes.c_int32)
        self.library.amr_day_result_denial_nodes.argtypes = (ctypes.c_void_p,)
        self.library.amr_day_result_denial_nodes.restype = ctypes.POINTER(ctypes.c_int32)
        self.library.amr_day_calendar_self_test.restype = ctypes.c_int32
        self.library.amr_day_dispatch_probe.argtypes = (ctypes.POINTER(DayInput),)
        self.library.amr_day_dispatch_probe.restype = ctypes.c_int32

    @property
    def ready(self) -> bool:
        return bool(self.library.amr_day_engine_ready())

    def route(self, coordinates, edges, start, goal, racks=(), tabu_nodes=(), tabu_edges=()):
        nodes = (self.Node * len(coordinates))(*(
            self.Node(x, y, index, 0, int(index in racks))
            for index, (x, y) in enumerate(coordinates)
        ))
        native_edges = (self.Edge * len(edges))(*(
            self.Edge(first, second, distance) for first, second, distance in edges
        ))
        blocked_nodes = (ctypes.c_int32 * len(tabu_nodes))(*tabu_nodes)
        encoded_edges = tuple((first << 32) | second for first, second in tabu_edges)
        blocked_edges = (ctypes.c_uint64 * len(encoded_edges))(*encoded_edges)
        output = (ctypes.c_int32 * len(coordinates))()
        distance = ctypes.c_double()
        count = self.library.amr_day_route(
            nodes, len(nodes), native_edges, len(native_edges), start, goal,
            blocked_nodes, len(blocked_nodes), blocked_edges, len(blocked_edges),
            output, len(output), ctypes.byref(distance),
        )
        if count < 0:
            raise RuntimeError(f"native route failed with status {count}")
        return None if count == 0 else (tuple(output[:count]), distance.value)

    def predicted_motion(self, coordinates, route, heading, profile):
        nodes = (self.Node * len(coordinates))(*(
            self.Node(x, y, index, 0, 0) for index, (x, y) in enumerate(coordinates)
        ))
        path = (ctypes.c_int32 * len(route))(*route)
        final_heading = ctypes.c_double()
        seconds = self.library.amr_day_predicted_motion(
            nodes, len(nodes), path, len(path), heading,
            profile.max_linear_speed_mps, profile.linear_acceleration_mps2,
            __import__("math").radians(profile.max_angular_speed_degps),
            __import__("math").radians(profile.angular_acceleration_degps2),
            ctypes.byref(final_heading),
        )
        if seconds < 0:
            raise RuntimeError("native motion input is invalid")
        return seconds, final_heading.value

    def pack_day(self, router, racks, tasks, mapping, config, trace=False):
        """Flatten validated Python inputs while retaining every backing array."""
        node_names = tuple(sorted(router.coordinates))
        node_ids = {node: index for index, node in enumerate(node_names)}
        sku_names = tuple(sorted({sku for rack in racks.values() for sku in rack.skus}))
        sku_ids = {sku: index for index, sku in enumerate(sku_names)}
        rack_names = tuple(sorted(racks))
        station_names = tuple(config.workstations)
        station_ids = {station: index for index, station in enumerate(station_names)}
        task_values = tuple(sorted(tasks, key=lambda task: task.task_id))

        nodes = (self.Node * len(node_names))(*(
            self.Node(*router.coordinates[node], node[0], node[1], int(node in router.rack_positions))
            for node in node_names
        ))
        edge_values = tuple(
            (node_ids[start], node_ids[end], weight)
            for start in node_names
            for end, weight in router.graph[start]
        )
        edges = (self.Edge * len(edge_values))(*(
            self.Edge(*edge) for edge in edge_values
        ))
        flat_rack_skus = []
        rack_values = []
        for rack_id in rack_names:
            rack = racks[rack_id]
            offset = len(flat_rack_skus)
            flat_rack_skus.extend(sku_ids[sku] for sku in sorted(rack.skus))
            rack_values.append(self.Rack(node_ids[rack.position], offset, len(rack.skus)))
        native_racks = (self.Rack * len(rack_values))(*rack_values)
        rack_skus = (ctypes.c_int32 * len(flat_rack_skus))(*flat_rack_skus)
        flat_lines = []
        native_tasks_values = []
        for task in task_values:
            offset = len(flat_lines)
            for sku, count in sorted(task.line_counts.items()):
                flat_lines.append(self.TaskLine(sku_ids[sku], count))
            native_tasks_values.append(
                self.Task(station_ids[mapping[task.store_id]], offset, len(task.line_counts))
            )
        native_tasks = (self.Task * len(native_tasks_values))(*native_tasks_values)
        task_lines = (self.TaskLine * len(flat_lines))(*flat_lines)
        spawns = (self.Spawn * config.amr_count)(*(
            self.Spawn(node_ids[grid_position(node)], math.radians(config.initial_heading_degrees))
            for node in config.spawn_nodes
        ))
        stations = (ctypes.c_int32 * len(station_names))(*(
            node_ids[router.workstations[station]] for station in station_names
        ))
        motion = config.motion
        native_config = self.Config(
            config.jack_up_seconds, config.jack_down_seconds, config.service_seconds,
            config.reservation_wait_seconds, config.dram_conflict_wait_seconds,
            motion.max_linear_speed_mps, motion.linear_acceleration_mps2,
            math.radians(motion.max_angular_speed_degps),
            math.radians(motion.angular_acceleration_degps2),
            config.loaded_priority_enabled, config.mutex_passage_enabled,
            config.corridor_coordination_enabled, trace,
        )
        value = self.DayInput(
            nodes, len(nodes), edges, len(edges), native_racks, len(native_racks),
            rack_skus, len(rack_skus), native_tasks, len(native_tasks),
            task_lines, len(task_lines), spawns, len(spawns), stations, len(stations),
            native_config, task_values[0].task_date.year,
            task_values[0].task_date.month, task_values[0].task_date.day,
        )
        # ctypes pointers do not own their arrays. Return both as one lifetime object.
        backing = (
            nodes, edges, native_racks, rack_skus, native_tasks, task_lines,
            spawns, stations,
        )
        ids = {
            "nodes": node_names, "skus": sku_names, "racks": rack_names,
            "stations": station_names,
            "tasks": tuple(task.task_id for task in task_values),
            "stores": tuple(task.store_id for task in task_values),
        }
        return value, backing, ids

    def simulate(self, router, racks, tasks, mapping, config, trace=False):
        packed, backing, ids = self.pack_day(
            router, racks, tasks, mapping, config, trace
        )
        handle = self.library.amr_day_simulate(ctypes.byref(packed))
        if not handle:
            raise RuntimeError("native day result allocation failed")
        try:
            error = self.library.amr_day_result_error(handle).decode()
            if error:
                raise RuntimeError(error)
            metrics = json.loads(
                self.library.amr_day_result_metrics_json(handle).decode()
            )
            metrics["workstations"] = {
                station: metrics["workstations"].pop(str(index))
                for index, station in enumerate(ids["stations"])
            }
            metrics["amrs"] = {
                f"AMR_{index + 1:02d}": metrics["amrs"].pop(str(index))
                for index in range(config.amr_count)
            }
            count = self.library.amr_day_result_job_count(handle)
            native_jobs = self.library.amr_day_result_jobs(handle)
            native_skus = self.library.amr_day_result_job_skus(handle)
            native_paths = self.library.amr_day_result_path_nodes(handle)
            jobs, paths = [], {}
            for index in range(count):
                source = native_jobs[index]
                covered = sorted(
                    ids["skus"][native_skus[source.sku_offset + offset]]
                    for offset in range(source.sku_count)
                )
                job_id = f"J{index + 1:06d}"
                queue_node = ids["nodes"][source.station_queue_position]
                jobs.append({
                    "job_id": job_id,
                    "task_id": ids["tasks"][source.task],
                    "store_id": ids["stores"][source.task],
                    "rack_id": ids["racks"][source.rack],
                    "amr_id": f"AMR_{source.robot + 1:02d}",
                    "workstation": ids["stations"][source.station],
                    "covered_skus": covered,
                    "covered_lines": source.covered_lines,
                    "dispatch_time": source.dispatch_time,
                    "rack_departure": source.rack_departure,
                    "rack_return": source.rack_return,
                    "station_queue_position": grid_name(queue_node),
                    "station_queue_enter": source.station_queue_enter,
                    "station_admitted": source.station_admitted,
                    "station_arrival": source.station_arrival,
                    "service_start": source.service_start,
                    "completion_time": source.completion_time,
                    "travel_distance_m": source.travel_distance,
                    "travel_time_seconds": source.travel_seconds,
                    "node_reservation_wait_seconds": source.node_wait_seconds,
                    "reservation_conflicts": source.reservation_conflicts,
                    "reservation_reroutes": source.reroutes,
                    "dram_conflict_wait_seconds": source.dram_wait_seconds,
                })
                if trace:
                    paths[job_id] = [
                        ids["nodes"][native_paths[source.path_offset + offset]]
                        for offset in range(source.path_count)
                    ]
            return DayResult(metrics, jobs=jobs, paths=paths)
        finally:
            self.library.amr_day_result_destroy(handle)


def load_day_engine(requested: str = "auto") -> tuple[NativeDayEngine | None, SimulationBackendInfo]:
    if requested == "python":
        return None, SimulationBackendInfo(requested, "python")
    try:
        library, build_hash, compile_seconds = _build()
        engine = NativeDayEngine(library)
        if not engine.ready:
            raise RuntimeError("native day engine has not passed full parity qualification")
        return engine, SimulationBackendInfo(
            requested, "native", build_hash, compile_seconds
        )
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        if requested == "native":
            raise RuntimeError(f"native simulation unavailable: {exc}") from exc
        return None, SimulationBackendInfo(
            requested, "python", fallback_reason=str(exc)
        )
