# Traffic-Aware Slotting

The **Traffic-Aware Slotting** tab supports two workflows:

1. **Optimize Existing Layout** loads a `.slotting.json`, validates its assigned
   inventory, and applies traffic-aware complete-unit relocation. Unassigned
   SKU rows are excluded and retained unchanged. It never regenerates or
   reorders the baseline with ABC or affinity.
2. **Generate Layout + Optimize Traffic** starts from an editable `.grid.json`,
   velocity data, and order history. Select either **ABC** or
   **ABC + Affinity** as the initial layout strategy, then run hard-rule
   validation, demand calculation, traffic optimization, and final comparison.

Neither workflow reads a building YAML. The grid project supplies the physical
map, generated buffers, rack zones, chilled/capacity settings, attribute
catalog, and inherited hierarchy values. A saved layout carries the same data
as a self-contained baseline.

The UI separates these inputs into two panels. **Initial ABC / affinity layout
generation** contains the grid project, velocity/chilled data, initial strategy,
and affinity weight used only by the full pipeline. **Traffic-aware
optimization** contains the optional existing layout, order history, movement
network, date range, congestion/travel parameters, and optimized output used by
the traffic stage.

## Warehouse configuration and complete assignment

The grid project must be completed in Grid Map Editor first. Its storage system,
handling-unit type, levels, slots, zones, temperature settings, physical
capacities, and advanced attributes are authoritative. Traffic-Aware Slotting
provides **Load warehouse project** for reviewing this configuration but does
not duplicate its editors.

If chilled or compatible capacity is insufficient, generation stops and directs
the operator to update and resave the grid project before rerunning.

The generated ABC or ABC + affinity baseline uses the same allocator and hard
rules as Inventory Slotting. Only the selected baseline is generated. Standard
inventory is placed before exception inventory.
Ambient user zones remain non-mixed `STANDARD` or `OVERSIZE` zones. A chilled
zone can split by whole rack into `*_chill_normal` and `*_chill_oversize`, with
standard chilled demand receiving capacity first. Known overweight and
oversize-plus-overweight inventory is assigned to level 2 when that level
exists (otherwise level 1). Unknown-size, unknown-weight, and non-volumetric
records use the exception segment and remain visibly unverified. If cumulative
compatible capacity is insufficient, assigned inventory continues through
traffic optimization while unassigned rows are excluded, retained unchanged,
and reported. A layout with no assigned inventory still stops.

Traffic optimization swaps complete handling units only. It preserves the
destination's zone, segment, buffer, capacity, and static address metadata.
Standard and exception segments cannot be crossed, weight capacity is never
bypassed, and units with incomplete physical data remain fixed and reported.

## Demand definition

The order workbook must contain `Date`, `Store ID`, and `Item or SKU`. One
fulfillment group is one `(Store ID, Date)`. For an AMR layout, each movable
shelf contributes one visit per group regardless of how many requested SKUs or
duplicate lines are stored on that shelf. For an ASRS layout, each occupied
slot/tote required by the group contributes one retrieval; multi-slot items
therefore count every occupied physical unit. Those retrievals remain grouped
under the item's primary relocatable unit during traffic optimization. Workbook
SKUs absent from the layout are reported.

## Movement networks

An embedded RMF map is adapted automatically. The **Use network / grid project
JSON** option can also load an editable `rmf_grid_map_editor/v2` `.grid.json`
directly, including `warehouse_grid.grid.json`; no separate network conversion
is required. In either case, pickup dispensers become storage nodes, drop-off
ingestors become equally weighted endpoints, and directed lanes become movement
resources.

For explicit resources, capacities, travel times, or non-RMF delivery systems,
use `warehouse_movement_network/v1`:

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
new assignments, refreshed buffer occupancy, workflow mode, initial strategy,
source paths, traffic settings, before/after metrics, trials, relocations, fixed
units, and an operation-log record.

**Export report** writes:

- `<name>.traffic.json` using `traffic_aware_slotting_analysis/v1`;
- `<name>_traffic_resources.csv` with before/after resource load; and
- `<name>_traffic_relocations.csv` with the complete-unit move audit.

The analysis is an expected-flow planning model. It does not replace fleet,
controller, queueing, or collision simulation before deployment.
