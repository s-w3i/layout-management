# SKU Velocity Analysis

`sku_velocity_analysis.py` reads transaction rows from an Excel workbook,
classifies SKUs by pick frequency, extracts conservative one-unit physical
profiles, plots daily demand for every SKU, and creates a deterministic chilled
requirements demo.

## Default input and output

| Purpose | Path |
|---|---|
| Transaction workbook | `resources/data/Sample Data.xlsx` (supply locally) |
| ABC summary | `resources/data/sku_velocity_output/sku_velocity_summary.csv` |
| Chilled demo | `resources/data/demo_chilled_requirements.csv` |
| Demand charts | `resources/data/sku_velocity_output/demand_plots/` |

Raw workbooks and generated demand plots are ignored by Git because they may be
large or warehouse-specific. Supply the workbook locally before running the
analysis. It must contain these columns:

- `Date`
- `Item or SKU`
- `Quantity (in EA)`

Physical extraction also uses `Length`, `Width`, `Height`, and `Weight` when
present. Zero, negative, and blank values are ignored. The maximum positive
value observed for each SKU is retained without unit conversion.

## Run the analysis

From the repository root:

```bash
python3 sku_velocity_analysis.py
```

Use `--skip-plots` when only the CSV inputs need refreshing:

```bash
python3 sku_velocity_analysis.py --skip-plots
```

Install the required packages if necessary:

```bash
python3 -m pip install openpyxl matplotlib
```

## Classification

SKUs are sorted by descending pick frequency and classified using cumulative
pick-frequency share:

- A: first 80% by default
- B: next 15%, through 95%
- C: remaining 5%

Custom boundaries and paths can be supplied from the command line:

```bash
python3 sku_velocity_analysis.py \
  --input resources/data/Sample\ Data.xlsx \
  --output resources/data/sku_velocity_output \
  --a-limit 0.80 \
  --b-limit 0.95 \
  --chilled-output resources/data/demo_chilled_requirements.csv \
  --chilled-rate 0.10 \
  --chilled-seed 42
```

The summary includes `req_max_item_length`, `req_max_item_width`,
`req_max_item_height`, `req_max_item_weight`, `physical_data_status`, and
`physical_storage_class`. Against the demo standard limits 25.0 × 19.3 × 19.2 and
weight 465.0, complete SKUs are classified as standard, oversize, overweight, or
both. Missing weight is conservatively classified as overweight, missing size
as oversize, and missing both usable size and weight as oversize plus
overweight; the data status remains `MISSING`. During inventory slotting,
weight `0` also disables the ergonomic weight heuristic for that SKU.

The chilled demo selects 10% of sorted unique SKU IDs uniformly with seed 42.
For the included 1,524-SKU summary this produces exactly 152 selected-only rows;
SKUs absent from that file are ambient.

The generated summary is the default SKU input for the Inventory Slotting tab
in `rmf_grid_map_editor.py`.
