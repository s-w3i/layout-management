import test from "node:test";
import assert from "node:assert/strict";
import {
  filterTableRows,
  demandFilters,
  inventoryFilters,
  stockFilters,
  stockTableRows,
  sortTableRows,
} from "../shared/table.js";

const rows = [
  {
    sku: "SKU10",
    velocity_class: "A",
    quantity_ea: 100,
    inventory_load_weight_kg: 12.5,
    assignment_status: "ASSIGNED",
    compatibility_status: "UNVERIFIED",
    storage_location_address: "R10",
  },
  {
    sku: "SKU2",
    velocity_class: "B",
    quantity_ea: 9,
    inventory_load_weight_kg: 2,
    assignment_status: "ASSIGNED",
    compatibility_status: "COMPATIBLE",
    storage_location_address: "R2",
  },
  {
    sku: "SKU3",
    velocity_class: "C",
    quantity_ea: 25,
    inventory_load_weight_kg: null,
    assignment_status: "UNASSIGNED_NO_CAPACITY",
    compatibility_status: "NOT_EVALUATED",
    storage_location_address: "",
  },
  {
    sku: "SKU4",
    velocity_class: "A",
    quantity_ea: 4,
    inventory_load_weight_kg: 0,
    assignment_status: "UNASSIGNED_NO_COMPATIBLE_LOCATION",
    compatibility_status: "NOT_EVALUATED",
    storage_location_address: null,
  },
];
const scoped = (selections: Record<string, string>, query = "") =>
  filterTableRows(rows, query, selections, inventoryFilters);

test("stock CSV rows use current demand classes without changing saved stock", () => {
  const stock = [
    { sku: " 10 ", total_required_ea: "2.5" },
    { sku: "20", velocity_class: "B" },
    { sku: "30", velocity_class: "C" },
    { sku: "unknown" },
  ];
  const before = JSON.stringify(stock);
  const display = stockTableRows(stock, [
    { sku: 10, velocity_class: "A" },
    { sku: "20", velocity_class: "C" },
    { sku: "demand-only", velocity_class: "B" },
  ]);
  assert.deepEqual(
    display.map((row) => row.velocity_class),
    ["A", "C", "C", ""],
  );
  assert.equal(display.length, stock.length);
  assert.equal(display[0].total_required_ea, "2.5");
  assert.deepEqual(
    filterTableRows(
      display,
      "",
      { velocity_class: "A" },
      stockFilters(display),
    ),
    [display[0]],
  );
  assert.equal(JSON.stringify(stock), before);
});

test("demand ABC filters combine with SKU search and sorting", () => {
  const matches = filterTableRows(
    rows,
    "sku",
    { velocity_class: "A" },
    demandFilters,
  );
  assert.deepEqual(
    sortTableRows(matches, { key: "quantity_ea", direction: "ascending" }),
    [rows[3], rows[0]],
  );
  assert.deepEqual(
    filterTableRows(rows, "sku2", { velocity_class: "A" }, demandFilters),
    [],
  );
  assert.deepEqual(filterTableRows(rows, "", {}, demandFilters), rows);
});

test("stock ABC and physical-data filters support every available calculation status", () => {
  const statuses = [
    "CALCULATED",
    "CALCULATED_MULTI_SLOT",
    "CALCULATED_UNVERIFIED_DIMENSIONS",
    "CALCULATED_UNVERIFIED_PHYSICAL",
    "CALCULATED_FEASIBLE_RACK_UPPER_BOUND",
    "ITEM_DOES_NOT_FIT",
    "IMPORTED_STATUS",
  ];
  const stock = statuses.map((status, index) => ({
    sku: `SKU${index}`,
    velocity_class: index % 2 ? "C" : "A",
    rack_calculation_status: status,
  }));
  const filters = stockFilters([...stock, stock[0], { sku: "UNKNOWN" }]);
  assert.deepEqual(
    filters[1].options.map((option) => option.value),
    [...statuses].sort(),
  );
  for (const status of statuses) {
    assert.deepEqual(
      filterTableRows(stock, "", { rack_calculation_status: status }, filters),
      stock.filter((row) => row.rack_calculation_status === status),
    );
  }
  assert.deepEqual(
    filterTableRows(
      stock,
      "sku3",
      {
        velocity_class: "C",
        rack_calculation_status: "CALCULATED_UNVERIFIED_PHYSICAL",
      },
      filters,
    ),
    [stock[3]],
  );
  assert.deepEqual(
    filterTableRows(
      stock,
      "",
      {
        velocity_class: "A",
        rack_calculation_status: "CALCULATED_UNVERIFIED_PHYSICAL",
      },
      filters,
    ),
    [],
  );
  assert.deepEqual(filterTableRows(stock, "", {}, filters), stock);
});

