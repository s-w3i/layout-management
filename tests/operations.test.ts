import test from "node:test";
import assert from "node:assert/strict";
import {
  findInventory,
  scenarioRows,
  movementColor,
} from "../shared/operations.js";
import { native, physical, project, sku } from "./fixtures.js";
import type { Row } from "../shared/model.js";

const located = (
  sku: string,
  rack: string,
  unit: string,
  distance = 1,
  quantity = 1,
): Row => ({
  sku,
  rack_id: rack,
  handling_unit_id: unit,
  average_workstation_distance_m: distance,
  quantity_ea: quantity,
  assignment_status: "ASSIGNED",
  storage_level: 1,
  storage_slot: 1,
});
const basket = (store: string, skus: string[], date = "2023-01-01") => ({
  date,
  store,
  skus,
});

test("AMR movement counts one visit per store/day and chooses maximum SKU coverage", () => {
  const result = native({
    command: "movement",
    assignments: [
      located("A", "R1", "S1"),
      located("B", "R1", "S1"),
      located("A", "R2", "S2", 2),
      located("C", "R2", "S2", 2),
    ],
    orders: {
      baskets: [
        basket("1", ["A", "B", "A"]),
        basket("1", ["A"]),
        basket("2", ["A", "C"]),
        basket("3", ["B"]),
      ],
    },
  });
  assert.equal(result.fulfillment_group_count, 3);
  assert.equal(result.total_handling_unit_visits, 3);
  assert.deepEqual(result.rack_visit_counts, { R1: 2, R2: 1 });
  assert.equal(result.estimate, true);
});

test("movement ties use mean distance then rack ID; classes use previous cumulative share", () => {
  const rows = [
    located("A", "R1", "S1", 2),
    located("A", "R2", "S2", 1),
    located("B", "R3", "S3"),
    located("C", "R4", "S4"),
  ];
  const orders = {
    baskets: Array.from({ length: 100 }, (_, i) =>
      basket(String(i).padStart(3, "0"), [i < 80 ? "A" : i < 95 ? "B" : "C"]),
    ),
  };
  const result = native({ command: "movement", assignments: rows, orders });
  assert.deepEqual(
    result.units.map((unit: Row) => [
      unit.rack_id,
      unit.visit_count,
      unit.movement_class,
    ]),
    [
      ["R2", 80, "A"],
      ["R3", 15, "B"],
      ["R4", 5, "C"],
    ],
  );
  rows[0].average_workstation_distance_m = 1;
  assert.equal(
    native({ command: "movement", assignments: rows, orders }).units[0].rack_id,
    "R1",
  );
});

test("slot movement distributes duplicate SKU sources by quantity and counts each occupied unit once", () => {
  const first = located("A", "R1", "T1", 1, 3);
  first.occupied_handling_units = [
    {
      rack_id: "R1",
      handling_unit_id: "T1",
      storage_level: 1,
      storage_slot: 1,
    },
    {
      rack_id: "R1",
      handling_unit_id: "T1B",
      storage_level: 1,
      storage_slot: 2,
    },
  ];
  const result = native({
    command: "movement",
    handling_unit_type: "Tote",
    assignments: [first, located("A", "R2", "T2")],
    orders: {
      baskets: Array.from({ length: 4 }, (_, i) => basket(String(i), ["A"])),
    },
  });
  assert.deepEqual(
    result.units.map((unit: Row) => [unit.handling_unit_id, unit.visit_count]),
    [
      ["T1", 3],
      ["T1B", 3],
      ["T2", 1],
    ],
  );
  assert.deepEqual(result.rack_visit_counts, { R1: 6, R2: 1 });
  assert.equal(result.ranking_level, "slot");
});

test("movement filters dates and reports missing assigned inventory", () => {
  const request = {
    command: "movement",
    assignments: [located("A", "R1", "S1")],
    settings: { start_date: "2023-01-02", end_date: "2023-01-02" },
    orders: {
      baskets: [
        basket("1", ["A"]),
        basket("1", ["A", "missing"], "2023-01-02"),
      ],
    },
  };
  const result = native(request);
  assert.equal(result.fulfillment_group_count, 1);
  assert.equal(result.total_handling_unit_visits, 1);
  assert.equal(result.groups_with_unassigned_skus, 1);
  assert.match(
    native({ ...request, orders: { baskets: [] } }, true).error,
    /No store\/day/,
  );
});

