import test from "node:test";
import assert from "node:assert/strict";
import {
  dateRangeError,
  initialDateRange,
  orderDateBounds,
} from "../shared/dates.js";

const bounds = { date_start: "2023-01-03", date_end: "2023-07-31" };

test("date ranges accept the imported endpoints and dates between them", () => {
  assert.equal(
    dateRangeError(
      { start_date: bounds.date_start, end_date: bounds.date_end },
      bounds,
    ),
    "",
  );
  assert.equal(
    dateRangeError(
      { start_date: "2023-03-01", end_date: "2023-03-01" },
      bounds,
    ),
    "",
  );
  assert.equal(dateRangeError({}, bounds), "");
  assert.equal(dateRangeError({ start_date: "", end_date: "" }, bounds), "");
});

test("out-of-data, reversed and impossible dates are rejected", () => {
  assert.match(
    dateRangeError({ start_date: "2023-01-02" }, bounds),
    /between 2023-01-03 and 2023-07-31/,
  );
  assert.match(dateRangeError({ end_date: "2023-08-01" }, bounds), /between/);
  assert.match(
    dateRangeError(
      { start_date: "2023-04-02", end_date: "2023-04-01" },
      bounds,
    ),
    /on or before/,
  );
  for (const date of ["2023-02-29", "2023-02-30", "2023-13-01", "03/04/2023"]) {
    assert.match(
      dateRangeError({ start_date: date }, bounds),
      /valid calendar date/,
    );
  }
  assert.equal(
    dateRangeError(
      { start_date: "2024-02-29" },
      { date_start: "2024-02-01", date_end: "2024-03-01" },
    ),
    "",
  );
});

test("a new order file resets incompatible movement dates to its full valid range", () => {
  assert.deepEqual(initialDateRange({}, bounds), {
    start_date: bounds.date_start,
    end_date: bounds.date_end,
  });
  assert.deepEqual(
    initialDateRange(
      { start_date: "2022-01-01", end_date: "2022-03-01" },
      bounds,
    ),
    { start_date: bounds.date_start, end_date: bounds.date_end },
  );
  const chosen = { start_date: "2023-04-01", end_date: "2023-04-03" };
  assert.deepEqual(initialDateRange(chosen, bounds), chosen);
  const replacement = { date_start: "2024-02-01", date_end: "2024-02-29" };
  assert.deepEqual(initialDateRange(chosen, replacement), {
    start_date: "2024-02-01",
    end_date: "2024-02-29",
  });
});

test("API bounds come from actual order rows rather than supplied metadata", () => {
  const data = {
    date_start: "2000-01-01",
    date_end: "2099-12-31",
    daily: [{ date: "2023-07-31" }, { date: "2023-01-03" }],
    baskets: [{ date: "2023-04-02" }],
  };
  assert.deepEqual(orderDateBounds(data), bounds);
  assert.throws(() => orderDateBounds({}), /no valid dates/);
  assert.throws(
    () => orderDateBounds({ baskets: [{ date: "2023-02-29" }] }),
    /Invalid calendar date/,
  );
});
