"""Replay the recorded stationary fleet through the real motion/allocator code."""

from datetime import date
import json
import math
from pathlib import Path
import unittest

import yaml

from amr_simulation.models import Rack, SimulationConfig, WorkloadTask
from current_heat_simulation.astar_planner import WarehouseMap
from current_heat_simulation.sim_types import ExecuteTask
from current_heat_simulation.warehouse_system import WarehouseSystem

ROOT = Path(__file__).resolve().parents[2]


class DeadlockRecoveryTests(unittest.TestCase):
    def make_stalled_system(self, fixture='stalled_zone_off.json'):
        config = yaml.safe_load((ROOT/'current_heat_simulation/current_heat.yaml').read_text())
        amr = SimulationConfig.load(ROOT/'amr_simulation/config/default.json')
        warehouse = WarehouseMap(ROOT/'resources/map/map1_1.grid.json')
        rack = 'G3_9'
        task = WorkloadTask('test', date(2023, 1, 3), 0, 'store', {'sku': 1})
        system = WarehouseSystem(config, warehouse, [task],
            {rack: Rack(rack, (3, 9), frozenset({'sku'}))}, {'store': amr.workstations[0]}, amr)
        rows = json.loads((Path(__file__).parent/'fixtures'/fixture).read_text())['robots']
        for robot, row in zip(system.simulator.robots, rows):
            self.assertEqual(robot.name, row['robot'])
            robot.current_vertex = row['node']
            robot.x, robot.y, robot.heading = row['x'], row['y'], row['heading']
            robot.jack_up = row['loaded']
            context = system.task_state_machine.contexts[robot.name]
            context.phase, context.last_goal, context.carrying_rack = row['phase'], row['goal'], row['loaded']
            if row['phase'] != 'idle':
                context.assigned_task = ExecuteTask(robot.name, robot.name, rack, amr.workstations[0], rack)
                system.plan_generation.current_tasks[robot.name] = robot.name
                system.plan_generation.current_goals[robot.name] = row['goal']
            system.allocator.set_full_path(robot.name, row['full_path'] or [row['node']], row['goal'])
            system.allocator.mutex_passage.update_move_buffer(robot.name, row['node'])
            system.allocator.global_reservations[row['node']] = robot.name
            if row.get('window_remaining'):
                window = [row['node']] + row['window_remaining']
                robot.active_window_path, robot.path_index = window, 1
                for node in row['window_remaining']:
                    system.allocator.mutex_passage.update_move_buffer(robot.name, node)
                    system.allocator.global_reservations[node] = robot.name
                system.allocator.states[robot.name].current_index = row['allocation_index']
        return system

    def test_recorded_stall_yields_and_preserves_original_task(self):
        system = self.make_stalled_system()
        recovery, allocator = system.deadlock_recovery, system.allocator
        snapshots = system.simulator.snapshots()
        recovery.update(system, snapshots)
        system.sim_time_sec = allocator.sim_time_sec = 31
        recovery.update(system, snapshots)
        self.assertIsNotNone(recovery.active)
        name, refuge, _ = recovery.active
        context = system.task_state_machine.contexts[name]
        original_goal, original_task, phase = context.last_goal, context.assigned_task, context.phase
        path = allocator.states[name].full_path[:]
        self.assertLessEqual(len(path), 7)
        self.assertNotEqual(refuge, original_goal)
        occupied = {r.current_vertex for n, r in snapshots.items() if n != name}
        self.assertFalse(set(path[1:]) & occupied)
        self.assertTrue(all(allocator.global_reservations[n] == name for n in path))
        # Repeated conflict reports must not erase the protected yielding route.
        for _ in range(5):
            system._replan(name, snapshots)
        self.assertEqual(allocator.states[name].full_path, path)
        system.simulator.set_window_paths(allocator.window_paths(snapshots))
        for _ in range(1200):
            system.simulator.update(.05)
            system.sim_time_sec += .05
            snapshots = system.simulator.snapshots()
            yielding = snapshots[name]
            self.assertTrue(all(math.dist((yielding.x, yielding.y), (other.x, other.y)) >= .35
                                for other_name, other in snapshots.items() if other_name != name))
            allocator.update(dt=.05, planner=system.planner, robot_snapshots=snapshots,
                task_contexts=system.task_state_machine.contexts,
                replan_callback=lambda n: system._replan(n, snapshots))
            recovery.update(system, snapshots)
            system.simulator.set_window_paths(allocator.window_paths(snapshots))
            if recovery.active is None:
                break
        self.assertEqual(recovery.last_event['event'], 'refuge_reached')
        self.assertEqual(snapshots[name].current_vertex, refuge)
        self.assertIs(context.assigned_task, original_task)
        self.assertEqual((context.last_goal, context.phase), (original_goal, phase))
        self.assertNotIn(name, allocator.recovery_robots)
        self.assertEqual(system.plan_generation.pending_replans[name][0], original_goal)

    def test_no_refuge_does_not_clear_routes_or_reservations(self):
        system = self.make_stalled_system()
        # Every other vertex is reserved; no legal escape may be invented.
        robot = system.simulator.robots[0]
        allocator = system.allocator
        allocator.global_reservations = {v.name: 'other' for v in system.map.vertices}
        allocator.global_reservations[robot.current_vertex] = robot.name
        before = dict(allocator.global_reservations)
        path = allocator.states[robot.name].full_path[:]
        self.assertIsNone(system.deadlock_recovery.refuge_path(system, system.simulator.snapshots(), robot.name))
        self.assertEqual(allocator.global_reservations, before)
        self.assertEqual(allocator.states[robot.name].full_path, path)

    def test_stationary_queue_does_not_trigger_while_fleet_moves(self):
        system = self.make_stalled_system()
        recovery = system.deadlock_recovery
        recovery.update(system, system.simulator.snapshots())
        system.sim_time_sec = 31
        system.simulator.robots[1].x += .1
        recovery.update(system, system.simulator.snapshots())
        self.assertIsNone(recovery.active)
        self.assertEqual(system.allocator.recovery_robots, set())


if __name__ == '__main__':
    unittest.main()
