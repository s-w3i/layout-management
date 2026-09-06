"""Regression checks for the standard dram_ws allocator semantics."""
import tempfile
import unittest
from unittest.mock import Mock

from current_heat_simulation.astar_planner import AStarPlanner
from current_heat_simulation.periodic_allocator import PeriodicAllocator
from current_heat_simulation.plan_generation import MoveRobotPathPlanner
from current_heat_simulation.priority_scheduling import get_alloc_order
from current_heat_simulation.sim_types import RobotAllocationState, RobotSnapshot, RobotTaskContext
from current_heat_simulation.tests.test_current_heat import small_map


class DramAlignmentTests(unittest.TestCase):
    def test_timeout_replans_only_empty_non_followers(self):
        with tempfile.TemporaryDirectory() as directory:
            warehouse = small_map(directory)
            for following, loaded, expected in [(True, False, 0), (False, True, 0), (False, False, 1)]:
                with self.subTest(following=following, loaded=loaded):
                    allocator = PeriodicAllocator(warehouse_map=warehouse, allocator_cfg={}, robot_names=['a', 'b'])
                    allocator.set_full_path('a', ['G1_1', 'G2_1', 'G3_1'], 'G3_1')
                    allocator.set_full_path('b', ['G2_1', 'G3_1' if following else 'G2_2'], None)
                    allocator.states['a'].current_index = 1
                    allocator.states['a'].last_arrival_time = 1
                    allocator.states['a'].conflict_start_times['G2_1'] = 1
                    allocator.mutex_passage.update_move_buffer('a', 'G1_1')
                    allocator.global_reservations['G2_1'] = 'b'
                    allocator.sim_time_sec = 10
                    callback = Mock()
                    allocator._allocate_robot_window(
                        robot_name='a', planner=AStarPlanner(warehouse),
                        robot_snapshots={'a': RobotSnapshot('a', 'G1_1', 1, 1, 0, 0, loaded)},
                        task_contexts={'a': RobotTaskContext(carrying_rack=loaded)},
                        replan_callback=callback)
                    self.assertEqual(callback.call_count, expected)

    def test_replan_clears_then_delivers_response_from_tail(self):
        with tempfile.TemporaryDirectory() as directory:
            warehouse = small_map(directory)
            planner = AStarPlanner(warehouse)
            allocator = PeriodicAllocator(warehouse_map=warehouse, allocator_cfg={}, robot_names=['a'])
            allocator.set_full_path('a', ['G1_1', 'G2_1', 'G3_1', 'G4_1'], 'G4_1')
            for node in ['G1_1', 'G2_1']:
                allocator.mutex_passage.update_move_buffer('a', node)
                allocator.global_reservations[node] = 'a'
            allocator.states['a'].current_index = 2
            bridge = MoveRobotPathPlanner(planner, allocator, warehouse)
            bridge.current_tasks['a'] = 'job'
            bridge.current_goals['a'] = 'G4_1'
            snapshot = RobotSnapshot('a', 'G1_1', 1, 1, 0, 0, False)
            allocator.sim_time_sec = 10
            def replan(name):
                self.assertEqual(allocator.tabu_sets[name], {'G2_1->G3_1'})
                bridge.replan_current_goal(robot_name=name, robot_snapshots={'a': snapshot},
                                           carrying_rack={'a': False}, occupied_shelves=set())
            allocator._trigger_blocked_replan(
                robot_name='a', state=allocator.states['a'], snapshot=snapshot,
                blocked_node='G3_1', planner=planner, task_context=RobotTaskContext(),
                replan_callback=replan, return_replan_callback=None)
            self.assertEqual(allocator.states['a'].full_path, [])
            self.assertEqual(allocator.window_paths({'a': snapshot})['a'], [])
            bridge.update(robot_snapshots={'a': snapshot}, carrying_rack={'a': False}, occupied_shelves=set())
            path = allocator.states['a'].full_path
            self.assertEqual(path[0], 'G2_1')
            self.assertNotIn('a', allocator.pending_replans)
            self.assertEqual(path[-1], 'G4_1')
            self.assertNotIn(('G2_1', 'G3_1'), list(zip(path, path[1:])))
            self.assertEqual(allocator.global_reservations['G2_1'], 'a')

    def test_priority_uses_full_path_then_numeric_priority_and_name(self):
        states = {'a': RobotAllocationState(full_path=['x']*4, current_index=3),
                  'b': RobotAllocationState(full_path=['x']*3),
                  'c': RobotAllocationState(full_path=['x']*3, priority=2)}
        self.assertEqual(get_alloc_order(states, {}, {}, 'ascPathLength'), ['c', 'b', 'a'])