test("inventory filters combine ABC, assignment, physical status and search", () => {
  for (const abc of ["A", "B", "C"]) {
    assert.ok(
      scoped({ velocity_class: abc }).every(
        (row) => row.velocity_class === abc,
      ),
    );
  }
  assert.deepEqual(
    scoped(
      {
        velocity_class: "A",
        assignment_status: "ASSIGNED",
        compatibility_status: "UNVERIFIED",
      },
      " sku10 ",
    ),
    [rows[0]],
  );
  assert.deepEqual(
    scoped({ velocity_class: "A", compatibility_status: "COMPATIBLE" }),
    [],
  );
  assert.deepEqual(
    scoped({ compatibility_status: "NOT_EVALUATED" }),
    rows.slice(2),
  );
  assert.deepEqual(scoped({}), rows);
});

test("Unassigned includes every failure reason; assigned unverified loads stay assigned", () => {
  assert.deepEqual(scoped({ assignment_status: "UNASSIGNED" }), rows.slice(2));
  assert.deepEqual(scoped({ assignment_status: "ASSIGNED" }), rows.slice(0, 2));
  assert.deepEqual(scoped({ compatibility_status: "UNVERIFIED" }), [rows[0]]);
});

test("all seven inventory columns sort in both directions without changing source data", () => {
  const original = JSON.stringify(rows);
  const expected: Record<string, [number[], number[]]> = {
    sku: [
      [1, 2, 3, 0],
      [0, 3, 2, 1],
    ],
    quantity_ea: [
      [3, 1, 2, 0],
      [0, 2, 1, 3],
    ],
    velocity_class: [
      [0, 3, 1, 2],
      [2, 1, 0, 3],
    ],
    storage_location_address: [
      [1, 0, 2, 3],
      [0, 1, 2, 3],
    ],
    inventory_load_weight_kg: [
      [3, 1, 0, 2],
      [0, 1, 3, 2],
    ],
    assignment_status: [
      [0, 1, 2, 3],
      [3, 2, 0, 1],
    ],
    compatibility_status: [
      [1, 2, 3, 0],
      [0, 2, 3, 1],
    ],
  };
  for (const [key, [ascending, descending]] of Object.entries(expected)) {
    assert.deepEqual(
      sortTableRows(rows, { key, direction: "ascending" }),
      ascending.map((i) => rows[i]),
      key,
    );
    assert.deepEqual(
      sortTableRows(rows, { key, direction: "descending" }),
      descending.map((i) => rows[i]),
      key,
    );
  }
  assert.equal(JSON.stringify(rows), original);
  assert.equal(sortTableRows(rows, null), rows);
});

test("sort applies to the entire filtered result before selecting a page", () => {
  const loads = Array.from({ length: 60 }, (_, i) => ({
    ...rows[0],
    quantity_ea: i + 1,
  }));
  const sorted = sortTableRows(
    filterTableRows(loads, "", { velocity_class: "A" }, inventoryFilters),
    { key: "quantity_ea", direction: "descending" },
  );
  assert.equal(sorted[0].quantity_ea, 60);
  assert.equal(sorted.slice(25, 50)[0].quantity_ea, 35);
});

test("CSV numeric text sorts decimals and negative quantities numerically", () => {
  const quantities = ["10.1", "2.5", "2.15", "-1", "0002", "-2", null].map(
    (value) => ({ value }),
  );
  assert.deepEqual(
    sortTableRows(quantities, { key: "value", direction: "ascending" }).map(
      (row) => row.value,
    ),
    ["-2", "-1", "0002", "2.15", "2.5", "10.1", null],
  );
  assert.deepEqual(
    sortTableRows(quantities, { key: "value", direction: "descending" }).map(
      (row) => row.value,
    ),
    ["10.1", "2.5", "2.15", "0002", "-1", "-2", null],
  );
  const codes = [
    { sku: "10000000000000000002" },
    { sku: "10000000000000000001" },
  ];
  assert.deepEqual(
    sortTableRows(codes, { key: "sku", direction: "ascending" }),
    [codes[1], codes[0]],
  );
});
