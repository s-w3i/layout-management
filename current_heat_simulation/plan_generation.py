from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from time import perf_counter
from typing import Deque, Dict, Optional, Sequence

from .astar_planner import AStarPlanner, WarehouseMap
from .periodic_allocator import PeriodicAllocator
from .run_metrics import RunMetricsRecorder
from .sim_types import MoveRobotRequest, RobotSnapshot


@dataclass
class QueuedMove:
    robot_name: str
    task_id: str
    goal_vertex_name: str


class MoveRobotPathPlanner:
    def __init__(
        self,
        planner: AStarPlanner,
        allocator: PeriodicAllocator,
        warehouse_map: WarehouseMap,
        metrics_recorder: Optional[RunMetricsRecorder] = None,
    ) -> None:
        self.planner = planner
        self.allocator = allocator
        self.map = warehouse_map
        self.metrics_recorder = metrics_recorder
        self.task_queue: Dict[str, Deque[QueuedMove]] = {}
        self.current_tasks: Dict[str, Optional[str]] = {}
        self.current_goals: Dict[str, Optional[str]] = {}

    def submit_move(self, request: MoveRobotRequest) -> None:
        queue = self.task_queue.setdefault(request.robot_name, deque())
        queue.clear()
        queue.append(
            QueuedMove(
                robot_name=request.robot_name,
                task_id=request.task_id,
                goal_vertex_name=request.goal_vertex_name,
            )
        )

    def task_complete(self, robot_name: str, task_id: str) -> None:
        if self.current_tasks.get(robot_name) == task_id:
            self.current_tasks[robot_name] = None
            self.current_goals[robot_name] = None

    def update(
        self,
        *,
        robot_snapshots: Dict[str, RobotSnapshot],
        carrying_rack: Dict[str, bool],
        occupied_shelves: set[str],
    ) -> None:
        for robot_name in list(self.task_queue):
            if self.current_tasks.get(robot_name) is not None:
                continue
            if not self.task_queue[robot_name]:
                continue
            queued = self.task_queue[robot_name].popleft()
            self._request_path_computation(
                robot_name=queued.robot_name,
                goal_vertex=queued.goal_vertex_name,
                task_id=queued.task_id,
                robot_snapshots=robot_snapshots,
                carrying_rack=carrying_rack,
                occupied_shelves=occupied_shelves,
            )

    def replan_current_goal(
        self,
        *,
        robot_name: str,
        robot_snapshots: Dict[str, RobotSnapshot],
        carrying_rack: Dict[str, bool],
        occupied_shelves: set[str],
    ) -> None:
        goal_vertex = self.current_goals.get(robot_name)
        task_id = self.current_tasks.get(robot_name)
        if not goal_vertex or not task_id:
            return
        if self.metrics_recorder is not None:
            self.metrics_recorder.record_replan()
        self._request_path_computation(
            robot_name=robot_name,
            goal_vertex=goal_vertex,
            task_id=task_id,
            robot_snapshots=robot_snapshots,
            carrying_rack=carrying_rack,
            occupied_shelves=occupied_shelves,
        )

    def nearest_reachable_goal(
        self,
        *,
        robot_name: str,
        start_name: str,
        candidate_goals: Sequence[str],
        carrying_rack: bool,
        occupied_shelves: set[str],
    ) -> tuple[str, list[str]]:
        started_at = perf_counter()
        try:
            result = self.planner.nearest_reachable_goal(
                start_name=start_name,
                candidate_goals=candidate_goals,
                carrying_rack=carrying_rack,
                occupied_shelves=occupied_shelves,
                robot_name=robot_name,
            )
        except ValueError:
            if self.metrics_recorder is not None:
                self.metrics_recorder.record_planning_result(
                    latency_ms=(perf_counter() - started_at) * 1000.0,
                    success=False,
                )
            raise
        if self.metrics_recorder is not None:
            self.metrics_recorder.record_planning_result(
                latency_ms=(perf_counter() - started_at) * 1000.0,
                success=True,
            )
        return result

    def clear_tabu_set_for_robot(self, robot_name: str) -> None:
        self.allocator.tabu_sets.pop(robot_name, None)
        self.planner.set_tabu_edges(self.allocator.tabu_sets)

    def reset_robot_task(self, robot_name: str) -> None:
        self.current_tasks[robot_name] = None
        self.current_goals[robot_name] = None
        self.task_queue.pop(robot_name, None)

    def active_move(self, robot_name: str) -> tuple[Optional[str], Optional[str]]:
        return self.current_tasks.get(robot_name), self.current_goals.get(robot_name)

    def _request_path_computation(
        self,
        *,
        robot_name: str,
        goal_vertex: str,
        task_id: str,
        robot_snapshots: Dict[str, RobotSnapshot],
        carrying_rack: Dict[str, bool],
        occupied_shelves: set[str],
    ) -> None:
        snapshot = robot_snapshots[robot_name]
        started_at = perf_counter()
        try:
            path = self.planner.plan(
                start_name=snapshot.current_vertex,
                goal_name=goal_vertex,
                carrying_rack=carrying_rack.get(robot_name, False),
                occupied_shelves=occupied_shelves,
                robot_name=robot_name,
            )
        except ValueError:
            self.clear_tabu_set_for_robot(robot_name)
            try:
                path = self.planner.plan(
                    start_name=snapshot.current_vertex,
                    goal_name=goal_vertex,
                    carrying_rack=carrying_rack.get(robot_name, False),
                    occupied_shelves=occupied_shelves,
                    robot_name=robot_name,
                )
            except ValueError:
                if self.metrics_recorder is not None:
                    self.metrics_recorder.record_planning_result(
                        latency_ms=(perf_counter() - started_at) * 1000.0,
                        success=False,
                    )
                raise

        if self.metrics_recorder is not None:
            self.metrics_recorder.record_planning_result(
                latency_ms=(perf_counter() - started_at) * 1000.0,
                success=True,
            )

        self.current_tasks[robot_name] = task_id
        self.current_goals[robot_name] = goal_vertex
        self.allocator.set_full_path(robot_name, path, goal_vertex)
