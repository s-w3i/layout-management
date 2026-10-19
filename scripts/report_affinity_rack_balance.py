"""Build a short, evidence-backed report after every revised benchmark run passes."""

import argparse
from collections import defaultdict
import csv
from html import escape
import json
import math
from pathlib import Path
from statistics import mean, stdev
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from current_heat_simulation.batch import fingerprint, read_checkpoint


def read_json(path):
    return json.loads(path.read_text())


def verified_days(root, name, dates):
    manifest = read_json(root / "manifest.json")
    result = []
    for day in dates:
        key = fingerprint({"batch": manifest["fingerprint"], "layout": name, "date": day})
        row = read_checkpoint(root / name / day, key)
        if row is None:
            raise ValueError(f"missing or invalid completed checkpoint: {root / name / day}")
        if row["completed_lines"] != row["source_lines"] or row["unfinished_tasks"]:
            raise ValueError("incomplete workload cannot enter the comparison")
        row["presentations_per_hour"] = row["rack_presentations"] / row["makespan_hours"]
        ratio = row["completed_lines"] / row["rack_presentations"]
        if not math.isclose(ratio * row["presentations_per_hour"], row["line_throughput_per_hour"]):
            raise ValueError("throughput decomposition is inconsistent")
        result.append(row)
    return result


def describe(rows):
    rates = [row["line_throughput_per_hour"] for row in rows]
    result = {"mean_throughput": mean(rates), "minimum_throughput": min(rates), "maximum_throughput": max(rates),
              "throughput_cv_percent": 100 * stdev(rates) / mean(rates),
              "mean_lines_per_presentation": mean(row["completed_lines"] / row["rack_presentations"] for row in rows),
              "mean_presentations_per_hour": mean(row["presentations_per_hour"] for row in rows),
              "mean_travel_metres_per_line": mean(row["travel_distance_m"] / row["completed_lines"] for row in rows),
              "mean_reservation_wait_seconds_per_line": mean(row["reservation_path_wait_seconds"] / row["completed_lines"] for row in rows),
              "mean_handling_seconds_per_line": mean(row["handling_service_seconds"] / row["completed_lines"] for row in rows),
              "mean_translation_seconds_per_line": mean(row["translation_seconds"] / row["completed_lines"] for row in rows),
              "mean_rotation_seconds_per_line": mean(row["rotation_seconds"] / row["completed_lines"] for row in rows),
              "mean_amr_utilization": mean(row["amr_utilization"] for row in rows)}
    if all("repeat_line_factor" in row for row in rows):
        result["mean_repeat_line_factor"] = mean(row["repeat_line_factor"] for row in rows)
        result["mean_distinct_skus_per_presentation"] = mean(row["distinct_skus_per_presentation"] for row in rows)
    log_q = [math.log(row["line_throughput_per_hour"]) for row in rows]
    log_l = [math.log(row["completed_lines"] / row["rack_presentations"]) for row in rows]
    log_p = [math.log(row["presentations_per_hour"]) for row in rows]

    def covariance(a, b):
        return mean((x - mean(a)) * (y - mean(b)) for x, y in zip(a, b))

    variance = covariance(log_q, log_q)
    result["lines_variation_accounting_share"] = covariance(log_q, log_l) / variance if variance else None
    result["presentation_rate_variation_accounting_share"] = covariance(log_q, log_p) / variance if variance else None
    if all("repeat_line_factor" in row for row in rows):
        result["repeated_lines_variation_accounting_share"] = covariance(log_q, [math.log(row["repeat_line_factor"]) for row in rows]) / variance if variance else None
        result["distinct_sku_grouping_variation_accounting_share"] = covariance(log_q, [math.log(row["distinct_skus_per_presentation"]) for row in rows]) / variance if variance else None
    factors = {
        "lines_per_presentation": [row["completed_lines"] / row["rack_presentations"] for row in rows],
        "presentations_per_hour": [row["presentations_per_hour"] for row in rows],
        "daily_source_lines": [row["source_lines"] for row in rows],
        "travel_metres_per_line": [row["travel_distance_m"] / row["completed_lines"] for row in rows],
        "reservation_wait_seconds_per_line": [row["reservation_path_wait_seconds"] / row["completed_lines"] for row in rows],
        "handling_seconds_per_line": [row["handling_service_seconds"] / row["completed_lines"] for row in rows],
        "translation_seconds_per_line": [row["translation_seconds"] / row["completed_lines"] for row in rows],
        "rotation_seconds_per_line": [row["rotation_seconds"] / row["completed_lines"] for row in rows],
        "amr_utilization": [row["amr_utilization"] for row in rows],
    }
    if all("repeat_line_factor" in row for row in rows):
        for field in ("repeat_line_factor", "distinct_skus_per_presentation", "store_day_task_count"):
            factors[field] = [row[field] for row in rows]
    correlations = {}
    for field, values in factors.items():
        denominator = math.sqrt(covariance(rates, rates) * covariance(values, values))
        correlations[field] = covariance(rates, values) / denominator if denominator else None
    result["pearson_throughput_by_factor"] = correlations
    result["pearson_throughput_lines"] = correlations["lines_per_presentation"]
    return result


