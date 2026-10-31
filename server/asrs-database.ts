import { DatabaseSync } from "node:sqlite";
import { mkdirSync, readFileSync } from "node:fs";
import { dirname } from "node:path";
import { randomUUID } from "node:crypto";
import type { Row } from "../shared/model.js";
import type { AsrsModel, SimulationOrder } from "../shared/asrs.js";
import type { SimulationSink } from "./asrs-engine.js";
import { createTaskId } from "../shared/tasks.js";

const eventFields = [
  "sequence",
  "sim_seconds",
  "simulation_time_local",
  "kind",
  "sku",
  "load_id",
  "quantity_micro",
  "delta_micro",
  "load_delta_micro",
  "receiving_delta_micro",
  "load_balance_micro",
  "load_location",
  "sku_balance_micro",
  "storage_balance_micro",
  "source_row",
  "order_date",
  "source_time",
  "order_reference",
  "store",
  "from_location",
  "to_location",
  "crane",
  "cycle_id",
  "reason",
];
const skuMovementKinds: Record<string, string[]> = {
  consume: ["ORDER_OUT"],
  replenish: ["REPLENISH_IN", "RETURN_PUTAWAY"],
  return: ["RETURN_IN"],
};

export class AsrsDatabase {
  readonly db: DatabaseSync;
  private runtimeToken = "";
  constructor(path: string) {
    if (path !== ":memory:") mkdirSync(dirname(path), { recursive: true });
    this.db = new DatabaseSync(path);
    this.db.exec(
      "PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA foreign_keys=ON; PRAGMA busy_timeout=10000;",
    );
    const version = (this.db.prepare("PRAGMA user_version").get() as Row)
      .user_version;
    if (version > 1)
      throw new Error("Simulation database is newer than this application");
    if (!version)
      this.db.exec(`
      BEGIN IMMEDIATE;
      CREATE TABLE runs(pk INTEGER PRIMARY KEY,id TEXT NOT NULL UNIQUE,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,status TEXT NOT NULL,progress TEXT NOT NULL,config_json TEXT NOT NULL,release_json TEXT,model_json TEXT,checkpoint_json TEXT,summary_json TEXT);
      CREATE TABLE orders(run_id INTEGER NOT NULL REFERENCES runs(pk),source_row INTEGER NOT NULL,date TEXT NOT NULL,sku TEXT NOT NULL,store TEXT NOT NULL,quantity_micro INTEGER NOT NULL,source_time TEXT NOT NULL,uom TEXT NOT NULL,order_reference TEXT NOT NULL,fulfilled_micro INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL DEFAULT 'pending',shortage_micro INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(run_id,source_row));
      CREATE INDEX order_day ON orders(run_id,date,store,sku,source_row);
      CREATE TABLE loads(run_id INTEGER NOT NULL REFERENCES runs(pk),id TEXT NOT NULL,sku TEXT NOT NULL,address TEXT NOT NULL,rack TEXT NOT NULL,crane TEXT NOT NULL,initial_micro INTEGER NOT NULL,quantity_micro INTEGER NOT NULL DEFAULT 0,location TEXT NOT NULL,PRIMARY KEY(run_id,id));
      CREATE INDEX load_sku ON loads(run_id,sku);
      CREATE TABLE receiving(run_id INTEGER NOT NULL REFERENCES runs(pk),sku TEXT NOT NULL,quantity_micro INTEGER NOT NULL,PRIMARY KEY(run_id,sku));
      CREATE TABLE events(run_id INTEGER NOT NULL REFERENCES runs(pk),sequence INTEGER NOT NULL,sim_seconds REAL NOT NULL,simulation_time_local TEXT NOT NULL,kind TEXT NOT NULL,sku TEXT NOT NULL,load_id TEXT NOT NULL,quantity_micro INTEGER NOT NULL,delta_micro INTEGER NOT NULL,load_delta_micro INTEGER NOT NULL,receiving_delta_micro INTEGER NOT NULL,load_balance_micro INTEGER NOT NULL,load_location TEXT NOT NULL,sku_balance_micro INTEGER NOT NULL,storage_balance_micro INTEGER NOT NULL,source_row INTEGER,order_date TEXT NOT NULL,source_time TEXT NOT NULL,order_reference TEXT NOT NULL,store TEXT NOT NULL,from_location TEXT NOT NULL,to_location TEXT NOT NULL,crane TEXT NOT NULL,cycle_id TEXT NOT NULL,reason TEXT NOT NULL,PRIMARY KEY(run_id,sequence));
      CREATE INDEX event_sku ON events(run_id,sku,sequence);
      CREATE INDEX event_source ON events(run_id,source_row,sequence);
      CREATE INDEX event_kind ON events(run_id,kind,sequence);
      CREATE TRIGGER immutable_event_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'Simulation stock events are immutable'); END;
      CREATE TRIGGER immutable_event_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'Simulation stock events are immutable'); END;
      CREATE TABLE cycles(run_id INTEGER NOT NULL REFERENCES runs(pk),id TEXT NOT NULL,crane TEXT NOT NULL,start REAL NOT NULL,end REAL NOT NULL,payload_json TEXT NOT NULL,PRIMARY KEY(run_id,id));
      PRAGMA user_version=1;
      COMMIT;
    `);
    this.db.exec(
      "CREATE TABLE IF NOT EXISTS runtime_owner(singleton INTEGER PRIMARY KEY CHECK(singleton=1),pid INTEGER NOT NULL,identity TEXT NOT NULL,token TEXT NOT NULL)",
    );
  }
  claimRuntime() {
    const identity = (pid: number) => {
      if (!Number.isSafeInteger(pid) || pid < 1) return "";
      try {
        const fields = readFileSync(`/proc/${pid}/stat`, "utf8")
          .split(") ")
          .at(-1)!
          .split(" ");
        return fields[0] === "Z" ? "" : fields[19];
      } catch {
        try {
          process.kill(pid, 0);
          return "unknown";
        } catch (e) {
          return (e as NodeJS.ErrnoException).code === "ESRCH" ? "" : "unknown";
        }
      }
    };
    this.transaction(() => {
      const previous = this.db
        .prepare("SELECT * FROM runtime_owner WHERE singleton=1")
        .get() as Row | undefined;
      if (previous) {
        const current = identity(previous.pid);
        if (
          current &&
          (current === "unknown" ||
            previous.identity === "unknown" ||
            current === previous.identity)
        )
          throw new Error(
            "Another application owns this simulation database. Stop that server or use a separate DATA_DIR",
          );
      }
      this.runtimeToken = randomUUID();
      this.db
        .prepare("INSERT OR REPLACE INTO runtime_owner VALUES(1,?,?,?)")
        .run(process.pid, identity(process.pid), this.runtimeToken);
    });
  }
  releaseRuntime() {
    if (this.runtimeToken)
      this.db
        .prepare("DELETE FROM runtime_owner WHERE token=?")
        .run(this.runtimeToken);
    this.runtimeToken = "";
  }
  transaction<T>(operation: () => T): T {
    this.db.exec("BEGIN IMMEDIATE");
    try {
      const value = operation();
      this.db.exec("COMMIT");
      return value;
    } catch (error) {
      this.db.exec("ROLLBACK");
      throw error;
    }
  }
  recover() {
    this.db
      .prepare(
        "UPDATE runs SET status='paused',progress='Server restarted; resume from the last committed event' WHERE status='running'",
      )
      .run();
    this.db
      .prepare(
        "UPDATE runs SET status='failed',progress='Preparation was interrupted; prepare a new run' WHERE status='preparing'",
      )
      .run();
  }
  create(config: Row) {
    if (
      this.db
        .prepare("SELECT 1 FROM runs WHERE status IN ('preparing','running')")
        .get()
    )
      throw new Error(
        "Pause the current simulation or finish preparing it before creating another run",
      );
    const id = randomUUID(),
      now = new Date().toISOString();
    this.db
      .prepare(
        "INSERT INTO runs(id,created_at,updated_at,status,progress,config_json) VALUES(?,?,?,'preparing','Connecting to the published warehouse API',?)",
      )
      .run(id, now, now, JSON.stringify(config));
    return { id };
  }
  required(id: string, full = false): Row {
    const row = this.db
      .prepare(
        `SELECT ${full ? "*" : "id,created_at,updated_at,status,progress,config_json,summary_json"} FROM runs WHERE id=?`,
      )
      .get(id) as Row | undefined;
    if (!row)
      throw Object.assign(new Error("Simulation run not found"), {
        status: 404,
      });
    return row;
  }
  private key(id: string): number {
    const row = this.db.prepare("SELECT pk FROM runs WHERE id=?").get(id) as
      | Row
      | undefined;
    if (!row)
      throw Object.assign(new Error("Simulation run not found"), {
        status: 404,
      });
    return row.pk;
  }
  run(id: string): Row {
    const r = this.required(id);
    return {
      id,
      created_at: r.created_at,
      updated_at: r.updated_at,
      status: r.status,
      progress: r.progress,
      config: JSON.parse(r.config_json),
      state: r.summary_json ? JSON.parse(r.summary_json) : null,
    };
  }
  list() {
    return (
      this.db
        .prepare("SELECT id FROM runs ORDER BY created_at DESC LIMIT 100")
        .all() as Row[]
    ).map((r) => {
      const run = this.run(r.id);
      return {
        ...run,
        state: run.state
          ? { totals: run.state.totals, now: run.state.now }
          : null,
      };
    });
  }
  status(id: string, status: string, progress: string) {
    this.db
      .prepare("UPDATE runs SET status=?,progress=?,updated_at=? WHERE id=?")
      .run(status, progress, new Date().toISOString(), id);
  }
  config(id: string, config: Row) {
    this.db
      .prepare("UPDATE runs SET config_json=? WHERE id=?")
      .run(JSON.stringify(config), id);
  }
  appendOrders(id: string, rows: SimulationOrder[]) {
    const key = this.key(id);
    const insert = this.db.prepare(
      "INSERT INTO orders(run_id,source_row,date,sku,store,quantity_micro,source_time,uom,order_reference) VALUES(?,?,?,?,?,?,?,?,?)",
    );
    this.transaction(() => {
      for (const r of rows)
        insert.run(
          key,
          r.source_row,
          r.date,
          r.sku,
          r.store,
          r.quantity_micro,
          r.source_time,
          r.uom,
          r.order_reference,
        );
    });
  }
  orders(id: string, date: string): SimulationOrder[] {
    return this.db
      .prepare(
        "SELECT source_row,date,sku,store,quantity_micro,source_time,uom,order_reference FROM orders WHERE run_id=? AND date=? ORDER BY store,sku,source_row",
      )
      .all(this.key(id), date) as unknown as SimulationOrder[];
  }
  init(id: string, release: Row, model: AsrsModel) {
    const key = this.key(id);
    this.db
      .prepare("UPDATE runs SET release_json=?,model_json=? WHERE id=?")
      .run(JSON.stringify(release), JSON.stringify(model), id);
    const insert = this.db.prepare(
      "INSERT INTO loads(run_id,id,sku,address,rack,crane,initial_micro,location) VALUES(?,?,?,?,?,?,?,?)",
    );
    for (const l of model.loads)
      insert.run(
        key,
        l.id,
        l.sku,
        l.address,
        l.rack,
        l.crane,
        l.initial,
        l.address,
      );
  }
  save(id: string, checkpoint: Row, summary: Row) {
    this.db
      .prepare(
        "UPDATE runs SET checkpoint_json=?,summary_json=?,updated_at=? WHERE id=?",
      )
      .run(
        JSON.stringify(checkpoint),
        JSON.stringify(summary),
        new Date().toISOString(),
        id,
      );
  }
  sink(id: string): SimulationSink {
    const key = this.key(id);
    const event = this.db.prepare(
        `INSERT INTO events(run_id,${eventFields.join(",")}) VALUES(${Array(
          eventFields.length + 1,
        )
          .fill("?")
          .join(",")})`,
      ),
      updateLoad = this.db.prepare(
        "UPDATE loads SET quantity_micro=?,location=? WHERE run_id=? AND id=?",
      ),
      receiving = this.db.prepare(
        "INSERT INTO receiving VALUES(?,?,?) ON CONFLICT(run_id,sku) DO UPDATE SET quantity_micro=quantity_micro+excluded.quantity_micro",
      ),
      line = this.db.prepare(
        "UPDATE orders SET fulfilled_micro=fulfilled_micro+?,status=?,shortage_micro=? WHERE run_id=? AND source_row=?",
      ),
      cycle = this.db.prepare("INSERT INTO cycles VALUES(?,?,?,?,?,?)");
    return {
      event: (r) => {
        event.run(key, ...eventFields.map((field) => r[field]));
        if (r.load_id)
          updateLoad.run(r.load_balance_micro, r.load_location, key, r.load_id);
        if (r.receiving_delta_micro)
          receiving.run(key, r.sku, r.receiving_delta_micro);
      },
      line: (sourceRow, quantity, status, shortage = 0) => {
        line.run(quantity, status, shortage, key, sourceRow);
      },
      cycle: (r) => {
        const { phases, ...record } = r;
        cycle.run(
          key,
          r.id,
          r.crane,
          r.start,
          r.end,
          JSON.stringify({ ...record, origin: phases[0].origin }),
        );
      },
    };
  }
  page(id: string, kind: string, query: Row): Row {
    const key = this.key(id);
    const limit = Math.min(
        5000,
        Math.max(1, Math.trunc(Number(query.limit) || 100)),
      ),
      after = Math.max(0, Math.trunc(Number(query.after) || 0));
    if (!Number.isSafeInteger(after)) throw new Error("Invalid cursor");
    const args: (string | number)[] = [key, after];
    if (kind === "events" || kind === "sku-movements") {
      const skuMovements = kind === "sku-movements";
      let where = "run_id=? AND sequence>?";
      if (skuMovements) {
        if (
          query.movement &&
          !Object.hasOwn(skuMovementKinds, String(query.movement))
        )
          throw new Error(
            "Choose consume, replenish or return for the SKU movement filter",
          );
        const kinds = query.movement
          ? skuMovementKinds[String(query.movement)]
          : Object.values(skuMovementKinds).flat();
        where += ` AND kind IN (${kinds.map(() => "?").join(",")})`;
        args.push(...kinds);
      }
      for (const key of ["sku", "source_row", "kind"])
        if (query[key]) {
          where += ` AND ${key}=?`;
          args.push(String(query[key]));
        }
      const latest = query.latest === "1";
      let rows = this.db
        .prepare(
          `SELECT ${eventFields.join(",")},(SELECT json_extract(payload_json,'$.kind') FROM cycles WHERE cycles.run_id=events.run_id AND cycles.id=events.cycle_id) AS _cycle_kind${skuMovements ? ",(SELECT COALESCE(SUM(initial_micro),0) FROM loads WHERE loads.run_id=events.run_id AND loads.sku=events.sku) AS initial_sku_micro" : ""} FROM events WHERE ${where} ORDER BY sequence ${latest ? "DESC" : "ASC"} LIMIT ?`,
        )
        .all(...args, limit) as Row[];
      if (latest) rows.reverse();
      let activeKinds: Map<string, string> | undefined;
      rows = rows.map((row) => {
        const { _cycle_kind, ...event } = row;
        let cycleKind =
          _cycle_kind ||
          (row.kind === "ORDER_OUT"
            ? "pick"
            : ["REPLENISH_IN", "RETURN_PUTAWAY"].includes(row.kind)
              ? "refill"
              : "");
        if (!cycleKind && row.cycle_id) {
          if (!activeKinds) {
            const saved = this.db
              .prepare("SELECT checkpoint_json FROM runs WHERE pk=?")
              .get(key) as Row;
            const checkpoint = saved.checkpoint_json
              ? JSON.parse(saved.checkpoint_json)
              : {};
            activeKinds = new Map(
              (checkpoint.cranes ?? [])
                .filter((c: Row) => c.cycle)
                .map((c: Row) => [c.cycle.id, c.cycle.kind]),
            );
          }
          cycleKind = activeKinds.get(row.cycle_id);
        }
        const type =
          cycleKind === "pick"
            ? "delivery"
            : cycleKind === "refill"
              ? "replenishment"
              : "";
        return {
          ...event,
          task_type: type,
          task_id: type ? createTaskId(type, `${id}-${row.cycle_id}`) : "",
        };
      });
      if (skuMovements)
        rows = rows.map((row) => ({
          ...row,
          movement_type:
            row.kind === "ORDER_OUT"
              ? "consume"
              : row.kind === "RETURN_IN"
                ? "return"
                : "replenish",
          sku_before_micro: row.sku_balance_micro - row.delta_micro,
          sku_after_micro: row.sku_balance_micro,
          consumed_micro: row.kind === "ORDER_OUT" ? row.quantity_micro : 0,
          replenished_micro: ["REPLENISH_IN", "RETURN_PUTAWAY"].includes(
            row.kind,
          )
            ? row.quantity_micro
            : 0,
          returned_micro: row.kind === "RETURN_IN" ? row.quantity_micro : 0,
          tote_before_micro: row.load_id
            ? row.load_balance_micro - row.load_delta_micro
            : null,
          tote_after_micro: row.load_id ? row.load_balance_micro : null,
        }));
      return {
        rows,
        next_after: rows.at(-1)?.sequence ?? after,
        done: rows.length < limit,
      };
    }
    if (kind === "orders") {
      let where = "run_id=? AND source_row>?";
      for (const key of ["sku", "status", "date"])
        if (query[key]) {
          where += ` AND ${key}=?`;
          args.push(String(query[key]));
        }
      const rows: Row[] = (
        this.db
          .prepare(
            `SELECT * FROM orders WHERE ${where} ORDER BY source_row LIMIT ?`,
          )
          .all(...args, limit) as Row[]
      ).map((row) => ({ ...row, run_id: id }));
      return {
        rows,
        next_after: rows.at(-1)?.source_row ?? after,
        done: rows.length < limit,
      };
    }
    throw new Error("Unknown simulation data view");
  }
  stock(id: string, query: Row): Row[] {
    return (
      this.db
        .prepare(
          "SELECT l.*,COALESCE(r.quantity_micro,0) AS receiving_micro FROM loads l LEFT JOIN receiving r ON r.run_id=l.run_id AND r.sku=l.sku WHERE l.run_id=? AND (?='' OR l.sku=?) ORDER BY l.sku,l.id",
        )
        .all(this.key(id), query.sku || "", query.sku || "") as Row[]
    ).map((row) => ({ ...row, run_id: id }));
  }
}
