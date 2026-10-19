"""Balance complete AMR rack groups after affinity packing."""

from collections import defaultdict
import json
import math

from ..storage_planning import combined_occupied_dynamic_address
from .affinity_support import allocate_rack_visits


def rack_workload(rows, positions, order_masks, group_count, *, speed_mps=1.5, service_seconds=18.0):
    """Travel/handling estimate; excludes queueing, turns and robot availability."""
    _touches, visits, uncovered = allocate_rack_visits(rows, order_masks, group_count)
    racks = {str(position["rack_id"]): position for position in positions}
    zones = defaultdict(lambda: {"rack_capacity": 0, "presentations": 0, "estimated_work_seconds": 0.0})
    for rack in racks.values():
        zones[str(rack["zone_id"])]["rack_capacity"] += 1
    rack_seconds = {}
    for rack_id, rack in racks.items():
        distance = float(rack["distance_m"])
        count = visits.get(rack_id, 0)
        work = count * (2 * distance / speed_mps + service_seconds) if count else 0.0
        rack_seconds[rack_id] = work
        zone = zones[str(rack["zone_id"])]
        zone["presentations"] += count
        zone["estimated_work_seconds"] += work
    for zone in zones.values():
        zone["normalized_work_seconds"] = zone["estimated_work_seconds"] / zone["rack_capacity"]
    return {
        "model": "joint_rack_presentations_round_trip_mean_station_distance_plus_handling",
        "speed_mps": speed_mps, "service_seconds_per_presentation": service_seconds,
        "uncovered_group_count": uncovered, "total_presentations": sum(visits.values()),
        "estimated_work_seconds": sum(rack_seconds.values()),
        "maximum_normalized_zone_work_seconds": max((zone["normalized_work_seconds"] for zone in zones.values()), default=0.0),
        "zones": dict(sorted(zones.items())), "rack_visit_counts": visits,
    }


def relocate_rack_rows(rows, destination, level_name):
    """Preserve a rack's complete inventory and occupied cell coordinates."""
    result = []
    for source in rows:
        row = dict(source)
        anchor = destination[int(row["storage_level"]), int(row["storage_slot"])]
        units = row.get("occupied_handling_units") or [{
            "storage_level": row["storage_level"], "storage_slot": row["storage_slot"],
        }]
        cells = [destination[int(unit["storage_level"]), int(unit["storage_slot"])] for unit in units]
        for key in (
            "static_address", "storage_location_address", "buffer_id", "buffer_level",
            "zone_id", "aisle_id", "static_bay_id", "rack_id", "pickup_dispenser_id",
            "rack_rank", "handling_unit_type", "handling_unit_id", "dynamic_address_level",
            "dynamic_address", "storage_area_type", "planned_zone_id", "planned_storage_type",
            "effective_location_attributes",
        ):
            row[key] = anchor[key]
        row.update(
            rack_waypoint=anchor["waypoint"], rack_vertex_index=anchor["vertex_index"],
            rmf_grid_address=f"{level_name}/{anchor['waypoint']}",
            generated_attribute_zone_id=anchor["zone_id"],
            workstations_evaluated="|".join(anchor["workstations"]),
            average_workstation_distance_m=round(anchor["distance_m"], 3) if math.isfinite(anchor["distance_m"]) else "",
            routing_status="REACHABLE" if math.isfinite(anchor["distance_m"]) else "UNREACHABLE_LAST_RESORT",
            occupied_static_addresses=sorted({cell["static_address"] for cell in cells}),
            occupied_buffer_ids=sorted({cell["buffer_id"] for cell in cells}),
            occupied_storage_location_addresses=[cell["storage_location_address"] for cell in cells],
            occupied_handling_units=[{
                "handling_unit_id": cell["handling_unit_id"], "rack_id": cell["rack_id"],
                "storage_level": cell["level"], "storage_slot": cell["slot"],
                "buffer_id": cell["buffer_id"], "static_address": cell["static_address"],
                "storage_location_address": cell["storage_location_address"],
            } for cell in cells],
        )
        row["occupied_dynamic_address"] = combined_occupied_dynamic_address(row)
        result.append(row)
    return result


