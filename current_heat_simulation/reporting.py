"""Paired-day comparison tables and static diagnostic charts."""

from collections import Counter
import csv
from pathlib import Path
from statistics import mean, median, stdev

from .run_metrics import TIME_CATEGORIES, ratio, write_csv, write_json

KPI_MEANS = (
    "completed_order_lines_per_rack_presentation",
    "makespan_hours", "travel_metres_per_completed_line", "lines_per_completed_rack_trip",
    "skus_per_completed_rack_trip", "rack_trips_per_1000_lines", "mean_store_completion_seconds",
    "p95_store_completion_seconds", "amr_utilization", "reservation_wait_seconds_per_line",
    "safety_wait_seconds_per_line", "replans_per_1000_lines", "empty_distance_m", "loaded_distance_m",
)


def csv_rows(path):
    if not path.exists():
        return []
    with path.open(newline="") as stream:
        return list(csv.DictReader(stream))


def table(path, rows):
    fields = list(dict.fromkeys(key for row in rows for key in row))
    write_csv(path, rows, fields)


def aggregate(results, layouts, dates, baseline):
    lookup = {(r["layout"], r["date"]): r for r in results}
    common = [day for day in sorted(dates) if all(
        lookup.get((name, day), {}).get("success_flag", False) for name in layouts)]
    complete = len(common) == len(dates)
    summaries = []
    for name in layouts:
        all_rows = [lookup.get((name, day), {"status": "missing"}) for day in dates]
        rows = [lookup[name, day] for day in common]
        rates = [r["line_throughput_per_hour"] for r in rows]
        summary = {
            "layout": name, "requested_days": len(dates), "comparison_days": len(common),
            "completed_days": sum(r.get("success_flag", False) for r in all_rows),
            "incomplete_days": sum(r.get("status") == "incomplete" for r in all_rows),
            "failed_days": sum(r.get("status") in {"failed", "missing"} for r in all_rows),
            "comparison_status": "complete" if complete else ("provisional" if common else "unavailable"),
            "mean_daily_throughput_lines_per_hour": mean(rates) if rates else None,
            "median_daily_throughput_lines_per_hour": median(rates) if rates else None,
            "stddev_daily_throughput_lines_per_hour": stdev(rates) if len(rates)>1 else (0.0 if rates else None),
            "weighted_throughput_lines_per_hour": ratio(sum(r["completed_lines"] for r in rows),
                                                        sum(r["sim_duration_s"] for r in rows)/3600),
            "total_completed_lines_comparison_days": sum(r["completed_lines"] for r in rows),
        }
        for key in KPI_MEANS:
            values = [r[key] for r in rows if r.get(key) is not None]
            summary["mean_daily_"+key] = mean(values) if values else None
        summaries.append(summary)
    base = next(s for s in summaries if s["layout"] == baseline)
    for s in summaries:
        value, reference = s["mean_daily_throughput_lines_per_hour"], base["mean_daily_throughput_lines_per_hour"]
        s["baseline_layout"] = baseline
        s["throughput_improvement_percent"] = (value/reference-1)*100 if value is not None and reference else None
    paired = []
    for day in common:
        base_rate = lookup[baseline, day]["line_throughput_per_hour"]
        for name in layouts:
            value = lookup[name, day]["line_throughput_per_hour"]
            paired.append({"layout": name, "date": day, "baseline_layout": baseline,
                           "throughput_lines_per_hour": value, "baseline_lines_per_hour": base_rate,
                           "difference_lines_per_hour": value-base_rate,
                           "improvement_percent": (value/base_rate-1)*100 if base_rate else None})
    return summaries, paired, common


