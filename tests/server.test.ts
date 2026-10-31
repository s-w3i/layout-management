import test from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createServer } from "node:net";
import { once } from "node:events";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { DatabaseSync } from "node:sqlite";
import { setTimeout as delay } from "node:timers/promises";
import ExcelJS from "exceljs";
import type { Row } from "../shared/model.js";
import { scenarioRows } from "../shared/operations.js";
import { physical, project, sku, native } from "./fixtures.js";

test("HTTP workflow imports, analyzes, sizes, generates, compares and restores portable workspaces", async (t) => {
  const listener = createServer();
  listener.listen(0, "127.0.0.1");
  await once(listener, "listening");
  const address = listener.address();
  assert.ok(address && typeof address !== "string");
  const port = address.port;
  await new Promise<void>((resolve) => listener.close(() => resolve()));
  const isolated = await mkdtemp(join(tmpdir(), "layout-http-"));
  let child = spawn(process.execPath, ["dist-server/server/index.js"], {
    env: { ...process.env, PORT: String(port), DATA_DIR: isolated },
    stdio: ["ignore", "pipe", "pipe"],
  });
  t.after(async () => {
    child.kill("SIGTERM");
    await once(child, "exit");
    await rm(isolated, { recursive: true, force: true });
  });
  await new Promise<void>((resolve, reject) => {
    child.once("error", reject);
    child.stdout.on("data", () => resolve());
    child.once("exit", (code) => reject(new Error(`Server stopped: ${code}`)));
  });
  const base = `http://127.0.0.1:${port}`;
  const request = async (path: string, value?: unknown) => {
    const response = await fetch(
      base + path,
      value === undefined
        ? {}
        : {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(value),
          },
    );
    const data = (await response.json()) as Row;
    assert.ok(response.ok, JSON.stringify(data));
    return data;
  };
  const poll = async (id: string) => {
    for (let i = 0; i < 600; ++i) {
      const job = await request(`/api/jobs/${id}`);
      if (job.status === "complete") return job.result as Row;
      if (job.status === "failed") throw new Error(job.error);
      await delay(20);
    }
    throw new Error("Job did not finish");
  };
  const upload = async (kind: string, name: string, body: string | Buffer) => {
    const response = await fetch(
      `${base}/api/uploads?kind=${kind}&name=${encodeURIComponent(name)}`,
      { method: "POST", body },
    );
    assert.equal(response.status, 202);
    const job = (await response.json()) as Row;
    const result = await poll(job.id);
    return result;
  };
  const engine = async (value: Row) =>
    poll((await request("/api/engine", value)).id);
  let orders: Row = {},
    layout: Row = {},
    reference: Row = {};
  const p = project(3, 3, 2);
  await t.test("loopback origin and command boundaries", async () => {
    assert.equal((await request("/api/health")).engine, "C++20");
    assert.equal(
      (
        await fetch(base + "/api/health", {
          headers: { Origin: "https://example.com" },
        })
      ).status,
      403,
    );
    assert.equal(
      (
        await fetch(base + "/api/engine", {
          method: "POST",
          body: JSON.stringify({ command: "score" }),
        })
      ).status,
      400,
    );
  });
  await t.test(
    "editor geometry returns authoritative C++ output without creating persistent jobs",
    async () => {
      const db = new DatabaseSync(join(isolated, "app.sqlite"), {
        readOnly: true,
      });
      try {
        const before = (
          db.prepare("SELECT count(*) AS n FROM jobs").get() as Row
        ).n;
        const g = await request("/api/geometry", { project: p });
        assert.deepEqual(g, native({ command: "geometry", project: p }));
        assert.equal(g.summary.rack_count, 3);
        assert.equal(g.summary.capacity, 18);
        assert.equal(
          (db.prepare("SELECT count(*) AS n FROM jobs").get() as Row).n,
          before,
        );
        const bad = await fetch(base + "/api/geometry", {
          method: "POST",
          headers: { Origin: "https://example.com" },
          body: JSON.stringify({ project: p }),
        });
        assert.equal(bad.status, 403);
        const invalid = await fetch(base + "/api/geometry", {
          method: "POST",
          body: JSON.stringify({ project: {} }),
        });
        assert.equal(invalid.status, 400);
      } finally {
        db.close();
      }
    },
  );
  await t.test(
    "CSV and XLSX use the same order and signed-quantity semantics",
    async () => {
      const rows = [
        [
          "date",
          "sku",
          "quantity_ea",
          "store",
          "UOM ConversionQty",
          "Time",
          "UOM",
          "Order ID",
        ],
        ["2026-01-01", "A", 4, "S", 0.5, "08:00:00", "EA", "ORDER1"],
        ["2026-01-03", "A", -1, "S", 0.5, "09:00:00", "EA", "ORDER2"],
        ["2026-01-03", "B", 2, "S", 1, "10:15:30", "EA", "ORDER3"],
      ];
      orders = await upload(
        "orders",
        "orders.csv",
        rows.map((r) => r.join(",")).join("\n"),
      );
      const book = new ExcelJS.Workbook();
      book.addWorksheet("orders").addRows(rows);
      const excel = await upload(
        "orders",
        "orders.xlsx",
        Buffer.from(await book.xlsx.writeBuffer()),
      );
      assert.equal(orders.line_count, 3);
      assert.equal(orders.store_day_count, 2);
      const analysis = await engine({
        command: "analyze",
        orders_id: orders.id,
      });
      const xlsx = await engine({ command: "analyze", orders_id: excel.id });
      assert.deepEqual(analysis, xlsx);
      assert.equal(
        analysis.rows.find((r: Row) => r.sku === "A").total_quantity_ea,
        3,
      );
    },
  );
  await t.test(
    "stock calculation and generation preserve quantities through the API",
    async () => {
      const stock = await engine({
        command: "stock",
        orders_id: orders.id,
        project: p,
        attributes: { A: physical, B: physical },
        settings: { minimum_stock_days: 2, buffer_stock_days: 1 },
      });
      assert.equal(
        stock.rows.find((r: Row) => r.sku === "A").total_slotted_ea,
        4,
      );
      assert.equal(
        stock.rows.find((r: Row) => r.sku === "A").uom_conversion_qty,
        0.5,
      );
      layout = await engine({
        command: "generate",
        project: p,
        sku_rows: stock.rows,
      });
      assert.equal(layout.validation.passed, true);
      assert.equal(layout.validation.complete, true);
      assert.equal(
        layout.assignments.reduce((n: number, r: Row) => n + r.quantity_ea, 0),
        6,
      );
      reference = await upload(
        "reference",
        "reference.json",
        JSON.stringify(layout),
      );
      const compared = await request("/api/compare", {
        layout,
        reference_id: reference.id,
      });
      assert.equal(compared.identical_placements, layout.assignments.length);
    },
  );
  await t.test(
    "portable export restores both order and reference assets",
    async () => {
      const saved = await request("/api/export-workspace", {
        name: "test",
        project: p,
        orders,
        reference,
      });
      assert.ok(saved.orders_data.baskets.length);
      const restored = await upload(
        "session",
        "workspace.json",
        JSON.stringify(saved),
      );
      assert.notEqual(restored.data.orders.id, orders.id);
      assert.notEqual(restored.data.reference.id, reference.id);
      assert.deepEqual(
        await engine({
          command: "analyze",
          orders_id: restored.data.orders.id,
        }),
        await engine({ command: "analyze", orders_id: orders.id }),
      );
      const compared = await request("/api/compare", {
        layout,
        reference_id: restored.data.reference.id,
      });
      assert.equal(compared.identical_placements, layout.assignments.length);
    },
  );
  await t.test(
    "movement and validated scenario swaps run through the HTTP job API",
    async () => {
      const before = JSON.stringify(layout);
      const movement = await engine({
        command: "movement",
        orders_id: orders.id,
        assignments: layout.assignments,
        handling_unit_type: layout.handling_unit_type,
      });
      assert.ok(movement.total_handling_unit_visits > 0);
      const first = layout.assignments[0],
        second = layout.assignments.find((row: Row) => row.sku !== first.sku);
      const changed = await engine({
        command: "swap",
        project: p,
        sku_rows: scenarioRows(layout),
        settings: layout.settings,
        layout,
        mode: "load",
        source: first.inventory_load_id,
        target: second.inventory_load_id,
      });
      assert.equal(changed.validation.passed, true);
      assert.equal(
        changed.assignments[0].storage_location_address,
        second.storage_location_address,
      );
      assert.equal(JSON.stringify(layout), before);
    },
  );
  await t.test(
    "date ranges are bounded by the selected input file through the API",
    async () => {
      for (const [command, settings, message] of [
        [
          "analyze",
          { start_date: "2025-12-31" },
          /between 2026-01-01 and 2026-01-03/,
        ],
        ["stock", { end_date: "2026-01-04" }, /between/],
        [
          "movement",
          { start_date: "2026-01-03", end_date: "2026-01-01" },
          /on or before/,
        ],
        ["generate", { end_date: "2026-02-30" }, /valid calendar date/],
      ] as const) {
        await assert.rejects(
          () => engine({ command, settings, orders_id: orders.id }),
          message,
        );
      }
      const valid = await engine({
        command: "analyze",
        orders_id: orders.id,
        settings: { start_date: orders.date_start, end_date: orders.date_end },
      });
      assert.equal(
        valid.rows.find((row: Row) => row.sku === "A").total_quantity_ea,
        3,
      );
      const partialStores = await upload(
        "orders",
        "partial-stores.csv",
        "date,sku,quantity_ea,store\n2026-01-01,A,1,\n2026-01-03,A,1,S\n",
      );
      const movement = await engine({
        command: "movement",
        orders_id: partialStores.id,
        assignments: layout.assignments,
        handling_unit_type: layout.handling_unit_type,
        settings: {
          start_date: partialStores.date_start,
          end_date: partialStores.date_end,
        },
      });
      assert.equal(movement.fulfillment_group_count, 1);
      await assert.rejects(
        () =>
          engine({
            command: "movement",
            settings: { start_date: "2020-01-01" },
            orders: {
              date_start: "2000-01-01",
              date_end: "2099-12-31",
              baskets: [{ date: "2026-01-01", store: "S", skus: ["A"] }],
            },
          }),
        /between 2026-01-01 and 2026-01-01/,
      );
    },
  );
  await t.test(
    "older autosaves cannot overwrite newer state; running generation can be cancelled",
    async () => {
      const workspace = await request("/api/workspaces", {});
      for (const [revision, expectedStatus] of [
        [2, 200],
        [1, 409],
        [2, 409],
      ]) {
        const response = await fetch(`${base}/api/workspaces/${workspace.id}`, {
          method: "PUT",
          body: JSON.stringify({
            _save_revision: revision,
            name: String(revision),
          }),
        });
        assert.equal(response.status, expectedStatus);
      }
      assert.equal(
        (await request(`/api/workspaces/${workspace.id}`)).name,
        "2",
      );
      const job = await request("/api/engine", {
        command: "generate",
        project: project(300),
        sku_rows: Array.from({ length: 3000 }, (_, i) =>
          sku(String(i), 1, 2, 2),
        ),
      });
      const cancelled = await fetch(`${base}/api/jobs/${job.id}`, {
        method: "DELETE",
      });
      assert.equal(((await cancelled.json()) as Row).status, "cancelled");
      assert.equal((await request(`/api/jobs/${job.id}`)).status, "cancelled");
    },
  );
  await t.test(
    "cancelling an order import releases its SQLite writer and hides partial history",
    async () => {
      const response = await fetch(
        `${base}/api/uploads?kind=orders&name=cancelled-orders.csv`,
        {
          method: "POST",
          body:
            "date,sku,quantity_ea,store\n" +
            "2026-01-01,A,1,S\n".repeat(500000),
        },
      );
      assert.equal(response.status, 202);
      const pending = (await response.json()) as Row;
      let started = false;
      for (let i = 0; i < 400; i++) {
        const job = await request(`/api/jobs/${pending.id}`);
        if (/Read /.test(job.progress)) {
          started = true;
          break;
        }
        if (job.status === "failed") throw new Error(job.error);
        await delay(10);
      }
      assert.equal(started, true);
      const preview = await request("/api/geometry", { project: p });
      assert.equal(preview.summary.rack_count, 3);
      assert.equal(
        (await request(`/api/jobs/${pending.id}`)).status,
        "running",
        "An import must not block the editor preview behind its queue",
      );
      const cancelled = await fetch(`${base}/api/jobs/${pending.id}`, {
        method: "DELETE",
      });
      assert.equal(((await cancelled.json()) as Row).status, "cancelled");
      let dataset: Row | undefined;
      for (let i = 0; i < 200; i++) {
        dataset = (await request("/api/manage/datasets")).find(
          (d: Row) => d.name === "cancelled-orders.csv",
        );
        if (dataset?.status === "failed") break;
        await delay(10);
      }
      assert.equal(dataset?.status, "failed");
      assert.equal(
        (await fetch(`${base}/api/manage/datasets/${dataset!.id}/lines`))
          .status,
        404,
      );
      assert.ok((await request("/api/workspaces", {})).id);
    },
  );
  await t.test(
    "SQLite inventory, publishing, scoped integration APIs and restart recovery",
    async () => {
      const ws = await request("/api/workspaces", {}),
        rows = [sku("API", 10, 4, 2)];
      const generated = await engine({
        command: "generate",
        project: p,
        sku_rows: rows,
      });
      const state = {
        name: "API warehouse",
        project: p,
        velocity: rows,
        stock: rows,
        attributes: { API: physical },
        settings: {},
        result: generated,
        _save_revision: 1,
      };
      const saved = await fetch(`${base}/api/workspaces/${ws.id}`, {
        method: "PUT",
        body: JSON.stringify(state),
      });
      assert.equal(saved.status, 200);
      await request(`/api/manage/warehouses/${ws.id}/sync`, {});
      await request("/api/manage/skus/import", { workspace_id: ws.id });
      const opening = {
        csv: "sku,quantity,location_id,lot\nAPI,5,RECEIVING,LOT1\n",
        actor: "Counter",
        reason: "Verified opening stock",
      };
      await request(
        `/api/manage/warehouses/${ws.id}/opening-stock-csv`,
        opening,
      );
      assert.equal(
        (
          await request(
            `/api/manage/warehouses/${ws.id}/opening-stock-csv`,
            opening,
          )
        ).duplicate,
        true,
      );
      const token = await request("/api/manage/keys", {
        name: "Read consumer",
        scope: "read",
      });
      const operate = await request("/api/manage/keys", {
        name: "Actual system",
        scope: "operate",
      });
      assert.equal((await fetch(base + "/api/v1/warehouses")).status, 401);
      const headers = { Authorization: `Bearer ${token.token}` };
      let r = await fetch(`${base}/api/v1/warehouses/${ws.id}/inventory`, {
        headers,
      });
      assert.equal(r.status, 200);
      assert.equal(((await r.json()) as Row[])[0].quantity, 5);
      r = await fetch(`${base}/api/v1/warehouses/${ws.id}/inventory`, {
        method: "POST",
        headers,
        body: JSON.stringify({}),
      });
      assert.equal(r.status, 403);
      const payload = {
        kind: "receipt",
        request_id: "external-receipt",
        reason: "External reference",
        sku: "API",
        quantity: "2",
        location_id: "RECEIVING",
      };
      for (let i = 0; i < 2; i++) {
        r = await fetch(`${base}/api/v1/warehouses/${ws.id}/inventory`, {
          method: "POST",
          headers: { Authorization: `Bearer ${operate.token}` },
          body: JSON.stringify(payload),
        });
        assert.equal(r.status, 201);
      }
      assert.equal(
        (await request(`/api/manage/warehouses/${ws.id}/inventory`)).reduce(
          (n: number, r: Row) => n + r.quantity,
          0,
        ),
        7,
      );
      const publication = await request(
        `/api/manage/warehouses/${ws.id}/publish`,
        { name: "API release", expected_revision: 1 },
      );
      const bundle = await request(`/api/manage/releases/${publication.id}`);
      assert.deepEqual(bundle.slotting.assignments, generated.assignments);
      assert.equal(
        bundle.inventory.reduce((n: number, r: Row) => n + r.quantity, 0),
        7,
      );
      const spec = await request("/api/openapi.json");
      assert.ok(spec.paths["/api/v1/warehouses/{warehouseId}/inventory"].post);
      const original = await request(`/api/manage/datasets/${orders.id}/lines`);
      assert.equal(original.total, 3);
      assert.equal(original.rows[0].source_time, "08:00:00");
      assert.equal(original.rows[0].uom, "EA");
      assert.equal(original.rows[0].order_reference, "ORDER1");
      const publicLines = await fetch(
        `${base}/api/v1/datasets/${orders.id}/lines?limit=2`,
        { headers },
      );
      assert.equal(publicLines.status, 200);
      assert.equal(((await publicLines.json()) as Row).rows.length, 2);
      const daily = await fetch(
        `${base}/api/v1/datasets/${orders.id}/daily?sku=A`,
        { headers },
      );
      assert.equal(daily.status, 200);
      assert.equal(((await daily.json()) as Row).total, 2);
      const snapshot = await fetch(
        `${base}/api/v1/warehouses/${ws.id}/inventory-snapshot`,
        { headers },
      );
      assert.equal(snapshot.status, 200);
      assert.ok(((await snapshot.json()) as Row).inventory_event_sequence > 0);
      assert.equal(
        (
          await fetch(`${base}/api/v1/warehouses/unknown/inventory`, {
            headers,
          })
        ).status,
        404,
      );
      assert.equal(original.rows[1].quantity_ea, -1);
      assert.equal(
        (await request(`/api/manage/datasets`)).find(
          (d: Row) => d.id === orders.id,
        ).raw_count,
        3,
      );
      child.kill("SIGTERM");
      await once(child, "exit");
      child = spawn(process.execPath, ["dist-server/server/index.js"], {
        env: { ...process.env, PORT: String(port), DATA_DIR: isolated },
        stdio: ["ignore", "pipe", "pipe"],
      });
      await new Promise<void>((resolve, reject) => {
        child.once("error", reject);
        child.stdout.once("data", () => resolve());
        child.once("exit", (code) =>
          reject(new Error(`Restart failed: ${code}`)),
        );
      });
      r = await fetch(`${base}/api/v1/warehouses/${ws.id}/published`, {
        headers,
      });
      assert.equal(r.status, 200);
      assert.deepEqual(await r.json(), bundle);
      assert.equal(
        (await request(`/api/manage/warehouses/${ws.id}/inventory`)).reduce(
          (n: number, r: Row) => n + r.quantity,
          0,
        ),
        7,
      );
      assert.equal(
        (await request(`/api/manage/warehouses/${ws.id}/events`)).at(0).actor,
        "integration:Actual system",
      );
      await fetch(`${base}/api/manage/keys/${token.id}`, { method: "DELETE" });
      assert.equal(
        (await fetch(base + "/api/v1/warehouses", { headers })).status,
        401,
      );
    },
  );
});
