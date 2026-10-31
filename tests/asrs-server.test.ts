import test from "node:test";
import assert from "node:assert/strict";
import { spawn, type ChildProcess } from "node:child_process";
import { createServer, request as httpRequest } from "node:http";
import { once } from "node:events";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { WarehouseDatabase } from "../server/database.js";
import { native, physical, sku } from "./fixtures.js";
import { defaultAsrsSettings } from "../shared/asrs.js";
import type { Row } from "../shared/model.js";
import { parse as parseCsv } from "csv-parse/sync";
import { exactQuantity } from "../shared/inventory.js";

test("ASRS connects through the deployed API, pins versions, replays every date mode and recovers a crashed run", async (t) => {
  const folder = await mkdtemp(join(tmpdir(), "layout-asrs-http-"));
  const db = new WarehouseDatabase(join(folder, "app.sqlite"));
  const project = JSON.parse(
    await readFile("resources/map/asrs/asrs_grid.grid.json", "utf8"),
  );
  const rows = [sku("A", 1, 10), sku("B", 1, 10)].map((r) => ({
    ...r,
    minimum_buffer_stock_ea: 3,
  }));
  const layout = native({ command: "generate", project, sku_rows: rows });
  assert.equal(layout.validation.complete, true);
  const warehouse = db.createWorkspace().id,
    dataset = "history";
  db.beginRawImport(dataset, "Selected order history");
  const inputs = [
    {
      source_row: 2,
      date: "2023-01-03",
      sku: "A",
      store: "S1",
      quantity_ea: 3,
    },
    {
      source_row: 3,
      date: "2023-01-03",
      sku: "A",
      store: "S1",
      quantity_ea: 0,
    },
    {
      source_row: 5,
      date: "2023-01-05",
      sku: "B",
      store: "S1",
      quantity_ea: -2,
    },
    {
      source_row: 6,
      date: "2023-02-01",
      sku: "A",
      store: "S2",
      quantity_ea: 12,
    },
    {
      source_row: 8,
      date: "2023-03-02",
      sku: "B",
      store: "S1",
      quantity_ea: 4,
    },
    {
      source_row: 9,
      date: "2023-03-02",
      sku: "MISSING",
      store: "S1",
      quantity_ea: 1,
    },
  ].map((r) => ({
    ...r,
    conversion: 1,
    source_time: "08:15:00",
    uom: "EA",
    order_reference: `ORDER-${r.source_row}`,
  }));
  db.appendOrderLines(dataset, inputs);
  db.putAsset(
    dataset,
    {
      daily: inputs
        .map((r) => ({
          date: r.date,
          sku: r.sku,
          quantity: r.quantity_ea,
          frequency: 1,
        }))
        .filter(
          (r, i, a) =>
            a.findIndex((x) => x.date === r.date && x.sku === r.sku) === i,
        ),
      baskets: inputs
        .map((r) => ({ date: r.date, store: r.store, skus: [r.sku] }))
        .filter(
          (r, i, a) =>
            a.findIndex((x) => x.date === r.date && x.store === r.store) === i,
        ),
    },
    { name: "Selected order history", kind: "orders" },
  );
  db.saveWorkspace(warehouse, {
    name: "ASRS API fixture",
    project,
    velocity: rows,
    stock: rows,
    attributes: { A: physical, B: physical },
    settings: {},
    orders: { id: dataset },
    result: layout,
    _save_revision: 1,
  });
  db.importWorkspaceSkus(warehouse);
  db.syncWarehouse(
    warehouse,
    project,
    native({ command: "geometry", project }),
    "ASRS API fixture",
    "Asia/Kuala_Lumpur",
  );
  const published = db.publish(warehouse, {
    name: "v1",
    expected_revision: 1,
    validation: layout.validation,
  });
  const key = db.createKey({ name: "Remote ASRS read-only", scope: "read" });
  db.close();
  const socket = createServer();
  socket.listen(0, "127.0.0.1");
  await once(socket, "listening");
  const port = (socket.address() as any).port;
  await new Promise<void>((resolve) => socket.close(() => resolve()));
  const base = `http://127.0.0.1:${port}`;
  let child: ChildProcess;
  const start = async () => {
    child = spawn(process.execPath, ["dist-server/server/index.js"], {
      env: { ...process.env, DATA_DIR: folder, PORT: String(port) },
      stdio: ["ignore", "pipe", "pipe"],
    });
    let errors = "";
    child.stderr!.on("data", (b) => (errors += b.toString()));
    await new Promise<void>((resolve, reject) => {
      child.once("error", reject);
      child.stdout!.once("data", () => resolve());
      child.once("exit", (code) =>
        reject(new Error(`Server exited ${code}: ${errors}`)),
      );
    });
  };
  const paths: string[] = [];
  const proxy = createServer(async (req, res) => {
    paths.push(req.url!);
    try {
      assert.equal(req.headers.authorization, `Bearer ${key.token}`);
      const outgoing = httpRequest(
        base + req.url,
        {
          headers: {
            authorization: req.headers.authorization!,
            host: req.headers.host!,
          },
        },
        (response) => {
          res.writeHead(response.statusCode!, {
            "content-type": "application/json",
          });
          response.pipe(res);
        },
      );
      outgoing.on("error", (e) => {
        res.writeHead(502);
        res.end(JSON.stringify({ error: e.message }));
      });
      outgoing.end();
    } catch (e) {
      res.writeHead(500);
      res.end(JSON.stringify({ error: (e as Error).message }));
    }
  });
  proxy.listen(0, "127.0.0.1");
  await once(proxy, "listening");
  const remote = `http://127.0.0.1:${(proxy.address() as any).port}`;
  t.after(async () => {
    proxy.closeAllConnections();
    await new Promise<void>((resolve) => proxy.close(() => resolve()));
    if (child && child.exitCode === null) {
      child.kill("SIGTERM");
      await once(child, "exit");
    }
    await rm(folder, { recursive: true, force: true });
  });
  await start();
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
    const data = (await response.json()) as any;
    assert.ok(response.ok, JSON.stringify(data));
    return data;
  };
  const wait = async (id: string, status: string[]) => {
    for (let i = 0; i < 500; i++) {
      const run = await request(`/api/asrs/runs/${id}`);
      if (status.includes(run.status)) return run;
      if (run.status === "failed") throw new Error(run.progress);
      await delay(20);
    }
    throw new Error("ASRS run did not reach expected state");
  };
  const auth = { Authorization: `Bearer ${key.token}` };
  const calendar = (await (
    await fetch(base + `/api/v1/datasets/${dataset}/calendar`, {
      headers: auth,
    })
  ).json()) as Row;
  assert.deepEqual(calendar.dates, [
    "2023-01-03",
    "2023-01-05",
    "2023-02-01",
    "2023-03-02",
  ]);
  const feed = (await (
    await fetch(
      base + `/api/v1/datasets/${dataset}/line-feed?after=2&limit=2`,
      { headers: auth },
    )
  ).json()) as Row;
  assert.deepEqual(
    feed.rows.map((r: Row) => r.source_row),
    [3, 5],
  );
  assert.equal(feed.next_after, 5);
  assert.equal(feed.done, false);
  assert.equal(
    (
      await fetch(base + `/api/v1/datasets/${dataset}/line-feed?after=-1`, {
        headers: auth,
      })
    ).status,
    400,
  );
  assert.equal(
    (await fetch(base + `/api/v1/datasets/${dataset}/calendar`)).status,
    401,
  );
  const foreignHostStatus = (path: string, token = key.token) =>
    new Promise<number>((resolve, reject) => {
      const outgoing = httpRequest(
        base + path,
        {
          headers: { Authorization: `Bearer ${token}`, host: "localhost:9999" },
        },
        (res) => {
          res.resume();
          resolve(res.statusCode!);
        },
      );
      outgoing.on("error", reject);
      outgoing.end();
    });
  assert.equal(await foreignHostStatus("/api/manage/keys"), 403);
  assert.equal(await foreignHostStatus("/api/v1/warehouses"), 200);
  assert.equal(await foreignHostStatus("/api/v1/warehouses", "invalid"), 401);
  const connection = await request("/api/asrs/connect", {
    base_url: remote,
    token: key.token,
  });
  assert.equal(connection.warehouses[0].published_release_id, published.id);
  const prepare = async (
    selection: Row,
    settings: Row = { speed: "max", skip_idle: true },
    local = false,
  ) => {
    const result = await request("/api/asrs/runs", {
      name: "ASRS replay",
      base_url: local ? base : remote,
      ...(!local ? { token: key.token } : {}),
      warehouse_id: warehouse,
      dataset_id: dataset,
      selection,
      settings: {
        ...defaultAsrsSettings,
        ...settings,
        replenishment_lead_seconds: 2,
      },
    });
    return wait(result.id, ["ready"]);
  };
  const selections = [
    { mode: "one_date", dates: ["2023-01-03"] },
    { mode: "multiple_dates", dates: ["2023-01-03", "2023-02-01"] },
    { mode: "one_month", months: ["2023-01"] },
    { mode: "multiple_months", months: ["2023-01", "2023-03"] },
    { mode: "full" },
  ];
  for (let index = 0; index < selections.length; index++) {
    let run = await prepare(selections[index], undefined, index === 0);
    if (index === 0) {
      await request(`/api/manage/warehouses/${warehouse}/publish`, {
        name: "v2",
        expected_revision: 1,
      });
      const pinned = await request(`/api/asrs/runs/${run.id}/model`);
      assert.equal(pinned.release.id, published.id);
      assert.equal(pinned.release.checksum, published.checksum);
      await request(`/api/asrs/runs/${run.id}/control`, {
        action: "speed",
        speed: 2500,
      });
      await request(`/api/asrs/runs/${run.id}/control`, {
        action: "speed",
        speed: "max",
      });
    }
    await request(`/api/asrs/runs/${run.id}/control`, { action: "resume" });
    run = await wait(run.id, ["completed", "completed_with_shortages"]);
    const orders = (await request(`/api/asrs/runs/${run.id}/orders?limit=5000`))
      .rows;
    assert.equal(orders.length, run.config.input_line_count);
    assert.ok(orders.every((r: Row) => run.config.dates.includes(r.date)));
    const events = (await request(`/api/asrs/runs/${run.id}/events?limit=5000`))
      .rows;
    assert.equal(
      events.reduce((n: number, e: Row) => n + e.delta_micro, 0),
      run.state.onhand_micro,
    );
    assert.equal(events.filter((e: Row) => e.kind === "OPENING").length, 2);
    assert.ok(
      events
        .filter((e: Row) => e.kind === "ORDER_OUT")
        .every(
          (e: Row) =>
            e.source_row && e.order_reference && e.source_time === "08:15:00",
        ),
    );
    assert.ok(
      orders.every((r: Row) =>
        ["completed", "returned", "no_movement", "unfulfilled"].includes(
          r.status,
        ),
      ),
    );
    assert.ok(!JSON.stringify(run.config).includes(key.token));
    const csv = await (
      await fetch(base + `/api/asrs/runs/${run.id}/events.csv?kind=ORDER_OUT`)
    ).text();
    assert.ok(
      csv.includes("source_row") &&
        csv.includes("Asia/Kuala_Lumpur") &&
        csv.includes("ORDER-2"),
    );
    assert.ok(!csv.includes("REPLENISH_REQUEST"));
    const movements = (
      await request(
        `/api/asrs/runs/${run.id}/sku-movements?movement=consume&limit=5000`,
      )
    ).rows;
    assert.equal(
      movements.length,
      events.filter((e: Row) => e.kind === "ORDER_OUT").length,
    );
    assert.ok(
      movements.every(
        (r: Row) =>
          r.movement_type === "consume" &&
          r.sku_before_micro - r.sku_after_micro === r.consumed_micro,
      ),
    );
    const skuCsv = await (
      await fetch(
        base +
          `/api/asrs/runs/${run.id}/sku-movements.csv?movement=consume&latest=1`,
      )
    ).text();
    const exported = parseCsv(skuCsv, { columns: true, bom: true }) as Row[];
    assert.equal(exported.length, movements.length);
    for (let i = 0; i < exported.length; i++) {
      assert.equal(
        exported[i].sku_before_ea,
        exactQuantity(movements[i].sku_before_micro),
      );
      assert.equal(
        exported[i].consumed_ea,
        exactQuantity(movements[i].consumed_micro),
      );
      assert.equal(
        exported[i].sku_after_ea,
        exactQuantity(movements[i].sku_after_micro),
      );
      assert.equal(exported[i].source_row, String(movements[i].source_row));
    }
    assert.equal(
      (
        await fetch(
          base + `/api/asrs/runs/${run.id}/sku-movements?movement=invalid`,
        )
      ).status,
      400,
    );
    assert.equal(
      (
        await fetch(
          base + `/api/asrs/runs/${run.id}/sku-movements.csv?movement=invalid`,
        )
      ).status,
      400,
    );
    if (index === 4) {
      assert.equal(run.state.totals.outbound, 19_000_000);
      assert.equal(run.state.onhand_micro, 13_000_000);
      assert.equal(run.state.totals.unfulfilled, 1);
    }
  }
  assert.ok(paths.some((p) => p.endsWith("/published")));
  assert.ok(paths.some((p) => p.includes("/line-feed?")));
  assert.deepEqual(
    await request(`/api/manage/warehouses/${warehouse}/inventory`),
    [],
  );
  assert.deepEqual(
    await request(`/api/manage/warehouses/${warehouse}/events`),
    [],
  );
  const invalid = await request("/api/asrs/runs", {
    warehouse_id: warehouse,
    dataset_id: dataset,
    selection: { mode: "one_date", dates: ["2023-01-04"] },
    settings: { speed: "max" },
  });
  const invalidRun = await wait(invalid.id, ["failed"]);
  assert.match(invalidRun.progress, /present/);
  let run = await prepare(
    { mode: "full" },
    { speed: 1, skip_idle: false },
    true,
  );
  await request(`/api/asrs/runs/${run.id}/control`, { action: "resume" });
  await delay(100);
  run = await request(`/api/asrs/runs/${run.id}`);
  assert.equal(run.status, "running");
  assert.ok(run.state.now < 2);
  child!.kill("SIGKILL");
  await once(child!, "exit");
  await start();
  const recovered = await request(`/api/asrs/runs/${run.id}`);
  assert.equal(recovered.status, "paused");
  assert.ok(recovered.state.now <= run.state.now);
  await request(`/api/asrs/runs/${run.id}/control`, {
    action: "speed",
    speed: "max",
  });
  await request(`/api/asrs/runs/${run.id}/control`, { action: "resume" });
  const completed = await wait(run.id, ["completed_with_shortages"]);
  assert.equal(completed.state.totals.outbound, 19_000_000);
  assert.equal(
    (await request(`/api/asrs/runs/${run.id}/events?kind=OPENING`)).rows.length,
    2,
  );
  const stopped = await prepare(
    { mode: "one_date", dates: ["2023-01-03"] },
    { speed: 1 },
    true,
  );
  await request(`/api/asrs/runs/${stopped.id}/control`, { action: "stop" });
  assert.equal(
    (await request(`/api/asrs/runs/${stopped.id}`)).status,
    "stopped",
  );
  const keys: Row[] = await request("/api/manage/keys");
  assert.ok(
    keys
      .filter((k) => k.name === "Temporary ASRS simulation reader")
      .every((k) => k.revoked_at),
  );
});
