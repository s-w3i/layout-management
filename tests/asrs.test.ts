import test from "node:test";
import assert from "node:assert/strict";
import { AsrsEngine, type SimulationSink } from "../server/asrs-engine.js";
import {
  asrsSettings,
  axisDistance,
  axisDuration,
  phasePosition,
  simulationDates,
  buildAsrsModel,
  type AsrsModel,
  type SimulationOrder,
} from "../shared/asrs.js";
import { AsrsDatabase } from "../server/asrs-database.js";
import { exactQuantity } from "../shared/inventory.js";
import type { Row } from "../shared/model.js";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const U = 1_000_000;
function model(): AsrsModel {
  return {
    cranes: [
      { id: "ASRS_01", workstation: "WS", home: [10, 1, 0.5], rows: [0, 2] },
    ],
    loads: [
      {
        id: "A#1",
        sku: "A",
        address: "A/L01/S01",
        rack: "G0_0",
        crane: "ASRS_01",
        position: [0, 0, 0.5],
        initial: 10 * U,
      },
    ],
    buffers: { A: 3 * U },
    warnings: [],
  };
}
function line(
  row: number,
  quantity: number,
  date = "2023-01-03",
  sku = "A",
  store = "S",
): SimulationOrder {
  return {
    source_row: row,
    date,
    sku,
    store,
    quantity_micro: Math.round(quantity * U),
    source_time: "08:00:00",
    uom: "EA",
    order_reference: `ORD-${row}`,
  };
}
function fixture(
  m = model(),
  orders = [line(2, 3), line(3, 5), line(4, 9)],
  dates = ["2023-01-03"],
) {
  const events: Row[] = [],
    cycles: Row[] = [],
    status = new Map<number, Row>();
  const sink: SimulationSink = {
    event: (r) => events.push(structuredClone(r)),
    cycle: (r) => cycles.push(structuredClone(r)),
    line: (row, q, state, shortage = 0) =>
      status.set(row, {
        quantity: (status.get(row)?.quantity || 0) + q,
        status: state,
        shortage,
      }),
  };
  const settings = asrsSettings({
    speed: "max",
    replenishment_lead_seconds: 60,
  });
  const create = (checkpoint?: Row) =>
    new AsrsEngine(
      m,
      dates,
      settings,
      (date) => orders.filter((l) => l.date === date),
      sink,
      checkpoint,
    );
  return { events, cycles, status, create };
}
function finish(engine: AsrsEngine) {
  let events = 0;
  while (!engine.finished) {
    engine.step();
    assert.ok(++events < 100000, "Scheduler failed to make progress");
    engine.snapshot();
  }
  return engine;
}

