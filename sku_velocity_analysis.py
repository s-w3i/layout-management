#!/usr/bin/env python3
"""Classify SKUs by pick frequency and plot daily demand for every SKU.

The input workbook is expected to contain transaction-level rows with columns
named ``Date``, ``Item or SKU`` and ``Quantity (in EA)``.  Outputs are written
under the selected data directory so the source workbook is not modified.
"""

from __future__ import annotations

import argparse
import csv
import math
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable


DEFAULT_INPUT = Path("resources/data/Sample Data.xlsx")
DEFAULT_OUTPUT = Path("resources/data/sku_velocity_output")


def excel_date(value) -> date | None:
    """Convert an Excel date, datetime, or ISO date to a Python date."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            return None
        return (datetime(1899, 12, 30) + timedelta(days=float(value))).date()
    text = str(value).strip()
    for parser in (datetime.fromisoformat,):
        try:
            return parser(text).date()
        except ValueError:
            pass
    return None


def safe_filename(value: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return name or "blank_sku"


def classify(rows: list[dict], a_limit: float, b_limit: float) -> list[dict]:
    """Assign A/B/C based on cumulative share of pick transactions."""
    rows.sort(key=lambda row: (-row["pick_frequency"], row["sku"]))
    total = sum(row["pick_frequency"] for row in rows) or 1
    cumulative = 0
    for row in rows:
        cumulative += row["pick_frequency"]
        share = cumulative / total
        row["cumulative_frequency_share"] = round(share, 6)
        row["velocity_class"] = "A" if share <= a_limit else "B" if share <= b_limit else "C"
    # Ensure the first SKU is not labelled B/C when a single SKU exceeds A's
    # boundary, and ensure every class boundary remains deterministic.
    if rows and rows[0]["velocity_class"] != "A":
        rows[0]["velocity_class"] = "A"
    return rows


def read_transactions(input_path: Path):
    try:
        from openpyxl import load_workbook
    except ImportError as exc:
        raise SystemExit("Missing dependency: install openpyxl and matplotlib with `pip install openpyxl matplotlib`.") from exc

    workbook = load_workbook(input_path, read_only=True, data_only=True)
    worksheet = workbook.active
    header = None
    rows = worksheet.iter_rows(values_only=True)
    for candidate in rows:
        candidate_names = {str(value).strip() for value in candidate if value is not None}
        if {"Date", "Item or SKU", "Quantity (in EA)"}.issubset(candidate_names):
            header = candidate
            break
    if not header:
        raise ValueError("The workbook is empty.")
    positions = {str(value).strip(): index for index, value in enumerate(header) if value is not None}
    required = {"Date", "Item or SKU", "Quantity (in EA)"}
    missing = required - positions.keys()
    if missing:
        raise ValueError(f"Missing required column(s): {', '.join(sorted(missing))}")

    sku_index, date_index, qty_index = (positions[name] for name in ("Item or SKU", "Date", "Quantity (in EA)"))
    for values in rows:
        sku = values[sku_index] if sku_index < len(values) else None
        picked_date = excel_date(values[date_index] if date_index < len(values) else None)
        quantity = values[qty_index] if qty_index < len(values) else 0
        if sku in (None, "") or picked_date is None:
            continue
        try:
            quantity = float(quantity or 0)
        except (TypeError, ValueError):
            quantity = 0.0
        yield str(sku).strip(), picked_date, quantity
    workbook.close()


def plot_demand(daily_demand: dict[str, dict[date, float]], classes: dict[str, str], output_dir: Path) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise SystemExit("Missing dependency: install openpyxl and matplotlib with `pip install openpyxl matplotlib`.") from exc

    plot_dir = output_dir / "demand_plots"
    plot_dir.mkdir(parents=True, exist_ok=True)
    colors = {"A": "#d62728", "B": "#ff9900", "C": "#2ca02c"}
    for sku, demand_by_day in daily_demand.items():
        days = sorted(demand_by_day)
        quantities = [demand_by_day[day] for day in days]
        fig, axis = plt.subplots(figsize=(10, 4.8))
        axis.plot(days, quantities, color=colors[classes[sku]], linewidth=1.4)
        axis.fill_between(days, quantities, color=colors[classes[sku]], alpha=0.14)
        axis.set_title(f"Daily demand — SKU {sku} (velocity class {classes[sku]})")
        axis.set_xlabel("Date")
        axis.set_ylabel("Quantity picked (EA)")
        axis.grid(alpha=0.25)
        fig.autofmt_xdate()
        fig.tight_layout()
        fig.savefig(plot_dir / f"{safe_filename(sku)}.png", dpi=140)
        plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="Input .xlsx transaction workbook")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Output folder (inside data folder by default)")
    parser.add_argument("--a-limit", type=float, default=0.80, help="Cumulative pick-frequency share ending class A")
    parser.add_argument("--b-limit", type=float, default=0.95, help="Cumulative pick-frequency share ending class B")
    args = parser.parse_args()
    if not 0 < args.a_limit < args.b_limit <= 1:
        parser.error("Require 0 < --a-limit < --b-limit <= 1")
    if not args.input.exists():
        parser.error(f"Input workbook not found: {args.input}")

    frequency = defaultdict(int)
    quantity = defaultdict(float)
    daily_demand = defaultdict(lambda: defaultdict(float))
    for sku, picked_date, qty in read_transactions(args.input):
        frequency[sku] += 1
        quantity[sku] += qty
        daily_demand[sku][picked_date] += qty

    summary = [{"sku": sku, "pick_frequency": frequency[sku], "total_quantity_ea": round(quantity[sku], 4),
                "active_days": len(daily_demand[sku])} for sku in frequency]
    summary = classify(summary, args.a_limit, args.b_limit)
    args.output.mkdir(parents=True, exist_ok=True)
    summary_path = args.output / "sku_velocity_summary.csv"
    fields = ["sku", "pick_frequency", "total_quantity_ea", "active_days", "cumulative_frequency_share", "velocity_class"]
    with summary_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(summary)
    classes = {row["sku"]: row["velocity_class"] for row in summary}
    plot_demand(daily_demand, classes, args.output)
    print(f"Processed {sum(frequency.values()):,} picks across {len(summary):,} SKUs.")
    print(f"Summary: {summary_path}")
    print(f"Plots:   {args.output / 'demand_plots'}")


if __name__ == "__main__":
    main()
