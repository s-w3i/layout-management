import { aliases, parseRequirement, physicalKeys, type Row } from "./model.js";

export const physicalLabels: Record<string, string> = {
  max_item_length: "Length (m)",
  max_item_width: "Width (m)",
  max_item_height: "Height (m)",
  max_item_weight: "Weight (kg)",
};
export function skuRequirements(
  row: Row,
  attributes: Record<string, Row>,
): Row {
  const values = { ...row.sku_requirements };
  for (const [key, value] of Object.entries(row))
    if (key.startsWith("req_") && value !== "" && value != null) {
      const field = key.slice(4);
      values[aliases[field] || field] = value;
    }
  const effective = { ...values, ...attributes[row.sku] };
  for (const [key, value] of Object.entries(effective)) {
    if (value == null || value === "") continue;
    try {
      effective[key] = parseRequirement(key, value);
    } catch {
      /* Keep invalid physical values available for the completeness review. */
    }
  }
  return effective;
}
export function incompleteSkus(
  velocity: Row[],
  attributes: Record<string, Row>,
): Row[] {
  return velocity
    .map((row) => {
      const values = skuRequirements(row, attributes);
      const missing = physicalKeys.filter(
        (key) =>
          values[key] == null ||
          values[key] === "" ||
          !Number.isFinite(Number(values[key])) ||
          Number(values[key]) <= 0,
      );
      return {
        ...row,
        values,
        missing,
        missing_fields: missing.map((key) => physicalLabels[key]).join(", "),
      };
    })
    .filter((row) => row.missing.length);
}
// Only this explicit correction path replaces embedded imported requirements.
export function correctSkuRows(rows: Row[], sku: string, values: Row): Row[] {
  return rows.map((row) => {
    if (row.sku !== sku) return row;
    const updated: Row = {
      ...row,
      sku_requirements: { ...row.sku_requirements, ...values },
    };
    for (const key of Object.keys(row))
      if (
        key.startsWith("req_") &&
        Object.hasOwn(values, aliases[key.slice(4)] || key.slice(4))
      )
        updated[key] = values[aliases[key.slice(4)] || key.slice(4)];
    return updated;
  });
}
export function preparationKey(
  velocity: Row[],
  attributes: Record<string, Row>,
  settings: Row,
): string {
  const input = JSON.stringify([
    settings.start_date,
    settings.end_date,
    settings.a_limit,
    settings.b_limit,
    velocity.map((row) => [
      row.sku,
      row.pick_frequency,
      row.velocity_class,
      skuRequirements(row, attributes),
    ]),
  ]);
  let a = 2166136261,
    b = 5381;
  for (let i = 0; i < input.length; i++) {
    a = Math.imul(a ^ input.charCodeAt(i), 16777619);
    b = Math.imul(b, 33) ^ input.charCodeAt(i);
  }
  return `${input.length}:${a >>> 0}:${b >>> 0}`;
}

export function applySkuAttributes(
  rows: Row[],
  attributes: Record<string, Row>,
): Row[] {
  return rows.map((row) =>
    attributes[row.sku]
      ? correctSkuRows([row], row.sku, attributes[row.sku])[0]
      : row,
  );
}
