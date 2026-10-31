import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { resolve } from "node:path";
import type { Row } from "../shared/model.js";
export const physical = {
  max_item_length: 0.1,
  max_item_width: 0.1,
  max_item_height: 0.1,
  max_item_weight: 1,
};
const standard = {
  max_item_length: 1.9,
  max_item_width: 0.5,
  max_item_height: 1,
  max_item_weight: 12.5,
};
export function project(rackCount = 3, levels = 1, slots = 2): Row {
  const markers = [
    { column: 0, row: 0, role: "workstation", endpoint_id: "WS" },
    ...Array.from({ length: rackCount }, (_, i) => ({
      column: i + 1,
      row: 0,
      role: "rack",
      endpoint_id: `R${i + 1}`,
    })),
  ];
  return {
    schema: "rmf_grid_map_editor/v2",
    grid: {
      width_m: rackCount,
      length_m: 1,
      spacing_m: 1,
      spacing_y_m: 1,
      map_name: "test",
      level_name: "L1",
    },
    markers,
    warehouse_storage_defaults: standard,
    machine_carrying_capacity: { max_item_weight: 150 },
    storage_layout: {
      system_type: "AMR",
      levels_per_rack: levels,
      slots_per_level: slots,
      handling_unit_type: "AMR shelf",
      buffer_level: "grid",
      machine_carrying_capacity: { max_item_weight: 150 },
      buffers: markers
        .filter((m) => m.role === "rack")
        .map((m) => ({
          buffer_id: `B-G${m.column}_0`,
          grid_waypoint: `G${m.column}_0`,
          buffer_level: "grid",
        })),
    },
    zone_assignments: Object.fromEntries(
      markers
        .filter((m) => m.role === "rack")
        .map((m) => [`G${m.column}_0`, m.column % 2 ? "Z1" : "Z2"]),
    ),
    location_attributes: {
      Z1: { ...standard, chilled: false },
      Z2: { ...standard, chilled: false },
    },
    attribute_catalog: [
      ...Object.keys(physical).map((key) => ({
        key,
        value_type: "number",
        match_rule: "capacity",
        label: key,
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
  };
}
export function sku(
  name: string,
  frequency = 1,
  quantity = 1,
  slots = 1,
  requirements: Row = physical,
): Row {
  return {
    sku: name,
    velocity_class: "A",
    pick_frequency: frequency,
    total_required_ea: quantity,
    total_slotted_ea: quantity,
    required_slots: slots,
    slots_per_unit: 1,
    sku_requirements: requirements,
  };
}
export function native(request: Row, fail = false): Row {
  const process = spawnSync(resolve("build/layout-engine"), [], {
    input: JSON.stringify(request),
    encoding: "utf8",
    maxBuffer: 128 * 1024 * 1024,
  });
  assert.equal(process.error, undefined);
  let data: Row;
  try {
    data = JSON.parse(process.stdout);
  } catch {
    throw new Error(process.stderr || "Invalid engine output");
  }
  assert.equal(Boolean(process.status), fail, data.error);
  return data;
}
