"""Run with: python3 -m unittest discover -s asrs_simulation/tests -v."""

import copy
from datetime import date
import json
import os
from pathlib import Path
import tempfile
import unittest

from amr_simulation.models import WorkloadTask
from asrs_simulation.engine import AxisMotion, Schedule
from asrs_simulation.inputs import Crane, Tote, load_config, load_layout, load_map
from asrs_simulation.reporting import aggregate, metrics, save_run, write_json
from asrs_simulation.run_simulation import REQUIRED_FILES, check_resume, file_hash, read_checkpoint

ROOT = Path(__file__).resolve().parents[2]


def fixture():
    # Synthetic physics checks must be independent of the user's editable YAML.
    config = {'motion': {'x': {'speed_mps': 3., 'acceleration_mps2': 1.},
                         'y': {'speed_mps': .5, 'acceleration_mps2': .5},
                         'z': {'speed_mps': 1., 'acceleration_mps2': .5}},
              'timing': {'picking_seconds': 4., 'extraction_dwell_seconds': 0., 'replacement_dwell_seconds': 0.}}
    cranes = [Crane('A', 'WS_A', (0, 2), (10., 2., .5)), Crane('B', 'WS_B', (3, 5), (10., 8., .5))]
    totes = [Tote('T1', 's1', 'B-G2_0/L01/S01', 'G2_0', 1, 1, 'A', (4., 0., 1.5)),
             Tote('T2', 's2', 'B-G3_2/L01/S01', 'G3_2', 1, 1, 'A', (6., 4., 2.5)),
             Tote('T3', 's3', 'B-G2_3/L01/S01', 'G2_3', 1, 1, 'B', (4., 6., 1.5))]
    return config, cranes, totes


def task(store, counts):
    return WorkloadTask(f'2023-01-03/{store}', date(2023, 1, 3), 0.0, store, counts)


