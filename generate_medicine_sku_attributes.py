#!/usr/bin/env python3
"""Generate deterministic demo medicine attributes from the picking workbook."""

from __future__ import annotations

import argparse
import csv
import random
from collections import defaultdict
from pathlib import Path

from sku_velocity_analysis import read_transactions


DEFAULT_INPUT = Path("resources/data/Sample Data.xlsx")
DEFAULT_OUTPUT = Path("resources/data/medicine_sku_attributes.csv")
def generate(input_path: Path, output_path: Path, seed: int = 42) -> list[dict]:
    physical_maxima: dict[str, dict[str, float]] = defaultdict(dict)
    sku_ids_seen: set[str] = set()
    for sku, _picked_date, _quantity, physical in read_transactions(input_path):
        sku_ids_seen.add(sku)
        for key in (
            "max_item_length", "max_item_width", "max_item_height",
            "max_item_weight",
        ):
            value = physical.get(key)
            if value is not None:
                physical_maxima[sku][key] = max(
                    value, physical_maxima[sku].get(key, value)
                )

    sku_ids = sorted(sku_ids_seen)
    chilled_count = round(len(sku_ids) * 0.10)
    chilled = set(random.Random(seed).sample(sku_ids, chilled_count))
    assignment_rng = random.Random(seed + 1)
    rows = []
    for sku in sku_ids:
        tablet = assignment_rng.random() < 0.55
        # A deterministic non-tablet subset exercises the flammable-area flag
        # without assigning a categorical dosage form to the SKU.
        flammable = not tablet and assignment_rng.random() < 0.20
        dimensions = physical_maxima[sku]
        rows.append({
            "sku": sku,
            "max_item_length": dimensions.get("max_item_length", ""),
            "max_item_width": dimensions.get("max_item_width", ""),
            "max_item_height": dimensions.get("max_item_height", ""),
            "max_item_weight": dimensions.get("max_item_weight", ""),
            "chilled": str(sku in chilled).lower(),
            "tablet": str(tablet).lower(),
            "flammable": str(flammable).lower(),
        })

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=(
                "sku", "max_item_length", "max_item_width", "max_item_height",
                "max_item_weight", "chilled", "tablet", "flammable",
            ),
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    rows = generate(args.input, args.output, args.seed)
    print(f"Generated {len(rows):,} SKU attribute rows: {args.output}")


if __name__ == "__main__":
    main()
