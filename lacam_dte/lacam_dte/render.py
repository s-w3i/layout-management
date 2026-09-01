from __future__ import annotations

import time
import math
from bisect import bisect_right

from .models import Config, DayResult, Grid, parse_node


def _profile_progress(amount: float, maximum_rate: float, acceleration: float, elapsed: float) -> float:
    """Distance/angle completed from rest under a symmetric motion profile."""
    if amount <= 1e-12 or elapsed <= 0: return 0.0
    threshold = maximum_rate * maximum_rate / acceleration
    if amount <= threshold:
        peak = math.sqrt(amount * acceleration); half = peak / acceleration
        if elapsed <= half: return .5 * acceleration * elapsed * elapsed
        if elapsed < 2 * half:
            remaining = 2 * half - elapsed
            return amount - .5 * acceleration * remaining * remaining
        return amount
    ramp = maximum_rate / acceleration
    ramp_distance = .5 * maximum_rate * maximum_rate / acceleration
    cruise = (amount - 2 * ramp_distance) / maximum_rate
    if elapsed <= ramp: return .5 * acceleration * elapsed * elapsed
    if elapsed <= ramp + cruise: return ramp_distance + maximum_rate * (elapsed - ramp)
    if elapsed < 2 * ramp + cruise:
        remaining = 2 * ramp + cruise - elapsed
        return amount - .5 * acceleration * remaining * remaining
    return amount


def _profile_duration(amount: float, maximum_rate: float, acceleration: float) -> float:
    if amount <= 1e-12: return 0.0
    threshold = maximum_rate * maximum_rate / acceleration
    return (2 * math.sqrt(amount / acceleration) if amount <= threshold else
            amount / maximum_rate + maximum_rate / acceleration)


def _angle_delta(target: float, current: float) -> float:
    return (target - current + math.pi) % (2 * math.pi) - math.pi


