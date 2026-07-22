"""Persistence for self-contained inventory slotting layouts."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from .attributes import (
    OVERSIZE_STORAGE_DEFAULTS,
    PHYSICAL_ATTRIBUTE_KEYS,
    STANDARD_STORAGE_DEFAULTS,
    StorageAttributeService,
)
from .config import LEGACY_SLOTTING_SCHEMA, SLOTTING_SCHEMA
from .storage_planning import derive_zone_storage_types


class SlottingLayoutRepository:
    """Read and write self-contained inventory slotting layout documents."""

    def save(
        self,
        rows: list[dict],
        building: dict,
        summary: dict,
        output_path: Path,
        *,
        strategy: str,
        handling_unit_type: str,
        levels_per_rack: int,
        slots_per_level: int,
        zone_assignments: dict[str, str],
        attribute_catalog=None,
        location_attributes: dict[str, dict] | None = None,
        source_building: str = "",
        source_grid_project: str = "",
        source_velocity: str = "",
        source_chilled: str = "",
        source_affinity: str = "",
        affinity_configuration: dict | None = None,
        standard_storage_defaults: dict | None = None,
        oversize_storage_defaults: dict | None = None,
        chilled_demo_rate: float = 0.10,
        chilled_demo_seed: int = 42,
        storage_layout=None,
    ) -> None:
        occupied_units: dict[str, set[str]] = {}
        for row in rows:
            if row.get("assignment_status") != "ASSIGNED":
                continue
            unit_id = str(row.get("handling_unit_id", ""))
            for buffer_id in row.get("occupied_buffer_ids", []):
                occupied_units.setdefault(str(buffer_id), set()).add(unit_id)
        buffer_records = []
        if storage_layout is not None:
            for source in storage_layout.buffers:
                buffer_id = str(source.get("buffer_id", ""))
                buffer_records.append({
                    **dict(source),
                    "status": "OCCUPIED" if buffer_id in occupied_units else "EMPTY",
                    "handling_unit_ids": sorted(
                        unit for unit in occupied_units.get(buffer_id, set()) if unit
                    ),
                })
        payload = {
            "schema": SLOTTING_SCHEMA,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "strategy": strategy,
            "handling_unit_type": handling_unit_type,
            "rack_capacity": {
                "levels": levels_per_rack,
                "slots_per_level": slots_per_level,
            },
            "sources": {
                "building_yaml": source_building,
                "grid_project_json": source_grid_project,
                "sku_velocity_csv": source_velocity,
                "chilled_requirements_csv": source_chilled,
                "affinity_order_workbook": source_affinity,
            },
            "affinity_configuration": affinity_configuration or {},
            "storage_defaults": {
                "standard": standard_storage_defaults or STANDARD_STORAGE_DEFAULTS,
                "oversize": oversize_storage_defaults or OVERSIZE_STORAGE_DEFAULTS,
            },
            "chilled_requirements": {
                "missing_sku_is_ambient": True,
                "demo_rate": chilled_demo_rate,
                "demo_seed": chilled_demo_seed,
            },
            "zone_assignments": zone_assignments,
            "storage_layout": (
                storage_layout.to_dict() if storage_layout is not None else {}
            ),
            "buffers": buffer_records,
            "attribute_catalog": StorageAttributeService.serialize_catalog(
                attribute_catalog
            ),
            "location_attributes": location_attributes or {},
            "summary": summary,
            "building": building,
            "assignments": rows,
            "operation_log": [],
        }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def load(self, path: Path) -> dict:
        payload = json.loads(path.read_text(encoding="utf-8"))
        schema = payload.get("schema")
        if schema not in {SLOTTING_SCHEMA, LEGACY_SLOTTING_SCHEMA}:
            raise ValueError("not a supported inventory slotting layout")
        if not isinstance(payload.get("building"), dict) or not isinstance(
            payload.get("assignments"), list
        ):
            raise ValueError("slotting layout is missing building or assignment data")
        if schema == LEGACY_SLOTTING_SCHEMA:
            payload["source_schema"] = LEGACY_SLOTTING_SCHEMA
            payload["schema"] = SLOTTING_SCHEMA
            payload.setdefault("attribute_catalog", [])
            payload.setdefault("location_attributes", {})
        payload.setdefault("sources", {})
        payload["sources"].setdefault("grid_project_json", "")
        payload["sources"].setdefault("chilled_requirements_csv", "")
        payload["sources"].setdefault("affinity_order_workbook", "")
        payload.setdefault("affinity_configuration", {})
        payload.setdefault("storage_layout", {})
        payload.setdefault("buffers", [])
        payload.setdefault("storage_defaults", {
            "standard": STANDARD_STORAGE_DEFAULTS,
            "oversize": {},
        })
        payload.setdefault("chilled_requirements", {
            "missing_sku_is_ambient": True,
            "demo_rate": 0.10,
            "demo_seed": 42,
        })
        attributes = StorageAttributeService()
        catalog = attributes.normalize_catalog(payload.get("attribute_catalog"))
        payload["attribute_catalog"] = attributes.serialize_catalog(catalog)
        payload["location_attributes"] = attributes.validate_location_attributes(
            payload.get("location_attributes"), catalog
        )
        for row in payload["assignments"]:
            requirements = row.setdefault("sku_requirements", {})
            if not isinstance(requirements, dict):
                raise ValueError("assignment sku_requirements must be an object")
            row["sku_requirements"] = attributes.validate_requirements(
                requirements, catalog
            )
            row.setdefault(
                "compatibility_status",
                "COMPATIBLE"
                if row.get("assignment_status") == "ASSIGNED"
                else "NOT_EVALUATED",
            )
            row.setdefault("compatibility_issues", [])
            row.setdefault("auto_attribute_overrides", {})
            row.setdefault("routing_status", "NOT_EVALUATED")
            if self._physical_requirements_present(row["sku_requirements"]):
                profile = attributes.physical_profile(row["sku_requirements"])
                row.setdefault("physical_data_status", profile["data_status"])
                row.setdefault("physical_storage_class", profile["storage_class"])
                row.setdefault("physical_missing_fields", profile["missing_fields"])
            else:
                row.setdefault("physical_data_status", "NOT_EVALUATED")
                row.setdefault("physical_storage_class", "NOT_EVALUATED")
                row.setdefault("physical_missing_fields", [])
            row.setdefault("effective_location_attributes", {})
            row.setdefault("storage_location_address", row.get("static_address", ""))
            row.setdefault("buffer_id", row.get("static_bay_id", ""))
            row.setdefault(
                "buffer_level",
                "grid" if row.get("handling_unit_type") == "AMR shelf" else "slot",
            )
            row.setdefault(
                "occupied_slot_count",
                1 if row.get("assignment_status") == "ASSIGNED" else 0,
            )
            row.setdefault(
                "occupied_level_span",
                1 if row.get("assignment_status") == "ASSIGNED" else 0,
            )
            row.setdefault(
                "occupied_horizontal_slot_span",
                int(row.get("occupied_slot_count") or 1)
                if row.get("assignment_status") == "ASSIGNED" else 0,
            )
            row.setdefault(
                "occupied_static_addresses",
                [row.get("static_address", "")]
                if row.get("assignment_status") == "ASSIGNED" else [],
            )
            row.setdefault(
                "occupied_buffer_ids",
                [row.get("buffer_id", "")]
                if row.get("assignment_status") == "ASSIGNED" else [],
            )
            row.setdefault(
                "occupied_storage_location_addresses",
                list(row.get("occupied_static_addresses", [])),
            )
            row.setdefault("rack_frequency_rank", "")
            row.setdefault("rack_pick_frequency", "")
            row.setdefault("rack_frequency_share", "")
            row.setdefault("rack_cumulative_frequency_share", "")
            row.setdefault("rack_velocity_class", "")
        zones = set(
            payload.get("summary", {}).get("zone_storage_types", {}).keys()
        )
        payload.setdefault("summary", {})["zone_storage_types"] = (
            derive_zone_storage_types(payload["assignments"], zones)
        )
        return payload

    @staticmethod
    def _physical_requirements_present(requirements: dict) -> bool:
        return any(key in requirements for key in PHYSICAL_ATTRIBUTE_KEYS)

    @staticmethod
    def save_payload(payload: dict, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    @staticmethod
    def save_csv(rows: list[dict], path: Path) -> None:
        if not rows:
            raise ValueError("there are no slotting rows to write")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
