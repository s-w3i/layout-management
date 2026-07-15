"""Inventory lookup and mock movement operations."""

from __future__ import annotations

from .slotting import SlottingService


class InventoryService:
    """Search assignments and apply SKU-slot or AMR-shelf swaps."""

    LOCATION_FIELDS = (
        "static_address", "rmf_grid_address", "zone_id", "aisle_id",
        "static_bay_id", "rack_id", "rack_waypoint", "pickup_dispenser_id",
        "rack_vertex_index", "rack_rank", "handling_unit_type",
        "handling_unit_id", "dynamic_address_level", "dynamic_address",
        "storage_level", "storage_slot", "workstations_evaluated",
        "average_workstation_distance_m",
    )
    STATIC_PROFILE_FIELDS = (
        "rmf_grid_address", "zone_id", "aisle_id", "static_bay_id", "rack_id",
        "rack_waypoint", "pickup_dispenser_id", "rack_vertex_index", "rack_rank",
        "workstations_evaluated", "average_workstation_distance_m",
    )

    def __init__(self, slotting: SlottingService | None = None):
        self.slotting = slotting or SlottingService()

    @staticmethod
    def find_sku(rows: list[dict], sku: str) -> dict:
        wanted = sku.strip().lower()
        exact = next(
            (row for row in rows if str(row.get("sku", "")).lower() == wanted),
            None,
        )
        if exact:
            return exact
        matches = [
            row for row in rows
            if wanted and wanted in str(row.get("sku", "")).lower()
        ]
        if not matches:
            raise ValueError(f"SKU not found: {sku}")
        return matches[0]

    def swap_sku_slots(
        self, rows: list[dict], first_sku: str, second_sku: str
    ) -> tuple[dict, dict]:
        first = self.find_sku(rows, first_sku)
        second = self.find_sku(rows, second_sku)
        if first is second:
            raise ValueError("select two different SKUs")
        first_location = {field: first.get(field, "") for field in self.LOCATION_FIELDS}
        second_location = {field: second.get(field, "") for field in self.LOCATION_FIELDS}
        for field in self.LOCATION_FIELDS:
            first[field] = second_location[field]
            second[field] = first_location[field]
        return first, second

    def swap_whole_shelves(
        self, rows: list[dict], first_sku: str, second_sku: str
    ) -> tuple[str, str, int, int]:
        first = self.find_sku(rows, first_sku)
        second = self.find_sku(rows, second_sku)
        first_unit = first.get("handling_unit_id", "")
        second_unit = second.get("handling_unit_id", "")
        if not first_unit or not second_unit:
            raise ValueError("the two SKUs must belong to different handling units")
        return self.swap_whole_shelf_units(rows, first_unit, second_unit)

    def swap_whole_shelf_units(
        self, rows: list[dict], first_unit: str, second_unit: str
    ) -> tuple[str, str, int, int]:
        first_unit, second_unit = first_unit.strip(), second_unit.strip()
        if not first_unit or not second_unit or first_unit == second_unit:
            raise ValueError("select two different shelves")
        first_rows = [row for row in rows if row.get("handling_unit_id") == first_unit]
        second_rows = [row for row in rows if row.get("handling_unit_id") == second_unit]
        if not first_rows:
            raise ValueError(f"shelf not found: {first_unit}")
        if not second_rows:
            raise ValueError(f"shelf not found: {second_unit}")
        if any(
            row.get("handling_unit_type") != "AMR shelf"
            for row in first_rows + second_rows
        ):
            raise ValueError("whole-shelf swap is only available for AMR shelf layouts")

        first_profile = self._static_profile(first_rows[0])
        second_profile = self._static_profile(second_rows[0])
        for row in first_rows:
            self._apply_shelf_profile(row, second_profile)
        for row in second_rows:
            self._apply_shelf_profile(row, first_profile)
        return first_unit, second_unit, len(first_rows), len(second_rows)

    def _static_profile(self, row: dict) -> dict:
        return {field: row.get(field, "") for field in self.STATIC_PROFILE_FIELDS}

    def _apply_shelf_profile(self, row: dict, profile: dict) -> None:
        row.update(profile)
        level = int(row.get("storage_level") or 1)
        slot = int(row.get("storage_slot") or 1)
        row["static_address"] = (
            f"{row['zone_id']}/{row['aisle_id']}/{row['static_bay_id']}"
            f"/L{level:02d}/S{slot:02d}"
        )
        row["dynamic_address"], row["dynamic_address_level"] = (
            self.slotting.build_dynamic_address(
                row["zone_id"],
                row["aisle_id"],
                row["static_bay_id"],
                level,
                slot,
                row.get("handling_unit_type", "AMR shelf"),
                row["handling_unit_id"],
            )
        )