test("ASRS date modes accept only observed dates and months, including discontinuous selections", () => {
  const dates = ["2023-01-03", "2023-01-05", "2023-02-01", "2023-03-02"];
  assert.deepEqual(
    simulationDates(dates, { mode: "one_date", dates: [dates[1]] }),
    [dates[1]],
  );
  assert.deepEqual(
    simulationDates(dates, {
      mode: "multiple_dates",
      dates: [dates[3], dates[0], dates[3]],
    }),
    [dates[0], dates[3]],
  );
  assert.deepEqual(
    simulationDates(dates, { mode: "one_month", months: ["2023-01"] }),
    dates.slice(0, 2),
  );
  assert.deepEqual(
    simulationDates(dates, {
      mode: "multiple_months",
      months: ["2023-01", "2023-03"],
    }),
    [dates[0], dates[1], dates[3]],
  );
  assert.deepEqual(simulationDates(dates, { mode: "full" }), dates);
  assert.throws(
    () => simulationDates(dates, { mode: "one_date", dates: ["2023-01-04"] }),
    /present/,
  );
  assert.throws(
    () =>
      simulationDates(dates, { mode: "one_date", dates: dates.slice(0, 2) }),
    /exactly one/,
  );
  assert.throws(
    () => simulationDates(dates, { mode: "one_month", months: ["2023-04"] }),
    /present/,
  );
  assert.throws(
    () => simulationDates(dates, { mode: "multiple_dates", dates: [] }),
    /at least/,
  );
});
test("ASRS analytical acceleration, concurrent axes and fork interlocks match the original cycle", () => {
  const axis = { speed: 3, acceleration: 1 };
  assert.equal(axisDuration(4, axis), 4);
  assert.equal(axisDuration(12, axis), 7);
  assert.equal(axisDistance(12, 3, axis), 4.5);
  assert.equal(axisDistance(12, 7, axis), 12);
  const m = model();
  m.buffers.A = 0;
  const f = fixture(m, [line(2, 1)]),
    engine = finish(f.create()),
    cycle = f.cycles[0];
  assert.ok(Math.abs(engine.now - 41) < 1e-9);
  for (const phase of cycle.phases) {
    const y = phase.origin[1] !== phase.target[1];
    if (y) {
      assert.equal(phase.origin[0], phase.target[0]);
      assert.equal(phase.origin[2], phase.target[2]);
    } else assert.equal(phase.origin[1], phase.target[1]);
    assert.deepEqual(
      phasePosition(phase, phase.end, engine.settings.axes),
      phase.target,
    );
  }
  assert.equal(f.status.get(2)?.status, "completed");
  assert.equal(f.status.get(2)?.quantity, U);
});
test("quantity depletion splits a large order without negative stock, coalesces replenishment and preserves exact flow", () => {
  const f = fixture(),
    engine = finish(f.create());
  assert.equal(engine.totals.outbound, 17 * U);
  assert.equal(engine.totals.inbound, 10 * U);
  assert.equal(engine.loads["A#1"].quantity, 3 * U);
  assert.equal(engine.totals.completed, 3);
  assert.equal(engine.totals.replenishments, 1);
  assert.equal(f.cycles.length, 3);
  assert.equal(
    f.events.filter((e) => e.kind === "REPLENISH_REQUEST").length,
    1,
  );
  assert.deepEqual(
    [...f.status.values()].map((v) => v.quantity),
    [3 * U, 5 * U, 9 * U],
  );
  let q = 0;
  for (const event of f.events) {
    q += event.load_delta_micro;
    assert.equal(event.load_balance_micro, q);
    assert.ok(q >= 0 && q <= 10 * U);
  }
  const refill = f.events.find((e) => e.kind === "REPLENISH_IN")!,
    request = f.events.find((e) => e.kind === "REPLENISH_REQUEST")!;
  assert.ok(refill.sim_seconds >= request.sim_seconds + 60);
  assert.equal(refill.load_balance_micro, 10 * U);
  assert.equal(
    f.events.reduce((n, e) => n + e.delta_micro, 0),
    engine.snapshot().onhand_micro,
  );
  assert.ok(
    f.events
      .filter((e) => e.kind === "ORDER_OUT")
      .every((e) => e.order_reference && e.source_row && e.crane && e.cycle_id),
  );
});
test("buffer threshold is SKU-wide and strict; other assigned stock is used before emergency purchase", () => {
  const m = model();
  m.loads.push({
    ...m.loads[0],
    id: "A#2",
    address: "A/L01/S02",
    position: [1, 2, 0.5],
  });
  m.buffers.A = 0;
  const f = fixture(m, [line(2, 15)]),
    engine = finish(f.create());
  assert.equal(engine.totals.inbound, 0);
  assert.equal(engine.totals.outbound, 15 * U);
  assert.equal(engine.storage.A, 5 * U);
  assert.equal(
    f.events.filter((e) => e.kind === "REPLENISH_REQUEST").length,
    0,
  );
  const strict = fixture(model(), [line(2, 7)]);
  finish(strict.create());
  assert.equal(
    strict.events.filter((e) => e.kind === "REPLENISH_REQUEST").length,
    0,
  );
});
test("returns, zero lines and missing SKU demand remain traceable; returned stock is used before external supply", () => {
  const orders = [
    line(2, -4),
    line(3, 0),
    line(4, 1, "2023-01-03", "MISSING"),
    line(5, 12),
  ];
  const f = fixture(model(), orders),
    engine = finish(f.create());
  assert.equal(engine.totals.returned, 1);
  assert.equal(engine.totals.zero, 1);
  assert.equal(engine.totals.unfulfilled, 1);
  assert.equal(engine.totals.outbound, 12 * U);
  assert.equal(engine.totals.return_quantity, 4 * U);
  assert.equal(engine.totals.inbound, 6 * U);
  assert.equal(engine.snapshot().onhand_micro, 8 * U);
  assert.equal(f.status.get(4)?.shortage, U);
  assert.equal(f.status.get(2)?.status, "returned");
  assert.equal(
    f.events
      .filter((e) => e.kind === "RETURN_PUTAWAY")
      .reduce((n, e) => n + e.quantity_micro, 0),
    4 * U,
  );
});
test("selected days carry stock forward and checkpoints reproduce every event and crane trajectory", () => {
  const orders = [line(2, 8), line(3, 12, "2023-02-07")],
    dates = ["2023-01-03", "2023-02-07"];
  const a = fixture(model(), orders, dates),
    expected = finish(a.create());
  const b = fixture(model(), orders, dates);
  let restored = b.create();
  for (let i = 0; i < 17; i++) restored.step();
  restored = b.create(JSON.parse(JSON.stringify(restored.checkpoint())));
  finish(restored);
  assert.deepEqual(b.events, a.events);
  assert.deepEqual(b.cycles, a.cycles);
  assert.deepEqual(restored.snapshot(), expected.snapshot());
  assert.equal(a.events.filter((e) => e.kind === "OPENING").length, 1);
  assert.ok(
    a.events.some(
      (e) =>
        e.order_date === "2023-02-07" &&
        e.simulation_time_local.startsWith("2023-02-07"),
    ),
  );
});
test("independent aisles operate concurrently and fractional base units never drift", () => {
  const m = model();
  m.cranes.push({
    id: "ASRS_02",
    workstation: "WS2",
    home: [10, 4, 0.5],
    rows: [3, 5],
  });
  m.loads.push({
    ...m.loads[0],
    id: "B#1",
    sku: "B",
    crane: "ASRS_02",
    position: [0, 3, 0.5],
  });
  m.buffers = { A: 0, B: 0 };
  const f = fixture(m, [
      line(2, 0.1),
      line(3, 0.2),
      line(4, 0.3, "2023-01-03", "B"),
    ]),
    engine = finish(f.create());
  assert.equal(engine.now, 41);
  assert.equal(exactQuantity(engine.loads["A#1"].quantity), "9.7");
  assert.equal(engine.totals.outbound, 600000);
});
test("simulation database commits checkpoints with immutable events and separates its stock from warehouse inventory", () => {
  const db = new AsrsDatabase(":memory:");
  try {
    const id = db.create({ name: "Test" }).id;
    db.appendOrders(id, [line(2, 3)]);
    db.transaction(() => {
      db.init(id, {}, model());
      const e = new AsrsEngine(
        model(),
        ["2023-01-03"],
        asrsSettings(),
        (d) => db.orders(id, d),
        db.sink(id),
      );
      finish(e);
      db.save(id, e.checkpoint(), e.snapshot());
    });
    const events = db.page(id, "events", { limit: 5000 }).rows;
    assert.equal(
      events.reduce((n: number, e: Row) => n + e.delta_micro, 0),
      7 * U,
    );
    assert.equal(db.stock(id, {})[0].quantity_micro, 7 * U);
    assert.equal(db.page(id, "orders", {}).rows[0].status, "completed");
    assert.throws(
      () => db.db.prepare("UPDATE events SET kind='NO_MOVEMENT'").run(),
      /immutable/,
    );
    assert.throws(
      () =>
        db.transaction(() => {
          db.db.prepare("UPDATE loads SET quantity_micro=0").run();
          throw new Error("rollback");
        }),
      /rollback/,
    );
    assert.equal(db.stock(id, {})[0].quantity_micro, 7 * U);
    db.status(id, "running", "running");
    db.recover();
    assert.equal(db.run(id).status, "paused");
    const latest = db.page(id, "events", { latest: "1", limit: 2 }).rows;
    assert.deepEqual(latest, events.slice(-2));
  } finally {
    db.db.close();
  }
});
test("SKU movement ledger separates consumption, refills and returns with event-time balances", () => {
  const db = new AsrsDatabase(":memory:"),
    m = model();
  m.loads.push({
    ...m.loads[0],
    id: "A#2",
    address: "A/L01/S02",
    position: [1, 0, 0.5],
    initial: 6.5 * U,
  });
  const orders = [
    line(2, 8),
    line(3, -3.25),
    line(4, 20),
    line(5, -1.125, "2023-01-03", "B"),
  ];
  try {
    const id = db.create({ name: "SKU ledger" }).id;
    db.appendOrders(id, orders);
    db.init(id, {}, m);
    const engine = finish(
      new AsrsEngine(
        m,
        ["2023-01-03"],
        asrsSettings({ speed: "max", replenishment_lead_seconds: 1 }),
        (date) => db.orders(id, date),
        db.sink(id),
      ),
    );
    const events = db.page(id, "events", { limit: 5000 }).rows,
      rows = db.page(id, "sku-movements", { limit: 5000 }).rows;
    assert.deepEqual(
      rows.map((r: Row) => r.sequence),
      events
        .filter((e: Row) =>
          ["ORDER_OUT", "REPLENISH_IN", "RETURN_PUTAWAY", "RETURN_IN"].includes(
            e.kind,
          ),
        )
        .map((e: Row) => e.sequence),
    );
    const balances: Record<string, number> = { A: 16.5 * U, B: 0 };
    for (const row of rows) {
      assert.equal(row.initial_sku_micro, row.sku === "A" ? 16.5 * U : 0);
      assert.equal(row.sku_before_micro, balances[row.sku]);
      assert.equal(row.sku_before_micro + row.delta_micro, row.sku_after_micro);
      balances[row.sku] = row.sku_after_micro;
      if (row.load_id)
        assert.equal(
          row.tote_before_micro + row.load_delta_micro,
          row.tote_after_micro,
        );
      else assert.equal(row.tote_before_micro, null);
    }
    assert.equal(
      Object.values(balances).reduce((a, b) => a + b, 0),
      engine.snapshot().onhand_micro,
    );
    const consume = db.page(id, "sku-movements", {
      movement: "consume",
      sku: "A",
      limit: 5000,
    }).rows;
    assert.equal(
      consume.reduce((q: number, r: Row) => q + r.consumed_micro, 0),
      28 * U,
    );
    assert.ok(
      consume.every(
        (r: Row) =>
          r.movement_type === "consume" &&
          r.order_reference === `ORD-${r.source_row}` &&
          r.replenished_micro === 0,
      ),
    );
    const refills = db.page(id, "sku-movements", {
      movement: "replenish",
      limit: 5000,
    }).rows;
    assert.ok(
      refills.some(
        (r: Row) =>
          r.kind === "REPLENISH_IN" &&
          r.sku_after_micro - r.sku_before_micro === r.replenished_micro,
      ),
    );
    const putaway = refills.find((r: Row) => r.kind === "RETURN_PUTAWAY");
    assert.ok(putaway);
    assert.equal(putaway.sku_before_micro, putaway.sku_after_micro);
    assert.equal(
      putaway.tote_after_micro - putaway.tote_before_micro,
      putaway.replenished_micro,
    );
    const returns = db.page(id, "sku-movements", {
      movement: "return",
      limit: 5000,
    }).rows;
    assert.equal(
      returns.reduce((q: number, r: Row) => q + r.returned_micro, 0),
      4.375 * U,
    );
    const first = db.page(id, "sku-movements", { limit: 1 });
    assert.deepEqual(
      db.page(id, "sku-movements", { after: first.next_after, limit: 5000 })
        .rows,
      rows.slice(1),
    );
    assert.deepEqual(
      db.page(id, "sku-movements", { latest: "1", limit: 2 }).rows,
      rows.slice(-2),
    );
    const lineRows = db.page(id, "sku-movements", {
      movement: "consume",
      source_row: 2,
      limit: 5000,
    }).rows;
    assert.equal(
      lineRows.reduce((q: number, r: Row) => q + r.consumed_micro, 0),
      8 * U,
    );
    assert.equal(
      db.page(id, "sku-movements", { sku: "missing" }).rows.length,
      0,
    );
    assert.throws(
      () => db.page(id, "sku-movements", { movement: "__proto__" }),
      /Choose/,
    );
    assert.equal(
      db.page(id, "events", { limit: 5000 }).rows.length,
      events.length,
    );
  } finally {
    db.db.close();
  }
});
test("invalid engineering settings and unsupported physical layouts are rejected", () => {
  assert.throws(() => asrsSettings({ speed: 0 }), /at least/);
  assert.throws(
    () => asrsSettings({ axes: [{ speed: 0, acceleration: 1 }] }),
    /Configure/,
  );
  assert.throws(
    () => asrsSettings({ replenishment_lead_seconds: -1 }),
    /nonnegative/,
  );
  assert.throws(
    () =>
      buildAsrsModel(
        {
          schema: "layout_generator_release/v1",
          warehouse: { project: { storage_layout: { system_type: "AMR" } } },
          slotting: {},
        },
        asrsSettings(),
      ),
    /Mini-load/,
  );
});

