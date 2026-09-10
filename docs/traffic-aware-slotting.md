# Traffic-Aware Slotting

The **Traffic-Aware Slotting** tab runs C&TBSA directly from an editable
`.grid.json`, SKU demand and physical requirements, and order history. ABC and
the separate affinity-slotting strategy are optional comparison methods in
Inventory Slotting; neither is a C&TBSA prerequisite.

The workflow does not read a building YAML. The grid project supplies the physical
map, generated buffers, rack zones, chilled/capacity settings, attribute
catalog, and inherited hierarchy values.

The UI separates inputs into two panels. **Warehouse and SKU constraints**
contains the grid project and SKU physical/chilled data. **Paper C&TBSA and
static validation** contains order history, optional movement network, date
range, NSGA-II parameters, and output.

## Warehouse configuration and complete assignment

The grid project must be completed in Grid Map Editor first. Its storage system,
handling-unit type, levels, slots, zones, temperature settings, physical
capacities, and advanced attributes are authoritative. Traffic-Aware Slotting
provides **Load warehouse project** for reviewing this configuration but does
not duplicate its editors.

If chilled or compatible capacity is insufficient, generation stops and directs
the operator to update and resave the grid project before rerunning.

Attribute activation is identical to Basic and Affinity slotting. The workflow
intersects SKU requirements discovered from the selected CSV with attributes
configured on zone roots. A CSV attribute absent from every zone is ignored.
Every active attribute is inherited from the saved map and used to partition
compatible C&TBSA rack groups; the implementation has no chilled-only or
dataset-specific attribute list.

An internal physical-feasibility seed uses the shared allocator only to enforce
warehouse rules, reserve exception storage, and identify eligible standard
shelves. It is not an ABC or affinity optimization, and its standard-SKU
membership is discarded by C&TBSA. Standard inventory is placed before
exception inventory.
Ambient user zones remain non-mixed `STANDARD` or `OVERSIZE` zones. A chilled
zone can split by whole rack into `*_chill_normal` and `*_chill_oversize`, with
standard chilled demand receiving capacity first. Known overweight and
oversize-plus-overweight inventory is assigned to level 2 when that level
exists (otherwise level 1). Unknown-size, unknown-weight, and non-volumetric
records use the exception segment and remain visibly unverified. If cumulative
compatible capacity is insufficient, assigned inventory continues through
traffic optimization while unassigned rows are excluded, retained unchanged,
and reported. A layout with no assigned inventory still stops.

C&TBSA follows Lee, Chung, and Yoon (2020): Stage 1 clusters inventory items and
Stage 2 assigns the clusters
to storage areas. For this AMR warehouse, one complete shelf is one storage
area and its levels/slots are the paper's individual storage locations.
Physical-exception or incomplete-data shelves remain fixed and are reported.

## Hard constraints

### Optional minimum-rack mode

Enable **Use minimum racks** beside the NSGA-II settings to limit clustering to
`ceil(inventory_loads / slots_per_rack)` racks in each compatible attribute
profile. The default is off, preserving the original all-candidate-rack search.
Affinity and maximum rack demand remain the optimization objectives; the rack
count is fixed first. Spare slots can move between clusters during optimization.
With zone balancing enabled, those clusters can still be assigned across all
compatible rack locations. Without it, demand-ranked clusters use the nearest
compatible racks.

This minimum applies to ordinary single-slot loads within each existing profile.
Exception racks stay fixed and count in addition to that minimum. The shared
allocator still validates physical and cumulative-weight limits; generation
fails if the chosen clustering cannot be placed, rather than silently opening
extra racks or relaxing constraints. This is not a global packing proof across
different profiles or exception inventory. Fewer racks do not guarantee fewer
visits or better throughput.

