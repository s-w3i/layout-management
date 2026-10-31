import test from "node:test";
import assert from "node:assert/strict";
import {
  addLane,
  deleteLane,
  lanePair,
  laneDirections,
  setLaneDirection,
  stockGeometryKey,
  numericDraft,
  nextGridPoint,
  validateGrid,
  zoneId,
} from "../shared/editor.js";
import { syncBuffers } from "../web/src/warehouse.js";
import { native, project } from "./fixtures.js";
import type { Row } from "../shared/model.js";

test("lane selectors retain canonical endpoints when the native graph reverses direction", () => {
  let p = project(2);
  const original = JSON.stringify(p),
    a = { column: 0, row: 0, id: "G0_0" },
    b = { column: 1, row: 0, id: "G1_0" };
  for (const direction of ["forward", "reverse", "both"] as const) {
    p = setLaneDirection(p, a, b, direction);
    const g = native({ command: "geometry", project: p }),
      edge = g.lanes.find(
        (l: Row) =>
          lanePair(g.nodes[l[0]], g.nodes[l[1]]).key === lanePair(a, b).key,
      )!;
    assert.equal(Boolean(edge[2]), direction === "both");
    const pair = lanePair(g.nodes[edge[0]], g.nodes[edge[1]]);
    assert.equal(pair.start.id, a.id);
    assert.equal(pair.end.id, b.id);
    assert.equal(laneDirections(p).get(pair.key) ?? "both", direction);
    if (direction !== "both")
      assert.equal(g.nodes[edge[0]].id, direction === "forward" ? a.id : b.id);
  }
  assert.equal(JSON.stringify(project(2)), original);
});
test("lane add/delete is deduplicated and clears stale direction rules", () => {
  const p = project(3),
    a = { column: 0, row: 0 },
    b = { column: 3, row: 0 };
  const added = addLane(addLane(p, a, b), b, a);
  assert.equal(added.added_lanes.length, 1);
  const directed = setLaneDirection(added, b, a, "reverse"),
    deleted = deleteLane(directed, a, b);
  assert.equal(deleted.added_lanes.length, 0);
  assert.equal(deleted.one_way_lanes.length, 0);
  assert.equal(deleted.deleted_lanes.length, 1);
  const restored = addLane(deleted, a, b);
  assert.equal(restored.deleted_lanes.length, 0);
  assert.equal(restored.added_lanes.length, 1);
  const g = native({ command: "geometry", project: restored });
  assert.ok(
    g.lanes.some(
      (l: Row) => Math.abs(g.nodes[l[0]].column - g.nodes[l[1]].column) === 3,
    ),
  );
  assert.throws(() => addLane(p, a, a), /different/);
  assert.equal(p.added_lanes, undefined);
});
test("lane edits preserve sparse ASRS storage and physical edits rebuild immutable buffers", () => {
  const p = project(2, 2, 2);
  p.storage_layout.system_type = "Mini-load ASRS";
  const full = syncBuffers(p),
    sparse = {
      ...full,
      storage_layout: {
        ...full.storage_layout,
        buffers: full.storage_layout.buffers.filter(
          (b: Row) => b.level === 1 && b.slot === 1,
        ),
      },
    };
  const before = JSON.stringify(sparse),
    changed = setLaneDirection(
      sparse,
      { column: 0, row: 0 },
      { column: 1, row: 0 },
      "reverse",
    );
  assert.equal(changed.storage_layout.buffers, sparse.storage_layout.buffers);
  assert.equal(
    native({ command: "geometry", project: changed }).summary.capacity,
    2,
  );
  const resized = syncBuffers({
    ...sparse,
    storage_layout: { ...sparse.storage_layout, levels_per_rack: 3 },
  });
  assert.equal(resized.storage_layout.buffers.length, 12);
  assert.equal(resized.storage_layout.slots.length, 12);
  assert.equal(JSON.stringify(sparse), before);
  const moved = syncBuffers({
    ...sparse,
    coordinate_overrides: [{ column: 1, row: 0, x: 8, y: -2 }],
  });
  const cells = moved.storage_layout.slots.filter(
    (s: Row) => s.rack_id === "G1_0",
  );
  assert.ok(cells.every((s: Row) => s.center_y === -2));
  assert.ok(Math.abs((cells[0].center_x + cells[1].center_x) / 2 - 8) < 1e-9);
  const fallback = { ...sparse, machine_carrying_capacity: undefined };
  assert.deepEqual(
    syncBuffers(fallback).storage_layout.machine_carrying_capacity,
    sparse.storage_layout.machine_carrying_capacity,
  );
  const minimal = syncBuffers({ ...p, storage_layout: {} });
  assert.equal(
    native({ command: "geometry", project: minimal }).summary.capacity,
    24,
  );
});
test("stock sizing becomes stale for every physical constraint, but not names or lane directions", () => {
  const p = project(),
    key = stockGeometryKey(p);
  assert.equal(
    stockGeometryKey({ ...p, grid: { ...p.grid, map_name: "Renamed" } }),
    key,
  );
  assert.equal(
    stockGeometryKey(
      setLaneDirection(
        p,
        { column: 0, row: 0 },
        { column: 1, row: 0 },
        "reverse",
      ),
    ),
    key,
  );
  for (const patch of [
    { rack_height_m: 5 },
    { clearance_m: 0.2 },
    { system_type: "Mini-load ASRS" },
    { levels_per_rack: 2 },
    { slots_per_level: 3 },
  ])
    assert.notEqual(
      stockGeometryKey({
        ...p,
        storage_layout: { ...p.storage_layout, ...patch },
      }),
      key,
    );
  assert.notEqual(
    stockGeometryKey({ ...p, grid: { ...p.grid, spacing_m: 2 } }),
    key,
  );
  assert.notEqual(
    stockGeometryKey({
      ...p,
      location_attributes: {
        ...p.location_attributes,
        Z1: { max_item_width: 0.2 },
      },
    }),
    key,
  );
});
test("numeric drafts tolerate clearing and incomplete input without committing zero or NaN", () => {
  for (const value of ["", " ", "-", "1e", "NaN", "Infinity", "1e999", "0"]) {
    assert.equal(numericDraft(value, 1, 64, true), null);
  }
  assert.equal(numericDraft("12", 1, 64, true), 12);
  assert.equal(numericDraft("12.", 1, 64, true), 12);
  assert.equal(numericDraft("1.25", 0.001), 1.25);
  assert.equal(numericDraft("1.25", 1, 64, true), null);
  assert.equal(numericDraft("-2"), -2);
  assert.equal(numericDraft("65", 1, 64), null);
});
test("keyboard navigation skips deleted points without jumping across rows", () => {
  const nodes = [
    { column: 0, row: 0 },
    { column: 2, row: 0 },
    { column: 3, row: 0 },
    { column: 0, row: 2 },
  ];
  assert.equal(nextGridPoint(nodes, nodes[0], 1, 0), nodes[1]);
  assert.equal(nextGridPoint(nodes, nodes[1], -1, 0), nodes[0]);
  assert.equal(nextGridPoint(nodes, nodes[0], 0, 1), nodes[3]);
  assert.equal(nextGridPoint(nodes, nodes[2], 1, 0), undefined);
});
test("new grids and zone IDs reject invalid boundaries before replacing a warehouse", () => {
  validateGrid({ width_m: 9, length_m: 7, spacing_m: 2, spacing_y_m: 3 });
  for (const patch of [
    { width_m: 0 },
    { length_m: -1 },
    { spacing_m: NaN },
    { spacing_y_m: Infinity },
    { width_m: 1e8 },
  ])
    assert.throws(() => validateGrid({ ...project().grid, ...patch }));
  assert.equal(zoneId(" Z99 "), "Z99");
  for (const id of ["", "Z/99", "__proto__", "constructor", "Z\n99"])
    assert.throws(() => zoneId(id));
});
