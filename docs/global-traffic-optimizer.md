# Global Traffic Optimizer

The Global Traffic Optimizer is an independent exact/time-bounded pipeline. It
does not replace or modify the original heuristic `Traffic-Aware Slotting` tab
or `warehouse_layout/traffic.py`.

## Workflows

The new UI tab supports three workflows:

1. **Existing layout** loads a `.slotting.json`, preserves every assigned
   SKU-to-handling-unit relationship, and optimizes the positions of those
   complete units. Unassigned SKU rows are excluded and retained unchanged.
2. **Full pipeline** loads the warehouse `.grid.json`, generates a basic ABC or
   ABC-plus-affinity layout with the existing shared allocator and hard rules,
   then sends that baseline to the independent global optimizer.
3. **Auto-search best layout** starts from an existing layout, screens the nine
   combinations of 0%/5%/10% travel increase and 50%/75%/100% relocation,
   refines the best three, and initially displays the best valid result. Select
   any successful trial to view it and make it the plan saved by
   **Save Layout**.

Auto-search ranks accepted layouts in this order: controllable peak,
controllable P95, shared-neighbourhood peak, controllable top-5% mean, zone
peak, expected travel, and relocation count. A failed scenario is reported but
does not abort the remaining portfolio. **Save Layout** writes the currently
selected successful trial to the configured Output path. The adjacent `_search` folder contains
`parameter_comparison.csv` and `search_summary.json`.

AMR uses a complete shelf as the movable unit. ASRS uses its configured tote,
pallet, tray, or other slot-level handling unit. Demand is one required unit
visit per `(Store ID, Date)` task; a multi-slot ASRS retrieval retains its
physical-unit visit count. Unassigned SKUs do not contribute demand or solver
variables and remain visible in the saved result.

## Optimization model

For each movable unit `u` and compatible candidate location `l`, the model
creates a binary placement variable `x[u,l]`. Every unit receives one location,
and a location receives at most one unit. Candidates that violate any shared
hard rule are omitted before solving.

The candidate-location set includes compatible occupied locations and empty
buffers from the baseline layout. CP-SAT may leave an old position empty, move
a unit into an empty buffer, or choose a complete permutation when no empty
buffer exists. Single-buffer AMR shelves and ASRS units are supported directly.
ASRS multi-buffer footprints remain on compatible occupied footprints until a
future packing model can reserve every target slot atomically.

Routes from each candidate location to every weighted service endpoint are
precomputed. A Store ID + Date visit contributes flow to each shared movement
resource on that fixed shortest route.

Resources are divided into two audited groups. A resource is
`layout-controllable` when at least one unit's route contribution changes across
its candidate locations. Invariant terminal lanes and other structural
resources remain reported but cannot mask a useful placement improvement.

The Global Congestion View colours lanes by normalized resource load and rack
fills by the combined Store ID + Date visit frequency of the handling units
currently placed at that rack. Rack heat uses its P95 visit count as the top of
the colour scale, moves with relocated shelves between Before/After views, and
can be toggled independently without hiding the exact visit labels.

The solver minimizes, in strict order:

1. Maximum normalized controllable-resource load.
2. Nearest-rank controllable-resource P95.
3. CVaR95 across controllable resources.
4. A convex queue-risk proxy whose marginal penalty rises above 60%, 75%, and
   90% utilization.
5. Maximum normalized shared-entrance neighbourhood load.
6. Maximum normalized zone load.
7. Expected travel under the configured maximum increase.
8. Number of relocated units.

An earlier optimum is fixed before the next stage starts. Later objectives
therefore cannot trade away a better congestion result merely to reduce travel
or relocations.

The default post-solve acceptance guard permits no increase in nearest-rank
controllable-resource P95. Interpolated P95 is retained as a diagnostic metric,
but it is not used as the hard order-statistic guard. The result is rejected
rather than saved when the nearest-rank guard fails. A different non-negative
tolerance can be selected explicitly in the UI or CLI.

Shared-resource neighbourhoods skip rack-unique first lanes and warehouse-wide
terminal trunks. They use the first route resource shared by a meaningful
cluster of storage locations, producing aisle/entrance-sized groups. Their peak
load may not exceed the baseline. Travel may not increase by default, and the
default relocation cap is 50% of occupied handling units.

An AMR shelf with incomplete SKU dimensions may move between matching
temperature and standard/oversize segments because the SKU remains in the same
internal shelf slot. Unknown-data ASRS handling units stay fixed. This
handling-unit-aware policy avoids freezing an entire AMR shelf because one SKU
record is incomplete without weakening ASRS physical-fit rules.

## Solver status

- `OPTIMAL` means every lexicographic stage was proven optimal and the gap is
  exactly zero.
- `FEASIBLE` means a valid incumbent is available, but the complete
  lexicographic proof did not finish. The result records the current bound and
  gap when the solver supplied one; otherwise the gap is `null`/unproven.
- No result is saved when the model is infeasible, cancelled, or fails final
  shared hard-rule validation.

This is an exact result for the fixed-route expected-flow model. It is not a
fleet simulation and does not prove collision-free execution, queue time, or
real throughput. Validate shortlisted layouts with simulation or a controlled
pilot.

Routes are deterministic shortest paths over the movement network. Storage/rack
grid nodes are treated as terminal-only obstacles: the assigned task rack may
be the route start or destination, while every other rack grid is forbidden as
an intermediate transit node.

## Standalone script

Optimize an existing layout:

```bash
python3 global_traffic_slotting.py \
  --mode existing \
  --layout resources/data/basic_slotting_layout.slotting.json \
  --orders "resources/data/Sample Data.xlsx" \
  --network resources/map/warehouse_grid.grid.json \
  --time-limit 300 \
  --gap-percent 0 \
  --max-travel-increase-percent 0 \
  --max-controllable-p95-increase-percent 0 \
  --max-relocated-percent 50 \
  --output resources/data/global_traffic_layout.slotting.json
```

Run the full basic-to-global pipeline:

```bash
python3 global_traffic_slotting.py \
  --mode full \
  --grid-project resources/map/demo.grid.json \
  --velocity resources/data/sku_velocity_output/sku_velocity_summary.csv \
  --orders "resources/data/Sample Data.xlsx" \
  --initial-strategy basic \
  --time-limit 300 \
  --output resources/data/global_traffic_layout.slotting.json
```

For an affinity baseline, use `--initial-strategy abc_affinity` and set
`--affinity-weight` from `0.0` to `1.0`. Omit `--network` to use the movement
network embedded in the selected layout or grid project.

`--time-limit 0` permits an unlimited solve. The UI and CLI default to 180
seconds because empty-buffer candidates materially enlarge the exact model.
Large warehouses can produce many placement variables, so begin with a bounded
run and review the reported proof status before increasing the limit.

For a bounded solve, the first stage receives 40% of the available time, with a
60-second target minimum and 120-second cap when the total budget permits. This
gives large placement models enough time to establish a feasible incumbent.
The remaining wall-clock budget is divided among the remaining lexicographic
stages. Each feasible incumbent warm-starts the next stage. An unproven stage
is fixed to its best incumbent value; the final layout remains `FEASIBLE` until
every stage is proven.

When the movement network has no explicit resource capacities, controllable and
structural values are relative expected loads rather than physical
utilizations. The output records a capacity warning; do not interpret those
values as queue time or throughput.
