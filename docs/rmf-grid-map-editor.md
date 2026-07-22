# RMF Grid Map Editor

This Python application creates an image-free Open-RMF `.building.yaml` map.
It uses `cartesian_meters`, so the bottom-left grid point is `G0_0` at `(0, 0)`
and positive Y points upward.

All map and layout views support consistent navigation. Hold the right mouse
button and drag to pan; scroll the mouse wheel to zoom in or out around the
pointer. Left-button editing, rack inspection, and rectangle selection remain
available at any zoom level.

## Start the editor

```bash
cd layout-management
python3 rmf_grid_map_editor.py
```

Enter:

- Total warehouse width in metres
- Total warehouse length in metres
- X and Y distance between grid points in metres
- Map and RMF level names

X and Y use independent positive grid distances, so cells may be rectangular.
Click **Generate / reset grid** to create all vertices and edges. Complete
intervals use the configured distance for their axis; a shorter final interval
is added when needed to meet the exact width or length. Every point is connected
to its horizontal and vertical neighbours, and every lane is bidirectional.

## Mark operational points

Choose a placement tool:

- **Fill racks (drag rectangle)** marks every grid point inside a dragged
  rectangle as a pickup. Press and release on one point to place one rack.
- **Place workstation drop-off** adds `dropoff_ingestor: [1, WORKSTATION_ID]`
- **Clear markers** can also be dragged over many points.
- **Select / edit** lets you change the role or endpoint ID

The rack-prefix field controls automatic IDs. With prefix `RACK`, column 3 and
row 4 becomes `RACK_3_4`. Every generated ID remains individually editable.

Use **Ctrl+Z** to undo and **Ctrl+Y** to redo. **Ctrl+Shift+Z** also redoes an
action. Painting or clearing several points in one mouse drag counts as one
undoable action, as does filling a complete rack rectangle.

Save an editable `.grid.json` project while working. Export the completed map as
`resources/map/v6.building.yaml` when ready.

Before saving the project for Inventory Slotting, select `AMR`, `Mini-load
ASRS`, or `Pallet ASRS`, enter its levels and slots per level, and click
**Assign empty storage buffers**. AMR produces one grid buffer per rack pickup;
ASRS produces level/slot buffers. The YAML export is unchanged and continues to
contain only RMF rack `pickup_dispenser` and workstation `dropoff_ingestor`
metadata.

## Command-line generation

An empty 20 m × 15 m map with 1 m grid spacing:

```bash
python3 rmf_grid_map_editor.py --generate \
  --width 20 --length 15 --x-spacing 1 --y-spacing 1 \
  --name warehouse_grid \
  --output resources/map/v6.building.yaml
```

Markers may be supplied as `ROLE,COLUMN,ROW,ENDPOINT_ID`:

```bash
python3 rmf_grid_map_editor.py --generate \
  --width 20 --length 15 --x-spacing 2 --y-spacing 1 \
  --marker rack,3,4,RACK_001 \
  --marker workstation,0,2,WS_OUTBOUND \
  --output resources/map/v6.building.yaml
```

For a 20 m width with 1 m spacing, columns run from 0 through 20. This means
there are 21 grid points across the width, including both warehouse boundaries.

## Generate the RMF navigation graph

After exporting the building map:

```bash
ros2 run rmf_building_map_tools building_map_generator nav \
  resources/map/v6.building.yaml output_nav_graphs
```

## SKU affinity tab

The **SKU Affinity** tab reads the line-level order workbook independently of
slotting. It finds `Date`, `Store ID`, and `Item or SKU` automatically. Every
valid row counts once, even when `Quantity (in EA)` is greater than one.

Click **Analyze** to scan the workbook on a background thread. Repeat loads use
a local source-fingerprinted cache. The inclusive date fields can then rebuild
the analysis without reopening Excel.

The heatmap displays direct SKU–store line frequencies. The relationship map
groups rows by `(Store ID, Date)`, treats SKU presence in each store-day as
binary, and calculates cosine similarity across those groups. Select a heatmap
cell, search for a SKU, or click a graph node to update the related-SKU and
store-frequency tables. The default threshold is three shared store-days.