def generate_report(output: Path, results, layouts, dates, baseline, warehouse_map):
    summaries, paired, common = aggregate(results, layouts, dates, baseline)
    excluded = sorted(set(dates)-set(common))
    for summary in summaries:
        name = summary["layout"]
        summary["slotting_strategy"] = layouts[name].get("strategy")
        table(output/name/"daily_metrics.csv", [r for r in results if r["layout"] == name])
        write_json(output/name/"summary.json", {**summary, "included_dates": common, "excluded_dates": excluded})
    table(output/"layout_comparison.csv", summaries)
    write_csv(output/"paired_daily_differences.csv", paired,
              ["layout", "date", "baseline_layout", "throughput_lines_per_hour", "baseline_lines_per_hour",
               "difference_lines_per_hour", "improvement_percent"])
    write_json(output/"comparison_dates.json", {"included_dates": common, "excluded_dates": excluded})
    charts = _charts(output, results, summaries, common, warehouse_map)
    status = summaries[0]["comparison_status"]
    lines = ["# Current-heat slotting comparison", "",
             f"Comparison status: **{status}**. Baseline: **{baseline}**.", "",
             "Primary metric: arithmetic mean daily completed order lines/hour. Lines complete after rack return and jack-down.",
             f"All layouts use the same {len(common)} completed dates out of {len(dates)} requested dates.", "",
             "Included dates: " + (", ".join(common) or "none"), "",
             "Excluded dates: " + (", ".join(excluded) or "none"), ""]
    if status != "complete":
        lines += ["**No overall winner is declared.** Failed or unfinished days are excluded from every layout's comparative average; partial-run throughput is retained only in daily diagnostics.", ""]
    lines += ["| Layout | Strategy | Completed/requested | Mean lines/hour | Change vs baseline |",
              "|---|---|---:|---:|---:|"]
    for s in summaries:
        value, change = s["mean_daily_throughput_lines_per_hour"], s["throughput_improvement_percent"]
        rate_text = f"{value:.2f}" if value is not None else "unavailable"
        change_text = f"{change:+.2f}%" if change is not None else "unavailable"
        lines.append(f"| {s['layout']} | {s.get('slotting_strategy') or 'unspecified'} | {s['completed_days']}/{s['requested_days']} | {rate_text} | {change_text} |")
    lines += ["", "| Layout | Completed order lines per rack presentation (daily mean) |",
              "|---|---:|"]
    for s in summaries:
        value = s["mean_daily_completed_order_lines_per_rack_presentation"]
        value_text = f"{value:.2f}" if value is not None else "unavailable"
        lines.append(f"| {s['layout']} | {value_text} |")
    for title, filename in charts:
        lines += ["", f"## {title}", "", f"![{title}](charts/{filename})"]
    lines += ["", "## Interpretation and definitions", "",
              "- Throughput bars use sample standard deviation across days, not a confidence interval. One day has no measured between-day variation.",
              "- Completed order lines per rack presentation divides returned/jacked-down order lines by rack arrivals at workstation service. Unfinished returns contribute a presentation but no completed lines. No presentations means unavailable.",
              "- Weighted throughput is total completed lines divided by total simulation hours over the common dates.",
              "- Travel is actual substep displacement, split by loaded/empty state. Normalized travel and waits use completed source order lines, not units or distinct SKUs.",
              "- Robot time categories are mutually exclusive. Utilization includes active waiting; movement and waiting charts explain the difference.",
              "- Reservation/path wait includes an active robot lacking a granted next step. Safety blocking is separately counted. Neither is an explicit workstation queue-time measurement.",
              "- Hotspots accumulate blocked robot-seconds at the desired next node when known, otherwise at the current node. Multiple robots' waiting times add together.",
              "- Workstation utilization is actual service time divided by daily simulation duration; completed lines are credited after rack return.",
              "- Replica layout differences should be zero. Strategy names describe input metadata and do not establish the cause of an observed performance difference.",
              "- Current-heat coordination and motion rules are retained. These results do not establish deadlock freedom or physical collision safety.", "",
              "Detailed results: [layout comparison](layout_comparison.csv), [paired daily differences](paired_daily_differences.csv), and per-layout/day CSV files.", ""]
    (output/"comparison_report.md").write_text("\n".join(lines))
    return summaries


