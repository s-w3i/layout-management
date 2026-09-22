# ASRS pick-and-return simulation

Nine fixed-aisle cranes retrieve single totes from either adjacent rack, present them at their own workstations, and return them to the original slots. After returning a tote, the crane travels directly to its next pickup. All inputs come from the existing map, slotting layouts, and store/day workbook; nothing writes back to those files.

## Run

Use Python 3.10+ from the repository root:

```bash
cd /home/usern/layout_management_master
# Only needed if dependencies are missing:
python3 -m pip install -r asrs_simulation/requirements.txt

# Pygame debug: basic layout, earliest observed date.
python3 -m asrs_simulation.run_simulation

# Compare all three layouts on one day.
python3 -m asrs_simulation.run_simulation --headless --date 2023-01-03 \
  --output asrs_simulation/results/one_day

# Select multiple dates AND layouts, using the same repeated flags as current_heat_simulation.
# This runs all four combinations: 2 dates × 2 layouts.
python3 -m asrs_simulation.run_simulation --headless \
  --date 2023-01-03 --date 2023-01-04 \
  --layout resources/map/asrs/basic_zone_on_slotting_layout.slotting.json \
  --layout resources/map/asrs/affinity_slotting_layout.slotting.json \
  --workers 4 --output asrs_simulation/results/selected_dates_layouts

# Fast independent runs over every observed date, all three layouts.
python3 -m asrs_simulation.run_simulation --headless --workers 4 \
  --output asrs_simulation/results/all_days

# Resume that exact batch, reusing verified successful day outputs.
python3 -m asrs_simulation.run_simulation --headless --workers 4 \
  --output asrs_simulation/results/all_days --resume

# Debug a particular layout and date.
python3 -m asrs_simulation.run_simulation --date 2023-01-03 \
  --layout resources/map/asrs/affinity_slotting_layout.slotting.json

# Select a date range; --date can alternatively be repeated.
python3 -m asrs_simulation.run_simulation --headless \
  --start-date 2023-01-03 --end-date 2023-01-10
```

Run `python3 -m asrs_simulation.run_simulation --help` for all options. `--layout` may be repeated in headless mode; `--baseline-layout` accepts a supplied layout's name or path. `--grid`/`--map`, `--orders`, `--config`, and `--max-seconds` override the inputs or daily time limit. Debug mode accepts one date/layout. Exit codes: 0 = all completed, 1 = incomplete/failed day, 2 = invalid input, 130 = interrupted.

Repeat `--date` once per date and `--layout` once per layout. Every selected layout runs on every selected date, with one combined comparison report under `--output`. Omitting `--layout` uses all three YAML layouts in headless mode; omitting dates uses all observed dates. Use either repeated `--date` flags or `--start-date`/`--end-date`, not both. Add `--resume` to the same command to reuse its verified completed runs.

Headless runs use `tqdm`, like current heat: one completed-order-line progress bar per layout for the current date, showing simulated seconds and scheduling/simulation/export status. Layouts run in parallel up to `--workers`; all layouts finish before the next date starts. Resumed days show full bars marked `resumed`, and incomplete days retain their actual line counts. Final status stays in each bar instead of printing a separate completion message per run.

The first run reads the Excel workbook. Subsequent runs use the existing workload loader's NumPy cache inside `asrs_simulation/.cache/`. Parallel workers receive the compact shared workload and slot records; they do not re-read Excel. Results and caches are ignored by Git.

## YAML configuration

Edit `asrs.yaml`. Paths in YAML resolve relative to the YAML file; CLI paths resolve relative to the working directory. `layout_files` order sets the default debug layout. Basic slotting is the default comparison baseline when included.

| Configuration | Default | Meaning |
|---|---:|---|
| `motion.x.speed_mps` / `acceleration_mps2` | 3 / 1 | Horizontal rail motion |
| `motion.y.speed_mps` / `acceleration_mps2` | 0.5 / 0.5 | Fork extension/retraction |
| `motion.z.speed_mps` / `acceleration_mps2` | 1 / 0.5 | Vertical lift motion |
| `timing.picking_seconds` | 4 | Per tote presentation, including workstation handoff |
| `timing.extraction_dwell_seconds` | 0 | Additional dwell before taking the tote |
| `timing.replacement_dwell_seconds` | 0 | Additional dwell before releasing the tote |
| `workstation_height_m` | 0.5375 | Workstation transfer height |
| `simulation.max_seconds` | 86400 | Daily simulated time limit |

These are editable engineering assumptions, not a manufacturer's performance specification. Deceleration equals acceleration. Speeds and accelerations must be positive and finite; delays can be zero. Display dimensions must be at least 1200 × 900.

`aisles` explicitly maps each crane to its workstation and two adjacent rack rows. Current rack-row pairs run from `[0,2]` through `[24,26]`, with workstations at column 21. Slot positions come from actual map X/Y/Z geometry, including the four rack levels and three horizontal positions per level. Preflight checks layout assignments against the map and rejects missing/inaccessible demand, inconsistent coordinates, duplicate occupied slots/IDs, shared rack rows, and unsupported multi-slot loads. Saved layout source paths and exported AMR-style `aisle_id` fields are not used for crane reachability.

## Motion and demand model

Each cycle has these stages:

1. Travel empty to the pickup X/Z position, starting at the workstation on the first job.
2. Extend Y into the rack, wait extraction dwell, retract with the tote.
3. Travel loaded to the workstation and wait the per-presentation picking delay.
4. Travel loaded back to the original slot, extend Y, wait replacement dwell, retract empty.
5. Start the next pickup directly from this position. There is no extra workstation leg or end-of-day homing.