**Export JSON + CSV…** writes the active analysis as a versioned affinity JSON,
a direct SKU–store CSV, and a derived SKU-pair CSV. The slotting strategy reads
the selected raw Excel workbook directly, while these exports remain review and
interchange artifacts.

## Inventory slotting tab

The **Inventory Slotting** tab accepts:

- An editable `.grid.json` containing the RMF grid, rack/workstation markers,
  storage-system profile, empty buffers, rack zones, and warehouse attributes
- The ABC SKU velocity summary CSV
- An optional selected-SKU chilled requirements CSV
- A strategy selected from the dropdown
- Optional typed SKU requirements using `req_<attribute_key>` CSV columns that
  match the attribute catalog saved in the grid project

Use this workflow:

1. In Grid Map Editor, generate buffers, assign every rack to a zone, configure
   chilled/capacity settings, and add any advanced hierarchy attributes.
2. Save the `.grid.json`, then browse for it in Inventory Slotting and click
   **Load project**.
3. Browse for the ABC SKU velocity CSV and optional chilled CSV. Physical
   requirement columns are `req_max_item_length`, `req_max_item_width`,
   `req_max_item_height`, and `req_max_item_weight`. A positive SKU weight
   enables a soft preference for middle rack levels; weight `0` disables this
   ergonomic preference for that SKU.
4. Select the strategy, then generate the layout.

For `abc_affinity`, select the order-history workbook and an affinity weight.
The weight directly balances same-bay affinity consolidation against ABC
placement: 0% is pure ABC, 100% is pure affinity with no ABC placement
tie-breaker, and intermediate values use the selected ratio. For an AMR
shelf, the affinity objective directly minimizes incremental distinct-rack
touches across `(Store ID, Date)` fulfillment groups.
The first generation automatically suggests minimum shared store-days, minimum
affinity score, and maximum service-distance increase from that workbook and
warehouse map. The values appear after generation and become editable. Use
**Regenerate with edited values** to test an adjustment or **Recalculate
automatic suggestion** after changing the source data.

Generation is blocked until the grid project supplies a zone for every rack.
The generated result viewer colours racks by assigned ABC class. Aisles are derived from rack columns
(common X coordinates), and bays are ordered along each column. Aisles are
numbered independently inside every zone, so each zone starts at `A01`.

The demo **basic** strategy calculates the shortest directed graph route from
each rack to every workstation, then uses the average of those route distances
as the rack score. It sorts SKUs by ABC class and pick frequency, checks chilled
exclusivity first, then uses rotation-aware dimensions, weight, and custom
attributes. Zone capacity for exceptions is planned in advance, but standard
SKUs are physically placed first and oversize/overweight SKUs last. Compatible
racks are filled before another rack is opened, so ABC classes may mix. Standard
SKUs cannot consume reserved oversize segments. Chilled exceptions use available slots inside a chilled zone, which
splits that zone into standard and oversize segments without changing its
temperature role.
For weighted SKUs, otherwise suitable slots are ranked from the rack center
outward; this is a heuristic and does not reject an available location.
Missing physical data is assigned with an `UNVERIFIED` warning. A general
not-enough-space result occurs only when every slot is occupied.

The **abc_affinity** strategy retains all storage constraints but makes ABC bay
purity a soft objective that competes directly with affinity. Placement follows
strong affinity connections, and the location score charges a primary cost for
using another bay plus a secondary cost for distance between bays. High weights
therefore permit cross-class same-bay consolidation. Automatic thresholds come
from empirical distributions in the selected workbook and map; they are not
fixed to the bundled sample data.

Chilled and ambient capacity is counted separately because neither category may
use slots from the other. The result distinguishes this temperature-zone
shortage from global not-enough-space.

Unknown weight is treated as overweight, unknown size as oversize, and missing
both usable size and weight as oversize plus overweight. Unknown properties are
stored as unbounded assumptions on the generated segment. Validate every
generated segment and capacity recommendation against real equipment.
Racks without a complete route to every workstation rank last but remain usable
and receive `UNREACHABLE_LAST_RESORT` when selected.

