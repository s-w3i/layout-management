# Inventory slotting architecture

Inventory slotting is organized as a stable service facade, independent
strategies, and shared domain rules. This keeps GUI and CLI callers compatible
while making new strategies easier to add.

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

When enabled, the shared allocator first filters candidates by all existing hard
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
