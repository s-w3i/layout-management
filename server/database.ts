import { DatabaseSync } from "node:sqlite";
import { createHash, randomBytes, randomUUID } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, readdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import {
  scaledQuantity,
  quantityScale,
  stockStatuses,
  exactQuantity,
} from "../shared/inventory.js";
import type { Row } from "../shared/model.js";
import { orderDateBounds, validDate } from "../shared/dates.js";
import { createTaskId, taskTypeForId } from "../shared/tasks.js";

export class DataError extends Error {
  constructor(
    message: string,
    public status = 400,
  ) {
    super(message);
  }
}
const encode = (value: unknown) => JSON.stringify(value);
const decode = (value: unknown) => JSON.parse(String(value));
export function canonicalJson(value: any): string {
  if (Array.isArray(value))
    return `[${value.map((v) => canonicalJson(v)).join(",")}]`;
  if (value && typeof value === "object")
    return `{${Object.keys(value)
      .filter((k) => value[k] !== undefined)
      .sort()
      .map((k) => `${encode(k)}:${canonicalJson(value[k])}`)
      .join(",")}}`;
  return encode(value);
}
const hash = (value: unknown) =>
  createHash("sha256")
    .update(typeof value === "string" ? value : canonicalJson(value))
    .digest("hex");
function text(value: unknown, name: string, max = 200): string {
  if (
    typeof value !== "string" ||
    !value.trim() ||
    value.length > max ||
    /[\x00-\x1f]/.test(value)
  )
    throw new DataError(`${name} is required (maximum ${max} characters)`);
  return value.trim();
}
function object(value: unknown, name: string): Row {
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw new DataError(`${name} must be an object`);
  return value as Row;
}
function positive(value: unknown, name: string): number {
  if (typeof value !== "number" || !Number.isFinite(value) || value <= 0)
    throw new DataError(`${name} must be positive`);
  return value;
}