Every generated storage zone is exactly `STANDARD` or `OVERSIZE`; `MIXED` is
not produced. Ambient user zones remain whole and the planner selects the
smallest nearby set for outliers. Chilled user zones may be partitioned into
separate standard and oversize generated subzones when capacity allows.
`planned_zone_id` records this generated identity while the original `zone_id`
continues to control chilled inheritance and static addressing.

Local attributes are stored against full static hierarchy paths, so `Z01/A01`
and `Z02/A01` are independent. Children inherit ancestor values and can override
them; clearing a local value restores inheritance. **Load saved layout…** in
the **Interactive Slotting Layout** tab restores this configuration from a v2
slotting JSON. That tab is a read-only assignment viewer; zone dragging remains
in the **Inventory Slotting** tab. The viewer accepts an order-history Excel
workbook and ranks deliverable-unit visits after grouping its rows by
`(Store ID, Date)`. AMR layouts are visualized and ranked at whole-shelf level;
ASRS layouts are visualized and ranked at tote/pallet slot level. Its movement
colour is independent of SKU and rack pick-frequency ABC classes.

The generated slotting JSON separates the static buffer from the movable unit:

- `static_address`: the fixed buffer address
- `dynamic_address`: the shelf slot, tote, or pallet address
- `storage_location_address`: the full level/slot path used by compatibility rules

Dynamic handling-unit IDs are independent of RMF waypoint and static rack IDs.
They are assigned sequentially from `001`. Complete addresses look like:

```text
AMR static:   Z03/A08/B-G11_11
AMR dynamic:  SHELF_001/L01/S01

ASRS static:  Z03/A08/B-G11_11/L01/S01
Tote dynamic: TOTE_001
```

An AMR grid buffer can contain one movable shelf, and all SKU positions on that
shelf share `SHELF_001`. Each ASRS slot buffer accepts one tote or pallet. The
summary reports `buffer_count`, `occupied_buffer_count`, `empty_buffer_count`,
and `buffer_occupancy_rate`; each buffer is counted once regardless of how many
SKUs are stored in its handling unit. The RMF-only address remains available as
`rmf_grid_address`.

After generation, the tab displays the RMF lane graph as an interactive map.
Rack points are coloured by the hottest SKU class assigned to them: A is red,
B is orange, C is green, and unused racks are grey. Workstations are blue
diamonds. Click a rack to inspect its handling-unit IDs, ABC counts, and assigned
SKU address records.

The rack-detail table focuses on inventory addressing. For every assigned SKU,
it shows the complete current static address, current dynamic address, handling
unit type and handling-unit ID. Routing and frequency values remain available
in the JSON assignment records but are omitted from the rack-detail view.

The default inputs are `demo.grid.json` and
`sku_velocity_summary.csv`. The result defaults to
`resources/data/basic_slotting_layout.slotting.json`.

## Inventory operations demo tab

The **Inventory Operations Demo** tab loads the generated `.slotting.json` file.
Because this file embeds the RMF building graph, zone assignments, strategy
metadata and every SKU assignment, the demo does not need the original YAML or
CSV files.

Available mock operations:

- Search by SKU identifier and highlight its current rack on the map.
- View the SKU's complete current static and dynamic addresses.
- Click a rack to show every assigned SKU in a scrollable inventory table,
  including each SKU's static address, dynamic address and handling-unit ID.
- Swap two individual SKU slots by clicking two SKU rows to fill the source and
  target fields, then clicking **Execute mock swap**. The SKU records exchange
  their complete location assignments.
- Swap two complete shelves by selecting **Whole shelf** and clicking the two
  occupied rack points to fill the source and target shelf fields, then clicking
  **Execute mock swap**. Every SKU remains tied to its movable shelf ID, while
  the shelves exchange fixed rack positions and all current addresses are
  recalculated.
- Save the modified layout and its timestamped operation log as a new JSON file.
