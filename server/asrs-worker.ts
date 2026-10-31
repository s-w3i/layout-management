import { parentPort, workerData } from "node:worker_threads";
import { createHash } from "node:crypto";
import { performance } from "node:perf_hooks";
import { AsrsDatabase } from "./asrs-database.js";
import { AsrsEngine } from "./asrs-engine.js";
import {
  asrsSettings,
  asrsEngineVersion,
  buildAsrsModel,
  simulationDates,
  validateSpeed,
  safeSum,
  type AsrsModel,
  type SimulationOrder,
} from "../shared/asrs.js";
import { scaledQuantity } from "../shared/inventory.js";
import { canonicalJson } from "./database.js";
import type { Row } from "../shared/model.js";

export async function warehouseRead(
  source: Row,
  path: string,
  signal?: AbortSignal,
) {
  const response = await fetch(source.base_url + path, {
    headers: { Authorization: `Bearer ${source.token}` },
    redirect: "error",
    signal: signal
      ? AbortSignal.any([signal, AbortSignal.timeout(60000)])
      : AbortSignal.timeout(15000),
  });
  if (Number(response.headers.get("content-length")) > 128 * 1024 * 1024)
    throw new Error("Warehouse API response is too large");
  const text = await response.text();
  if (text.length > 128 * 1024 * 1024)
    throw new Error("Warehouse API response is too large");
  let result: any;
  try {
    result = JSON.parse(text);
  } catch {
    throw new Error(
      "Warehouse API returned a non-JSON response; check the deployment URL",
    );
  }
  if (!response.ok)
    throw new Error(
      `Warehouse API (${response.status}): ${result.error ?? response.statusText}`,
    );
  return result;
}

