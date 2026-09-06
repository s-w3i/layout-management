from __future__ import annotations

from typing import Dict, List, Sequence, Tuple


from .astar_planner import WarehouseMap
from .plan_generation import MoveRobotPathPlanner
from .periodic_allocator import PeriodicAllocator
from .sim_types import ExecuteTask, MoveRobotRequest, RobotSnapshot, RobotTaskContext, TaskCompletion


class TaskStateMachineManager:
    def __init__(self, config: dict, warehouse_map: WarehouseMap) -> None:
        self.config = config
        self.map = warehouse_map
        self.task_cfg = {
            "pickup_time_sec": 0.0,
            "dropoff_wait_sec": 0.0,
            "return_time_sec": 0.0,
            **self.config.get("tasks", {}),
        }
        self.ingestor_access = self._parse_ingestor_access()
        self.contexts: Dict[str, RobotTaskContext] = {
            name: RobotTaskContext() for name in self.config.get("robots", [])
        }
        self.rack_positions: Dict[str, str] = {}
        self.reserved_return_slots: set[str] = set()
        self._completed_tasks: List[TaskCompletion] = []

    def seed_rack_positions(self, rack_positions: Dict[str, str]) -> None:
        self.rack_positions = dict(rack_positions)

    def _parse_ingestor_access(self) -> Dict[str, Tuple[str, str]]:
        mapping: Dict[str, Tuple[str, str]] = {}
        for item in self.task_cfg.get("ingestor_access", []):
            if not isinstance(item, str) or "-" not in item or "," not in item:
                continue
            access_part, workstation = [part.strip() for part in item.split("-", 1)]
            entry, exit_node = [part.strip() for part in access_part.split(",", 1)]
            mapping[workstation] = (entry, exit_node)
        return mapping

    @property
    def occupied_shelves(self) -> set[str]:
        return set(self.rack_positions.values())

    def available_robots(self) -> Sequence[str]:
        return [
            robot_name
            for robot_name, context in self.contexts.items()
            if context.phase == "idle" and not context.carrying_rack and context.assigned_task is None
        ]

    def carrying_rack_states(self) -> Dict[str, bool]:
        return {robot_name: context.carrying_rack for robot_name, context in self.contexts.items()}

    def jack_states(self) -> Dict[str, bool]:
        return self.carrying_rack_states()

    def can_accept_task(self, robot_name: str) -> bool:
        context = self.contexts[robot_name]
        return context.phase == "idle" and not context.carrying_rack and context.assigned_task is None

    def start_execute_task(self, task: ExecuteTask, planner_bridge: MoveRobotPathPlanner) -> bool:
        if not self.can_accept_task(task.robot_name):
            return False
        context = self.contexts[task.robot_name]
        context.assigned_task = task
        context.rack_id = task.rack_id
        context.pickup_vertex = task.target_shelf
        if context.return_vertex:
            self.reserved_return_slots.discard(context.return_vertex)
        context.return_vertex = None
        context.ingestor_entry = None
        context.ingestor_exit = None
        context.action_timer = 0.0
        context.carrying_rack = False
        context.phase = "to_pickup"
        context.last_goal = task.target_shelf
        planner_bridge.reset_robot_task(task.robot_name)
        planner_bridge.clear_tabu_set_for_robot(task.robot_name)
        planner_bridge.submit_move(
            MoveRobotRequest(
                robot_name=task.robot_name,
                goal_vertex_name=task.target_shelf,
                task_id=f"{task.task_id}:pickup",
            )
        )
        return True

    def submit_execute_task(self, task: ExecuteTask, planner_bridge: MoveRobotPathPlanner) -> None:
        self.start_execute_task(task, planner_bridge)

    def update(
        self,
        *,
        dt: float,
        robot_snapshots: Dict[str, RobotSnapshot],
        allocator: PeriodicAllocator,
        planner_bridge: MoveRobotPathPlanner,
    ) -> None:
        for robot_name, context in self.contexts.items():
            snapshot = robot_snapshots.get(robot_name)
            if snapshot is None:
                continue

            if context.action_timer > 0.0:
                context.action_timer = max(0.0, context.action_timer - dt)
                if context.action_timer <= 0.0:
                    self._finish_wait(robot_name, robot_snapshots, planner_bridge)
                continue

            if context.phase == "idle" or context.assigned_task is None or not context.last_goal:
                continue

            if (
                snapshot.current_vertex != context.last_goal
                and allocator.needs_path_recovery(robot_name, snapshot.current_vertex, context.last_goal)
            ):
                allocator.sync_robot_to_current_vertex(robot_name, snapshot.current_vertex)
                if allocator.can_attempt_path_recovery(robot_name):
                    allocator.mark_path_recovery_attempt(robot_name)
                    planner_bridge.clear_tabu_set_for_robot(robot_name)
                    expected_task_id = self._current_move_task_id(context)
                    planner_task_id, planner_goal = planner_bridge.active_move(robot_name)
                    if planner_task_id != expected_task_id or planner_goal != context.last_goal:
                        planner_bridge.reset_robot_task(robot_name)
                        planner_bridge.submit_move(
                            MoveRobotRequest(
                                robot_name=robot_name,
                                goal_vertex_name=context.last_goal,
                                task_id=expected_task_id,
                            )
                        )
                    elif context.phase == "to_return" and context.carrying_rack:
                        self.reselect_return_slot(robot_name, robot_snapshots, planner_bridge)
                    else:
                        planner_bridge.replan_current_goal(
                            robot_name=robot_name,
                            robot_snapshots=robot_snapshots,
                            carrying_rack=self.carrying_rack_states(),
                            occupied_shelves=self.occupied_shelves,
                        )
                continue

            if snapshot.current_vertex == context.last_goal and allocator.robot_at_goal(robot_name):
                self._handle_arrival(robot_name, planner_bridge, allocator, robot_snapshots)

    def _handle_arrival(
        self,
        robot_name: str,
        planner_bridge: MoveRobotPathPlanner,
        allocator: PeriodicAllocator,
        robot_snapshots: Dict[str, RobotSnapshot],
    ) -> None:
        context = self.contexts[robot_name]
        task = context.assigned_task
        if task is None:
            return

        if context.phase == "to_pickup" and context.pickup_vertex:
            planner_bridge.task_complete(robot_name, f"{task.task_id}:pickup")
            context.phase = "pickup_wait"
            context.action_timer = float(self.task_cfg.get("pickup_time_sec", 0.0))
            if context.action_timer <= 0.0:
                self._finish_wait(robot_name, robot_snapshots, planner_bridge)
            return

        if context.phase == "to_dropoff_entry":
            planner_bridge.task_complete(robot_name, f"{task.task_id}:dropoff_entry")
            context.phase = "to_workstation"
            planner_bridge.reset_robot_task(robot_name)
            planner_bridge.clear_tabu_set_for_robot(robot_name)
            planner_bridge.submit_move(
                MoveRobotRequest(
                    robot_name=robot_name,
                    goal_vertex_name=task.workstation,
                    task_id=f"{task.task_id}:workstation",
                )
            )
            context.last_goal = task.workstation
            return

        if context.phase == "to_workstation":
            planner_bridge.task_complete(robot_name, f"{task.task_id}:workstation")
            context.phase = "dropoff_wait"
            context.action_timer = float(self.task_cfg.get("dropoff_wait_sec", 0.0))
            if context.action_timer <= 0.0:
                self._finish_wait(robot_name, robot_snapshots, planner_bridge)
            return

        if context.phase == "to_ingestor_exit" and context.ingestor_exit:
            planner_bridge.task_complete(robot_name, f"{task.task_id}:ingestor_exit")
            self._request_return(robot_name, robot_snapshots, planner_bridge)
            return

        if context.phase == "to_return" and context.return_vertex:
            planner_bridge.task_complete(robot_name, f"{task.task_id}:return")
            context.phase = "return_wait"
            context.action_timer = float(self.task_cfg.get("return_time_sec", 0.0))
            if context.action_timer <= 0.0:
                self._finish_wait(robot_name, robot_snapshots, planner_bridge)

    def _finish_wait(
        self,
        robot_name: str,
        robot_snapshots: Dict[str, RobotSnapshot],
        planner_bridge: MoveRobotPathPlanner,
    ) -> None:
        context = self.contexts[robot_name]
        task = context.assigned_task
        if task is None:
            return
        context.action_timer = 0.0

        if context.phase == "pickup_wait":
            if context.rack_id:
                self.rack_positions.pop(context.rack_id, None)
            context.carrying_rack = True
            entry, exit_vertex = self.ingestor_access.get(task.workstation, (task.workstation, task.workstation))
            context.ingestor_entry = entry
            context.ingestor_exit = exit_vertex
            context.phase = "to_dropoff_entry"
            context.last_goal = entry
            planner_bridge.reset_robot_task(robot_name)
            planner_bridge.clear_tabu_set_for_robot(robot_name)
            planner_bridge.submit_move(
                MoveRobotRequest(
                    robot_name=robot_name,
                    goal_vertex_name=entry,
                    task_id=f"{task.task_id}:dropoff_entry",
                )
            )
            return

        if context.phase == "dropoff_wait":
            exit_vertex = context.ingestor_exit or robot_snapshots[robot_name].current_vertex
            context.phase = "to_ingestor_exit"
            context.last_goal = exit_vertex
            planner_bridge.reset_robot_task(robot_name)
            planner_bridge.clear_tabu_set_for_robot(robot_name)
            planner_bridge.submit_move(
                MoveRobotRequest(
                    robot_name=robot_name,
                    goal_vertex_name=exit_vertex,
                    task_id=f"{task.task_id}:ingestor_exit",
                )
            )
            return

        if context.phase == "return_wait":
            if context.rack_id and context.return_vertex:
                self.rack_positions[context.rack_id] = context.return_vertex
                self.reserved_return_slots.discard(context.return_vertex)
            context.carrying_rack = False
            context.assigned_task = None
            context.rack_id = None
            context.pickup_vertex = None
            context.return_vertex = None
            context.phase = "idle"
            context.last_goal = None
            context.ingestor_entry = None
            context.ingestor_exit = None
            context.task_complete_count += 1
            planner_bridge.reset_robot_task(robot_name)
            planner_bridge.clear_tabu_set_for_robot(robot_name)
            self._completed_tasks.append(TaskCompletion(robot_name=robot_name, task_id=task.task_id))

    def _request_return(
        self,
        robot_name: str,
        robot_snapshots: Dict[str, RobotSnapshot],
        planner_bridge: MoveRobotPathPlanner,
    ) -> None:
        context = self.contexts[robot_name]
        snapshot = robot_snapshots[robot_name]
        # Store/day jobs return the rack to its original slot, never relocate it.
        candidate_sets = [[context.pickup_vertex or snapshot.current_vertex]]

        planner_bridge.clear_tabu_set_for_robot(robot_name)
        return_vertex = None
        for candidate_goals in candidate_sets:
            if not candidate_goals:
                continue
            try:
                return_vertex, _path = planner_bridge.nearest_reachable_goal(
                    robot_name=robot_name,
                    start_name=snapshot.current_vertex,
                    candidate_goals=candidate_goals,
                    carrying_rack=True,
                    occupied_shelves=self.occupied_shelves,
                )
                break
            except ValueError:
                continue
        if return_vertex is None:
            return
        if context.return_vertex:
            self.reserved_return_slots.discard(context.return_vertex)
        context.return_vertex = return_vertex
        self.reserved_return_slots.add(return_vertex)
        context.phase = "to_return"
        context.last_goal = return_vertex
        task_id = context.assigned_task.task_id if context.assigned_task else "return"
        planner_bridge.reset_robot_task(robot_name)
        planner_bridge.clear_tabu_set_for_robot(robot_name)
        planner_bridge.submit_move(
            MoveRobotRequest(
                robot_name=robot_name,
                goal_vertex_name=return_vertex,
                task_id=f"{task_id}:return",
            )
        )

    def reselect_return_slot(
        self,
        robot_name: str,
        robot_snapshots: Dict[str, RobotSnapshot],
        planner_bridge: MoveRobotPathPlanner,
    ) -> None:
        context = self.contexts[robot_name]
        if context.phase != "to_return" or not context.carrying_rack or context.assigned_task is None:
            return
        self._request_return(robot_name, robot_snapshots, planner_bridge)

    def _current_move_task_id(self, context: RobotTaskContext) -> str:
        base_task_id = context.assigned_task.task_id if context.assigned_task is not None else "recovery"
        if context.phase == "to_pickup":
            return f"{base_task_id}:pickup"
        if context.phase == "to_dropoff_entry":
            return f"{base_task_id}:dropoff_entry"
        if context.phase == "to_workstation":
            return f"{base_task_id}:workstation"
        if context.phase == "to_ingestor_exit":
            return f"{base_task_id}:ingestor_exit"
        if context.phase == "to_return":
            return f"{base_task_id}:return"
        return base_task_id

    def drain_completed_tasks(self) -> List[TaskCompletion]:
        completed = list(self._completed_tasks)
        self._completed_tasks.clear()
        return completed