def add_demand_factors(rows, snapshot):
    for row in rows:
        tasks = snapshot[row["date"]]
        source_lines = sum(task["source_lines"] for task in tasks)
        distinct = sum(len(task["line_counts"]) for task in tasks)
        if source_lines != row["completed_lines"]:
            raise ValueError("completed lines differ from source snapshot")
        row["distinct_store_sku_requests"] = distinct
        row["store_day_task_count"] = len(tasks)
        row["repeat_line_factor"] = source_lines / distinct
        row["distinct_skus_per_presentation"] = distinct / row["rack_presentations"]
        if not math.isclose(row["repeat_line_factor"] * row["distinct_skus_per_presentation"]
                            * row["presentations_per_hour"], row["line_throughput_per_hour"]):
            raise ValueError("repeated-line/grouping/execution decomposition is inconsistent")


def jobs_by_task(directory):
    grouped = defaultdict(list)
    with (directory / "rack_jobs.csv").open() as stream:
        for row in csv.DictReader(stream):
            if row["completion_time_s"]:
                grouped[row["task_id"]].append({"rack": row["rack_id"], "skus": json.loads(row["covered_skus"]),
                                                 "lines": int(row["covered_lines"]), "job_id": row["job_id"]})
    return grouped


def task_examples(old_root, new_root, cap, dates):
    examples = []
    subset_example = None
    for day in dates:
        old = jobs_by_task(old_root / f"affinity_zone_on_max_same_sku_{cap}" / day)
        new = jobs_by_task(new_root / f"revised_max_same_sku_{cap}" / day)
        if old.keys() != new.keys():
            raise ValueError("store/day task sets differ")
        for task in old:
            old_skus = {sku for job in old[task] for sku in job["skus"]}
            new_skus = {sku for job in new[task] for sku in job["skus"]}
            if old_skus != new_skus or sum(job["lines"] for job in old[task]) != sum(job["lines"] for job in new[task]):
                raise ValueError("store/day coverage changed")
            if 1 < len(old_skus) <= 5:
                examples.append({"task_id": task, "date": day, "cap": cap, "skus": sorted(old_skus),
                                 "old": old[task], "new": new[task], "saved_presentations": len(old[task]) - len(new[task])})
            elif len(old_skus) > 5:
                old_job_by_sku = {sku: index for index, job in enumerate(old[task]) for sku in job["skus"]}
                for job in new[task]:
                    representatives = {}
                    for sku in sorted(job["skus"]):
                        representatives.setdefault(old_job_by_sku[sku], sku)
                    if len(representatives) < 2:
                        continue
                    skus = sorted(list(representatives.values())[:3])
                    before = [job for job in old[task] if set(skus).intersection(job["skus"])]
                    after = [job for job in new[task] if set(skus).intersection(job["skus"])]
                    candidate = {"task_id": task, "date": day, "cap": cap, "skus": skus,
                                 "old": before, "new": after, "saved_presentations": len(before) - len(after),
                                 "scope": "subset_of_same_store_day", "basket_sku_count": len(old_skus),
                                 "old_full_basket_visits": len(old[task]), "new_full_basket_visits": len(new[task])}
                    if subset_example is None or (candidate["saved_presentations"], -len(old_skus)) > (
                            subset_example["saved_presentations"], -subset_example["basket_sku_count"]):
                        subset_example = candidate
    favorable = max(examples, key=lambda row: (row["saved_presentations"], -len(row["skus"]))) if examples else None
    unfavorable = min(examples, key=lambda row: (row["saved_presentations"], len(row["skus"]))) if examples else None
    return {"fewer_presentations": favorable if favorable and favorable["saved_presentations"] > 0 else subset_example,
            "more_presentations": unfavorable if unfavorable and unfavorable["saved_presentations"] < 0 else None}


