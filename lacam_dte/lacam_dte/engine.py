from __future__ import annotations

import math
from collections import Counter
from dataclasses import asdict

from .models import Amr, Config, DayResult, Grid, Job, Node, Task, parse_node
from .planner import LaCAMPlanner, PlannerError, distance_tables


def _travel_seconds(distance: float, speed: float, acceleration: float) -> float:
    threshold = speed * speed / acceleration
    return 2 * math.sqrt(distance / acceleration) if distance <= threshold else distance / speed + speed / acceleration


def tick_seconds(grid: Grid, config: Config) -> float:
    longest = max(
        math.dist(grid.coordinate(a), grid.coordinate(b)) for a, b in grid.edges
    )
    move = _travel_seconds(longest, config.motion.max_linear_speed_mps, config.motion.linear_acceleration_mps2)
    turn = 2 * math.sqrt(180 / config.motion.angular_acceleration_degps2)
    if 180 > config.motion.max_angular_speed_degps**2 / config.motion.angular_acceleration_degps2:
        turn = 180 / config.motion.max_angular_speed_degps + config.motion.max_angular_speed_degps / config.motion.angular_acceleration_degps2
    return move + turn


def _normalize_angle(value: float) -> float:
    return (value + math.pi) % (2 * math.pi) - math.pi


def _action_seconds(grid: Grid, config: Config, actions: list[tuple[Amr, Node]]) -> float:
    rotations, translations = [], []
    for amr, target in actions:
        if target == amr.node: continue
        x0, y0 = grid.coordinate(amr.node); x1, y1 = grid.coordinate(target)
        heading = math.atan2(y1 - y0, x1 - x0)
        rotations.append(_travel_seconds(
            abs(_normalize_angle(heading - amr.heading_radians)),
            math.radians(config.motion.max_angular_speed_degps),
            math.radians(config.motion.angular_acceleration_degps2),
        ))
        translations.append(_travel_seconds(
            math.hypot(x1 - x0, y1 - y0),
            config.motion.max_linear_speed_mps,
            config.motion.linear_acceleration_mps2,
        ))
    return max(rotations, default=0.0) + max(translations, default=0.0)


def _crossing_time(distance: float, total: float, maximum_rate: float, acceleration: float) -> float:
    """Time at which a rest-to-rest profile crosses an intermediate distance."""
    distance = min(total, max(0.0, distance))
    threshold = maximum_rate * maximum_rate / acceleration
    if total <= threshold:
        half_time = math.sqrt(total / acceleration)
        half_distance = total / 2
        return (math.sqrt(2 * distance / acceleration) if distance <= half_distance else
                2 * half_time - math.sqrt(2 * (total - distance) / acceleration))
    ramp_time = maximum_rate / acceleration
    ramp_distance = .5 * maximum_rate * maximum_rate / acceleration
    cruise_time = (total - 2 * ramp_distance) / maximum_rate
    if distance <= ramp_distance: return math.sqrt(2 * distance / acceleration)
    if distance <= total - ramp_distance:
        return ramp_time + (distance - ramp_distance) / maximum_rate
    return 2 * ramp_time + cruise_time - math.sqrt(2 * (total - distance) / acceleration)


