# RMF Grid Map Editor — User Manual

`rmf_grid_map_editor.py` is a Python desktop application for creating a
grid-based Open-RMF warehouse map, assigning inventory zones, generating a
basic ABC slotting layout, exploring SKU/store affinity, and demonstrating
inventory search and position swaps.

The application does not require a warehouse drawing. Grid point `(0, 0)` is
the bottom-left point of a generated map, and positive Y points upward.

## Contents

1. [Install and start](#install-and-start)
2. [Application workflow](#application-workflow)
3. [Grid Map Editor tab](#1-grid-map-editor-tab)
4. [SKU Affinity tab](#2-sku-affinity-tab)
5. [Inventory Slotting tab](#3-inventory-slotting-tab)
6. [Inventory Operations Demo tab](#4-inventory-operations-demo-tab)
7. [Inventory address rules](#inventory-address-rules)
8. [Command-line map generation](#command-line-map-generation)
9. [Files and folders](#files-and-folders)
10. [Code architecture](#code-architecture)
11. [Troubleshooting](#troubleshooting)

## Install and start

### Requirements

- Python 3.10 or newer
- Tkinter
- PyYAML, openpyxl and matplotlib

On Ubuntu or Debian, install the required packages with:

```bash
sudo apt install python3 python3-tk python3-pip
python3 -m pip install PyYAML openpyxl matplotlib
```

Clone the public repository and start the application:

```bash
git clone https://github.com/s-w3i/layout-management.git
cd layout-management
python3 rmf_grid_map_editor.py
```

The application opens with four tabs:

| Tab | Purpose |
|---|---|
| **Grid Map Editor** | Create the grid, place racks and workstations, and export RMF YAML |
| **SKU Affinity** | Explore SKU–store frequency and SKU relationships from line-level orders |
| **Inventory Slotting** | Generate an ABC-only or ABC-plus-affinity recommendation |
| **Inventory Operations Demo** | Search inventory and demonstrate SKU or AMR-shelf swaps |

## Application workflow

For a new warehouse, use the tabs in this order:

1. Create the warehouse grid in **Grid Map Editor**.
2. Place rack pickup points and workstation drop-off points.
3. Save the editable project and export the RMF building YAML.
4. Open **SKU Affinity**, analyze the order workbook, and review SKU relationships.
5. Export the affinity snapshot and CSV review files when required.
6. Open **Inventory Slotting** and load the building YAML.
7. Group every rack into a zone.
8. Review the initialized zone capacities and mark real chilled zones.
9. Load the ABC SKU velocity CSV and optional chilled-requirements CSV. Choose
   `basic` or `abc_affinity`; for affinity, also select the order-history Excel
   file and the desired ABC/affinity weight.
10. Open **Inventory Operations Demo** and load the generated `.slotting.json`.
11. Search for SKUs or demonstrate position swaps, then save changes when required.

To use the included demonstration map, start directly from the Inventory
Slotting tab. Its default building file is:

```text
resources/map/demo.building.yaml
```

## 1. Grid Map Editor tab

### Create the warehouse grid

Enter the following values:

| Field | Meaning |
|---|---|
| **Map name** | Name written into the RMF building file |
| **Level name** | RMF floor or level name, such as `L1` |
| **Total width (m)** | Warehouse size along the X axis |
| **Total length (m)** | Warehouse size along the Y axis |
| **Distance per grid (m)** | Real distance between neighbouring grid points |

Width and length must be exact multiples of the grid distance. For example, a
20 m width with 1 m spacing produces points from X=0 through X=20, including
both boundaries.

Click **Generate / reset grid**. The editor creates horizontal and vertical
edges between neighbouring points. All generated lanes are bidirectional by
default.

> Generating a new grid removes the current rack and workstation markers after
> confirmation.

### Place rack pickup points

Set **Rack ID prefix**, then choose one of these tools:

- **Paint rack pickups (drag)** — hold the left mouse button and paint rack
  points individually or along an irregular shape.
- **Fill rack rectangle (2 clicks)** — click two opposite corners to fill every
  grid point in the rectangle with racks.

Rack IDs are generated from the prefix and grid location. They can be changed
later with **Select / edit**.

### Place workstation drop-off points

Choose **Place workstation drop-off**, then click a grid point. The resulting
RMF vertex is exported with a `dropoff_ingestor` property.

Use **Select / edit** to give the workstation a meaningful endpoint ID, such as
`WS_INBOUND_01` or `WS_PACKING_01`.

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

- **Save editable project…** writes a `.grid.json` file. Use this format when
  you want to continue editing the grid later.
- **Export RMF building YAML…** writes a `.building.yaml` file for RMF and the
  Inventory Slotting tab.

Use **Load editable project…** to reopen a `.grid.json` project. A building YAML
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
| Building YAML | `resources/map/demo.building.yaml` |
| ABC SKU velocity CSV | `resources/data/sku_velocity_output/sku_velocity_summary.csv` |
| Chilled SKU CSV | `resources/data/demo_chilled_requirements.csv` |
| Affinity order workbook | `resources/data/Sample Data.xlsx` |
| Output layout | `resources/data/basic_slotting_layout.slotting.json` |

The YAML must contain at least one rack with `pickup_dispenser` and at least one
workstation with `dropoff_ingestor`.

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
attribute. A blank core dimension or weight is instead treated as missing
physical data and classifies the SKU as `UNVERIFIED_OVERSIZE`. Requirement keys
must exist in the attribute catalog before generation.

The included compact CSV is ready for the demo. To analyse another warehouse,
place its transaction workbook under `resources/data/` and follow the
[SKU velocity analysis guide](docs/sku-velocity-analysis.md). Raw workbooks and
generated demand plots are intentionally excluded from the public repository.

### Load the building map

1. Confirm or browse for the **Building YAML**.
2. Click **Load map**.
3. Confirm that the status reports the expected rack and workstation counts.

### Assign rack zones

Every rack must have a zone before slotting can be generated.

1. Leave **Rectangle zone selection** enabled.
2. Enter the first **Zone ID**, normally `Z01`.
3. Drag a rectangle around a group of rack points.
4. Repeat until the status reports zero unassigned racks.

With **Auto next ID** enabled, the zone advances automatically after each
successful selection:

```text
Z01 → Z02 → Z03
```

IDs such as `ZONE_001` also advance while preserving their numeric width. Use
**Clear zones** to restart zone assignment.

### Aisle and bay rules

- A zone is a collection of racks.
- Each different rack column, identified by its X coordinate, is a separate
  aisle.
- Aisle numbering restarts at `A01` in every zone.
- The fixed bay ID uses the RMF grid waypoint name.
- Bays in an aisle are ordered by their Y coordinate.

For example, racks in two columns inside `Z02` are addressed under `Z02/A01`
and `Z02/A02`, even if another zone already uses those aisle numbers.

### Configure rack capacity and handling units

Select:

- **Strategy** — `basic` keeps the existing ABC-only flow; `abc_affinity`
  preserves ABC and physical constraints, then uses store-day relationships to
  choose among otherwise eligible locations.
- **Affinity weight** — user-selected from 0% (service/ABC emphasis) to 100%
  (affinity emphasis within the ABC constraints).
- **Handling unit** — `AMR shelf`, `Tote`, or `Pallet`.
- **Levels** — vertical storage levels in each fixed bay.
- **Slots per level** — SKU positions on each level.

The handling-unit choice controls where the dynamic identity appears in the
address. See [Inventory address rules](#inventory-address-rules).

### Configure zone storage settings

Click **Zone storage settings…** after grouping the racks. The application
initializes all zones with these source-unit demo limits:

| Storage | Length | Width | Height | Weight |
|---|---:|---:|---:|---:|
| Standard | 15 | 16 | 13 | 250 |

No zone is designated as oversize before generation. Change the capacity values
only to match real storage limits. The units are deliberately labelled as
unconfirmed source units; the demo does not perform a centimetre, millimetre,
gram or kilogram conversion.

Mark only physically chilled zones with the **Chilled area** checkbox. Chilled
defaults to false because it cannot be inferred safely from an RMF map. Zone
values inherit down to every aisle, bay, level and slot.

Use **Advanced attributes…** to give a particular aisle, bay, level, or slot a
larger capacity than its parent. For the sample data, 150 × 50 × 95 and weight
640 are available as reference maximum values, but they are not automatically
applied to a zone. An oversize SKU can therefore use an enlarged child slot
inside an otherwise normal zone.

### Configure advanced hierarchy attributes

After assigning every rack to a zone and setting the rack capacity, click
**Advanced attributes…**. The editor uses the static hierarchy:

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

If zones or rack capacity change, values on unchanged address paths are kept.
The application asks before discarding values whose paths no longer exist.
Use **Load previous layout…** to restore zones, capacity, catalog, local values,
and assignments from an existing `.slotting.json`.

### Generate the slotting layout

1. Confirm that every rack has a zone.
2. Confirm the SKU CSV and output paths.
3. Select the handling unit and rack capacity.
4. Review zone limits and chilled areas; optionally add advanced attributes.
5. Confirm or clear the optional chilled-SKU CSV path.
6. Click **Generate slotting layout**.

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
3. Sorts and allocates all class A inventory before B, and all B before C.
4. Within each ABC class, groups standard and physical-exception SKUs, then
   applies descending pick frequency.
5. Fills an existing rack of the same ABC and physical group before opening a
   new rack. Another ABC class enters that rack only when dedicated capacity is
   exhausted, so mixing is limited to transition racks.
6. Uses ambient versus chilled as the only hard location boundary.
7. Prefers a slot that already satisfies dimensions, weight, and custom
   requirements. When oversize inventory must share a rack with standard
   inventory, oversize and unverified-oversize SKUs use L03 (or the highest
   available level when the rack has fewer than three levels).
8. If a matching-temperature slot needs different soft attributes, writes
   those requirements as local child-slot overrides and assigns the SKU.
9. Uses average workstation distance to rank otherwise equivalent racks and
   returns general not-enough-space only after every storage slot is occupied.

The **abc_affinity** strategy runs that same ABC-first ordering and compatibility
logic. For eligible locations at the same ABC/physical priority, it minimizes a
weighted combination of service distance and distance to already placed related
SKUs. Relationship strength is cosine similarity of binary SKU presence by
`(Store ID, Date)`, weighted by shared store-days. The generated layout reports
its selected parameters and compares affinity-pair and service distance against
the basic baseline.

Chilled SKUs still require chilled slots and ambient SKUs require non-chilled
slots. If one temperature category has insufficient slots, the result reports a
temperature-zone shortage even when the opposite category has empty capacity.

The optional chilled CSV contains `sku,chilled_required`. Only selected chilled
SKUs need rows; all absent SKUs are ambient. The included file selects 10% of
the 1,524 sample SKUs with seed 42. Invalid booleans, duplicates, unknown SKUs,
or conflicts with `req_chilled` stop generation.

An SKU with missing physical data is assigned to an available slot and remains
`UNVERIFIED`. Complete oversize, overweight, and custom requirements can produce
`COMPATIBLE_AUTO_OVERRIDE`. The generated local override records what the slot
would need to support; it does not physically increase rack capacity. Production
users must validate generated overrides against the actual equipment.

After allocation, each zone receives a generated result type: `STANDARD` when
it holds only standard SKUs, `OVERSIZE` when it holds only exception or
unverified SKUs, `MIXED` when it holds both, and `UNUSED` when empty. This is
output metadata—not a zone setting used to restrict allocation.

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

## 4. Inventory Operations Demo tab

### Load a slotting layout

1. Confirm or browse for the `.slotting.json` file.
2. Click **Load layout**.
3. Click any occupied rack to display every SKU in that rack, including the
   same chilled and physical-exception storage flags shown during slotting.

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

### Static address

The static address always describes a fixed warehouse position:

```text
ZONE/AISLE/FIXED-BAY/LEVEL/SLOT
Z01/A05/BAY-G611/L01/S01
```

### AMR shelf: dynamic at bay level

An AMR shelf is the movable bay. Every SKU position on the shelf shares the
same shelf ID:

```text
Static:  Z01/A05/BAY-G611/L01/S01
Dynamic: Z01/A05/BAY-SHELF_001/L01/S01
```

### Tote and pallet: dynamic at slot level

Each tote or pallet has an independent ID at the storage-slot layer:

```text
Static:         Z01/A05/BAY-G611/L01/S01
Tote dynamic:   Z01/A05/BAY-G611/L01/SLOT-TOTE_001
Pallet dynamic: Z01/A05/BAY-G611/L01/SLOT-PALLET_001
```

The JSON field `dynamic_address_level` records `bay` for an AMR shelf and
`slot` for a tote or pallet.

## Command-line map generation

The script can generate a map without opening the GUI.

Create an empty 20 m × 15 m grid with 1 m spacing:

```bash
python3 rmf_grid_map_editor.py --generate \
  --width 20 \
  --length 15 \
  --spacing 1 \
  --name warehouse_grid \
  --level L1 \
  --output resources/map/new_warehouse.building.yaml
```

Add markers using `ROLE,COLUMN,ROW,ENDPOINT_ID`:

```bash
python3 rmf_grid_map_editor.py --generate \
  --width 20 --length 15 --spacing 1 \
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
│   ├── inventory.py                               Search and swap service
│   ├── rmf.py                                     RMF/project persistence service
│   └── slotting.py                                Routing, addressing and slotting
├── tests/
│   ├── test_attributes.py                         Attribute and compatibility tests
│   ├── test_affinity.py                           Affinity analysis and cache tests
│   └── test_services.py                           Service regression tests
├── docs/
│   ├── README.md                                  Documentation index
│   ├── rmf-grid-map-editor.md                     Additional editor notes
│   └── sku-velocity-analysis.md                   ABC analysis guide
└── resources/
    ├── data/
    │   ├── Sample Data.xlsx                     Optional local input (ignored)
    │   ├── demo_chilled_requirements.csv        Seeded chilled demo input
    │   ├── basic_slotting_layout.slotting.json  Generated output (ignored)
    │   └── sku_velocity_output/
    │       ├── sku_velocity_summary.csv
    │       └── demand_plots/
    ├── map/
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
| `inventory.py` | `InventoryService` | SKU lookup, SKU-slot swap and AMR-shelf swap |
| `attributes.py` | `StorageAttributeService` | Inheritance, physical classification and compatibility |
| `zone_settings_editor.py` | `ZoneStorageSettingsEditor` | Chilled and physical capacity input by zone |
| `gui.py` | `GridMapEditorApp` | Tkinter widgets and user interaction |
| `cli.py` | `GridMapEditorCommand` | Command-line parsing and application startup |

Run the service regression tests from the repository root:

```bash
python3 -m unittest discover -s tests -v
```

On Linux, run the automated four-tab GUI workflow with a virtual display:

```bash
sudo apt install xvfb
xvfb-run -a python3 -m tests.gui_workflow_smoke
```

The GUI smoke test covers grid editing, affinity loading and filtering, affinity
exports, automatic and edited affinity slotting, map loading and drawing, rectangle zone selection, all handling-unit
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

### Width or length is not an exact multiple

Change the warehouse dimension or grid distance so division produces a whole
number. For example, 20 m works with 1 m spacing, while 20 m does not work with
3 m spacing.

### Building YAML has no racks or workstations

The slotting input requires:

- at least one `pickup_dispenser` rack vertex; and
- at least one `dropoff_ingestor` workstation vertex.

Return to Grid Map Editor, add the missing markers, and export the YAML again.

### Slotting says racks remain unassigned

Every rack must belong to a zone. Enable **Rectangle zone selection** and group
the remaining grey rack points before generating.

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
- [Additional RMF editor notes](docs/rmf-grid-map-editor.md)
