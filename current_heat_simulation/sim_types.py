from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


@dataclass
class Task:
    task_id: str
    order_id: str
    rack_id: str
    workstation: str


@dataclass
class ExecuteTask:
    robot_name: str
    task_id: str
    rack_id: str
    workstation: str
    target_shelf: str
    shelf_storing_position: str = ""


@dataclass
class MoveRobotRequest:
    robot_name: str
    goal_vertex_name: str
    task_id: str


@dataclass
class RobotSnapshot:
    name: str
    current_vertex: str
    x: float
    y: float
    heading: float
    speed: float
    jack_up: bool


@dataclass
class OverlayState:
    sim_time_sec: float = 0.0
    pending_tasks: int = 0
    completed_tasks: int = 0
    active_robots: int = 0
    reserved_nodes: int = 0


@dataclass
class RobotTaskContext:
    phase: str = "idle"
    assigned_task: Optional[ExecuteTask] = None
    rack_id: Optional[str] = None
    pickup_vertex: Optional[str] = None
    return_vertex: Optional[str] = None
    action_timer: float = 0.0
    last_goal: Optional[str] = None
    carrying_rack: bool = False
    task_complete_count: int = 0
    ingestor_entry: Optional[str] = None
    ingestor_exit: Optional[str] = None


@dataclass
class RobotAllocationState:
    full_path: List[str] = field(default_factory=list)
    current_index: int = 0
    last_goal: Optional[str] = None
    conflict_start_times: Dict[str, float] = field(default_factory=dict)
    last_arrival_time: float = 0.0
    last_arrival_node: Optional[str] = None
    last_blocked_replan_key: Optional[str] = None
    last_blocked_replan_time: float = 0.0
    last_path_recovery_time: float = 0.0
    resolution: str = "allocate"


@dataclass
class WindowCommand:
    robot_name: str
    path: List[str]


@dataclass
class JackCommand:
    robot_name: str
    jack_up: bool


@dataclass
class TaskCompletion:
    robot_name: str
    task_id: str


Color = Tuple[int, int, int]
