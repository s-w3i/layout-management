# SKU Affinity Analysis

The **SKU Affinity** tab in `rmf_grid_map_editor.py` explores line-order history
without changing the slotting layout. Its default input is
`resources/data/Sample Data.xlsx`. The Inventory Slotting tab can consume the
same raw workbook when `abc_affinity` is selected.

## Input interpretation

The first worksheet containing these columns is selected automatically:

- `Date`
- `Store ID`
- `Item or SKU`

Each valid row is one order event. Quantity is not used, rows are not grouped
into baskets, and duplicate rows count separately. Rows with a blank SKU, blank
store, or invalid date are skipped and included in the KPI summary.

## Affinity definition

Direct frequency records how many line-order rows connect a store and SKU:

```text
F(i,s) = count of rows for SKU i and Store ID s
```

For derived affinity, one fulfillment group is created for every `(Store ID,
Date)`. SKU presence is binary within a group, so duplicate SKU lines on one
store-day count once. Let `X(g,i)` be 1 when group `g` contains SKU `i`.

The number of shared store-days is:

```text
N(i,j) = sum_g X(g,i) * X(g,j)
```

Two SKUs are related by cosine similarity across store-day groups:

```text
A(i,j) = N(i,j) / sqrt(N(i) * N(j))
```

Here `N(i)` and `N(j)` are the numbers of store-day groups containing each SKU.
The application presents `A(i,j)` as 0–100% and also reports shared store-days,
line-order totals, and strongest common stores. The default minimum is three
shared store-days.

## Views and filters

- **Store–SKU Heatmap** shows direct line-order counts for the most active SKUs
  and stores. Colour uses `log(1 + orders)` so lower frequencies remain visible.
- **SKU Relationship Map** shows one selected SKU and its 12 strongest
  qualifying relationships. Click a node to recenter.
- **Related SKUs** is a sortable numerical view of the graph relationships.
- **Store Frequency** shows the selected SKU's direct store distribution.

Date filters are inclusive. The full source range is selected after loading.
The top-SKU, top-store, and minimum-shared-store-day controls affect only the active
view and export, not the source workbook.

## Cache and export

The first workbook scan stores compact event arrays under
`.cache/rmf_grid_map_editor/affinity/`. A cache is reused only when the absolute
source path, file size, modification time, and cache schema still match. A
corrupt or stale cache is discarded and rebuilt. Cancelling a scan never saves
a partial cache.

**Export JSON + CSV…** creates:

- `<name>.affinity.json` with schema `sku_affinity_analysis/v2`;
- `<name>_sku_store.csv` with direct frequencies; and
- `<name>_sku_pairs.csv` with derived relationships.

The JSON contains the active date range, metric and threshold metadata, SKU
totals, a sparse direct-frequency matrix, and up to 20 qualifying relationships
per SKU. Slotting reads the raw Excel workbook rather than requiring this export,
so every selected input file is fingerprinted, analyzed, and calibrated on its
own data.

## Use in slotting

The `abc_affinity` strategy keeps ABC class, physical compatibility, temperature
compatibility, and capacity logic ahead of affinity. The user chooses only the
strategy and affinity weight for the initial run. The service then derives the
minimum shared store-days and affinity score from empirical quantiles of the
current workbook's relationships. It derives service-distance allowance
candidates from the current warehouse map and selects a candidate from the
relationship confidence and chosen affinity weight. The result is compared
against the basic baseline. These recommendations are displayed after
generation and can be edited for a subsequent regeneration.

## Common errors

- **Required columns not found** — confirm that the workbook contains the exact
  `Date`, `Store ID`, and `Item or SKU` headings within its first 50 rows.
- **No events in range** — reset the date fields or choose a range covered by
  the source workbook.
- **No qualifying relationships** — lower the shared-store-day threshold or select
  a more frequently ordered SKU.

Return to the [documentation index](README.md).
