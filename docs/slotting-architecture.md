# Inventory slotting architecture

Inventory slotting is organized as a stable service facade, independent
strategies, and shared domain rules. This keeps GUI and CLI callers compatible
while making new strategies easier to add.

Repeated quantity loads are consolidated by the shared allocator. The optional
`maximum_same_sku_slots_per_rack` parameter counts occupied physical cells,
defaults to configured rack capacity, and applies identically to Basic,
Affinity, and C&TBSA regeneration. Basic and C&TBSA prefer contiguous same-SKU
footprints on the ergonomically ranked level before another compatible level
or rack. AMR affinity treats consolidation as a tie preference; another eligible
rack can win when it serves additional co-ordered groups. Its ordering places
one load of each logical SKU before additional inventory copies.
AMR affinity keeps Basic's complete feasible placement as future reservations.
An affinity choice can exchange an unplaced standard load into an equivalent
cell only when every reserved load still fits its same-SKU and whole-rack weight
limits. Exceptional footprints remain anchored to the feasible seed. This
preserves assignment coverage when several SKUs need the same restricted racks.

## Module map

- `warehouse_layout/slotting.py` is the public `SlottingService` facade. It
  loads SKU inputs, calculates rack routes, and dispatches generation.
- `warehouse_layout/slotting_strategies/basic.py` is the pure ABC strategy.
- `warehouse_layout/slotting_strategies/abc_affinity.py` owns affinity tuning
  and comparison against the ABC baseline.
- `warehouse_layout/slotting_strategies/allocation.py` is the shared physical
  allocation engine used by both strategies.
- `warehouse_layout/slotting_strategies/affinity_support.py` contains affinity
  ordering, metrics, and tuning helpers.
- `warehouse_layout/slotting_rules.py` contains shared hard rules and ranking,
  including physical fit, overweight level placement, rack reuse, and rack ABC
  ranking.
- `warehouse_layout/storage_planning.py` plans standard/oversize segments and
  constructs static and dynamic addresses.
- `warehouse_layout/slotting_repository.py` reads and writes slotting JSON and
  CSV output.
- `warehouse_layout/traffic.py` remains the separate traffic-aware pipeline.

## Adding a strategy

1. Add a class under `warehouse_layout/slotting_strategies/` with a unique
   `name` and a `generate(service, ...)` method.
2. Register the class in `STRATEGY_TYPES` in
   `warehouse_layout/slotting_strategies/__init__.py`.
3. Reuse `allocation.allocate` when the strategy should retain the current
   physical compatibility and storage-planning rules. Add strategy-specific
   ordering or tuning in its own module.
4. Add focused tests for the new ordering plus an integration test through
   `SlottingService`.

The existing `SlottingService.generate_basic` and
`SlottingService.generate_abc_affinity` methods are compatibility APIs and
should remain stable for the GUI, CLI, tests, and traffic-aware comparison.

## Optional zone-workload balance

In **Inventory Slotting**, **Balance workload across zones** applies to both
`basic` and `abc_affinity` (including pure affinity). It defaults to off and is
independent of the Traffic-Aware Slotting options.

For Basic and slot-level handling units, the allocator first filters candidates by all existing hard
constraints. It then prefers the zone with the lowest projected demand divided
by that zone's configured usable slot count. Basic or Affinity selects the rack
and slot within the preferred zone; equal zone scores retain the original
strategy's choice. Reserved exception footprints and compatibility remain
authoritative. This is a greedy placement preference, not a guarantee of equal
final workloads. It can trade affinity grouping and travel distance for balance.

Demand uses the velocity CSV's `pick_frequency`. Multiple inventory loads share
their SKU's demand in proportion to stored quantity, so replicas do not multiply
the demand. This is an inventory-demand proxy, not simulated congestion or
deduplicated rack visits. Zone capacity is fixed for the run, not remaining
capacity as slots fill.

Both `SlottingService.generate_basic()` and `generate_abc_affinity()` accept
`zone_workload_enabled=True`. Pass `False` or omit it for the original placement.
Affinity's internal Basic comparison uses the same setting. Saved layout
summaries include `zone_workload_enabled` and `zone_workload` (capacity, demand,
and normalized demand by zone); loading the layout restores the checkbox, with
older files defaulting to off. Save on/off outputs separately with otherwise
identical inputs and affinity settings to compare them.

For **AMR affinity with a positive affinity weight**, packing first builds rack
contents without the per-slot zone preference. The same-SKU limit, physical
compatibility, footprint reservations and whole-rack weight remain mandatory.
After packing, zone balancing assigns complete rack groups to physical rack
locations. Groups move only between racks with identical cell coordinates,
effective attributes and storage types. This preserves all existing physical
checks, including contiguous footprints and required levels. Individual items
are never split out of a group by this balancing pass. A 0% affinity run retains
Basic's behavior.