export class AsrsService {
  private database: AsrsDatabase;
  private engine: AsrsEngine | null = null;
  private active = "";
  private config: Row = {};
  private timer: ReturnType<typeof setTimeout> | undefined;
  private anchorWall = 0;
  private anchorSim = 0;
  private runningWall = 0;
  private wallBefore = 0;
  private lastSave = 0;
  private preparing: { id: string; controller: AbortController } | null = null;
  constructor(path: string) {
    this.database = new AsrsDatabase(path);
    try {
      this.database.claimRuntime();
    } catch (error) {
      this.database.db.close();
      throw error;
    }
    this.database.recover();
  }
  list() {
    return this.database.list();
  }
  create(config: Row) {
    const settings = asrsSettings(config.settings);
    const selection = {
      mode: config.selection?.mode,
      dates: config.selection?.dates,
      months: config.selection?.months,
    };
    return this.database.create({
      name: String(config.name || "ASRS simulation").slice(0, 120),
      warehouse_id: String(config.warehouse_id || ""),
      dataset_id: String(config.dataset_id || ""),
      source_url: config.source_url,
      selection,
      settings,
    });
  }
  async prepare(id: string, source: Row) {
    if (this.preparing)
      throw new Error("A simulation is already being prepared");
    const controller = new AbortController();
    this.preparing = { id, controller };
    try {
      const r = this.database.required(id),
        config = JSON.parse(r.config_json);
      const release = await warehouseRead(
        source,
        `/api/v1/warehouses/${encodeURIComponent(config.warehouse_id)}/published`,
        controller.signal,
      );
      const { checksum, ...bundle } = release;
      if (
        createHash("sha256").update(canonicalJson(bundle)).digest("hex") !==
        checksum
      )
        throw new Error("Published release checksum verification failed");
      if (release.warehouse_id !== config.warehouse_id)
        throw new Error("Published release belongs to another warehouse");
      const model = buildAsrsModel(release, config.settings);
      const calendar = await warehouseRead(
        source,
        `/api/v1/datasets/${encodeURIComponent(config.dataset_id)}/calendar`,
        controller.signal,
      );
      if (!calendar.raw_count)
        throw new Error(
          "This history contains aggregates only. Reimport the original order file to retain order-line quantities and references",
        );
      const dates = simulationDates(calendar.dates, config.selection),
        wanted = new Set(dates),
        digest = createHash("sha256");
      let after = 0,
        count = 0,
        quantityTotal = 0;
      while (true) {
        controller.signal.throwIfAborted();
        const page = await warehouseRead(
          source,
          `/api/v1/datasets/${encodeURIComponent(config.dataset_id)}/line-feed?after=${after}&limit=5000&start=${dates[0]}&end=${dates.at(-1)}`,
          controller.signal,
        );
        if (!Array.isArray(page.rows) || page.rows.length > 5000)
          throw new Error("Warehouse API returned an invalid order-line page");
        const rows: SimulationOrder[] = [];
        let previous = after;
        for (const line of page.rows) {
          if (
            !Number.isSafeInteger(line.source_row) ||
            line.source_row <= previous
          )
            throw new Error(
              "Order-line cursor must increase strictly; duplicate or changed source data",
            );
          previous = line.source_row;
          if (!wanted.has(line.date)) continue;
          let quantity: number;
          try {
            quantity = scaledQuantity(String(line.quantity_ea), true);
          } catch (e) {
            throw new Error(
              `Source row ${line.source_row}: ${(e as Error).message}`,
            );
          }
          quantityTotal = safeSum(quantityTotal, Math.abs(quantity));
          const row: SimulationOrder = {
            source_row: line.source_row,
            date: line.date,
            sku: String(line.sku),
            store: String(line.store ?? ""),
            quantity_micro: quantity,
            source_time: String(line.source_time ?? ""),
            uom: String(line.uom ?? ""),
            order_reference: String(line.order_reference ?? ""),
          };
          if (!row.sku)
            throw new Error(`Source row ${line.source_row} has no SKU`);
          rows.push(row);
          digest.update(canonicalJson(row) + "\n");
        }
        if (rows.length) this.database.appendOrders(id, rows);
        count += rows.length;
        this.database.status(
          id,
          "preparing",
          `Copied ${count.toLocaleString()} selected order lines through the warehouse API`,
        );
        if (page.done === true) break;
        if (
          !page.rows.length ||
          !Number.isSafeInteger(page.next_after) ||
          page.next_after !== previous ||
          previous <= after
        )
          throw new Error(
            "Warehouse API order-line pagination did not advance",
          );
        after = previous;
      }
      if (!count)
        throw new Error("No retained order lines exist for the selected dates");
      controller.signal.throwIfAborted();
      Object.assign(config, {
        dates,
        input_line_count: count,
        input_checksum: digest.digest("hex"),
        release_id: release.id,
        release_version: release.version,
        engine_version: asrsEngineVersion,
        release_checksum: checksum,
        warehouse_name: release.warehouse.name,
        time_zone: release.warehouse.time_zone,
        wall_seconds: 0,
      });
      this.database.transaction(() => {
        this.database.init(id, release, model);
        this.database.config(id, config);
        const engine = new AsrsEngine(
          model,
          dates,
          config.settings,
          (day) => this.database.orders(id, day),
          this.database.sink(id),
        );
        this.database.save(
          id,
          engine.checkpoint(),
          this.summary(engine, config, 0),
        );
        this.database.status(
          id,
          "ready",
          "Ready: published release pinned and opening stock recorded. Start when ready",
        );
      });
      return this.database.run(id);
    } catch (e) {
      if (this.database.required(id).status !== "stopped")
        this.database.status(id, "failed", (e as Error).message);
      throw e;
    } finally {
      this.preparing = null;
    }
  }
  private summary(engine: AsrsEngine, config: Row, wall: number) {
    return {
      ...engine.snapshot(),
      load_quantities: engine.model.loads.map(
        (l) => engine.loads[l.id].quantity,
      ),
      wall_seconds: wall,
      lines_per_second:
        wall > 0
          ? (engine.totals.completed +
              engine.totals.returned +
              engine.totals.zero +
              engine.totals.unfulfilled) /
            wall
          : 0,
      input_line_count: config.input_line_count,
      time_zone: config.time_zone,
      requested_speed: config.settings.speed,
    };
  }
  private wall() {
    return this.wallBefore + (performance.now() - this.runningWall) / 1000;
  }
  private persist() {
    if (!this.engine) return;
    this.config.wall_seconds = this.wall();
    this.database.config(this.active, this.config);
    this.database.save(
      this.active,
      this.engine.checkpoint(),
      this.summary(this.engine, this.config, this.config.wall_seconds),
    );
    this.lastSave = performance.now();
  }
  private loop = () => {
    if (!this.engine || !this.active) return;
    const engine = this.engine,
      settings = engine.settings,
      began = performance.now();
    let target =
      settings.speed === "max"
        ? Infinity
        : this.anchorSim + ((began - this.anchorWall) / 1000) * settings.speed;
    let changed = false;
    try {
      this.database.transaction(() => {
        while (!engine.finished && performance.now() - began < 80) {
          if (
            settings.skip_idle &&
            engine.idle &&
            Number.isFinite(engine.nextTime) &&
            engine.nextTime > target
          ) {
            target = engine.nextTime;
            this.anchorSim = target;
            this.anchorWall = performance.now();
          }
          if (engine.nextTime > target) break;
          engine.step();
          changed = true;
        }
        if (
          Number.isFinite(target) &&
          target > engine.now &&
          target < engine.nextTime &&
          !engine.finished
        )
          engine.advanceClock(target);
        if (
          changed ||
          performance.now() - this.lastSave >= 500 ||
          engine.finished
        )
          this.persist();
        if (engine.finished)
          this.database.status(
            this.active,
            engine.totals.unfulfilled
              ? "completed_with_shortages"
              : "completed",
            engine.totals.unfulfilled
              ? "Completed; some order lines had no accessible assigned stock"
              : "Completed; all selected source lines accounted for",
          );
      });
      if (engine.finished) {
        this.engine = null;
        this.active = "";
        return;
      }
      this.timer = setTimeout(
        this.loop,
        settings.speed === "max" || engine.nextTime <= target ? 0 : 15,
      );
    } catch (e) {
      this.database.status(
        this.active,
        "failed",
        `Run stopped: ${(e as Error).message}. Committed stock-flow records remain available`,
      );
      this.engine = null;
      this.active = "";
    }
  };
  control(id: string, command: Row) {
    const run = this.database.run(id);
    if (command.action === "stop" && run.status === "preparing") {
      this.preparing?.controller.abort();
      this.database.status(id, "stopped", "Preparation stopped");
      return this.database.run(id);
    }
    if (!["resume", "pause", "stop", "speed"].includes(command.action))
      throw new Error("Unknown simulation control");
    if (!["ready", "paused", "running"].includes(run.status))
      throw new Error("This run is finished or unavailable; prepare a new run");
    if (command.action === "resume") {
      if (run.status === "running") return this.run(id);
      if (this.active || this.preparing)
        throw new Error(
          "Pause the active simulation before resuming another run",
        );
      const r = this.database.required(id, true),
        config = JSON.parse(r.config_json),
        model: AsrsModel = JSON.parse(r.model_json);
      if (config.engine_version !== asrsEngineVersion)
        throw new Error(
          "This run uses a different simulation engine version; prepare a new run to use this engine",
        );
      this.config = config;
      this.active = id;
      this.wallBefore = config.wall_seconds || 0;
      this.runningWall = this.anchorWall = performance.now();
      this.engine = new AsrsEngine(
        model,
        config.dates,
        config.settings,
        (day) => this.database.orders(id, day),
        this.database.sink(id),
        JSON.parse(r.checkpoint_json),
      );
      this.anchorSim = this.engine.now;
      this.database.status(id, "running", "Simulation running");
      this.timer = setTimeout(this.loop, 0);
    } else if (command.action === "speed") {
      const speed = validateSpeed(command.speed),
        skipIdle = command.skip_idle;
      if (skipIdle !== undefined && typeof skipIdle !== "boolean")
        throw new Error("Skip idle must be true or false");
      run.config.settings.speed = speed;
      if (skipIdle !== undefined) run.config.settings.skip_idle = skipIdle;
      this.database.config(id, run.config);
      if (id === this.active && this.engine) {
        this.engine.settings.speed = speed;
        if (skipIdle !== undefined) this.engine.settings.skip_idle = skipIdle;
        this.config = run.config;
        this.anchorSim = this.engine.now;
        this.anchorWall = performance.now();
      }
    } else {
      if (id === this.active && this.engine) {
        clearTimeout(this.timer);
        this.database.transaction(() => this.persist());
        this.engine = null;
        this.active = "";
      }
      this.database.status(
        id,
        command.action === "stop" ? "stopped" : "paused",
        command.action === "stop"
          ? "Stopped; this run is incomplete and its ledger is retained"
          : "Paused; resume from this exact state",
      );
    }
    return this.run(id);
  }
  run(id: string) {
    const run = this.database.run(id);
    if (this.active === id && this.engine)
      run.state = this.summary(this.engine, this.config, this.wall());
    return run;
  }
  model(id: string) {
    const r = this.database.required(id, true);
    if (!r.model_json)
      throw new Error("Simulation configuration is still being prepared");
    return {
      release: JSON.parse(r.release_json),
      model: JSON.parse(r.model_json),
    };
  }
  page(id: string, kind: string, query: Row) {
    return this.database.page(id, kind, query);
  }
  stock(id: string, query: Row) {
    return this.database.stock(id, query);
  }
  shutdown() {
    this.preparing?.controller.abort();
    if (this.active) this.control(this.active, { action: "pause" });
  }
  close() {
    this.shutdown();
    this.database.releaseRuntime();
    this.database.db.close();
  }
}

if (parentPort) {
  const service = new AsrsService(workerData.path);
  parentPort.on("message", async (m) => {
    try {
      const allowed = [
        "list",
        "create",
        "prepare",
        "run",
        "model",
        "control",
        "page",
        "stock",
        "shutdown",
        "close",
      ];
      if (!allowed.includes(m.method))
        throw new Error("Unknown simulation operation");
      const result = await (service as any)[m.method](...m.args);
      parentPort!.postMessage({ id: m.id, result });
    } catch (error) {
      parentPort!.postMessage({
        id: m.id,
        error: (error as Error).message,
        status: (error as any).status,
      });
    }
  });
  parentPort.postMessage({ ready: true });
}
