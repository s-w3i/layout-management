# Migration validation

Validated locally on 2026-10-31 against the current source and real input files in `/home/usern/layout_management_master`. Automated source hashes and comparison details are in `migration-validation.json`, `current-affinity-comparison.json` and `historical-affinity-comparison.json`.

## Basic and stock parity

All cases below feed the same grid, velocity, explicit SKU requirements, imported stock targets and rule settings into a fresh legacy run and the C++ implementation. Each case requests **3,335 loads for 1,524 SKUs**. Placement identity means the same load occupies the same physical storage cells; it does not mean every reporting field is byte-identical.

| Case | Identical load placements | C++ engine | Existing Python |
| --- | ---: | ---: | ---: |
| AMR Basic, zone balancing off, same-SKU cap 12 | 3,335 / 3,335 | 17.61 s | 156.43 s |
| AMR Basic, zone balancing on, same-SKU cap 12 | 3,335 / 3,335 | 16.64 s | 169.53 s |
| ASRS Basic, zone balancing off, same-SKU cap 12 | 3,335 / 3,335 | 18.98 s | 187.54 s |

Stock recalculation also matches the current Python stock functions for **all 1,524 SKUs**, including required/slotted EA, purchasing-unit conversion, units per cell, footprints and required cells/racks. Zero mismatched SKUs.

**Input revisions matter:** the supplied stock CSV requests 3,335 loads; recalculating with current dimensions produces **3,268 loads and the same 267,184 EA**. Only SKU `139112` changes its load count: the current 1.9 × 0.5 × 1 m cell fits two of its 0.8 × 0.5 × 0.95 m items, while the saved CSV records one item per cell (135 loads instead of 68). The new and current Python calculations agree. The older saved affinity layout contains 3,249 loads and must not be compared directly against the newer 3,335-load input as if parameters were identical.

## Fresh affinity comparison

The same current 3,335-load input, AMR grid, cap 12, zone balancing off, fixed store/workstation mapping and order date **2023-01-03** are used for both generators. That date contains 97 store/day groups.

| Metric | Existing Python | C++ |
| --- | ---: | ---: |
| Assigned loads | 3,335 | 3,335 |
| Uncovered store/day groups | 0 | 0 |
| Rack presentations | 1,135 | 1,132 |
| Round-trip travel | 89,100 m | 87,668 m |
| Peak workstation work | 16,288.67 s | 15,796 s |
| Peak zone work / configured rack | 2,647.08 s | 1,987.33 s |
| Generation time | 405.91 s | 45.27 s |

**2,559 / 3,335 final placements are identical.** The native packing stage achieves the legacy final replay metrics before its refinement; native rack/bundle refinement finds a further improvement. Both results pass independent structural validation. Importantly, the original Python `OrderReplay` independently scores the native result and agrees with the native replay on presentations, travel, peak workstation work, peak zone work and uncovered groups (floating tolerance 1e-6).

Both layouts have **27 assigned loads with incomplete physical data**. Those loads are reported as unverified; the validation does not claim complete physical-fit verification.

## Full historical-layout comparison

Stock quantities and SKU requirements are reconstructed from the existing saved `affinity_zone_on_max_same_sku_2.slotting.json` so both results represent exactly **3,249 requested loads**. The same current grid, supplied 275-store workstation mapping, all **12,398 store/day groups**, cap 2 and zone balancing enabled are used for scoring. The reference is a historical saved output, not a fresh run of the current Python source.

| Metric | Saved layout | C++ |
| --- | ---: | ---: |
| Assigned loads | 3,249 | 3,249 |
| Uncovered groups | 0 | 0 |
| Rack presentations | 561,451 | 427,852 |
| Round-trip travel | 55,554,416 m | 35,261,912 m |
| Peak workstation work | 8,908,997.33 s | 6,158,326 s |
| Peak zone work / configured rack | 202,077.28 s | 400,353.33 s |

All requested quantities are preserved and both layouts pass structural checks; eight loads have incomplete physical data. Native generation took 130.68 seconds in this run. **The native result trades a higher peak zone workload for fewer presentations and lower travel.** Zone guards compare refinement with its own initial packing, so they do not promise improvement over a historical reference. This result is not a blanket affinity quality win and is not exact placement parity.

## Process and interface checks

- Original XLSX import: **982,992 accepted lines, zero ignored rows, 1,524 SKUs, 275 stores**, 2023-01-03 through 2023-07-31, 12,398 store/day groups.
- **26 automated tests pass.** Tests cover deterministic packing, stock conservation, hard constraints, shared footprints, incomplete data, malformed/malicious assignment records, routing, machine/buffer limits, affinity, signed demand and fractional purchasing units.
- HTTP integration covers CSV/XLSX equivalence, analysis → stock → generation, reference comparison, portable order/reference restore, cancellation, monotonic autosave, explicit rejection of stale/equal tab revisions, and loopback/origin boundaries.
- Browser workflow: real order analysis, stock recalculation, generation of all 3,268 recalculated loads, JSON export and reference re-import. The exported layout is accepted by the original `SlottingLayoutRepository`; its embedded building is accepted by the original RMF loader.
- Responsive review and screenshots are recorded alongside this report. Tables intentionally scroll inside their own containers; the page should not overflow horizontally.

Timing figures are observed local runs, not general performance guarantees. These checks establish tested cases and invariants, not correctness for every possible warehouse or a global optimum. Congestion, dynamic fleet execution, replenishment and inventory depletion remain outside the static model.

## 3D view addition

The subsequent 3D view is covered by [3D-VALIDATION.md](3D-VALIDATION.md). The expanded automated suite has 31 passing tests. Native layout-generation algorithms remain unchanged.
