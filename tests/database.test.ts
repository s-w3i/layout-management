import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync, mkdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createHash, randomUUID } from "node:crypto";
import { WarehouseDatabase, canonicalJson } from "../server/database.js";
import { Storage } from "../server/storage.js";
import { scaledQuantity, exactQuantity } from "../shared/inventory.js";
import { native, project, physical, sku } from "./fixtures.js";
import type { Row } from "../shared/model.js";

test("expiry uses the warehouse time zone and configuration changes invalidate cached profiles", () => {
  let clock = new Date("2026-10-31T15:59:00Z");
  const db = new WarehouseDatabase(":memory:", () => clock);
  try {
    const id = db.createWorkspace().id,
      p = project(2),
      g = native({ command: "geometry", project: p });
    db.syncWarehouse(id, p, g, "Malaysia", "Asia/Kuala_Lumpur");
    db.saveSku({ sku: "TIME", name: "Time zone test" });
    const receipt = db.postInventory({
      warehouse_id: id,
      kind: "receipt",
      sku: "TIME",
      quantity: "1",
      location_id: "RECEIVING",
      expiry: "2026-10-31",
      request_id: "tz-receipt",
      actor: "Counter",
      reason: "Date boundary",
    });
    assert.equal(db.listInventory(id)[0].available, 1);
    clock = new Date("2026-10-31T16:00:00Z");
    assert.equal(db.listInventory(id)[0].available, 0);
    assert.throws(
      () =>
        db.postInventory({
          warehouse_id: id,
          kind: "reserve",
          position_id: receipt.position_id,
          quantity: "1",
          request_id: "tz-reserve",
          actor: "Picker",
          reason: "Expired locally",
        }),
      /not eligible/,
    );
    db.syncWarehouse(id, p, g, "UTC", "UTC");
    assert.equal(db.listInventory(id)[0].available, 1);
    assert.equal(db.warehouse(id).time_zone, "UTC");
    assert.throws(
      () => db.syncWarehouse(id, p, g, "Invalid", "Invalid/Zone"),
      /IANA/,
    );
  } finally {
    db.close();
  }
});

test("a consistent inventory snapshot plus the event feed reconstructs later stock exactly", (t) => {
  const { db, id, post } = fixture(t);
  const receipt = post({
    kind: "receipt",
    sku: "001",
    location_id: "RECEIVING",
    quantity: "10",
  });
  const snapshot = db.inventorySnapshot(id);
  post({
    kind: "transfer",
    position_id: receipt.position_id,
    quantity: "4",
    destination: "DISPATCH",
  });
  post({ kind: "reserve_sku", sku: "001", quantity: "2" });
  const positions = new Map<string, Row>(
    snapshot.inventory.map((p: Row) => [p.id, { ...p }]),
  );
  for (const event of db.listEvents(
    id,
    String(snapshot.inventory_event_sequence),
  ))
    for (const line of event.lines) {
      const p = positions.get(line.position_id) || {
        id: line.position_id,
        sku: line.sku,
        location_id: line.location_id,
        quantity_micro: 0,
        reserved_micro: 0,
      };
      p.quantity_micro += line.quantity_delta_micro;
      p.reserved_micro += line.reserved_delta_micro;
      positions.set(p.id, p);
    }
  for (const actual of db.listInventory(id)) {
    const replayed = positions.get(actual.id)!;
    assert.equal(replayed.quantity_micro, actual.quantity_micro);
    assert.equal(replayed.reserved_micro, actual.reserved_micro);
    assert.equal(replayed.location_id, actual.location_id);
  }
});