export class WarehouseDatabase {
  readonly db: DatabaseSync;
  private transactionDepth = 0;
  private integrity: { value: string; at: string } | null = null;
  private warehouseCache = new Map<
    string,
    { key: string; project: Row; formatter: Intl.DateTimeFormat }
  >();
  private timestamp() {
    return this.clock().toISOString();
  }
  constructor(
    path: string,
    private clock: () => Date = () => new Date(),
  ) {
    if (path !== ":memory:") mkdirSync(dirname(path), { recursive: true });
    this.db = new DatabaseSync(path);
    const sqliteVersion = String(
      (this.db.prepare("SELECT sqlite_version() AS version").get() as Row)
        .version,
    )
      .split(".")
      .map(Number);
    if (
      sqliteVersion[0] < 3 ||
      (sqliteVersion[0] === 3 &&
        (sqliteVersion[1] < 51 ||
          (sqliteVersion[1] === 51 && sqliteVersion[2] < 3)))
    ) {
      this.db.close();
      throw new Error(
        "SQLite 3.51.3 or later is required. Upgrade the Node.js runtime to a build with a patched SQLite library.",
      );
    }
    this.db.exec(
      "PRAGMA foreign_keys=ON; PRAGMA busy_timeout=10000; PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;",
    );
    const version = Number(
      (this.db.prepare("PRAGMA user_version").get() as Row).user_version,
    );
    if (version > 1)
      throw new Error("Database schema is newer than this application");
    if (!version)
      this.transaction(() => {
        this.db.exec(`
        CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE input_sets(id TEXT PRIMARY KEY);
        CREATE TABLE planning_skus(input_id TEXT NOT NULL REFERENCES input_sets(id),sku TEXT NOT NULL,velocity_json TEXT,stock_json TEXT,attributes_json TEXT,ordinal INTEGER NOT NULL,stock_ordinal INTEGER,PRIMARY KEY(input_id,sku));
        CREATE TABLE workspaces(id TEXT PRIMARY KEY,name TEXT NOT NULL,state_json TEXT NOT NULL,input_id TEXT REFERENCES input_sets(id),result_id TEXT,revision INTEGER NOT NULL DEFAULT 0,updated_at TEXT NOT NULL);
        CREATE TABLE runs(id TEXT PRIMARY KEY,workspace_id TEXT REFERENCES workspaces(id),result_hash TEXT NOT NULL,result_json TEXT NOT NULL,input_json TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(workspace_id,result_hash));
        CREATE TABLE datasets(id TEXT PRIMARY KEY,kind TEXT NOT NULL,name TEXT NOT NULL,status TEXT NOT NULL,payload_json TEXT,metadata_json TEXT NOT NULL DEFAULT '{}',source_path TEXT,sha256 TEXT,raw_count INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL);
        CREATE TABLE demand_daily(dataset_id TEXT NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,ordinal INTEGER NOT NULL,date TEXT NOT NULL,sku TEXT NOT NULL,payload_json TEXT NOT NULL,PRIMARY KEY(dataset_id,date,sku));
        CREATE TABLE demand_baskets(dataset_id TEXT NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,ordinal INTEGER NOT NULL,date TEXT NOT NULL,store TEXT NOT NULL,skus_json TEXT NOT NULL,PRIMARY KEY(dataset_id,date,store));
        CREATE TABLE order_lines(dataset_id TEXT NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,source_row INTEGER NOT NULL,date TEXT NOT NULL,sku TEXT NOT NULL,store TEXT NOT NULL,quantity_ea REAL NOT NULL,conversion REAL NOT NULL,source_time TEXT NOT NULL,uom TEXT NOT NULL,order_reference TEXT NOT NULL,PRIMARY KEY(dataset_id,source_row));
        CREATE INDEX order_lookup ON order_lines(dataset_id,date,sku);
        CREATE TABLE jobs(id TEXT PRIMARY KEY,status TEXT NOT NULL,payload_json TEXT NOT NULL,result_json TEXT,created_at TEXT NOT NULL);
        CREATE TABLE skus(sku TEXT PRIMARY KEY,name TEXT NOT NULL,base_uom TEXT NOT NULL,attributes_json TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1,revision INTEGER NOT NULL DEFAULT 1,updated_at TEXT NOT NULL);
        CREATE TABLE sku_uoms(sku TEXT NOT NULL REFERENCES skus(sku),uom TEXT NOT NULL,factor_micro INTEGER NOT NULL CHECK(factor_micro>0),PRIMARY KEY(sku,uom));
        CREATE TABLE partners(id TEXT PRIMARY KEY,kind TEXT NOT NULL CHECK(kind IN ('store','supplier')),name TEXT NOT NULL,details_json TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE warehouses(id TEXT PRIMARY KEY REFERENCES workspaces(id),name TEXT NOT NULL,project_json TEXT NOT NULL,geometry_json TEXT NOT NULL,version_hash TEXT NOT NULL,updated_at TEXT NOT NULL,time_zone TEXT NOT NULL);
        CREATE TABLE locations(warehouse_id TEXT NOT NULL REFERENCES warehouses(id),id TEXT NOT NULL,name TEXT NOT NULL,kind TEXT NOT NULL,rack_id TEXT,level INTEGER,slot INTEGER,active INTEGER NOT NULL DEFAULT 1,details_json TEXT NOT NULL,PRIMARY KEY(warehouse_id,id));
        CREATE TABLE equipment(id TEXT PRIMARY KEY,warehouse_id TEXT NOT NULL REFERENCES warehouses(id),kind TEXT NOT NULL CHECK(kind IN ('AMR','ASRS')),name TEXT NOT NULL,details_json TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE containers(warehouse_id TEXT NOT NULL REFERENCES warehouses(id),code TEXT NOT NULL,location_id TEXT NOT NULL,PRIMARY KEY(warehouse_id,code),FOREIGN KEY(warehouse_id,location_id) REFERENCES locations(warehouse_id,id));
        CREATE TABLE positions(id TEXT PRIMARY KEY,warehouse_id TEXT NOT NULL,sku TEXT NOT NULL REFERENCES skus(sku),location_id TEXT NOT NULL,lot TEXT NOT NULL,expiry TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN ('available','quarantine','damaged','blocked')),container TEXT NOT NULL,quantity_micro INTEGER NOT NULL CHECK(quantity_micro>=0 AND quantity_micro<=9007199254740991),reserved_micro INTEGER NOT NULL DEFAULT 0 CHECK(reserved_micro>=0 AND reserved_micro<=quantity_micro),revision INTEGER NOT NULL DEFAULT 1,FOREIGN KEY(warehouse_id,location_id) REFERENCES locations(warehouse_id,id),UNIQUE(warehouse_id,sku,location_id,lot,expiry,status,container));
        CREATE INDEX stock_lookup ON positions(warehouse_id,sku,location_id);
        CREATE TABLE reservations(id TEXT PRIMARY KEY,position_id TEXT NOT NULL REFERENCES positions(id),quantity_micro INTEGER NOT NULL CHECK(quantity_micro>0),reference TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN ('active','dispatched','cancelled')),created_at TEXT NOT NULL);
        CREATE TABLE movement_tasks(id TEXT PRIMARY KEY,warehouse_id TEXT NOT NULL REFERENCES warehouses(id),source_position_id TEXT NOT NULL REFERENCES positions(id),destination TEXT NOT NULL,reservation_id TEXT NOT NULL REFERENCES reservations(id),transit_position_id TEXT REFERENCES positions(id),quantity_micro INTEGER NOT NULL,status TEXT NOT NULL CHECK(status IN ('reserved','in_transit','completed','cancelled','failed')),reference TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,FOREIGN KEY(warehouse_id,destination) REFERENCES locations(warehouse_id,id));
        CREATE TABLE task_items(task_id TEXT NOT NULL REFERENCES movement_tasks(id),position_id TEXT NOT NULL REFERENCES positions(id),reservation_id TEXT NOT NULL REFERENCES reservations(id),quantity_micro INTEGER NOT NULL,transit_position_id TEXT REFERENCES positions(id),PRIMARY KEY(task_id,position_id));
        CREATE TABLE stock_events(id TEXT PRIMARY KEY,request_id TEXT NOT NULL UNIQUE,request_hash TEXT NOT NULL,warehouse_id TEXT NOT NULL REFERENCES warehouses(id),kind TEXT NOT NULL,actor TEXT NOT NULL,reason TEXT NOT NULL,payload_json TEXT NOT NULL,result_json TEXT NOT NULL,created_at TEXT NOT NULL);
        CREATE TABLE stock_event_lines(event_id TEXT NOT NULL REFERENCES stock_events(id),position_id TEXT NOT NULL REFERENCES positions(id),quantity_delta_micro INTEGER NOT NULL,reserved_delta_micro INTEGER NOT NULL);
        CREATE TABLE releases(id TEXT PRIMARY KEY,warehouse_id TEXT NOT NULL REFERENCES warehouses(id),version INTEGER NOT NULL,name TEXT NOT NULL,bundle_json TEXT NOT NULL,checksum TEXT NOT NULL,published_at TEXT NOT NULL,UNIQUE(warehouse_id,version));
        CREATE TABLE publications(warehouse_id TEXT PRIMARY KEY REFERENCES warehouses(id),release_id TEXT NOT NULL REFERENCES releases(id));
        CREATE TABLE api_keys(id TEXT PRIMARY KEY,name TEXT NOT NULL,token_hash TEXT NOT NULL UNIQUE,scope TEXT NOT NULL CHECK(scope IN ('read','operate')),created_at TEXT NOT NULL,revoked_at TEXT);
        CREATE TRIGGER immutable_event_update BEFORE UPDATE ON stock_events BEGIN SELECT RAISE(ABORT,'Stock events are immutable'); END;
        CREATE TRIGGER immutable_event_delete BEFORE DELETE ON stock_events BEGIN SELECT RAISE(ABORT,'Stock events are immutable'); END;
        CREATE TRIGGER immutable_lines_update BEFORE UPDATE ON stock_event_lines BEGIN SELECT RAISE(ABORT,'Stock event lines are immutable'); END;
        CREATE TRIGGER immutable_lines_delete BEFORE DELETE ON stock_event_lines BEGIN SELECT RAISE(ABORT,'Stock event lines are immutable'); END;
        CREATE TRIGGER immutable_release_update BEFORE UPDATE ON releases BEGIN SELECT RAISE(ABORT,'Published releases are immutable'); END;
        CREATE TRIGGER immutable_release_delete BEFORE DELETE ON releases BEGIN SELECT RAISE(ABORT,'Published releases are immutable'); END;
        PRAGMA user_version=1;
      `);
      });
  }
  close() {
    this.db.close();
  }
  transaction<T>(operation: () => T, write = true): T {
    if (this.transactionDepth) return operation();
    this.db.exec(write ? "BEGIN IMMEDIATE" : "BEGIN");
    this.transactionDepth++;
    try {
      const result = operation();
      this.db.exec("COMMIT");
      return result;
    } catch (error) {
      this.db.exec("ROLLBACK");
      throw error;
    } finally {
      this.transactionDepth--;
    }
  }
  private get(sql: string, ...args: any[]): Row | undefined {
    return this.db.prepare(sql).get(...args) as Row | undefined;
  }
  private all(sql: string, ...args: any[]): Row[] {
    return this.db.prepare(sql).all(...args) as Row[];
  }
  private run(sql: string, ...args: any[]) {
    return this.db.prepare(sql).run(...args);
  }
  private required(sql: string, ...args: any[]): Row {
    const row = this.get(sql, ...args);
    if (!row) throw new DataError("Record not found", 404);
    return row;
  }
  private auditRevision(row: Row, revision: unknown) {
    if (!Number.isSafeInteger(revision) || row.revision !== revision)
      throw new DataError("Record changed. Refresh before saving.", 409);
  }
  migrateLegacy(root: string) {
    if (this.get("SELECT 1 FROM metadata WHERE key='legacy_migration'")) return;
    const folder = resolve(root, "workspaces"),
      assetMetadata = new Map<string, Row>();
    if (existsSync(folder))
      for (const file of readdirSync(folder).filter((f) =>
        /^[a-f0-9-]{36}\.json$/.test(f),
      )) {
        const id = file.slice(0, -5);
        if (!this.get("SELECT 1 FROM workspaces WHERE id=?", id))
          this.createWorkspace(id);
        const state = decode(readFileSync(resolve(folder, file), "utf8"));
        if (state.orders?.id)
          assetMetadata.set(state.orders.id, {
            ...state.orders,
            kind: "orders",
            source_level: "aggregated",
          });
        if (state.reference?.id)
          assetMetadata.set(state.reference.id, {
            ...state.reference,
            kind: "reference",
          });
        if (
          Object.keys(state).length &&
          !this.get("SELECT 1 FROM workspaces WHERE id=? AND revision>0", id)
        )
          this.saveWorkspace(id, {
            ...state,
            _save_revision: Math.max(1, state._save_revision || 0),
          });
      }
    const assets = resolve(root, "assets");
    if (existsSync(assets))
      for (const file of readdirSync(assets).filter((f) =>
        /^[a-f0-9-]{36}\.json$/.test(f),
      )) {
        const id = file.slice(0, -5);
        if (this.get("SELECT 1 FROM datasets WHERE id=?", id)) continue;
        const value = decode(readFileSync(resolve(assets, file), "utf8"));
        this.putAsset(id, value, {
          name: value.name || file,
          kind: value.daily
            ? "orders"
            : value.kind || (value.data?.assignments ? "reference" : "legacy"),
          ...assetMetadata.get(id),
        });
      }
    this.run(
      "INSERT INTO metadata VALUES('legacy_migration',?)",
      this.timestamp(),
    );
  }
  recoverJobs() {
    for (const row of this.all(
      "SELECT * FROM jobs WHERE status IN ('running','queued')",
    )) {
      const job = {
        ...decode(row.payload_json),
        status: "failed",
        error: "Server restarted before this job completed",
      };
      this.saveJob(job);
    }
    this.run(
      "UPDATE datasets SET status='interrupted' WHERE status='importing'",
    );
  }
  createWorkspace(id: string = randomUUID()) {
    this.run(
      "INSERT INTO workspaces(id,name,state_json,updated_at) VALUES(?,?,?,?)",
      id,
      "Untitled warehouse",
      "{}",
      this.timestamp(),
    );
    return { id };
  }
  listWorkspaces() {
    return this.all(
      "SELECT id,name,revision,updated_at FROM workspaces ORDER BY updated_at DESC",
    );
  }
  getWorkspace(id: string): Row {
    const row = this.required("SELECT * FROM workspaces WHERE id=?", id),
      state = decode(row.state_json);
    if (!row.revision) return state;
    state.velocity = [];
    state.stock = [];
    state.attributes = {};
    for (const input of this.all(
      "SELECT * FROM planning_skus WHERE input_id=? ORDER BY ordinal",
      row.input_id,
    )) {
      if (input.velocity_json) state.velocity.push(decode(input.velocity_json));
      if (input.attributes_json)
        Object.defineProperty(state.attributes, input.sku, {
          value: decode(input.attributes_json),
          enumerable: true,
          writable: true,
          configurable: true,
        });
    }
    state.stock = this.all(
      "SELECT stock_json FROM planning_skus WHERE input_id=? AND stock_json IS NOT NULL ORDER BY stock_ordinal",
      row.input_id,
    ).map((r) => decode(r.stock_json));
    state.result = row.result_id
      ? decode(
          this.required(
            "SELECT result_json FROM runs WHERE id=?",
            row.result_id,
          ).result_json,
        )
      : null;
    state._save_revision = row.revision;
    return state;
  }
  saveWorkspace(id: string, state: Row) {
    if (!Number.isSafeInteger(state._save_revision) || state._save_revision < 1)
      throw new DataError("A positive workspace revision is required");
    const inputs = {
      velocity: state.velocity || [],
      stock: state.stock || [],
      attributes: state.attributes || {},
    };
    if (!Array.isArray(inputs.velocity) || !Array.isArray(inputs.stock))
      throw new DataError("Invalid workspace SKU rows");
    const inputId = hash(inputs);
    return this.transaction(() => {
      const previous = this.required(
        "SELECT revision FROM workspaces WHERE id=?",
        id,
      );
      if (previous.revision >= state._save_revision)
        throw new DataError(
          "This workspace has a newer saved version. Reload it or export a copy of your changes.",
          409,
        );
      if (!this.get("SELECT 1 FROM input_sets WHERE id=?", inputId)) {
        this.run("INSERT INTO input_sets VALUES(?)", inputId);
        const rows = new Map<string, Row>();
        let ordinal = 0;
        for (const [kind, values] of [
          ["velocity_json", inputs.velocity],
          ["stock_json", inputs.stock],
        ] as const) {
          const seen = new Set<string>();
          for (const [i, value] of values.entries()) {
            const sku = text(value.sku, "SKU");
            if (seen.has(sku))
              throw new DataError("Duplicate SKU in workspace inputs");
            seen.add(sku);
            const row = rows.get(sku) || { ordinal: ordinal++ };
            row[kind] = encode(value);
            if (kind === "stock_json") row.stock_ordinal = i;
            rows.set(sku, row);
          }
        }
        for (const [sku, value] of Object.entries(inputs.attributes)) {
          const row = rows.get(sku) || { ordinal: ordinal++ };
          row.attributes_json = encode(value);
          rows.set(sku, row);
        }
        const insert = this.db.prepare(
          "INSERT INTO planning_skus VALUES(?,?,?,?,?,?,?)",
        );
        for (const [sku, row] of rows)
          insert.run(
            inputId,
            sku,
            row.velocity_json || null,
            row.stock_json || null,
            row.attributes_json || null,
            row.ordinal,
            row.stock_ordinal ?? null,
          );
      }
      let resultId: string | null = null;
      if (state.result) {
        const resultHash = hash(state.result),
          existing = this.get(
            "SELECT id FROM runs WHERE workspace_id=? AND result_hash=?",
            id,
            resultHash,
          );
        resultId = existing?.id || randomUUID();
        if (!existing)
          this.run(
            "INSERT INTO runs VALUES(?,?,?,?,?,?)",
            resultId,
            id,
            resultHash,
            encode(state.result),
            encode({
              project: state.project,
              ...inputs,
              settings: state.settings,
              orders: state.orders,
              reference: state.reference,
            }),
            this.timestamp(),
          );
      }
      const thin = { ...state };
      for (const key of [
        "velocity",
        "stock",
        "attributes",
        "result",
        "orders_data",
        "reference_data",
      ])
        delete thin[key];
      this.run(
        "UPDATE workspaces SET name=?,state_json=?,input_id=?,result_id=?,revision=?,updated_at=? WHERE id=?",
        String(state.name || "Untitled warehouse"),
        encode(thin),
        inputId,
        resultId,
        state._save_revision,
        this.timestamp(),
        id,
      );
      return { saved_at: this.timestamp(), revision: state._save_revision };
    });
  }
  beginRawImport(id: string, name: string) {
    this.run(
      "INSERT INTO datasets(id,kind,name,status,created_at) VALUES(?,'orders',?,'importing',?)",
      id,
      name,
      this.timestamp(),
    );
  }
  appendOrderLines(id: string, rows: Row[]) {
    this.transaction(() => {
      const insert = this.db.prepare(
        "INSERT INTO order_lines VALUES(?,?,?,?,?,?,?,?,?,?)",
      );
      for (const r of rows)
        insert.run(
          id,
          r.source_row,
          r.date,
          r.sku,
          r.store,
          r.quantity_ea,
          r.conversion,
          r.source_time || "",
          r.uom || "",
          r.order_reference || "",
        );
      this.run(
        "UPDATE datasets SET raw_count=raw_count+? WHERE id=? AND status='importing'",
        rows.length,
        id,
      );
    });
  }
  failDataset(id: string) {
    this.run(
      "UPDATE datasets SET status='failed' WHERE id=? AND status='importing'",
      id,
    );
  }
  putAsset(id: string, value: Row, meta: Row = {}) {
    if (this.get("SELECT 1 FROM datasets WHERE id=? AND status='ready'", id))
      return;
    return this.transaction(() => {
      this.run(
        "INSERT INTO datasets(id,kind,name,status,created_at) VALUES(?,?,?,'importing',?) ON CONFLICT(id) DO NOTHING",
        id,
        meta.kind || "orders",
        meta.name || id,
        this.timestamp(),
      );
      if (Array.isArray(value.daily) && Array.isArray(value.baskets)) {
        meta = { ...meta, ...orderDateBounds(value) };
        if (!meta.stores)
          meta.stores = [
            ...new Set(value.baskets.map((r: Row) => r.store)),
          ].sort();
        if (meta.sku_count == null)
          meta.sku_count = new Set(value.daily.map((r: Row) => r.sku)).size;
        if (meta.store_count == null) meta.store_count = meta.stores.length;
        if (meta.store_day_count == null)
          meta.store_day_count = value.baskets.length;
        if (meta.line_count == null)
          meta.line_count = value.daily.reduce(
            (n: number, r: Row) => n + Number(r.frequency || 0),
            0,
          );
        this.run("DELETE FROM demand_daily WHERE dataset_id=?", id);
        this.run("DELETE FROM demand_baskets WHERE dataset_id=?", id);
        const daily = this.db.prepare(
            "INSERT INTO demand_daily VALUES(?,?,?,?,?)",
          ),
          baskets = this.db.prepare(
            "INSERT INTO demand_baskets VALUES(?,?,?,?,?)",
          );
        value.daily.forEach((r: Row, i: number) =>
          daily.run(id, i, r.date, r.sku, encode(r)),
        );
        value.baskets.forEach((r: Row, i: number) =>
          baskets.run(id, i, r.date, r.store, encode(r.skus)),
        );
      }
      this.run(
        "UPDATE datasets SET kind=?,name=?,status='ready',payload_json=?,metadata_json=?,source_path=?,sha256=? WHERE id=?",
        meta.kind || "orders",
        meta.name || id,
        value.daily ? null : encode(value),
        encode(meta),
        meta.source_path || null,
        meta.sha256 || null,
        id,
      );
    });
  }
  getAsset(id: string): Row {
    const row = this.required(
      "SELECT * FROM datasets WHERE id=? AND status='ready'",
      id,
    );
    if (row.payload_json) return decode(row.payload_json);
    return {
      daily: this.all(
        "SELECT payload_json FROM demand_daily WHERE dataset_id=? ORDER BY ordinal",
        id,
      ).map((r) => decode(r.payload_json)),
      baskets: this.all(
        "SELECT date,store,skus_json FROM demand_baskets WHERE dataset_id=? ORDER BY ordinal",
        id,
      ).map((r) => ({
        date: r.date,
        store: r.store,
        skus: decode(r.skus_json),
      })),
    };
  }
  hasAsset(id: string) {
    return Boolean(
      this.get("SELECT 1 FROM datasets WHERE id=? AND status='ready'", id),
    );
  }
  listDatasets(): Row[] {
    return this.all(
      "SELECT id,kind,name,status,raw_count,metadata_json,sha256,created_at FROM datasets ORDER BY created_at DESC",
    ).map((r) => ({
      ...r,
      metadata: decode(r.metadata_json),
      metadata_json: undefined,
    }));
  }
  datasetCalendar(id: string) {
    const dataset = this.required(
      "SELECT raw_count FROM datasets WHERE id=? AND kind='orders' AND status='ready'",
      id,
    );
    const dates = this.all(
      "SELECT DISTINCT date FROM demand_daily WHERE dataset_id=? ORDER BY date",
      id,
    ).map((r) => r.date);
    return {
      dataset_id: id,
      raw_count: dataset.raw_count,
      dates,
      months: [...new Set(dates.map((d) => d.slice(0, 7)))],
    };
  }
  orderLineFeed(id: string, query: Row = {}) {
    this.required(
      "SELECT 1 FROM datasets WHERE id=? AND kind='orders' AND status='ready' AND raw_count>0",
      id,
    );
    const after = Number(query.after ?? 0),
      limit = Number(query.limit ?? 5000);
    if (
      !Number.isSafeInteger(after) ||
      after < 0 ||
      !Number.isInteger(limit) ||
      limit < 1 ||
      limit > 5000
    )
      throw new DataError(
        "Use a nonnegative source-row cursor and a limit between 1 and 5000",
      );
    for (const key of ["start", "end"])
      if (query[key] && !validDate(query[key]))
        throw new DataError("Invalid calendar date");
    if (query.start && query.end && query.start > query.end)
      throw new DataError("Start must be on or before end");
    const rows = this.all(
      "SELECT * FROM order_lines INDEXED BY sqlite_autoindex_order_lines_1 WHERE dataset_id=? AND source_row>? AND (?='' OR date>=?) AND (?='' OR date<=?) ORDER BY source_row LIMIT ?",
      id,
      after,
      query.start || "",
      query.start || "",
      query.end || "",
      query.end || "",
      limit,
    );
    return {
      rows,
      next_after: rows.at(-1)?.source_row ?? after,
      done: rows.length < limit,
    };
  }
  orderLines(id: string, query: Row = {}) {
    this.required("SELECT 1 FROM datasets WHERE id=? AND status='ready'", id);
    const limit = Math.min(500, Math.max(1, Number(query.limit) || 100)),
      offset = Math.max(0, Number(query.offset) || 0);
    const where =
      "dataset_id=? AND (?='' OR sku=?) AND (?='' OR date>=?) AND (?='' OR date<=?)";
    const args = [
      id,
      query.sku || "",
      query.sku || "",
      query.start || "",
      query.start || "",
      query.end || "",
      query.end || "",
    ];
    return {
      rows: this.all(
        `SELECT * FROM order_lines WHERE ${where} ORDER BY source_row LIMIT ? OFFSET ?`,
        ...args,
        limit,
        offset,
      ),
      total: this.required(
        `SELECT count(*) AS n FROM order_lines WHERE ${where}`,
        ...args,
      ).n,
      limit,
      offset,
    };
  }
  datasetRows(id: string, kind: string, query: Row = {}) {
    if (kind === "lines") return this.orderLines(id, query);
    if (!["daily", "baskets"].includes(kind))
      throw new DataError("Unknown dataset view");
    this.required(
      "SELECT 1 FROM datasets WHERE id=? AND kind='orders' AND status='ready'",
      id,
    );
    for (const field of ["start", "end"])
      if (query[field] && !validDate(query[field]))
        throw new DataError("Use valid calendar dates for dataset filters");
    if (query.start && query.end && query.start > query.end)
      throw new DataError("Start must be on or before end");
    const table = kind === "daily" ? "demand_daily" : "demand_baskets",
      limit = Math.min(
        500,
        Math.max(1, Math.trunc(Number(query.limit) || 100)),
      ),
      offset = Math.max(0, Math.trunc(Number(query.offset) || 0));
    const where =
      "dataset_id=? AND (?='' OR date>=?) AND (?='' OR date<=?)" +
      (kind === "daily"
        ? " AND (?='' OR sku=?)"
        : " AND (?='' OR EXISTS (SELECT 1 FROM json_each(skus_json) WHERE value=?))");
    const args = [
      id,
      query.start || "",
      query.start || "",
      query.end || "",
      query.end || "",
      query.sku || "",
      query.sku || "",
    ];
    const rows = this.all(
      `SELECT * FROM ${table} WHERE ${where} ORDER BY ordinal LIMIT ? OFFSET ?`,
      ...args,
      limit,
      offset,
    ).map((r) =>
      kind === "daily"
        ? decode(r.payload_json)
        : { date: r.date, store: r.store, skus: decode(r.skus_json) },
    );
    return {
      rows,
      total: this.required(
        `SELECT count(*) AS n FROM ${table} WHERE ${where}`,
        ...args,
      ).n,
      limit,
      offset,
    };
  }
  saveJob(job: Row, result?: Row) {
    const { cancel, result: ignored, ...safe } = job;
    this.run(
      "INSERT INTO jobs VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,payload_json=excluded.payload_json,result_json=COALESCE(excluded.result_json,jobs.result_json)",
      job.id,
      job.status,
      encode(safe),
      result ? encode(result) : null,
      job.created_at,
    );
  }
  getJob(id: string) {
    const r = this.get("SELECT * FROM jobs WHERE id=?", id);
    return r
      ? {
          ...decode(r.payload_json),
          ...(r.result_json ? { result: decode(r.result_json) } : {}),
        }
      : null;
  }
  listSkus(query = ""): Row[] {
    return this.all(
      "SELECT * FROM skus WHERE sku LIKE ? OR name LIKE ? ORDER BY sku",
      `%${query}%`,
      `%${query}%`,
    ).map((r) => ({
      ...r,
      attributes: decode(r.attributes_json),
      attributes_json: undefined,
      uoms: this.all(
        "SELECT uom,factor_micro,factor_micro/1000000.0 AS factor FROM sku_uoms WHERE sku=?",
        r.sku,
      ),
    }));
  }
  private saveSkuRow(value: Row, imported = false) {
    const sku = text(value.sku, "SKU", 120),
      previous = this.get("SELECT * FROM skus WHERE sku=?", sku);
    if (imported && previous) return;
    if (previous) this.auditRevision(previous, value.revision);
    const attributes = { ...object(value.attributes || {}, "SKU attributes") };
    if (imported)
      for (const key of [
        "max_item_length",
        "max_item_width",
        "max_item_height",
        "max_item_weight",
      ])
        if (!(attributes[key] > 0)) delete attributes[key];
    for (const key of [
      "max_item_length",
      "max_item_width",
      "max_item_height",
      "max_item_weight",
    ])
      if (
        attributes[key] !== undefined &&
        attributes[key] !== null &&
        attributes[key] !== ""
      )
        positive(attributes[key], key);
    for (const key of ["chilled", "tablet", "flammable"])
      if (attributes[key] !== undefined && typeof attributes[key] !== "boolean")
        throw new DataError(`${key} must be boolean`);
    const base = text(value.base_uom || "EA", "Base UOM", 30);
    if (
      previous &&
      base !== previous.base_uom &&
      this.get("SELECT 1 FROM positions WHERE sku=?", sku)
    )
      throw new DataError(
        "Base UOM cannot change after inventory has been recorded",
      );
    const units = [
      { uom: base, factor: "1" },
      ...(Array.isArray(value.uoms)
        ? value.uoms.filter((u: Row) => u.uom !== base)
        : []),
    ];
    const seen = new Set<string>();
    for (const unit of units) {
      unit.uom = text(unit.uom, "UOM", 30);
      if (seen.has(unit.uom)) throw new DataError("Duplicate UOM");
      seen.add(unit.uom);
      if (scaledQuantity(unit.factor) <= 0)
        throw new DataError("UOM conversion must be positive");
    }
    this.run(
      "INSERT INTO skus VALUES(?,?,?,?,?,1,?) ON CONFLICT(sku) DO UPDATE SET name=excluded.name,base_uom=excluded.base_uom,attributes_json=excluded.attributes_json,active=?,revision=skus.revision+1,updated_at=excluded.updated_at",
      sku,
      text(value.name || sku, "SKU name"),
      base,
      encode(attributes),
      value.active === false || value.active === 0 ? 0 : 1,
      this.timestamp(),
      value.active === false || value.active === 0 ? 0 : 1,
    );
    this.run("DELETE FROM sku_uoms WHERE sku=?", sku);
    for (const unit of units)
      this.run(
        "INSERT INTO sku_uoms VALUES(?,?,?)",
        sku,
        unit.uom,
        scaledQuantity(unit.factor),
      );
  }
  saveSku(value: Row) {
    return this.transaction(() => {
      this.saveSkuRow(value);
      return this.listSkus(String(value.sku).trim()).find(
        (r) => r.sku === String(value.sku).trim(),
      );
    });
  }
  importMasterRows(kind: string, rows: Row[]) {
    if (
      !["skus", "uoms", "partners"].includes(kind) ||
      !rows.length ||
      rows.length > 10000
    )
      throw new DataError("Import requires 1–10,000 master-data rows");
    return this.transaction(() => {
      const seen = new Set<string>();
      for (const row of rows) {
        const key =
            kind === "partners"
              ? text(row.id, "Partner code")
              : text(row.sku, "SKU"),
          identity = kind === "uoms" ? `${key}:${row.uom}` : key;
        if (seen.has(identity))
          throw new DataError("Duplicate master-data key in CSV");
        seen.add(identity);
        if (kind === "partners") {
          const previous = this.get("SELECT * FROM partners WHERE id=?", key);
          this.savePartner({
            id: key,
            name: row.name,
            kind: row.kind,
            revision: previous?.revision,
            details: {
              ...(previous ? decode(previous.details_json) : {}),
              ...(row.contact !== undefined ? { contact: row.contact } : {}),
              ...(row.workstation_id !== undefined
                ? { workstation_id: row.workstation_id }
                : {}),
            },
          });
          continue;
        }
        const previous = this.get("SELECT * FROM skus WHERE sku=?", key);
        if (kind === "uoms" && !previous)
          throw new DataError(
            `Create SKU ${key} before importing its conversions`,
          );
        const attributes: Row = previous
            ? decode(previous.attributes_json)
            : {},
          uoms = this.all("SELECT * FROM sku_uoms WHERE sku=?", key).map(
            (u) => ({ uom: u.uom, factor: exactQuantity(u.factor_micro) }),
          );
        if (kind === "uoms") {
          const uom = text(row.uom, "UOM", 30),
            factor = scaledQuantity(row.factor);
          if (uom === previous!.base_uom && factor !== quantityScale)
            throw new DataError("Base UOM factor must be one");
          const old = uoms.find((u) => u.uom === uom);
          if (old) old.factor = String(row.factor);
          else uoms.push({ uom, factor: String(row.factor) });
        } else {
          for (const field of [
            "max_item_length",
            "max_item_width",
            "max_item_height",
            "max_item_weight",
          ])
            if (Object.hasOwn(row, field)) {
              if (row[field] === "" || Number(row[field]) === 0)
                delete attributes[field];
              else attributes[field] = Number(row[field]);
            }
          for (const field of ["chilled", "tablet", "flammable"])
            if (Object.hasOwn(row, field)) {
              const value = String(row[field]).toLowerCase();
              if (!value) delete attributes[field];
              else if (["true", "1", "false", "0"].includes(value))
                attributes[field] = ["true", "1"].includes(value);
              else
                throw new DataError(
                  `${field} must be true, false, 1, 0 or empty`,
                );
            }
        }
        let active = previous?.active ?? 1;
        if (Object.hasOwn(row, "active")) {
          if (
            !["1", "0", "true", "false"].includes(
              String(row.active).toLowerCase(),
            )
          )
            throw new DataError("Active must be true, false, 1 or 0");
          active = ["1", "true"].includes(String(row.active).toLowerCase())
            ? 1
            : 0;
        }
        this.saveSkuRow({
          sku: key,
          name: row.name || previous?.name || key,
          base_uom: row.base_uom || previous?.base_uom || "EA",
          attributes,
          uoms,
          active,
          revision: previous?.revision,
        });
      }
      return { imported: rows.length };
    });
  }
  importWorkspaceSkus(id: string) {
    const w = this.getWorkspace(id),
      names = new Set<string>([
        ...(w.velocity || []).map((r: Row) => r.sku),
        ...(w.stock || []).map((r: Row) => r.sku),
        ...Object.keys(w.attributes || {}),
      ]);
    return this.transaction(() => {
      let count = 0;
      for (const sku of names)
        if (!this.get("SELECT 1 FROM skus WHERE sku=?", sku)) {
          this.saveSkuRow({ sku, attributes: w.attributes?.[sku] || {} }, true);
          count++;
        }
      return { imported: count, existing: names.size - count };
    });
  }
  listPartners(): Row[] {
    return this.all("SELECT * FROM partners ORDER BY kind,id").map((r) => ({
      ...r,
      details: decode(r.details_json),
      details_json: undefined,
    }));
  }
  savePartner(value: Row) {
    const id = text(value.id, "Partner code", 120);
    if (!["store", "supplier"].includes(value.kind))
      throw new DataError("Choose store or supplier");
    return this.transaction(() => {
      const old = this.get("SELECT * FROM partners WHERE id=?", id);
      if (old) this.auditRevision(old, value.revision);
      this.run(
        "INSERT INTO partners VALUES(?,?,?,?,1) ON CONFLICT(id) DO UPDATE SET kind=excluded.kind,name=excluded.name,details_json=excluded.details_json,revision=partners.revision+1",
        id,
        value.kind,
        text(value.name, "Name"),
        encode(object(value.details || {}, "Details")),
      );
      return { id };
    });
  }
  listWarehouses() {
    return this.all(
      "SELECT w.id,w.name,w.version_hash,w.updated_at,w.time_zone,json_extract(w.project_json,'$.storage_layout.system_type') AS system_type,p.release_id AS published_release_id,r.version AS published_version FROM warehouses w LEFT JOIN publications p ON p.warehouse_id=w.id LEFT JOIN releases r ON r.id=p.release_id ORDER BY w.name",
    );
  }
  requireWarehouse(id: string) {
    return this.required("SELECT id FROM warehouses WHERE id=?", id);
  }
  syncWarehouse(
    id: string,
    project: Row,
    geometry: Row,
    name: string,
    timeZone?: string,
  ) {
    return this.transaction(() => {
      this.required("SELECT 1 FROM workspaces WHERE id=?", id);
      const previous = this.get("SELECT * FROM warehouses WHERE id=?", id);
      const zone = timeZone || previous?.time_zone || "UTC";
      try {
        new Intl.DateTimeFormat("en-US", {
          timeZone: text(zone, "Warehouse time zone", 100),
        });
      } catch {
        throw new DataError(
          "Warehouse time zone must be a valid IANA identifier, such as Asia/Kuala_Lumpur",
        );
      }
      const version = hash(project);
      this.run(
        "INSERT INTO warehouses VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,project_json=excluded.project_json,geometry_json=excluded.geometry_json,version_hash=excluded.version_hash,updated_at=excluded.updated_at,time_zone=excluded.time_zone",
        id,
        name || id,
        encode(project),
        encode(geometry),
        version,
        this.timestamp(),
        zone,
      );
      const locations: Row[] = [
        { id: "RECEIVING", name: "Receiving", kind: "receiving" },
        { id: "DISPATCH", name: "Dispatch", kind: "dispatch" },
      ];
      const storage = project.storage_layout || {},
        isAmr = (storage.system_type || "AMR") === "AMR",
        buffers: Row[] = storage.buffers || [];
      let unit = 0;
      for (const rack of geometry.racks || [])
        for (let level = 1; level <= geometry.summary.levels; level++)
          for (let slot = 1; slot <= geometry.summary.slots; slot++) {
            const buffer = buffers.find(
              (b) =>
                b.grid_waypoint === rack.id &&
                (isAmr ||
                  (Number(b.level ?? 1) === level &&
                    Number(b.slot ?? 1) === slot)),
            );
            if (!isAmr && buffers.length && !buffer) continue;
            const bay = buffer
              ? String(buffer.buffer_id).split("/L")[0]
              : `B-${rack.id}`;
            const address = `${rack.zone}/${rack.aisle || "A01"}/${bay}/L${String(level).padStart(2, "0")}/S${String(slot).padStart(2, "0")}`;
            const handling =
              (isAmr
                ? "SHELF_"
                : storage.system_type === "Pallet ASRS"
                  ? "PALLET_"
                  : "TOTE_") +
              String(isAmr ? rack.rank : ++unit).padStart(3, "0");
            locations.push({
              id: `${rack.id}:L${level}:S${slot}`,
              name: `${rack.id} · level ${level} · slot ${slot}`,
              kind: "storage",
              rack_id: rack.id,
              level,
              slot,
              zone: rack.zone,
              address,
              handling_unit_id: handling,
              dynamic_address: isAmr
                ? `${handling}/L${String(level).padStart(2, "0")}/S${String(slot).padStart(2, "0")}`
                : handling,
              buffer_id: buffer?.buffer_id || null,
              x: rack.x,
              y: rack.y,
              reachable: rack.distance !== null,
            });
          }
      for (const [id, station] of Object.entries(geometry.stations || {}))
        locations.push({
          id: `STATION:${id}`,
          name: `Workstation ${id}`,
          kind: "workstation",
          ...(station as Row),
        });
      const ids = new Set(locations.map((r) => r.id));
      for (const old of this.all(
        "SELECT * FROM locations WHERE warehouse_id=? AND kind!='transit'",
        id,
      ))
        if (!ids.has(old.id)) {
          if (
            this.get(
              "SELECT 1 FROM positions WHERE warehouse_id=? AND location_id=? AND quantity_micro>0",
              id,
              old.id,
            ) ||
            this.get(
              "SELECT 1 FROM movement_tasks WHERE warehouse_id=? AND destination=? AND status IN ('reserved','in_transit','failed')",
              id,
              old.id,
            )
          )
            throw new DataError(
              `Location ${old.id} has inventory or active tasks and cannot be removed`,
              409,
            );
          this.run(
            "UPDATE locations SET active=0 WHERE warehouse_id=? AND id=?",
            id,
            old.id,
          );
        }
      if (
        previous &&
        previous.version_hash !== version &&
        this.get(
          "SELECT 1 FROM positions WHERE warehouse_id=? AND quantity_micro>0",
          id,
        )
      )
        throw new DataError(
          "Warehouse geometry cannot change while inventory is present. Use a new workspace/version or empty inventory first.",
          409,
        );
      for (const r of locations)
        this.run(
          "INSERT INTO locations VALUES(?,?,?,?,?,?,?,1,?) ON CONFLICT(warehouse_id,id) DO UPDATE SET name=excluded.name,kind=excluded.kind,rack_id=excluded.rack_id,level=excluded.level,slot=excluded.slot,active=1,details_json=excluded.details_json",
          id,
          r.id,
          r.name,
          r.kind,
          r.rack_id || null,
          r.level || null,
          r.slot || null,
          encode(r),
        );
      return { id, version_hash: version, location_count: locations.length };
    });
  }
  warehouse(id: string) {
    const r = this.required("SELECT * FROM warehouses WHERE id=?", id);
    return {
      id,
      name: r.name,
      version_hash: r.version_hash,
      time_zone: r.time_zone,
      project: decode(r.project_json),
      geometry: decode(r.geometry_json),
      locations: this.listLocations(id),
      equipment: this.listEquipment(id),
      containers: this.listContainers(id),
    };
  }
  listLocations(id: string): Row[] {
    return this.all(
      "SELECT * FROM locations WHERE warehouse_id=? AND active=1 ORDER BY kind,id",
      id,
    ).map((r) => ({
      ...r,
      details: decode(r.details_json),
      details_json: undefined,
    }));
  }
  listEquipment(id: string): Row[] {
    return this.all(
      "SELECT * FROM equipment WHERE warehouse_id=? ORDER BY id",
      id,
    ).map((r) => ({
      ...r,
      details: decode(r.details_json),
      details_json: undefined,
    }));
  }
  listContainers(id: string): Row[] {
    return this.all(
      "SELECT * FROM containers WHERE warehouse_id=? ORDER BY code",
      id,
    ).map((c) => ({
      ...c,
      contents: this.all(
        "SELECT p.id,p.sku,p.lot,p.expiry,p.status,p.quantity_micro/1000000.0 AS quantity,s.base_uom FROM positions p JOIN skus s ON s.sku=p.sku WHERE p.warehouse_id=? AND p.container=? AND p.quantity_micro>0",
        id,
        c.code,
      ),
    }));
  }
  private unlockedContainer(warehouseId: string, code: string) {
    if (
      code &&
      this.get(
        "SELECT 1 FROM movement_tasks t JOIN positions p ON p.id=t.source_position_id WHERE t.warehouse_id=? AND p.container=? AND t.status IN ('reserved','in_transit','failed')",
        warehouseId,
        code,
      )
    )
      throw new DataError(
        "Container belongs to an active relocation task",
        409,
      );
  }
  saveEquipment(value: Row) {
    if (!["AMR", "ASRS"].includes(value.kind))
      throw new DataError("Equipment type must be AMR or ASRS");
    return this.transaction(() => {
      const old = this.get("SELECT * FROM equipment WHERE id=?", value.id);
      if (old) this.auditRevision(old, value.revision);
      if (old && old.warehouse_id !== value.warehouse_id)
        throw new DataError("Equipment ID belongs to another warehouse", 409);
      const details = object(value.details || {}, "Capabilities");
      if (details.max_payload_kg != null)
        positive(details.max_payload_kg, "Maximum payload");
      const warehouse = this.required(
        "SELECT geometry_json FROM warehouses WHERE id=?",
        value.warehouse_id,
      );
      if (
        details.workstation_id &&
        !Object.hasOwn(
          decode(warehouse.geometry_json).stations,
          details.workstation_id,
        )
      )
        throw new DataError("Workstation does not belong to this warehouse");
      this.run(
        "INSERT INTO equipment VALUES(?,?,?,?,?,1) ON CONFLICT(id) DO UPDATE SET name=excluded.name,kind=excluded.kind,details_json=excluded.details_json,revision=equipment.revision+1",
        text(value.id, "Equipment ID", 120),
        text(value.warehouse_id, "Warehouse"),
        value.kind,
        text(value.name, "Name"),
        encode(details),
      );
      return { id: value.id };
    });
  }
  private position(id: string) {
    return this.required("SELECT * FROM positions WHERE id=?", id);
  }
  private warehouseContext(id: string) {
    const row = this.required(
        "SELECT version_hash,time_zone FROM warehouses WHERE id=?",
        id,
      ),
      key = row.version_hash + ":" + row.time_zone;
    let cached = this.warehouseCache.get(id);
    if (cached?.key !== key) {
      cached = {
        key,
        project: decode(
          this.required("SELECT project_json FROM warehouses WHERE id=?", id)
            .project_json,
        ),
        formatter: new Intl.DateTimeFormat("en-US", {
          timeZone: row.time_zone,
          year: "numeric",
          month: "2-digit",
          day: "2-digit",
        }),
      };
      this.warehouseCache.set(id, cached);
    }
    return cached!;
  }
  private today(id: string) {
    const parts = this.warehouseContext(id).formatter.formatToParts(
      this.clock(),
    );
    return ["year", "month", "day"]
      .map((type) => parts.find((p) => p.type === type)!.value)
      .join("-");
  }
  private eligible(position: Row) {
    const location = this.required(
      "SELECT * FROM locations WHERE warehouse_id=? AND id=?",
      position.warehouse_id,
      position.location_id,
    );
    try {
      this.physicalFit(
        position.warehouse_id,
        position.sku,
        position.location_id,
      );
    } catch {
      return false;
    }
    return (
      position.status === "available" &&
      (!position.expiry ||
        position.expiry >= this.today(position.warehouse_id)) &&
      location.active &&
      !["transit", "dispatch"].includes(location.kind)
    );
  }
  listInventory(id: string, query = ""): Row[] {
    return this.all(
      "SELECT p.*,s.base_uom,l.name AS location_name,l.kind AS location_kind FROM positions p JOIN skus s ON s.sku=p.sku JOIN locations l ON l.warehouse_id=p.warehouse_id AND l.id=p.location_id WHERE p.warehouse_id=? AND p.quantity_micro>0 AND (p.sku LIKE ? OR p.location_id LIKE ? OR p.lot LIKE ? OR p.container LIKE ?) ORDER BY p.sku,p.expiry,p.location_id",
      id,
      ...Array(4).fill(`%${query}%`),
    ).map((p) => {
      let physical_check = "UNVERIFIED",
        physical_reason =
          "Container capacity and aggregate rack loading require separate verification";
      try {
        physical_check = this.physicalFit(p.warehouse_id, p.sku, p.location_id);
      } catch (error: any) {
        physical_check = "FAILED";
        physical_reason = error.message;
      }
      return {
        ...p,
        quantity: p.quantity_micro / quantityScale,
        reserved: p.reserved_micro / quantityScale,
        quantity_exact: exactQuantity(p.quantity_micro),
        reserved_exact: exactQuantity(p.reserved_micro),
        available_exact: exactQuantity(
          this.eligible(p) && physical_check !== "FAILED"
            ? p.quantity_micro - p.reserved_micro
            : 0,
        ),
        available:
          this.eligible(p) && physical_check !== "FAILED"
            ? (p.quantity_micro - p.reserved_micro) / quantityScale
            : 0,
        physical_check,
        physical_reason,
      };
    });
  }
  private physicalFit(
    warehouseId: string,
    sku: string,
    locationId: string,
  ): string {
    const location = this.required(
      "SELECT * FROM locations WHERE warehouse_id=? AND id=? AND active=1",
      warehouseId,
      locationId,
    );
    if (location.kind !== "storage") return "UNVERIFIED";
    const a = decode(
      this.required("SELECT attributes_json FROM skus WHERE sku=?", sku)
        .attributes_json,
    );
    const project = this.warehouseContext(warehouseId).project;
    const defaults = project.warehouse_storage_defaults || {},
      details = decode(location.details_json);
    const profile: Row = { ...defaults };
    const address = String(details.address || details.zone || "");
    let prefix = "";
    for (const segment of address.split("/")) {
      prefix = prefix ? prefix + "/" + segment : segment;
      Object.assign(profile, project.location_attributes?.[prefix] || {});
    }
    let known = 0;
    for (const field of [
      "max_item_length",
      "max_item_width",
      "max_item_height",
      "max_item_weight",
    ]) {
      if (a[field] > 0 && profile[field] > 0) {
        known++;
        if (a[field] > profile[field] + 1e-9)
          throw new DataError(`SKU ${sku} exceeds ${field} at ${locationId}`);
      }
    }
    let flagsKnown = true;
    for (const field of ["chilled", "tablet", "flammable"])
      if (a[field] === true) {
        if (profile[field] === false)
          throw new DataError(`SKU ${sku} requires ${field} at ${locationId}`);
        if (profile[field] !== true) flagsKnown = false;
      }
    return known === 4 && flagsKnown ? "ITEM_CHECKED" : "UNVERIFIED";
  }
  private targetPosition(
    warehouseId: string,
    source: Row,
    locationId: string,
    allowTransit = false,
  ): Row {
    const location = this.required(
      "SELECT kind FROM locations WHERE warehouse_id=? AND id=? AND active=1",
      warehouseId,
      locationId,
    );
    if (!allowTransit && location.kind === "transit")
      throw new DataError("Use task actions to change stock in transit");
    this.physicalFit(warehouseId, source.sku, locationId);
    if (source.container) {
      const code = text(source.container, "Container code", 120),
        unit = this.get(
          "SELECT * FROM containers WHERE warehouse_id=? AND code=?",
          warehouseId,
          code,
        );
      if (unit && unit.location_id !== locationId)
        throw new DataError(
          "Container is at another location. Move the whole container together.",
          409,
        );
      if (!unit)
        this.run(
          "INSERT INTO containers VALUES(?,?,?)",
          warehouseId,
          code,
          locationId,
        );
    }
    const existing = this.get(
      "SELECT * FROM positions WHERE warehouse_id=? AND sku=? AND location_id=? AND lot=? AND expiry=? AND status=? AND container=?",
      warehouseId,
      source.sku,
      locationId,
      source.lot,
      source.expiry,
      source.status,
      source.container,
    );
    if (existing) return existing;
    const id = randomUUID();
    this.run(
      "INSERT INTO positions(id,warehouse_id,sku,location_id,lot,expiry,status,container,quantity_micro) VALUES(?,?,?,?,?,?,?,?,0)",
      id,
      warehouseId,
      source.sku,
      locationId,
      source.lot,
      source.expiry,
      source.status,
      source.container,
    );
    return this.position(id);
  }
  private convertQuantity(
    sku: string,
    quantity: unknown,
    uom?: string,
    signed = false,
  ) {
    const master = this.required(
      "SELECT base_uom,active FROM skus WHERE sku=?",
      sku,
    );
    if (!master.active) throw new DataError("SKU is inactive");
    const factor = this.required(
      "SELECT factor_micro FROM sku_uoms WHERE sku=? AND uom=?",
      sku,
      uom || master.base_uom,
    ).factor_micro;
    const product = BigInt(scaledQuantity(quantity, signed)) * BigInt(factor);
    if (product % 1_000_000n)
      throw new DataError("Converted quantity exceeds six decimal places");
    const result = Number(product / 1_000_000n);
    if (!Number.isSafeInteger(result) || !result)
      throw new DataError(
        "Quantity must be nonzero and within supported precision",
      );
    return result;
  }
  private eventLines: Row[] = [];
  private change(position: Row, quantity: number, reserved = 0) {
    const q = position.quantity_micro + quantity,
      r = position.reserved_micro + reserved;
    if (!Number.isSafeInteger(q) || q < 0 || r < 0 || r > q)
      throw new DataError("Insufficient available stock", 409);
    this.run(
      "UPDATE positions SET quantity_micro=?,reserved_micro=?,revision=revision+1 WHERE id=?",
      q,
      r,
      position.id,
    );
    this.eventLines.push({ position_id: position.id, quantity, reserved });
  }
  postInventory(value: Row) {
    const requestId = text(value.request_id, "Unique request ID", 120),
      actor = text(value.actor, "Operator", 120),
      reason = text(value.reason, "Reason/reference", 500);
    const requestHash = hash(value),
      warehouseId = text(value.warehouse_id, "Warehouse");
    return this.transaction(() => {
      const previous = this.get(
        "SELECT * FROM stock_events WHERE request_id=?",
        requestId,
      );
      if (previous) {
        if (previous.request_hash !== requestHash)
          throw new DataError(
            "Request ID was already used for a different operation",
            409,
          );
        return decode(previous.result_json);
      }
      this.required("SELECT 1 FROM warehouses WHERE id=?", warehouseId);
      this.eventLines = [];
      let result: Row;
      const kind = String(value.kind);
      if (["opening", "receipt", "return"].includes(kind)) {
        const sku = text(value.sku, "SKU", 120),
          quantity = this.convertQuantity(sku, value.quantity, value.uom);
        this.unlockedContainer(warehouseId, value.container || "");
        const expiry = value.expiry || "",
          status =
            value.status || (kind === "return" ? "quarantine" : "available");
        if (expiry && !validDate(expiry))
          throw new DataError("Expiry must be a valid calendar date");
        if (
          expiry &&
          expiry < this.today(warehouseId) &&
          status === "available"
        )
          throw new DataError("Expired stock must be quarantined or blocked");
        if (!stockStatuses.includes(status))
          throw new DataError("Invalid stock status");
        const p = this.targetPosition(
          warehouseId,
          {
            sku,
            lot: String(value.lot || ""),
            expiry,
            status,
            container: String(value.container || ""),
          },
          text(value.location_id, "Location"),
        );
        this.change(p, quantity);
        result = { position_id: p.id };
      } else if (kind === "reserve_sku") {
        const sku = text(value.sku, "SKU", 120),
          quantity = this.convertQuantity(sku, value.quantity, value.uom);
        let remaining = quantity;
        const reservations: string[] = [];
        const positions = this.all(
          "SELECT * FROM positions WHERE warehouse_id=? AND sku=? AND quantity_micro>reserved_micro ORDER BY CASE WHEN expiry='' THEN 1 ELSE 0 END,expiry,id",
          warehouseId,
          sku,
        );
        for (const p of positions) {
          if (!remaining) break;
          if (!this.eligible(p)) continue;
          try {
            this.unlockedContainer(warehouseId, p.container);
          } catch {
            continue;
          }
          const take = Math.min(remaining, p.quantity_micro - p.reserved_micro),
            id = randomUUID();
          this.change(p, 0, take);
          this.run(
            "INSERT INTO reservations VALUES(?,?,?,?,'active',?)",
            id,
            p.id,
            take,
            reason,
            this.timestamp(),
          );
          reservations.push(id);
          remaining -= take;
        }
        if (remaining)
          throw new DataError("Insufficient eligible stock for this SKU", 409);
        result = {
          reservation_ids: reservations,
          quantity_exact: exactQuantity(quantity),
        };
      } else if (kind === "container_transfer") {
        const code = text(value.container, "Container code", 120),
          destination = text(value.destination, "Destination");
        const unit = this.required(
          "SELECT * FROM containers WHERE warehouse_id=? AND code=?",
          warehouseId,
          code,
        );
        this.unlockedContainer(warehouseId, code);
        if (unit.location_id === destination)
          throw new DataError("Choose a different destination");
        const positions = this.all(
          "SELECT * FROM positions WHERE warehouse_id=? AND container=? AND quantity_micro>0",
          warehouseId,
          code,
        );
        if (!positions.length || positions.some((p) => p.reserved_micro > 0))
          throw new DataError(
            "Container is empty or contains reserved stock",
            409,
          );
        this.run(
          "UPDATE containers SET location_id=? WHERE warehouse_id=? AND code=?",
          destination,
          warehouseId,
          code,
        );
        for (const p of positions) {
          const target = this.targetPosition(warehouseId, p, destination);
          this.change(p, -p.quantity_micro);
          this.change(target, p.quantity_micro);
        }
        result = { container: code, location_id: destination };
      } else if (
        ["adjustment", "transfer", "reserve", "status"].includes(kind)
      ) {
        const p = this.position(text(value.position_id, "Stock position"));
        if (p.warehouse_id !== warehouseId)
          throw new DataError(
            "Stock position belongs to a different warehouse",
          );
        this.unlockedContainer(warehouseId, p.container);
        if (
          this.required(
            "SELECT kind FROM locations WHERE warehouse_id=? AND id=?",
            warehouseId,
            p.location_id,
          ).kind === "transit"
        )
          throw new DataError("Use task actions to change stock in transit");
        if (value.expected_revision !== undefined)
          this.auditRevision(p, value.expected_revision);
        const quantity = this.convertQuantity(
          p.sku,
          value.quantity,
          value.uom,
          kind === "adjustment",
        );
        if (kind === "adjustment") {
          this.change(p, quantity);
          result = { position_id: p.id };
        } else if (kind === "reserve") {
          if (!this.eligible(p))
            throw new DataError("Stock is not eligible for reservation");
          this.change(p, 0, quantity);
          const id = randomUUID();
          this.run(
            "INSERT INTO reservations VALUES(?,?,?,?, 'active',?)",
            id,
            p.id,
            quantity,
            reason,
            this.timestamp(),
          );
          result = { reservation_id: id };
        } else {
          if (p.quantity_micro - p.reserved_micro < quantity)
            throw new DataError("Insufficient available stock", 409);
          let target: Row;
          if (kind === "status") {
            if (!stockStatuses.includes(value.status))
              throw new DataError("Invalid stock status");
            if (
              value.status === "available" &&
              p.expiry &&
              p.expiry < this.today(warehouseId)
            )
              throw new DataError("Expired stock cannot be made available");
            target = this.targetPosition(
              warehouseId,
              { ...p, status: value.status },
              p.location_id,
            );
          } else {
            if (p.container)
              throw new DataError(
                "Use whole-container transfer or a relocation task to move this container",
              );
            target = this.targetPosition(
              warehouseId,
              p,
              text(value.destination, "Destination"),
            );
          }
          if (target.id === p.id)
            throw new DataError(
              "Choose a different destination or stock status",
            );
          this.change(p, -quantity);
          this.change(target, quantity);
          result = { position_id: target.id };
        }
      } else if (["dispatch", "cancel_reservation"].includes(kind)) {
        const r = this.required(
            "SELECT * FROM reservations WHERE id=?",
            text(value.reservation_id, "Reservation"),
          ),
          p = this.position(r.position_id);
        if (p.warehouse_id !== warehouseId || r.status !== "active")
          throw new DataError(
            "Reservation is not active in this warehouse",
            409,
          );
        if (
          this.get("SELECT 1 FROM movement_tasks WHERE reservation_id=?", r.id)
        )
          throw new DataError("Use movement task actions for this reservation");
        if (kind === "dispatch" && !this.eligible(p))
          throw new DataError("Reserved stock is blocked or expired");
        this.change(
          p,
          kind === "dispatch" ? -r.quantity_micro : 0,
          -r.quantity_micro,
        );
        this.run(
          "UPDATE reservations SET status=? WHERE id=?",
          kind === "dispatch" ? "dispatched" : "cancelled",
          r.id,
        );
        result = { reservation_id: r.id };
      } else if (kind === "task_create") {
        const p = this.position(text(value.position_id, "Stock position"));
        if (p.warehouse_id !== warehouseId || !this.eligible(p))
          throw new DataError("Stock cannot be reserved for movement");
        const destination = text(value.destination, "Destination");
        if (destination === p.location_id)
          throw new DataError("Choose a different destination");
        if (
          this.required(
            "SELECT kind FROM locations WHERE warehouse_id=? AND id=? AND active=1",
            warehouseId,
            destination,
          ).kind === "transit"
        )
          throw new DataError("Task destination must be a warehouse location");
        this.unlockedContainer(warehouseId, p.container);
        this.physicalFit(warehouseId, p.sku, destination);
        const quantity = this.convertQuantity(p.sku, value.quantity, value.uom);
        if (p.container && quantity !== p.quantity_micro)
          throw new DataError(
            "A container relocation must move its full contents",
          );
        const items = p.container
          ? this.all(
              "SELECT * FROM positions WHERE warehouse_id=? AND container=? AND quantity_micro>0",
              warehouseId,
              p.container,
            )
          : [p];
        for (const item of items) {
          if (!this.eligible(item))
            throw new DataError("Container contains blocked or expired stock");
          this.physicalFit(warehouseId, item.sku, destination);
        }
        this.change(p, 0, quantity);
        const reservation = randomUUID(),
          id =
            value.task_type == null
              ? randomUUID()
              : createTaskId(value.task_type, randomUUID());
        this.run(
          "INSERT INTO reservations VALUES(?,?,?,?,'active',?)",
          reservation,
          p.id,
          quantity,
          reason,
          this.timestamp(),
        );
        this.run(
          "INSERT INTO movement_tasks VALUES(?,?,?,?,?,NULL,?,'reserved',?,?,?)",
          id,
          warehouseId,
          p.id,
          destination,
          reservation,
          quantity,
          reason,
          this.timestamp(),
          this.timestamp(),
        );
        this.run(
          "INSERT INTO task_items VALUES(?,?,?,?,NULL)",
          id,
          p.id,
          reservation,
          quantity,
        );
        for (const item of items.filter((item) => item.id !== p.id)) {
          this.change(item, 0, item.quantity_micro);
          const rid = randomUUID();
          this.run(
            "INSERT INTO reservations VALUES(?,?,?,?,'active',?)",
            rid,
            item.id,
            item.quantity_micro,
            reason,
            this.timestamp(),
          );
          this.run(
            "INSERT INTO task_items VALUES(?,?,?,?,NULL)",
            id,
            item.id,
            rid,
            item.quantity_micro,
          );
        }
        result = {
          task_id: id,
          task_type: taskTypeForId(id),
          status: "reserved",
          container: p.container || null,
          item_count: items.length,
        };
      } else if (
        [
          "task_pickup",
          "task_complete",
          "task_cancel",
          "task_fail",
          "task_redirect",
        ].includes(kind)
      ) {
        const task = this.required(
          "SELECT * FROM movement_tasks WHERE id=?",
          text(value.task_id, "Task"),
        );
        if (task.warehouse_id !== warehouseId)
          throw new DataError("Task belongs to another warehouse");
        const source = this.position(task.source_position_id),
          q = task.quantity_micro;
        const items = this.all(
          "SELECT * FROM task_items WHERE task_id=?",
          task.id,
        );
        if (
          kind === "task_redirect" &&
          ["reserved", "in_transit", "failed"].includes(task.status)
        ) {
          const destination = text(value.destination, "Destination");
          if (
            this.required(
              "SELECT kind FROM locations WHERE warehouse_id=? AND id=? AND active=1",
              warehouseId,
              destination,
            ).kind === "transit"
          )
            throw new DataError(
              "Task destination must be a warehouse location",
            );
          for (const item of items)
            this.physicalFit(
              warehouseId,
              this.position(item.position_id).sku,
              destination,
            );
          this.run(
            "UPDATE movement_tasks SET destination=?,updated_at=? WHERE id=?",
            destination,
            this.timestamp(),
            task.id,
          );
        } else if (kind === "task_cancel" && task.status === "reserved") {
          for (const item of items) {
            this.change(
              this.position(item.position_id),
              0,
              -item.quantity_micro,
            );
            this.run(
              "UPDATE reservations SET status='cancelled' WHERE id=?",
              item.reservation_id,
            );
          }
          this.run(
            "UPDATE movement_tasks SET status='cancelled',updated_at=? WHERE id=?",
            this.timestamp(),
            task.id,
          );
        } else if (kind === "task_pickup" && task.status === "reserved") {
          if (
            items.some(
              (item) => !this.eligible(this.position(item.position_id)),
            )
          )
            throw new DataError("Source stock is blocked or expired");
          const transit = `TRANSIT:${task.id}`;
          this.run(
            "INSERT INTO locations VALUES(?,?,?,'transit',NULL,NULL,NULL,1,'{}')",
            warehouseId,
            transit,
            `In transit · ${task.id.slice(0, 8)}`,
          );
          if (source.container)
            this.run(
              "UPDATE containers SET location_id=? WHERE warehouse_id=? AND code=?",
              transit,
              warehouseId,
              source.container,
            );
          let first = "";
          for (const item of items) {
            const p = this.position(item.position_id),
              target = this.targetPosition(warehouseId, p, transit, true);
            first ||= target.id;
            this.change(p, -item.quantity_micro, -item.quantity_micro);
            this.change(target, item.quantity_micro);
            this.run(
              "UPDATE reservations SET status='dispatched' WHERE id=?",
              item.reservation_id,
            );
            this.run(
              "UPDATE task_items SET transit_position_id=? WHERE task_id=? AND position_id=?",
              target.id,
              task.id,
              p.id,
            );
          }
          this.run(
            "UPDATE movement_tasks SET status='in_transit',transit_position_id=?,updated_at=? WHERE id=?",
            first,
            this.timestamp(),
            task.id,
          );
        } else if (
          kind === "task_complete" &&
          ["in_transit", "failed"].includes(task.status)
        ) {
          if (source.container)
            this.run(
              "UPDATE containers SET location_id=? WHERE warehouse_id=? AND code=?",
              task.destination,
              warehouseId,
              source.container,
            );
          for (const item of items) {
            const transit = this.position(item.transit_position_id),
              target = this.targetPosition(
                warehouseId,
                transit,
                task.destination,
              );
            this.change(transit, -item.quantity_micro);
            this.change(target, item.quantity_micro);
          }
          this.run(
            "UPDATE movement_tasks SET status='completed',updated_at=? WHERE id=?",
            this.timestamp(),
            task.id,
          );
        } else if (kind === "task_fail" && task.status === "in_transit")
          this.run(
            "UPDATE movement_tasks SET status='failed',updated_at=? WHERE id=?",
            this.timestamp(),
            task.id,
          );
        else
          throw new DataError(
            "Task action is not valid in its current state",
            409,
          );
        result = {
          task_id: task.id,
          task_type: taskTypeForId(task.id),
          status: this.required(
            "SELECT status FROM movement_tasks WHERE id=?",
            task.id,
          ).status,
        };
      } else throw new DataError("Unsupported inventory operation");
      const id = randomUUID();
      result = { ...result, event_id: id };
      this.run(
        "INSERT INTO stock_events VALUES(?,?,?,?,?,?,?,?,?,?)",
        id,
        requestId,
        requestHash,
        warehouseId,
        kind,
        actor,
        reason,
        encode(value),
        encode(result),
        this.timestamp(),
      );
      for (const line of this.eventLines)
        this.run(
          "INSERT INTO stock_event_lines VALUES(?,?,?,?)",
          id,
          line.position_id,
          line.quantity,
          line.reserved,
        );
      return result;
    });
  }
  listReservations(id: string) {
    return this.all(
      "SELECT r.*,p.sku,p.location_id,r.quantity_micro/1000000.0 AS quantity FROM reservations r JOIN positions p ON p.id=r.position_id WHERE p.warehouse_id=? ORDER BY r.created_at DESC",
      id,
    );
  }
  importOpening(id: string, value: Row) {
    if (
      !Array.isArray(value.rows) ||
      !value.rows.length ||
      value.rows.length > 10000
    )
      throw new DataError("Opening import requires 1–10,000 rows");
    const batch = hash({ warehouse: id, rows: value.rows });
    return this.transaction(() => {
      const old = this.get(
        "SELECT value FROM metadata WHERE key=?",
        `opening:${batch}`,
      );
      if (old) return { ...decode(old.value), duplicate: true };
      value.rows.forEach((row: Row, i: number) =>
        this.postInventory({
          ...row,
          kind: "opening",
          warehouse_id: id,
          actor: value.actor,
          reason: value.reason,
          request_id: `opening:${batch}:${i}`,
        }),
      );
      const result = { imported: value.rows.length, batch };
      this.run(
        "INSERT INTO metadata VALUES(?,?)",
        `opening:${batch}`,
        encode(result),
      );
      return result;
    });
  }
  listTasks(id: string): Row[] {
    return this.all(
      "SELECT t.*,p.sku,p.container,p.location_id AS source,t.quantity_micro/1000000.0 AS quantity FROM movement_tasks t JOIN positions p ON p.id=t.source_position_id WHERE t.warehouse_id=? ORDER BY t.created_at DESC",
      id,
    ).map((t) => ({
      ...t,
      task_type: taskTypeForId(t.id),
      items: this.all(
        "SELECT i.*,p.sku,i.quantity_micro/1000000.0 AS quantity FROM task_items i JOIN positions p ON p.id=i.position_id WHERE i.task_id=?",
        t.id,
      ),
    }));
  }
  listEvents(id: string, after = "", limit = 200, before = 0): Row[] {
    return this.all(
      `SELECT rowid AS sequence,id,request_id,kind,actor,reason,created_at,result_json FROM stock_events WHERE warehouse_id=? AND rowid${before ? "<" : ">"}? ORDER BY rowid ${before ? "DESC" : "ASC"} LIMIT ?`,
      id,
      before || Number(after) || 0,
      Math.min(1000, Math.max(1, Number(limit) || 200)),
    ).map((r) => {
      const result = decode(r.result_json);
      return {
        ...r,
        result,
        task_id: result.task_id ?? "",
        task_type: result.task_id ? taskTypeForId(result.task_id) : "",
        result_json: undefined,
        lines: this.all(
          "SELECT e.position_id,p.warehouse_id,p.sku,p.location_id,p.lot,p.expiry,p.status,p.container,s.base_uom,e.quantity_delta_micro,e.reserved_delta_micro,e.quantity_delta_micro/1000000.0 AS quantity_delta,e.reserved_delta_micro/1000000.0 AS reserved_delta FROM stock_event_lines e JOIN positions p ON p.id=e.position_id JOIN skus s ON s.sku=p.sku WHERE e.event_id=?",
          r.id,
        ),
      };
    });
  }
  inventorySnapshot(id: string) {
    return this.transaction(() => {
      this.required("SELECT 1 FROM warehouses WHERE id=?", id);
      const sequence = this.required(
        "SELECT COALESCE(MAX(rowid),0) AS n FROM stock_events",
      ).n;
      return {
        warehouse_id: id,
        inventory_event_sequence: sequence,
        inventory: this.listInventory(id),
      };
    }, false);
  }
  publish(id: string, value: Row) {
    return this.transaction(() => {
      const warehouse = this.warehouse(id),
        workspace = this.getWorkspace(id);
      this.auditRevision(
        { revision: workspace._save_revision },
        value.expected_revision,
      );
      if (!workspace.result || workspace.stockStale || workspace.demandStale)
        throw new DataError("Generate a current layout before publishing");
      if (workspace.orders?.id && !this.hasAsset(workspace.orders.id))
        throw new DataError(
          "Select an available order dataset before publishing",
        );
      if (
        hash(workspace.project) !== warehouse.version_hash ||
        hash(workspace.result.project) !== warehouse.version_hash
      )
        throw new DataError(
          "Synchronize the warehouse and regenerate the layout before publishing",
        );
      if (!value.validation?.passed || !value.validation?.complete)
        throw new DataError(
          "The layout must pass current engine validation before publishing",
        );
      const catalog = this.listSkus(),
        master = new Map(catalog.map((s) => [s.sku, s]));
      const normalized = (a: Row) =>
        Object.fromEntries(
          Object.entries(a).filter(
            ([k, v]) =>
              !k.startsWith("max_item_") || (typeof v === "number" && v > 0),
          ),
        );
      for (const row of workspace.velocity || []) {
        const sku = master.get(row.sku);
        if (!sku?.active)
          throw new DataError(
            `Import or activate SKU ${row.sku} before publishing`,
          );
        if (sku.base_uom !== "EA")
          throw new DataError(
            `Planning SKU ${row.sku} must use EA as its base unit for the current layout engine`,
          );
        if (
          hash(normalized(workspace.attributes?.[row.sku] || {})) !==
          hash(normalized(sku.attributes))
        )
          throw new DataError(
            `Master attributes changed for ${row.sku}. Apply master attributes to planning and regenerate before publishing.`,
          );
      }
      const version = this.required(
        "SELECT COALESCE(MAX(version),0)+1 AS n FROM releases WHERE warehouse_id=?",
        id,
      ).n;
      const releaseId = randomUUID();
      const bundle = {
        schema: "layout_generator_release/v1",
        api_version: "v1",
        id: releaseId,
        warehouse_id: id,
        version,
        name: text(value.name, "Release name"),
        published_at: this.timestamp(),
        units: {
          length: "m",
          weight: "kg",
          quantity: "SKU base UOM",
          time: "ISO 8601 UTC",
        },
        warehouse,
        skus: catalog,
        partners: this.listPartners(),
        settings: workspace.settings,
        planning_inputs: {
          velocity: workspace.velocity,
          stock: workspace.stock,
          attributes: workspace.attributes,
          orders: workspace.orders,
        },
        slotting: workspace.result,
        validation: value.validation,
        inventory: this.listInventory(id),
        inventory_event_sequence: this.required(
          "SELECT COALESCE(MAX(rowid),0) AS n FROM stock_events",
        ).n,
      };
      const checksum = hash(bundle);
      this.run(
        "INSERT INTO releases VALUES(?,?,?,?,?,?,?)",
        releaseId,
        id,
        version,
        bundle.name,
        encode(bundle),
        checksum,
        bundle.published_at,
      );
      this.run(
        "INSERT INTO publications VALUES(?,?) ON CONFLICT(warehouse_id) DO UPDATE SET release_id=excluded.release_id",
        id,
        releaseId,
      );
      return {
        id: releaseId,
        version,
        checksum,
        published_at: bundle.published_at,
      };
    });
  }
  listReleases(id: string) {
    return this.all(
      "SELECT id,warehouse_id,version,name,checksum,published_at FROM releases WHERE warehouse_id=? ORDER BY version DESC",
      id,
    );
  }
  getRelease(id: string) {
    const r = this.required("SELECT * FROM releases WHERE id=?", id);
    return { ...decode(r.bundle_json), checksum: r.checksum };
  }
  published(id: string) {
    return this.getRelease(
      this.required(
        "SELECT release_id FROM publications WHERE warehouse_id=?",
        id,
      ).release_id,
    );
  }
  listKeys() {
    return this.all(
      "SELECT id,name,scope,created_at,revoked_at FROM api_keys ORDER BY created_at DESC",
    );
  }
  createKey(value: Row) {
    if (!["read", "operate"].includes(value.scope))
      throw new DataError("Key scope must be read or operate");
    const id = randomUUID(),
      token = "lg_" + randomBytes(32).toString("hex");
    this.run(
      "INSERT INTO api_keys VALUES(?,?,?,?,?,NULL)",
      id,
      text(value.name, "Integration name", 108),
      hash(token),
      value.scope,
      this.timestamp(),
    );
    return { id, token, scope: value.scope };
  }
  revokeKey(id: string) {
    this.required("SELECT id FROM api_keys WHERE id=?", id);
    this.run(
      "UPDATE api_keys SET revoked_at=? WHERE id=?",
      this.timestamp(),
      id,
    );
    return { revoked: true };
  }
  authenticate(token: string, write = false) {
    const key = this.get(
      "SELECT id,name,scope FROM api_keys WHERE token_hash=? AND revoked_at IS NULL",
      hash(token),
    );
    if (!key) throw new DataError("Valid Bearer API key required", 401);
    if (write && key.scope !== "operate")
      throw new DataError("This API key has read-only access", 403);
    return key;
  }
  backup(path: string) {
    if (existsSync(path))
      throw new DataError("Backup file already exists", 409);
    this.db.exec(`VACUUM INTO '${path.replace(/'/g, "''")}'`);
    return { name: path.split("/").at(-1) };
  }
  health() {
    this.integrity ||= {
      value: String(this.required("PRAGMA quick_check").quick_check),
      at: this.timestamp(),
    };
    return {
      sqlite_version: this.required("SELECT sqlite_version() AS version")
        .version,
      schema_version: 1,
      integrity: this.integrity.value,
      integrity_checked_at: this.integrity.at,
      journal_mode: this.required("PRAGMA journal_mode").journal_mode,
    };
  }
}
