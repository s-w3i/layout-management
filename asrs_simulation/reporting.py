"""Operational metrics and paired-day ASRS layout comparison reports."""

from collections import defaultdict
import csv
import json
from pathlib import Path
from statistics import mean, median, stdev


CATEGORIES = ('empty_travel', 'loaded_travel', 'fork', 'picking', 'dwell')
KPI_MEANS = ('makespan_hours', 'travel_metres_per_completed_line', 'lines_per_completed_tote_trip',
             'tote_trips_per_1000_lines', 'mean_store_completion_seconds', 'p95_store_completion_seconds',
             'crane_utilization', 'workstation_utilization', 'empty_distance_m', 'loaded_distance_m',
             'completed_order_lines_per_tote_presentation')


def ratio(n, d):
    return n/d if d else 0.0


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix+'.tmp')
    temp.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False)+'\n')
    temp.replace(path)


def write_csv(path, rows, fields=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = fields or list(dict.fromkeys(key for row in rows for key in row))
    with path.open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def percentile(values, fraction):
    if not values:
        return None
    values = sorted(values)
    index = (len(values)-1)*fraction
    lower = int(index)
    return values[lower]+(values[min(lower+1, len(values)-1)]-values[lower])*(index-lower)


def metrics(schedule, wall_seconds=0.0, failure_reason=None):
    now = schedule.now
    by_task, by_crane = defaultdict(list), defaultdict(list)
    for job in schedule.jobs:
        by_task[job['task_id']].append(job)
        by_crane[job['crane']].append(job)
    tasks, jobs, cranes, stations = [], [], [], []
    for task in schedule.tasks:
        assigned = by_task[task.task_id]
        completed = sum(j['covered_lines'] for j in assigned if j['return_time_s'] <= now)
        complete_at = max((j['return_time_s'] for j in assigned), default=0.0)
        start_at = min((j['dispatch_time_s'] for j in assigned), default=0.0)
        tasks.append({'task_id': task.task_id, 'store_id': task.store_id, 'date': task.task_date.isoformat(),
                      'release_time_s': 0.0, 'source_lines': task.source_lines, 'completed_lines': completed,
                      'unfinished_lines': task.source_lines-completed,
                      'start_time_s': start_at if start_at <= now else None,
                      'completion_time_s': complete_at if complete_at <= now else None,
                      'cranes': '|'.join(sorted({j['crane'] for j in assigned})),
                      'status': 'completed' if completed == task.source_lines else 'incomplete'})
    for job in schedule.jobs:
        if job['dispatch_time_s'] >= now:
            continue
        row = {k: v for k, v in job.items() if not k.endswith('_end_s') and k != 'return_time_s'}
        row.update({k: v if v <= now else None for k, v in job.items() if k.endswith('_end_s')})
        row.update(planned_return_time_s=job['return_time_s'],
                   completion_time_s=job['return_time_s'] if job['return_time_s'] <= now else None,
                   completed_lines=job['covered_lines'] if job['return_time_s'] <= now else 0,
                   status='completed' if job['return_time_s'] <= now else 'incomplete')
        jobs.append(row)
    for name, crane in schedule.cranes.items():
        counts = {key+'_seconds': 0.0 for key in CATEGORIES}
        counts.update({key+'_distance_m': 0.0 for key in ('empty', 'loaded', 'x', 'y', 'z')})
        counts.update({axis+'_motion_seconds': 0.0 for axis in 'xyz'})
        for segment in schedule.segments[name]:
            elapsed = max(0.0, min(now, segment.end)-segment.start)
            if elapsed <= 0:
                continue
            if segment.stage in ('to_pickup', 'to_workstation', 'to_return'):
                category = 'loaded_travel' if segment.loaded else 'empty_travel'
            elif segment.stage == 'picking':
                category = 'picking'
            elif segment.stage.endswith('dwell'):
                category = 'dwell'
            else:
                category = 'fork'
            counts[category+'_seconds'] += elapsed
            for axis, motion, a, b in zip('xyz', schedule.axes, segment.origin, segment.target):
                distance = motion.distance_at(b-a, elapsed)
                counts[axis+'_distance_m'] += distance
                counts[('loaded' if segment.loaded else 'empty')+'_distance_m'] += distance
                counts[axis+'_motion_seconds'] += min(elapsed, motion.duration(b-a))
        busy = sum(counts[c+'_seconds'] for c in CATEGORIES)
        finished = [j for j in by_crane[name] if j['return_time_s'] <= now]
        presented = [j for j in by_crane[name] if j['picking_end_s'] <= now]
        lines = sum(j['covered_lines'] for j in finished)
        cranes.append({'crane': name, 'workstation': crane.workstation, **counts,
                       'idle_seconds': max(0.0, now-busy), 'busy_seconds': busy,
                       'utilization': ratio(busy, now), 'completed_lines': lines,
                       'completed_tote_jobs': len(finished), 'tote_presentations': len(presented)})
        stations.append({'workstation': crane.workstation, 'crane': name,
                         'service_seconds': counts['picking_seconds'],
                         'utilization': ratio(counts['picking_seconds'], now),
                         'completed_lines': lines, 'tote_presentations': len(presented)})
    source_lines = sum(t.source_lines for t in schedule.tasks)
    completed_lines = schedule.completed_lines()
    completed_jobs = [j for j in schedule.jobs if j['return_time_s'] <= now]
    durations = [t['completion_time_s'] for t in tasks if t['status'] == 'completed']
    total = {key: sum(c[key] for c in cranes) for key in counts}
    travel = total['empty_distance_m']+total['loaded_distance_m']
    presentations = sum(c['tote_presentations'] for c in cranes)
    success = now >= schedule.makespan and not failure_reason
    reason = failure_reason or (None if success else 'simulation_time_limit_with_unfinished_tasks')
    summary = {'method': 'asrs', 'date': schedule.tasks[0].task_date.isoformat(),
               'status': 'completed' if success else 'incomplete', 'success_flag': success,
               'failure_reason': reason, 'sim_duration_s': now,
               'makespan_hours': now/3600 if success else None,
               'released_tasks': len(tasks), 'completed_tasks': len(durations),
               'unfinished_tasks': len(tasks)-len(durations), 'source_lines': source_lines,
               'completed_lines': completed_lines, 'unfinished_lines': source_lines-completed_lines,
               'completion_ratio': ratio(completed_lines, source_lines),
               'line_throughput_per_hour': ratio(completed_lines*3600, now),
               'tote_jobs': len(jobs), 'completed_tote_jobs': len(completed_jobs),
               'tote_presentations': presentations,
               'completed_order_lines_per_tote_presentation': ratio(completed_lines, presentations),
               'min_completed_order_lines_per_tote_presentation': min((j['covered_lines'] for j in completed_jobs), default=None),
               'max_completed_order_lines_per_tote_presentation': max((j['covered_lines'] for j in completed_jobs), default=None),
               'lines_per_completed_tote_trip': ratio(completed_lines, len(completed_jobs)),
               'tote_trips_per_1000_lines': ratio(len(completed_jobs)*1000, completed_lines),
               'mean_store_completion_seconds': mean(durations) if durations else None,
               'p95_store_completion_seconds': percentile(durations, .95),
               'crane_utilization': ratio(sum(c['busy_seconds'] for c in cranes), now*len(cranes)),
               'workstation_utilization': ratio(total['picking_seconds'], now*len(cranes)),
               'travel_distance_m': travel, 'travel_metres_per_completed_line': ratio(travel, completed_lines),
               'wall_clock_seconds': wall_seconds,
               'simulated_seconds_per_wall_second': ratio(now, wall_seconds), **total}
    return summary, {'tasks.csv': tasks, 'tote_jobs.csv': jobs, 'cranes.csv': cranes,
                     'workstations.csv': stations, 'allocation.csv': schedule.allocation}


def save_run(directory, schedule, wall_seconds, failure_reason=None):
    summary, tables = metrics(schedule, wall_seconds, failure_reason)
    for name, rows in tables.items():
        write_csv(Path(directory)/name, rows, None if rows else ['job_id', 'completion_time_s'])
    write_json(Path(directory)/'summary.json', summary)
    return summary


def aggregate(results, names, dates, baseline):
    lookup = {(r['layout'], r['date']): r for r in results}
    common = [day for day in sorted(dates) if all(
        lookup.get((name, day), {}).get('success_flag', False) for name in names)]
    summaries, paired = [], []
    for name in names:
        rows = [lookup[name, day] for day in common]
        all_rows = [lookup.get((name, day), {'status': 'missing'}) for day in dates]
        rates = [r['line_throughput_per_hour'] for r in rows]
        summary = {'layout': name, 'baseline_layout': baseline, 'requested_days': len(dates),
                   'comparison_days': len(common),
                   'completed_days': sum(r.get('success_flag', False) for r in all_rows),
                   'incomplete_days': sum(r['status'] == 'incomplete' for r in all_rows),
                   'failed_days': sum(r['status'] in ('failed', 'missing') for r in all_rows),
                   'comparison_status': 'complete' if len(common) == len(dates) else ('provisional' if common else 'unavailable'),
                   'mean_daily_throughput_lines_per_hour': mean(rates) if rates else None,
                   'median_daily_throughput_lines_per_hour': median(rates) if rates else None,
                   'stddev_daily_throughput_lines_per_hour': stdev(rates) if len(rates)>1 else (0.0 if rates else None),
                   'weighted_throughput_lines_per_hour': ratio(sum(r['completed_lines'] for r in rows),
                                                              sum(r['sim_duration_s'] for r in rows)/3600) if rows else None,
                   'total_completed_lines_comparison_days': sum(r['completed_lines'] for r in rows)}
        for key in KPI_MEANS:
            values = [r[key] for r in rows if r.get(key) is not None]
            summary['mean_daily_'+key] = mean(values) if values else None
        for kind, operation in [('min', min), ('max', max)]:
            key = kind+'_completed_order_lines_per_tote_presentation'
            values = [r[key] for r in rows if r.get(key) is not None]
            summary[key] = operation(values) if values else None
        summaries.append(summary)
    base = next(s for s in summaries if s['layout'] == baseline)['mean_daily_throughput_lines_per_hour']
    for summary in summaries:
        value = summary['mean_daily_throughput_lines_per_hour']
        summary['throughput_improvement_percent'] = (value/base-1)*100 if value is not None and base else None
    for day in common:
        reference = lookup[baseline, day]['line_throughput_per_hour']
        for name in names:
            value = lookup[name, day]['line_throughput_per_hour']
            paired.append({'layout': name, 'date': day, 'baseline_layout': baseline,
                           'throughput_lines_per_hour': value, 'baseline_lines_per_hour': reference,
                           'difference_lines_per_hour': value-reference,
                           'improvement_percent': (value/reference-1)*100 if reference else None})
    return summaries, paired, common


def generate_report(output, results, layouts, dates, baseline):
    output = Path(output)
    names = list(layouts)
    summaries, paired, common = aggregate(results, names, dates, baseline)
    excluded = sorted(set(dates)-set(common))
    for row in summaries:
        name = row['layout']
        row['slotting_strategy'] = layouts[name]['strategy']
        write_csv(output/name/'daily_metrics.csv', sorted((r for r in results if r['layout'] == name), key=lambda r: r['date']))
        write_json(output/name/'summary.json', {**row, 'included_dates': common, 'excluded_dates': excluded})
    write_csv(output/'layout_comparison.csv', summaries)
    write_csv(output/'paired_daily_differences.csv', paired, ['layout', 'date', 'baseline_layout',
              'throughput_lines_per_hour', 'baseline_lines_per_hour', 'difference_lines_per_hour', 'improvement_percent'])
    write_json(output/'comparison_dates.json', {'included_dates': common, 'excluded_dates': excluded})
    charts = make_charts(output, results, summaries, common)
    def fmt(value):
        return f'{value:,.2f}' if value is not None else 'unavailable'
    text = ['# ASRS slotting layout comparison', '', f'Comparison status: **{summaries[0]["comparison_status"]}**. Baseline: **{baseline}**.', '',
            'Primary metric: arithmetic mean daily completed order lines/hour. Lines complete only after the tote is returned and the fork retracts.',
            f'All layouts use the same {len(common)} completed dates out of {len(dates)} requested dates.', '',
            '| Layout | Completed/requested days | Mean lines/hour | Change vs baseline |',
            '|---|---:|---:|---:|']
    for row in summaries:
        text.append(f'| {row["layout"]} | {row["completed_days"]}/{len(dates)} | {fmt(row["mean_daily_throughput_lines_per_hour"])} | {fmt(row["throughput_improvement_percent"])}% |')
    text += ['', '## Comparison controls', '',
             'The same store/day SKU line counts are released at time zero. Each crane processes one store batch at a time. A single tote covers all matching lines in that batch; quantities and stock depletion are not modeled.',
             'Duplicate SKU locations are allocated by estimated completion time including queued work. Scheduling uses deterministic greedy allocation and nearest-next retrieval, not a global optimum.',
             'Picking is per tote presentation and includes workstation handoff. Source totes always return to their original slots. Cranes finish at their final return locations; there is no end-of-day homing leg.',
             'Travel distance is summed absolute X/Y/Z actuator travel (including the fork), not Euclidean carriage distance. X and Z move concurrently, so their motion times overlap. Stage time categories are exclusive and sum to crane busy time.',
             'Tote presentations count completed workstation picking delays, including totes not yet returned at a cutoff. Completed lines and jobs require completed return/retraction.', '',
             'Included dates: '+(', '.join(common) or 'none'), '', 'Excluded dates: '+(', '.join(excluded) or 'none')]
    if excluded:
        text += ['', '**No overall winner is declared.** Incomplete/failed dates are excluded from every comparative average. Partial metrics remain available in daily diagnostics.']
    text += ['', '## Results', '']
    text += [f'![{Path(path).stem}]({path})' for path in charts]
    text += ['', 'Per-day tables: `tasks.csv`, `tote_jobs.csv`, `cranes.csv`, `workstations.csv`, `allocation.csv`, and `summary.json`. See the manifest, config snapshot, workload snapshot, and validation reports for reproducibility.']
    (output/'comparison_report.md').write_text('\n'.join(text)+'\n')


def make_charts(output, results, summaries, common):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    names = [s['layout'] for s in summaries]
    labels = [n.removesuffix('_slotting_layout').replace('_', '\n') for n in names]
    fig, axes = plt.subplots(2, 3, figsize=(17, 10), constrained_layout=True)
    panels = [('mean_daily_throughput_lines_per_hour', 'Completed lines / hour'),
              ('mean_daily_makespan_hours', 'Daily makespan (hours)'),
              ('mean_daily_crane_utilization', 'Crane utilization'),
              ('mean_daily_lines_per_completed_tote_trip', 'Lines per completed tote trip'),
              ('mean_daily_p95_store_completion_seconds', 'P95 store completion (seconds)'),
              ('mean_daily_travel_metres_per_completed_line', 'Actuator metres per completed line')]
    for axis, (key, title) in zip(axes.flat, panels):
        values = [s.get(key) for s in summaries]
        if common:
            bars = axis.bar(labels, values, color=['#287c8e', '#d49735', '#688b4c'][:len(names)] if len(names)<=3 else '#287c8e')
            axis.bar_label(bars, fmt='%.2f', padding=3)
            axis.margins(y=.12)
            axis.grid(axis='y', alpha=.2)
        else:
            axis.text(.5, .5, 'No common completed dates', ha='center', transform=axis.transAxes)
        axis.set_title(title)
    fig.suptitle(f'ASRS layout comparison | {len(common)} common completed days')
    paths = []
    for extension in ('png', 'svg'):
        fig.savefig(output/f'comparison_overview.{extension}', dpi=150)
    paths.append('comparison_overview.png')
    plt.close(fig)
    if common:
        fig, axes = plt.subplots(1, 2, figsize=(15, 5), constrained_layout=True)
        for name in names:
            rows = sorted((r for r in results if r['layout'] == name and r['date'] in common), key=lambda r: r['date'])
            axes[0].plot([r['date'] for r in rows], [r['line_throughput_per_hour'] for r in rows], marker='.', label=name)
        axes[0].set_title('Daily completed lines / hour')
        axes[0].tick_params(axis='x', rotation=45)
        axes[0].xaxis.set_major_locator(plt.MaxNLocator(8))
        axes[0].legend(fontsize=7)
        bottoms = [0.0]*len(names)
        for category in CATEGORIES:
            values = [mean(r[category+'_seconds']/3600 for r in results if r['layout']==name and r['date'] in common) for name in names]
            axes[1].bar(labels, values, bottom=bottoms, label=category.replace('_', ' '))
            bottoms = [a+b for a, b in zip(bottoms, values)]
        axes[1].set_title('Mean daily fleet busy hours by stage')
        axes[1].legend(fontsize=8)
        for extension in ('png', 'svg'):
            fig.savefig(output/f'daily_throughput_and_time.{extension}', dpi=150)
        plt.close(fig)
        paths.append('daily_throughput_and_time.png')
        # Weight utilization by simulated duration, as in the current-heat station report.
        fig, axes = plt.subplots(1, 2, figsize=(15, 5), constrained_layout=True)
        fleet_rows = {}
        rack_counts = {}
        for name in names:
            fleet_rows[name], rack_counts[name] = defaultdict(lambda: defaultdict(float)), defaultdict(int)
            for day in common:
                with (output/name/day/'cranes.csv').open(newline='') as stream:
                    for row in csv.DictReader(stream):
                        for key in ('busy_seconds', 'idle_seconds', 'picking_seconds'):
                            fleet_rows[name][row['crane']][key] += float(row[key])
                with (output/name/day/'tote_jobs.csv').open(newline='') as stream:
                    for row in csv.DictReader(stream):
                        if row['completion_time_s']:
                            rack_counts[name][row['rack_id']] += 1
        crane_names = sorted({c for rows in fleet_rows.values() for c in rows})
        width = .8/len(names)
        for index, name in enumerate(names):
            values = fleet_rows[name]
            x = [i-.4+width*(index+.5) for i in range(len(crane_names))]
            for ax, key in zip(axes, ('busy_seconds', 'picking_seconds')):
                ax.bar(x, [100*ratio(values[c][key], values[c]['busy_seconds']+values[c]['idle_seconds'])
                           for c in crane_names], width=width, label=name)
        for ax, title in zip(axes, ('Crane utilization by aisle', 'Workstation picking utilization by aisle')):
            ax.set_xticks(range(len(crane_names)), crane_names, rotation=25)
            ax.set(title=title, ylabel='% of simulated time')
            ax.legend(fontsize=7)
        for extension in ('png', 'svg'):
            fig.savefig(output/f'aisle_utilization.{extension}', dpi=150)
        plt.close(fig)
        paths.append('aisle_utilization.png')
        fig, axes = plt.subplots(1, len(names), figsize=(6*len(names), 5), constrained_layout=True, squeeze=False)
        maximum = max((max(rows.values(), default=0) for rows in rack_counts.values()), default=1)
        for ax, name in zip(axes[0], names):
            rows = rack_counts[name]
            coordinates = [tuple(map(int, rack.removeprefix('G').split('_'))) for rack in rows]
            if coordinates:
                x, y = zip(*coordinates)
                dots = ax.scatter(x, y, c=list(rows.values()), cmap='viridis', vmin=0, vmax=maximum, marker='s', s=24)
                fig.colorbar(dots, ax=ax, label='Completed tote retrievals', shrink=.75)
            ax.set(title=name.removesuffix('_slotting_layout').replace('_', ' '),
                   xlabel='Rack column', ylabel='Rack row', aspect='equal')
            ax.invert_yaxis()
        fig.suptitle('Rack retrieval demand on common completed days (shared color scale)')
        for extension in ('png', 'svg'):
            fig.savefig(output/f'rack_retrievals.{extension}', dpi=150)
        plt.close(fig)
        paths.append('rack_retrievals.png')
    return paths
