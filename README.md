# Warehouse Layout Management

A Python desktop application for:

- creating grid-based Open-RMF warehouse maps;
- generating ABC and affinity-based inventory layouts;
- optimizing traffic and global congestion;
- searching inventory and testing slot or shelf swaps.

No warehouse drawing is required. Grid point `(0, 0)` is the bottom-left
corner, and positive Y points upward.

## Quick start

### Requirements

- Python 3.10 or newer
- Tkinter
- PyYAML, openpyxl, matplotlib and OR-Tools

On Ubuntu or Debian:

```bash
sudo apt install python3 python3-tk python3-pip
python3 -m pip install PyYAML openpyxl matplotlib ortools
```

Clone and start:

```bash
git clone https://github.com/s-w3i/layout-management.git
cd layout-management
python3 rmf_grid_map_editor.py
```

## Main workflow

Use the tabs from left to right:

| Tab | Purpose |
|---|---|
| **Grid Map Editor** | Create the grid, racks, workstations, zones, and buffers |
| **SKU Affinity** | Review SKU–store frequency and SKU relationships |
| **Inventory Slotting** | Generate an ABC or ABC-plus-affinity layout |
| **Interactive Slotting Layout** | Inspect assignments and movement ranks |
| **Traffic-Aware Slotting** | Improve congestion using complete-unit swaps |
| **Global Traffic Optimizer** | Search globally for a congestion-balanced layout |
| **Inventory Operations Demo** | Find inventory and test slot or shelf swaps |

For a new warehouse:

1. Build and configure the grid, then save the `.grid.json`.
2. Analyze the order workbook in **SKU Affinity** when affinity is required.
3. Generate a layout in **Inventory Slotting**.
4. Improve it with **Traffic-Aware Slotting** or **Global Traffic Optimizer**.
5. Inspect or test the result, then save the selected `.slotting.json`.

Start with the included demonstration map:

```text
resources/map/demo.grid.json
```

## 1. Grid Map Editor tab

All map and layout canvases use the same navigation controls: hold the right
mouse button and drag to pan, and scroll the mouse wheel to zoom around the
pointer. Editing and rectangle selection continue to use the left mouse button.

### Create the warehouse grid

Enter the following values:

| Field | Meaning |
|---|---|
| **Map name** | Name written into the RMF building file |
| **Level name** | RMF floor or level name, such as `L1` |
| **Total width (m)** | Warehouse size along the X axis |
| **Total length (m)** | Warehouse size along the Y axis |
| **Grid distance X / Y (m)** | Independent horizontal and vertical distances between neighbouring grid points |

X and Y grid distances can differ, allowing rectangular as well as square grid
cells. Width and length do not need to be exact multiples of their respective
distance. The editor shortens only the last interval to meet the exact outer
boundary. For example, width 20 m with X distance 3 m produces
X=`0, 3, 6, 9, 12, 15, 18, 20`.

Click **Generate / reset grid**. The editor creates horizontal and vertical
edges between neighbouring points. All generated lanes are bidirectional by
default.

> Generating a new grid removes the current rack and workstation markers after
> confirmation.

### Place rack pickup points

Set **Rack ID prefix**, then choose **Fill racks (drag rectangle)**. Press at
one corner, drag to the opposite corner, and release to fill every grid point
in the rectangle with racks. Press and release on the same point to place one
rack.

Rack IDs are generated from the prefix and grid location. They can be changed
later with **Select / edit**.

### Place workstation drop-off points

Choose **Place workstation drop-off**, then click a grid point. The resulting
RMF vertex is exported with a `dropoff_ingestor` property.

Use **Select / edit** to give the workstation a meaningful endpoint ID, such as
`WS_INBOUND_01` or `WS_PACKING_01`.

### Assign empty storage buffers

Choose the layout type and capacity, then click **Assign empty storage
buffers**. AMR creates one grid buffer (`B-G11_11`) for every rack pickup.
Mini-load and pallet ASRS create an empty slot buffer for every configured rack,
level, and slot (`B-G11_11/L01/S01`). Shelves, totes, pallets and SKUs are not
created in this tab. Changing rack markers invalidates the buffer catalog and
requires this step to be run again.

### Configure warehouse zones and attributes

Grid Map Editor is the authoritative warehouse-settings editor. Choose
**Assign rack zone (drag rectangle)**, enter a zone ID, then press and drag a
rectangle around a group of racks. Repeat until every rack is assigned. With automatic
advancement enabled, `Z01` advances to `Z02`, while IDs such as `ZONE_001`
preserve their numeric width.

Rack points are intentionally shown without overlaid rack IDs. A yellow dot is
unassigned, a red dot belongs to an ambient zone, and a blue dot belongs to a
chilled zone. Each assigned zone is surrounded by its own coloured boundary.
The zone ID is shown once as a filled badge immediately above the top-left of
that boundary.
A persistent legend below the map explains the rack and workstation dot colours.

