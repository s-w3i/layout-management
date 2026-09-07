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

For separate dates, repeat `--date`, for example
`--headless --date 2023-01-03 --date 2023-02-03 --date 2023-03-03`.
Each selected date must have workload data. Dates are sorted and duplicates
run only once. Multiple dates require headless mode; do not combine explicit
dates with `--start-date` or `--end-date`.

The fleet size is fixed across layouts. Default: 40 robots from the AMR config;
use `--amrs 10` to use its first ten spawn positions. Workers default to the
smaller of four, available CPUs, and pending layout/day runs. They are processes,
not threads, and each day has fresh mutable simulation state.

Dates run in chronological order. For each date, all requested layouts run
(up to `--workers` in parallel); the next date starts only after every layout
for the current date has finished its attempt. Completed checkpoints count
as finished when resuming. Each run keeps separate files under
`OUTPUT/LAYOUT/YYYY-MM-DD/`, with its live log beside that date folder.
One combined report and comparison charts are generated after all dates finish.
Each date opens fresh per-layout progress bars using only that day's order-line
total. Date labels identify each set; completed bars remain visible. Resumed
layouts start with their checkpoint's completed-line count for that date.

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

## Live diagnostics

Live diagnostics are written automatically to
`OUTPUT/LAYOUT/YYYY-MM-DD.diagnostics.jsonl` at startup, every 10 wall-clock
seconds after a simulation step/frame, and on completion or interruption.
Each JSON line is flushed immediately and survives temporary result cleanup.
Rerunning an incomplete day replaces that day's diagnostic log.

Snapshots contain per-robot positions, routes, goals, task phases, interval
travel and activity times, reservation owners, wait-for dependencies, last
substep safety blockers, conflicts, tabu edges, planner counts and time, and
unfinished station workloads. `no_*_sampled_sim_s` values measure quiet time
at snapshot resolution, not exact event timestamps. Zero completed lines with
positive travel can indicate ongoing work; zero travel plus sustained safety
or reservation waits identifies robots to inspect. Recorded conflicts and
wait-for entries alone are not proof of deadlock; compare successive samples.
If a single step hangs, snapshots stop too; the last snapshot is the last
completed step. The progress bar's simulation clock also refreshes without
requiring an order line to finish.

```bash
tail -f OUTPUT/LAYOUT/YYYY-MM-DD.diagnostics.jsonl
```

## Recovery from prolonged movement stalls

Beyond the standard DRAM rules, the simulator checks movement every five
simulated seconds. After the whole fleet has made no sampled positional
progress for 30 seconds, it considers a yielding move by a blocked active robot.
Ordinary queues are left alone while the fleet is still moving. Only one robot yields
at a time; its short route (at most six edges) is reserved in full and protected
from conflict-triggered replanning. Normal collision safety remains enabled.

The refuge must be reachable through directed lanes without crossing another
robot's occupied cells or reservations, avoid rack/workstation parking and
other robots' immediate routes, and have a static path back to the original
task goal. The task and rack assignment remain unchanged. On arrival, normal
planning resumes toward the original goal. An escape that cannot finish within
60 simulated seconds is released only when the robot is at a vertex. If no
legal refuge is found, the simulator records that outcome instead of forcing
a move. This is a bounded local recovery heuristic, not a guarantee that every
fleet configuration can be resolved.

Failed replan requests retry after five simulated seconds instead of every
tick. Live diagnostics include the active recovery and the most recent 100
recovery events (`yield_started`, `refuge_reached`, `yield_timeout`, and
`no_reachable_refuge`). These policies intentionally extend the DRAM reference
behavior; use the same code version for all compared layouts.

## Workstation admission and balanced dispatch

`dispatch.workstation_admission_limit` in `current_heat.yaml` defaults to **2**.
It limits rack jobs from dispatch through workstation exit, including pickup,
travel, and service. Returning racks no longer occupy an admission slot.

All store/day groups still release at time zero and keep the shared store-to-
workstation mapping. Each station works on one store at a time. It starts its
next store only when the current store has no undispatched SKUs and all its
admitted jobs have left the station. Rack returns from the previous store may
still be in progress; order lines count as completed after jack-down as before.