test("mixed concurrent demand, returns and repeated compact recovery preserve the complete ledger", () => {
  const m = model();
  m.cranes.push({
    id: "ASRS_02",
    workstation: "WS2",
    home: [10, 4, 0.5],
    rows: [3, 5],
  });
  m.loads.push({
    ...m.loads[0],
    id: "A#2",
    address: "A/2",
    crane: "ASRS_02",
    position: [1, 3, 0.5],
  });
  m.loads.push({
    ...m.loads[0],
    id: "B#1",
    sku: "B",
    address: "B/1",
    position: [1, 2, 0.5],
  });
  m.loads.push({
    ...m.loads[1],
    id: "B#2",
    sku: "B",
    address: "B/2",
    position: [2, 5, 0.5],
  });
  m.buffers = { A: 5 * U, B: 5 * U };
  let seed = 3425;
  const orders = Array.from({ length: 300 }, (_, i) => {
    seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
    const q = ((seed % 8 === 0 ? -1 : 1) * (seed % 91)) / 10;
    return line(
      i + 2,
      q,
      i < 150 ? "2023-01-03" : "2023-02-07",
      i % 2 ? "A" : "B",
      `S${i % 17}`,
    );
  });
  const dates = ["2023-01-03", "2023-02-07"],
    a = fixture(m, orders, dates),
    b = fixture(m, orders, dates);
  finish(a.create());
  let engine = b.create(),
    steps = 0;
  while (!engine.finished) {
    engine.step();
    engine.snapshot();
    if (++steps % 37 === 0)
      engine = b.create(JSON.parse(JSON.stringify(engine.checkpoint())));
    assert.ok(steps < 100000);
    assert.ok(
      Object.values(engine.loads).every(
        (l) => l.quantity >= 0 && l.quantity <= 10 * U,
      ),
    );
  }
  assert.deepEqual(b.events, a.events);
  assert.deepEqual(b.cycles, a.cycles);
  for (const order of orders)
    assert.equal(
      b.status.get(order.source_row)?.quantity,
      Math.abs(order.quantity_micro),
    );
});

test("a second application cannot reset or write an owned simulation database", () => {
  const folder = mkdtempSync(join(tmpdir(), "asrs-owner-")),
    a = new AsrsDatabase(join(folder, "asrs.sqlite")),
    b = new AsrsDatabase(join(folder, "asrs.sqlite"));
  try {
    a.claimRuntime();
    assert.throws(() => b.claimRuntime(), /Another application owns/);
    b.releaseRuntime();
    assert.throws(() => b.claimRuntime(), /Another application owns/);
    a.releaseRuntime();
    b.claimRuntime();
    b.releaseRuntime();
    a.db.prepare("INSERT INTO runtime_owner VALUES(1,-1,'stale','old')").run();
    a.claimRuntime();
    a.releaseRuntime();
  } finally {
    a.db.close();
    b.db.close();
    rmSync(folder, { recursive: true, force: true });
  }
});