After buffers and zones exist, use **Zone settings…** to configure chilled
storage and maximum item length, width, height, and weight. A blank physical
maximum means no configured maximum. Use **Advanced attributes…** to add typed
warehouse attributes or set inherited values at zone, aisle, bay, level, or
slot scope:

```text
Zone → Aisle → Bay → Level → Slot
```

These user-authored settings are saved in `.grid.json`. Slotting may generate
standard/oversize child zones and SKU-specific capacity overrides, but those
recommendations are written only to `.slotting.json` and never overwrite the
warehouse project.

### Edit or remove points

- Choose **Select / edit** and click a point.
- Change **Role** to `rack`, `workstation`, or `none`.
- Edit **Endpoint ID** if necessary.
- Click **Apply point edit**.

For bulk removal, choose **Clear markers (drag)** and drag over the points.

### Undo and redo

| Action | Shortcut |
|---|---|
| Undo | `Ctrl+Z` |
| Redo | `Ctrl+Y` or `Ctrl+Shift+Z` |

A drag-paint, drag-clear, or rectangle-fill action is treated as one undoable
operation.

### Save and export

The editor has two different save formats:

- **Save grid project JSON…** writes a `.grid.json` containing the editable map,
  storage-system profile, empty buffers, rack zones, attribute definitions, and
  hierarchy values. Inventory and Traffic-Aware Slotting both use this file.
- **Export RMF building YAML…** writes the same RMF-compatible `.building.yaml`
  structure as before. Every rack remains a `pickup_dispenser`; buffer metadata
  is deliberately excluded.

Use **Load grid project JSON…** to reopen a `.grid.json` project. A building YAML
is an export format and cannot replace the editable project file.

## 2. SKU Affinity tab

The **SKU Affinity** tab is an exploratory view and export tool. It never
modifies racks or layouts directly. The same raw workbook can also be selected
by the `abc_affinity` slotting strategy, which recalculates affinity from that
file during generation.

The default workbook is `resources/data/Sample Data.xlsx`. The application
automatically selects the first worksheet containing `Date`, `Store ID`, and
`Item or SKU`. Every valid row is one line-order event; quantity is deliberately
ignored. Blank identifiers and invalid dates are skipped and reported.

For SKU `i` and store `s`, direct line frequency is:

```text
F(i,s) = number of rows containing SKU i and Store ID s
```

SKU affinity uses one binary fulfillment group for every `(Store ID, Date)`.
Duplicate lines for an SKU in the same store-day count once for affinity. Two
SKUs are scored by cosine similarity of their presence across these groups. The
UI displays the score together with shared store-day count, total line orders,
and strongest contributing stores. Relationships require at least three shared
store-days by default.

Use the tab as follows:

1. Confirm the workbook and click **Analyze**. The first scan runs in the
   background and writes a source-fingerprinted cache under `.cache/`.
2. Review the KPI line, then optionally narrow the inclusive date range.
3. Search for a SKU or select a heatmap cell.
4. Use **Store–SKU Heatmap** for direct frequencies and **SKU Relationship Map**
   for the selected SKU's 12 strongest qualifying relationships.
5. Double-click a related SKU to recenter the graph. Sort either detail table by
   clicking its column headings.
6. Click **Export JSON + CSV…** to write a reusable `.affinity.json` snapshot,
   a SKU–store frequency CSV, and a SKU-pair CSV for the active filters.

See the [SKU affinity analysis guide](docs/sku-affinity-analysis.md) for the
metric, cache, export, and error-handling details.

## 3. Inventory Slotting tab

### Input files

The default inputs are:

| Input | Default path |
|---|---|
| Grid project JSON | `resources/map/demo.grid.json` |
| ABC SKU velocity CSV | `resources/data/sku_velocity_output/sku_velocity_summary.csv` |
| Chilled SKU CSV | `resources/data/demo_chilled_requirements.csv` |
| Affinity order workbook | `resources/data/Sample Data.xlsx` |
| Output layout | `resources/data/basic_slotting_layout.slotting.json` |

The grid JSON must contain generated empty buffers, at least one rack pickup,
and at least one workstation. Inventory Slotting reconstructs the RMF graph in
memory for route calculations.

The SKU CSV must contain these columns:

- `sku`
- `pick_frequency`
- `velocity_class`

It may also contain typed storage requirements using `req_<attribute_key>`
columns. For example:

```csv
sku,pick_frequency,velocity_class,req_max_item_length,req_max_item_width,req_max_item_height,req_max_item_weight
SKU_001,120,A,12,8,6,25
SKU_002,80,B,20,10,8,270
```

A blank custom requirement means that the SKU does not constrain that custom
attribute. Missing physical data remains unverified and is classified explicitly
as `UNKNOWN_WEIGHT`, `UNKNOWN_SIZE`, or `NON_VOLUMETRIC_DATA`. These categories
use exception storage planning without displaying a verified overweight or
oversize measurement. Set
`req_max_item_weight` to `0` to explicitly disable ergonomic weight placement
for that SKU. Requirement keys must exist in the attribute catalog before
generation.