def _joint_path_step_seconds(grid: Grid, config: Config, amrs: list[Amr],
                             configurations: list[list[Node]], locked: frozenset[int]) -> list[float]:
    """Time-parameterize complete LaCAM paths, preserving speed through straight runs."""
    steps = [0.0] * (len(configurations) - 1)
    angular_speed = math.radians(config.motion.max_angular_speed_degps)
    angular_accel = math.radians(config.motion.angular_acceleration_degps2)
    for agent, amr in enumerate(amrs):
        if agent in locked: continue
        heading = amr.heading_radians; step = 1
        while step < len(configurations):
            start, after = configurations[step - 1][agent], configurations[step][agent]
            if start == after:
                step += 1; continue
            x0, y0 = grid.coordinate(start); x1, y1 = grid.coordinate(after)
            direction = math.atan2(y1 - y0, x1 - x0)
            run_start = step; distances = []
            while step < len(configurations):
                before, node = configurations[step - 1][agent], configurations[step][agent]
                if before == node: break
                bx, by = grid.coordinate(before); nx, ny = grid.coordinate(node)
                candidate = math.atan2(ny - by, nx - bx)
                if abs(_normalize_angle(candidate - direction)) > 1e-9: break
                distances.append(math.hypot(nx - bx, ny - by)); step += 1
            total = sum(distances); previous = 0.0; cumulative = 0.0
            turn = _travel_seconds(abs(_normalize_angle(direction - heading)), angular_speed, angular_accel)
            for offset, distance in enumerate(distances):
                cumulative += distance
                crossing = _crossing_time(
                    cumulative, total, config.motion.max_linear_speed_mps,
                    config.motion.linear_acceleration_mps2,
                )
                duration = crossing - previous + (turn if offset == 0 else 0.0)
                index = run_start + offset - 1
                steps[index] = max(steps[index], duration); previous = crossing
            heading = direction
    wait_quantum = min(
        _travel_seconds(math.dist(grid.coordinate(a), grid.coordinate(b)),
                        config.motion.max_linear_speed_mps, config.motion.linear_acceleration_mps2)
        for a, b in grid.edges
    )
    return [duration if duration > 1e-12 else wait_quantum for duration in steps]


