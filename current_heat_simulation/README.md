# Current-heat slotting comparison

Compare SKU-to-rack slotting layouts on the same native grid and daily order
workload. The primary measure is **mean daily completed order lines/hour**.
The planner is the copied `current_heat` implementation: weighted A*, directional
heat, committed-path penalties, periodic reservations, and Pygame motion.
No EECBS, WHCA, LNS2, ROS, or external `mapf_sim` installation is required.

## Run a comparison

From the repository root:

```bash
python3 -m current_heat_simulation.pygame_simulator --headless \
  --layout resources/map/map1_basic.slotting.json \
  --layout resources/map/map1_pure_affinity.slotting.json \
  --layout resources/map/map1_traffic_zone_balance_off.slotting.json \
  --layout resources/map/map1_traffic_zone_balance_on.slotting.json \
  --start-date 2023-01-03 --end-date 2023-01-04 \
  --workers 4 --output current_heat_simulation/results/layout_comparison
```

Omit the date arguments to run **all observed workbook dates headlessly**.
Use `--date 2023-01-03` for a single day. Start with a representative small
selection before scheduling all dates: congested runs can remain incomplete.

The fleet size is fixed across layouts. Default: 40 robots from the AMR config;
use `--amrs 10` to use its first ten spawn positions. Workers default to the
smaller of four, available CPUs, and pending layout/day runs. They are processes,
not threads, and each day has fresh mutable simulation state.

To resume, repeat the same input/configuration arguments and append `--resume`
with the same `--output`. Successful, intact, compatible days are skipped;
failed, incomplete, missing, or damaged days are rerun. The worker count may
change on resume. Content hashes cover input files, selected dates, effective
settings, runtime versions, and simulation/shared Python code. If those change,
choose a new output directory. An existing batch is never overwritten silently.

Default baseline: `map1_basic` when supplied, otherwise the first layout.
Override with `--baseline-layout map1_pure_affinity` or a supplied file path.
Layout filenames must produce distinct names. Strategy labels are retained from
input metadata; they do not establish causality.

The original single-layout rendered command still works:

```bash
python3 -m current_heat_simulation.pygame_simulator \
  --config current_heat_simulation/current_heat.yaml --date 2023-01-03
```

Without a date, rendered mode uses the earliest workbook day. Rendered mode
accepts one layout. Both modes stop at completion or `--max-seconds` (per-day
simulation seconds, default 86,400). An early window close is incomplete.
Exit status: 0 if every requested run completes, 1 for incomplete/failed runs,
2 for input or batch/report errors. Daily results survive interruption.

## Shared inputs and fairness

YAML paths resolve relative to the YAML file; CLI paths resolve relative to the
working directory. Defaults:

| Input | Default |
|---|---|
| Native grid | `resources/map/map1_1.grid.json` |
| Order workbook | `resources/data/Sample Data.xlsx` (local, not tracked) |
| Slotting layout | `resources/map/map1_basic.slotting.json` |
| Fleet, stations, handling/motion settings | `amr_simulation/config/default.json` |

Override with `--grid`, `--orders`, `--layout`, and `--amr-config`.
Dependencies: Python 3.10+, PyYAML, openpyxl, NumPy, Pygame, Matplotlib, and
`tqdm` for the per-layout order-line progress bars.
These are the existing repository libraries plus Pygame.

- `RmfMapService` and `GridRouter` preserve deleted/added nodes and lanes,
  one-way directions, and coordinate overrides. IDs use `Gcolumn_row`.
- All non-endpoint rack markers are obstacles, loaded or empty, matching the
  AMR router. Racks return to their original slots.
- `amr_simulation.inputs.load_workload` groups source rows by date and store,
  retaining per-SKU **order-line counts**, not quantities.
- Every group is released at time zero; order-line timestamps and historical
  intra-day release rates are ignored.
- Workstation balancing is calculated once across the selected workload and
  shared across layouts, including store overrides.
- The scheduler follows AMR task-ID order, rack SKU coverage, rack-to-station
  distance, and predicted pickup travel time for free-robot assignment.
- One store group may need several rack trips; one trip may cover several SKUs
  and multiple source lines. A group finishes after all trips return/jack down.
- Every layout is validated before any run starts. Invalid assignments or
  unreachable required locations reject the batch.
