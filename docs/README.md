# Documentation

## Core Python workflow

- [Progress meeting presentation](progress-meeting-presentation.pdf) —
  approach-focused LaTeX slides covering ABC slotting, affinity identification
  and placement, the reproduced two-stage C&TBSA traffic-aware workflow,
  global traffic optimization, and a common-input comparison of the saved
  basic, affinity, and traffic-aware layouts.
- [SKU velocity analysis](sku-velocity-analysis.md) — classify SKUs as A, B or
  C using pick frequency and generate a demand graph for every SKU.
- [SKU affinity analysis](sku-affinity-analysis.md) — explore direct SKU–store
  order frequency and use data-calibrated store-day relationships in slotting.
- [Traffic-aware slotting](traffic-aware-slotting.md) — reproduce C&TBSA
  correlation/workload-balanced clustering and validate the resulting expected
  movement-resource load without claiming fleet simulation.
- [Global traffic optimizer](global-traffic-optimizer.md) — run the independent
  exact/time-bounded complete-unit assignment and audit solver proof status.
- [Inventory slotting architecture](slotting-architecture.md) — understand the
  strategy modules, shared hard rules, and extension points.
- [RMF grid map editor](rmf-grid-map-editor.md) — create and edit the warehouse
  grid, place racks and workstations, assign zones, generate slotting layouts,
  inspect inventory, and demonstrate SKU or shelf swaps.

The root [user manual](../README.md) also documents the object-oriented module
structure and service regression tests.

## Reference assets

- `resources/map/` contains RMF building maps and source layout images.
- `resources/data/` contains the source workbook, ABC output, demand charts and
  generated slotting layout.
- `resources/others/` contains inventory-addressing design references.

Return to the [project overview](../README.md).
