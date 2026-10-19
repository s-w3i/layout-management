"""Rack selection shared by slotting estimates and AMR task dispatch."""


def rack_selection_key(covered_sku_count: int, distance: float, rack_id: str) -> tuple:
    return -covered_sku_count, distance, rack_id
