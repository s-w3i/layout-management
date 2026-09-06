"""Store/day releases and rack/AMR selection following amr_simulation.engine."""

from collections import defaultdict
from dataclasses import dataclass
import math

from amr_simulation.models import Rack, SimulationConfig, WorkloadTask, grid_position

from .astar_planner import WarehouseMap
from .sim_types import ExecuteTask, RobotSnapshot


@dataclass
class StoreTask:
    source: WorkloadTask
    workstation: str
    outstanding: dict[str, int]
    inflight: int = 0
    completed_lines: int = 0
    completed_at: float | None = None


@dataclass
class RackJob:
    request: ExecuteTask
    task: StoreTask
    lines: dict[str, int]
    dispatch_time: float
    completion_time: float | None = None


class StoreDayScheduler:
    def __init__(
        self, tasks: list[WorkloadTask], racks: dict[str, Rack],
        mapping: dict[str, str], config: SimulationConfig, warehouse_map: WarehouseMap,
    ) -> None:
        if not tasks or len({task.task_date for task in tasks}) != 1:
            raise ValueError("each simulation must contain tasks from exactly one day")
        self.tasks = [
            StoreTask(task, mapping[task.store_id], dict(task.line_counts))
            for task in sorted(tasks, key=lambda task: task.task_id)
        ]
        self.racks = racks
        self.config = config
        self.map = warehouse_map
        self.racks_by_sku = defaultdict(set)
        for rack in racks.values():
            for sku in rack.skus:
                self.racks_by_sku[sku].add(rack.rack_id)
        self.reserved_racks: set[str] = set()
        self.jobs: dict[str, RackJob] = {}

    @property
    def completed_tasks(self) -> int:
        return sum(task.completed_at is not None for task in self.tasks)

    @property
    def done(self) -> bool:
        return self.completed_tasks == len(self.tasks)

    def dispatch_next(
        self, snapshots: dict[str, RobotSnapshot], available_robots: list[str], now: float,
    ) -> ExecuteTask | None:
        if not available_robots:
            return None
        # Every group is released at t=0. Source order-line times are not used.
        for task in self.tasks:
            if not task.outstanding:
                continue
            station_vertex = self.map.workstations[task.workstation]
            station_x, station_y = self.map.point(station_vertex)
            candidate_ids = {
                rack_id for sku in task.outstanding for rack_id in self.racks_by_sku[sku]
            }
            candidates = []
            for rack_id in candidate_ids - self.reserved_racks:
                rack = self.racks[rack_id]
                covered = sorted(rack.skus & task.outstanding.keys())
                x, y = self.map.point(rack_id)
                candidates.append((-len(covered), math.hypot(station_x-x, station_y-y), rack_id, covered))
            for _coverage, _distance, rack_id, covered in sorted(candidates):
                choices = []
                for name in available_robots:
                    # A rack occupied by another robot cannot be dispatched yet.
                    if any(s.current_vertex == rack_id and other != name for other, s in snapshots.items()):
                        continue
                    snapshot = snapshots[name]
                    route = self.map.router.route(grid_position(snapshot.current_vertex), self.racks[rack_id].position)
                    if route is not None:
                        predicted = self.map.router.predicted_motion_time(route, snapshot.heading, self.config.motion)
                        choices.append((predicted, name))
                if not choices:
                    continue
                _predicted, name = min(choices)
                job_id = f"J{len(self.jobs)+1:06d}"
                request = ExecuteTask(name, job_id, rack_id, station_vertex, rack_id, rack_id)
                lines = {sku: task.outstanding.pop(sku) for sku in covered}
                task.inflight += 1
                self.reserved_racks.add(rack_id)
                self.jobs[job_id] = RackJob(request, task, lines, now)
                return request
        return None

    def mark_completed(self, job_id: str, now: float) -> None:
        job = self.jobs[job_id]
        if job.completion_time is not None:
            raise ValueError(f"job completed twice: {job_id}")
        job.completion_time = now
        self.reserved_racks.remove(job.request.rack_id)
        job.task.inflight -= 1
        job.task.completed_lines += sum(job.lines.values())
        if not job.task.outstanding and not job.task.inflight:
            job.task.completed_at = now
        return sum(job.lines.values())
