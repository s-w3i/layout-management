import test from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createServer } from "node:net";
import { once } from "node:events";
import { unlink } from "node:fs/promises";
import { setTimeout as delay } from "node:timers/promises";
import ExcelJS from "exceljs";
import type { Row } from "../shared/model.js";
import { scenarioRows } from "../shared/operations.js";
import { physical, project, sku } from "./fixtures.js";

test("HTTP workflow imports, analyzes, sizes, generates, compares and restores portable workspaces", async (t) => {
  const listener = createServer();
  listener.listen(0, "127.0.0.1");
  await once(listener, "listening");
  const address = listener.address();
  assert.ok(address && typeof address !== "string");
  const port = address.port;
  await new Promise<void>((resolve) => listener.close(() => resolve()));
  const child = spawn(process.execPath, ["dist-server/server/index.js"], {
    env: { ...process.env, PORT: String(port) },
    stdio: ["ignore", "pipe", "pipe"],
  });
  const owned = new Set<string>();
  t.after(async () => {
    child.kill("SIGTERM");
    await once(child, "exit");
    await Promise.all([...owned].map((path) => unlink(path).catch(() => {})));
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
    owned.add(`.data/jobs/${id}.json`);
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
    owned.add(`.data/assets/${result.id}.json`);
    owned.add(`.data/uploads/${result.id}.${name.split(".").at(-1)}`);
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
    "CSV and XLSX use the same order and signed-quantity semantics",
    async () => {
      const rows = [
        ["date", "sku", "quantity_ea", "store", "UOM ConversionQty"],
        ["2026-01-01", "A", 4, "S", 0.5],
        ["2026-01-03", "A", -1, "S", 0.5],
        ["2026-01-03", "B", 2, "S", 1],
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
      owned.add(`.data/assets/${restored.data.reference.id}.json`);
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
      owned.add(`.data/workspaces/${workspace.id}.json`);
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
      owned.add(`.data/jobs/${job.id}.json`);
      const cancelled = await fetch(`${base}/api/jobs/${job.id}`, {
        method: "DELETE",
      });
      assert.equal(((await cancelled.json()) as Row).status, "cancelled");
      assert.equal((await request(`/api/jobs/${job.id}`)).status, "cancelled");
    },
  );
});