class DirectReferenceTests(unittest.TestCase):
    def test_heat_matches_dram_blending_and_smoothing(self):
        import ast
        from pathlib import Path
        from current_heat_simulation.directional_cost_layer import DirectionalCostLayer
        from current_heat_simulation.astar_planner import PlannerConfig
        source = Path('/home/usern/dram_ws/src/dram_viz/dram_viz/directional_cost_layer.py')
        if not source.exists():
            self.skipTest('dram_ws reference checkout unavailable')
        tree = ast.parse(source.read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef))
        method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == '_blend_directional_costs')
        ns = {}
        exec('from __future__ import annotations\n'+ast.unparse(method), ns)
        with tempfile.TemporaryDirectory() as directory:
            warehouse = small_map(directory)
            layer = DirectionalCostLayer(warehouse, PlannerConfig())
            planner = AStarPlanner(warehouse)
            layer.set_base_costs({'G1_1->G2_1': 3})
            layer.path_changed('a', ['G1_1','G2_1'])
            layer.update(4, planner)
            self.assertEqual(planner.directional_heat_costs, {})
            layer.update(1, planner)
            key = layer._canonical_orientation[('G1_1','G2_1')]
            old = layer._directional_cache[key]
            expected = ns['_blend_directional_costs'](layer, key, 3, 1, 1)
            layer.path_changed('b', ['G2_1','G1_1'])
            layer.update(5, planner)
            self.assertEqual(layer._directional_cache[key], expected)
            self.assertNotEqual(old, expected)
            a, b = [warehouse.name_to_index[x] for x in key]
            self.assertEqual(planner._edge_cost(a, b, 1), 1 + expected[0])

    def test_all_priority_strategies_match_dram(self):
        import runpy
        from pathlib import Path
        from types import SimpleNamespace
        from collections import deque
        source = Path('/home/usern/dram_ws/src/dram_plan/dram_plan/priority_scheduling.py')
        if not source.exists():
            self.skipTest('dram_ws reference checkout unavailable')
        reference = runpy.run_path(str(source))['get_alloc_order']
        with tempfile.TemporaryDirectory() as directory:
            warehouse = small_map(directory)
            paths = {'a': ['G1_1','G2_1','G3_1'], 'b': ['G1_1','G1_2'], 'c': ['G2_2','G2_3']}
            states = {n: RobotAllocationState(full_path=p, current_index=1, priority=i,
                        last_reservation_time=float(i)) for i,(n,p) in enumerate(paths.items())}
            snapshots = {n: RobotSnapshot(n,p[0],*warehouse.point(p[0]),0,0,n=='b') for n,p in paths.items()}
            buffers = {n: deque(p[:i+1]) for i,(n,p) in enumerate(paths.items())}
            robots = {n: SimpleNamespace(full_path=[(x,*warehouse.point(x)) for x in p],
                      current_index=1, priority=states[n].priority, move_buffer=buffers[n],
                      last_reservation_time=states[n].last_reservation_time,
                      last_state=(snapshots[n].x,snapshots[n].y), jack_up_state=snapshots[n].jack_up)
                      for n,p in paths.items()}
            for field in ['MoveBufferSize','LastReservationTime','NextMoveCost','PathLength','PathCost']:
                for direction in ['asc','desc']:
                    strategy = direction+field
                    self.assertEqual(get_alloc_order(states,snapshots,{},strategy,warehouse,buffers),
                                     reference(robots,strategy), strategy)

    def test_wait_falls_through_and_replan_requires_drained_buffer(self):
        from collections import deque
        with tempfile.TemporaryDirectory() as directory:
            warehouse = small_map(directory)
            for resolution, buffer, expected in [('wait',['G1_1'],False),
                                                  ('replan',['G1_1','G2_1'],False),
                                                  ('replan',['G1_1'],True)]:
                allocator = PeriodicAllocator(warehouse_map=warehouse, allocator_cfg={}, robot_names=['a'])
                allocator.set_full_path('a',['G1_1','G2_1','G3_1'], 'G3_1')
                allocator.mutex_passage.robots_move_buffer['a'] = deque(buffer)
                allocator.states['a'].current_index = len(buffer)
                allocator.set_conflict_resolutions({'a':resolution})
                callback = Mock()
                allocator._allocate_robot_window(robot_name='a', planner=AStarPlanner(warehouse),
                    robot_snapshots={'a':RobotSnapshot('a','G1_1',1,1,0,0,False)},
                    task_contexts={'a':RobotTaskContext()}, replan_callback=callback)
                self.assertEqual(callback.called,expected)
                if not expected:
                    self.assertEqual(allocator.global_reservations['G3_1'],'a')

    def test_failed_replan_retries_on_later_tick_without_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            warehouse = small_map(directory)
            allocator = PeriodicAllocator(warehouse_map=warehouse, allocator_cfg={}, robot_names=['a'])
            planner = AStarPlanner(warehouse)
            bridge = MoveRobotPathPlanner(planner, allocator, warehouse)
            bridge.current_tasks['a'] = 'job'
            bridge.current_goals['a'] = 'G3_1'
            snapshot = RobotSnapshot('a','G1_1',1,1,0,0,False)
            planner.plan = Mock(side_effect=[ValueError('blocked'), ['G1_1','G2_1','G3_1']])
            allocator.tabu_sets['a'] = {'G1_1->G2_1'}
            bridge.replan_current_goal(robot_name='a',robot_snapshots={'a':snapshot},
                                       carrying_rack={'a':False},occupied_shelves=set())
            for attempt in range(2):
                bridge.update(robot_snapshots={'a':snapshot},carrying_rack={'a':False},occupied_shelves=set())
                self.assertEqual(planner.plan.call_count,attempt+1)
                if attempt == 0:
                    self.assertIn('a',bridge.pending_replans)
                    self.assertNotIn('a',allocator.tabu_sets)
                    self.assertEqual(allocator.states['a'].full_path,[])
            self.assertNotIn('a',allocator.pending_replans)
            self.assertEqual(allocator.states['a'].full_path[-1],'G3_1')

    def test_conflict_replan_publishes_only_its_conflict_edge(self):
        from current_heat_simulation.conflict_resolution import ConflictResolver
        with tempfile.TemporaryDirectory() as directory:
            warehouse = small_map(directory)
            resolver = ConflictResolver(warehouse)
            resolver._select_robot_to_replan = Mock(return_value='a')
            snapshots = {'a': RobotSnapshot('a','G1_1',1,1,0,0,False),
                         'b': RobotSnapshot('b','G2_1',2,1,0,0,False)}
            conflicts = [{'type':'head_to_head','robots':[
                {'robot':'a','from':'G1_1','to':'G2_1','step':1},
                {'robot':'b','from':'G2_1','to':'G1_1','step':1}]}]
            # A later overlap wait must not overwrite an earlier replan.
            conflicts.append({'type':'path_overlap','robots':conflicts[0]['robots']})
            actions = resolver.resolve(conflicts=conflicts,priority_order=['b','a'],
                robot_snapshots=snapshots,jack_states={'a':False,'b':False},sim_time_sec=10)
            self.assertEqual(actions['a'],'replan')
            self.assertEqual(resolver.replan_tabu_edges,{'a':{'G1_1->G2_1'}})
            planner = AStarPlanner(warehouse)
            planner.set_tabu_edges(resolver.replan_tabu_edges)
            path = planner.plan('G1_1','G3_1',False,robot_name='a')
            self.assertNotIn(('G1_1','G2_1'),list(zip(path,path[1:])))
            resolver.resolve(conflicts=[],priority_order=['b','a'],robot_snapshots=snapshots,
                jack_states={'a':False,'b':False},sim_time_sec=11)
            self.assertEqual(resolver.replan_tabu_edges,{})


if __name__ == "__main__":
    unittest.main()
