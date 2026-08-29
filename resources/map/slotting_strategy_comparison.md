# Slotting strategy comparison (ABC baseline)

All three layouts use `map1`, the same quantity/attribute inputs, and the same order history. Affinity uses the UI default 50% weight with automatic map/workbook tuning. Traffic is recomputed consistently for every strategy; lower travel, peak/P95 load, and rack-load CV are better.

| Metric | ABC baseline | Affinity | C&TBSA |
|---|---:|---:|---:|
| Occupied racks | 272 | 272 | 329 |
| Loads moved vs ABC | 0 | 3,223 | 3,225 |
| Racks affected by moved loads | 0 | 275 | 327 |
| Racks with different SKU set | 0 | 256 | 327 |
| Expected travel | 21,153,534.00 | 21,137,589.67 | 32,983,664.67 |
| Peak normalized resource load | 13.0543 | 11.7967 | 18.2716 |
| P95 normalized resource load | 2.0480 | 2.1454 | 3.3132 |
| Average rack weight (kg) | 88.224 | 88.224 | 72.939 |
| Maximum rack weight (kg) | 148.729 | 148.729 | 128.043 |
| Rack visit-load CV | 0.9814 | 0.9822 | 0.4033 |
| Total rack touches | 550,631 | 542,752 | 743,648 |
| Average racks per fulfillment group | 44.4129 | 43.7774 | 59.9813 |

## Interpretation

- A ‘rack affected by moved loads’ is a source or destination rack for a load whose rack, level, slot, or oversize footprint changed from ABC; rack map coordinates are unchanged.
- ‘Different SKU set’ compares distinct logical SKUs per rack. The rack-detail CSV also reports load-set changes, weight, visits, and occupied slots.
- C&TBSA preserves its original shelf-load-balancing objective while treating separate quantity loads of one SKU as independent assignable items.

## Generated files

- Affinity layout: `resources/map/affinity_CO_quantity_slotting_layout.slotting.json`
- Machine-readable summary: `resources/map/slotting_strategy_comparison.json`
- Summary CSV: `resources/map/slotting_strategy_comparison.csv`
- Rack-detail CSV: `resources/map/slotting_strategy_rack_comparison.csv`
