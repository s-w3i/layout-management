export type Row = Record<string, any>;
export interface Settings extends Row {
  strategy: "basic" | "abc_affinity";
  minimum_stock_days: number;
  buffer_stock_days: number;
  a_limit: number;
  b_limit: number;
  start_date: string;
  end_date: string;
  maximum_same_sku_slots_per_rack: number;
  zone_workload_enabled: boolean;
  ergonomic_weight_heuristic: boolean;
  store_workstation_mapping: Record<string, string>;
}
export const defaultSettings: Settings = {
  strategy: "basic",
  minimum_stock_days: 2,
  buffer_stock_days: 1,
  a_limit: 0.8,
  b_limit: 0.95,
  start_date: "",
  end_date: "",
  maximum_same_sku_slots_per_rack: 12,
  zone_workload_enabled: false,
  ergonomic_weight_heuristic: true,
  store_workstation_mapping: {},
};
export const physicalKeys = [
  "max_item_length",
  "max_item_width",
  "max_item_height",
  "max_item_weight",
];
export const aliases: Record<string, string> = {
  length: "max_item_length",
  width: "max_item_width",
  height: "max_item_height",
  weight: "max_item_weight",
  chilled_required: "chilled",
};
export function parseRequirement(key: string, raw: unknown): number | boolean {
  if (
    !/^[a-z][a-z0-9_]*$/.test(key) ||
    ["__proto__", "constructor", "prototype"].includes(key)
  )
    throw new Error(`Invalid attribute key: ${key}`);
  if (physicalKeys.includes(key)) {
    const n = Number(raw);
    if (!Number.isFinite(n) || n < 0 || (key !== "max_item_weight" && n === 0))
      throw new Error(
        `${key} must be ${key === "max_item_weight" ? "nonnegative" : "positive"}`,
      );
    return n;
  }
  if (typeof raw === "boolean") return raw;
  const value = String(raw).trim().toLowerCase();
  if (["true", "yes", "y", "1"].includes(value)) return true;
  if (["false", "no", "n", "0"].includes(value)) return false;
  throw new Error(`${key} must contain true/false, yes/no, or 1/0`);
}
export function attributeRows(rows: Row[]): Record<string, Row> {
  const result: Record<string, Row> = Object.create(null);
  for (const row of rows) {
    const sku = String(row.sku ?? "").trim();
    if (!sku || result[sku])
      throw new Error("Attribute CSV contains a blank or duplicate SKU");
    const req: Row = {};
    for (const [column, value] of Object.entries(row)) {
      if (column === "sku" || value === "" || value == null) continue;
      const source = column.toLowerCase().replace(/^req_/, ""),
        key = aliases[source] ?? source;
      if (
        !/^[a-z][a-z0-9_]*$/.test(key) ||
        ["__proto__", "constructor", "prototype"].includes(key)
      )
        throw new Error(`Invalid attribute key: ${key}`);
      if (Object.hasOwn(req, key))
        throw new Error(`Two columns map to attribute ${key}`);
      req[key] = parseRequirement(key, value);
    }
    result[sku] = req;
  }
  return result;
}
export function mergeRows(
  velocity: Row[],
  stock: Row[],
  attributes: Record<string, Row>,
): Row[] {
  const stockBySku = new Map<string, Row>();
  for (const row of stock) {
    const sku = String(row.sku ?? "").trim();
    if (!sku || stockBySku.has(sku))
      throw new Error("Stock CSV contains a blank or duplicate SKU");
    stockBySku.set(sku, row);
  }
  const known = new Set<string>();
  const rows = velocity.map((source) => {
    const sku = String(source.sku ?? "").trim();
    if (!sku || known.has(sku))
      throw new Error("Velocity CSV contains a blank or duplicate SKU");
    known.add(sku);
    if (
      !Number.isFinite(Number(source.pick_frequency)) ||
      Number(source.pick_frequency) < 0 ||
      !["A", "B", "C"].includes(String(source.velocity_class))
    )
      throw new Error(`Invalid velocity data for SKU ${sku}`);
    const req = { ...source.sku_requirements };
    for (const [column, value] of Object.entries(source))
      if (column.startsWith("req_") && value !== "" && value != null)
        req[aliases[column.slice(4)] ?? column.slice(4)] = parseRequirement(
          aliases[column.slice(4)] ?? column.slice(4),
          value,
        );
    for (const [key, value] of Object.entries(attributes[sku] ?? {})) {
      if (
        req[key] !== undefined &&
        req[key] !== value &&
        !(
          typeof value === "number" &&
          Math.abs(Number(req[key]) - value) <=
            1e-9 * Math.max(1, Math.abs(value))
        )
      )
        throw new Error(`Conflicting ${key} requirement for SKU ${sku}`);
      req[key] = value;
    }
    const stockValues: Row = {};
    const stockRow = stockBySku.get(sku);
    for (const key of [
      "total_required_ea",
      "total_slotted_ea",
      "units_per_slot",
      "slots_per_unit",
      "required_slots",
      "required_racks",
      "rack_calculation_status",
    ])
      if (stockRow && Object.hasOwn(stockRow, key))
        stockValues[key] = stockRow[key];
    return { ...source, ...stockValues, sku, sku_requirements: req };
  });
  return rows;
}
export function csvString(rows: Row[]): string {
  const fields = [...new Set(rows.flatMap((row) => Object.keys(row)))];
  const value = (v: unknown) => {
    let s =
      v == null ? "" : typeof v === "object" ? JSON.stringify(v) : String(v);
    if (/^[=+@\t\r]/.test(s) || (/^-/.test(s) && !Number.isFinite(Number(s))))
      s = "'" + s;
    return /[",\r\n]/.test(s) ? `"${s.replaceAll('"', '""')}"` : s;
  };
  return [
    fields.map(value).join(","),
    ...rows.map((r) => fields.map((f) => value(r[f])).join(",")),
  ].join("\r\n");
}
export function compareLayouts(current: Row, reference: Row): Row {
  const assignments: Row[] = current.assignments ?? [],
    previous: Row[] = reference.assignments ?? [];
  const byId = new Map(
    previous.map((r) => [String(r.inventory_load_id ?? r.sku), r]),
  );
  let comparable = 0,
    identical = 0,
    statusChanges = 0;
  const differences: Row[] = [];
  const quantity = (rows: Row[]) => {
    const result: Record<string, number> = Object.create(null);
    for (const r of rows)
      if (r.assignment_status === "ASSIGNED")
        result[r.sku] = (result[r.sku] ?? 0) + Number(r.quantity_ea ?? 0);
    return result;
  };
  const a = quantity(assignments),
    b = quantity(previous);
  for (const row of assignments) {
    const old = byId.get(String(row.inventory_load_id ?? row.sku));
    if (!old) continue;
    comparable++;
    const same =
      row.assignment_status === old.assignment_status &&
      row.storage_location_address === old.storage_location_address &&
      JSON.stringify(row.occupied_storage_location_addresses ?? []) ===
        JSON.stringify(old.occupied_storage_location_addresses ?? []) &&
      Number(row.quantity_ea) === Number(old.quantity_ea);
    if (same) identical++;
    else if (differences.length < 50)
      differences.push({
        load: row.inventory_load_id,
        sku: row.sku,
        reference: old.storage_location_address || old.assignment_status,
        current: row.storage_location_address || row.assignment_status,
      });
    if (row.assignment_status !== old.assignment_status) statusChanges++;
  }
  const quantitiesEqual = Object.keys({ ...a, ...b }).every(
    (s) => Math.abs((a[s] ?? 0) - (b[s] ?? 0)) < 1e-9,
  );
  const canonical = (value: any): any =>
    Array.isArray(value)
      ? value.map(canonical)
      : value && typeof value === "object"
        ? Object.fromEntries(
            Object.keys(value)
              .sort()
              .map((key) => [key, canonical(value[key])]),
          )
        : value;
  const equal = (a: any, b: any) =>
    JSON.stringify(canonical(a)) === JSON.stringify(canonical(b));
  const levels = (layout: Row) =>
    Number(
      layout.rack_capacity?.levels_per_rack ?? layout.rack_capacity?.levels,
    );
  const geometry = (layout: Row) =>
    Object.entries(layout.building?.levels ?? {}).map(
      ([name, level]: [string, any]) => ({
        name,
        vertices: (level.vertices ?? []).map((v: any[]) => [
          v[0],
          v[1],
          v[3],
          v[4]?.pickup_dispenser?.[1] ?? "",
          v[4]?.dropoff_ingestor?.[1] ?? "",
        ]),
        lanes: (level.lanes ?? [])
          .map((l: any[]) => [l[0], l[1], Boolean(l[2]?.bidirectional?.[1])])
          .sort((a: number[], b: number[]) => a[0] - b[0] || a[1] - b[1]),
      }),
    );
  const parameterChecks: Row = {
    strategy: reference.strategy === current.strategy,
    levels: levels(reference) === levels(current),
    slots:
      Number(reference.rack_capacity?.slots_per_level) ===
      Number(current.rack_capacity?.slots_per_level),
    same_sku_limit:
      Number(
        reference.summary?.maximum_same_sku_slots_per_rack ??
          levels(reference) * Number(reference.rack_capacity?.slots_per_level),
      ) === Number(current.summary?.maximum_same_sku_slots_per_rack),
    zone_balance:
      Boolean(reference.summary?.zone_workload_enabled) ===
      Boolean(current.summary?.zone_workload_enabled),
    ergonomic_preference:
      Boolean(
        reference.summary?.ergonomic_weight_heuristic ??
          reference.settings?.ergonomic_weight_heuristic ??
          true,
      ) ===
      Boolean(
        current.summary?.ergonomic_weight_heuristic ??
          current.settings?.ergonomic_weight_heuristic ??
          true,
      ),
    warehouse_graph: equal(geometry(reference), geometry(current)),
    storage_rules: equal(
      reference.location_attributes ?? {},
      current.location_attributes ?? {},
    ),
    standard_envelope: equal(
      reference.storage_defaults?.standard ?? {},
      current.storage_defaults?.standard ?? {},
    ),
    sku_requirements: assignments.every((row) =>
      equal(
        row.sku_requirements ?? {},
        byId.get(String(row.inventory_load_id ?? row.sku))?.sku_requirements ??
          {},
      ),
    ),
    demand_priority: assignments.every((row) => {
      const old = byId.get(String(row.inventory_load_id ?? row.sku));
      return (
        old &&
        row.velocity_class === old.velocity_class &&
        Number(row.pick_frequency) === Number(old.pick_frequency)
      );
    }),
    zones:
      JSON.stringify(
        Object.entries(reference.zone_assignments ?? {}).sort(),
      ) ===
      JSON.stringify(Object.entries(current.zone_assignments ?? {}).sort()),
    stock_loads:
      assignments.length === previous.length &&
      comparable === assignments.length &&
      assignments.every((row) => {
        const old = byId.get(String(row.inventory_load_id ?? row.sku));
        return (
          old &&
          row.sku === old.sku &&
          Number(row.quantity_ea) === Number(old.quantity_ea) &&
          Number(row.total_required_ea) === Number(old.total_required_ea)
        );
      }),
  };
  return {
    reference_strategy: reference.strategy,
    reference_algorithm:
      reference.summary?.affinity_tuning?.method ??
      reference.affinity_configuration?.method ??
      "saved Basic",
    parameter_checks: parameterChecks,
    parameters_match: Object.values(parameterChecks).every(Boolean),
    comparable_loads: comparable,
    identical_placements: identical,
    placement_match_fraction: comparable ? identical / comparable : 0,
    inventory_quantities_equal: quantitiesEqual,
    assignment_status_changes: statusChanges,
    current_loads: assignments.length,
    reference_loads: previous.length,
    current_assigned: assignments.filter(
      (r) => r.assignment_status === "ASSIGNED",
    ).length,
    reference_assigned: previous.filter(
      (r) => r.assignment_status === "ASSIGNED",
    ).length,
    differences,
  };
}
