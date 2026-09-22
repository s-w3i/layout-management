"""Analytical trajectories and deterministic independent-crane event schedules."""

from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass
import math

from .inputs import sku_index


@dataclass(frozen=True)
class AxisMotion:
    speed: float
    acceleration: float

    def duration(self, distance):
        d = abs(distance)
        if d <= self.speed**2/self.acceleration:
            return 2*math.sqrt(d/self.acceleration)
        return d/self.speed+self.speed/self.acceleration

    def distance_at(self, distance, seconds):
        d = abs(distance)
        total = self.duration(d)
        t = max(0.0, min(seconds, total))
        ramp = min(self.speed/self.acceleration, total/2)
        if t <= ramp:
            return .5*self.acceleration*t*t
        if t >= total-ramp:
            return d-.5*self.acceleration*(total-t)**2
        return .5*self.acceleration*ramp*ramp+self.acceleration*ramp*(t-ramp)


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    origin: tuple[float, float, float]
    target: tuple[float, float, float]
    stage: str
    loaded: bool
    job_id: str

    def position(self, now, axes):
        return tuple(a+math.copysign(m.distance_at(b-a, now-self.start), b-a)
                     for a, b, m in zip(self.origin, self.target, axes))


class Schedule:
    """Build once; both playback and metrics consume the exact same phase boundaries.

    With one exclusive crane/station per aisle, no resource conflicts can alter a
    scheduled phase. A sorted event array is sufficient; no time-step physics loop.
    """

    def __init__(self, config, cranes, totes, tasks):
        self.config, self.cranes = config, {c.id: c for c in cranes}
        self.tasks = sorted(tasks, key=lambda t: (t.task_date, t.store_id, t.task_id))
        self.axes = tuple(AxisMotion(config['motion'][a]['speed_mps'],
                                     config['motion'][a]['acceleration_mps2']) for a in 'xyz')
        self.segments = {c.id: [] for c in cranes}
        self.jobs, self.allocation = [], []
        self.now = 0.0
        positions = {c.id: c.home for c in cranes}
        ends = {c.id: 0.0 for c in cranes}
        candidates = sku_index(totes)
        for task in self.tasks:
            batches = defaultdict(list)
            estimated_ends, estimated_positions = dict(ends), dict(positions)
            for sku, lines in sorted(task.line_counts.items()):
                if sku not in candidates:
                    raise ValueError(f'{task.task_id}: missing SKU {sku}')
                tote = min(candidates[sku], key=lambda t: (
                    estimated_ends[t.crane]+self.cycle_time(estimated_positions[t.crane], t),
                    t.crane, t.address))
                estimated_ends[tote.crane] += self.cycle_time(estimated_positions[tote.crane], tote)
                estimated_positions[tote.crane] = self.pickup_position(tote)
                batches[tote.crane].append((tote, lines))
            for name, batch in sorted(batches.items()):
                # ponytail: nearest-next O(batch²); replace with a spatial index only for very large store batches.
                while batch:
                    tote, lines = min(batch, key=lambda item: (
                        self.delivery_time(positions[name], item[0]), item[0].address))
                    batch.remove((tote, lines))
                    job = self._job(task, tote, lines, positions[name], ends[name])
                    self.jobs.append(job)
                    self.allocation.append({k: job[k] for k in (
                        'task_id', 'store_id', 'sku', 'covered_lines', 'crane', 'workstation', 'tote_id', 'slot_address')})
                    ends[name], positions[name] = job['return_time_s'], self.pickup_position(tote)
        self.makespan = max(ends.values(), default=0.0)
        self.events = sorted({s.end for segments in self.segments.values() for s in segments} | {self.makespan})
        self._starts = {name: [s.start for s in segments] for name, segments in self.segments.items()}
        self.jobs_by_id = {j['job_id']: j for j in self.jobs}
        self._completed = sorted((j['return_time_s'], j['covered_lines']) for j in self.jobs)
        self._completion_times = [t for t, _ in self._completed]
        self._line_prefix = [0]
        for _, lines in self._completed:
            self._line_prefix.append(self._line_prefix[-1]+lines)

    def travel_time(self, origin, target):
        return max(m.duration(b-a) for a, b, m in zip(origin, target, self.axes))

    def pickup_position(self, tote):
        return (tote.position[0], self.cranes[tote.crane].home[1], tote.position[2])

    def fork_time(self, tote):
        return self.axes[1].duration(tote.position[1]-self.cranes[tote.crane].home[1])

    def delivery_time(self, origin, tote):
        point = self.pickup_position(tote)
        return (self.travel_time(origin, point)+2*self.fork_time(tote)
                +self.config['timing']['extraction_dwell_seconds']
                +self.travel_time(point, self.cranes[tote.crane].home))

    def cycle_time(self, origin, tote):
        return (self.delivery_time(origin, tote)+self.config['timing']['picking_seconds']
                +self.travel_time(self.cranes[tote.crane].home, self.pickup_position(tote))
                +2*self.fork_time(tote)+self.config['timing']['replacement_dwell_seconds'])

    def _job(self, task, tote, lines, origin, start):
        crane, point, cfg = self.cranes[tote.crane], self.pickup_position(tote), self.config['timing']
        jid = f'{task.task_id}/{crane.id}/{tote.address}'
        now, position = start, origin
        stages = {}

        def add(stage, target, loaded, dwell=None):
            nonlocal now, position
            duration = self.travel_time(position, target) if dwell is None else dwell
            segment = Segment(now, now+duration, position, target, stage, loaded, jid)
            if duration > 0:
                self.segments[crane.id].append(segment)
            now, position = segment.end, target
            stages[stage+'_end_s'] = now

        add('to_pickup', point, False)
        add('extract_extend', tote.position, False)
        add('extraction_dwell', tote.position, False, cfg['extraction_dwell_seconds'])
        add('extract_retract', point, True)
        add('to_workstation', crane.home, True)
        add('picking', crane.home, True, cfg['picking_seconds'])
        add('to_return', point, True)
        add('replace_extend', tote.position, True)
        add('replacement_dwell', tote.position, True, cfg['replacement_dwell_seconds'])
        add('replace_retract', point, False)
        return {'job_id': jid, 'task_id': task.task_id, 'store_id': task.store_id,
                'sku': tote.sku, 'covered_lines': lines, 'crane': crane.id,
                'workstation': crane.workstation, 'tote_id': tote.id, 'slot_address': tote.address,
                'rack_id': tote.rack, 'level': tote.level, 'slot': tote.slot,
                'rack_side': 'left' if tote.position[1] < crane.home[1] else 'right',
                'slot_x_m': tote.position[0], 'slot_y_m': tote.position[1], 'slot_z_m': tote.position[2],
                'origin_x_m': origin[0], 'origin_z_m': origin[2],
                'dispatch_time_s': start, 'return_time_s': now, **stages}

    def advance(self, seconds):
        if not math.isfinite(seconds) or seconds < self.now:
            raise ValueError('simulation time must be finite and monotonic')
        self.now = min(seconds, self.makespan)

    def run(self, limit, progress=None):
        end = min(limit, self.makespan)
        # At most about 100 updates per run; progress must not dominate fast DES execution.
        interval = max((end-self.now)/100, 1e-9)
        next_update = self.now+interval
        if progress is not None:
            progress(self.completed_lines(), self.now)
        for event in self.events[bisect_right(self.events, self.now):]:
            if event > end:
                break
            self.advance(event)
            if progress is not None and self.now >= next_update:
                progress(self.completed_lines(), self.now)
                next_update = self.now+interval
        self.advance(end)
        if progress is not None:
            progress(self.completed_lines(), self.now)

    def completed_lines(self, now=None):
        return self._line_prefix[bisect_right(self._completion_times, self.now if now is None else now)]

    def state(self, name, now=None):
        at = self.now if now is None else now
        segments = self.segments[name]
        i = bisect_right(self._starts[name], at)-1
        if i < 0:
            return {'position': self.cranes[name].home, 'stage': 'idle', 'loaded': False, 'job': None}
        segment = segments[i]
        if at >= segment.end:
            return {'position': segment.target, 'stage': 'idle', 'loaded': False, 'job': None}
        return {'position': segment.position(at, self.axes), 'stage': segment.stage,
                'loaded': segment.loaded, 'job': self.jobs_by_id[segment.job_id]}