The included compact CSV is ready for the demo. To analyse another warehouse,
place its transaction workbook under `resources/data/` and follow the
[SKU velocity analysis guide](docs/sku-velocity-analysis.md). Raw workbooks and
generated demand plots are intentionally excluded from the public repository.

### Load the grid project

1. Confirm or browse for the **Grid project JSON**.
2. Click **Load project**.
3. Confirm that the status reports the expected rack and workstation counts.

### Warehouse configuration source

Every rack must already have a zone before slotting can be generated. Zone
assignment, chilled settings, physical capacities, and advanced attributes are
edited and saved in **Grid Map Editor**, not Inventory Slotting.

1. In Grid Map Editor, choose **Assign rack zone (drag rectangle)**.
2. Enter the first **Zone ID**, normally `Z01`.
3. Press at one corner, drag around the rack points, and release at the
   opposite corner.
4. Repeat until the status reports zero unassigned racks.

With **Auto next ID** enabled, the zone advances automatically after each
successful selection:

```text
Z01 → Z02 → Z03
```

IDs such as `ZONE_001` also advance while preserving their numeric width. Use
**Clear rack zones** to restart zone assignment.

### Aisle and bay rules

- A zone is a collection of racks.
- Each different rack column, identified by its X coordinate, is a separate
  aisle.
- Aisle numbering restarts at `A01` in every zone.
- The fixed bay ID uses the RMF grid waypoint name.
- Bays in an aisle are ordered by their Y coordinate.

For example, racks in two columns inside `Z02` are addressed under `Z02/A01`
and `Z02/A02`, even if another zone already uses those aisle numbers.

### Select the slotting strategy

Select:

- **Strategy** — `basic` keeps the existing ABC-only flow; `abc_affinity`
  directly balances affinity-based bay consolidation against ABC bay purity.
- **Affinity weight** — 0% is pure ABC placement, 100% is pure affinity
  placement, and intermediate values use the displayed ABC/affinity ratio.
- Handling unit, levels, and slots per level are inherited from the grid project
  and intentionally omitted from the Inventory Slotting form.

