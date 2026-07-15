# SKU Velocity Analysis

`sku_velocity_analysis.py` reads transaction rows from an Excel workbook,
classifies SKUs by pick frequency, and plots daily demand for every SKU.

## Default input and output

| Purpose | Path |
|---|---|
| Transaction workbook | `resources/data/Sample Data.xlsx` (supply locally) |
| ABC summary | `resources/data/sku_velocity_output/sku_velocity_summary.csv` |
| Demand charts | `resources/data/sku_velocity_output/demand_plots/` |

Raw workbooks and generated demand plots are ignored by Git because they may be
large or warehouse-specific. Supply the workbook locally before running the
analysis. It must contain these columns:

- `Date`
- `Item or SKU`
- `Quantity (in EA)`

## Run the analysis

From the repository root:

```bash
python3 sku_velocity_analysis.py
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
  --b-limit 0.95
```

The generated summary is the default SKU input for the Inventory Slotting tab
in `rmf_grid_map_editor.py`.
