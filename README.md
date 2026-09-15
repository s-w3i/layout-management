# Warehouse Layout Management

Design an AMR warehouse, generate slotting layouts, and compare throughput with
a deterministic discrete-event simulator.

## Quick start

Requirements: Python 3.10+, Tkinter, PyYAML, openpyxl, matplotlib, and OR-Tools.

```bash
sudo apt install python3 python3-tk python3-pip
python3 -m pip install PyYAML openpyxl matplotlib ortools
git clone https://github.com/s-w3i/layout-management.git
cd layout-management
python3 rmf_grid_map_editor.py
```

The desktop app provides grid editing, SKU analysis, slotting, traffic
optimization, and inventory operations.

## Current-heat Pygame simulation

Run the copied current-heat planner on the native grid, with store/day tasks
released at time zero using the AMR workload rules:

```bash
python3 -m current_heat_simulation.pygame_simulator
```

See [current_heat_simulation/README.md](current_heat_simulation/README.md) for
date, layout, fleet, headless-run, and output options. Pygame is required.

## Compare four slotting layouts

Run all observed order dates with 40 AMRs and one persistent worker per layout:

```bash
python3 amr_simulation/run_simulation.py \
  --mode batch \
  --grid resources/map/map1_1.grid.json \
  --orders "resources/data/Sample Data.xlsx" \
  --config amr_simulation/config/default.json \
  --layout resources/map/map1_basic.slotting.json \
  --layout resources/map/map1_pure_affinity.slotting.json \
  --layout resources/map/map1_traffic_zone_balance_off.slotting.json \
  --layout resources/map/map1_traffic_zone_balance_on.slotting.json \
  --amrs 40 \
  --workers 4 \
  --output amr_simulation/results/all_layouts_40_amrs
```

Open the final comparison:

```text
amr_simulation/results/all_layouts_40_amrs/layout_comparison.csv
```

Useful batch options:

- `--workers N`: maximum parallel layout workers.
- `--amrs N`: fleet size from 1 to 40 using configured spawn nodes.
- `--start-date YYYY-MM-DD --end-date YYYY-MM-DD`: limit the date range.
- `--event-log`: export detailed events; omit it for faster runs.

## Run the live simulation

```bash
python3 amr_simulation/run_simulation.py \
  --mode debug \
  --grid resources/map/map1_1.grid.json \
  --orders "resources/data/Sample Data.xlsx" \
  --config amr_simulation/config/default.json \
  --layout resources/map/map1_basic.slotting.json \
  --date 2023-01-03 \
  --speed 120 \
  --output amr_simulation/results/live_demo
```

The window includes play/pause, next-event, restart, and speed controls. Use
`--speed 1` for wall-clock playback.

## Workflow

1. Create the grid, racks, workstations, zones, and lanes in **Grid Map Editor**.
2. Analyze orders in **SKU Affinity** and calculate stock requirements.
3. Generate a layout in **Inventory Slotting**.
4. Improve it with **Traffic-Aware Slotting** or **Global Traffic Optimizer**.
5. Compare throughput in batch mode, then inspect one date in live mode.

## Simulator behavior

- Directed and bidirectional lanes are honored by deterministic A* routing.
- A* plans only the active pickup, delivery, or rack-return stage.
- Every workstation processes one robot at a time; arrivals queue physically.
- AMRs reserve available straight-path nodes and release each node after crossing.
- Blocked AMRs reroute after five simulated seconds using stage-local tabu nodes.
- A rack stays reserved until it returns home and jack-down finishes.
- Original order lines complete at jack-down; quantities do not multiply lines.
- All tasks for a selected date start at simulation time zero.

V1 uses node ownership rather than a time-expanded reservation table.

## Outputs

Each layout directory contains:

- `daily_metrics.csv`
- `summary.json`
- `config_snapshot.json`
- `store_workstation_mapping.json`
- `validation_report.json`

Multiple layouts also produce `layout_comparison.csv`. Detailed runs add
`event_log.csv`.

## Main files

| Path | Purpose |
|---|---|
| `rmf_grid_map_editor.py` | Launch the desktop app |
| `warehouse_layout/` | Grid, slotting, traffic, and GUI code |
| `amr_simulation/` | Simulator, debugger, CLI, and default config |
| `resources/map/` | Grid and slotting layout files |
| `resources/data/` | Orders, SKU attributes, and stock inputs |
| `tests/` | Automated tests |

## Troubleshooting

**Missing Python module**

```bash
python3 -m pip install PyYAML openpyxl matplotlib ortools
```

**Tkinter is missing**

```bash
sudo apt install python3-tk
```

**Simulation validation fails**

Open `validation_report.json`. It identifies invalid spawns, stations, racks,
routes, and unmapped SKUs.

**Batch run is slow**

Use `--workers 4`, omit `--event-log`, and confirm the workload cache exists at
`amr_simulation/.cache/`.

**Need more detail**

See [`docs/`](docs/) for detailed map editor, traffic optimizer, and slotting
documentation.