test("master CSV imports preserve unspecified fields and roll back invalid batches", (t) => {
  const { db } = fixture(t);
  db.importMasterRows("uoms", [{ sku: "001", uom: "CTN", factor: "12" }]);
  db.importMasterRows("skus", [{ sku: "001", name: "Updated" }]);
  let row = db.listSkus()[0];
  assert.equal(row.name, "Updated");
  assert.deepEqual(row.attributes, physical);
  assert.equal(row.uoms.find((u: Row) => u.uom === "CTN").factor, 12);
  assert.throws(() =>
    db.importMasterRows("skus", [
      { sku: "001", name: "Must roll back" },
      { sku: "BAD", max_item_weight: "invalid" },
    ]),
  );
  assert.equal(db.listSkus()[0].name, "Updated");
  assert.equal(db.listSkus("BAD").length, 0);
  db.importMasterRows("skus", [{ sku: "INACTIVE", active: "false" }]);
  assert.equal(db.listSkus("INACTIVE")[0].active, 0);
  assert.throws(
    () => db.importMasterRows("uoms", [{ sku: "001", uom: "EA", factor: "2" }]),
    /Base UOM/,
  );
  assert.equal(exactQuantity(9007199254740991), "9007199254.740991");
  assert.equal(exactQuantity(-1), "-0.000001");
});
test("SKU reservations allocate by earliest expiry and roll back shortages", (t) => {
  const { db, id, post } = fixture(t);
  for (const [lot, expiry, quantity] of [
    ["UNDATED", "", "20"],
    ["LATER", "2099-06-01", "4"],
    ["EARLY", "2099-01-01", "2"],
  ])
    post({
      kind: "receipt",
      sku: "001",
      location_id: "RECEIVING",
      lot,
      expiry,
      quantity,
    });
  const allocation = post({ kind: "reserve_sku", sku: "001", quantity: "6" });
  assert.equal(allocation.reservation_ids.length, 2);
  const stock = db.listInventory(id);
  assert.equal(stock.find((p) => p.lot === "EARLY")?.reserved, 2);
  assert.equal(stock.find((p) => p.lot === "LATER")?.reserved, 4);
  assert.equal(stock.find((p) => p.lot === "UNDATED")?.reserved, 0);
  const count = db.listReservations(id).length;
  assert.throws(
    () => post({ kind: "reserve_sku", sku: "001", quantity: "30" }),
    /Insufficient/,
  );
  assert.equal(db.listReservations(id).length, count);
});
test("workspace normalization preserves independent velocity and stock ordering", (t) => {
  const { db, id, state } = fixture(t);
  const next = {
    ...state,
    velocity: [{ sku: "B" }, { sku: "A" }],
    stock: [{ sku: "A" }, { sku: "B" }],
    result: null,
    _save_revision: 2,
  };
  db.saveWorkspace(id, next);
  assert.deepEqual(db.getWorkspace(id).stock, next.stock);
  assert.deepEqual(db.getWorkspace(id).velocity, next.velocity);
});

