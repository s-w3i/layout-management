import type { IncomingMessage, ServerResponse } from "node:http";
import { resolve } from "node:path";
import { Storage } from "./storage.js";
import { warehouseRead } from "./asrs-worker.js";
import { csvString, type Row } from "../shared/model.js";

export class AsrsApi {
  private worker: Storage;
  private preparations = new Set<Promise<unknown>>();
  private closing: Promise<void> | null = null;
  constructor(
    private warehouse: Storage,
    data: string,
    private localUrl: string,
  ) {
    this.worker = new Storage(
      resolve(data, "asrs.sqlite"),
      undefined,
      new URL("./asrs-worker.js", import.meta.url),
    );
  }
  get ready() {
    return this.worker.ready;
  }
  private async source(value: Row): Promise<Row> {
    const url = new URL(value.base_url || this.localUrl);
    if (
      !["http:", "https:"].includes(url.protocol) ||
      url.username ||
      url.password ||
      url.pathname !== "/" ||
      url.search ||
      url.hash
    )
      throw new Error(
        "Use the warehouse server's HTTP(S) origin, without a path, password or query string",
      );
    if (
      url.origin === this.localUrl ||
      url.origin === this.localUrl.replace("127.0.0.1", "localhost")
    ) {
      const key = await this.warehouse.call("createKey", {
        name: "Temporary ASRS simulation reader",
        scope: "read",
      });
      return { base_url: this.localUrl, token: key.token, key_id: key.id };
    }
    if (typeof value.token !== "string" || !value.token.trim())
      throw new Error("Paste a read API key from the deployed warehouse");
    return { base_url: url.origin, token: value.token.trim() };
  }
  private async release(source: Row) {
    if (source.key_id) await this.warehouse.call("revokeKey", source.key_id);
  }
  async handle(
    req: IncomingMessage,
    res: ServerResponse,
    url: URL,
    body: (req: IncomingMessage) => Promise<Row>,
    send: (res: ServerResponse, status: number, value: unknown) => void,
  ) {
    if (!url.pathname.startsWith("/api/asrs/")) return false;
    const parts = url.pathname
        .slice("/api/asrs/".length)
        .split("/")
        .map(decodeURIComponent),
      method = req.method;
    const reply = (value: unknown, status = 200) => {
      send(res, status, value);
      return true;
    };
    if (method === "POST" && ["connect", "calendar"].includes(parts[0])) {
      const value = await body(req),
        source = await this.source(value);
      try {
        if (parts[0] === "calendar")
          return reply(
            await warehouseRead(
              source,
              `/api/v1/datasets/${encodeURIComponent(String(value.dataset_id))}/calendar`,
            ),
          );
        const [warehouses, datasets] = await Promise.all([
          warehouseRead(source, "/api/v1/warehouses"),
          warehouseRead(source, "/api/v1/datasets"),
        ]);
        return reply({ source_url: source.base_url, warehouses, datasets });
      } finally {
        await this.release(source);
      }
    }
    if (method === "GET" && parts.length === 1 && parts[0] === "runs")
      return reply(await this.worker.call("list"));
    if (method === "POST" && parts.length === 1 && parts[0] === "runs") {
      const value = await body(req),
        source = await this.source(value);
      let run: Row;
      try {
        run = await this.worker.call("create", {
          name: value.name,
          warehouse_id: value.warehouse_id,
          dataset_id: value.dataset_id,
          selection: value.selection,
          settings: value.settings,
          source_url: source.base_url,
        });
      } catch (e) {
        await this.release(source);
        throw e;
      }
      const preparation = this.worker
        .call("prepare", run.id, source)
        .catch(() => {})
        .finally(() => this.release(source))
        .catch((error) =>
          console.error("ASRS reader cleanup failed:", error.message),
        )
        .finally(() => this.preparations.delete(preparation));
      this.preparations.add(preparation);
      return reply(run, 202);
    }
    if (parts[0] === "runs" && parts[1]) {
      const id = parts[1],
        view = parts[2];
      if (method === "POST" && view === "control" && parts.length === 3)
        return reply(await this.worker.call("control", id, await body(req)));
      if (method === "GET") {
        const query = Object.fromEntries(url.searchParams);
        if (parts.length === 2) return reply(await this.worker.call("run", id));
        if (view === "model") return reply(await this.worker.call("model", id));
        if (view === "stock")
          return reply(await this.worker.call("stock", id, query));
        if (["events", "orders"].includes(view))
          return reply(await this.worker.call("page", id, view, query));
        if (["events.csv", "orders.csv"].includes(view)) {
          const run = await this.worker.call("run", id),
            kind = view.split(".")[0];
          res.writeHead(200, {
            "Content-Type": "text/csv; charset=utf-8",
            "Content-Disposition": `attachment; filename="asrs-${id}-${view}"`,
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
          });
          try {
            let after = 0,
              first = true;
            while (!res.destroyed) {
              const page = await this.worker.call("page", id, kind, {
                ...query,
                after,
                limit: 5000,
              });
              if (!page.rows.length) break;
              const rows = page.rows.map((r: Row) => ({
                run_id: id,
                release_id: run.config.release_id,
                dataset_id: run.config.dataset_id,
                time_zone: run.config.time_zone,
                ...r,
              }));
              const csv = csvString(rows),
                lines = first
                  ? "\ufeff" + csv
                  : csv.slice(csv.indexOf("\r\n") + 2);
              if (!res.write((first ? "" : "\r\n") + lines))
                await new Promise<void>((resolve) => {
                  const done = () => {
                    res.off("drain", done);
                    res.off("close", done);
                    resolve();
                  };
                  res.once("drain", done);
                  res.once("close", done);
                });
              first = false;
              after = page.next_after;
              if (page.done) break;
            }
            res.end();
          } catch (error) {
            res.destroy(error as Error);
          }
          return true;
        }
      }
    }
    return reply({ error: "Unknown ASRS endpoint" }, 404);
  }
  close() {
    return (this.closing ??= (async () => {
      await this.worker.call("shutdown");
      await Promise.allSettled(this.preparations);
      await this.worker.close();
    })());
  }
}
