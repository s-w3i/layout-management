"""Smooth real-time Matplotlib rendering of an authoritative simulation run."""

from __future__ import annotations

import bisect
import math
import time
from dataclasses import dataclass

from warehouse_layout.domain import GridProject

from .models import DayResult, grid_position


@dataclass(slots=True)
class PlaybackController:
    event_times: tuple[float, ...]
    start_time: float
    end_time: float
    time: float
    speed: float = 1.0
    playing: bool = False

    @classmethod
    def from_result(cls, result: DayResult) -> "PlaybackController":
        times = tuple(sorted({float(event["time_seconds"]) for event in result.events}))
        start = float(result.metrics["first_release_seconds"])
        end = float(result.metrics["final_completion_seconds"])
        return cls(times, start, end, start)

    def toggle(self) -> None:
        self.playing = not self.playing

    def restart(self) -> None:
        self.time, self.playing = self.start_time, False

    def next_event(self) -> None:
        index = bisect.bisect_right(self.event_times, self.time + 1e-9)
        if index < len(self.event_times):
            self.time = self.event_times[index]
        self.playing = False

    def advance(self, real_seconds: float) -> None:
        if self.playing:
            self.time = min(self.end_time, self.time + real_seconds * self.speed)
            if self.time >= self.end_time:
                self.playing = False


def _pose(segments, starts, when, project, spawn, initial_heading):
    index = bisect.bisect_right(starts, when) - 1
    if index >= 0:
        segment = segments[index]
        if when <= segment.end_time:
            return (*segment.pose_at(when), segment.kind, segment.loaded)
        x, y, heading = segment.pose_at(segment.end_time)
        return x, y, heading, "idle", False
    x, y = project.coordinates(*grid_position(spawn))
    return x, y, initial_heading, "idle", False


def _format_elapsed(seconds: float) -> str:
    return (
        f"{int(seconds // 3600):02d}h "
        f"{int(seconds % 3600 // 60):02d}m "
        f"{seconds % 60:05.2f}s"
    )