def collect(output):
    old_root = ROOT / "current_heat_simulation/results/affinity_zone_on_max_same_sku_1_to_6_10_dates"
    days = read_json(old_root / "manifest.json")["identity"]["dates"]
    demand = read_json(old_root / "workload_snapshot.json")["days"]
    generated = read_json(output / "generation_results.json")
    if sorted(row["cap"] for row in generated) != list(range(1, 7)):
        raise ValueError("all six limits are required")
    comparisons = []
    all_rows = {}
    for entry in generated:
        cap = entry["cap"]
        old = verified_days(old_root, f"affinity_zone_on_max_same_sku_{cap}", days)
        new = verified_days(output / "simulation", f"revised_max_same_sku_{cap}", days)
        add_demand_factors(old, demand)
        add_demand_factors(new, demand)
        if [row["source_lines"] for row in old] != [row["source_lines"] for row in new]:
            raise ValueError("daily source lines differ")
        before, after = describe(old), describe(new)
        layout = read_json(Path(entry["path"]))
        original_layout = read_json(ROOT / f"resources/map/amr/affinity_zone_on_max_same_sku_{cap}.slotting.json")
        comparisons.append({"cap": cap, "old": before, "new": after,
                            "throughput_change_percent": 100 * (after["mean_throughput"] / before["mean_throughput"] - 1),
                            "inventory_audit": layout["summary"]["inventory_audit"],
                            "original_unassigned_loads": original_layout["summary"]["unassigned_count"],
                            "unassigned_inventory": [{key: row.get(key) for key in
                                                       ("sku", "inventory_load_id", "quantity_ea", "assignment_status")}
                                                      for row in layout["assignments"] if row["assignment_status"] != "ASSIGNED"],
                            "unverified_physical_inventory": [{key: row.get(key) for key in
                                                               ("sku", "inventory_load_id", "physical_missing_fields")}
                                                              for row in layout["assignments"]
                                                              if row["assignment_status"] == "ASSIGNED"
                                                              and row.get("physical_data_status") == "MISSING"],
                            "rack_zone_balance": layout["summary"]["rack_zone_balance"]})
        all_rows[cap] = {"old": old, "new": new}
    winner = max(comparisons, key=lambda row: row["new"]["mean_throughput"])["cap"]
    selection = read_json(output / "additional_date_selection.json")
    if selection["selected_cap"] != winner:
        raise ValueError("additional-date selection differs from the ten-date winner")
    extra_old = verified_days(output / "additional_dates", f"affinity_zone_on_max_same_sku_{winner}", selection["dates"])
    extra_new = verified_days(output / "additional_dates", f"revised_max_same_sku_{winner}", selection["dates"])
    extra_demand = read_json(output / "additional_dates" / "workload_snapshot.json")["days"]
    add_demand_factors(extra_old, extra_demand)
    add_demand_factors(extra_new, extra_demand)
    if [row["source_lines"] for row in extra_old] != [row["source_lines"] for row in extra_new]:
        raise ValueError("additional-date source lines differ")
    counter_days = []
    for cap, rows in all_rows.items():
        for old, new in zip(rows["old"], rows["new"]):
            if (new["rack_presentations"] < old["rack_presentations"]
                    and new["line_throughput_per_hour"] <= old["line_throughput_per_hour"]
                    and new["travel_distance_m"] < old["travel_distance_m"]
                    and new["reservation_path_wait_seconds"] > old["reservation_path_wait_seconds"]):
                counter_days.append({"cap": cap, "old": old, "new": new})
    counter_day = min(counter_days, key=lambda row: row["new"]["line_throughput_per_hour"]
                      / row["old"]["line_throughput_per_hour"], default=None)
    return {"main_dates": days, "selected_cap": winner, "comparisons": comparisons,
            "interpretation_note": "Correlations are descriptive paired-date associations. Shared numerators/denominators and demand mix prevent causal attribution. Log-variance accounting shares are signed mathematical contributions, not causal percentages.",
            "orders_input_verification": read_json(output / "orders_input_verification.json"),
            "daily": all_rows, "counter_day": counter_day,
            "additional": {"selection": selection, "old": extra_old, "new": extra_new,
                                               "old_statistics": describe(extra_old), "new_statistics": describe(extra_new)},
            "examples": {str(cap): task_examples(old_root, output / "simulation", cap, days) for cap in sorted({1, winner})}}


