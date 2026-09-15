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
- **Delete grid points** removes a clicked point or every point crossed during a
  drag. You can also select a point and use **Delete selected** (or press the
  Delete key while the map has focus). Horizontal and vertical lanes
  automatically bridge each gap to the next remaining point.
- **Delete lanes** removes the nearest lane segment when you click it. Drag
  across multiple segments to remove several lanes in one undoable action.
- **Select / edit** exposes the selected point's X and Y coordinates. Enter new
  metre values and click **Apply point edit** to move it. Coordinates may be
  negative or extend beyond the configured total width and length; the dashed
  rectangle continues to show the original warehouse extent.
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
- An optional SKU attributes CSV. Plain attribute headers and `req_` headers
  are supported; `length`, `width`, `height`, and `chilled_required` remain
  backward-compatible aliases.
- A strategy selected from the dropdown
- Optional typed SKU requirements using `req_<attribute_key>` CSV columns.
  Definitions are the union of explicit grid-JSON definitions and definitions
  inferred from the selected CSV; the application injects no starter schema.

Use this workflow:

1. In Stock Requirements, load the SKU attributes CSV and enter the slot
   length, width, height, rack levels, and slots per level. The tab calculates
   demand-based rack requirements for every SKU and every selected Boolean
   attribute combination. A fixed overlay at the top-right of Grid Map Editor
   displays those calculated combination totals; Grid Map Editor no longer has
   separate SKU-attribute controls.
   A demand row turns dark blue when currently configured zones provide enough
   racks with the exact Boolean profile and matching STANDARD/OVERSIZE storage
   capacity. An active Boolean flag omitted from a zone is counted as `false`,
   so operators only need to label the `true` zones. Multiple matching zones
   contribute their rack counts to the same demand row.
   Use **Rack grouping attributes** in Stock Requirements to choose which
   Boolean attributes participate in this rack calculation. The
   selection changes only the overlay grouping; it does not remove attribute
   definitions or change the rules enforced during slotting. Physical
   STANDARD/OVERSIZE classification is always retained.
   Generate buffers, assign every rack to a zone, then configure those values
   through zone settings or the advanced hierarchy editor.

In **Warehouse Settings**, enter the default standard-storage length, width,
height, and weight capacity, then choose **Apply storage defaults to zones**.
These values are saved in the grid project JSON, replace the built-in standard
envelope for STANDARD/OVERSIZE classification, and are copied into every
existing and newly created zone. Zone Settings can then override the copied
capacity for an individual zone. **Reset all to standard** in Zone Settings
uses these saved warehouse values rather than application constants.
Zone Settings can be opened as soon as at least one rack has a zone. Other
racks may remain unassigned while that zone is configured. The advanced
hierarchy editor likewise builds paths only for currently assigned racks;
unassigned racks are not silently placed into a temporary or default zone.
2. Save the `.grid.json`, then browse for it in Inventory Slotting and click
   **Load project**.
3. Browse for the ABC SKU velocity CSV and optional SKU attributes CSV. Physical
   requirement columns are `req_max_item_length`, `req_max_item_width`,
   `req_max_item_height`, and `req_max_item_weight`. A positive SKU weight
   enables a soft preference for middle rack levels; weight `0` disables this
   ergonomic preference for that SKU.
   The included `medicine_sku_attributes.csv` adds workbook-derived dimensions
   and weight plus the Boolean `chilled`, `tablet`, and `flammable`
   requirements for all sample SKUs.
4. Select the strategy, then generate the layout.

The saved map is authoritative during slotting. The allocator preserves every
zone ID, rack membership, boundary, and local attribute exactly as configured,
except for maximum weight on an explicitly oversize-capable zone. That one
capacity is raised to the heaviest compatible SKU in the selected dataset and
is carried into the generated layout.
It does not generate subzones, rename zones, write missing SKU attributes into
locations, or create STANDARD/OVERSIZE partitions. For each candidate location,
the allocator first collects the attributes configured across all zones. An SKU
attribute absent from every zone is ignored. Once an attribute exists in any
zone, it becomes active map-wide. A missing Boolean zone value defaults to
`false`; a missing numeric value remains undefined. A defined Boolean or numeric
mismatch is rejected. Physical footprint and weight rules
apply when their corresponding capacities are configured anywhere in the map.
Every dimensional oversize, overweight, combined, or `NON_VOLUMETRIC_DATA`
physical exception additionally requires a zone with `oversize_capable=true`.
Standard zones reject these SKUs. Items without usable dimensions remain marked
unverified and reserve one slot because a larger footprint cannot be calculated.
Contiguous multi-slot or multi-level occupancy is only attempted inside an
oversize-capable zone and never changes that zone's values.

