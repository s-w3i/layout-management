# Traffic-Aware Slotting

The **Traffic-Aware Slotting** tab runs the complete recommendation pipeline
from raw building, velocity, and order-history inputs. It does not require an
assignment from the Inventory Slotting tab.

The stages are ABC baseline generation, affinity-based SKU grouping, hard-rule
validation, handling-unit visit calculation, traffic-aware complete-unit
placement, and final comparison. An optional `.slotting.json` can supply storage
zones and attributes, but its assignments are explicitly ignored.

## Chilled-zone setup and complete assignment

Use **Load map / label chilled zones** when the tab has no imported storage-rule
file. The editor controls temperature zoning only: choose Ambient or Chilled,
then drag a rectangle around the real racks belonging to that zone. After a
rectangle labels at least one rack, the zone ID advances automatically (`Z01`
to `Z02`, or `ZONE_009` to `ZONE_010`); an empty selection leaves it unchanged.

Before processing orders, the tab compares the SKU requirements with the
currently labeled chilled areas and available slot capacity. If a chilled SKU
has no chilled location, the pipeline pauses and asks the operator to finish
temperature-zone labeling.

Standard versus oversize placement is decided by the ABC/affinity slotting
algorithm, not zone labels. An oversize item remains in normal demand and
affinity ordering but reserves a contiguous rectangle of horizontal slots and
vertical levels when required. The assignment stores `occupied_slot_count`,
the horizontal and vertical spans, and `occupied_static_addresses`. Overweight
and oversize-plus-overweight footprints start at level 1 instead of being
rejected for exceeding the standard upper-level weight threshold. If cumulative
compatible capacity is insufficient,
generation stops with a warning and does not expose a partial layout for saving.
Missing physical measurements remain explicitly unverified because fit cannot
be guaranteed from absent source data.

## Demand definition

The order workbook must contain `Date`, `Store ID`, and `Item or SKU`. One
fulfillment group is one `(Store ID, Date)`. Each `handling_unit_id` contributes
one visit per group, regardless of how many requested SKUs or duplicate order
lines from that group are stored in the unit. Workbook SKUs absent from the
layout are reported.

## Movement networks

An embedded RMF map is adapted automatically. Pickup dispensers become storage
nodes, drop-off ingestors become equally weighted endpoints, and directed lanes
become movement resources.

Other delivery systems use `warehouse_movement_network/v1`:

```json
{
  "schema": "warehouse_movement_network/v1",
  "nodes": [
    {"id": "BIN_01_NODE", "x": 0, "y": 0, "kind": "storage", "location_ids": ["BIN_01"]},
    {"id": "PACK_NODE", "x": 10, "y": 0, "kind": "endpoint"}
  ],
  "resources": [
    {"id": "LIFT_01", "capacity": 120}
  ],
  "links": [
    {"id": "BIN_TO_PACK", "from": "BIN_01_NODE", "to": "PACK_NODE", "travel_time": 15, "resource_id": "LIFT_01"}
  ],
  "endpoints": [
    {"id": "PACK_01", "node_id": "PACK_NODE", "weight": 1}
  ]
}
```

`distance` or `travel_time` supplies the positive shortest-path cost. A link is
directed unless `bidirectional` is true. Several links may use the same
`resource_id`, representing a shared aisle, lift, crane, conveyor, transfer
point, or intersection. Capacity is optional; with no capacity, analysis uses
relative expected resource load.

`location_ids` or the optional top-level `storage_locations` object maps layout
addresses, rack IDs, waypoints, pickup IDs, or vertex references to nodes.
Unmapped and unreachable handling units remain visible in the results.

## Optimization and safety

The optimizer evaluates deterministic complete-unit swaps. Feasible candidates
must have matching slot shapes and must pass chilled, rotatable dimension,
maximum-weight, generic attribute, and capacity checks in both directions.
Incomplete physical data fixes a unit in place. No local capacity override is
created by this stage.

Candidate layouts are ranked by peak normalized resource load, P95 load, total
expected travel, and relocation count. Automatic generation derives travel-cap
candidates from feasible swaps in the active dataset, evaluates their Pareto
tradeoff, and exposes the selected maximum travel increase and hotspot
percentile for operator adjustment. Before and after relative metrics retain the
same baseline denominator; raw peak and P95 traffic are also reported.

The preview overlays lane traffic and handling-unit visits. Lanes use a
blue-to-red load scale. Rack markers use the same scale based on visits generated
at that location. Swapped racks remain outlined in both Before and After views.

## Outputs

**Save layout** writes an `inventory_slotting_layout/v2` document containing the
new assignments, source paths, traffic settings, before/after metrics, trials,
relocations, fixed units, and an operation-log record.

**Export report** writes:

- `<name>.traffic.json` using `traffic_aware_slotting_analysis/v1`;
- `<name>_traffic_resources.csv` with before/after resource load; and
- `<name>_traffic_relocations.csv` with the complete-unit move audit.

The analysis is an expected-flow planning model. It does not replace fleet,
controller, queueing, or collision simulation before deployment.