class ASRSTests(unittest.TestCase):
    def test_motion_analytical_profiles(self):
        motion = AxisMotion(3, 1)
        self.assertEqual(motion.duration(0), 0)
        self.assertEqual(motion.duration(1), 2)
        self.assertEqual(motion.duration(-30), 13)
        self.assertEqual(motion.distance_at(1, 1), .5)
        self.assertEqual(motion.distance_at(30, 3), 4.5)
        self.assertEqual(motion.distance_at(30, 10), 25.5)
        self.assertEqual(motion.distance_at(30, 13), 30)
        self.assertEqual(motion.distance_at(30, -1), 0)
        self.assertEqual(motion.distance_at(0, 100), 0)
        cfg, cranes, totes = fixture()
        schedule = Schedule(cfg, cranes, totes, [task('1', {'s1': 3})])
        self.assertEqual(schedule.travel_time((0, 0, 0), (30, 0, 9)), 13)
        for motion in schedule.axes:
            for distance in (0, .01, 1, 2, 10, 100):
                total = motion.duration(distance)
                self.assertAlmostEqual(motion.distance_at(distance, total), distance)
                self.assertAlmostEqual(motion.distance_at(distance, total/2), distance/2)

    def test_single_tote_cycle_and_picking_delay(self):
        cfg, cranes, totes = fixture()
        schedule = Schedule(cfg, cranes, totes, [task('1', {'s1': 5})])
        j = schedule.jobs[0]
        self.assertEqual(len(schedule.jobs), 1)
        self.assertEqual(j['picking_end_s']-j['to_workstation_end_s'], 4)
        self.assertAlmostEqual(schedule.makespan, schedule.cycle_time(cranes[0].home, totes[0]))
        schedule.advance(j['picking_end_s'])
        summary, _ = metrics(schedule)
        self.assertEqual(summary['tote_presentations'], 1)
        self.assertEqual(summary['completed_lines'], 0)
        schedule.run(86400)
        summary, tables = metrics(schedule)
        self.assertEqual(summary['completed_lines'], 5)
        self.assertEqual(summary['lines_per_completed_tote_trip'], 5)
        self.assertEqual(summary['picking_seconds'], 4)
        self.assertTrue(summary['success_flag'])
        self.assertEqual(schedule.state('A')['position'], schedule.pickup_position(totes[0]))
        for c in tables['cranes.csv']:
            self.assertAlmostEqual(c['busy_seconds']+c['idle_seconds'], schedule.now)

    def test_both_sides_and_fork_interlocks(self):
        cfg, cranes, totes = fixture()
        left = totes[0]
        right = Tote('R', 's1', 'R', 'G2_2', 1, 1, 'A', (4., 4., 1.5))
        a = Schedule(cfg, cranes, [left], [task('1', {'s1': 1})])
        b = Schedule(cfg, cranes, [right], [task('1', {'s1': 1})])
        self.assertAlmostEqual(a.makespan, b.makespan)
        self.assertEqual(a.jobs[0]['rack_side'], 'left')
        self.assertEqual(b.jobs[0]['rack_side'], 'right')
        for schedule in (a, b):
            for segment in schedule.segments['A']:
                if segment.stage in ('to_pickup', 'to_workstation', 'to_return'):
                    self.assertEqual(segment.origin[1], cranes[0].home[1])
                    self.assertEqual(segment.target[1], cranes[0].home[1])
                if segment.origin[1] != segment.target[1]:
                    self.assertEqual(segment.origin[0], segment.target[0])
                    self.assertEqual(segment.origin[2], segment.target[2])

    def test_direct_next_pickup_and_store_batches(self):
        cfg, cranes, totes = fixture()
        schedule = Schedule(cfg, cranes, totes, [task('1', {'s1': 2, 's2': 3}), task('2', {'s1': 7})])
        jobs = schedule.jobs
        self.assertEqual([j['store_id'] for j in jobs], ['1', '1', '2'])
        for previous, nxt in zip(jobs, jobs[1:]):
            self.assertEqual(nxt['dispatch_time_s'], previous['return_time_s'])
            self.assertEqual(nxt['origin_x_m'], previous['slot_x_m'])
            self.assertEqual(nxt['origin_z_m'], previous['slot_z_m'])
        self.assertNotEqual(jobs[1]['origin_x_m'], cranes[0].home[0])
        schedule.run(86400)
        self.assertEqual(schedule.completed_lines(), 12)

    def test_split_group_parallel_completion_and_duplicate_sku(self):
        cfg, cranes, totes = fixture()
        parallel = Schedule(cfg, cranes, totes, [task('1', {'s1': 4, 's3': 6})])
        self.assertEqual([j['dispatch_time_s'] for j in parallel.jobs], [0, 0])
        parallel.run(86400)
        summary, tables = metrics(parallel)
        self.assertEqual(tables['tasks.csv'][0]['cranes'], 'A|B')
        self.assertEqual(tables['tasks.csv'][0]['completion_time_s'], parallel.makespan)
        self.assertEqual(summary['completed_lines'], 10)
        duplicate = Tote('T4', 's1', 'B-G2_3/L01/S01', 'G2_3', 1, 1, 'B', (4., 6., 1.5))
        schedule = Schedule(cfg, cranes, [totes[0], duplicate], [task('1', {'s1': 4}), task('2', {'s1': 7})])
        self.assertEqual([j['crane'] for j in schedule.jobs], ['A', 'B'])
        self.assertEqual(len(schedule.allocation), 2)
        schedule.run(86400)
        self.assertEqual(schedule.completed_lines(), 11)
        self.assertEqual(schedule.allocation, Schedule(cfg, cranes, [duplicate, totes[0]], schedule.tasks).allocation)

    def test_time_limit_and_sampled_playback_agree(self):
        cfg, cranes, totes = fixture()
        tasks = [task('1', {'s1': 2, 's2': 3, 's3': 7})]
        headless, sampled = (Schedule(cfg, cranes, totes, tasks) for _ in range(2))
        end = headless.jobs[0]['extract_retract_end_s']+.7
        headless.run(end)
        while sampled.now < end:
            sampled.advance(min(sampled.now+.11, end))
        self.assertEqual(metrics(headless), metrics(sampled))
        self.assertFalse(metrics(headless)[0]['success_flag'])
        for name in headless.cranes:
            self.assertEqual(headless.state(name), sampled.state(name))
        headless.run(86400)
        sampled.advance(sampled.makespan)
        self.assertEqual(metrics(headless), metrics(sampled))
        with self.assertRaises(ValueError):
            sampled.advance(0)

    def test_progress_matches_completed_returns_without_changing_results(self):
        cfg, cranes, totes = fixture()
        tasks = [task('1', {'s1': 2, 's2': 3, 's3': 7})]
        for limit in (20, 86400):
            plain, observed = (Schedule(cfg, cranes, totes, tasks) for _ in range(2))
            updates = []
            plain.run(limit)
            observed.run(limit, lambda lines, seconds: updates.append((lines, seconds)))
            self.assertEqual(metrics(plain), metrics(observed))
            self.assertEqual(updates[0], (0, 0.0))
            self.assertEqual(updates[-1], (observed.completed_lines(), observed.now))
            self.assertLessEqual(len(updates), 103)
            self.assertEqual(updates, sorted(updates))
            for lines, seconds in updates:
                self.assertEqual(lines, sum(j['covered_lines'] for j in observed.jobs if j['return_time_s'] <= seconds))

    def test_headless_progress_multiple_dates_layouts_resume_and_cutoff(self):
        from contextlib import redirect_stdout, redirect_stderr
        from dataclasses import replace
        import io
        from unittest.mock import patch
        from tqdm import tqdm
        from asrs_simulation.run_simulation import execute_batch, run_day
        cfg, cranes, totes = fixture()
        first = task('1', {'s1': 2, 's2': 3})
        second = replace(first, task_id='2023-01-04/1', task_date=date(2023, 1, 4))
        shared = {'config': cfg, 'cranes': cranes, 'layouts': {name: {'totes': totes, 'strategy': name} for name in ('one', 'two')},
                  'days': {'2023-01-04': [second], '2023-01-03': [first]}, 'headless': True,
                  'limit': 86400, 'fingerprint': 'test'}
        bars, stream = [], io.StringIO()

        def make_bar(*args, **kwargs):
            bar = tqdm(*args, **kwargs, file=stream)
            bars.append(bar)
            return bar

        with tempfile.TemporaryDirectory() as td, redirect_stdout(stream), redirect_stderr(stream), \
                patch('asrs_simulation.run_simulation.tqdm', side_effect=make_bar):
            output = Path(td)
            with patch('asrs_simulation.run_simulation.run_day', wraps=run_day) as runner:
                results = execute_batch(shared, output, 1, False)
                self.assertEqual([(c.args[1], c.args[2]) for c in runner.call_args_list],
                                 [('one', '2023-01-03'), ('two', '2023-01-03'),
                                  ('one', '2023-01-04'), ('two', '2023-01-04')])
            self.assertEqual(len(results), 4)
            self.assertTrue(all(r['success_flag'] for r in results))
            self.assertTrue(all(b.n == b.total == 5 and 'completed' in b.postfix for b in bars))
            self.assertNotIn(': completed,', stream.getvalue())
            bars.clear()
            with patch('asrs_simulation.run_simulation.run_day') as runner:
                self.assertEqual(execute_batch(shared, output, 4, True), results)
                runner.assert_not_called()
            self.assertTrue(all(b.n == b.total == 5 and 'resumed' in b.postfix for b in bars))
            bars.clear()
            partial = {**shared, 'layouts': {'one': shared['layouts']['one']}, 'days': {'2023-01-03': [first]},
                       'limit': Schedule(cfg, cranes, totes, [first]).jobs[0]['return_time_s']+.5}
            result = execute_batch(partial, output/'partial', 1, False)[0]
            self.assertEqual(result['status'], 'incomplete')
            self.assertEqual(bars[0].n, result['completed_lines'])
            self.assertTrue(0 < bars[0].n < bars[0].total)
            self.assertIn('incomplete', bars[0].postfix)
            bars.clear()
            failed = {**partial, 'days': {'2023-01-03': [task('1', {'missing': 5})]}}
            result = execute_batch(failed, output/'failed', 1, False)[0]
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(bars[0].n, 0)
            self.assertIn('failed', bars[0].postfix)

    def test_config_and_real_map_layout_preflight(self):
        config = load_config(ROOT/'asrs_simulation/asrs.yaml')
        _, cranes, geometry, racks = load_map(ROOT/'resources/map/asrs/asrs_grid.grid.json', config)
        self.assertEqual(len(cranes), 9)
        self.assertEqual(len(geometry), 4536)
        for path in (ROOT/'resources/map/asrs').glob('*.slotting.json'):
            layout = load_layout(path, cranes, geometry, racks, [])
            self.assertTrue(layout['validation']['valid'], layout['validation']['errors'])
            self.assertEqual(layout['validation']['totes'], 3344)
            self.assertEqual(layout['validation']['skus'], 1524)
        path = ROOT/'resources/map/asrs/basic_zone_on_slotting_layout.slotting.json'
        bad = load_layout(path, cranes, geometry, racks, ['nonexistent'])
        self.assertFalse(bad['validation']['valid'])
        changed = copy.deepcopy(config)
        changed['aisles'][0]['rack_rows'] = [0, 3]
        with self.assertRaises(ValueError):
            load_map(ROOT/'resources/map/asrs/asrs_grid.grid.json', changed)
        with tempfile.TemporaryDirectory() as td:
            import yaml
            changed = copy.deepcopy(config)
            changed['motion']['x']['speed_mps'] = float('nan')
            p = Path(td)/'bad.yaml'
            p.write_text(yaml.safe_dump(changed))
            with self.assertRaises(ValueError):
                load_config(p)
            raw = json.loads(path.read_text())
            raw['assignments'][0]['center_x'] += .01
            p = Path(td)/'bad.slotting.json'
            p.write_text(json.dumps(raw))
            self.assertFalse(load_layout(p, cranes, geometry, racks, [])['validation']['valid'])

    def test_shared_workload_groups_lines_by_day_and_store(self):
        from amr_simulation.inputs import load_workload
        from openpyxl import Workbook
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)/'orders.xlsx'
            book = Workbook()
            sheet = book.active
            sheet.append(['Date', 'Store ID', 'Item or SKU'])
            for row in [('2023-01-03', 'shop', 's1'), ('2023-01-03', 'shop', 's1'),
                        ('2023-01-03', 'shop', 's2'), ('2023-01-04', 'shop', 's1')]:
                sheet.append(row)
            book.save(p)
            workload = load_workload(p, cache_dir=Path(td)/'cache')
            self.assertEqual(len(workload.tasks), 2)
            self.assertEqual(workload.tasks[0].line_counts, {'s1': 2, 's2': 1})
            self.assertEqual([t.release_seconds for t in workload.tasks], [0.0, 0.0])

    def test_reports_resume_and_paired_exclusions(self):
        cfg, cranes, totes = fixture()
        schedule = Schedule(cfg, cranes, totes, [task('1', {'s1': 5})])
        schedule.run(86400)
        with tempfile.TemporaryDirectory() as td:
            directory = Path(td)
            summary = save_run(directory, schedule, 1.0)
            write_json(directory/'checkpoint.json', {'fingerprint': 'ok', 'files': {f: file_hash(directory/f) for f in REQUIRED_FILES}})
            self.assertTrue(read_checkpoint(directory, 'ok')['success_flag'])
            self.assertIsNone(read_checkpoint(directory, 'wrong'))
            (directory/'cranes.csv').write_text('corrupted')
            self.assertIsNone(read_checkpoint(directory, 'ok'))
            manifest = directory/'manifest.json'
            write_json(manifest, {'fingerprint': 'ok'})
            check_resume(manifest, 'ok', True)
            with self.assertRaisesRegex(ValueError, 'fingerprint mismatch'):
                check_resume(manifest, 'changed', True)
            with self.assertRaises(ValueError):
                check_resume(manifest, 'ok', False)
        results = [{**summary, 'layout': name, 'date': day} for name in ('a', 'b') for day in ('d1', 'd2')]
        results[-1].update(success_flag=False, status='incomplete')
        summaries, paired, common = aggregate(results, ['a', 'b'], ['d1', 'd2'], 'a')
        self.assertEqual(common, ['d1'])
        self.assertEqual(len(paired), 2)
        self.assertTrue(all(s['comparison_status'] == 'provisional' for s in summaries))
        for result in results:
            result.update(success_flag=False, status='failed')
        summaries, _, common = aggregate(results, ['a', 'b'], ['d1', 'd2'], 'a')
        self.assertEqual(common, [])
        self.assertIsNone(summaries[0]['mean_daily_throughput_lines_per_hour'])

    def test_pygame_renderer_and_event_loop(self):
        os.environ['SDL_VIDEODRIVER'] = 'dummy'
        os.environ['SDL_AUDIODRIVER'] = 'dummy'
        from asrs_simulation.debugger import Renderer, play, pygame
        config = load_config(ROOT/'asrs_simulation/asrs.yaml')
        data, cranes, geometry, racks = load_map(ROOT/'resources/map/asrs/asrs_grid.grid.json', config)
        layout = load_layout(ROOT/'resources/map/asrs/basic_zone_on_slotting_layout.slotting.json', cranes, geometry, racks, [])
        tote = layout['totes'][0]
        schedule = Schedule(config, cranes, layout['totes'], [task('debug', {tote.sku: 3})])
        renderer = Renderer(schedule, data, layout['totes'])
        try:
            for index in range(6):
                renderer.selected = index
                renderer.draw()
            schedule.advance(schedule.jobs[0]['extract_retract_end_s']/2)
            renderer.draw(True, 10)
        finally:
            pygame.quit()
        reason = play(schedule, data, layout['totes'], 86400, max_frames=2)
        self.assertEqual(reason, 'window_closed_before_completion')


if __name__ == '__main__':
    unittest.main()