X and Z start together and move independently, with the longer axis setting travel duration. Y only moves when X/Z are stationary, and is fully retracted before rail/lift travel. For distance `d`, maximum speed `v`, and acceleration `a`, travel time is `2*sqrt(d/a)` when `d <= v*v/a`, otherwise `d/v + v/a`. Animation evaluates these same trajectories.

The reused `amr_simulation.inputs.load_workload` groups rows by `(date, store)` and counts lines per SKU. All groups are released at time zero each day. Groups are processed in store-ID order; SKUs within allocation use SKU order. Each SKU's complete line count goes to one accessible tote/crane, selected by lowest estimated completion time including queued work; ties use crane ID then slot address. The estimate appends complete cycles to each crane's existing queue. Once allocated, a crane finishes that store batch using nearest-next pickup-and-delivery time, with slot-address ties, before its next batch. This is a deterministic greedy heuristic, not a globally optimal routing algorithm.

Different cranes may handle portions of the same store simultaneously at separate workstations. A store/day group completes only when every portion finishes. A single presentation covers all matching lines in its active store batch, without consuming item quantities. There is no cross-store consolidation, inventory depletion, replenishment, conveyor transport, station sharing, or aisle switching. One tote contains one SKU, as in the supplied layouts. The modeling reference is [Daifuku's mini-load ASRS overview](https://www.daifuku.com/solution/intralogistics/products/automated-warehouse/miniload-asrs/); configurable cycle timings are this simulator's assumptions.

With exclusive aisle cranes and workstations, resources cannot contend across aisles. The engine therefore builds deterministic event schedules once and advances between phase boundaries. Headless simulation has no time-step loop. Pygame samples the same schedule; changing frame rate or playback speed changes only how it is displayed.

## Debug controls

- **Space:** pause/resume; **+ / −:** playback speed.
- **N:** advance to the next event while paused.
- **1–9**, **Tab**, or aisle buttons: select the elevation view.
- **Esc** or close: export the current run, marking it incomplete if unfinished.

The overview shows all nine cranes and Y fork motion. The two elevation panels show X/Z and the target slot on each side. Green is an empty crane and gold indicates a carried tote/target. The selected crane's active store, SKU, tote, line count, and signed fork extension appear above the elevations. Left/right are fixed while facing into the aisle from the workstation (−X). Debugging pauses at completion or the daily limit; close the window to write reports.

## Reports and reproducibility

Each batch writes `comparison_report.md`, `layout_comparison.csv`, `paired_daily_differences.csv`, `comparison_dates.json`, PNG/SVG charts, `manifest.json`, `config_snapshot.json`, and `workload_snapshot.json`. Each layout has a preflight `validation_report.json`, `daily_metrics.csv`, and aggregate `summary.json`.

Each `<layout>/<date>/` contains:

| File | Contents |
|---|---|
| `tasks.csv` | Original store/day groups, source/completed/pending lines, participating cranes, completion time |
| `tote_jobs.csv` | Dispatched cycles, tote/SKU/slot/side, covered lines, stage timestamps, actual completion and planned return |
| `cranes.csv` | Travel, fork, picking, dwell, idle time, axis distances/motion times, completed jobs and utilization |
| `workstations.csv` | Picking service time, presentations and utilization per workstation |
| `allocation.csv` | Complete store/SKU line allocation, including work not yet dispatched at a cutoff |
| `summary.json` | Day totals, completion status, throughput, cycle consolidation, store timing, and elapsed wall time |
| `checkpoint.json` | Successful output hashes and batch identity for resume |

Lines complete after tote replacement **and fork retraction**, matching current-heat's return-before-completion convention. Presentations count completed workstation delays, so a presented tote may still be in flight at a cutoff. Future phase timestamps are blank in job outputs; the explicitly named `planned_return_time_s` remains available. Task start time is the first aisle dispatch and completion is the last aisle return.

Distances are summed absolute **actuator travel** in metres: X + Y + Z, including fork travel. Empty/loaded totals split this same distance by carried-load state. X/Z motion seconds overlap because those axes move concurrently; exclusive stage categories sum to busy time. Utilization uses the entire observed day's elapsed simulation time, including a crane's idle tail after its final return.

Comparisons use the intersection of successfully completed dates across **all** selected layouts. The primary metric is arithmetic mean daily completed lines/hour, accompanied by median, standard deviation, duration-weighted throughput, paired daily differences, makespan, tote trip metrics, store completion time, distance, and utilization. Failed/incomplete days remain in diagnostics but are excluded from every layout's comparative aggregates; a partial comparison declares no overall winner. Charts include overview KPIs, daily throughput, fleet time, crane/station balance, and rack retrieval demand.

Resume requires the same inputs, code, configuration, mode, selected dates/layouts, baseline, and time limit. File contents and runtime versions are fingerprinted. Changing `--workers` alone is allowed. Only successful days whose CSV/JSON hashes still match are reused; incomplete, failed, or damaged days rerun. Each day is staged and published with its checkpoint last. An existing output directory is never silently reused as a new batch.

## Validate

```bash
python3 -m unittest discover -s asrs_simulation/tests -v
```

Checks cover analytical motion, fork interlocks and both sides, grouped line accounting, direct next pickup, concurrent aisle portions, deterministic duplicate-SKU assignment, partial runs, sampled/headless agreement, all three real layout geometries, workbook grouping, resume integrity, paired-date exclusion, and Pygame with an offscreen driver.
