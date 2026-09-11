"""One reserved yielding move at a time for prolonged movement stalls."""

from collections import deque
import math


class DeadlockRecovery:
    def __init__(self):
        self.positions = {}
        self.last_moved = {}
        self.last_fleet_motion = 0.0
        self.last_task_progress = 0.0
        self.task_progress = None
        self.next_check = 0.0
        self.active = None
        self.last_event = None
        self.events = deque(maxlen=100)

    def record(self, event):
        self.last_event = event
        self.events.append(event)

    def update(self, system, snapshots):
        now = system.sim_time_sec
        if now < self.next_check:
            return
        self.next_check = now + 5.0
        allocator = system.allocator
        contexts = system.task_state_machine.contexts
        progress = (sum(task.completed_lines for task in system.task_scheduler.tasks),
                    tuple((name, context.assigned_task.task_id if context.assigned_task else None,
                           context.phase) for name, context in sorted(contexts.items())))
        if progress != self.task_progress or any(context.action_timer > 0 for context in contexts.values()):
            self.last_task_progress = now
        self.task_progress = progress
        for name, robot in snapshots.items():
            position = (robot.x, robot.y)
            previous = self.positions.get(name)
            if previous is None or math.dist(position, previous) > 1e-6:
                self.last_moved[name] = now
                self.last_fleet_motion = now
            self.positions[name] = position

        if self.active:
            name, refuge, started = self.active
            robot = snapshots[name]
            arrived = robot.current_vertex == refuge and allocator.robot_at_goal(name)
            if not arrived and now - started < 60.0:
                return
            # Never revoke a command while a robot is traversing its edge.
            if math.dist((robot.x, robot.y), system.map.point(robot.current_vertex)) > .05:
                return
            allocator.recovery_robots.discard(name)
            self.active = None
            self.last_moved[name] = now
            self.record({"event": "refuge_reached" if arrived else "yield_timeout",
                         "robot": name, "node": robot.current_vertex, "sim_time_s": now})
            system.plan_generation.clear_tabu_set_for_robot(name)
            allocator.sync_robot_to_current_vertex(name, robot.current_vertex)
            system._replan(name, snapshots)
            return

        motion_stalled = now - self.last_fleet_motion >= 30.0
        task_stalled = now - self.last_task_progress >= 120.0
        if not motion_stalled and not task_stalled:
            return
        trigger = "no_fleet_motion" if motion_stalled else "no_task_progress"
        candidates = [name for name, robot in snapshots.items()
                      if now - self.last_moved[name] >= 30.0
                      and contexts[name].assigned_task is not None
                      and contexts[name].action_timer <= 0
                      and contexts[name].phase not in {"idle", "pickup_wait", "dropoff_wait", "return_wait"}
                      and robot.current_vertex != contexts[name].last_goal
                      and len(allocator._window_nodes(name, robot.current_vertex)) <= 1
                      and math.dist((robot.x, robot.y), system.map.point(robot.current_vertex)) <= .05]
        for name in sorted(candidates, key=lambda n: (self.last_moved[n], n)):
            path = self.refuge_path(system, snapshots, name)
            if path is None:
                continue
            bridge = system.plan_generation
            bridge.pending_replans.pop(name, None)
            bridge.retry_after.pop(name, None)
            allocator.pending_replans.discard(name)
            bridge.clear_tabu_set_for_robot(name)
            allocator.set_full_path(name, path, contexts[name].last_goal)
            # Reserve the whole short escape before letting anyone else allocate.
            for node in path:
                allocator.mutex_passage.update_move_buffer(name, node)
                allocator.global_reservations[node] = name
            allocator.states[name].current_index = len(path)
            allocator.recovery_robots.add(name)
            if bridge.heat_layer is not None:
                bridge.heat_layer.path_changed(name, path)
            self.active = (name, path[-1], now)
            self.record({"event": "yield_started", "robot": name, "path": path,
                         "original_goal": contexts[name].last_goal, "sim_time_s": now,
                         "trigger": trigger, "no_task_progress_sim_s": now-self.last_task_progress})
            return
        if candidates:
            self.record({"event": "no_reachable_refuge", "robots": sorted(candidates),
                         "sim_time_s": now, "trigger": trigger})

    @staticmethod
    def refuge_path(system, snapshots, name):
        """Find a short directed escape, with a static route back to the task goal."""
        warehouse, allocator = system.map, system.allocator
        start = snapshots[name].current_vertex
        goal = system.task_state_machine.contexts[name].last_goal
        shelves = set(warehouse.pickup_dispensers) | system.task_state_machine.occupied_shelves
        static_blocked = shelves - {start, goal}
        blocked = static_blocked | {node for node, owner in allocator.global_reservations.items() if owner != name}
        simulator = system.simulator
        for robot in simulator.robots:
            if robot.name != name:
                blocked.update(simulator._occupied_vertices(robot,
                    float(simulator.motion_cfg["waypoint_epsilon_m"]),
                    float(simulator.motion_cfg["collision_clearance_m"])))
        # Do not park on another robot's immediate route or on a workstation/rack.
        unsuitable = shelves | set(warehouse.workstations.values()) | {start, goal}
        for other, state in allocator.states.items():
            if other != name:
                unsuitable.update(state.full_path[:3])

        reverse = {}
        for a, b, cost in warehouse.adjacency_items():
            source, target = warehouse.vertices[a].name, warehouse.vertices[b].name
            if source not in static_blocked and target not in static_blocked and math.isfinite(system.planner._edge_cost(a, b, cost)):
                reverse.setdefault(target, []).append(source)
        reachable_goal = {goal}
        queue = deque([goal])
        while queue:
            for source in reverse.get(queue.popleft(), ()):
                if source not in reachable_goal:
                    reachable_goal.add(source)
                    queue.append(source)

        queue = deque([[start]])
        visited = {start}
        while queue:
            path = queue.popleft()
            node = path[-1]
            if node not in unsuitable and node in reachable_goal:
                return path
            if len(path) >= 7:
                continue
            index = warehouse.name_to_index[node]
            for neighbor, cost in warehouse.adjacency[index]:
                target = warehouse.vertices[neighbor].name
                if target not in visited and target not in blocked and math.isfinite(system.planner._edge_cost(index, neighbor, cost)):
                    visited.add(target)
                    queue.append(path + [target])
        return None
