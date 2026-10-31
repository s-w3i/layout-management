import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { layoutScene, visibleScene } from "../shared/layout3d.js";

const project = {
  grid: { width_m: 8, length_m: 6, spacing_m: 2, spacing_y_m: 2 },
  storage_layout: {
    system_type: "AMR",
    levels_per_rack: 3,
    slots_per_level: 4,
    rack_height_m: 3.1,
  },
};
const geometry = { racks: [{ id: "G2_2", x: 4, y: 4, zone: "Z1" }] };
const assigned = {
  assignment_status: "ASSIGNED",
  inventory_load_id: "A-1",
  sku: "A",
  rack_id: "G2_2",
  velocity_class: "A",
  physical_data_status: "COMPLETE",
  occupied_handling_units: [
    { rack_id: "G2_2", storage_level: 1, storage_slot: 1 },
    { rack_id: "G2_2", storage_level: 1, storage_slot: 2 },
    { rack_id: "G2_2", storage_level: 2, storage_slot: 1 },
    { rack_id: "G2_2", storage_level: 2, storage_slot: 2 },
  ],
};
test("3D uses the complete contiguous allocation footprint and leaves source inventory unchanged", () => {
  const result = { assignments: [assigned] },
    snapshot = JSON.stringify(result);
  const model = layoutScene(project, geometry, result);
  assert.equal(model.cells.length, 12);
  assert.equal(model.loads.length, 1);
  assert.equal(model.loads[0].cells.length, 4);
  assert.ok(Math.abs(model.loads[0].width - 0.95) < 1e-9);
  assert.ok(Math.abs(model.loads[0].height - 2) < 1e-9);
  assert.equal(model.estimatedCells, 12);
  assert.equal(JSON.stringify(result), snapshot);
  const cut = visibleScene(model, 1);
  assert.equal(cut.cells.length, 4);
  assert.equal(cut.loads[0].cells.length, 2);
  assert.ok(Math.abs(cut.loads[0].height - 1) < 1e-9);
  assert.ok(Math.abs(model.loads[0].height - 2) < 1e-9);
});
test("3D respects explicit slot coordinates, empty cells and configured ASRS buffer subsets", () => {
  const storage = {
    system_type: "Mini-load ASRS",
    levels_per_rack: 3,
    slots_per_level: 4,
    buffers: [{ grid_waypoint: "G2_2", level: 2, slot: 3 }],
    slots: [
      {
        rack_id: "G2_2",
        level: 2,
        slot: 3,
        center_x: 12,
        center_y: 7,
        center_z: 4,
        width: 0.8,
        length: 1.5,
        height: 0.9,
      },
    ],
  };
  const model = layoutScene(project, geometry, {
    storage_layout: storage,
    assignments: [],
  });
  assert.equal(model.cells.length, 1);
  assert.deepEqual(
    { x: model.cells[0].x, y: model.cells[0].y, z: model.cells[0].z },
    { x: 12, y: 7, z: 4 },
  );
  assert.equal(model.estimatedCells, 0);
  assert.equal(model.loads.length, 0);
  assert.equal(visibleScene(model, 1).racks.length, 0);
  assert.equal(visibleScene(model, 3, "other").cells.length, 0);
});
test("unassigned and unknown cells do not become fabricated 3D allocations", () => {
  const model = layoutScene(project, geometry, {
    assignments: [
      { ...assigned, assignment_status: "UNASSIGNED_NO_CAPACITY" },
      {
        ...assigned,
        occupied_handling_units: [
          { rack_id: "missing", storage_level: 1, storage_slot: 1 },
        ],
      },
    ],
  });
  assert.equal(model.loads.length, 0);
  assert.equal(model.unmappedUnits, 1);
});
test("legacy occupied-unit geometry keeps its explicit horizontal and vertical coordinates", () => {
  const unit = {
    rack_id: "G2_2",
    storage_level: 1,
    storage_slot: 1,
    center_x: 10,
    center_y: 20,
    center_z: 2,
    width: 0.7,
    length: 1.2,
    height: 0.8,
  };
  const result = {
    storage_layout: {
      system_type: "Pallet ASRS",
      levels_per_rack: 1,
      slots_per_level: 1,
      buffers: [{ grid_waypoint: "G2_2", level: 1, slot: 1 }],
    },
    assignments: [{ ...assigned, occupied_handling_units: [unit] }],
  };
  const model = layoutScene(project, geometry, result);
  assert.equal(model.estimatedCells, 0);
  assert.equal(model.loads.length, 1);
  assert.deepEqual(
    [model.loads[0].x, model.loads[0].y, model.loads[0].z],
    [10, 20, 2],
  );
  assert.ok(Math.abs(model.loads[0].width - 0.7) < 1e-9);
});
test("the real AMR example displays exactly the native capacity", () => {
  const p = JSON.parse(readFileSync("examples/amr.grid.json", "utf8"));
  const racks = p.markers
    .filter((m: any) => m.role === "rack")
    .map((m: any) => ({
      id: `G${m.column}_${m.row}`,
      x: m.column * p.grid.spacing_m,
      y: m.row * p.grid.spacing_y_m,
      zone: p.zone_assignments[`G${m.column}_${m.row}`],
    }));
  const model = layoutScene(p, { racks }, { assignments: [] });
  assert.equal(model.racks.length, 342);
  assert.equal(model.cells.length, 4104);
  assert.ok(
    model.cells.every((c) =>
      [c.x, c.y, c.z, c.width, c.depth, c.height].every(Number.isFinite),
    ),
  );
  assert.equal(model.cells.length, new Set(model.cells.map((c) => c.key)).size);
});
