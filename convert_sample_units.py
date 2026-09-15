#!/usr/bin/env python3
"""Convert the sample workbook from cm/g to warehouse m/kg units."""

from __future__ import annotations

import argparse
from decimal import Decimal
import json
from pathlib import Path
import re
import shutil
import tempfile
from zipfile import ZIP_DEFLATED, ZipFile

from generate_medicine_sku_attributes import generate
from warehouse_layout.config import (
    DEFAULT_MACHINE_CAPACITY_BY_SYSTEM,
    DEFAULT_SLOT_CAPACITY,
)


DEFAULT_WORKBOOK = Path("resources/data/Sample Data.xlsx")
DEFAULT_ATTRIBUTES = Path("resources/data/medicine_sku_attributes.csv")
SHEET_PATH = "xl/worksheets/sheet1.xml"
SHARED_STRINGS_PATH = "xl/sharedStrings.xml"
CELL_PATTERN = re.compile(
    rb'<c r="([JKLM])(\d+)"([^>]*)>(.*?)</c>', re.DOTALL
)
VALUE_PATTERN = re.compile(rb"<v>([^<]+)</v>")


def _scaled_number(raw: bytes, divisor: Decimal) -> bytes:
    value = Decimal(raw.decode("ascii")) / divisor
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return (text or "0").encode("ascii")


def _convert_cell(match: re.Match[bytes]) -> bytes:
    column, row, attributes, body = match.groups()
    if int(row) < 5:
        return match.group(0)
    value_match = VALUE_PATTERN.search(body)
    if value_match is None:
        return match.group(0)
    divisor = Decimal(1000 if column == b"M" else 100)
    scaled = _scaled_number(value_match.group(1), divisor)
    body = body[:value_match.start(1)] + scaled + body[value_match.end(1):]
    return b'<c r="' + column + row + b'"' + attributes + b">" + body + b"</c>"


def _convert_sheet(source, target) -> None:
    buffer = b""
    while True:
        chunk = source.read(8 * 1024 * 1024)
        if not chunk:
            break
        buffer += chunk
        boundary = buffer.rfind(b"</c>")
        if boundary < 0:
            continue
        boundary += len(b"</c>")
        complete, buffer = buffer[:boundary], buffer[boundary:]
        complete = CELL_PATTERN.sub(_convert_cell, complete)
        complete = complete.replace(
            b"0.7*40*30*30", b"0.7*0.4*0.3*0.3"
        )
        complete = complete.replace(b"&gt;60", b"&gt;0.6")
        complete = complete.replace(b"&gt;40", b"&gt;0.4")
        complete = complete.replace(b"&gt;30", b"&gt;0.3")
        target.write(complete)
    if buffer:
        target.write(CELL_PATTERN.sub(_convert_cell, buffer))


def convert_workbook(path: Path) -> bool:
    """Convert in place and return False when it is already metric."""
    path = Path(path)
    with ZipFile(path) as workbook:
        shared_strings = workbook.read(SHARED_STRINGS_PATH)
        if b"<t>Length (m)</t>" in shared_strings:
            return False

    with tempfile.TemporaryDirectory(dir=path.parent) as directory:
        temporary = Path(directory) / path.name
        with ZipFile(path, "r") as source, ZipFile(
            temporary, "w", compression=ZIP_DEFLATED, compresslevel=6
        ) as target:
            for info in source.infolist():
                if info.filename == SHEET_PATH:
                    with source.open(info) as stream, target.open(info, "w") as output:
                        _convert_sheet(stream, output)
                elif info.filename == SHARED_STRINGS_PATH:
                    content = source.read(info)
                    for old, new in (
                        (b"<t>Length</t>", b"<t>Length (m)</t>"),
                        (b"<t>Width</t>", b"<t>Width (m)</t>"),
                        (b"<t>Height</t>", b"<t>Height (m)</t>"),
                        (b"<t>Weight</t>", b"<t>Weight (kg)</t>"),
                    ):
                        content = content.replace(old, new, 1)
                    target.writestr(info, content)
                else:
                    with source.open(info) as stream, target.open(info, "w") as output:
                        shutil.copyfileobj(stream, output, length=8 * 1024 * 1024)
        temporary.replace(path)
    return True


def update_grid_projects(directory: Path) -> int:
    """Move bundled warehouse projects to metric defaults and units."""
    updated = 0
    for path in sorted(Path(directory).glob("*.grid.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        already_metric = any(
            definition.get("key") == "max_item_length"
            and definition.get("unit") == "m"
            for definition in payload.get("attribute_catalog") or []
        )
        system_type = (payload.get("storage_layout") or {}).get(
            "system_type", "AMR"
        )
        payload["warehouse_storage_defaults"] = dict(DEFAULT_SLOT_CAPACITY)
        machine = dict(DEFAULT_MACHINE_CAPACITY_BY_SYSTEM[system_type])
        payload["machine_carrying_capacity"] = machine
        if payload.get("storage_layout"):
            payload["storage_layout"]["machine_carrying_capacity"] = machine
        for definition in payload.get("attribute_catalog") or []:
            key = definition.get("key")
            if key in {"max_item_length", "max_item_width", "max_item_height"}:
                definition["unit"] = "m"
            elif key == "max_item_weight":
                definition["unit"] = "kg"
        for hierarchy_path, values in (
            payload.get("location_attributes") or {}
        ).items():
            if "/" not in hierarchy_path:
                values.update(DEFAULT_SLOT_CAPACITY)
                continue
            if not already_metric:
                for key in ("max_item_length", "max_item_width", "max_item_height"):
                    if key in values and values[key] not in (None, ""):
                        values[key] = float(values[key]) / 100
                if (
                    "max_item_weight" in values
                    and values["max_item_weight"] not in (None, "")
                ):
                    values["max_item_weight"] = (
                        float(values["max_item_weight"]) / 1000
                    )
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        updated += 1
    return updated


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workbook", type=Path, default=DEFAULT_WORKBOOK)
    parser.add_argument("--attributes", type=Path, default=DEFAULT_ATTRIBUTES)
    parser.add_argument(
        "--grid-project-dir", type=Path, default=Path("resources/map")
    )
    args = parser.parse_args()
    converted = convert_workbook(args.workbook)
    rows = generate(args.workbook, args.attributes)
    project_count = update_grid_projects(args.grid_project_dir)
    print("Converted workbook to m/kg." if converted else "Workbook already uses m/kg.")
    print(f"Regenerated {len(rows):,} SKU attributes: {args.attributes}")
    print(f"Updated {project_count:,} grid projects to metric warehouse units.")


if __name__ == "__main__":
    main()