def balance_rack_groups(rows, positions, order_masks, group_count, level_name):
    """Assign intact groups to equivalent physical racks, then verify the estimate."""
    rack_positions = defaultdict(dict)
    for position in positions:
        rack_positions[str(position["rack_id"])][int(position["level"]), int(position["slot"])] = position
    rack_rows = defaultdict(list)
    for row in rows:
        if row.get("assignment_status") == "ASSIGNED":
            rack_rows[str(row["rack_id"])].append(row)
    before = rack_workload(rows, positions, order_masks, group_count)
    pools = defaultdict(list)
    signature_by_rack = {}
    for rack, cells in rack_positions.items():
        # shortcut: move only between identical cell profiles; broader relocation
        # needs the full constrained-footprint planner, including level rules.
        signature = tuple((coordinate, json.dumps(cell["effective_location_attributes"], sort_keys=True), cell["planned_storage_type"])
                          for coordinate, cell in sorted(cells.items()))
        signature_by_rack[rack] = signature
        pools[signature].append(rack)
    compatible_racks = {rack: tuple(pools[signature]) for rack, signature in signature_by_rack.items()}
    capacities = {zone: value["rack_capacity"] for zone, value in before["zones"].items()}
    workloads = {zone: 0.0 for zone in capacities}
    mapping = {}
    for source in sorted(rack_rows, key=lambda rack: (
        len(pools[signature_by_rack[rack]]), -before["rack_visit_counts"].get(rack, 0), rack,
    )):
        visits = before["rack_visit_counts"].get(source, 0)

        def candidate_key(rack):
            cell = next(iter(rack_positions[rack].values()))
            zone = str(cell["zone_id"])
            distance = float(cell["distance_m"])
            seconds = visits * (2 * distance / before["speed_mps"] + before["service_seconds_per_presentation"]) if visits else 0.0
            return ((workloads[zone] + seconds) / capacities[zone], seconds, rack)

        pool = pools[signature_by_rack[source]]
        destination = min(pool, key=candidate_key)
        pool.remove(destination)
        mapping[source] = destination
        cell = next(iter(rack_positions[destination].values()))
        workloads[str(cell["zone_id"])] += candidate_key(destination)[1]
    def mapped_rows(candidate_mapping):
        relocated = {
            id(row): replacement
            for source, destination in candidate_mapping.items()
            for row, replacement in zip(rack_rows[source], relocate_rack_rows(rack_rows[source], rack_positions[destination], level_name))
        }
        return [relocated.get(id(row), row) for row in rows]

    proposed = mapped_rows(mapping)
    after = rack_workload(proposed, positions, order_masks, group_count)

    def improves(candidate):
        return (
            candidate["uncovered_group_count"] <= before["uncovered_group_count"]
            and candidate["total_presentations"] <= before["total_presentations"]
            and (candidate["maximum_normalized_zone_work_seconds"], candidate["estimated_work_seconds"])
            < (before["maximum_normalized_zone_work_seconds"], before["estimated_work_seconds"])
        )

    accepted = improves(after)
    global_proposal = after
    local_trials = []
    selected_proposal = "whole_map" if accepted else "none"
    if not accepted and before["zones"]:
        peak_zone = max(before["zones"], key=lambda zone: before["zones"][zone]["normalized_work_seconds"])
        rack_cells = {rack: next(iter(cells.values())) for rack, cells in rack_positions.items()}
        visits = before["rack_visit_counts"]

        def cycle_work(rack, count):
            return count * (2 * float(rack_cells[rack]["distance_m"]) / before["speed_mps"]
                            + before["service_seconds_per_presentation"]) if count else 0.0

        candidates = {}
        sources = sorted((rack for rack in rack_rows if str(rack_cells[rack]["zone_id"]) == peak_zone),
                         key=lambda rack: (-visits.get(rack, 0), rack))[:8]
        for source in sources:
            for target in compatible_racks[source]:
                source_zone, target_zone = str(rack_cells[source]["zone_id"]), str(rack_cells[target]["zone_id"])
                if source_zone == target_zone:
                    continue
                projected = {zone: value["estimated_work_seconds"] for zone, value in before["zones"].items()}
                source_visits, target_visits = visits.get(source, 0), visits.get(target, 0)
                projected[source_zone] += cycle_work(source, target_visits) - cycle_work(source, source_visits)
                projected[target_zone] += cycle_work(target, source_visits) - cycle_work(target, target_visits)
                maximum = max(projected[zone] / capacities[zone] for zone in projected)
                if maximum >= before["maximum_normalized_zone_work_seconds"]:
                    continue
                score = (maximum, abs(float(rack_cells[source]["distance_m"]) - float(rack_cells[target]["distance_m"])),
                         sum(projected.values()), source, target)
                pair = source, target_zone
                if pair not in candidates or score < candidates[pair]:
                    candidates[pair] = score
        # shortcut: verify at most eight promising swaps; deepen this search if
        # rejected candidates leave a measured zone bottleneck worth solving.
        for *_score, source, target in sorted(candidates.values())[:8]:
            local_mapping = {rack: rack for rack in rack_rows}
            local_mapping[source] = target
            if target in rack_rows:
                local_mapping[target] = source
            candidate_rows = mapped_rows(local_mapping)
            candidate_work = rack_workload(candidate_rows, positions, order_masks, group_count)
            local_trials.append({"source": source, "target": target,
                                 "presentations": candidate_work["total_presentations"],
                                 "maximum_normalized_zone_work_seconds": candidate_work["maximum_normalized_zone_work_seconds"],
                                 "accepted": improves(candidate_work)})
            if local_trials[-1]["accepted"]:
                proposed, after, mapping, accepted = candidate_rows, candidate_work, local_mapping, True
                selected_proposal = "verified_pair_swap"
                break
    return (proposed if accepted else rows), {
        "method": "whole_rack_groups_equivalent_physical_profiles",
        "accepted": accepted, "moved_rack_group_count": sum(source != target for source, target in mapping.items()) if accepted else 0,
        "before": before, "after": after if accepted else before,
        "proposed": global_proposal, "selected_proposal": selected_proposal, "local_search_trials": local_trials,
        "rack_mapping": mapping if accepted else {rack: rack for rack in rack_rows},
        "placement_diagnostics": "affinity_scores_recorded_before_whole_rack_relocation",
    }
