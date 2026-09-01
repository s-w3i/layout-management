# Standalone LaCAM DTE

This package is independent from `amr_simulation` and `warehouse_layout`. It
reads their external grid, layout, and workbook formats without importing
either package.

Build and run one day headlessly:

```bash
python -m lacam_dte batch \
  --grid resources/map/map1_1.grid.json \
  --orders 'resources/data/Sample Data.xlsx' \
  --config lacam_dte/config/default.json \
  --layout resources/map/map1_basic.slotting.json \
  --start-date 2023-01-03 --end-date 2023-01-03
```

Use `debug` with one layout and `--date` to simulate and open authoritative
Matplotlib playback. The first invocation builds the pinned C++17 planner.