Rack workload uses joint greedy store/day allocation: maximum distinct required
SKUs, then mean workstation distance and rack ID, with all racks available and
no inventory depletion. The rack cycle estimate is round-trip mean workstation
distance at 1.5 m/s plus 18 seconds of handling per presentation (the standard
AMR configuration's 4-second lift, 4-second lower and 10-second picking time).
It excludes turning, queues, reservations, empty robot positioning and real-time
rack availability. These effects are measured in the full simulation benchmark.
The allocator balances estimated work per configured rack in each zone. It
accepts a complete-group reassignment only if it preserves demand coverage and
does not increase estimated presentations, then improves maximum normalized
zone workload or, on a tie, total estimated work.
Moving intact contents can still change greedy source selection, because
distance and rack ID break coverage ties. If the full-map proposal fails the
acceptance rule, the balancer verifies up to eight promising rack-pair swaps
from the busiest zone. It favors similar travel distances and accepts the first
swap that satisfies the same coverage, presentation-count and workload checks.

Saved summaries expose `zone_balance_model`, `rack_workload` and
`rack_zone_balance` with before/after estimates and the accepted rack mapping.
The existing `zone_workload` fields remain the quantity-weighted pick-frequency
diagnostic, not a count of rack presentations. Placement-row affinity scores are
recorded before whole-rack relocation; final layout metrics are recalculated.

Affinity pair metrics use every replica location (minimum pair distance and
whether any rack is shared), while service distance weights replica locations
by inventory quantity. Rack-visit metrics and AMR movement ranks use joint rack
selection rather than independent quantity-proportional SKU allocation.

Run `python3 scripts/benchmark_affinity_rack_balance.py` to regenerate limits
1–6 from the saved stock, compare the same ten dates against the original
outputs and validate the selected limit on three additional dates. Outputs and
content-checked checkpoints go to
`current_heat_simulation/results/affinity_rack_balance_revision/`. The additional
dates are outside the ten-date limit selection, but are included in the original
historical affinity training data; they are not an unseen-data validation.

ASRS placement keeps its existing zone policy. A future ASRS change should
balance predicted tote-cycle seconds per crane using crane travel, tote source
choice and queue load; AMR whole-rack presentation counts are inappropriate for
single-SKU tote retrieval. Its evaluation must preserve completed lines/tote and
measure crane utilization, throughput and daily variation separately.

## Revised implementation and validation plan - completed 13 October 2026

Build useful rack contents first, then balance complete racks across zones.
The same-SKU setting is a maximum, not a target: reducing it does not guarantee
more completed lines per rack presentation.

All six revised layouts, 60 main simulations and six additional-date simulations
are complete. The highest revised ten-date mean is **891 lines/hour at limit 6**,
up **12.0%** against the original at limit 6 and **7.1%** against the best original
mean (832 lines/hour at limit 2). These are observed simulation means, not a
claim that limit 6 is universally optimal. Daily throughput variation at limit 6
increased: coefficient of variation **9.3% to 10.6%**. This revision improves
mean output but does not establish smoother daily throughput. The three
additional dates improve by **16.0%-24.8%** at the selected limit.

1. **Preserve stock feasibility before optimizing affinity - complete.**
   Retain Basic's placement as reservations for future loads. An affinity
   exchange must preserve valid locations for both current and displaced loads,
   required quantity records, assignment coverage, compatibility, footprints,
   same-SKU physical-cell limits and whole-rack weight. All six stock audits
   pass their coverage gates. Limit 1 assigns 3,248 of 3,249 loads, one more than
   its original: one 49-unit load of SKU 111964 remains unassigned. This is a
   remaining placement shortfall, not proof of infeasibility. Limits 2-6 assign
   all 3,249 loads.
2. **Use every replica and joint rack selection - complete.**
   Affinity metrics retain every SKU location. Estimate rack presentations
   jointly for each store/day using coverage, distance and rack-ID priorities.
   Preserve the existing dispatch policy in the controlled comparison; the
   shared rack-ranking helper is behaviorally equivalent. Static estimates
   assume all racks are available, while the full simulation measures live
   availability and traffic.
3. **Group SKUs, then balance whole racks - complete.**
   Place first copies before extra inventory copies, subject to future stock
   reservations. Move intact groups only between racks with equivalent physical
   profiles. Accept a relocation only if demand coverage and estimated
   presentation count are preserved and normalized zone work improves. Full
   remaps that increased estimated visits were rejected; verified pair swaps
   move two groups in each final layout. Predicted peak zone work falls
   9.0%-11.7% (9.5% at limit 6). This planning estimate excludes queues and turns;
   it is not the measured throughput gain. The comparison evaluates the
   combined affinity revision, so it does not isolate the pair swap's effect.
4. **Pass regressions and full-inventory checks - complete.**
   All 84 tests and 19 subtests pass with the production code frozen throughout
   generation and simulation. Independent audits verify intact groups, replicas,
   occupied footprints, known physical fit, configured compatibility and weight.
   Eight existing loads lack dimensions and weight; their physical fit remains
   explicitly unverified in exception storage. The map configures chilled
   storage but does not configure tablet or flammable separation; this revision
   retains that existing constraint scope.
5. **Run the controlled simulation comparison - complete.**
   Compare limits 1-6 on the same ten dates, inventory targets, map, 40 AMRs,
   workstation mapping and simulation settings, then compare the selected limit
   with its original on three additional dates. All 66 new runs finish every
   source line; all 60 saved original runs are hash-verified comparison inputs.
   The extra dates are outside limit selection but inside historical affinity
   training, so they do not test unseen customer data. Checkpoints and input/code
   hashes remain available. The workbook binary differs from the original
   manifest. An uncached read verifies the same ordered 724 original-date tasks
   and 56,188 source lines with identical SKU counts and release times. Full
   historical training events match the legacy cache, but that cache's original
   binary provenance is independently unverified because the original workbook
   is unavailable. Preserve this qualification.
6. **Return a short report with actual examples - complete.**
   The verified two-page report compares all six limits and includes daily
   throughput, lines per presentation, fleet presentations/hour, travel,
   handling, path waiting, AMR utilization and daily variation. Combined daily
   throughput/presentation charts and complete correlation tables are saved
   with the comparison results. Exact relationships are:
   `lines/hour = lines/presentation x presentations/hour` and
   `lines/hour = repeat-line factor x distinct SKUs/presentation x presentations/hour`.
   Slotting and source selection affect grouping; travel, queues, rack
   availability and task allocation affect the fleet visit rate. Throughput
   covers release through the final rack return, including startup and finishing
   tasks. Correlations are descriptive and share quantities in their formulas;
   they do not identify causal percentages.

Actual examples make the tradeoffs clear. On 14 February, DESA's three-SKU basket
needs three visits originally and one after revision at both limits 1 and 6.
On 3 January, PKMT's two-SKU basket at limit 1 changes from one visit to two.
On 30 May at limit 2, lines/visit improves from 1.66 to 2.08 and travel falls
from 93 to 69 metres/line, but path waiting rises from 49 to 89 seconds/line;
throughput falls from 796 to 747 lines/hour. For revised limit 6's lowest and
highest days, repeats change only +1.2%, distinct SKUs/visit +40.3% and fleet
visits/hour -4.6%, together producing +35.5% throughput. Grouping matters,
but better grouping alone does not guarantee higher or steadier throughput.

For customer design, preserve feasible stock, form useful co-ordered groups,
then evaluate zone workload and traffic with the actual dispatcher across
representative demanding days. Picking is currently fixed at 10 seconds per
visit regardless of lines served; capacity commitments need measured picking
times. This revision targets AMR affinity. Basic and C&TBSA retain their
placement policies with shared correctness fixes where applicable. ASRS crane
balancing remains a separate design using tote-cycle work and crane utilization.

Final report: `output/pdf/Affinity_rack_balance_revision_results.pdf`.
Evidence and charts: `current_heat_simulation/results/affinity_rack_balance_revision/`
(`result_analysis.json`, `completion_audit.json`, `comparison/charts/`, and
`additional_dates/`). Reproduce with `python3 scripts/benchmark_affinity_rack_balance.py`;
rebuild only the report with `python3 scripts/report_affinity_rack_balance.py`.

## Required stock quantities in the desktop app

Calculate **Stock Requirements** before generating Basic, Affinity, or
Traffic-Aware layouts. All three GUI paths attach the calculated
`total_required_ea` and `required_slots` by SKU. Missing, partial, or unresolved
targets stop generation with an alert instead of silently assigning one load
per SKU. A velocity CSV already containing complete quantity targets is also
accepted. Zero-demand SKUs with zero required slots are omitted correctly.

Traffic generation snapshots these quantities before its background worker
starts. Its minimum-rack option therefore operates on expanded inventory loads,
not just the number of distinct SKUs. Stock results held in memory must be
recalculated after restarting the app unless quantity targets are supplied in
the velocity CSV. Programmatic callers can request the same guard with
`apply_stock_requirements(..., require_complete=True)`.
