#!/usr/bin/env python3
"""Calculate minimum and buffer stock for every SKU in an order workbook."""

from __future__ import annotations

import argparse
from pathlib import Path

from warehouse_layout.attributes import StorageAttributeService
from warehouse_layout.config import (
    DEFAULT_AFFINITY_INPUT,
    DEFAULT_MACHINE_CAPACITY_BY_SYSTEM,
    DEFAULT_SLOT_CAPACITY,
    DEFAULT_SKU_ATTRIBUTES_INPUT,
    PROJECT_ROOT,
)
from warehouse_layout.rmf import RmfMapService
from warehouse_layout.slotting import SlottingService
from warehouse_layout.stock_determination import (
    calculate_rack_requirements,
    determine_stock_requirements,
    write_attribute_combination_csv,
    write_stock_requirements_csv,
)


DEFAULT_OUTPUT = PROJECT_ROOT / "resources/data/minimum_stock_requirements.csv"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_AFFINITY_INPUT)
    parser.add_argument("--minimum-days", type=int, default=2)
    parser.add_argument("--buffer-days", type=int, default=1)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--sku-attributes", type=Path, default=DEFAULT_SKU_ATTRIBUTES_INPUT
    )
    parser.add_argument(
        "--slot-length", type=float,
        default=DEFAULT_SLOT_CAPACITY["max_item_length"],
    )
    parser.add_argument(
        "--slot-width", type=float,
        default=DEFAULT_SLOT_CAPACITY["max_item_width"],
    )
    parser.add_argument(
        "--slot-height", type=float,
        default=DEFAULT_SLOT_CAPACITY["max_item_height"],
    )
    parser.add_argument(
        "--slot-max-weight", type=float,
        default=None,
        help=(
            "Cumulative kg allowed in one slot; AMR defaults to the whole-rack "
            "limit divided evenly across its slots"
        ),
    )
    parser.add_argument(
        "--warehouse-type",
        choices=tuple(DEFAULT_MACHINE_CAPACITY_BY_SYSTEM),
        default="AMR",
    )
    parser.add_argument("--rack-levels", type=int, default=3)
    parser.add_argument("--slots-per-level", type=int, default=4)
    args = parser.parse_args()
    try:
        rows = determine_stock_requirements(
            args.input, args.minimum_days, args.buffer_days
        )
        attribute_service = StorageAttributeService()
        machine_capacity = DEFAULT_MACHINE_CAPACITY_BY_SYSTEM[
            args.warehouse_type
        ]
        slot_max_weight = args.slot_max_weight
        if slot_max_weight is None:
            slot_max_weight = (
                machine_capacity["max_item_weight"]
                / (args.rack_levels * args.slots_per_level)
                if args.warehouse_type == "AMR"
                else DEFAULT_SLOT_CAPACITY["max_item_weight"]
            )
        handling_unit = {
            "AMR": "AMR shelf",
            "Mini-load ASRS": "Tote",
            "Pallet ASRS": "Pallet",
        }[args.warehouse_type]
        attribute_service.set_machine_carrying_capacity(
            machine_capacity, handling_unit
        )
        slotting = SlottingService(RmfMapService(), attribute_service)
        catalog, summary = slotting.inspect_sku_attribute_csv(args.sku_attributes)
        requirements = slotting.load_sku_attribute_requirements(
            args.sku_attributes,
            {str(row["sku"]) for row in rows},
            catalog,
            include_derived_grouping=True,
        )
        rows, combinations = calculate_rack_requirements(
            rows,
            requirements,
            (args.slot_length, args.slot_width, args.slot_height),
            args.rack_levels,
            args.slots_per_level,
            tuple(
                key for key in ("chilled", "oversize")
                if key in summary["available_combination_attributes"]
            ),
            slot_max_weight=slot_max_weight,
            rack_max_weight=(
                machine_capacity["max_item_weight"]
                if args.warehouse_type == "AMR" else None
            ),
        )
        output = write_stock_requirements_csv(rows, args.output)
        combination_output = args.output.with_name(
            f"{args.output.stem}_attribute_combinations.csv"
        )
        write_attribute_combination_csv(combinations, combination_output)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(f"Calculated stock requirements for {len(rows):,} SKUs.")
    print(f"Attribute combinations: {len(combinations):,}")
    print(f"Output: {output}")
    print(f"Grouped output: {combination_output}")


if __name__ == "__main__":
    main()
