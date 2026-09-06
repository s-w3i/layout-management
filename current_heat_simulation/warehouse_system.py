"""Current-heat planner/allocator with native grid and store/day workload inputs."""

from pathlib import Path
import math
from time import perf_counter

from amr_simulation.models import SimulationConfig, WorkloadTask

from .astar_planner import AStarPlanner, PlannerConfig, WarehouseMap
from .conflict_detection import ConflictDetector
from .conflict_resolution import ConflictResolver
from .periodic_allocator import PeriodicAllocator
from .plan_generation import MoveRobotPathPlanner
from .priority_scheduling import get_alloc_order
from .run_metrics import RunMetricsRecorder
from .sim_types import OverlayState
from .simulator import PygameE3DSimulator
from .task_scheduler import StoreDayScheduler
from .task_state_machine import TaskStateMachineManager


class WarehouseSystem:
    def __init__(
        self, config: dict, warehouse_map: WarehouseMap, tasks: list[WorkloadTask],
        racks: dict, mapping: dict[str, str], amr_config: SimulationConfig,
    ) -> None:
        self.config = config
        self.map = warehouse_map
        names = [f"AMR_{i+1:02d}" for i in range(amr_config.amr_count)]
        runtime = {
            **config,
            "robots": names,
            "initial_vertices": [f"{name}:{node}" for name, node in zip(names, amr_config.spawn_nodes)],
            "initial_heading_degrees": amr_config.initial_heading_degrees,
            "motion": {
                **config.get("motion", {}),
                "max_linear_speed_mps": amr_config.motion.max_linear_speed_mps,
                "linear_accel_mps2": amr_config.motion.linear_acceleration_mps2,
                "linear_decel_mps2": amr_config.motion.linear_acceleration_mps2,
                "max_turn_speed_radps": math.radians(amr_config.motion.max_angular_speed_degps),
            },
            "tasks": {
                "pickup_time_sec": amr_config.jack_up_seconds,
                "dropoff_wait_sec": amr_config.service_seconds,
                "return_time_sec": amr_config.jack_down_seconds,
            },
        }
        self.simulator = PygameE3DSimulator(runtime, warehouse_map)
        planner_config = PlannerConfig(**config.get("planner", {}))
        if not planner_config.use_directional_heat:
            raise ValueError("this package supports only current_heat; directional heat must be enabled")
        self.planner = AStarPlanner(warehouse_map, planner_config)
        self.allocator = PeriodicAllocator(
            warehouse_map=warehouse_map, allocator_cfg=config.get("allocator", {}), robot_names=names,
        )
        self.metrics = RunMetricsRecorder()
        self.plan_generation = MoveRobotPathPlanner(self.planner, self.allocator, warehouse_map, self.metrics)
        self.conflict_detector = ConflictDetector(**config.get("conflicts", {}))
        self.conflict_resolver = ConflictResolver(warehouse_map)
        self.task_scheduler = StoreDayScheduler(tasks, racks, mapping, amr_config, warehouse_map)
        self.task_state_machine = TaskStateMachineManager(runtime, warehouse_map)
        self.task_state_machine.seed_rack_positions({rack_id: rack_id for rack_id in racks})
        self.simulator.set_rack_positions(self.task_state_machine.rack_positions)
        self.sim_time_sec = 0.0
        self.run_summary = None
        self.progress_callback = None
        self.status_callback = None
        self._last_status_time = -1.0
        self.headless = False
        self.metrics.contexts = self.task_state_machine.contexts
        self.metrics.scheduler = self.task_scheduler
        self.metrics.allocator = self.allocator
        self.simulator.metrics = self.metrics

    @property
    def done(self) -> bool:
        return self.task_scheduler.done

    def _replan(self, name: str, snapshots: dict) -> None:
        self.plan_generation.replan_current_goal(
            robot_name=name, robot_snapshots=snapshots,
            carrying_rack=self.task_state_machine.carrying_rack_states(),
            occupied_shelves=self.task_state_machine.occupied_shelves,
        )

    def step(self, dt: float) -> None:
        if not math.isfinite(dt) or dt <= 0:
            raise ValueError("simulation step must be positive and finite")
        if self.done:
            return
        self.simulator.update(dt)
        self.sim_time_sec += dt
        if self.status_callback is not None and self.sim_time_sec - self._last_status_time >= 5.0:
            self._last_status_time = self.sim_time_sec
            self.status_callback(self.sim_time_sec)
        snapshots = self.simulator.snapshots()
        # Refresh before dispatch so new and replanned routes see current commitments.
        self.planner.set_committed_paths(self.allocator.full_paths())
        while True:
            request = self.task_scheduler.dispatch_next(
                snapshots, list(self.task_state_machine.available_robots()), self.sim_time_sec,
            )
            if request is None:
                break
            self.task_state_machine.start_execute_task(request, self.plan_generation)
            self.metrics.record_stage(request.task_id, "to_pickup", self.sim_time_sec)
        self.plan_generation.update(
            robot_snapshots=snapshots,
            carrying_rack=self.task_state_machine.carrying_rack_states(),
            occupied_shelves=self.task_state_machine.occupied_shelves,
        )
        previous = {name: (c.phase, c.assigned_task.task_id if c.assigned_task else None)
                    for name, c in self.task_state_machine.contexts.items()}
        self.task_state_machine.update(
            dt=dt, robot_snapshots=snapshots, allocator=self.allocator,
            planner_bridge=self.plan_generation,
        )
        for name, context in self.task_state_machine.contexts.items():
            phase, job_id = previous[name]
            if job_id and context.phase != phase:
                self.metrics.record_stage(job_id, context.phase, self.sim_time_sec)
        for completion in self.task_state_machine.drain_completed_tasks():
            completed_lines = self.task_scheduler.mark_completed(completion.task_id, self.sim_time_sec)
            if self.progress_callback is not None:
                for _ in range(completed_lines):
                    self.progress_callback(1)

        conflicts = self.conflict_detector.detect_and_log_conflicts(self.allocator.conflict_paths(snapshots))
        order = get_alloc_order(
            self.allocator.states, snapshots, self.task_state_machine.contexts,
            self.allocator.allocator_cfg["priority_strategy"],
        )
        self.allocator.set_conflict_resolutions(self.conflict_resolver.resolve(
            conflicts=conflicts, priority_order=order, robot_snapshots=snapshots,
            jack_states=self.task_state_machine.jack_states(), sim_time_sec=self.sim_time_sec,
        ))
        self.allocator.update(
            dt=dt, planner=self.planner, robot_snapshots=snapshots,
            task_contexts=self.task_state_machine.contexts,
            replan_callback=lambda name: self._replan(name, snapshots),
            return_replan_callback=lambda name: self._replan(name, snapshots),
        )
        self.simulator.set_window_paths(self.allocator.window_paths(snapshots))
        self.simulator.set_jack_states(self.task_state_machine.jack_states())
        if not self.headless:
            self.simulator.set_full_path_previews(self.allocator.full_paths())
            self.simulator.set_rack_positions(self.task_state_machine.rack_positions)
            self.simulator.set_reservations(self.allocator.global_reservations)
            self.simulator.set_overlay(OverlayState(
                sim_time_sec=self.sim_time_sec,
                pending_tasks=len(self.task_scheduler.tasks)-self.task_scheduler.completed_tasks,
                completed_tasks=self.task_scheduler.completed_tasks,
                active_robots=sum(c.phase != "idle" for c in self.task_state_machine.contexts.values()),
                reserved_nodes=len(self.allocator.global_reservations),
            ))

    def run(self, *, headless: bool, output: Path, max_seconds: float | None = None) -> dict:
        import pygame

        started = perf_counter()
        self.headless = headless
        sim = self.simulator.sim_cfg
        dt = float(sim["fixed_sim_step_sec"])
        limit = float(max_seconds if max_seconds is not None else sim.get("stop_at_sim_time_sec", 86400))
        if not math.isfinite(dt) or dt <= 0 or not math.isfinite(limit) or limit <= 0:
            raise ValueError("step size and duration must be positive and finite")
        reason = ""
        try:
            if headless:
                while not self.done and self.sim_time_sec < limit-1e-9:
                    self.step(min(dt, limit-self.sim_time_sec))
            else:
                screen, clock = self.simulator.make_screen()
                accumulator = 0.0
                while not self.done and self.sim_time_sec < limit-1e-9:
                    if any(event.type == pygame.QUIT for event in pygame.event.get()):
                        reason = "window_closed_before_completion"
                        break
                    frame_dt = min(clock.tick(int(sim["fps"]))/1000, float(sim["max_frame_dt_sec"]))
                    accumulator += frame_dt*float(sim["sim_time_scale"])
                    for _ in range(int(sim["max_sim_steps_per_frame"])):
                        if accumulator < dt or self.done or self.sim_time_sec >= limit-1e-9:
                            break
                        self.step(min(dt, limit-self.sim_time_sec))
                        accumulator -= dt
                    self.simulator.draw(screen)
                    pygame.display.flip()
            if not self.done and not reason:
                reason = "simulation_time_limit_with_unfinished_tasks"
        except KeyboardInterrupt:
            reason = "interrupted"
            raise
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            self.run_summary = self.metrics.finalize(
                output, self.task_scheduler, self.sim_time_sec, reason,
                self.simulator.safety_interventions, perf_counter()-started,
            )
            if not headless:
                pygame.quit()
        return self.run_summary
