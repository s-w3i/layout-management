import type { Row } from "./model.js";
import { validDate } from "./dates.js";
import { scaledQuantity } from "./inventory.js";

export type Vec3 = [number, number, number];
export type SimulationSpeed = number | "max";
export const asrsEngineVersion = "asrs_quantity/v1";
export type DateMode =
  | "one_date"
  | "multiple_dates"
  | "one_month"
  | "multiple_months"
  | "full";
export interface SimulationSelection {
  mode: DateMode;
  dates?: string[];
  months?: string[];
}
export interface Axis {
  speed: number;
  acceleration: number;
}
export interface AsrsSettings {
  axes: [Axis, Axis, Axis];
  picking_seconds: number;
  extraction_seconds: number;
  replacement_seconds: number;
  replenishment_lead_seconds: number;
  workstation_height_m: number;
  speed: SimulationSpeed;
  skip_idle: boolean;
}
export const defaultAsrsSettings: AsrsSettings = {
  axes: [
    { speed: 3, acceleration: 1 },
    { speed: 0.5, acceleration: 0.5 },
    { speed: 1, acceleration: 0.5 },
  ],
  picking_seconds: 10,
  extraction_seconds: 0,
  replacement_seconds: 0,
  replenishment_lead_seconds: 60,
  workstation_height_m: 0.5375,
  speed: 1,
  skip_idle: true,
};
export interface AsrsLoad {
  id: string;
  sku: string;
  address: string;
  rack: string;
  crane: string;
  position: Vec3;
  initial: number;
}
export interface AsrsModel {
  loads: AsrsLoad[];
  cranes: { id: string; workstation: string; home: Vec3; rows: number[] }[];
  buffers: Record<string, number>;
  warnings: string[];
}
export interface SimulationOrder {
  source_row: number;
  date: string;
  sku: string;
  store: string;
  quantity_micro: number;
  source_time: string;
  uom: string;
  order_reference: string;
}
export interface AsrsPhase {
  start: number;
  end: number;
  origin: Vec3;
  target: Vec3;
  stage: string;
  loaded: boolean;
}