const setup = () => {
  const request = {
    project: project(3, 1, 2),
    sku_rows: [sku("A", 10, 2, 2), sku("B", 5, 2, 2)],
    settings: { strategy: "basic", maximum_same_sku_slots_per_rack: 2 },
  };
  return { request, layout: native(request) };
};
test("single-load swaps preserve identity, stock and shelf identity at the destination", () => {
  const { request, layout } = setup();
  const before = JSON.stringify(layout);
  const a = layout.assignments[0],
    b = layout.assignments[2];
  const result = native({
    ...request,
    command: "swap",
    layout,
    mode: "load",
    source: a.inventory_load_id,
    target: b.inventory_load_id,
  });
  assert.equal(
    result.assignments[0].storage_location_address,
    b.storage_location_address,
  );
  assert.equal(result.assignments[0].handling_unit_id, b.handling_unit_id);
  assert.deepEqual(
    result.assignments.map((row: Row) => [
      row.inventory_load_id,
      row.quantity_ea,
    ]),
    layout.assignments.map((row: Row) => [
      row.inventory_load_id,
      row.quantity_ea,
    ]),
  );
  assert.equal(result.validation.passed, true);
  assert.equal(result.operation_log.length, 1);
  assert.equal(JSON.stringify(layout), before);
  assert.equal(
    native({ ...request, command: "validate", assignments: result.assignments })
      .passed,
    true,
  );
});
test("whole-shelf swaps retain mobile identities and survive a following SKU swap", () => {
  const { request, layout } = setup();
  const a = layout.assignments[0],
    b = layout.assignments[2];
  const result = native({
    ...request,
    command: "swap",
    layout,
    mode: "shelf",
    source: a.rack_id,
    target: b.rack_id,
  });
  assert.equal(result.assignments[0].rack_id, b.rack_id);
  assert.equal(result.assignments[0].handling_unit_id, a.handling_unit_id);
  assert.equal(result.assignments[0].dynamic_address, a.dynamic_address);
  const next = native({
    ...request,
    command: "swap",
    layout: result,
    mode: "load",
    source: a.inventory_load_id,
    target: b.inventory_load_id,
  });
  assert.equal(next.assignments[0].rack_id, a.rack_id);
  assert.equal(next.assignments[0].handling_unit_id, b.handling_unit_id);
  assert.equal(next.assignments[1].handling_unit_id, a.handling_unit_id);
  assert.equal(next.operation_log.length, 2);
  assert.equal(next.validation.passed, true);
  const reopened = JSON.parse(JSON.stringify(next));
  assert.equal(
    native({
      command: "validate",
      project: reopened.project,
      settings: reopened.settings,
      sku_rows: scenarioRows(reopened),
      assignments: reopened.assignments,
    }).passed,
    true,
  );
});
test("invalid trial swaps are rejected for attributes, weight and duplicate selection", () => {
  const { request, layout } = setup();
  const a = layout.assignments[0],
    b = layout.assignments[2];
  const swap = {
    command: "swap",
    layout,
    mode: "load",
    source: a.inventory_load_id,
    target: b.inventory_load_id,
  };
  assert.match(
    native({ ...request, ...swap, target: a.inventory_load_id }, true).error,
    /different/,
  );
  const chilled = structuredClone(request);
  chilled.project.location_attributes.Z1.chilled = true;
  chilled.sku_rows[0].sku_requirements = { ...physical, chilled: true };
  chilled.sku_rows[1].sku_requirements = { ...physical, chilled: false };
  const constrained = native(chilled);
  assert.match(
    native({ ...chilled, ...swap, layout: constrained }, true).error,
    /constraints failed/,
  );
  const heavy = structuredClone(request);
  heavy.project.machine_carrying_capacity.max_item_weight = 10;
  heavy.project.storage_layout.machine_carrying_capacity.max_item_weight = 10;
  heavy.sku_rows = [
    sku("A", 10, 1, 1, { ...physical, max_item_weight: 9 }),
    sku("B", 5, 2, 2, { ...physical, max_item_weight: 5 }),
  ];
  const weighted = native(heavy);
  const target = weighted.assignments.find((row: Row) => row.sku === "B");
  assert.match(
    native(
      {
        ...heavy,
        ...swap,
        layout: weighted,
        source: weighted.assignments[0].inventory_load_id,
        target: target.inventory_load_id,
      },
      true,
    ).error,
    /Whole-rack weight/,
  );
});
test("whole-shelf swaps preserve multi-cell footprints; individual multi-cell swaps are rejected", () => {
  const p = project(2, 2, 2);
  p.warehouse_storage_defaults = {
    max_item_length: 0.5,
    max_item_width: 0.5,
    max_item_height: 0.5,
    max_item_weight: 12.5,
  };
  p.location_attributes.Z1 = p.location_attributes.Z2 = {
    ...p.warehouse_storage_defaults,
    max_item_length: 1.5,
    oversize_capable: true,
  };
  const requirements = {
    max_item_length: 1.5,
    max_item_width: 1,
    max_item_height: 1,
    max_item_weight: 1,
  };
  const request = {
    project: p,
    sku_rows: [
      sku("A", 2, 1, 4, requirements),
      sku("B", 1, 1, 4, requirements),
    ].map((row) => ({ ...row, slots_per_unit: 4 })),
    settings: { maximum_same_sku_slots_per_rack: 4 },
  };
  const layout = native(request);
  assert.equal(layout.summary.assigned_count, 2);
  assert.equal(layout.assignments[0].occupied_slot_count, 4);
  const [a, b] = layout.assignments;
  assert.match(
    native(
      {
        ...request,
        command: "swap",
        layout,
        mode: "load",
        source: a.inventory_load_id,
        target: b.inventory_load_id,
      },
      true,
    ).error,
    /single-cell/,
  );
  const moved = native({
    ...request,
    command: "swap",
    layout,
    mode: "shelf",
    source: a.rack_id,
    target: b.rack_id,
  });
  assert.equal(moved.validation.passed, true);
  assert.equal(moved.assignments[0].occupied_handling_units.length, 4);
  assert.equal(moved.assignments[0].rack_id, b.rack_id);
  assert.equal(moved.assignments[0].handling_unit_id, a.handling_unit_id);
});
test("scenario reconstruction and SKU lookup preserve duplicate load identities", () => {
  const { layout } = setup();
  assert.equal(scenarioRows(layout).length, 2);
  assert.equal(scenarioRows(layout)[0].required_slots, 2);
  assert.equal(findInventory(layout.assignments, "a").length, 2);
  assert.deepEqual(findInventory([{ sku: "A" }, { sku: "AA" }], "A"), [
    { sku: "A" },
  ]);
  assert.throws(
    () => scenarioRows({ assignments: [{ sku: "A" }] }),
    /stock targets/,
  );
  assert.equal(movementColor(0, 10), "#e5e9ee");
  assert.notEqual(movementColor(1, 10), movementColor(10, 10));
});