def run_debugger(
    project: GridProject,
    result: DayResult,
    spawn_nodes: tuple[str, ...],
    *,
    speed: float = 120.0,
    initial_heading_degrees: float = 0.0,
) -> None:
    """Render continuous frames; playback never mutates simulation results."""
    import matplotlib.pyplot as plt
    from matplotlib.animation import FuncAnimation
    from matplotlib.widgets import Button, Slider

    controller = PlaybackController.from_result(result)
    controller.speed, controller.playing = speed, True
    figure, axis = plt.subplots(figsize=(15, 8))
    plt.subplots_adjust(bottom=0.18, right=0.70)
    play = Button(plt.axes((0.08, 0.05, 0.1, 0.05)), "Play/Pause")
    next_event = Button(plt.axes((0.20, 0.05, 0.1, 0.05)), "Next event")
    restart = Button(plt.axes((0.32, 0.05, 0.1, 0.05)), "Restart")
    speed_control = Slider(
        plt.axes((0.50, 0.06, 0.22, 0.03)),
        "Speed", 0.1, max(3600.0, speed * 2), valinit=speed,
    )
    play.on_clicked(lambda _event: controller.toggle())
    next_event.on_clicked(lambda _event: controller.next_event())

    def restart_live(_event):
        controller.restart()
        controller.playing = True

    restart.on_clicked(restart_live)
    speed_control.on_changed(lambda value: setattr(controller, "speed", float(value)))

    for start, end in project.iter_traversable_lane_positions():
        x1, y1 = project.coordinates(*start)
        x2, y2 = project.coordinates(*end)
        axis.plot((x1, x2), (y1, y2), color="#dddddd", linewidth=0.4, zorder=0)
    rack_items = sorted(
        (position, project.vertex_name(*position))
        for position, marker in project.markers.items()
        if marker.role == "rack"
    )
    rack_xy = [project.coordinates(*position) for position, _rack_id in rack_items]
    rack_artist = axis.scatter(
        [point[0] for point in rack_xy], [point[1] for point in rack_xy],
        marker="s", s=18, color="#b8a58d", zorder=1,
    )
    station_xy = [
        project.coordinates(*position)
        for position, marker in project.markers.items()
        if marker.endpoint_id in result.metrics["workstations"]
    ]
    axis.scatter(
        [point[0] for point in station_xy], [point[1] for point in station_xy],
        marker="D", s=30, color="#277da1", zorder=2,
    )
    amr_ids = sorted(result.metrics["amrs"])
    amr_artist = axis.scatter([], [], s=35, zorder=4)
    headings = [axis.plot([], [], color="#222222", linewidth=1.5, zorder=5)[0] for _ in amr_ids]
    paths = [axis.plot([], [], color="#4895ef", alpha=0.35, zorder=2)[0] for _ in amr_ids]
    status = axis.text(
        1.04, 0.98, "", transform=axis.transAxes, va="top",
        fontsize=9, family="monospace", linespacing=1.35,
        bbox={"facecolor": "white", "edgecolor": "#dddddd", "pad": 8},
    )
    axis.set_title("Live AMR warehouse simulation")
    axis.set_aspect("equal", adjustable="box")

    segments_by_amr = {
        amr_id: sorted(
            (segment for segment in result.motion_segments if segment.amr_id == amr_id),
            key=lambda segment: (segment.start_time, segment.end_time),
        )
        for amr_id in amr_ids
    }
    segment_starts = {
        amr_id: [segment.start_time for segment in segments]
        for amr_id, segments in segments_by_amr.items()
    }
    jobs_by_amr = {
        amr_id: sorted(
            (job for job in result.jobs if job["amr_id"] == amr_id),
            key=lambda job: job["dispatch_time"],
        )
        for amr_id in amr_ids
    }
    job_starts = {
        amr_id: [job["dispatch_time"] for job in jobs]
        for amr_id, jobs in jobs_by_amr.items()
    }
    completed_jobs = sorted(result.jobs, key=lambda job: float(job["completion_time"]))
    completion_times = [float(job["completion_time"]) for job in completed_jobs]
    completed_line_totals, total = [], 0
    for job in completed_jobs:
        total += job["covered_lines"]
        completed_line_totals.append(total)
    task_completion_times = sorted(
        max(
            float(job["completion_time"])
            for job in result.jobs
            if job["task_id"] == task_id
        )
        for task_id in {job["task_id"] for job in result.jobs}
    )
    initial_heading = math.radians(initial_heading_degrees)
    last_frame_time = time.monotonic()

    def active_job(amr_id):
        index = bisect.bisect_right(job_starts[amr_id], controller.time) - 1
        if index < 0:
            return None
        job = jobs_by_amr[amr_id][index]
        return job if controller.time < float(job["completion_time"]) else None

    def draw(_frame):
        nonlocal last_frame_time
        now = time.monotonic()
        controller.advance(now - last_frame_time)
        last_frame_time = now
        active = {amr_id: active_job(amr_id) for amr_id in amr_ids}
        reserved = {job["rack_id"] for job in active.values() if job}
        away = {
            job["rack_id"]
            for job in active.values()
            if job
            and float(job["rack_departure"]) <= controller.time < float(job["rack_return"])
        }
        rack_artist.set_facecolors([
            "#e76f51" if rack_id in reserved else "#b8a58d"
            for _position, rack_id in rack_items
        ])
        rack_artist.set_sizes([
            0 if rack_id in away else 18
            for _position, rack_id in rack_items
        ])
        poses, colors = [], []
        for index, (amr_id, spawn) in enumerate(zip(amr_ids, spawn_nodes)):
            x, y, heading, state, loaded = _pose(
                segments_by_amr[amr_id], segment_starts[amr_id],
                controller.time, project, spawn, initial_heading,
            )
            poses.append((x, y))
            colors.append("#d62828" if loaded else "#2a9d8f")
            headings[index].set_data(
                (x, x + 0.55 * math.cos(heading)),
                (y, y + 0.55 * math.sin(heading)),
            )
            job = active[amr_id]
            points = [
                project.coordinates(*position)
                for position in result.paths.get(job["job_id"], [])
            ] if job else []
            paths[index].set_data(
                [point[0] for point in points], [point[1] for point in points]
            )
        amr_artist.set_offsets(poses)
        amr_artist.set_facecolors(colors)
        completed_index = bisect.bisect_right(completion_times, controller.time) - 1
        completed_lines = completed_line_totals[completed_index] if completed_index >= 0 else 0
        completed_tasks = bisect.bisect_right(task_completion_times, controller.time)
        elapsed = max(0.0, controller.time - controller.start_time)
        throughput = completed_lines * 3600.0 / elapsed if elapsed else 0.0
        status.set_text(
            "SIMULATION\n"
            f"Elapsed       {_format_elapsed(elapsed)}\n"
            f"Speed         {controller.speed:g}x\n\n"
            "CUMULATIVE OUTPUT\n"
            f"Tasks         {completed_tasks:,} / {result.metrics['completed_tasks']:,}\n"
            f"Order lines   {completed_lines:,} / {result.metrics['completed_lines']:,}\n"
            f"Throughput    {throughput:,.2f} lines/h"
        )
        return (rack_artist, amr_artist, status, *headings, *paths)

    animation = FuncAnimation(
        figure, draw, interval=50, cache_frame_data=False, blit=False
    )
    figure._amr_live_animation = animation
    plt.show()