For programmatic generation, pass
`CtbsaParameters(minimize_rack_count=True, ...)` as `ctbsa_parameters` to the
traffic pipeline (or as `parameters` to `optimize_ctbsa`). The reusable
`cluster_capacities()` function selects the rack budget. Saved results record
`minimize_rack_count`, `optimized_rack_count`, `selected_rack_count`, and
`rack_budget_policy`; loading a result restores the checkbox. Older results
default to off. For comparison, keep the seed, search budget, zone option,
inputs, and evaluation dates identical and save on/off runs to different files.

The paper model enforces:

1. Every inventory load is assigned to exactly one cluster:
   `sum_k x(j,k) = 1`.
2. Cluster `k` contains no more loads than its available locations:
   `sum_j x(j,k) <= Z(k)`.
3. Cluster demand is bounded by the optimized maximum:
   `sum_j F(j)x(j,k) <= Wmax`.
4. `x(j,k)` is binary. The permutation chromosome and fixed-capacity section
   boundaries preserve the first two constraints during NSGA-II operations.

The warehouse implementation adds non-paper safety constraints: AMR-shelf
storage only, unique occupied addresses, separation by every map-active SKU
attribute profile, inherited attribute compatibility, and physical capacity
checks. Oversize, overweight,
multi-slot, incomplete-data, or otherwise exceptional racks remain fixed because
the paper assumes one SKU occupies one ordinary storage location. These additions
restrict feasibility but do not change either paper objective. Quantity support
treats each `inventory_load_id` as one paper item, divides its logical SKU demand
by stored quantity, and therefore permits one SKU to appear in multiple clusters.

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

## Paper-replication optimization

For order `i`, SKU `j`, and shelf cluster `k`, the paper defines demand and
correlation as `F(j) = sum_i y(i,j)` and
`N(j,j') = sum_i y(i,j)y(i,j')`, where `y(i,j)=1` when order `i` requests SKU
`j`. Stage 1 maximizes total `N(j,j')` for SKU pairs in the same cluster and
minimizes `Wmax`, the largest sum of `F(j)` in any cluster.

For a multi-rack SKU, the implementation divides `F(j)` proportionally across
its quantity loads. Loads of the same SKU have no artificial self-correlation;
correlation with other SKUs remains inherited from `N(j,j')`. The original
maximum-cluster-demand and correlation objectives are otherwise unchanged.

A chromosome is a permutation divided into shelf-capacity sections. Empty shelf
slots are zero-demand dummy genes. NSGA-II uses the paper's final settings:
population 100, PMX crossover probability 0.9, 2-opt swap mutation probability
0.1, and 50,000 generations. Five evenly distributed Pareto representatives
are retained and balanced solution `C&TBSA3` is selected by default.

In Stage 2, clusters are sorted by decreasing demand and assigned to shelves in
increasing average workstation distance. Load order within a shelf is randomized
with a recorded seed. Chilled inventory uses a separate feasibility stratum;
oversize, overweight, multi-slot, or incomplete-data exception shelves remain
fixed so each exceptional load retains its required physical footprint.

Expected lane load is calculated only after placement. It is a static diagnostic
and does not affect either paper objective. No picking-delay, collision, queue,
or throughput result is claimed without simulation.

The preview overlays lane traffic and handling-unit visits. Lanes use a
blue-to-red load scale. Rack markers use the same scale based on visits generated
at that location. Reassigned racks remain outlined in both Before and After views.

## Outputs

**Save layout** writes an `inventory_slotting_layout/v2` document containing the
new assignments, refreshed buffer occupancy, paper parameters, five Pareto
representatives, cluster-to-shelf assignments, seed/result static expected-flow
metrics, SKU relocations, fixed exceptions, and an operation-log record.

**Export report** writes:

- `<name>.traffic.json` using `traffic_aware_slotting_analysis/v1`;
- `<name>_traffic_resources.csv` with before/after resource load; and
- `<name>_traffic_relocations.csv` with the SKU reassignment audit.

The analysis is an expected-flow planning model. It does not replace fleet,
controller, queueing, or collision simulation before deployment.
