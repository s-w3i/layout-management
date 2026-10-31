import type { Row } from "./model.js";

export interface TableFilter {
  key: string;
  label: string;
  options: { value: string; label: string }[];
  getValue?: (row: Row) => string;
}
export interface TableSort {
  key: string;
  direction: "ascending" | "descending";
}

export const demandFilters: TableFilter[] = [
  {
    key: "velocity_class",
    label: "ABC class",
    options: ["A", "B", "C"].map((value) => ({ value, label: value })),
  },
];

export function stockTableRows(stock: Row[], velocity: Row[]): Row[] {
  const classes = new Map(
    velocity.map((row) => [String(row.sku ?? "").trim(), row.velocity_class]),
  );
  return stock.map((row) => ({
    ...row,
    velocity_class:
      classes.get(String(row.sku ?? "").trim()) ?? row.velocity_class ?? "",
  }));
}

export function stockFilters(rows: Row[]): TableFilter[] {
  return [
    ...demandFilters,
    {
      key: "rack_calculation_status",
      label: "Physical data",
      options: [
        ...new Set(
          rows
            .map((row) => String(row.rack_calculation_status ?? ""))
            .filter(Boolean),
        ),
      ]
        .sort()
        .map((value) => ({
          value,
          label: value
            .replace("CALCULATED", "Sized")
            .replaceAll("_", " ")
            .toLowerCase(),
        })),
    },
  ];
}

export const inventoryFilters: TableFilter[] = [
  ...demandFilters,
  {
    key: "assignment_status",
    label: "Assignment",
    options: [
      { value: "ASSIGNED", label: "Assigned" },
      { value: "UNASSIGNED", label: "Unassigned" },
    ],
    getValue: (row) =>
      row.assignment_status === "ASSIGNED" ? "ASSIGNED" : "UNASSIGNED",
  },
  {
    key: "compatibility_status",
    label: "Physical check",
    options: [
      { value: "COMPATIBLE", label: "Compatible" },
      { value: "UNVERIFIED", label: "Unverified" },
      { value: "NOT_EVALUATED", label: "Not evaluated" },
      { value: "INCOMPATIBLE", label: "Incompatible" },
    ],
  },
];

export function filterTableRows(
  rows: Row[],
  query: string,
  selections: Record<string, string>,
  filters: TableFilter[],
): Row[] {
  const search = query.trim().toLowerCase();
  return rows.filter(
    (row) =>
      filters.every((filter) => {
        const selected = selections[filter.key];
        return (
          !selected ||
          selected === String(filter.getValue?.(row) ?? row[filter.key])
        );
      }) &&
      (!search ||
        Object.values(row).some(
          (value) =>
            typeof value !== "object" &&
            String(value ?? "")
              .toLowerCase()
              .includes(search),
        )),
  );
}

const collator = new Intl.Collator(undefined, {
  numeric: true,
  sensitivity: "base",
});
export function sortTableRows(rows: Row[], sort: TableSort | null): Row[] {
  if (!sort) return rows;
  const sign = sort.direction === "ascending" ? 1 : -1;
  return [...rows].sort((a, b) => {
    const left = a[sort.key],
      right = b[sort.key];
    // Missing values stay at the bottom in either direction.
    if (left == null || left === "")
      return right == null || right === "" ? 0 : 1;
    if (right == null || right === "") return -1;
    const numeric = [left, right].every(
      (value) =>
        typeof value === "number" ||
        (typeof value === "string" &&
          /^[+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?$/i.test(value.trim()) &&
          Math.abs(Number(value)) <= Number.MAX_SAFE_INTEGER),
    );
    return (
      sign *
      (numeric
        ? Number(left) - Number(right)
        : collator.compare(String(left), String(right)))
    );
  });
}
