"""Optional capacity-normalized zone preference after hard feasibility checks."""

import math


def balanced_zone_candidates(candidates, workloads, capacities, incoming_demand):
    """Keep all ties so the selected strategy still decides rack and slot."""
    zones = {str(record[2]["zone_id"]) for record in candidates}
    projected = {
        zone: (workloads.get(zone, 0.0) + incoming_demand) / capacities[zone]
        for zone in zones
    }
    lowest = min(projected.values())
    return [
        record for record in candidates
        if math.isclose(projected[str(record[2]["zone_id"])], lowest, rel_tol=1e-12, abs_tol=1e-12)
    ]