function fixture(t: any, disk = false) {
  const folder = mkdtempSync(join(tmpdir(), "warehouse-db-")),
    path = disk ? join(folder, "app.sqlite") : ":memory:";
  const db = new WarehouseDatabase(path),
    id = db.createWorkspace().id,
    p = project(3, 3, 2);
  const rows = [sku("001", 10, 4, 2)],
    result = native({ project: p, sku_rows: rows });
  const state = {
    name: "Warehouse",
    project: p,
    velocity: rows,
    stock: rows,
    attributes: { "001": physical },
    settings: {},
    result,
    _save_revision: 1,
  };
  db.saveWorkspace(id, state);
  db.importWorkspaceSkus(id);
  db.syncWarehouse(
    id,
    p,
    native({ command: "geometry", project: p }),
    "Warehouse",
  );
  t.after(() => {
    db.close();
    rmSync(folder, { recursive: true, force: true });
  });
  let sequence = 0;
  const post = (value: Row) =>
    db.postInventory({
      warehouse_id: id,
      actor: "Operator",
      reason: "Test reference",
      request_id: `test-${++sequence}`,
      ...value,
    });
  return { db, id, p, state, result, path, folder, post };
}
test("quantities use exact base-unit arithmetic and SKU-specific UOM conversions", (t) => {
  const { db, post, id } = fixture(t);
  assert.equal(scaledQuantity("0.000001"), 1);
  for (const bad of ["1.0000001", "NaN", "1e2", Infinity, -1])
    assert.throws(() => scaledQuantity(bad));
  const master = db.listSkus()[0];
  db.saveSku({
    ...master,
    uoms: [
      { uom: "CTN", factor: "12" },
      { uom: "HALF", factor: "0.5" },
    ],
  });
  post({
    kind: "receipt",
    sku: "001",
    location_id: "RECEIVING",
    quantity: "2",
    uom: "CTN",
  });
  post({
    kind: "receipt",
    sku: "001",
    location_id: "RECEIVING",
    quantity: "0.2",
    uom: "HALF",
  });
  assert.equal(db.listInventory(id)[0].quantity, 24.1);
  assert.throws(
    () =>
      post({
        kind: "receipt",
        sku: "001",
        location_id: "RECEIVING",
        quantity: "0.000001",
        uom: "HALF",
      }),
    /six decimal/,
  );
  assert.throws(
    () =>
      post({
        kind: "receipt",
        sku: "001",
        location_id: "RECEIVING",
        quantity: "1",
        uom: "UNKNOWN",
      }),
    /not found/,
  );
});
test("idempotent postings, atomic transfers and reservations prevent double spending", (t) => {
  const { db, post, id } = fixture(t),
    receipt = {
      kind: "receipt",
      sku: "001",
      location_id: "RECEIVING",
      quantity: "10",
      request_id: "receipt-once",
    };
  assert.deepEqual(post(receipt), post(receipt));
  assert.equal(db.listInventory(id)[0].quantity, 10);
  assert.throws(
    () => post({ ...receipt, quantity: "11" }),
    /different operation/,
  );
  const source = db.listInventory(id)[0];
  const reservation = post({
    kind: "reserve",
    position_id: source.id,
    quantity: "7",
  });
  assert.equal(db.listInventory(id)[0].available, 3);
  assert.throws(
    () =>
      post({
        kind: "transfer",
        position_id: source.id,
        quantity: "4",
        destination: "DISPATCH",
      }),
    /Insufficient/,
  );
  post({
    kind: "transfer",
    position_id: source.id,
    quantity: "3",
    destination: "DISPATCH",
  });
  assert.equal(
    db.listInventory(id).reduce((n, r) => n + r.quantity, 0),
    10,
  );
  post({ kind: "dispatch", reservation_id: reservation.reservation_id });
  assert.equal(
    db.listInventory(id).reduce((n, r) => n + r.quantity, 0),
    3,
  );
  assert.throws(
    () =>
      post({ kind: "dispatch", reservation_id: reservation.reservation_id }),
    /not active/,
  );
  const lines = db.listEvents(id).flatMap((r) => r.lines);
  assert.equal(
    lines.reduce((n, r) => n + r.quantity_delta, 0),
    3,
  );
  assert.throws(() => db.db.exec("DELETE FROM stock_events"), /immutable/);
});
test("relocation confirms source, transit, interruption recovery and destination separately", (t) => {
  const { db, post, id } = fixture(t);
  const receipt = post({
    kind: "receipt",
    sku: "001",
    location_id: "RECEIVING",
    quantity: "8",
  });
  const task = post({
    kind: "task_create",
    position_id: receipt.position_id,
    quantity: "5",
    destination: "DISPATCH",
  });
  assert.equal(task.task_type, "relocation");
  assert.equal(db.listInventory(id)[0].available, 3);
  assert.throws(
    () => post({ kind: "task_complete", task_id: task.task_id }),
    /not valid/,
  );
  post({ kind: "task_pickup", task_id: task.task_id });
  let positions = db.listInventory(id);
  assert.equal(
    positions.reduce((n, r) => n + r.quantity, 0),
    8,
  );
  assert.equal(
    positions.find((r) => r.location_kind === "transit")?.available,
    0,
  );
  post({ kind: "task_fail", task_id: task.task_id });
  assert.equal(db.listTasks(id)[0].status, "failed");
  post({ kind: "task_complete", task_id: task.task_id });
  positions = db.listInventory(id);
  assert.equal(
    positions.find((r) => r.location_id === "DISPATCH")?.quantity,
    5,
  );
  assert.equal(
    positions.some((r) => r.location_kind === "transit"),
    false,
  );
  assert.throws(
    () => post({ kind: "task_pickup", task_id: task.task_id }),
    /not valid/,
  );
  const cancelled = post({
    kind: "task_create",
    position_id: receipt.position_id,
    quantity: "2",
    destination: "DISPATCH",
  });
  post({ kind: "task_cancel", task_id: cancelled.task_id });
  assert.equal(
    db.listInventory(id).find((r) => r.location_id === "RECEIVING")?.reserved,
    0,
  );
});
test("delivery and replenishment task IDs retain their purpose through retries and lifecycle actions", (t) => {
  const { db, post, id } = fixture(t);
  const stock = post({
    kind: "receipt",
    sku: "001",
    location_id: "RECEIVING",
    quantity: "20",
  });
  for (const [type, prefix] of [
    ["delivery", "DEL"],
    ["replenishment", "RPL"],
  ]) {
    const value = {
      kind: "task_create",
      task_type: type,
      position_id: stock.position_id,
      quantity: "2",
      destination: "DISPATCH",
      request_id: `typed-${type}`,
    };
    const created = post(value);
    assert.match(created.task_id, new RegExp(`^${prefix}-[0-9a-f-]{36}$`));
    assert.equal(created.task_type, type);
    assert.deepEqual(post(value), created);
    assert.throws(
      () =>
        post({
          ...value,
          task_type: type === "delivery" ? "replenishment" : "delivery",
        }),
      /different operation/,
    );
    assert.equal(
      db.listTasks(id).find((t) => t.id === created.task_id)?.task_type,
      type,
    );
    for (const kind of [
      "task_pickup",
      "task_fail",
      "task_redirect",
      "task_complete",
    ]) {
      const action = post({
        kind,
        task_id: created.task_id,
        task_type: "relocation",
        ...(kind === "task_redirect" ? { destination: "DISPATCH" } : {}),
      });
      assert.equal(action.task_id, created.task_id);
      assert.equal(action.task_type, type);
    }
    const history = db
      .listEvents(id)
      .filter((e) => e.task_id === created.task_id);
    assert.equal(history.length, 5);
    assert.ok(history.every((e) => e.task_type === type));
  }
  const before = db.listInventory(id),
    eventCount = db.listEvents(id).length;
  assert.throws(
    () =>
      post({
        kind: "task_create",
        task_type: "__proto__",
        position_id: stock.position_id,
        quantity: "1",
        destination: "DISPATCH",
      }),
    /Choose delivery/,
  );
  assert.deepEqual(db.listInventory(id), before);
  assert.equal(db.listEvents(id).length, eventCount);
  const cancelled = post({
    kind: "task_create",
    task_type: "replenishment",
    position_id: stock.position_id,
    quantity: "1",
    destination: "DISPATCH",
  });
  assert.equal(
    post({ kind: "task_cancel", task_id: cancelled.task_id }).task_type,
    "replenishment",
  );
});
test("expired, quarantined and damaged stock cannot be reserved; returns default to quarantine", (t) => {
  const { db, post, id } = fixture(t);
  assert.throws(
    () =>
      post({
        kind: "receipt",
        sku: "001",
        location_id: "RECEIVING",
        quantity: "1",
        expiry: "2000-01-01",
      }),
    /Expired/,
  );
  const returned = post({
    kind: "return",
    sku: "001",
    location_id: "RECEIVING",
    quantity: "2",
    expiry: "2000-01-01",
  });
  assert.equal(db.listInventory(id)[0].status, "quarantine");
  assert.throws(
    () =>
      post({
        kind: "reserve",
        position_id: returned.position_id,
        quantity: "1",
      }),
    /not eligible/,
  );
  assert.throws(
    () =>
      post({
        kind: "status",
        position_id: returned.position_id,
        quantity: "1",
        status: "available",
      }),
    /Expired/,
  );
  assert.throws(
    () =>
      post({
        kind: "receipt",
        sku: "001",
        location_id: "RECEIVING",
        quantity: "1",
        expiry: "2026-02-30",
      }),
    /calendar/,
  );
});
test("opening-stock imports roll back every row on error and do not repost identical batches", (t) => {
  const { db, id } = fixture(t);
  const value = {
    actor: "Counter",
    reason: "Opening stock",
    rows: [
      { sku: "001", quantity: "4", location_id: "RECEIVING" },
      { sku: "missing", quantity: "2", location_id: "DISPATCH" },
    ],
  };
  assert.throws(() => db.importOpening(id, value));
  assert.equal(db.listInventory(id).length, 0);
  assert.equal(db.listEvents(id).length, 0);
  value.rows[1].sku = "001";
  assert.equal(db.importOpening(id, value).imported, 2);
  assert.equal(db.importOpening(id, value).duplicate, true);
  assert.equal(
    db.listInventory(id).reduce((n, r) => n + r.quantity, 0),
    6,
  );
});
test("workspace migration preserves normalized planning rows, revision guards and calculation outputs", (t) => {
  const { db, id, state, result } = fixture(t);
  const restored = db.getWorkspace(id);
  assert.deepEqual(restored.result, result);
  assert.deepEqual(restored.velocity, state.velocity);
  assert.deepEqual(restored.stock, state.stock);
  assert.deepEqual(restored.attributes, state.attributes);
  const thin = JSON.parse(
    String(
      db.db.prepare("SELECT state_json FROM workspaces WHERE id=?").get(id)!
        .state_json,
    ),
  );
  assert.equal(thin.velocity, undefined);
  assert.equal(thin.stock, undefined);
  assert.equal(thin.result, undefined);
  assert.throws(
    () => db.saveWorkspace(id, { ...state, _save_revision: 1 }),
    /newer saved version/,
  );
  db.saveWorkspace(id, { ...state, name: "Renamed", _save_revision: 2 });
  assert.equal(db.getWorkspace(id).name, "Renamed");
});
test("published releases pin consistent versions, retain inventory snapshots and reject stale inputs", (t) => {
  const { db, id, state, post } = fixture(t);
  post({
    kind: "receipt",
    sku: "001",
    location_id: "RECEIVING",
    quantity: "3",
  });
  const first = db.publish(id, {
    name: "Version 1",
    expected_revision: 1,
    validation: { passed: true, complete: true },
  });
  const bundle = db.getRelease(first.id);
  assert.equal(bundle.inventory[0].quantity, 3);
  const { checksum, ...payload } = bundle;
  assert.equal(
    createHash("sha256").update(canonicalJson(payload)).digest("hex"),
    checksum,
  );
  post({
    kind: "receipt",
    sku: "001",
    location_id: "RECEIVING",
    quantity: "2",
  });
  assert.equal(db.getRelease(first.id).inventory[0].quantity, 3);
  assert.equal(db.listInventory(id)[0].quantity, 5);
  const second = db.publish(id, {
    name: "Version 2",
    expected_revision: 1,
    validation: { passed: true, complete: true },
  });
  assert.equal(second.version, 2);
  assert.equal(db.published(id).id, second.id);
  assert.throws(
    () =>
      db.publish(id, {
        name: "bad",
        expected_revision: 0,
        validation: { passed: true, complete: true },
      }),
    /changed/,
  );
  db.saveWorkspace(id, { ...state, stockStale: true, _save_revision: 2 });
  assert.throws(
    () =>
      db.publish(id, {
        name: "bad",
        expected_revision: 2,
        validation: { passed: true, complete: true },
      }),
    /current layout/,
  );
  assert.throws(
    () => db.db.exec("UPDATE releases SET name='changed'"),
    /immutable/,
  );
});
test("warehouse changes with stock roll back; API keys are hashed, scoped and revocable", (t) => {
  const { db, id, p, post } = fixture(t);
  const before = db.warehouse(id).version_hash;
  post({
    kind: "receipt",
    sku: "001",
    location_id: "RECEIVING",
    quantity: "1",
  });
  const changed = { ...p, grid: { ...p.grid, width_m: 4 } };
  assert.throws(
    () =>
      db.syncWarehouse(
        id,
        changed,
        native({ command: "geometry", project: changed }),
        "Changed",
      ),
    /inventory is present/,
  );
  assert.equal(db.warehouse(id).version_hash, before);
  const key = db.createKey({ name: "External system", scope: "read" });
  assert.equal(db.authenticate(key.token).name, "External system");
  assert.throws(() => db.authenticate(key.token, true), /read-only/);
  assert.ok(!JSON.stringify(db.listKeys()).includes(key.token));
  assert.notEqual(
    db.db.prepare("SELECT token_hash FROM api_keys WHERE id=?").get(key.id)!
      .token_hash,
    key.token,
  );
  db.revokeKey(key.id);
  assert.throws(() => db.authenticate(key.token), /Bearer/);
});
test("two database workers cannot reserve the same last unit", async (t) => {
  const { db, id, path, post } = fixture(t, true);
  const receipt = post({
    kind: "receipt",
    sku: "001",
    location_id: "RECEIVING",
    quantity: "1",
  });
  const a = new Storage(path),
    b = new Storage(path);
  await Promise.all([a.ready, b.ready]);
  try {
    const results = await Promise.allSettled(
      [a, b].map((s, i) =>
        s.call("postInventory", {
          warehouse_id: id,
          kind: "reserve",
          position_id: receipt.position_id,
          quantity: "1",
          actor: "Concurrent operator",
          reason: "Last unit",
          request_id: `concurrent-${i}`,
        }),
      ),
    );
    assert.equal(results.filter((r) => r.status === "fulfilled").length, 1);
    assert.equal(results.filter((r) => r.status === "rejected").length, 1);
    assert.equal(db.listInventory(id)[0].reserved, 1);
  } finally {
    await Promise.all([a.close(), b.close()]);
  }
});
test("legacy files migrate once and database backups restore complete inventory and releases", (t) => {
  const { db, id, folder, post } = fixture(t, true);
  const legacy = join(folder, "legacy");
  mkdirSync(join(legacy, "workspaces"), { recursive: true });
  mkdirSync(join(legacy, "assets"));
  const legacyId = randomUUID();
  writeFileSync(
    join(legacy, "workspaces", legacyId + ".json"),
    JSON.stringify({
      name: "Legacy",
      _save_revision: 18,
      velocity: [{ sku: "L" }],
      attributes: { L: { chilled: true } },
    }),
  );
  const asset = randomUUID();
  writeFileSync(
    join(legacy, "assets", asset + ".json"),
    JSON.stringify({
      daily: [{ date: "2026-01-01", sku: "L", quantity: 2, frequency: 1 }],
      baskets: [{ date: "2026-01-01", store: "S", skus: ["L"] }],
    }),
  );
  db.migrateLegacy(legacy);
  assert.equal(db.getWorkspace(legacyId)._save_revision, 18);
  assert.equal(db.getAsset(asset).daily[0].quantity, 2);
  assert.equal(
    db.listDatasets().find((d) => d.id === asset)!.metadata.date_start,
    "2026-01-01",
  );
  db.migrateLegacy(legacy);
  assert.equal(db.listWorkspaces().length, 2);
  post({
    kind: "receipt",
    sku: "001",
    location_id: "RECEIVING",
    quantity: "5",
  });
  db.publish(id, {
    name: "Backup release",
    expected_revision: 1,
    validation: { passed: true, complete: true },
  });
  const backup = join(folder, "backup.sqlite");
  db.backup(backup);
  const restored = new WarehouseDatabase(backup);
  try {
    assert.equal(restored.health().integrity, "ok");
    assert.deepEqual(restored.listInventory(id), db.listInventory(id));
    assert.deepEqual(restored.published(id), db.published(id));
  } finally {
    restored.close();
  }
});
test("mixed-SKU containers relocate together and cannot receive or move extra stock during transit", (t) => {
  const { db, id, post } = fixture(t);
  db.saveSku({ sku: "002", name: "Second SKU", attributes: physical });
  const first = post({
    kind: "receipt",
    sku: "001",
    location_id: "RECEIVING",
    quantity: "4",
    container: "BIN-1",
  });
  post({
    kind: "receipt",
    sku: "002",
    location_id: "RECEIVING",
    quantity: "3",
    container: "BIN-1",
  });
  assert.throws(
    () =>
      post({
        kind: "transfer",
        position_id: first.position_id,
        quantity: "4",
        destination: "DISPATCH",
      }),
    /whole-container/,
  );
  const task = post({
    kind: "task_create",
    position_id: first.position_id,
    quantity: "4",
    destination: "DISPATCH",
  });
  assert.equal(task.item_count, 2);
  assert.throws(
    () =>
      post({
        kind: "receipt",
        sku: "002",
        location_id: "RECEIVING",
        quantity: "1",
        container: "BIN-1",
      }),
    /active relocation/,
  );
  post({ kind: "task_pickup", task_id: task.task_id });
  assert.ok(db.listContainers(id)[0].location_id.startsWith("TRANSIT:"));
  assert.throws(
    () =>
      post({
        kind: "container_transfer",
        container: "BIN-1",
        destination: "RECEIVING",
      }),
    /active relocation/,
  );
  post({ kind: "task_fail", task_id: task.task_id });
  post({
    kind: "task_redirect",
    task_id: task.task_id,
    destination: "RECEIVING",
  });
  post({ kind: "task_complete", task_id: task.task_id });
  assert.equal(db.listContainers(id)[0].location_id, "RECEIVING");
  assert.equal(
    db.listInventory(id).reduce((n, r) => n + r.quantity, 0),
    7,
  );
  post({
    kind: "container_transfer",
    container: "BIN-1",
    destination: "DISPATCH",
  });
  assert.equal(db.listContainers(id)[0].location_id, "DISPATCH");
  assert.equal(
    db.listInventory(id).every((p) => p.location_id === "DISPATCH"),
    true,
  );
});
test("storage checks inherit zone restrictions and sparse ASRS buffers define valid locations", (t) => {
  const { db, id, p, post } = fixture(t);
  const zone = Object.values(p.zone_assignments)[0];
  const changed = {
    ...p,
    location_attributes: {
      ...p.location_attributes,
      [String(zone)]: { ...p.warehouse_storage_defaults, chilled: false },
    },
  };
  db.syncWarehouse(
    id,
    changed,
    native({ command: "geometry", project: changed }),
    "Zone test",
  );
  const master = db.listSkus()[0];
  db.saveSku({ ...master, attributes: { ...physical, chilled: true } });
  const location = db
    .listLocations(id)
    .find((l) => l.kind === "storage" && l.details.zone === zone)!;
  assert.throws(
    () =>
      post({
        kind: "receipt",
        sku: "001",
        location_id: location.id,
        quantity: "1",
      }),
    /requires chilled/,
  );
  assert.equal(db.listInventory(id).length, 0);
  const asrs = project(2, 3, 2);
  asrs.storage_layout = {
    ...asrs.storage_layout,
    system_type: "Mini-load ASRS",
    handling_unit_type: "Tote",
    buffer_level: "slot",
    buffers: asrs.storage_layout.buffers.map((b: Row) => ({
      ...b,
      buffer_id: b.buffer_id + "/L01/S01",
      buffer_level: "slot",
      level: 1,
      slot: 1,
    })),
  };
  const geometry = native({ command: "geometry", project: asrs });
  db.syncWarehouse(id, asrs, geometry, "Sparse ASRS");
  assert.equal(geometry.summary.capacity, 2);
  assert.equal(
    db.listLocations(id).filter((l) => l.kind === "storage").length,
    2,
  );
  for (const [system, handling] of [
    ["Mini-load ASRS", "Tote"],
    ["Pallet ASRS", "Pallet"],
  ]) {
    asrs.storage_layout.system_type = system;
    asrs.storage_layout.handling_unit_type = handling;
    const g = native({ command: "geometry", project: asrs });
    db.syncWarehouse(id, asrs, g, system);
    const generated = native({
      project: asrs,
      sku_rows: [sku("ONE", 1, 1, 1)],
    });
    const locations = db.listLocations(id).filter((l) => l.kind === "storage");
    assert.equal(locations.length, 2);
    for (const row of generated.assignments)
      for (const unit of row.occupied_handling_units) {
        const location = locations.find(
          (l) => l.details.address === unit.storage_location_address,
        );
        assert.ok(location);
        assert.equal(location.details.handling_unit_id, unit.handling_unit_id);
        assert.equal(location.details.buffer_id, unit.buffer_id);
      }
  }
});