Dispatch considers each station's current store, prioritizes fewer admitted
jobs, and breaks ties by least recent dispatch then station name. It skips
stations whose racks cannot currently be dispatched, so their work does not
hold up another station. Within a store, rack coverage and pickup travel still
determine the rack/robot choice. Work cannot be guaranteed at every station
when its demand is exhausted, robots are unavailable, or required racks are busy.

Use the same limit for every layout. Edit the YAML to compare limits of 1, 2,
or 3; restart the simulator and use a new output directory for each comparison.
`rack_jobs.csv` includes `station_release_time_s` to audit admission intervals
and store transitions. This dispatch policy intentionally differs from the
original task sequence; it does not change the path planner.

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

## Standard DRAM coordination alignment

The simulator ports the following behaviors from `dram_ws/src/dram_plan/dram_plan`
and `dram_ws/src/dram_viz/dram_viz/directional_cost_layer.py` (WHCA excluded):

- Timeout replanning waits for both five-second conditions and excludes loaded
  robots and followers with the same two-node continuation.
- Explicit conflict replans require at most one buffered node and add no tabu
  edge. A `wait` resolution falls through to normal reservation checks, matching
  the standard allocator's implementation; reservation/safety checks still apply.
- A replan queues a request from the buffer tail, clears published paths, and
  marks the robot computing. The response is delivered on the next simulation
  tick. It resets passage tracking to the current node and installs the returned
  path directly. Failed replans clear tabu edges; the simulator now delays
  retries by five simulated seconds as described in the recovery section.
  This models callback ordering, not ROS wall-clock/network latency.
- All deterministic priority strategies use DRAM's quantities: actual buffer
  size, reservation timestamp (infinity when unset, as in DRAM), distance to the
  next node, full remaining path length, and geometric path cost. Jack state,
  numeric priority, and robot name determine group/tie ordering.
- Directional costs use DRAM's affected-edge histogram updates, base edge heat,
  clamping, smoothing, and publication interval. Path creation/clearing feeds
  the overlay; robot motion does not silently shorten its published paths.

`planner.directional_smoothing_alpha` defaults to 0.4 and
`planner.directional_publish_period_sec` to 5 simulated seconds. Existing heat
weights remain explicit in YAML. `base_edge_heat_costs` supplies the equivalent
of `/edge_heat_costs`, for example `{"G1_1->G2_1": 3.0}`; keys are undirected.
With no external base heat supplied, the base is zero. No external heat publisher
or ROS transport is simulated.

Tests compare heat calculations and all deterministic priority strategies
against the local `dram_ws` source when that checkout is available. Callback,
reservation, and workload regression tests run without ROS. This does not claim
complete DRAM engine parity: the current-heat A* search, native rack-obstacle
rules, workload allocation, and physical motion remain simulation-specific.
Use a new output directory after these changes; old checkpoints have a different
code fingerprint. Full-day throughput improvement must be measured separately.

### Fleet-stall regression

The standard conflict resolver's tabu-edge publication must accompany its
replan decisions. Omitting it allowed repeated requests for the same blocked
route. Overlap waits also must not replace an already selected replan.
Both are covered by `tests/test_dram_alignment.py`.

A bounded reproduction using `map1_basic`, 2023-01-03, and 40 robots completed
17 lines before all positions stopped changing (unchanged samples from 480
through 660 simulated seconds). With the fixes, the same setup completed 143
lines by 1,200 simulated seconds, with continued movement. This is a stall
regression check, not evidence of full-day completion or an aggregate throughput
improvement. Restart an already-running simulator to load the changes.

### Completed order lines per rack presentation

Daily `completed_order_lines_per_rack_presentation` is completed order lines
(after return/jack-down) divided by `rack_presentations` (rack arrivals at
workstation service). Repeated visits by the same rack count separately.
An unfinished return contributes a presentation but no completed lines; zero
presentations gives an unavailable value. On fully completed days this equals
`lines_per_completed_rack_trip`. Daily summaries/CSVs include the KPI, and layout
summaries, `layout_comparison.csv`, and the Markdown report include its arithmetic
mean over the common completed dates.
