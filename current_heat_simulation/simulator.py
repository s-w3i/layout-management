from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import pygame

from .astar_planner import WarehouseMap
from .sim_types import Color, OverlayState, RobotSnapshot


@dataclass
class SimRobot:
    name: str
    current_vertex: str
    x: float
    y: float
    color: Color
    heading: float = 0.0
    speed: float = 0.0
    jack_up: bool = False
    active_window_path: List[str] = field(default_factory=list)
    path_index: int = 0
    full_path_preview: List[str] = field(default_factory=list)
    segment_heading: Optional[float] = None
    pending_window_path: List[str] = field(default_factory=list)

    def snapshot(self) -> RobotSnapshot:
        return RobotSnapshot(
            name=self.name,
            current_vertex=self.current_vertex,
            x=self.x,
            y=self.y,
            heading=self.heading,
            speed=self.speed,
            jack_up=self.jack_up,
        )


class PygameE3DSimulator:
    def __init__(self, config: dict, warehouse_map: WarehouseMap) -> None:
        self.config = config
        self.display_cfg = {
            "width": 1280,
            "height": 720,
            "margin": 40,
            "background_color": [245, 245, 245],
            "lane_color": [0, 0, 0],
            "lane_width": 1,
            "vertex_color": [0, 0, 0],
            "vertex_radius": 2,
            "pickup_color": [40, 120, 255],
            "pickup_size": 6,
            "robot_radius": 6,
            "path_width": 2,
            "text_color": [20, 20, 20],
            **self.config.get("display", {}),
        }
        self.sim_cfg = {
            "fps": 60,
            "sim_time_scale": 5.0,
            "fixed_sim_step_sec": 0.05,
            "max_frame_dt_sec": 0.1,
            "max_sim_steps_per_frame": 8,
            **self.config.get("simulation", {}),
        }
        self.motion_cfg = {
            "max_linear_speed_mps": 1.5,
            "linear_accel_mps2": 0.6,
            "linear_decel_mps2": 0.6,
            "max_turn_speed_radps": 0.8,
            "waypoint_epsilon_m": 0.05,
            "heading_align_rad": 0.08,
            "enforce_collision_safety": False,
            "collision_clearance_m": 0.35,
            "collision_safety_substep_sec": 0.05,
            **self.config.get("motion", {}),
        }
        self.map = warehouse_map
        self.rack_positions: Dict[str, str] = {}
        self.overlay = OverlayState()
        self.safety_interventions = 0
        self.safety_blockers = {}
        self.global_reservations: Dict[str, str] = {}
        self.font: Optional[pygame.font.Font] = None
        self.world_to_screen = None
        self.metrics = None
        self.robots = self._build_robots()

    def _build_robots(self) -> List[SimRobot]:
        initial_vertices: Dict[str, str] = {}
        for item in self.config.get("initial_vertices", []):
            robot_name, vertex_name = item.split(":", 1)
            initial_vertices[robot_name] = vertex_name

        palette = [
            (220, 80, 80),
            (80, 130, 220),
            (80, 180, 120),
            (210, 150, 70),
            (160, 90, 200),
            (60, 170, 170),
        ]

        robots: List[SimRobot] = []
        for index, name in enumerate(self.config.get("robots", [])):
            start_vertex = initial_vertices.get(name) or self.map.spawn_vertices.get(name)
            if not start_vertex:
                continue
            x, y = self.map.point(start_vertex)
            robots.append(
                SimRobot(
                    name=name,
                    current_vertex=start_vertex,
                    x=x,
                    y=y,
                    color=palette[index % len(palette)],
                    heading=math.radians(float(self.config.get("initial_heading_degrees", 0.0))),
                )
            )
        return robots

    def _build_projection(self):
        xs = [vertex.x for vertex in self.map.vertices]
        ys = [vertex.y for vertex in self.map.vertices]
        margin = self.display_cfg.get("margin", 40)
        width = self.display_cfg["width"]
        height = self.display_cfg["height"]
        world_w = max(xs) - min(xs)
        world_h = max(ys) - min(ys)
        scale_x = (width - 2 * margin) / world_w if world_w else 1.0
        top = max(margin, 130)
        scale_y = (height - top - margin) / world_h if world_h else 1.0
        scale = min(scale_x, scale_y)
        min_x = min(xs)
        max_y = max(ys)

        def project(point: Tuple[float, float]) -> Tuple[int, int]:
            x, y = point
            sx = margin + (x - min_x) * scale
            sy = top + (max_y - y) * scale
            return int(sx), int(sy)

        return project

    def snapshots(self) -> Dict[str, RobotSnapshot]:
        return {robot.name: robot.snapshot() for robot in self.robots}

    def robot_by_name(self, name: str) -> Optional[SimRobot]:
        for robot in self.robots:
            if robot.name == name:
                return robot
        return None

    def set_window_paths(self, window_paths: Dict[str, Sequence[str]]) -> None:
        waypoint_epsilon = float(self.motion_cfg["waypoint_epsilon_m"])
        for robot in self.robots:
            path = list(window_paths.get(robot.name, []))
            if path and path[0] != robot.current_vertex:
                if robot.current_vertex in path:
                    path = path[path.index(robot.current_vertex) :]
                else:
                    path.insert(0, robot.current_vertex)

            if not path:
                if self._robot_in_transit(robot, waypoint_epsilon) and robot.path_index < len(robot.active_window_path):
                    robot.pending_window_path = [robot.current_vertex]
                else:
                    robot.active_window_path = [robot.current_vertex]
                    robot.path_index = len(robot.active_window_path)
                    robot.segment_heading = None
                continue

            current_target = (
                robot.active_window_path[robot.path_index]
                if robot.path_index < len(robot.active_window_path)
                else None
            )
            incoming_target = path[1] if len(path) > 1 else None
            if self._robot_in_transit(robot, waypoint_epsilon) and current_target and incoming_target != current_target:
                robot.pending_window_path = path
                continue

            robot.active_window_path = path
            robot.path_index = 1 if len(path) > 1 else len(path)
            robot.segment_heading = None
            robot.pending_window_path = []

    def set_full_path_previews(self, full_paths: Dict[str, Sequence[str]]) -> None:
        for robot in self.robots:
            path = list(full_paths.get(robot.name, []))
            if path and path[0] != robot.current_vertex:
                if robot.current_vertex in path:
                    path = path[path.index(robot.current_vertex) :]
                else:
                    path.insert(0, robot.current_vertex)
            robot.full_path_preview = path

    def set_jack_states(self, jack_states: Dict[str, bool]) -> None:
        for robot in self.robots:
            robot.jack_up = bool(jack_states.get(robot.name, False))

    def set_rack_positions(self, rack_positions: Dict[str, str]) -> None:
        self.rack_positions = dict(rack_positions)

    def set_overlay(self, overlay: OverlayState) -> None:
        self.overlay = overlay

    def set_reservations(self, reservations: Dict[str, str]) -> None:
        self.global_reservations = dict(reservations)

    def update(self, dt: float) -> None:
        if dt <= 0.0:
            return

        if bool(self.motion_cfg.get("enforce_collision_safety", False)):
            max_substep = max(1e-3, float(self.motion_cfg.get("collision_safety_substep_sec", 0.05)))
            remaining = dt
            while remaining > 1e-9:
                step_dt = min(max_substep, remaining)
                allowed_translation = self._compute_translation_permissions()
                for robot in self.robots:
                    self._advance_robot(robot, step_dt, robot.name in allowed_translation)
                remaining -= step_dt
            return

        allowed_translation = self._compute_translation_permissions()
        for robot in self.robots:
            self._advance_robot(robot, dt, robot.name in allowed_translation)

    def _advance_robot(self, robot, dt, allowed):
        before = (robot.x, robot.y, robot.heading)
        target = self._active_target_name(robot)
        self._update_robot(robot, dt, allowed)
        if self.metrics is not None:
            self.metrics.observe_motion(robot, before, dt, allowed, target)

    def _update_robot(self, robot: SimRobot, dt: float, allow_translation: bool) -> None:
        linear_decel = float(self.motion_cfg["linear_decel_mps2"])
        waypoint_epsilon = float(self.motion_cfg["waypoint_epsilon_m"])
        max_turn_speed = float(self.motion_cfg["max_turn_speed_radps"])
        heading_align = float(self.motion_cfg["heading_align_rad"])
        linear_accel = float(self.motion_cfg["linear_accel_mps2"])
        max_linear_speed = float(self.motion_cfg["max_linear_speed_mps"])
        straight_heading_tolerance = max(heading_align * 0.5, 0.02)

        if not robot.active_window_path or robot.path_index >= len(robot.active_window_path):
            robot.speed = max(0.0, robot.speed - linear_decel * dt)
            return

        target_name = self._active_target_name(robot)
        if target_name is None or not self._is_adjacent(robot.current_vertex, target_name):
            robot.speed = 0.0
            robot.segment_heading = None
            return

        target_x, target_y = self.map.point(target_name)
        desired_heading = self._heading_between_points((robot.x, robot.y), (target_x, target_y))
        heading_error = self._normalize_angle(desired_heading - robot.heading)
        max_turn = max_turn_speed * dt
        heading_step = max(-max_turn, min(max_turn, heading_error))
        robot.heading = self._normalize_angle(robot.heading + heading_step)

        if abs(heading_error) > heading_align:
            robot.speed = max(0.0, robot.speed - linear_decel * dt)
            robot.segment_heading = None
            return

        robot.segment_heading = desired_heading
        if not allow_translation:
            robot.speed = max(0.0, robot.speed - linear_decel * dt)
            return

        runway_distance = self._runway_distance(robot, target_name, straight_heading_tolerance)
        stop_distance = (robot.speed ** 2) / (2 * linear_decel) if linear_decel > 0 else 0.0
        if runway_distance <= stop_distance + waypoint_epsilon:
            robot.speed = max(0.0, robot.speed - linear_decel * dt)
        else:
            robot.speed = min(max_linear_speed, robot.speed + linear_accel * dt)

        remaining_travel = robot.speed * dt
        while remaining_travel > 0.0:
            target_name = self._active_target_name(robot)
            if target_name is None or not self._is_adjacent(robot.current_vertex, target_name):
                robot.speed = 0.0
                robot.segment_heading = None
                return

            target_x, target_y = self.map.point(target_name)
            distance = math.hypot(target_x - robot.x, target_y - robot.y)
            if distance <= waypoint_epsilon:
                next_target = self._consume_window_target(robot, target_name, target_x, target_y)
                if next_target is None:
                    robot.speed = 0.0
                    return
                next_heading = self._heading_between_vertices(robot.current_vertex, next_target)
                if next_heading is None or abs(self._normalize_angle(next_heading - desired_heading)) > straight_heading_tolerance:
                    robot.speed = 0.0
                    robot.segment_heading = None
                    return
                robot.segment_heading = next_heading
                desired_heading = next_heading
                if remaining_travel <= waypoint_epsilon:
                    return
                continue

            if remaining_travel + waypoint_epsilon < distance:
                robot.x += math.cos(desired_heading) * remaining_travel
                robot.y += math.sin(desired_heading) * remaining_travel
                return

            remaining_travel = max(0.0, remaining_travel - distance)
            next_target = self._consume_window_target(robot, target_name, target_x, target_y)
            if next_target is None:
                robot.speed = 0.0
                return
            next_heading = self._heading_between_vertices(robot.current_vertex, next_target)
            if next_heading is None or abs(self._normalize_angle(next_heading - desired_heading)) > straight_heading_tolerance:
                robot.speed = 0.0
                robot.segment_heading = None
                return
            robot.segment_heading = next_heading
            desired_heading = next_heading

    def _consume_window_target(
        self,
        robot: SimRobot,
        target_name: str,
        target_x: float,
        target_y: float,
    ) -> Optional[str]:
        robot.x = target_x
        robot.y = target_y
        robot.current_vertex = target_name
        robot.segment_heading = None
        if robot.path_index < len(robot.active_window_path):
            robot.active_window_path = robot.active_window_path[robot.path_index:]
        robot.path_index = 1 if len(robot.active_window_path) > 1 else len(robot.active_window_path)
        if robot.pending_window_path:
            pending = list(robot.pending_window_path)
            if pending and pending[0] != robot.current_vertex:
                if robot.current_vertex in pending:
                    pending = pending[pending.index(robot.current_vertex) :]
                else:
                    pending.insert(0, robot.current_vertex)
            robot.active_window_path = pending if pending else [robot.current_vertex]
            robot.path_index = 1 if len(robot.active_window_path) > 1 else len(robot.active_window_path)
            robot.pending_window_path = []
        return self._active_target_name(robot)

    @staticmethod
    def _normalize_angle(angle: float) -> float:
        while angle > math.pi:
            angle -= 2 * math.pi
        while angle < -math.pi:
            angle += 2 * math.pi
        return angle

    def _segment_heading(self, robot: SimRobot, target_x: float, target_y: float) -> float:
        if robot.segment_heading is not None:
            return robot.segment_heading
        robot.segment_heading = math.atan2(target_y - robot.y, target_x - robot.x)
        return robot.segment_heading

    def _active_target_name(self, robot: SimRobot) -> Optional[str]:
        if not robot.active_window_path or robot.path_index >= len(robot.active_window_path):
            return None
        return robot.active_window_path[robot.path_index]

    def _compute_translation_permissions(self) -> set[str]:
        self.safety_blockers = {}
        if not bool(self.motion_cfg.get("enforce_collision_safety", False)):
            return {robot.name for robot in self.robots}

        waypoint_epsilon = float(self.motion_cfg["waypoint_epsilon_m"])
        collision_clearance = float(self.motion_cfg["collision_clearance_m"])
        intents: Dict[str, Dict[str, object]] = {}
        occupied_vertices: Dict[str, set[str]] = {}

        for robot in self.robots:
            occupied_vertices[robot.name] = self._occupied_vertices(robot, waypoint_epsilon, collision_clearance)
            target_name = self._active_target_name(robot)
            if target_name is None or not self._is_adjacent(robot.current_vertex, target_name):
                continue
            target_x, target_y = self.map.point(target_name)
            distance_to_target = math.hypot(target_x - robot.x, target_y - robot.y)
            intents[robot.name] = {
                "current": robot.current_vertex,
                "target": target_name,
                "distance": distance_to_target,
                "in_transit": self._robot_in_transit(robot, waypoint_epsilon) or robot.speed > 1e-6,
            }

        allowed = set(intents.keys())

        target_groups: Dict[str, List[str]] = {}
        for robot_name, intent in intents.items():
            target_groups.setdefault(str(intent["target"]), []).append(robot_name)

        for target_name, contenders in target_groups.items():
            blocking_occupants = [
                robot_name
                for robot_name, occupied in occupied_vertices.items()
                if robot_name not in contenders and target_name in occupied
            ]
            if blocking_occupants:
                for robot_name in contenders:
                    allowed.discard(robot_name)
                    self.safety_blockers[robot_name] = {"reason": "occupied_target", "robots": sorted(blocking_occupants), "target": target_name}
                continue
            if len(contenders) <= 1:
                continue
            winner = min(contenders, key=lambda name: self._translation_priority(name, intents[name]))
            for robot_name in contenders:
                if robot_name != winner:
                    allowed.discard(robot_name)
                    self.safety_blockers[robot_name] = {"reason": "same_target", "robots": [winner], "target": target_name}

        processed_edges: set[Tuple[str, str]] = set()
        for robot_name, intent in intents.items():
            current = str(intent["current"])
            target = str(intent["target"])
            edge_key = tuple(sorted((f"{current}->{target}", f"{target}->{current}")))
            if edge_key in processed_edges:
                continue
            contenders = [
                other_name
                for other_name, other_intent in intents.items()
                if other_name != robot_name
                and str(other_intent["current"]) == target
                and str(other_intent["target"]) == current
            ]
            if not contenders:
                continue
            processed_edges.add(edge_key)
            winner = min(
                [robot_name] + contenders,
                key=lambda name: self._translation_priority(name, intents[name]),
            )
            for loser in [robot_name] + contenders:
                if loser != winner:
                    allowed.discard(loser)
                    self.safety_blockers.setdefault(loser, {"reason": "opposing_edge", "robots": [winner], "target": str(intents[loser]["target"])})

        self.safety_interventions += len(intents)-len(allowed)
        return allowed

    @staticmethod
    def _translation_priority(robot_name: str, intent: Dict[str, object]) -> Tuple[int, float, str]:
        return (
            0 if bool(intent["in_transit"]) else 1,
            float(intent["distance"]),
            robot_name,
        )

    def _occupied_vertices(
        self,
        robot: SimRobot,
        waypoint_epsilon: float,
        collision_clearance: float,
    ) -> set[str]:
        occupied: set[str] = set()
        target_name = self._active_target_name(robot)
        if target_name is None:
            return {robot.current_vertex}
        current_x, current_y = self.map.point(robot.current_vertex)
        if math.hypot(robot.x - current_x, robot.y - current_y) <= max(waypoint_epsilon, collision_clearance):
            return {robot.current_vertex}
        target_x, target_y = self.map.point(target_name)
        segment_dx = target_x - current_x
        segment_dy = target_y - current_y
        segment_length_sq = segment_dx * segment_dx + segment_dy * segment_dy
        if segment_length_sq <= 1e-12:
            return {target_name}

        # Keep the source vertex occupied until the robot has clearly passed the
        # midpoint of the outgoing edge. This avoids another robot entering the
        # source while the first robot is still effectively turning/leaving it.
        progress = (
            ((robot.x - current_x) * segment_dx) + ((robot.y - current_y) * segment_dy)
        ) / segment_length_sq
        progress = max(0.0, min(1.0, progress))

        if progress <= 0.5:
            occupied.add(robot.current_vertex)
        if progress >= 0.5:
            occupied.add(target_name)
        if math.hypot(robot.x - current_x, robot.y - current_y) <= collision_clearance:
            occupied.add(robot.current_vertex)
        if math.hypot(robot.x - target_x, robot.y - target_y) <= collision_clearance:
            occupied.add(target_name)
        return occupied

    def _runway_distance(self, robot: SimRobot, target_name: str, straight_heading_tolerance: float) -> float:
        target_x, target_y = self.map.point(target_name)
        runway = math.hypot(target_x - robot.x, target_y - robot.y)
        if robot.pending_window_path:
            return runway

        base_heading = self._heading_between_points((robot.x, robot.y), (target_x, target_y))
        path = robot.active_window_path
        if not path:
            return runway

        index = robot.path_index
        while index + 1 < len(path):
            start_name = path[index]
            end_name = path[index + 1]
            next_heading = self._heading_between_vertices(start_name, end_name)
            if next_heading is None or abs(self._normalize_angle(next_heading - base_heading)) > straight_heading_tolerance:
                break
            start_x, start_y = self.map.point(start_name)
            end_x, end_y = self.map.point(end_name)
            runway += math.hypot(end_x - start_x, end_y - start_y)
            index += 1
        return runway

    @staticmethod
    def _heading_between_points(start: Tuple[float, float], end: Tuple[float, float]) -> float:
        return math.atan2(end[1] - start[1], end[0] - start[0])

    def _heading_between_vertices(self, start_name: str, end_name: str) -> Optional[float]:
        if not self._is_adjacent(start_name, end_name):
            return None
        return self._heading_between_points(self.map.point(start_name), self.map.point(end_name))

    def _is_adjacent(self, start_name: str, end_name: str) -> bool:
        if start_name == end_name:
            return True
        start_idx = self.map.name_to_index.get(start_name)
        end_idx = self.map.name_to_index.get(end_name)
        if start_idx is None or end_idx is None:
            return False
        return any(neighbor == end_idx for neighbor, _ in self.map.adjacency[start_idx])

    def _robot_in_transit(self, robot: SimRobot, waypoint_epsilon: float) -> bool:
        current_x, current_y = self.map.point(robot.current_vertex)
        return math.hypot(robot.x - current_x, robot.y - current_y) > waypoint_epsilon

    def draw(self, screen: pygame.Surface) -> None:
        screen.fill(tuple(self.display_cfg.get("background_color", [245, 245, 245])))
        self._draw_lanes(screen)
        self._draw_vertices(screen)
        self._draw_shelves(screen)
        self._draw_paths(screen)
        self._draw_robots(screen)
        self._draw_overlay(screen)

    def _draw_lanes(self, screen: pygame.Surface) -> None:
        lane_color = tuple(self.display_cfg.get("lane_color", [0, 0, 0]))
        lane_width = int(self.display_cfg.get("lane_width", 1))
        for start, end, _meta in self.map.adjacency_items():
            a = self.world_to_screen((self.map.vertices[start].x, self.map.vertices[start].y))
            b = self.world_to_screen((self.map.vertices[end].x, self.map.vertices[end].y))
            pygame.draw.line(screen, lane_color, a, b, lane_width)

    def _draw_vertices(self, screen: pygame.Surface) -> None:
        normal_color = tuple(self.display_cfg.get("vertex_color", [0, 0, 0]))
        pickup_color = tuple(self.display_cfg.get("pickup_color", [40, 120, 255]))
        dropoff_color = (20, 170, 90)
        radius = int(self.display_cfg.get("vertex_radius", 2))
        pickup_size = int(self.display_cfg.get("pickup_size", 6))
        for vertex in self.map.vertices:
            point = self.world_to_screen((vertex.x, vertex.y))
            if vertex.name in self.map.pickup_dispensers:
                rect = pygame.Rect(0, 0, pickup_size, pickup_size)
                rect.center = point
                pygame.draw.rect(screen, pickup_color, rect, width=1)
            elif vertex.name in self.map.dropoff_ingestors:
                pygame.draw.circle(screen, dropoff_color, point, radius + 2, width=1)
            else:
                pygame.draw.circle(screen, normal_color, point, radius)

    def _draw_shelves(self, screen: pygame.Surface) -> None:
        rack_color = (120, 80, 40)
        carried_color = (255, 170, 40)
        for rack_vertex in set(self.rack_positions.values()):
            point = self.world_to_screen(self.map.point(rack_vertex))
            rect = pygame.Rect(0, 0, 8, 8)
            rect.center = point
            pygame.draw.rect(screen, rack_color, rect)
        for robot in self.robots:
            if not robot.jack_up:
                continue
            point = self.world_to_screen((robot.x, robot.y))
            rect = pygame.Rect(0, 0, 10, 10)
            rect.center = point
            pygame.draw.rect(screen, carried_color, rect)

    def _draw_paths(self, screen: pygame.Surface) -> None:
        width = int(self.display_cfg.get("path_width", 2))
        for robot in self.robots:
            if len(robot.full_path_preview) >= 2:
                faded = tuple(max(0, min(255, int(channel * 0.45))) for channel in robot.color)
                full_points = [self.world_to_screen(self.map.point(name)) for name in robot.full_path_preview]
                pygame.draw.lines(screen, faded, False, full_points, 1)
            if len(robot.active_window_path) >= 2:
                points = [self.world_to_screen(self.map.point(name)) for name in robot.active_window_path]
                pygame.draw.lines(screen, robot.color, False, points, width)

    def _draw_robots(self, screen: pygame.Surface) -> None:
        radius = int(self.display_cfg.get("robot_radius", 6))
        for robot in self.robots:
            center = self.world_to_screen((robot.x, robot.y))
            pygame.draw.circle(screen, robot.color, center, radius)
            nose = (
                center[0] + int(math.cos(robot.heading) * (radius + 5)),
                center[1] - int(math.sin(robot.heading) * (radius + 5)),
            )
            pygame.draw.line(screen, (30, 30, 30), center, nose, 2)
            if robot.jack_up:
                marker_half = radius + 3
                pygame.draw.line(screen, (20, 20, 20), (center[0] - marker_half, center[1] - marker_half), (center[0] + marker_half, center[1] + marker_half), 2)
                pygame.draw.line(screen, (20, 20, 20), (center[0] - marker_half, center[1] + marker_half), (center[0] + marker_half, center[1] - marker_half), 2)

    def _draw_overlay(self, screen: pygame.Surface) -> None:
        assert self.font is not None
        lines = [
            f"Sim time: {self.overlay.sim_time_sec:8.1f}s",
            f"Unfinished store/day tasks: {self.overlay.pending_tasks}",
            f"Completed store/day tasks: {self.overlay.completed_tasks}",
            f"Active robots: {self.overlay.active_robots}/{len(self.robots)}",
            f"Reserved nodes: {self.overlay.reserved_nodes}",
        ]
        for idx, text in enumerate(lines):
            surface = self.font.render(text, True, tuple(self.display_cfg.get("text_color", [20, 20, 20])))
            screen.blit(surface, (12, 12 + idx * 20))

    def make_screen(self) -> Tuple[pygame.Surface, pygame.time.Clock]:
        self.world_to_screen = self._build_projection()
        pygame.init()
        pygame.display.set_caption("Current heat — store/day tasks")
        screen = pygame.display.set_mode((self.display_cfg["width"], self.display_cfg["height"]))
        clock = pygame.time.Clock()
        self.font = pygame.font.SysFont("consolas", 16)
        return screen, clock
