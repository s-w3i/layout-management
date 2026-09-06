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
        self.pending_replans = {}
        self.heat_layer = None

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
            if self.heat_layer is not None:
                self.heat_layer.path_changed(robot_name, [])

    def update(
        self,
        *,
        robot_snapshots: Dict[str, RobotSnapshot],
        carrying_rack: Dict[str, bool],
        occupied_shelves: set[str],
    ) -> None:
        # Deliver requests on a later tick, like ROS service response callbacks.
        pending, self.pending_replans = self.pending_replans, {}
        for robot_name, (goal, task_id, start) in pending.items():
            self._request_path_computation(
                robot_name=robot_name, goal_vertex=goal, task_id=task_id,
                robot_snapshots=robot_snapshots, carrying_rack=carrying_rack,
                occupied_shelves=occupied_shelves, replan_start=start,
            )
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
        if robot_name in self.pending_replans:
            return
        if self.metrics_recorder is not None:
            self.metrics_recorder.record_replan()
        snapshot = robot_snapshots[robot_name]
        window = self.allocator._window_nodes(robot_name, snapshot.current_vertex)
        start = window[-1] if window else snapshot.current_vertex
        self.pending_replans[robot_name] = (goal_vertex, task_id, start)
        self.allocator.pending_replans.add(robot_name)
        self.allocator.conflict_resolutions[robot_name] = "computing"
        self.allocator.states[robot_name].full_path = []
        self.allocator.states[robot_name].current_index = 0
        if self.heat_layer is not None:
            self.heat_layer.path_changed(robot_name, [])

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
        replan_start: Optional[str] = None,
    ) -> None:
        snapshot = robot_snapshots[robot_name]
        start_name = replan_start if replan_start is not None else snapshot.current_vertex
        started_at = perf_counter()
        try:
            path = self.planner.plan(
                start_name=start_name,
                goal_name=goal_vertex,
                carrying_rack=carrying_rack.get(robot_name, False),
                occupied_shelves=occupied_shelves,
                robot_name=robot_name,
            )
        except ValueError:
            self.clear_tabu_set_for_robot(robot_name)
            if replan_start is not None:
                if self.metrics_recorder is not None:
                    self.metrics_recorder.record_planning_result(
                        latency_ms=(perf_counter() - started_at) * 1000.0, success=False)
                self.pending_replans[robot_name] = (goal_vertex, task_id, start_name)
                return
            try:
                path = self.planner.plan(
                    start_name=start_name,
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
        if replan_start is not None:
            self.allocator.pending_replans.discard(robot_name)
            self.allocator.mutex_passage.remove_all_planned_paths(robot_name)
            self.allocator.mutex_passage.update_move_buffer(robot_name, snapshot.current_vertex)
            self.allocator.conflict_resolutions[robot_name] = "clear"
        # planned_path_callback installs the response as given, without merging.
        state = self.allocator.states[robot_name]
        state.full_path = list(path)
        state.current_index = 0
        state.last_goal = goal_vertex
        self.allocator.mutex_passage.add_planned_path(robot_name, path)
        if self.heat_layer is not None:
            self.heat_layer.path_changed(robot_name, path)
