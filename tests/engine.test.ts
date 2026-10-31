import test from "node:test";
import assert from "node:assert/strict";
import {
  attributeRows,
  compareLayouts,
  mergeRows,
  csvString,
  type Row,
} from "../shared/model.js";

import { physical, project, sku, native } from "./fixtures.js";
test("route validation requires an AMR round trip without passing through another rack", () => {
  const p = project(2, 1, 1);
  p.deleted_positions = [0, 1, 2].map((column) => ({ column, row: 1 }));
  const request = {
    project: p,
    sku_rows: [sku("A", 1, 2, 2)],
    settings: { maximum_same_sku_slots_per_rack: 1 },
  };
  const result = native(request);
  assert.equal(result.validation.complete, true);
  assert.equal(result.validation.unreachable_loads, 1);
  assert.ok(
    result.validation.warnings.some((text: string) => text.includes("reach")),
  );
  const forged = result.assignments.map((r: Row) => ({
    ...r,
    routing_status: "REACHABLE",
  }));
  assert.equal(
    native({ ...request, command: "validate", assignments: forged })
      .unreachable_loads,
    1,
  );
});
test("Basic puts known-weight stock on middle levels, conserves quantities and is deterministic", () => {
  const request = {
    project: project(3, 3, 2),
    sku_rows: [sku("A", 20, 11, 3), sku("B", 10, 2, 1)],
    settings: { maximum_same_sku_slots_per_rack: 2 },
  };
  const a = native(request),
    b = native(request);
  assert.equal(a.summary.assigned_count, 4);
  assert.equal(a.validation.passed, true);
  assert.equal(a.validation.complete, true);
  assert.deepEqual(a.assignments, b.assignments);
  assert.equal(a.assignments[0].storage_level, 2);
  assert.equal(
    a.assignments
      .filter((r: Row) => r.sku === "A")
      .reduce((n: number, r: Row) => n + r.quantity_ea, 0),
    11,
  );
  const counts = new Map();
  for (const r of a.assignments) {
    const key = r.sku + r.rack_id;
    counts.set(key, (counts.get(key) ?? 0) + r.occupied_slot_count);
  }
  assert.ok([...counts.values()].every((v) => v <= 2));
});
test("complete stock is mandatory; no one-slot-per-SKU fallback", () => {
  const data = native(
    {
      project: project(),
      sku_rows: [{ sku: "A", pick_frequency: 1, velocity_class: "A" }],
    },
    true,
  );
  assert.match(data.error, /Stock requirements/);
});
test("hard chilled rules and cumulative rack weight hold under capacity pressure", () => {
  const p = project(2, 1, 2);
  p.machine_carrying_capacity.max_item_weight = 5;
  p.storage_layout.machine_carrying_capacity.max_item_weight = 5;
  p.location_attributes.Z2.chilled = true;
  const data = native({
    project: p,
    sku_rows: [
      sku("ambient", 20, 1, 1, {
        ...physical,
        max_item_weight: 4,
        chilled: false,
      }),
      sku("cold", 10, 1, 1, { ...physical, max_item_weight: 4, chilled: true }),
      sku("extra", 1, 1, 1, {
        ...physical,
        max_item_weight: 4,
        chilled: false,
      }),
    ],
  });
  assert.equal(data.summary.assigned_count, 2);
  assert.equal(data.validation.complete, false);
  assert.equal(
    data.assignments.find((r: Row) => r.sku === "cold").zone_id,
    "Z2",
  );
  assert.ok(data.validation.warnings.length > 0);
});
test("unconfigured SKU flags do not invent map constraints", () => {
  const p = project();
  const data = native({
    project: p,
    sku_rows: [sku("A", 1, 1, 1, { ...physical, tablet: true })],
  });
  assert.equal(data.summary.assigned_count, 1);
});
test("known oversize stock reserves a contiguous multi-cell footprint before normal stock", () => {
  const p = project(2, 2, 2);
  p.warehouse_storage_defaults = {
    max_item_length: 0.5,
    max_item_width: 0.5,
    max_item_height: 0.5,
    max_item_weight: 12.5,
  };
  p.location_attributes.Z1 = {
    ...p.warehouse_storage_defaults,
    max_item_length: 1.5,
    oversize_capable: true,
  };
  p.location_attributes.Z2 = { ...p.warehouse_storage_defaults };
  const data = native({
    project: p,
    sku_rows: [
      sku("normal", 20),
      sku("large", 1, 1, 4, {
        max_item_length: 1.5,
        max_item_width: 1,
        max_item_height: 1,
        max_item_weight: 1,
      }),
    ].map((r) => (r.sku === "large" ? { ...r, slots_per_unit: 4 } : r)),
    settings: { maximum_same_sku_slots_per_rack: 4 },
  });
  const large = data.assignments.find((r: Row) => r.sku === "large");
  assert.equal(large.occupied_slot_count, 4);
  assert.equal(large.occupied_level_span, 2);
  assert.equal(large.occupied_horizontal_slot_span, 2);
  assert.equal(data.validation.passed, true);
  assert.equal(data.summary.assigned_count, 2);
  const addresses = data.assignments.flatMap(
    (r: Row) => r.occupied_storage_location_addresses,
  );
  assert.equal(new Set(addresses).size, 5);
});
test("missing physical data remains unverified, never silently verified", () => {
  const p = project();
  p.location_attributes.Z1.oversize_capable = true;
  p.location_attributes.Z2.oversize_capable = true;
  const data = native({ project: p, sku_rows: [sku("unknown", 1, 1, 1, {})] });
  assert.equal(data.validation.unverified_loads, 1);
  assert.equal(data.validation.physical_fit_verified, false);
  assert.equal(data.assignments[0].compatibility_status, "UNVERIFIED");
});
test("validator rejects overlaps, missing loads, changed quantities and unknown coordinates", () => {
  const request = { project: project(), sku_rows: [sku("A"), sku("B")] };
  const result = native(request);
  const rows = structuredClone(result.assignments);
  rows[1].occupied_handling_units = rows[0].occupied_handling_units;
  rows[1].rack_id = rows[0].rack_id;
  assert.equal(
    native({ ...request, command: "validate", assignments: rows }).passed,
    false,
  );
  assert.equal(
    native({
      ...request,
      command: "validate",
      assignments: result.assignments.slice(1),
    }).passed,
    false,
  );
  const changed = structuredClone(result.assignments);
  changed[0].quantity_ea += 1;
  assert.equal(
    native({ ...request, command: "validate", assignments: changed }).passed,
    false,
  );
});
test("deleted grid points bridge neighbors and directed lanes affect distances", () => {
  const p = project(2);
  p.markers = p.markers.filter((m: Row) => m.column !== 1);
  p.deleted_positions = [{ column: 1, row: 0 }];
  p.storage_layout.buffers = p.storage_layout.buffers.filter(
    (b: Row) => b.grid_waypoint !== "G1_0",
  );
  const geo = native({ command: "geometry", project: p });
  const a = geo.nodes.findIndex((n: Row) => n.id === "G0_0"),
    b = geo.nodes.findIndex((n: Row) => n.id === "G2_0");
  assert.ok(
    geo.lanes.some(
      (l: Row) => [l[0], l[1]].includes(a) && [l[0], l[1]].includes(b),
    ),
  );
  assert.equal(geo.racks[0].distance, 2);
});
test("affinity improves a co-order fixture while preserving all inventory", () => {
  const p = project(2, 1, 2),
    rows = [sku("A", 40), sku("C", 30), sku("B", 20), sku("D", 10)];
  const orders = {
    baskets: [
      { date: "2023-01-01", store: "S", skus: ["A", "B"] },
      { date: "2023-01-01", store: "T", skus: ["C", "D"] },
    ],
  };
  const result = native({
    project: p,
    sku_rows: rows,
    settings: { strategy: "abc_affinity" },
    orders,
  });
  const affinity = result.summary.affinity_pipeline;
  assert.ok(affinity.after.deliveries < affinity.before.deliveries);
  assert.equal(affinity.after.deliveries, 2);
  assert.equal(result.validation.passed, true);
  assert.equal(result.summary.assigned_count, 4);
});
test("multi-station affinity requires complete explicit station mapping", () => {
  const p = project(2);
  p.markers.push({
    column: 0,
    row: 1,
    role: "workstation",
    endpoint_id: "WS2",
  });
  assert.match(
    native(
      {
        project: p,
        sku_rows: [sku("A")],
        settings: { strategy: "abc_affinity" },
        orders: { baskets: [{ date: "2023-01-01", store: "S", skus: ["A"] }] },
      },
      true,
    ).error,
    /explicit/,
  );
});
test("analysis uses line frequency, calendar stock coverage and purchasing-unit rounding", () => {
  const orders = {
    daily: [
      {
        date: "2023-01-01",
        sku: "A",
        quantity: 10,
        frequency: 2,
        conversion: 3,
        requirements: physical,
      },
      {
        date: "2023-01-03",
        sku: "A",
        quantity: 4,
        frequency: 1,
        conversion: 3,
        requirements: physical,
      },
      {
        date: "2023-01-01",
        sku: "B",
        quantity: 0,
        frequency: 1,
        conversion: 1,
        requirements: physical,
      },
    ],
    baskets: [
      { date: "2023-01-01", store: "S", skus: ["A", "B"] },
      { date: "2023-01-03", store: "S", skus: ["A"] },
    ],
  };
  const analysis = native({ command: "analyze", orders });
  assert.equal(analysis.rows[0].pick_frequency, 3);
  assert.equal(analysis.summary.store_day_count, 2);
  assert.equal(analysis.relationships[0].shared_store_days, 1);
  const stock = native({
    command: "stock",
    project: project(),
    orders,
    settings: { minimum_stock_days: 2, buffer_stock_days: 1 },
  });
  const a = stock.rows.find((r: Row) => r.sku === "A");
  assert.equal(a.observation_days, 3);
  assert.equal(a.minimum_stock_ea, 10);
  assert.equal(a.total_required_ea, 14);
  assert.equal(a.total_slotted_ea, 15);
  assert.equal(a.total_required_uom, 5);
});
test("boundary validation rejects malformed dimensions, invalid dates and noninteger limits", () => {
  assert.match(
    native(
      {
        project: {
          ...project(),
          grid: { width_m: -1, length_m: 1, spacing_m: 1 },
        },
        sku_rows: [sku("A")],
      },
      true,
    ).error,
    /positive/,
  );
  assert.match(
    native(
      {
        project: project(),
        sku_rows: [sku("A")],
        settings: { maximum_same_sku_slots_per_rack: 1.5 },
      },
      true,
    ).error,
    /whole number/,
  );
  assert.match(
    native(
      {
        command: "analyze",
        orders: { daily: [{ date: "2023-02-30", sku: "A", quantity: 1 }] },
      },
      true,
    ).error,
    /calendar date/,
  );
});
test("CSV attribute merge rejects conflicts and duplicates", () => {
  const attrs = attributeRows([{ sku: "A", length: "0.1", chilled: "true" }]);
  assert.deepEqual({ ...attrs.A }, { max_item_length: 0.1, chilled: true });
  assert.throws(() => attributeRows([{ sku: "A" }, { sku: "A" }]), /duplicate/);
  assert.throws(
    () =>
      mergeRows(
        [
          {
            sku: "A",
            pick_frequency: 1,
            velocity_class: "A",
            req_max_item_length: 2,
          },
        ],
        [],
        attrs,
      ),
    /Conflicting/,
  );
  assert.ok(csvString([{ sku: '=HYPERLINK("bad")' }]).includes("'=HYPERLINK"));
});
test("comparison distinguishes inventory equality from placement identity and mismatched inputs", () => {
  const request = { project: project(), sku_rows: [sku("A"), sku("B")] };
  const a = native(request),
    b = structuredClone(a);
  b.assignments[0].storage_location_address = "different";
  b.summary.maximum_same_sku_slots_per_rack = 1;
  const comparison = compareLayouts(a, b);
  assert.equal(comparison.inventory_quantities_equal, true);
  assert.equal(comparison.identical_placements, 1);
  assert.equal(comparison.parameters_match, false);
});

