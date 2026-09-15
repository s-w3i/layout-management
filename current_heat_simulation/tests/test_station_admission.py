from datetime import date
import tempfile
import unittest

from amr_simulation.models import Rack, WorkloadTask
from current_heat_simulation.sim_types import RobotSnapshot
from current_heat_simulation.task_scheduler import StoreDayScheduler
from current_heat_simulation.tests.test_current_heat import small_map, amr_config


class StationAdmissionTests(unittest.TestCase):
    def make_scheduler(self, directory, limit=2):
        warehouse = small_map(directory)
        warehouse.workstations['WS2'] = 'G6_3'
        tasks = [WorkloadTask(name, date(2023,1,3), 0, name, lines)
                 for name, lines in [('A',{'x':1,'y':1,'z':1}), ('B',{'w':1}), ('C',{'v':1})]]
        racks = {f'G{i}_0':Rack(f'G{i}_0',(i,0),frozenset({sku}))
                 for i,sku in enumerate(['x','y','z','w','v'])}
        return StoreDayScheduler(tasks,racks,{'A':'WS','B':'WS','C':'WS2'},
                                 amr_config(),warehouse,limit)

    def dispatch(self, scheduler):
        name = f'robot{len(scheduler.jobs)}'
        return scheduler.dispatch_next({name:RobotSnapshot(name,'G6_2',6,2,0,0,False)},[name],0)

    def test_balances_stations_caps_jobs_and_locks_store_until_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            scheduler = self.make_scheduler(directory)
            first = self.dispatch(scheduler)
            second = self.dispatch(scheduler)
            self.assertEqual(scheduler.jobs[first.task_id].task.source.store_id,'A')
            self.assertEqual(scheduler.jobs[second.task_id].task.source.store_id,'C')
            third = self.dispatch(scheduler)
            self.assertEqual(scheduler.jobs[third.task_id].task.source.store_id,'A')
            self.assertIsNone(self.dispatch(scheduler))  # WS capped, WS2 already supplied.
            scheduler.mark_station_released(first.task_id,10)
            fourth = self.dispatch(scheduler)
            self.assertEqual(scheduler.jobs[fourth.task_id].task.source.store_id,'A')
            scheduler.mark_station_released(third.task_id,11)
            self.assertIsNone(self.dispatch(scheduler))  # A still at the station; cannot start B.
            scheduler.mark_station_released(fourth.task_id,12)
            fifth = self.dispatch(scheduler)
            self.assertEqual(scheduler.jobs[fifth.task_id].task.source.store_id,'B')
            self.assertEqual(scheduler.tasks[0].completed_lines,0)  # Returns still pending.
            self.assertIsNone(scheduler.tasks[0].completed_at)
            for request in [first,second,third,fourth,fifth]:
                scheduler.mark_completed(request.task_id,20)
            self.assertTrue(scheduler.done)
            self.assertEqual(sum(t.completed_lines for t in scheduler.tasks),5)
            self.assertTrue(all(t.station_inflight==0 for t in scheduler.tasks))

    def test_blocked_station_does_not_prevent_other_station_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            scheduler = self.make_scheduler(directory,1)
            scheduler.reserved_racks.update(['G0_0','G1_0','G2_0'])
            job = self.dispatch(scheduler)
            self.assertEqual(scheduler.jobs[job.task_id].task.source.store_id,'C')
            self.assertIsNone(self.dispatch(scheduler))  # B cannot jump ahead of blocked A.

    def test_invalid_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            for limit in [0,-1,1.5,True]:
                with self.assertRaises(ValueError):
                    self.make_scheduler(directory,limit)


if __name__ == '__main__':
    unittest.main()