export function simulationDates(
  available: string[],
  selection: SimulationSelection,
): string[] {
  if (!selection || typeof selection !== "object")
    throw new Error("Choose a simulation date mode");
  const all = [...new Set(available)].sort();
  if (!all.length || all.some((d) => !validDate(d)))
    throw new Error("Order history has no valid observed dates");
  if (selection.mode === "full") return all;
  const single =
    selection.mode === "one_date" || selection.mode === "one_month";
  const byMonth =
    selection.mode === "one_month" || selection.mode === "multiple_months";
  if (
    !["one_date", "multiple_dates", "one_month", "multiple_months"].includes(
      selection.mode,
    )
  )
    throw new Error("Choose a valid simulation date mode");
  const chosen = byMonth ? selection.months : selection.dates;
  if (
    !Array.isArray(chosen) ||
    !chosen.length ||
    (single && chosen.length !== 1)
  )
    throw new Error(
      single
        ? "Choose exactly one date or month"
        : "Choose at least one date or month",
    );
  const valid = new Set(byMonth ? all.map((d) => d.slice(0, 7)) : all);
  if (chosen.some((d) => typeof d !== "string" || !valid.has(d)))
    throw new Error(
      "Choose dates or months present in the selected input data only",
    );
  const wanted = new Set(chosen);
  return all.filter((d) => wanted.has(byMonth ? d.slice(0, 7) : d));
}
export function validateSpeed(value: unknown): SimulationSpeed {
  if (value === "max") return value;
  if (
    typeof value !== "number" ||
    !Number.isFinite(value) ||
    value < 1 ||
    value > Number.MAX_SAFE_INTEGER
  )
    throw new Error("Speed must be at least 1×, or Max");
  return value;
}
export function asrsSettings(value: Row = {}): AsrsSettings {
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw new Error("Simulation settings must be an object");
  const settings = Object.fromEntries(
    Object.entries(defaultAsrsSettings).map(([key, fallback]) => [
      key,
      Object.hasOwn(value, key) ? value[key] : fallback,
    ]),
  ) as unknown as AsrsSettings;
  if (!Array.isArray(settings.axes) || settings.axes.length !== 3)
    throw new Error("Configure X, Y and Z motion");
  for (const axis of settings.axes)
    for (const key of ["speed", "acceleration"] as const)
      if (
        !axis ||
        typeof axis[key] !== "number" ||
        !Number.isFinite(axis[key]) ||
        axis[key] <= 0
      )
        throw new Error(
          "Axis speeds and accelerations must be positive finite numbers",
        );
  settings.axes = settings.axes.map((axis) => ({
    speed: axis.speed,
    acceleration: axis.acceleration,
  })) as AsrsSettings["axes"];
  for (const key of [
    "picking_seconds",
    "extraction_seconds",
    "replacement_seconds",
    "replenishment_lead_seconds",
    "workstation_height_m",
  ] as const)
    if (
      typeof settings[key] !== "number" ||
      !Number.isFinite(settings[key]) ||
      settings[key] < 0
    )
      throw new Error(`${key} must be a finite nonnegative number`);
  settings.speed = validateSpeed(settings.speed);
  if (typeof settings.skip_idle !== "boolean")
    throw new Error("Skip idle must be true or false");
  return settings;
}
export function axisDuration(distance: number, axis: Axis): number {
  const d = Math.abs(distance),
    { speed: v, acceleration: a } = axis;
  return d <= (v * v) / a ? 2 * Math.sqrt(d / a) : d / v + v / a;
}
export function axisDistance(
  distance: number,
  seconds: number,
  axis: Axis,
): number {
  const d = Math.abs(distance),
    total = axisDuration(d, axis),
    t = Math.max(0, Math.min(seconds, total));
  const ramp = Math.min(axis.speed / axis.acceleration, total / 2);
  return t <= ramp
    ? 0.5 * axis.acceleration * t * t
    : t >= total - ramp
      ? d - 0.5 * axis.acceleration * (total - t) ** 2
      : 0.5 * axis.acceleration * ramp * ramp +
        axis.acceleration * ramp * (t - ramp);
}
export function phasePosition(
  phase: AsrsPhase,
  now: number,
  axes: AsrsSettings["axes"],
): Vec3 {
  return phase.origin.map(
    (a, i) =>
      a +
      Math.sign(phase.target[i] - a) *
        axisDistance(phase.target[i] - a, now - phase.start, axes[i]),
  ) as Vec3;
}
export function safeSum(a: number, b: number): number {
  const sum = a + b;
  if (!Number.isSafeInteger(sum))
    throw new Error("Stock total exceeds exact quantity limits");
  return sum;
}
export function buildAsrsModel(
  release: Row,
  settings: AsrsSettings,
): AsrsModel {
  if (
    release.schema !== "layout_generator_release/v1" ||
    !release.warehouse ||
    !release.slotting
  )
    throw new Error("Expected a published Layout Generator warehouse release");
  const project = release.warehouse.project,
    storage = project?.storage_layout;
  if (storage?.system_type !== "Mini-load ASRS")
    throw new Error(
      "Publish a Mini-load ASRS warehouse; AMR and pallet systems need their own equipment models",
    );
  const sx = Number(project.grid.spacing_m),
    sy = Number(project.grid.spacing_y_m ?? sx);
  if (!(sx > 0 && sy > 0)) throw new Error("Warehouse grid spacing is invalid");
  const stations = (project.markers as Row[])
    .filter((m) => m.role === "workstation")
    .sort((a, b) =>
      String(a.endpoint_id).localeCompare(String(b.endpoint_id), "en", {
        numeric: true,
      }),
    );
  if (!stations.length) throw new Error("ASRS warehouse has no workstations");
  const cranes = stations.map((m, i) => ({
    id: `ASRS_${String(i + 1).padStart(2, "0")}`,
    workstation: String(m.endpoint_id),
    rows: [m.row - 1, m.row + 1],
    home: [m.column * sx, m.row * sy, settings.workstation_height_m] as Vec3,
  }));
  const byRow = new Map<number, (typeof cranes)[number]>();
  for (const crane of cranes)
    for (const row of crane.rows) {
      if (byRow.has(row))
        throw new Error(
          "Workstations share rack rows; fixed-aisle cranes require exclusive aisles",
        );
      byRow.set(row, crane);
    }
  const racks = new Map<string, Row>(
    (project.markers as Row[])
      .filter((m) => m.role === "rack")
      .map((m) => [`G${m.column}_${m.row}`, m]),
  );
  for (const [id, rack] of racks)
    if (!byRow.has(rack.row))
      throw new Error(
        `${id}: no adjacent workstation/crane can reach this rack row`,
      );
  const slots = new Map<string, Row>(
    (storage.slots ?? []).map((s: Row) => [
      `${s.rack_id}/${s.level}/${s.slot}`,
      s,
    ]),
  );
  const occupied = new Set<string>(),
    ids = new Set<string>(),
    loads: AsrsLoad[] = [],
    warnings: string[] = [];
  let unverified = 0;
  for (const row of release.slotting.assignments ?? []) {
    if (row.assignment_status !== "ASSIGNED") continue;
    if (
      row.handling_unit_type !== "Tote" ||
      row.occupied_slot_count !== 1 ||
      row.occupied_handling_units?.length !== 1
    )
      throw new Error(
        `${row.inventory_load_id}: the ported crane model requires one single-slot tote per load`,
      );
    if (row.compatibility_status === "INCOMPATIBLE")
      throw new Error(
        `${row.inventory_load_id}: incompatible physical assignment`,
      );
    if (row.physical_data_status !== "COMPLETE") unverified++;
    const key = `${row.rack_id}/${row.storage_level}/${row.storage_slot}`,
      slot = slots.get(key),
      rack = racks.get(row.rack_id);
    if (!slot || !rack || occupied.has(key) || ids.has(row.inventory_load_id))
      throw new Error(
        `${key}: missing geometry or duplicate occupied tote/slot`,
      );
    const position = [slot.center_x, slot.center_y, slot.center_z] as Vec3;
    if (
      !position.every(Number.isFinite) ||
      Math.abs(position[1] - rack.row * sy) > 1e-6 ||
      Math.abs(position[0] - rack.column * sx) > sx / 2 + 1e-6
    )
      throw new Error(
        `${key}: slot coordinates disagree with the published rack`,
      );
    const unit = row.occupied_handling_units[0];
    for (const [index, axis] of ["x", "y", "z"].entries())
      if (
        unit[`center_${axis}`] != null &&
        Math.abs(unit[`center_${axis}`] - position[index]) > 1e-6
      )
        throw new Error(`${key}: assignment and map coordinates differ`);
    occupied.add(key);
    ids.add(row.inventory_load_id);
    const initial = scaledQuantity(String(row.quantity_ea));
    if (initial < 0)
      throw new Error(`${row.sku}: assigned quantity cannot be negative`);
    loads.push({
      id: row.inventory_load_id,
      sku: String(row.sku),
      address: String(row.storage_location_address),
      rack: row.rack_id,
      crane: byRow.get(rack.row)!.id,
      position,
      initial,
    });
  }
  if (!loads.length)
    throw new Error("Published warehouse has no assigned totes");
  const buffers: Record<string, number> = Object.create(null);
  const stock = new Map<string, Row>(
    (release.planning_inputs?.stock ?? []).map((s: Row) => [String(s.sku), s]),
  );
  for (const load of loads)
    if (!Object.hasOwn(buffers, load.sku)) {
      const threshold = stock.get(load.sku)?.minimum_buffer_stock_ea;
      if (threshold == null)
        throw new Error(
          `${load.sku}: buffer stock is missing. Recalculate stock requirements and republish the warehouse`,
        );
      buffers[load.sku] = scaledQuantity(String(threshold));
      if (buffers[load.sku] < 0)
        throw new Error(`${load.sku}: buffer stock cannot be negative`);
    }
  if (unverified)
    warnings.push(
      `${unverified} assigned totes have incomplete physical data; physical fit remains unverified.`,
    );
  return { loads, cranes, buffers, warnings };
}
