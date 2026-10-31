import type { Row } from "../../shared/model";
export const standard = {
  max_item_length: 1.9,
  max_item_width: 0.5,
  max_item_height: 1,
  max_item_weight: 12.5,
};
export function newProject(): Row {
  return {
    schema: "rmf_grid_map_editor/v2",
    grid: {
      width_m: 24,
      length_m: 16,
      spacing_m: 2,
      spacing_y_m: 2,
      map_name: "Untitled warehouse",
      level_name: "L1",
    },
    warehouse_storage_defaults: { ...standard },
    machine_carrying_capacity: {
      max_item_length: null,
      max_item_width: null,
      max_item_height: null,
      max_item_weight: 150,
    },
    markers: [],
    zone_assignments: {},
    location_attributes: {
      Z01: { ...standard, chilled: false, oversize_capable: false },
    },
    attribute_catalog: [
      ...Object.keys(standard).map((key) => ({
        key,
        label: key.replace("max_item_", ""),
        value_type: "number",
        match_rule: "capacity",
        unit: key.endsWith("weight") ? "kg" : "m",
      })),
      {
        key: "chilled",
        label: "Chilled",
        value_type: "boolean",
        match_rule: "exact",
      },
      {
        key: "oversize_capable",
        label: "Oversize capable",
        value_type: "boolean",
        match_rule: "exact",
      },
    ],
    storage_layout: {
      system_type: "AMR",
      handling_unit_type: "AMR shelf",
      buffer_level: "grid",
      levels_per_rack: 3,
      slots_per_level: 4,
      rack_height_m: 3.1,
      clearance_m: 0.05,
      buffers: [],
    },
  };
}
export function syncBuffers(project: Row): Row {
  const p = structuredClone(project),
    s = p.storage_layout ?? {},
    amr = s.system_type === "AMR";
  const levels = Number(s.levels_per_rack ?? 3),
    slots = Number(s.slots_per_level ?? 4);
  if (
    !Number.isInteger(levels) ||
    levels < 1 ||
    levels > 64 ||
    !Number.isInteger(slots) ||
    slots < 1 ||
    slots > 64 ||
    levels * slots > 256
  )
    throw new Error("Rack capacity must be between 1 and 256 cells");
  s.buffer_level = amr ? "grid" : "slot";
  s.handling_unit_type = amr
    ? "AMR shelf"
    : s.system_type === "Pallet ASRS"
      ? "Pallet"
      : "Tote";
  s.machine_carrying_capacity = p.machine_carrying_capacity;
  s.buffers = [];
  s.slots = [];
  const sx = Number(p.grid.spacing_m),
    sy = Number(p.grid.spacing_y_m ?? sx),
    height = Number(s.rack_height_m ?? 3.1),
    clearance = Number(s.clearance_m ?? 0.05);
  const width = sx - 2 * clearance,
    depth = sy - 2 * clearance,
    slotHeight = (height - 2 * clearance) / levels;
  if (Math.min(width, depth, slotHeight) <= 0)
    throw new Error(
      "Grid spacing and rack height must exceed twice the outer clearance",
    );
  const overrides = new Map<string, Row>(
    (p.coordinate_overrides ?? []).map((o: Row) => [
      `G${o.column}_${o.row}`,
      o,
    ]),
  );
  for (const m of p.markers ?? [])
    if (m.role === "rack") {
      const id = `G${m.column}_${m.row}`,
        base = {
          column: m.column,
          row: m.row,
          grid_waypoint: id,
          rack_endpoint_id: m.endpoint_id,
          buffer_level: s.buffer_level,
          status: "EMPTY",
        };
      if (amr) s.buffers.push({ ...base, buffer_id: `B-${id}` });
      const x = overrides.get(id)?.x ?? Math.min(m.column * sx, p.grid.width_m),
        y = overrides.get(id)?.y ?? Math.min(m.row * sy, p.grid.length_m);
      for (let level = 1; level <= levels; level++)
        for (let slot = 1; slot <= slots; slot++) {
          if (!amr)
            s.buffers.push({
              ...base,
              buffer_id: `B-${id}/L${String(level).padStart(2, "0")}/S${String(slot).padStart(2, "0")}`,
              level,
              slot,
            });
          s.slots.push({
            rack_id: id,
            level,
            slot,
            center_x: x - width / 2 + ((slot - 0.5) * width) / slots,
            center_y: y,
            center_z: clearance + (level - 0.5) * slotHeight,
            length: depth,
            width: width / slots,
            height: slotHeight,
          });
        }
    }
  p.storage_layout = s;
  return p;
}
export function augmentCatalog(
  project: Row,
  attributes: Record<string, Row>,
): Row {
  const p = structuredClone(project),
    known = new Set((p.attribute_catalog ?? []).map((d: Row) => d.key));
  for (const req of Object.values(attributes))
    for (const [key, value] of Object.entries(req))
      if (!known.has(key)) {
        p.attribute_catalog ??= [];
        p.attribute_catalog.push({
          key,
          label: key.replaceAll("_", " "),
          value_type: typeof value === "boolean" ? "boolean" : "number",
          match_rule: typeof value === "boolean" ? "exact" : "capacity",
          unit: key.endsWith("weight")
            ? "kg"
            : typeof value === "number"
              ? "m"
              : "",
        });
        known.add(key);
      }
  return p;
}