def simulate_day(grid: Grid, layout: dict[str, set[str]], tasks: list[Task], mapping: dict[str, str], config: Config, planner_binary, progress=None, distance_cache=None) -> DayResult:
    tick_s = tick_seconds(grid, config)
    spawns = [parse_node(v) for v in config.spawn_nodes[: config.amr_count]]
    heading = math.radians(config.initial_heading_degrees)
    amrs = [Amr(f"AMR_{i + 1:03d}", node, node, heading_radians=heading) for i, node in enumerate(spawns)]
    rack_nodes = {parse_node(rack) for rack in layout}
    station_nodes = {grid.workstations[station] for station in mapping.values()}
    distance_cache = distance_cache or distance_tables(grid, grid.nodes)
    ordered_tasks = sorted(tasks, key=lambda task: task.task_id)
    outstanding = {task.task_id: Counter(task.lines) for task in ordered_tasks}
    inflight = Counter()
    jobs: list[Job] = []
    job_sequence = 0
    planner = LaCAMPlanner(grid, planner_binary, config.planner_timeout_seconds, config.planner_seed)
    terminals = set(grid.racks.values()) | set(grid.workstations.values())
    workstation_lanes, workstation_holds = {}, {}
    adjacency = {node: [] for node in grid.nodes}
    for before, after in grid.edges: adjacency[before].append(after)
    for neighbours in adjacency.values(): neighbours.sort()

    def straight_path(start: Node, goal: Node) -> list[Node]:
        if start[0] != goal[0] and start[1] != goal[1]:
            raise ValueError(f"workstation lane segment is not straight: {start} -> {goal}")
        dx = (goal[0] > start[0]) - (goal[0] < start[0])
        dy = (goal[1] > start[1]) - (goal[1] < start[1])
        path = [start]
        while path[-1] != goal:
            node = (path[-1][0] + dx, path[-1][1] + dy)
            if node not in grid.nodes or (path[-1], node) not in grid.edges:
                raise ValueError(f"invalid directed workstation lane edge {path[-1]} -> {node}")
            path.append(node)
        return path

    def workstation_lane(entry: Node, station: Node, exit_node: Node) -> list[Node]:
        entry_bend = (entry[0], station[1])
        exit_bend = (exit_node[0], station[1])
        segments = (
            straight_path(entry, entry_bend),
            straight_path(entry_bend, station),
            straight_path(station, exit_bend),
            straight_path(exit_bend, exit_node),
        )
        lane = segments[0]
        for segment in segments[1:]: lane += segment[1:]
        return lane

    def nearest_free_hold(start: Node, unavailable: set[Node]) -> Node:
        pending, seen = [start], {start}
        for node in pending:
            if node not in unavailable and node not in terminals: return node
            for neighbour in adjacency[node]:
                if neighbour not in terminals and neighbour not in seen:
                    seen.add(neighbour); pending.append(neighbour)
        raise ValueError(f"no unique holding node reachable from {start}")

    def available_station_holds(station_id: str) -> tuple[Node, ...]:
        internal = {
            amr.node for amr in amrs
            if amr.job and amr.job.station_id == station_id and amr.job.fifo_index >= 0
        }
        return tuple(node for node in workstation_holds[station_id] if node not in internal)

    for station_id in config.workstations:
        station = grid.workstations[station_id]
        entry = parse_node(config.workstation_entries[station_id]) if station_id in config.workstation_entries else (station[0] + 2, 17)
        exit_node = parse_node(config.workstation_exits[station_id]) if station_id in config.workstation_exits else (station[0] - 1, 17)
        workstation_lanes[station_id] = workstation_lane(entry, station, exit_node)
        holds = tuple(parse_node(node) for node in config.workstation_holding_paths.get(station_id, ())) or (entry,)
        if holds[-1] != entry:
            raise ValueError(f"workstation holding path for {station_id} must end at its entry {entry}")
        if len(set(holds)) != len(holds) or any(node not in grid.nodes for node in holds):
            raise ValueError(f"workstation holding path for {station_id} contains invalid or duplicate nodes")
        if any((before, after) not in grid.edges for before, after in zip(holds, holds[1:])):
            raise ValueError(f"workstation holding path for {station_id} violates directed grid edges")
        workstation_holds[station_id] = holds[-config.station_queue_capacity:]
        terminals.update(workstation_lanes[station_id])
    clock_s = 0.0
    events, trajectory = [], [{"tick": 0, "time_seconds": 0.0, "positions": [list(a.node) for a in amrs]}]
    tick = completed_lines = rack_presentations = no_progress = collision_failures = total_soc = 0
    station_wait_ticks = travel_m = 0.0
    failed = None

    def event(kind: str, amr: Amr | None = None, event_time: float | None = None, **extra):
        events.append({"tick": tick, "time_seconds": clock_s if event_time is None else event_time, "event": kind,
                       "amr_id": amr.amr_id if amr else "", **extra})

    def fifo_actions(protected: set[Node] = set()) -> list[tuple[Amr, Node]]:
        actions, occupied = [], {a.node for a in amrs}
        for station_id, lane in workstation_lanes.items():
            agents = sorted(
                (a for a in amrs if a.job and a.job.station_id == station_id and a.job.fifo_index >= 0),
                key=lambda a: a.job.fifo_index, reverse=True,
            )
            for amr in agents:
                if amr.stage == "fifo_service": continue
                if amr.job.fifo_index + 1 >= len(lane): continue
                target = lane[amr.job.fifo_index + 1]
                if target not in occupied and target not in protected:
                    actions.append((amr, target)); occupied.remove(amr.node); occupied.add(target)
        return actions

    def advance_fifo(protected: set[Node] = set()) -> bool:
        nonlocal station_wait_ticks, travel_m
        changed = False
        for station_id, lane in workstation_lanes.items():
            workstation_index = lane.index(grid.workstations[station_id])
            agents = sorted(
                (a for a in amrs if a.job and a.job.station_id == station_id and a.job.fifo_index >= 0),
                key=lambda a: a.job.fifo_index, reverse=True,
            )
            occupied = {a.node for a in amrs}
            station_wait_ticks += sum(a.job.fifo_index < workstation_index for a in agents)
            for amr in agents:
                index = amr.job.fifo_index
                if amr.stage == "fifo_service": continue
                if index + 1 >= len(lane):
                    amr.job.fifo_index = -1; amr.job.station_queue_node = None
                    amr.stage = "to_return"; amr.goal = amr.job.rack_node
                    event("workstation_exit", amr, job_id=amr.job.job_id, station=station_id); changed = True
                    continue
                target = lane[index + 1]
                if target in occupied or target in protected: continue
                occupied.remove(amr.node); occupied.add(target)
                distance = math.dist(grid.coordinate(amr.node), grid.coordinate(target))
                x0, y0 = grid.coordinate(amr.node); x1, y1 = grid.coordinate(target)
                amr.node = target; amr.goal = target; amr.job.fifo_index += 1
                amr.heading_radians = math.atan2(y1 - y0, x1 - x0)
                amr.distance_m += distance; travel_m += distance; amr.busy_ticks += 1
                if amr.job.fifo_index == workstation_index:
                    amr.stage = "fifo_service"
                    amr.busy_until = clock_s + config.service_seconds
                    event("station_reached", amr, job_id=amr.job.job_id, station=station_id)
                else:
                    amr.stage = "workstation_fifo"
                changed = True
        return changed

    max_ticks = 10_000_000
    while tick < max_ticks:
        progressed = False
        for amr in amrs:
            if amr.busy_until > clock_s: continue
            if amr.stage == "pickup":
                amr.stage = "to_station_entry"; amr.goal = amr.job.station_queue_node
                event("depart_rack_for_queue", amr, event_time=amr.busy_until,
                      job_id=amr.job.job_id, entry_node=list(amr.goal))
                progressed = True
            elif amr.stage == "fifo_service":
                amr.stage = "workstation_fifo"
                event("service_done", amr, event_time=amr.busy_until,
                      job_id=amr.job.job_id, station=amr.job.station_id)
                progressed = True
            elif amr.stage == "dropoff":
                job = amr.job; job.stage = "complete"; job.completion_tick = tick
                inflight[job.task_id] -= 1
                completed_lines += job.lines
                if progress is not None: progress(job.lines)
                event("job_complete", amr, event_time=amr.busy_until, job_id=job.job_id, lines=job.lines)
                amr.job = None; amr.stage = "idle"; amr.loaded = False; amr.goal = amr.node; progressed = True
        idle = sorted((a for a in amrs if a.stage == "idle"), key=lambda a: a.amr_id)
        claimed_racks = {a.job.rack_node for a in amrs if a.job}
        while idle:
            choice = None
            for task in ordered_tasks:
                remaining = outstanding[task.task_id]
                if not remaining: continue
                station_id = mapping[task.store]
                admitted = sum(
                    a.job is not None and a.job.station_id == station_id
                    and a.stage in {"to_rack", "pickup", "to_station_entry"}
                    for a in amrs
                )
                if admitted >= len(available_station_holds(station_id)): continue
                station_node = grid.workstations[station_id]
                lane = workstation_lanes[station_id]
                queue_node = lane[0]
                candidates = []
                for rack_id, skus in layout.items():
                    rack_node = parse_node(rack_id)
                    if rack_node in claimed_racks: continue
                    covered = sorted(skus & remaining.keys())
                    if covered:
                        candidates.append((
                            -len(covered), math.dist(grid.coordinate(station_node), grid.coordinate(rack_node)),
                            rack_id, rack_node, covered,
                        ))
                for candidate in sorted(candidates):
                    rack_node = candidate[3]
                    if rack_node not in distance_cache[queue_node]: continue
                    owner = next((amr for amr in idle if amr.node == rack_node), None)
                    choices = [owner] if owner is not None else idle
                    reachable = [
                        (distance_cache[rack_node].get(amr.node, 10**9), amr.amr_id, amr)
                        for amr in choices if amr is not None and amr.node in distance_cache[rack_node]
                    ]
                    if reachable:
                        choice = task, station_id, station_node, queue_node, candidate, min(reachable)[2]
                        break
                if choice is not None: break
            if choice is None: break
            task, station_id, station_node, queue_node, candidate, amr = choice
            _coverage, _station_distance, rack_id, rack_node, covered = candidate
            lines = {sku: outstanding[task.task_id].pop(sku) for sku in covered}
            job_sequence += 1
            job = Job(
                f"J{job_sequence:06d}", task.task_id, task.store, rack_id, rack_node,
                station_id, station_node, sum(lines.values()), 0, queue_node,
                stage="active", amr_id=amr.amr_id, dispatch_tick=tick,
            )
            jobs.append(job); inflight[task.task_id] += 1
            amr.job = job; amr.stage = "to_rack"; amr.goal = rack_node
            claimed_racks.add(rack_node); idle.remove(amr)
            event("job_dispatched", amr, job_id=job.job_id, covered_skus=covered,
                  covered_lines=job.lines); progressed = True

        # Complete zero-distance pickup arrivals before asking LaCAM to plan.
        for amr in amrs:
            if amr.stage == "to_rack" and amr.node == amr.goal:
                amr.stage = "pickup"; amr.loaded = True
                amr.busy_until = clock_s + config.jack_up_seconds
                rack_presentations += 1; event("rack_reached", amr, job_id=amr.job.job_id); progressed = True
        if all(not value for value in outstanding.values()) and all(amr.job is None for amr in amrs): break

        # Give every admitted station job a stable, unique FIFO holding goal.
        # Only the head job may target the entry; promotion happens after it
        # crosses into the deterministic internal workstation lane.
        entrants = [a for a in amrs if a.stage == "to_station_entry"]
        idle = [a for a in amrs if a.stage == "idle"]
        for station_id in workstation_holds:
            holds = available_station_holds(station_id)
            admitted = sorted(
                (a for a in amrs if a.job and a.job.station_id == station_id
                 and a.stage in {"to_rack", "pickup", "to_station_entry"}),
                key=lambda a: (a.job.dispatch_tick, a.job.job_id, a.amr_id),
            )
            if len(admitted) > len(holds):
                raise RuntimeError(
                    f"workstation admission overflow at {station_id}: "
                    f"admitted={[(a.amr_id, a.node, a.goal, a.stage) for a in admitted]}, "
                    f"holds={holds}, internal={[(a.amr_id, a.node, a.job.fifo_index, a.stage) for a in amrs if a.job and a.job.station_id == station_id and a.job.fifo_index >= 0]}"
                )
            for index, amr in enumerate(admitted):
                amr.job.station_queue_node = holds[-1 - index]
                if amr.stage == "to_station_entry": amr.goal = amr.job.station_queue_node
        reserved_goals = {a.goal for a in amrs if a.stage != "idle"}
        idle_starts = {a.node for a in idle}
        returning = any(a.stage == "to_return" for a in amrs)
        for amr in sorted(idle, key=lambda a: a.amr_id):
            must_clear = amr.node in reserved_goals or (returning and amr.node in terminals)
            amr.goal = (amr.node if not must_clear else
                        nearest_free_hold(amr.node, reserved_goals | (idle_starts - {amr.node})))
            reserved_goals.add(amr.goal)

        starts, goals = [a.node for a in amrs], [a.goal for a in amrs]
        fifo_active = any(amr.job and amr.job.fifo_index >= 0 for amr in amrs)
        if starts == goals:
            if fifo_active:
                available_fifo_actions = fifo_actions()
                step_s = _action_seconds(grid, config, available_fifo_actions)
                future = [a.busy_until for a in amrs if a.busy_until > clock_s]
                if step_s == 0 and future:
                    clock_s = min(future)
                    trajectory.append({"tick": tick, "time_seconds": clock_s, "positions": [list(a.node) for a in amrs],
                                       "stages": [a.stage for a in amrs], "goals": [list(a.goal) for a in amrs],
                                       "loaded": [a.loaded for a in amrs]})
                    continue
                tick += 1; clock_s += step_s; progressed |= advance_fifo()
                trajectory.append({"tick": tick, "time_seconds": clock_s, "positions": [list(a.node) for a in amrs],
                                   "stages": [a.stage for a in amrs], "goals": [list(a.goal) for a in amrs],
                                   "loaded": [a.loaded for a in amrs]})
                continue
            future = [a.busy_until for a in amrs if a.busy_until > clock_s]
            if not future:
                failed = "no executable work remains"; break
            clock_s = min(future)
            trajectory.append({"tick": tick, "time_seconds": clock_s, "positions": [list(a.node) for a in amrs],
                               "stages": [a.stage for a in amrs], "goals": [list(a.goal) for a in amrs],
                               "loaded": [a.loaded for a in amrs]})
            continue
        try:
            locked = frozenset(
                index for index, amr in enumerate(amrs)
                if amr.job and amr.job.fifo_index >= 0
            )
            plan = planner.solve(starts, goals, terminals, locked)
        except PlannerError as exc:
            failed = str(exc); event("planner_failure", reason=failed); break
        total_soc += plan.sum_of_costs
        stop = len(plan.configurations) - 1
        for step in range(1, len(plan.configurations)):
            if any(
                i not in locked
                and plan.configurations[step][i] == goals[i]
                and starts[i] != goals[i]
                and (
                    amrs[i].stage in {"to_rack", "to_return"}
                    or (amrs[i].stage == "to_station_entry"
                        and goals[i] == amrs[i].job.station_queue_node)
                )
                for i in range(len(amrs))
            ):
                stop = step; break
        executing = plan.configurations[:stop + 1]
        planned_step_seconds = _joint_path_step_seconds(grid, config, amrs, executing, locked)
        for plan_step, config_step in enumerate(executing[1:]):
            protected = {
                configuration[i]
                for configuration in executing[plan_step + 1:]
                for i in range(len(amrs)) if i not in locked
            }
            planned_wait = [
                i not in locked and nxt == amr.node and amr.node != amr.goal
                for i, (amr, nxt) in enumerate(zip(amrs, config_step))
            ]
            trajectory[-1]["planned_wait"] = planned_wait
            step_s = max(planned_step_seconds[plan_step],
                         _action_seconds(grid, config, fifo_actions(protected)))
            future = [a.busy_until for a in amrs if a.busy_until > clock_s]
            event_due = bool(future and min(future) < clock_s + step_s)
            tick += 1; clock_s += step_s
            moved = False
            for i, (amr, nxt) in enumerate(zip(amrs, config_step)):
                if i in locked: continue
                if nxt != amr.node:
                    distance = math.dist(grid.coordinate(amr.node), grid.coordinate(nxt))
                    x0, y0 = grid.coordinate(amr.node); x1, y1 = grid.coordinate(nxt)
                    amr.heading_radians = math.atan2(y1 - y0, x1 - x0)
                    amr.distance_m += distance; travel_m += distance; amr.busy_ticks += 1; moved = True
                elif amr.stage != "idle": amr.wait_ticks += 1
                amr.node = nxt
            progressed |= advance_fifo(protected)
            trajectory.append({"tick": tick, "time_seconds": clock_s, "positions": [list(a.node) for a in amrs],
                               "stages": [a.stage for a in amrs], "goals": [list(a.goal) for a in amrs],
                               "loaded": [a.loaded for a in amrs]})
            progressed |= moved
            if event_due: break
        for amr in amrs:
            if amr.node != amr.goal or amr.stage not in {"to_rack", "to_station_entry", "to_return"}: continue
            if amr.stage == "to_rack":
                amr.stage = "pickup"; amr.loaded = True
                amr.busy_until = clock_s + config.jack_up_seconds
                rack_presentations += 1; event("rack_reached", amr, job_id=amr.job.job_id)
            elif amr.stage == "to_station_entry":
                # Reaching an external holding cell is not workstation entry.
                # The job remains admitted and is promoted toward lane[0] when
                # earlier FIFO tickets have crossed into the internal lane.
                if amr.node != workstation_lanes[amr.job.station_id][0]:
                    continue
                amr.stage = "workstation_fifo"; amr.job.fifo_index = 0; amr.goal = amr.node
                event("station_queue_arrival", amr, job_id=amr.job.job_id, station=amr.job.station_id)
            elif amr.stage == "to_return":
                amr.stage = "dropoff"; amr.busy_until = clock_s + config.jack_down_seconds
                event("rack_returned", amr, job_id=amr.job.job_id)
            progressed = True
        no_progress = 0 if progressed else no_progress + 1
        if no_progress >= config.no_progress_ticks:
            failed = "no-progress limit reached"; break

    makespan_s = clock_s
    completed_tasks = sum(not outstanding[task.task_id] and inflight[task.task_id] == 0 for task in ordered_tasks)
    metrics = {
        "date": tasks[0].date.isoformat(), "tick_seconds": tick_s,
        "makespan_ticks": tick, "makespan_seconds": makespan_s, "makespan_hours": makespan_s / 3600,
        "completed_lines": completed_lines, "completed_tasks": completed_tasks,
        "rack_presentations": rack_presentations, "line_throughput_per_hour": completed_lines / (makespan_s / 3600) if makespan_s else 0,
        "travel_distance_m": travel_m, "wait_ticks": sum(a.wait_ticks for a in amrs),
        "station_queue_time_seconds": station_wait_ticks * tick_s,
        "amr_utilization": sum(a.busy_ticks for a in amrs) / (max(1, tick) * len(amrs)),
        "planner_calls": planner.calls, "planner_time_seconds": planner.runtime_seconds,
        "planner_retries": planner.retries, "planner_timeouts": planner.timeouts,
        "lacam_sum_of_costs": total_soc,
        "collision_validation_failures": collision_failures, "no_progress_failures": int(failed == "no-progress limit reached"),
        "failed": bool(failed), "failure_reason": failed or "",
    }
    return DayResult(metrics, events, trajectory, [asdict(j) for j in jobs])