test("stock imports preserve demand and physical requirements; unused attribute profiles support date filtering", () => {
  const rows = mergeRows(
    [
      {
        sku: "A",
        pick_frequency: 4,
        velocity_class: "A",
        sku_requirements: physical,
      },
    ],
    [
      {
        sku: "A",
        total_required_ea: 5,
        total_slotted_ea: 5,
        required_slots: 1,
        sku_requirements: {},
        pick_frequency: 999,
      },
    ],
    { B: { chilled: true } },
  );
  assert.equal(rows[0].pick_frequency, 4);
  assert.deepEqual(rows[0].sku_requirements, physical);
  assert.equal(rows[0].total_slotted_ea, 5);
});
test("signed quantities remain visible in velocity; stock ignores returns and supports fractional UOM", () => {
  const orders = {
    daily: [
      {
        date: "2023-01-01",
        sku: "A",
        quantity: 2,
        raw_quantity: -2,
        frequency: 1,
        conversion: 0.5,
        requirements: physical,
      },
    ],
    baskets: [{ date: "2023-01-01", store: "S", skus: ["A"] }],
  };
  assert.equal(
    native({ command: "analyze", orders }).rows[0].total_quantity_ea,
    -2,
  );
  const result = native({
    command: "stock",
    orders,
    project: project(),
    settings: { minimum_stock_days: 1, buffer_stock_days: 0 },
  });
  assert.equal(result.rows[0].uom_conversion_qty, 0.5);
  assert.equal(result.rows[0].total_slotted_ea, 2);
});
test("explicitly missing attribute dimensions remain unverified during stock sizing", () => {
  const result = native({
    command: "stock",
    project: project(),
    orders: {
      daily: [
        {
          date: "2023-01-01",
          sku: "A",
          quantity: 5,
          frequency: 1,
          conversion: 1,
          requirements: physical,
        },
      ],
    },
    attributes: { A: {} },
    settings: { minimum_stock_days: 1, buffer_stock_days: 0 },
  });
  assert.equal(result.rows[0].units_per_slot, 1);
  assert.equal(result.rows[0].required_slots, 5);
  assert.match(result.rows[0].rack_calculation_status, /UNVERIFIED/);
});
test("ASRS scores individual tote presentations and respects partial configured buffers", () => {
  const p = project(1, 1, 2);
  p.storage_layout.system_type = "Mini-load ASRS";
  p.storage_layout.handling_unit_type = "Tote";
  p.storage_layout.buffer_level = "slot";
  p.storage_layout.buffers = [1, 2].map((slot) => ({
    buffer_id: `B-G1_0/L01/S0${slot}`,
    grid_waypoint: "G1_0",
    buffer_level: "slot",
    status: "EMPTY",
    level: 1,
    slot,
  }));
  const result = native({
    project: p,
    sku_rows: [sku("A"), sku("B")],
    settings: { strategy: "abc_affinity" },
    orders: { baskets: [{ date: "2023-01-01", store: "S", skus: ["A", "B"] }] },
  });
  assert.equal(result.summary.affinity_pipeline.after.deliveries, 2);
  assert.match(result.summary.affinity_pipeline.after.model, /single_sku_tote/);
  p.storage_layout.buffers.pop();
  assert.equal(native({ project: p, command: "geometry" }).summary.capacity, 1);
});
test("extreme inputs fail safely before integer conversion; duplicate and occupied buffers are rejected", () => {
  assert.match(
    native(
      {
        project: project(),
        sku_rows: [{ ...sku("A"), required_slots: 1e100 }],
      },
      true,
    ).error,
    /limits/,
  );
  const p = project();
  p.storage_layout.buffers.push(p.storage_layout.buffers[0]);
  assert.match(
    native({ project: p, command: "geometry" }, true).error,
    /unique/,
  );
  p.storage_layout.buffers.pop();
  p.storage_layout.buffers[0].status = "OCCUPIED";
  assert.match(
    native({ project: p, command: "geometry" }, true).error,
    /EMPTY/,
  );
});