def _charts(output, results, summaries, common, warehouse_map):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    directory = output/"charts"
    directory.mkdir(exist_ok=True)
    names = [s["layout"] for s in summaries]
    labels = [name.replace("map1_", "").replace("_", " ") for name in names]
    colors = plt.get_cmap("tab10")(np.arange(len(names)) % 10)
    charts = []

    def save(fig, title, filename):
        fig.tight_layout()
        fig.savefig(directory/filename, dpi=150)
        plt.close(fig)
        charts.append((title, filename))

    fig, ax = plt.subplots(figsize=(max(8, len(names)*2), 4.5))
    if common:
        ax.bar(labels, [s["mean_daily_throughput_lines_per_hour"] for s in summaries], color=colors,
               yerr=[s["stddev_daily_throughput_lines_per_hour"] for s in summaries], capsize=4)
        ax.set_ylabel("Completed order lines/hour")
    else:
        ax.text(.5, .5, "No common completed dates — comparison unavailable", ha="center", transform=ax.transAxes)
    ax.set_title(f"Mean daily throughput · {len(common)} common days (± daily SD)")
    ax.tick_params(axis="x", labelrotation=15)
    save(fig, "Throughput comparison", "throughput.png")

    fig, ax = plt.subplots(figsize=(10, 4))
    for name, label, color in zip(names, labels, colors):
        rows = sorted((r for r in results if r["layout"] == name and r.get("success_flag")), key=lambda r:r["date"])
        ax.plot([r["date"] for r in rows], [r["line_throughput_per_hour"] for r in rows], ".-", label=label, color=color)
    ax.set(title="Daily throughput · successful days only; see excluded dates in report", ylabel="Order lines/hour")
    if len(ax.get_xticks()) > 12:
        for i, label in enumerate(ax.get_xticklabels()):
            label.set_visible(i % max(1, len(ax.get_xticklabels())//10) == 0)
    ax.tick_params(axis="x", labelrotation=45)
    ax.legend(fontsize=8)
    save(fig, "Daily throughput", "daily_throughput.png")

    fig, axes = plt.subplots(1, 2, figsize=(11, 4))
    for ax, key, title in zip(axes, ["travel_metres_per_completed_line", "rack_trips_per_1000_lines"],
                             ["Travel metres / completed line", "Rack trips / 1,000 lines"]):
        if common:
            ax.bar(labels, [s["mean_daily_"+key] or 0 for s in summaries], color=colors)
        ax.set_title(title)
        ax.tick_params(axis="x", labelrotation=25)
    save(fig, "Travel and rack consolidation", "travel_and_trips.png")

    fig, axes = plt.subplots(1, 2, figsize=(13, 5))
    bottoms = np.zeros(len(names))
    for category in TIME_CATEGORIES:
        values = []
        for name in names:
            rows = [r for r in results if r["layout"] == name and r["date"] in common]
            total = sum(sum(r.get(c+"_seconds", 0) for c in TIME_CATEGORIES) for r in rows)
            values.append(sum(r.get(category+"_seconds", 0) for r in rows)/total*100 if total else 0)
        axes[0].bar(labels, values, bottom=bottoms, label=category.replace("_", " "))
        bottoms += values
    axes[0].set(title="Robot time breakdown (common days)", ylabel="% available robot time")
    axes[0].legend(fontsize=7, loc="upper left", bbox_to_anchor=(0, 1.4), ncol=2)
    station_names = sorted(warehouse_map.workstations)
    for index, (name, label, color) in enumerate(zip(names, labels, colors)):
        totals = Counter()
        duration = sum(r["sim_duration_s"] for r in results if r["layout"] == name and r["date"] in common)
        for day in common:
            for row in csv_rows(output/name/day/"workstations.csv"):
                totals[row["workstation"]] += float(row["service_seconds"])
        width = .8/len(names)
        axes[1].bar(np.arange(len(station_names))+index*width, [totals[s]/duration*100 if duration else 0 for s in station_names],
                    width=width, label=label, color=color)
    axes[1].set_xticks(np.arange(len(station_names))+.4-.4/len(names), station_names)
    axes[1].set(title="Workstation service utilization", ylabel="% simulation time")
    axes[1].legend(fontsize=7)
    for ax in axes:
        ax.tick_params(axis="x", labelrotation=30)
    save(fig, "Robot activity and workstation balance", "activity_and_stations.png")

    fig, axes = plt.subplots(1, len(names), figsize=(max(7, len(names)*4), 5), squeeze=False)
    per_layout = []
    maximum = 0
    for name in names:
        totals = Counter()
        for day in common:
            for row in csv_rows(output/name/day/"blocking_hotspots.csv"):
                totals[row["node"]] += float(row["blocked_robot_seconds"])
        per_layout.append(totals)
        maximum = max(maximum, max(totals.values(), default=0))
    for ax, label, totals in zip(axes[0], labels, per_layout):
        for a, b, _ in warehouse_map.adjacency_items():
            start, end = warehouse_map.vertices[a], warehouse_map.vertices[b]
            ax.plot([start.x, end.x], [start.y, end.y], color="0.85", linewidth=.35, zorder=0)
        nodes = [n for n in totals if n in warehouse_map.name_to_index]
        if nodes:
            x, y = zip(*(warehouse_map.point(n) for n in nodes))
            dots = ax.scatter(x, y, c=[totals[n] for n in nodes], cmap="magma", vmin=0, vmax=maximum or 1, s=24)
            fig.colorbar(dots, ax=ax, label="Blocked robot-seconds", shrink=.6)
        ax.set(title=label, xlabel="metres", ylabel="metres", aspect="equal")
    save(fig, "Blocking hotspots on common days", "blocking_hotspots.png")
    return charts
