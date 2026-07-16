# RMF Grid Map Editor

This Python application creates an image-free Open-RMF `.building.yaml` map.
It uses `cartesian_meters`, so the bottom-left grid point is `G0_0` at `(0, 0)`
and positive Y points upward.

## Start the editor

```bash
cd layout-management
python3 rmf_grid_map_editor.py
```

Enter:

- Total warehouse width in metres
- Total warehouse length in metres
- Distance between grid points in metres
- Map and RMF level names

Width and length must be exact multiples of grid spacing. Click **Generate / reset
grid** to create all vertices and edges. Every point is initially connected to
its horizontal and vertical neighbours, and every lane is bidirectional.

## Mark operational points

Choose a placement tool:

- **Paint rack pickups** lets you hold the mouse button and drag across a row
  or irregular set of grid points.
- **Fill rack rectangle** takes two clicks on opposite corners and marks every
  grid point inside the rectangle as a pickup.
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

## Command-line generation

An empty 20 m × 15 m map with 1 m grid spacing:

```bash
python3 rmf_grid_map_editor.py --generate \
  --width 20 --length 15 --spacing 1 \
  --name warehouse_grid \
  --output resources/map/v6.building.yaml
```

Markers may be supplied as `ROLE,COLUMN,ROW,ENDPOINT_ID`:

```bash
python3 rmf_grid_map_editor.py --generate \
  --width 20 --length 15 --spacing 1 \
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

- An RMF `.building.yaml` containing rack `pickup_dispenser` points and
  workstation `dropoff_ingestor` points
- The ABC SKU velocity summary CSV
- An optional selected-SKU chilled requirements CSV
- A strategy selected from the dropdown
- The handling-unit model: AMR shelf, tote, or pallet
- Levels and slots per rack
- Optional typed hierarchy attributes and matching `req_<attribute_key>` CSV
  columns

Use this workflow:

1. Browse for the building YAML and click **Load map**.
2. The first zone ID defaults to `Z01`.
3. Keep **Rectangle zone selection** enabled and drag over a group of racks.
4. Repeat until every rack belongs to a zone. After each successful rectangle,
   the ID advances automatically to `Z02`, `Z03`, and so on. IDs such as
   `ZONE_001` advance to `ZONE_002` while preserving their numeric width.
5. Set levels and slots, then click **Zone storage settings…**. Limits default
   to 15 × 16 × 13 and weight 250 in unconfirmed source units. No zone is
   pre-labelled or automatically enlarged for oversize storage.
6. Mark actual chilled zones. Chilled defaults to false and is never inferred
   from the map.
7. Use **Advanced attributes…** for extra inherited values or lower-level
   overrides. A child slot can use larger limits such as the sample maximums
   150 × 50 × 95 and weight 640 while its parent zone remains normal.
8. Browse for the ABC SKU velocity CSV and optional chilled CSV. Physical
   requirement columns are `req_max_item_length`, `req_max_item_width`,
   `req_max_item_height`, and `req_max_item_weight`.
9. Select the strategy and handling-unit type, then generate the layout.

For `abc_affinity`, select the order-history workbook and an affinity weight.
The first generation automatically suggests minimum shared store-days, minimum
affinity score, and maximum service-distance increase from that workbook and
warehouse map. The values appear after generation and become editable. Use
**Regenerate with edited values** to test an adjustment or **Recalculate
automatic suggestion** after changing the source data.

Racks are coloured by zone while grouping. Generation is blocked until all
racks have a zone. After generation, rectangle mode turns off and rack colours
change to their assigned ABC class. Aisles are derived from rack columns
(common X coordinates), and bays are ordered along each column. Aisles are
numbered independently inside every zone, so each zone starts at `A01`.

The demo **basic** strategy calculates the shortest directed graph route from
each rack to every workstation, then uses the average of those route distances
as the rack score. It sorts SKUs by ABC class and pick frequency, checks chilled
exclusivity first, then uses rotation-aware dimensions, weight, and custom
attributes as best-fit preferences. Chilled versus ambient is the only hard
storage boundary. If a matching-temperature slot needs different soft values,
the generator writes local overrides on that child slot and assigns the SKU.
Missing physical data is assigned with an `UNVERIFIED` warning. A general
not-enough-space result occurs only when every slot is occupied.

The **abc_affinity** strategy retains that ABC-first allocation and all storage
constraints. It uses store-day cosine affinity only to rank locations that are
otherwise eligible at the same ABC/physical priority, balancing service distance
against proximity to already placed related SKUs. Automatic thresholds come
from empirical distributions in the selected workbook and map; they are not
fixed to the bundled sample data.

Chilled and ambient capacity is counted separately because neither category may
use slots from the other. The result distinguishes this temperature-zone
shortage from global not-enough-space.

An automatic override is recommendation metadata, not a physical modification
to a rack. Validate generated limits against real equipment before production.
Racks without a complete route to every workstation rank last but remain usable
and receive `UNREACHABLE_LAST_RESORT` when selected.

Zone storage type is generated after allocation from actual contents:
`STANDARD`, `OVERSIZE`, `MIXED`, or `UNUSED`. It is output metadata and is not a
pre-generation zone role. Compatibility always uses the effective slot values,
including child overrides inherited from or replacing parent values.

Local attributes are stored against full static hierarchy paths, so `Z01/A01`
and `Z02/A01` are independent. Children inherit ancestor values and can override
them; clearing a local value restores inheritance. **Load previous layout…**
restores this configuration from a v2 slotting JSON.

The generated slotting JSON contains both address levels:

- `static_address`: zone, aisle, fixed grid bay, level and slot
- `dynamic_address`: the current full address with the movable-unit ID applied
  at its correct hierarchy layer

Dynamic handling-unit IDs are independent of RMF waypoint and static rack IDs.
They are assigned sequentially from `001`. Complete addresses look like:

```text
AMR static:   Z01/A05/BAY-G611/L01/S01
AMR dynamic:  Z01/A05/BAY-SHELF_001/L01/S01

ASRS static:  Z01/A05/BAY-G611/L01/S01
Tote dynamic: Z01/A05/BAY-G611/L01/SLOT-TOTE_001
```

An AMR shelf is movable at the bay layer, so all SKU slots on one shelf share
the same `SHELF_001` ID. An ASRS tote or pallet is movable at the slot layer,
so each slot receives an independent ID such as `TOTE_001` or `PALLET_001`.
Each assignment records this explicitly as `dynamic_address_level` (`bay` or
`slot`). The RMF-only address remains available separately as
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

The default inputs are the existing `demo.building.yaml` and
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