- The timestep remains **0.05 seconds**; conflict-check and allocator frequencies
  are unchanged. Deterministic dependency traversal avoids process/hash-order
  tie differences. Random allocation priority is not supported for comparisons.

Headless execution avoids screen/projection setup and display-only path/overlay
updates. It retains motion commands and safety checks. No fallback planner or
serialized recovery is introduced. Increasing playback speed does not accelerate
headless simulation.

## Metrics

For a successful day, throughput is source order lines completed divided by
hours from time zero through the last rack's jack-down. Across layouts, comparisons
use **the same dates successfully completed by every layout**:

- Primary: arithmetic mean daily throughput.
- Supporting: median, sample standard deviation, and total lines/total hours.
- Baseline comparison: aggregate percentage improvement and paired daily
  absolute/percentage differences.
- Coverage: completed, incomplete, and failed day counts; included/excluded dates.

An incomplete batch is provisional. No overall winner is declared, and no
comparison is available if there are no common completed dates. Partial-run
throughput and backlog remain visible in daily diagnostics but do not enter
full-workload averages. Sample SD is not a confidence interval.

Diagnostic KPIs include travel metres/line, loaded/empty distance, lines and
SKUs per completed rack trip, rack trips/1,000 lines, mean/p95 store completion
time, robot utilization, reservation/path and safety waiting seconds/line,
station service utilization and completed lines, replans/1,000 lines, blocking
hotspots, and completion/backlog counts. Aggregate KPI columns prefixed with
`mean_daily_` are arithmetic means across common completed dates.

Motion is measured as actual displacement within each physical substep, not
planned-path length. Robot-seconds are mutually exclusive: handling/service,
translation, rotation, safety blocking, reservation/path waiting, other active
waiting, or idle. Busy utilization includes active waiting. Substep classification
uses handling first, then translation, rotation, safety blocking, missing granted
path, other active waiting; idle means no assigned task. Simultaneous tiny heading
adjustments during translation remain translation time.

Stage timestamps in `rack_jobs.csv` use the simulator's phase names:
`pickup_wait` = pickup arrival; `to_dropoff_entry` = jack-up complete;
`dropoff_wait` = station service start; `to_ingestor_exit` = service complete;
`return_wait` = home arrival; `idle` = jack-down/job complete. Values are the first
entry to each phase. Durations have the fixed-step model's time resolution.

Blocking hotspots use the intended next node when known, otherwise the current
node. `safety_blocked_robot_substeps` counts movement denials, **not collisions**.
General blocking is not labeled workstation queue time: there is no explicit
station queue in this model. Workstation busy time comes from actual service
intervals. Wall-clock execution time, planning latency, and simulation/wall-time
ratio are reported separately from warehouse throughput.

## Output

```
output/
  manifest.json, config_snapshot.json, workload_snapshot.json
  store_workstation_mapping.json, batch_timing.json
  layout_comparison.csv, paired_daily_differences.csv, comparison_dates.json
  comparison_report.md
  charts/                         # five PNG comparison/diagnostic charts
  <layout>/
    validation_report.json, daily_metrics.csv, summary.json
    <YYYY-MM-DD>/
      summary.json, tasks.csv, rack_jobs.csv, robots.csv
      workstations.csv, blocking_hotspots.csv
      checkpoint.json             # only for complete, fully published runs
```

Each day is staged privately, then published as a directory with content-checked
completion metadata. Normal batches retain counters/job summaries instead of
per-step traces. Worker-local day state and route caches are discarded after use.
A rendered single-layout run uses this same layout/date output structure.

Charts cover mean throughput/variation, daily throughput, travel and rack-trip
consolidation, robot activity and workstation balance, and spatial blocking
hotspots. Except the diagnostic daily throughput plot, comparisons use common
completed dates. Raw dates/results remain available in CSV for independent analysis.

## Verification and limitations

```bash
python3 -m unittest discover -s current_heat_simulation/tests -v
```

Tests cover input grouping, directed topology, selection and rack return,
sequential/parallel determinism, replica layouts, known-distance/time accounting,
paired-date aggregation, checkpoints, failed runs, and report generation.

This is not the original C++ DRAM solver or the AMR discrete-event engine.
Pygame rotation uses a constant angular-speed limit while the shared AMR dispatch
estimate includes angular acceleration. Congestion may prevent completion;
reported safety interventions are not a proof of physical collision safety.
Do not infer fleet-scale or full-year performance from a small successful sample.
