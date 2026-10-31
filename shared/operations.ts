import type { Row } from "./model.js";

export function scenarioRows(layout: Row): Row[] {
  if (!Array.isArray(layout.assignments) || !layout.assignments.length)
    throw new Error("The file has no inventory assignments.");
  const skus = new Map<string, Row>();
  for (const row of layout.assignments) {
    const sku = String(row.sku ?? "").trim();
    if (!sku || row.required_slot_count == null || row.total_slotted_ea == null)
      throw new Error(
        "The layout must include SKU stock targets and physical load identities.",
      );
    if (!skus.has(sku))
      skus.set(sku, { ...row, sku, required_slots: row.required_slot_count });
  }
  return [...skus.values()];
}

export function findInventory(rows: Row[], query: string): Row[] {
  const search = query.trim().toLowerCase();
  if (!search) return rows;
  const exact = rows.filter((row) => String(row.sku).toLowerCase() === search);
  return exact.length
    ? exact
    : rows.filter((row) => String(row.sku).toLowerCase().includes(search));
}

export const movementColor = (visits: number, maximum: number) =>
  visits > 0 && maximum > 0
    ? `hsl(212, 76%, ${90 - 57 * Math.min(1, visits / maximum)}%)`
    : "#e5e9ee";