The handling-unit choice controls where the dynamic identity appears in the
address. See [Inventory address rules](#inventory-address-rules).

### Zone storage settings in Grid Map Editor

Click **Zone settings…** in Grid Map Editor after grouping the racks. The application
initializes all zones with these source-unit demo limits:

| Storage | Length | Width | Height | Weight |
|---|---:|---:|---:|---:|
| Standard | 15 | 16 | 13 | 250 |

Only chilled storage is predefined by the user. The slotting algorithm plans
standard and oversize/overweight segments inside the selected ambient or
chilled zones. Units are deliberately labelled as unconfirmed source units;
the demo does not perform a centimetre, millimetre, gram or kilogram conversion.

Length, width, height, and weight maximums may be left empty. An empty field
means that no maximum is configured for that property; it does not mean zero
capacity. A numeric value continues to act as the maximum inherited by child
locations.

Maximums are planning inputs for normal storage. When an outlier needs more
capacity—or has unknown size or weight—the generated child-slot segment records
an oversize-capable override and the required or unbounded physical properties.

Mark only physically chilled zones with the **Chilled area** checkbox. Chilled
defaults to false because it cannot be inferred safely from an RMF map. Chilled
values inherit down to every aisle, bay, level, and slot. Oversize segments are
created automatically and never change the chilled/ambient role.

Use **Advanced attributes…** for optional operational attributes. The generated
`oversize_capable` child-slot values are normally managed by the algorithm.

### Advanced hierarchy attributes in Grid Map Editor

After assigning every rack to a zone and setting the buffer capacity, click
**Advanced attributes…** in Grid Map Editor. The editor uses the static hierarchy:

```text
Zone → Aisle → Bay → Level → Slot
```

The starter catalog contains `chilled`, `max_item_length`, `max_item_width`,
`max_item_height`, and `max_item_weight`. You can add reusable
boolean, number, text, or choice definitions. Exact matching is available for
all types; numeric attributes can use capacity matching, where the location
value must be at least the SKU requirement.

Select one or several hierarchy nodes to apply a value in bulk. Values inherit
downward. A child can override an inherited value, and **Clear local value**
removes the override so inheritance applies again. The effective-values table
shows both the final value and the ancestor that supplied it. Values never
inherit upward.

If zones or buffer capacity change, the editor asks before discarding values
whose hierarchy paths no longer exist. **Interactive Slotting Layout** can load
a generated `.slotting.json` for viewing, but warehouse configuration changes
belong in the source `.grid.json`.

The interactive viewer surrounds each warehouse zone with the same distinct
outline and external zone-ID badge used by Grid Map Editor. Its persistent
legend identifies A, B, C, unranked movement units, and the workstation
diamond. Import the historical order workbook in this tab and click
**Calculate movement ranks**. Rows are grouped into one task per
`(Store ID, Date)`; repeated demand for the same unit in that task counts as
one visit. For an AMR layout, one dot represents the movable shelf and the
whole shelf receives one movement rank. For an ASRS layout, the small dots
represent individual tote/pallet slots and each slot receives its own rank.
The red, amber, and green classes therefore describe handling-unit visit
frequency, not SKU ABC class or the rack's aggregate pick-frequency class.

### Generate the slotting layout

1. Load a grid project with buffers, zones, and warehouse attributes.
2. Confirm the SKU CSV and output paths.
3. Confirm or clear the optional chilled-SKU CSV path.
4. Select `basic` or `abc_affinity` and configure affinity inputs when needed.
5. Click **Generate slotting layout**.

The progress bar reports input loading, affinity analysis when applicable,
layout generation, and output saving. After generation, **SKUs not slotted**
lists every rejected SKU with its chilled requirement, length × width × height,
weight, physical-data status, and the reason no compatible slot was available.

With `abc_affinity`, the initial generation derives these values from the
selected workbook and current map: minimum shared store-days, minimum affinity
score, and maximum service-distance increase. It searches empirical relationship
quantiles, then selects a map-distance quantile from relationship confidence and
the user-selected affinity weight. No sample-specific threshold
is reused for another Excel file. The calculated values appear in the UI after
generation; edit them and click **Regenerate with edited values**, or click
**Recalculate automatic suggestion** after changing the workbook, map, or
weight.

The **basic** strategy:

1. Calculates the directed route distance from each rack to every workstation.
2. Uses the average distance across all workstations as the rack score.
3. Calculates logical ABC rank with A before B before C and descending pick
   frequency, then physically places all standard inventory before any
   oversize/overweight exception inventory.
4. Preserves that logical ABC rank in the output after physical planning.
5. Fills an already-open compatible rack before opening another rack. ABC
   classes may mix inside that rack; efficient rack utilization has priority
   over ABC purity.
6. Enforces the user-defined ambient/chilled separation. Exception inventory is
   planned inside a matching-temperature zone, so chilled outliers remain in a
   chilled zone.
7. Reserves oversize capacity during zone planning but slots those oversize and
   overweight exceptions last. Ambient source zones are
   allocated as complete `STANDARD` or `OVERSIZE` zones; the planner chooses the
   nearest combination with just enough capacity while preserving standard
   capacity where possible. A chilled source zone may be partitioned into
   separate `*_chill_normal` and `*_chill_oversize` generated zones.
8. Uses SKU weight as a soft ergonomic heuristic: a positive weight prefers
   the middle rack level and then expands outward toward lower and upper
   levels. This is not a hard constraint; weight `0` disables the preference.
9. Reserves every position in the smallest rotation-aware contiguous footprint
   required by a known dimension-oversize item. Width may span adjacent slots
   and height may span rack levels; an item whose depth cannot fit in any
   rotation is rejected. Weight-only exceptions remain single-slot inventory
   on level 2. Unknown properties become unbounded planning assumptions on the
   generated exception segment. The `occupied_dynamic_address` field presents
   multi-position AMR occupancy compactly, for example
   `SHELF_124/L02/S02,03`.
10. Uses average workstation distance to rank otherwise equivalent racks and
   returns general not-enough-space only after every storage slot is occupied.

The **abc_affinity** strategy keeps temperature, physical-fit, and capacity
requirements as eligibility rules, then directly balances two soft objectives:
store-order rack-touch consolidation and ABC placement. It builds fulfillment
groups from `(Store ID, Date)` and prefers a rack already required by those
groups, reducing the distinct racks an AMR must carry. It also follows direct
order-overlap clusters during placement, so at a high affinity weight a strongly
related B or C SKU can be processed immediately after an A SKU.
At 0%, placement matches the basic ABC strategy. At 100%, ABC class is excluded
from placement ordering, scoring, and tie-breaking; it is calculated afterward
only for rack reporting. Intermediate values apply `1 - affinity weight` to the
ABC objective and `affinity weight` to the affinity objective, so the larger
ratio dominates. Relationship strength is cosine similarity of binary SKU presence by
`(Store ID, Date)`, weighted by shared store-days.

The output preserves `abc_frequency_rank` separately from
`affinity_placement_rank`; the backward-compatible `sku_rank` remains the ABC
rank. The summary compares basic and affinity average, median, P95, one-rack
rate, and total projected rack touches per store-day fulfillment group.

For an AMR shelf, a related pair in the same bay belongs to the same movable
shelf unit. This can let an AMR satisfy more order lines with one shelf pickup
instead of visiting several bays. A different bay incurs an affinity cost first;
physical distance between different bays is the secondary affinity cost. The
generated layout reports weighted same-bay relationship coverage, mixed-ABC bay
count, selected parameters, and service distance against the basic baseline.

Chilled SKUs still require chilled slots and ambient SKUs require non-chilled
slots. If one temperature category has insufficient slots, the result reports a
temperature-zone shortage even when the opposite category has empty capacity.

The optional chilled CSV contains `sku,chilled_required`. Only selected chilled
SKUs need rows; all absent SKUs are ambient. The included file selects 10% of
the 1,524 sample SKUs with seed 42. Invalid booleans, duplicates, unknown SKUs,
or conflicts with `req_chilled` stop generation.

Missing physical data is classified conservatively: unknown weight is treated
as overweight, unknown size as oversize, and missing both usable size and weight
as oversize plus overweight. These SKUs remain visibly `UNVERIFIED`, but the
algorithm gives them generated exception segments rather than normal storage.
Production users must validate planned capacities against actual equipment.

Each generated zone has exactly one result type: `STANDARD` or `OVERSIZE`.
`MIXED` zones are not generated. `planned_zone_id` identifies the generated
subzone while `zone_id` retains the user-defined source zone used for chilled
inheritance and static addressing.

If a rack cannot reach every workstation through the directed RMF graph, it
ranks after reachable racks but remains usable storage. An assignment on it is
marked `UNREACHABLE_LAST_RESORT`.

### Inspect the result

After generation:

- Red racks contain class A inventory.
- Orange racks contain class B inventory.
- Green racks contain class C inventory.
- Grey racks are unused.
- Blue diamonds are workstations.

Click a rack to view all assigned SKUs and their complete static and dynamic
addresses. The **Storage flags** column marks each row as `CHILLED`, `OVERSIZE`,
`OVERWEIGHT`, `UNVERIFIED OVERSIZE`, or `STANDARD AMBIENT`; combined conditions
show multiple flags. The rack summary also shows chilled and physical-exception
counts. Use **Show all assignments** to return to the complete table.

The generated v2 `.slotting.json` is self-contained. It stores the building
map, zone assignments, zone limits, chilled and affinity input metadata,
strategy settings, selected or edited affinity parameters,
attribute catalog, local hierarchy values, physical SKU requirements,
generated slot overrides, inventory assignments, and operation log. Existing
v1 files remain loadable and are normalized with an empty attribute model.

## 4. Traffic-Aware Slotting tab

The tab exposes two primary workflows:

1. **Optimize Existing Layout** takes a complete `.slotting.json` and
   order-history workbook. It preserves that file's SKU-to-unit membership and
   skips all ABC/affinity regeneration.
2. **Generate Layout + Optimize Traffic** takes the editable `.grid.json`, ABC
   velocity CSV, optional chilled-SKU CSV, and order-history workbook. Select
   **ABC** or **ABC + Affinity** as the initial strategy. Affinity weight is used
   only for the latter. The grid project must already contain storage buffers,
   rack zones, chilled/capacity settings, and advanced attributes.
3. Keep **Use embedded RMF map** to reuse the active layout, or select
   **Use network / grid project JSON**. That option accepts either an editable
   `rmf_grid_map_editor/v2` `.grid.json` directly or a generic
   `warehouse_movement_network/v1` JSON with explicit resources and capacities
   for ASRS, conveyors, cranes, lifts, or another delivery system.
4. Review the selected baseline's unit visits, lane and rack heatmaps,
   before/after traffic KPIs, congested resources, relocations, fixed/rejected
   units, and selected parameters. Rerun the same workflow after editing maximum
   travel increase or hotspot percentile when operational policy requires it.
5. Save the compatible v2 slotting layout and optionally export the traffic JSON
   plus resource and relocation CSV files.

The UI keeps the settings in separate side-by-side panels. Initial ABC/affinity
generation settings are on the left and traffic-aware optimization settings are
on the right, so parameters that are ignored by the existing-layout workflow
are visibly isolated.

Demand is grouped by `(Store ID, Date)`. An AMR shelf is visited at most once
inside a group even when it contains several requested SKUs. For ASRS, every
occupied slot/tote needed by the group counts as a retrieval, including every
physical unit occupied by a multi-slot item. These retrievals remain grouped
under the primary relocatable unit for optimization. Routing uses deterministic
shortest paths to reachable service endpoints. Endpoint weights are equal for
RMF maps and configurable in generic networks.

Every relocation is an atomic pairwise unit swap. Chilled/ambient separation,
standard/oversize segment separation, rotation-aware dimensions, maximum
weight, slot shape, and capacity are strict move gates. The traffic stage never
creates a capacity override. A unit with incomplete physical data remains fixed
and is reported. This conservative rule does not invalidate an unchanged
generated row that is reported as unverified.

Full-pipeline baseline generation uses the same rules as basic and affinity slotting:
standard inventory is placed before exceptions, ambient zones remain non-mixed,
chilled normal and oversize stock use separate racks, and known overweight or
oversize-plus-overweight stock is intentionally placed at level 2 when present.
Generated buffers from the grid project are enforced and their occupancy is
included in the result.
Unassigned SKU rows are excluded from traffic demand and relocation, retained
unchanged in the result, and reported in the status and saved metadata. The
workflow stops only when no assigned inventory remains to optimize.

The map contains two heat layers. Lane colour and width represent expected route
load. Rack colour represents the handling-unit visits generated at that physical
location. Before and After use the same demand, while handling-unit relocations
move rack heat between positions. Purple outlines show all swapped locations;
selecting a relocation marks its source and destination separately.

When movement-resource capacities exist, the map reports utilization. Otherwise
it reports relative expected load, allowing the same workflow to operate without
an AMR, ASRS, or conveyor profile. This is a static expected-flow recommendation,
not collision-free fleet simulation.

See [Traffic-aware slotting](docs/traffic-aware-slotting.md) for the generic
network contract and export details.

## 5. Global Traffic Optimizer tab

This separate tab leaves the original pair-swap Traffic-Aware Slotting
workflow unchanged. It offers three buttons:

1. **Optimize Existing Layout Globally** reuses a saved layout. Assigned
   handling units are optimized; unassigned SKU rows are retained unchanged.
2. **Generate Layout + Globally Optimize** first creates the selected ABC or
   ABC-plus-affinity baseline from the grid project, then runs the global
   solver.
3. **Auto-search Best Layout** screens nine travel/relocation configurations
   for an existing layout, refines the best three, ranks every valid result by
   congestion first, and initially displays the recommended winner.

Auto-search tries travel limits of 0%, 5%, and 10% crossed with relocation
limits of 50%, 75%, and 100%. The screen/final solve times and finalist count
are editable beside the optimizer settings. Select a successful trial and
click **Save Layout** to write its `.slotting.json`; `parameter_comparison.csv`
and `search_summary.json` are written to its adjacent `_search` report folder.

Historical demand is grouped by `(Store ID, Date)`. The solver simultaneously
assigns every movable AMR shelf or ASRS handling unit to a compatible occupied
or empty candidate buffer. It does not require an empty buffer: when every
candidate location is occupied, the result is a permutation or movement cycle
rather than a sequence of pair swaps.

Hard-incompatible variables are never added. Chilled, standard/oversize,
dimensions, contiguous footprint, ergonomic overweight level, and inherited
attributes therefore remain mandatory. An AMR shelf containing incomplete
SKU physical data may move as one complete shelf between equivalent compatible
segments because its SKUs stay in the same internal slots. Unknown-data ASRS
units remain fixed.

OR-Tools CP-SAT solves these objectives lexicographically:

1. Peak normalized layout-controllable resource utilization.
2. Layout-controllable nearest-rank P95.
3. CVaR95 and convex queue-risk penalties.
4. Shared-entrance neighbourhood concentration.
5. Zone utilization.
6. Expected travel within the configured increase.
7. Relocation count.

The tab displays solver stages, bounds, gaps, relocations, controllable versus
structural resource loads, before/after balance, and a pan/zoom network heat
map. Its **Auto-search Trials** view shows every accepted or failed parameter
run and identifies the recommended and currently viewed plans. Selecting a
successful row refreshes the congestion view and all result tables. Resources
whose load cannot change
under any candidate placement remain
visible but do not block the placement objective. The default acceptance guards
reject a recommendation if nearest-rank controllable-resource P95 or the
shared-entrance neighbourhood peak is worse than the baseline. Travel may not
increase by default, and at most 50% of handling units may be relocated.
`OPTIMAL` is written only when every stage has a proven zero gap. A time-limited
incumbent is written as `FEASIBLE`; an unavailable proof gap is shown as
unproven.

The network view overlays two heat layers. Lane colour represents normalized
resource load. Rack fill represents Store ID + Date handling-unit visit
frequency at that location, scaled to the rack P95 so one extreme rack does not
hide variation among the others. The **Rack picking-frequency heat** checkbox
toggles the rack layer; exact visit counts remain visible in rack labels.

Global routes use deterministic shortest paths through transit grids. Rack
grids are terminal-only: a route may start or end at its task rack, but may not
cross any other rack grid as an intermediate path node.

The same independent pipeline is available without Tkinter:

```bash
python3 global_traffic_slotting.py \
  --mode existing \
  --layout resources/data/basic_slotting_layout.slotting.json \
  --orders resources/data/Sample\ Data.xlsx \
  --output resources/data/global_traffic_layout.slotting.json
```

See [Global traffic optimizer](docs/global-traffic-optimizer.md) for the full
pipeline command and model scope.

## 6. Inventory Operations Demo tab

### Load a slotting layout

1. Confirm or browse for the `.slotting.json` file.
2. Click **Load layout**.
3. Confirm or browse for the historical-order Excel workbook.
4. Click **Calculate movement ranks**.
5. Click any occupied rack to display every SKU in that rack, including the
   same chilled and physical-exception storage flags shown during slotting.

The map follows the same viewing procedure and legend as **Interactive Slotting
Layout**. Orders are grouped by `(Store ID, Date)`, and a handling unit is
counted at most once per combined task. AMR layouts show and rank one movable
shelf dot per rack; ASRS layouts show and rank the individual tote/pallet slot
dots. Red, amber, and green mean movement classes A, B, and C; grey means the
unit has not been ranked or did not occur in matching history. Zone boundaries,
external zone-ID badges, workstation diamonds, right-button panning, and wheel
zooming are shared with the interactive viewer.

This tab intentionally does not show zone-detail or rack-detail panels. A rack
click only lists its inventory and supports mock-swap selection. Find SKU still
shows the selected SKU's address, handling unit, requirements, and compatibility
information.

### Search for a SKU

1. Enter a complete SKU or part of a SKU in **Find SKU**.
2. Click **Search**.

The application highlights the current rack and shows the SKU's static address,
dynamic address, handling-unit ID, RMF grid position, requirements, effective
location attributes, and compatibility state.

### Swap two SKU slots

1. Select **SKU slot** as the swap type.
2. Click a rack and select the first SKU row. It fills **Source SKU**.
3. Click another rack if required and select the second SKU row. It fills
   **Target SKU**.
4. Review both values.
5. Click **Execute mock swap**.

The two SKU records exchange their complete location assignments. Selecting the
rows does not change inventory; the change occurs only after Execute is clicked.
Both target locations are checked first. A chilled/ambient mismatch rejects the
complete swap; other requirements create local overrides on the target slots.

### Swap two AMR shelves

Whole-shelf swapping is available only for layouts generated with **AMR shelf**.

1. Select **Whole shelf** as the swap type.
2. Click the first occupied rack point. Its shelf becomes the source.
3. Click the second occupied rack point. Its shelf becomes the target.
4. Confirm that both selected racks have orange rings and both shelf IDs appear
   in the source and target fields.
5. Click **Execute mock swap**.

Every SKU remains tied to its movable shelf ID while the two shelves exchange
fixed rack positions. Static and dynamic addresses are recalculated.
Every SKU on both shelves is validated before anything moves. A chilled/ambient
mismatch rejects the complete swap atomically; soft requirements are recorded
as target-slot overrides before movement is applied.

Tote and pallet layouts do not use whole-shelf swap because their movable IDs
exist at slot level.

### Save operation changes

Operations are held in memory until saved. Click **Save changes as…** to write a
new `.slotting.json` containing the modified assignments and timestamped
operation log.

## Inventory address rules

Every assigned SKU has a static address and a dynamic address.

### Buffer and dynamic addresses

The static address stops at the buffer. For AMR the grid is the buffer and the
shelf owns its internal levels and slots:

```text
AMR static:  Z03/A08/B-G11_11
AMR dynamic: SHELF_001/L01/S01
```

For ASRS the rack slot is the buffer and the tote or pallet is dynamic:

```text
ASRS static:   Z03/A08/B-G11_11/L01/S01
Tote dynamic:  TOTE_001
Pallet dynamic: PALLET_001
```

`storage_location_address` retains the full level/slot path used for inherited
capacity checks. The output also records `buffer_id`, `buffer_level`, and the
dynamic-unit ID. Buffer occupancy is calculated from distinct occupied buffer
IDs—not SKU rows—and is reported as total, occupied, empty, and occupancy rate.

## Command-line map generation

The script can generate a map without opening the GUI.

Create an empty 20 m × 15 m grid with 1 m spacing:

```bash
python3 rmf_grid_map_editor.py --generate \
  --width 20 \
  --length 15 \
  --x-spacing 1 --y-spacing 1 \
  --name warehouse_grid \
  --level L1 \
  --output resources/map/new_warehouse.building.yaml
```

Add markers using `ROLE,COLUMN,ROW,ENDPOINT_ID`:

```bash
python3 rmf_grid_map_editor.py --generate \
  --width 20 --length 15 --x-spacing 2 --y-spacing 1 \
  --marker rack,3,4,RACK_001 \
  --marker workstation,0,2,WS_OUTBOUND \
  --output resources/map/new_warehouse.building.yaml
```

Valid marker roles are `rack` and `workstation`. The bottom-left point is
column 0, row 0.

## Files and folders

```text
layout_management_master/
├── README.md
├── rmf_grid_map_editor.py                         Application launcher only
├── global_traffic_slotting.py                     Independent global optimizer CLI
├── sku_velocity_analysis.py
├── warehouse_layout/
│   ├── cli.py                                     CLI application controller
│   ├── affinity.py                                SKU/store affinity analysis and exports
│   ├── config.py                                  Paths and schema constants
│   ├── attributes.py                              Inheritance and compatibility service
│   ├── attribute_editor.py                        Hierarchy attribute editor
│   ├── zone_settings_editor.py                    Core zone-capacity editor
│   ├── domain.py                                  Grid domain models
│   ├── gui.py                                     Tkinter application class
│   ├── global_traffic.py                          Exact/bounded global CP-SAT service
│   ├── global_traffic_gui.py                      Independent global optimizer tab
│   ├── global_traffic_search.py                   Automatic parameter portfolio and reports
│   ├── inventory.py                               Search and swap service
│   ├── rmf.py                                     RMF/project persistence service
│   ├── slotting.py                                Routing, addressing and slotting
│   └── traffic.py                                 Generic traffic analysis and unit optimization
├── tests/
│   ├── test_attributes.py                         Attribute and compatibility tests
│   ├── test_affinity.py                           Affinity analysis and cache tests
│   ├── test_services.py                           Service regression tests
│   ├── test_global_traffic.py                     Global solver and CLI tests
│   ├── test_global_traffic_search.py              Automatic parameter-search tests
│   └── test_traffic.py                            Traffic network, constraints and export tests
├── docs/
│   ├── README.md                                  Documentation index
│   ├── rmf-grid-map-editor.md                     Additional editor notes
│   ├── sku-velocity-analysis.md                   ABC analysis guide
│   ├── global-traffic-optimizer.md                Global congestion model and CLI
│   └── traffic-aware-slotting.md                  Traffic algorithm and network contract
└── resources/
    ├── data/
    │   ├── Sample Data.xlsx                     Optional local input (ignored)
    │   ├── demo_chilled_requirements.csv        Seeded chilled demo input
    │   ├── basic_slotting_layout.slotting.json  Generated output (ignored)
    │   └── sku_velocity_output/
    │       ├── sku_velocity_summary.csv
    │       └── demand_plots/
    ├── map/
    │   ├── demo.grid.json
    │   ├── demo.building.yaml
    │   └── v6.building.yaml
    └── others/
```

## Code architecture

`rmf_grid_map_editor.py` is intentionally a minimal launcher. Application code
is organized by responsibility inside the `warehouse_layout` package:

| Module | Main class | Responsibility |
|---|---|---|
| `affinity.py` | `AffinityService` | Parse line orders, derive SKU/store affinity, cache events and export analysis |
| `domain.py` | `GridProject`, `GridSpec`, `Marker` | Grid state, validation and RMF dictionary construction |
| `rmf.py` | `RmfMapService` | Load/save editable projects and import/export building YAML |
| `slotting.py` | `SlottingService` | Rack routing, zone-local aisles, dynamic addresses, ABC slotting and ABC-plus-affinity tuning |
| `slotting.py` | `SlottingLayoutRepository` | Read/write self-contained slotting JSON |
| `traffic.py` | `TrafficAwareSlottingService` | Adapt RMF/generic networks, derive store-day unit visits, balance resource traffic and export audits |
| `inventory.py` | `InventoryService` | SKU lookup, SKU-slot swap and AMR-shelf swap |
| `attributes.py` | `StorageAttributeService` | Inheritance, physical classification and compatibility |
| `zone_settings_editor.py` | `ZoneStorageSettingsEditor` | Chilled and physical capacity input by zone |
| `gui.py` | `GridMapEditorApp` | Tkinter widgets and user interaction |
| `cli.py` | `GridMapEditorCommand` | Command-line parsing and application startup |

Run the service regression tests from the repository root:

```bash
python3 -m unittest discover -s tests -v
```

On Linux, run the automated five-tab GUI workflow with a virtual display:

```bash
sudo apt install xvfb
xvfb-run -a python3 -m tests.gui_workflow_smoke
```

The GUI smoke test covers grid editing, affinity loading and filtering, affinity
exports, automatic and edited affinity slotting, traffic analysis/generation and
exports, map loading and drawing, rectangle zone selection, all handling-unit
address models, rack inspection, SKU search, SKU-slot swap, AMR-shelf swap, and
operation-layout saving.

## Troubleshooting

### `No module named yaml`

Install PyYAML:

```bash
python3 -m pip install PyYAML
```

### `No module named tkinter`

On Ubuntu or Debian:

```bash
sudo apt install python3-tk
```

### The last grid interval is shorter

This is expected when a width or length is not divisible by the configured grid
distance. Full intervals use the requested distance and the final interval ends
at the exact warehouse boundary.

### Grid project has no buffers, racks, or workstations

Inventory Slotting requires:

- at least one rack pickup marker;
- at least one workstation marker; and
- an assigned empty-buffer catalog.

Return to Grid Map Editor, add the missing markers, click **Assign empty storage
buffers**, and save the editable grid JSON again. YAML export is not used by
Inventory Slotting.

### Slotting says racks remain unassigned

Every rack must belong to a zone. In Grid Map Editor, use **Assign rack zone
(drag rectangle)** for the remaining rack points, then save and reload the grid
project before generating.

### Some racks are unreachable

Check RMF lane directions and graph continuity. The basic strategy requires a
directed route from each usable rack to every workstation.

### Whole-shelf swap is unavailable

Whole-shelf swap applies only to **AMR shelf** layouts. Tote and pallet dynamic
identities are stored at slot level.

### Existing JSON still shows an older address format

Return to Inventory Slotting and regenerate the `.slotting.json`. Existing
generated files are not automatically migrated when address rules change.

## Related documentation

- [SKU velocity analysis](docs/sku-velocity-analysis.md)
- [SKU affinity analysis](docs/sku-affinity-analysis.md)
- [Traffic-aware slotting](docs/traffic-aware-slotting.md)
- [Additional RMF editor notes](docs/rmf-grid-map-editor.md)
