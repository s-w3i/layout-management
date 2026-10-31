import type { Row } from "./model.js";

export interface Box3D {
  x: number;
  y: number;
  z: number;
  width: number;
  depth: number;
  height: number;
}
export interface Cell3D extends Box3D {
  key: string;
  rack: string;
  level: number;
  slot: number;
  zone: string;
  estimated: boolean;
}
export interface Load3D extends Box3D {
  id: string;
  sku: string;
  rack: string;
  zone: string;
  velocity: string;
  unverified: boolean;
  cells: Cell3D[];
}
export const cellKey = (rack: string, level: number, slot: number) =>
  `${rack}/${level}/${slot}`;
const positive = (value: unknown, fallback: number) =>
  Number.isFinite(Number(value)) && Number(value) > 0
    ? Number(value)
    : fallback;
function explicitBox(row: Row): Box3D | null {
  if (
    !["center_x", "center_y", "center_z", "width", "length", "height"].every(
      (key) => row[key] != null && Number.isFinite(Number(row[key])),
    )
  )
    return null;
  if (Math.min(Number(row.width), Number(row.length), Number(row.height)) <= 0)
    return null;
  return {
    x: Number(row.center_x),
    y: Number(row.center_y),
    z: Number(row.center_z),
    width: Number(row.width),
    depth: Number(row.length),
    height: Number(row.height),
  };
}
export function enclosingBox(boxes: Box3D[]): Box3D {
  const minX = Math.min(...boxes.map((b) => b.x - b.width / 2)),
    maxX = Math.max(...boxes.map((b) => b.x + b.width / 2));
  const minY = Math.min(...boxes.map((b) => b.y - b.depth / 2)),
    maxY = Math.max(...boxes.map((b) => b.y + b.depth / 2));
  const minZ = Math.min(...boxes.map((b) => b.z - b.height / 2)),
    maxZ = Math.max(...boxes.map((b) => b.z + b.height / 2));
  return {
    x: (minX + maxX) / 2,
    y: (minY + maxY) / 2,
    z: (minZ + maxZ) / 2,
    width: maxX - minX,
    depth: maxY - minY,
    height: maxZ - minZ,
  };
}
export function layoutScene(project: Row, geometry: Row, result: Row) {
  const storage =
    result.storage_layout ??
    geometry.project?.storage_layout ??
    project.storage_layout ??
    {};
  const levels = positive(storage.levels_per_rack, 3),
    slots = positive(storage.slots_per_level, 4);
  const sx = positive(project.grid.spacing_m, 2),
    sy = positive(project.grid.spacing_y_m, sx);
  const clearance =
    Number.isFinite(Number(storage.clearance_m)) &&
    Number(storage.clearance_m) >= 0
      ? Number(storage.clearance_m)
      : 0.05;
  const width = Math.max(0.05, sx - 2 * clearance),
    depth = Math.max(0.05, sy - 2 * clearance),
    height = positive(storage.rack_height_m, 3.1);
  const cellHeight = Math.max(0.05, (height - 2 * clearance) / levels);
  const configured = new Set(
    (storage.buffers ?? []).map((b: Row) =>
      cellKey(b.grid_waypoint, Number(b.level), Number(b.slot)),
    ),
  );
  const restricted =
    storage.system_type != "AMR" && Array.isArray(storage.buffers);
  const explicit = new Map<string, Box3D>();
  for (const slot of storage.slots ?? []) {
    const box = explicitBox(slot);
    if (box)
      explicit.set(
        cellKey(slot.rack_id, Number(slot.level), Number(slot.slot)),
        box,
      );
  }
  // Older exports attach physical geometry to occupied units rather than the slot catalogue.
  for (const row of result.assignments ?? [])
    for (const unit of row.occupied_handling_units ?? []) {
      const box = explicitBox(unit);
      const key = cellKey(
        unit.rack_id ?? row.rack_id,
        Number(unit.storage_level),
        Number(unit.storage_slot),
      );
      if (box && !explicit.has(key)) explicit.set(key, box);
    }
  const cells: Cell3D[] = [];
  for (const rack of geometry.racks ?? [])
    for (let level = 1; level <= levels; level++)
      for (let slot = 1; slot <= slots; slot++) {
        const key = cellKey(rack.id, level, slot);
        if (restricted && !configured.has(key)) continue;
        const supplied = explicit.get(key);
        cells.push({
          key,
          rack: rack.id,
          zone: rack.zone,
          level,
          slot,
          estimated: !supplied,
          ...(supplied ?? {
            x: rack.x - width / 2 + ((slot - 0.5) * width) / slots,
            y: rack.y,
            z: clearance + (level - 0.5) * cellHeight,
            width: width / slots,
            depth,
            height: cellHeight,
          }),
        });
      }
  const byKey = new Map(cells.map((c) => [c.key, c]));
  const loads: Load3D[] = [];
  let unmappedUnits = 0;
  for (const row of result.assignments ?? []) {
    if (row.assignment_status !== "ASSIGNED") continue;
    const footprint: Cell3D[] = [];
    for (const unit of row.occupied_handling_units ?? []) {
      const c = byKey.get(
        cellKey(
          unit.rack_id ?? row.rack_id,
          Number(unit.storage_level),
          Number(unit.storage_slot),
        ),
      );
      if (c) footprint.push(c);
      else unmappedUnits++;
    }
    if (footprint.length)
      loads.push({
        id: row.inventory_load_id,
        sku: row.sku,
        rack: row.rack_id,
        zone: footprint[0].zone,
        velocity: row.velocity_class,
        unverified: row.physical_data_status !== "COMPLETE",
        cells: footprint,
        ...enclosingBox(footprint),
      });
  }
  const byRack = new Map<string, Cell3D[]>();
  for (const cell of cells) {
    if (!byRack.has(cell.rack)) byRack.set(cell.rack, []);
    byRack.get(cell.rack)!.push(cell);
  }
  const racks = (geometry.racks ?? []).map((r: Row) => {
    const rackCells = byRack.get(r.id) ?? [];
    return {
      ...r,
      box: rackCells.length
        ? enclosingBox(rackCells)
        : { x: r.x, y: r.y, z: height / 2, width, depth, height },
    };
  });
  return {
    racks,
    cells,
    loads,
    levels,
    estimatedCells: cells.filter((c) => c.estimated).length,
    unmappedUnits,
  };
}
export type LayoutScene = ReturnType<typeof layoutScene>;
export function visibleScene(
  model: LayoutScene,
  throughLevel: number,
  rackId = "",
) {
  const cells = model.cells.filter(
    (c) => c.level <= throughLevel && (!rackId || c.rack === rackId),
  );
  const visible = new Set(cells.map((c) => c.key));
  const loads = model.loads.flatMap((load) => {
    const footprint = load.cells.filter((c) => visible.has(c.key));
    return footprint.length
      ? [{ ...load, cells: footprint, ...enclosingBox(footprint) }]
      : [];
  });
  const byRack = new Map<string, Cell3D[]>();
  for (const cell of cells) {
    if (!byRack.has(cell.rack)) byRack.set(cell.rack, []);
    byRack.get(cell.rack)!.push(cell);
  }
  const racks = model.racks.flatMap((rack: Row) => {
    const footprint = byRack.get(rack.id);
    return footprint?.length ? [{ ...rack, box: enclosingBox(footprint) }] : [];
  });
  return { cells, loads, racks };
}