def render(grid: Grid, result: DayResult, config: Config, speed: float = 20.0) -> None:
    import matplotlib.pyplot as plt
    from matplotlib.widgets import Button, Slider

    rows = result.trajectory
    if not rows: raise ValueError("trace contains no trajectory")
    row_times = [row.get("time_seconds", row["tick"] * result.metrics["tick_seconds"]) for row in rows]
    count = len(rows[0]["positions"])
    row_headings = [[math.radians(config.initial_heading_degrees)] * count]
    for before, after in zip(rows, rows[1:]):
        values = list(row_headings[-1])
        for i, (start, end) in enumerate(zip(before["positions"], after["positions"])):
            if start != end:
                x0, y0 = grid.coordinate(tuple(start)); x1, y1 = grid.coordinate(tuple(end))
                values[i] = math.atan2(y1 - y0, x1 - x0)
        row_headings.append(values)
    figure, axis = plt.subplots(figsize=(15, 8)); plt.subplots_adjust(bottom=.18, right=.76)
    for a, b in grid.edges:
        x1, y1 = grid.coordinate(a); x2, y2 = grid.coordinate(b)
        axis.plot((x1, x2), (y1, y2), color="#dddddd", linewidth=.35, zorder=0)
    rack_xy = [grid.coordinate(n) for n in grid.racks.values()]
    station_xy = [grid.coordinate(n) for n in grid.workstations.values()]
    axis.scatter([p[0] for p in rack_xy], [p[1] for p in rack_xy], marker="s", s=15, color="#b8a58d")
    axis.scatter([p[0] for p in station_xy], [p[1] for p in station_xy], marker="D", s=35, color="#277da1")
    entries = [grid.coordinate(parse_node(config.workstation_entries[ws])) for ws in config.workstations]
    exits = [grid.coordinate(parse_node(config.workstation_exits[ws])) for ws in config.workstations]
    for ws in config.workstations:
        entry = parse_node(config.workstation_entries[ws]); station = grid.workstations[ws]
        exit_node = parse_node(config.workstation_exits[ws])
        lane = (entry, (entry[0], station[1]), station, (exit_node[0], station[1]), exit_node)
        points = [grid.coordinate(node) for node in lane]
        axis.plot([p[0] for p in points], [p[1] for p in points], color="#43aa8b",
                  linestyle="--", linewidth=1.5, alpha=.8, zorder=2)
    axis.scatter([p[0] for p in entries], [p[1] for p in entries], marker="^", s=55,
                 color="#43aa8b", label="queue entry", zorder=3)
    axis.scatter([p[0] for p in exits], [p[1] for p in exits], marker="v", s=55,
                 color="#577590", label="queue exit", zorder=3)
    artist = axis.scatter([], [], s=45, c="#f94144", zorder=4)
    paths = [axis.plot([], [], alpha=.35, linewidth=1)[0] for _ in config.spawn_nodes[:config.amr_count]]
    headings = [axis.plot([], [], color="#222222", linewidth=1.5)[0] for _ in paths]
    status = axis.text(1.02, .98, "", transform=axis.transAxes, va="top", family="monospace", fontsize=9)
    state = {"index": 0, "play_time": row_times[0], "playing": True, "speed": speed, "last": time.monotonic()}
    play = Button(plt.axes((.08, .05, .10, .05)), "Play/Pause")
    restart = Button(plt.axes((.20, .05, .10, .05)), "Restart")
    next_event = Button(plt.axes((.32, .05, .10, .05)), "Next event")
    slider = Slider(plt.axes((.50, .06, .22, .03)), "Time (s)", row_times[0], row_times[-1], valinit=row_times[0])
    play.on_clicked(lambda _e: state.update(playing=not state["playing"]))
    restart.on_clicked(lambda _e: (state.update(index=0, play_time=row_times[0], playing=False), slider.set_val(row_times[0])))
    event_times = sorted({event["time_seconds"] for event in result.events})
    next_event.on_clicked(lambda _e: slider.set_val(next((t for t in event_times if t > state["play_time"]), row_times[-1])))
    slider.on_changed(lambda v: state.update(
        index=min(len(rows)-1, bisect_right(row_times, float(v)) - 1), play_time=float(v), playing=False))

    def draw(_frame):
        now = time.monotonic(); elapsed = now - state["last"]; state["last"] = now
        if state["playing"]:
            state["play_time"] = min(row_times[-1], state["play_time"] + elapsed * state["speed"])
            state["index"] = min(len(rows)-1, bisect_right(row_times, state["play_time"]) - 1)
            slider.eventson = False; slider.set_val(state["play_time"]); slider.eventson = True
        index = state["index"]; row = rows[index]
        positions = [grid.coordinate(tuple(p)) for p in row["positions"]]
        display_headings = list(row_headings[index])
        if index + 1 < len(rows) and row_times[index + 1] > row_times[index]:
            following = rows[index + 1]; local_time = state["play_time"] - row_times[index]
            moving = [i for i, (a, b) in enumerate(zip(row["positions"], following["positions"])) if a != b]
            turns = {}
            for i in moving:
                x0, y0 = positions[i]; x1, y1 = grid.coordinate(tuple(following["positions"][i]))
                target = math.atan2(y1 - y0, x1 - x0)
                turns[i] = (target, _angle_delta(target, row_headings[index][i]))
            angular_speed = math.radians(config.motion.max_angular_speed_degps)
            angular_accel = math.radians(config.motion.angular_acceleration_degps2)
            for i in moving:
                start = positions[i]; end = grid.coordinate(tuple(following["positions"][i]))
                target, delta = turns[i]
                turned = _profile_progress(abs(delta), angular_speed, angular_accel, local_time)
                display_headings[i] = row_headings[index][i] + math.copysign(turned, delta)
                interval = row_times[index + 1] - row_times[index]
                rotation = _profile_duration(abs(delta), angular_speed, angular_accel)
                # Preserve velocity through consecutive straight LaCAM
                # configurations. Turns rotate first; explicit wait
                # configurations remain stationary because start == end.
                translation_time = max(interval - rotation, 1e-12)
                fraction = min(1.0, max(0.0, (local_time - rotation) / translation_time))
                positions[i] = (start[0] + (end[0] - start[0]) * fraction,
                                start[1] + (end[1] - start[1]) * fraction)
                if fraction > 0: display_headings[i] = target
        artist.set_offsets(positions)
        stages = row.get("stages", ["idle"] * len(positions))
        waits = row.get("planned_wait", [False] * len(positions))
        artist.set_color(["#f9c74f" if waits[i] else
                          "#9b5de5" if stage == "fifo_service" else
                          "#f8961e" if stage == "workstation_fifo" else
                          "#adb5bd" if stage == "idle" else "#f94144" for i, stage in enumerate(stages)])
        for i, line in enumerate(paths):
            history = [grid.coordinate(tuple(r["positions"][i])) for r in rows[:state["index"]+1]]
            if positions[i] != history[-1]: history.append(positions[i])
            line.set_data([p[0] for p in history], [p[1] for p in history])
            x, y = positions[i]; angle = display_headings[i]
            headings[i].set_data((x, x + .4 * math.cos(angle)), (y, y + .4 * math.sin(angle)))
        completed = sum(int(e.get("lines", 0)) for e in result.events if e["event"] == "job_complete" and e["time_seconds"] <= row_times[state["index"]])
        calls = sum(e["event"] == "job_dispatched" for e in result.events if e["time_seconds"] <= row_times[state["index"]])
        status.set_text(f"plan step: {row['tick']}\ntime: {state['play_time']:.1f}s\ncompleted lines: {completed}\nactive jobs: {calls}\n" + "\n".join(f"AMR_{i+1:03d}: {s}{' MAPF-WAIT' if waits[i] else ''} {'loaded' if row.get('loaded',[False]*len(paths))[i] else 'empty'} → {row.get('goals', row['positions'])[i]}" for i,s in enumerate(row.get("stages", []))))
        return artist, status, *paths, *headings

    from matplotlib.animation import FuncAnimation
    animation = FuncAnimation(figure, draw, interval=33, blit=False, cache_frame_data=False)
    figure._lacam_animation = animation
    axis.legend(loc="upper left"); axis.set_aspect("equal")
    axis.set_title("LaCAM DTE playback — yellow: LaCAM wait, orange: FIFO, purple: service")
    plt.show()
