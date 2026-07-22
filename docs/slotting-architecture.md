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