def make_pdf(data, destination):
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak

    for name, filename in (("Report", "DejaVuSans.ttf"), ("ReportBold", "DejaVuSans-Bold.ttf")):
        pdfmetrics.registerFont(TTFont(name, "/usr/share/fonts/truetype/dejavu/" + filename))
    pdfmetrics.registerFontFamily("Report", normal="Report", bold="ReportBold", italic="Report", boldItalic="ReportBold")
    styles = {"body": ParagraphStyle("body", fontName="Report", fontSize=10, leading=13.5, spaceAfter=6),
              "title": ParagraphStyle("title", fontName="ReportBold", fontSize=19, leading=24, spaceAfter=12),
              "heading": ParagraphStyle("heading", fontName="ReportBold", fontSize=12, leading=16, spaceAfter=7),
              "small": ParagraphStyle("small", fontName="Report", fontSize=8, leading=11, spaceAfter=5)}
    story = []

    def paragraph(text, style="body"):
        story.append(Paragraph(text, styles[style]))

    def table(headers, rows, widths):
        content = [[Paragraph("<b>" + escape(str(cell)) + "</b>", styles["small"]) for cell in headers]]
        content += [[Paragraph(escape(str(cell)), styles["small"]) for cell in row] for row in rows]
        result = Table(content, colWidths=widths, repeatRows=1)
        result.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#DDE8EF")),
                                   ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F3F6F8")]),
                                   ("GRID", (0, 0), (-1, -1), .3, colors.HexColor("#CCD6DD")),
                                   ("VALIGN", (0, 0), (-1, -1), "TOP"),
                                   ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
        story.extend([result, Spacer(1, 9)])

    winner = next(row for row in data["comparisons"] if row["cap"] == data["selected_cap"])
    old_best = max(data["comparisons"], key=lambda row: row["old"]["mean_throughput"])
    paragraph("Affinity slotting: measured results", "title")
    paragraph("<b>Group useful rack contents, then balance complete racks across zones.</b> "
              "The ten-date comparison fixes inventory targets, 40 AMRs, workstation assignments and simulation settings.")
    paragraph(f"The highest revised mean throughput is <b>{winner['new']['mean_throughput']:,.0f} lines/hour at limit {winner['cap']}</b>, "
              f"a {winner['throughput_change_percent']:+.1f}% change from the original at the same limit.")
    paragraph(f"The best original result is {old_best['old']['mean_throughput']:,.0f} lines/hour at limit {old_best['cap']}. "
              f"The best revised result changes that rate by {100*(winner['new']['mean_throughput']/old_best['old']['mean_throughput']-1):+.1f}%.", "small")
    table(["Same-SKU limit", "Original lines/h", "Revised lines/h", "Change", "Lines/visit old / new", "Daily CV % old / new"],
          [[row["cap"], f"{row['old']['mean_throughput']:,.0f}", f"{row['new']['mean_throughput']:,.0f}",
            f"{row['throughput_change_percent']:+.1f}%", f"{row['old']['mean_lines_per_presentation']:.3f} / {row['new']['mean_lines_per_presentation']:.3f}",
            f"{row['old']['throughput_cv_percent']:.1f} / {row['new']['throughput_cv_percent']:.1f}"] for row in data["comparisons"]], [66, 88, 88, 58, 105, 67])
    paragraph(f"Daily CV = daily standard deviation / mean; lower means steadier. At limit {winner['cap']} it changes from "
              f"{winner['old']['throughput_cv_percent']:.1f}% to {winner['new']['throughput_cv_percent']:.1f}%. "
              "Each column averages daily rates or ratios separately.", "small")
    shortages = [f"limit {row['cap']}: {len(row['unassigned_inventory'])} {'load' if len(row['unassigned_inventory']) == 1 else 'loads'} "
                 f"({sum(int(load['quantity_ea'] or 0) for load in row['unassigned_inventory'])} units; "
                 f"SKU {', '.join(sorted({load['sku'] for load in row['unassigned_inventory']}))}; "
                 f"original: {row['original_unassigned_loads']} unassigned loads)"
                 for row in data["comparisons"] if row["unassigned_inventory"]]
    paragraph("<b>Stock audit:</b> " + ("Unassigned required inventory remains at " + "; ".join(shortages)
              + ". These quantities remain in the input records but are not physically placed. See the evidence for SKU IDs and reasons."
              if shortages else "All required inventory loads are physically assigned at every limit.")
              + " The generation gate rejects any loss of assignment coverage against Basic or the original layout.", "small")
    unknown_count = max(len(row["unverified_physical_inventory"]) for row in data["comparisons"])
    if unknown_count:
        paragraph(f"{unknown_count} existing loads lack dimensions or weight; their physical fit remains unverified in exception storage.", "small")
    if winner["unassigned_inventory"]:
        complete = [row for row in data["comparisons"] if not row["unassigned_inventory"]]
        if complete:
            stock_winner = max(complete, key=lambda row: row["new"]["mean_throughput"])
            paragraph(f"The rate winner still has unassigned stock. Among layouts placing all required stock, "
                      f"limit {stock_winner['cap']} has the highest mean rate, {stock_winner['new']['mean_throughput']:,.0f} lines/hour.", "small")
    paragraph("Why the throughput changes", "heading")
    paragraph("<b>Lines/hour = lines/presentation x presentations/hour.</b> "
              "Grouping can reduce visits; travel, waiting, rack availability and task allocation affect the fleet's visit rate.")
    table([f"Limit {winner['cap']}", "Lines/visit", "Visits/hour", "Travel m/line", "Path wait s/line", "Handling s/line", "AMR active %"],
          [[label, f"{row['mean_lines_per_presentation']:.3f}", f"{row['mean_presentations_per_hour']:.0f}",
            f"{row['mean_travel_metres_per_line']:.1f}", f"{row['mean_reservation_wait_seconds_per_line']:.1f}",
            f"{row['mean_handling_seconds_per_line']:.1f}", f"{100*row['mean_amr_utilization']:.1f}"]
           for label, row in (("Original", winner["old"]), ("Revised", winner["new"]))], [52, 70, 68, 82, 82, 82, 60])
    paragraph("Visits/hour covers the whole fleet. AMR active time includes waiting; higher utilization alone does not imply higher output.", "small")
    counter = data.get("counter_day")
    if counter:
        old, new = counter["old"], counter["new"]
        lines = old["completed_lines"]
        paragraph(f"<b>More grouping, lower output:</b> {old['date']}, limit {counter['cap']}: lines/visit "
                  f"{lines/old['rack_presentations']:.2f} to {lines/new['rack_presentations']:.2f}, but lines/hour "
                  f"{old['line_throughput_per_hour']:,.0f} to {new['line_throughput_per_hour']:,.0f}. "
                  f"Travel fell {old['travel_distance_m']/lines:.0f} to {new['travel_distance_m']/lines:.0f} m/line; "
                  f"path waiting rose {old['reservation_path_wait_seconds']/lines:.0f} to "
                  f"{new['reservation_path_wait_seconds']/lines:.0f} seconds/line.", "small")
    paragraph("A limit of 1 spreads stock across racks and can fragment co-ordered SKU groups. "
              "It therefore does not guarantee more lines per visit.")
    balance = winner["rack_zone_balance"]
    paragraph((f"Whole-rack balancing moved {balance['moved_rack_group_count']} groups; predicted peak zone work "
               f"per configured rack fell {100*(1-balance['after']['maximum_normalized_zone_work_seconds']/balance['before']['maximum_normalized_zone_work_seconds']):.1f}%. "
              if balance["accepted"] else "The balancing proposal at the selected limit did not pass its acceptance rule, so its rack groups were retained. ")
              + "This planning estimate excludes queues and turns; throughput comes from the full traffic simulation.", "small")
    paragraph("ASRS retrieves single-SKU totes. Repeated lines for a store/day can exceed one line per trip; "
              "crane workload needs a separate evaluation.", "small")
    story.append(PageBreak())
    paragraph("Actual cases and daily variation", "title")
    new_days = data["daily"][data["selected_cap"]]["new"]
    low = min(new_days, key=lambda row: row["line_throughput_per_hour"])
    high = max(new_days, key=lambda row: row["line_throughput_per_hour"])
    table([f"Revised limit {winner['cap']}", "Repeat-line factor", "Distinct SKUs/visit", "Visits/hour", "Lines/hour"],
          [[row["date"], f"{row['repeat_line_factor']:.3f}", f"{row['distinct_skus_per_presentation']:.3f}",
            f"{row['presentations_per_hour']:.0f}", f"{row['line_throughput_per_hour']:,.0f}"] for row in (low, high)], [110, 88, 94, 86, 94])
    paragraph("<b>Repeat-line factor x distinct SKUs/visit x visits/hour = lines/hour.</b> "
              "The repeat-line factor measures multiple source lines for the same SKU and store/day. It is identical for every layout on a given date. "
              f"Across these ten dates it ranges from {min(row['repeat_line_factor'] for row in new_days):.3f} to {max(row['repeat_line_factor'] for row in new_days):.3f}. "
              "Slotting and rack selection affect distinct SKUs per visit; traffic and resource availability affect the fleet's visit rate.", "small")
    r = winner["new"]["pearson_throughput_lines"]
    paragraph((f"Across ten dates, throughput and lines per visit have correlation <b>{r:+.2f}</b>. " if r is not None else "")
              + "Both share completed lines in their calculation, so this describes the results without proving a single cause. "
              "Use the visit rate and travel/wait columns to assess the other drivers.")
    paragraph(f"<b>From the lowest to highest day:</b> repeated lines change by "
              f"{100*(high['repeat_line_factor']/low['repeat_line_factor']-1):+.1f}%, "
              f"distinct SKUs per visit by {100*(high['distinct_skus_per_presentation']/low['distinct_skus_per_presentation']-1):+.1f}% "
              f"and visits/hour by {100*(high['presentations_per_hour']/low['presentations_per_hour']-1):+.1f}%. "
              f"Together they give a {100*(high['line_throughput_per_hour']/low['line_throughput_per_hour']-1):+.1f}% "
              "throughput change. These factors multiply; their percentages do not add.", "small")
    paragraph("Same customer basket, different rack grouping", "heading")
    paragraph("These matched store/day cases retain identical requested SKUs and source lines.", "small")
    for cap, examples in data["examples"].items():
        example = examples["fewer_presentations"] or examples["more_presentations"]
        if example:
            paragraph(f"<b>{escape(example['task_id'])}, limit {cap}:</b> SKUs {', '.join(example['skus'])}. "
                      f"Completed visits: original {len(example['old'])}; revised {len(example['new'])}. "
                      + (f"These are {len(example['skus'])} SKUs within a {example['basket_sku_count']}-SKU basket; "
                         f"the complete basket needs {example['old_full_basket_visits']} visits originally and "
                         f"{example['new_full_basket_visits']} now." if example.get("scope") else ""))
    counterexample = data["examples"][str(winner["cap"])]["more_presentations"]
    if counterexample is None:
        counterexample = next((row["more_presentations"] for row in data["examples"].values()
                               if row["more_presentations"] is not None), None)
    if counterexample:
        paragraph(f"<b>Counterexample, {escape(counterexample['task_id'])}, limit {counterexample['cap']}:</b> "
                  f"SKUs {', '.join(counterexample['skus'])}; the revised layout needs "
                  f"{len(counterexample['new'])} visits instead of {len(counterexample['old'])}. "
                  "Slotting can help one basket and hurt another; the complete daily results determine the overall effect.")
    paragraph("Three additional dates", "heading")
    extra = data["additional"]
    table(["Date", "Original lines/h", "Revised lines/h", "Change"],
          [[old["date"], f"{old['line_throughput_per_hour']:,.0f}", f"{new['line_throughput_per_hour']:,.0f}",
            f"{100*(new['line_throughput_per_hour']/old['line_throughput_per_hour']-1):+.1f}%"]
           for old, new in zip(extra["old"], extra["new"])], [130, 114, 114, 114])
    paragraph("These dates were excluded from limit selection but were included in historical affinity training. "
              "They check robustness across more dates, not performance on unseen customer data.", "small")
    if not data["orders_input_verification"]["original_full_training_content_verified"]:
        paragraph("Input check: the workbook binary differs from the saved benchmark. All original-date store/SKU/line-count tasks "
                  "match its saved workload exactly, verified against a direct workbook read. Equality of the complete historical "
                  "training contents is unverified because the original binary is unavailable.", "small")
    paragraph("Design and model limits", "heading")
    paragraph("Throughput is completed source lines divided by simulated hours from release through the final rack return. "
              "Startup and finishing task groups affect that average. Preserve stock, form useful rack groups, then check zone workload "
              "and traffic with the actual dispatcher. Design against representative demanding days and daily spread. "
              "Picking is fixed at 10 seconds per visit regardless of lines served; customer capacity estimates need measured picking times.")

    def footer(canvas, document):
        canvas.setFont("Report", 8)
        canvas.setFillColor(colors.HexColor("#52616D"))
        canvas.drawString(48, 23, "Affinity rack grouping and zone balance | Controlled simulation comparison")
        canvas.drawRightString(564, 23, str(document.page))

    destination.parent.mkdir(parents=True, exist_ok=True)
    SimpleDocTemplate(str(destination), pagesize=(612, 792), leftMargin=48, rightMargin=48,
                      topMargin=40, bottomMargin=42, title="Affinity slotting: measured results").build(
                          story, onFirstPage=footer, onLaterPages=footer)


def build_report(output):
    data = collect(output)
    (output / "result_analysis.json").write_text(json.dumps(data, indent=2) + "\n")
    make_pdf(data, ROOT / "output/pdf/Affinity_rack_balance_revision_results.pdf")
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "current_heat_simulation/results/affinity_rack_balance_revision")
    args = parser.parse_args()
    build_report(args.output)


if __name__ == "__main__":
    main()