The editor keeps slot capacity and global machine carrying capacity as separate
inputs. AMR layouts enable only the global whole-rack weight field. Mini-load
and pallet ASRS layouts enable global L/W/H and weight. The Stock Requirements
tab exposes the same warehouse-type-dependent fields. A SKU becomes oversize or
overweight when it exceeds either its slot capacity or the applicable global
machine limit.

Warehouse dimensions use metres and weights use kilograms. Defaults are
calibrated from the included SKU attributes and quantity target: slots use
`1.9 × 0.5 × 1.0 m`
and a cumulative `12.5 kg` slot limit, ASRS machines use a
`0.25 × 0.193 × 0.192 m` envelope and `0.465 kg` limit, and AMR uses a
`150 kg` whole-rack limit with 3 levels and 4 slots per level by default.

All nonphysical SKU attributes are Boolean flags. In **Advanced attributes…**,
their hierarchy level controls editor organization and inheritance. It does not
instruct slotting to subdivide a zone. Only length, width, height, and weight
remain numeric capacity attributes.

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
as the rack score. It sorts SKUs by ABC class and pick frequency, then enforces
only the chilled, physical, and custom attributes configured on the candidate
map location. Compatible racks are filled before another rack is opened, so ABC
classes may mix. It never changes zone geometry or creates exception segments.
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

Unknown physical values remain marked unverified. They do not cause the map to
be mutated or create an exception segment.
Racks without a complete route to every workstation rank last but remain usable
and receive `UNREACHABLE_LAST_RESORT` when selected.

Result rows retain the original map zone. `planned_zone_id` is therefore the
configured `zone_id`, and `generated_attribute_zones` remains empty.

Local attributes are stored against full static hierarchy paths, so `Z01/A01`
and `Z02/A01` are independent. Children inherit ancestor values and can override
them; clearing a local value restores inheritance. **Load saved layout…** in
the **Interactive Slotting Layout** tab restores this configuration from a v2
slotting JSON. Zone boundaries have no text badges, leaving rack points
unobstructed, and neighboring perimeters are inset to leave a visible gap.
Select a rack to edit **Zone name** in Rack details, then choose
**Apply & save**. This renames the entire zone consistently in the current
layout, including static addresses, generated zones, and zone attributes.
Existing zone names are rejected to prevent an accidental merge. Zone dragging
remains in the **Inventory Slotting** tab. The viewer accepts an order-history Excel
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

If Stock Requirements was calculated in the current application session, its
quantity targets feed Inventory Slotting automatically. Repeated loads of one
SKU fill adjacent compatible positions on the preferred ergonomic rack level
up to **Maximum same-SKU slots/rack**, then continue on another compatible
level or rack. The limit counts physical occupied cells and defaults to full
rack capacity. Each physical
oversize copy continues to reserve one contiguous merged footprint rather than
being separated into independent footprint cells.

After generation, the tab displays the RMF lane graph as an interactive map.
Rack points are coloured by the hottest SKU class assigned to them: A is red,
B is orange, C is green, and unused racks are grey. Workstations are blue
diamonds. Click a rack to inspect its handling-unit IDs, ABC counts, and assigned
SKU address records.

The rack-detail table focuses on inventory addressing. For every assigned SKU,
it shows the complete current static address, current dynamic address, handling
unit type and handling-unit ID. Routing and frequency values remain available
in the JSON assignment records but are omitted from the rack-detail view.

The default inputs are `map1.grid.json` and
`sku_velocity_summary.csv`. The result defaults to
`resources/data/basic_slotting_layout.slotting.json`.

## Inventory operations demo tab

The **Inventory Operations Demo** tab loads the generated `.slotting.json` file.
Because this file embeds the RMF building graph, zone assignments, strategy
metadata and every SKU assignment, the demo does not need the original YAML or
CSV files.

Available mock operations:

- Search by SKU identifier and highlight every rack containing that SKU.
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
