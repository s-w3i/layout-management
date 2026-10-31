import type { IncomingMessage, ServerResponse } from "node:http";
import { randomUUID } from "node:crypto";
import { mkdir } from "node:fs/promises";
import { resolve } from "node:path";
import { Storage } from "./storage.js";
import { mergeRows, type Row } from "../shared/model.js";
import { openapi } from "./openapi.js";
import { parse } from "csv-parse/sync";

type Context = {
  storage: Storage;
  data: string;
  body: (req: IncomingMessage) => Promise<Row>;
  send: (res: ServerResponse, status: number, value: unknown) => void;
  compute: (request: Row) => Promise<Row>;
};
export async function management(
  req: IncomingMessage,
  res: ServerResponse,
  url: URL,
  c: Context,
): Promise<boolean> {
  if (url.pathname === "/api/openapi.json" && req.method === "GET") {
    c.send(res, 200, openapi);
    return true;
  }
  const external = url.pathname.startsWith("/api/v1/");
  if (!external && !url.pathname.startsWith("/api/manage/")) return false;
  const route = url.pathname
    .replace(/^\/api\/(manage|v1)\//, "")
    .split("/")
    .map(decodeURIComponent);
  const method = req.method || "GET",
    write = !["GET", "HEAD"].includes(method);
  const key = external
    ? await c.storage.call(
        "authenticate",
        (req.headers.authorization || "").replace(/^Bearer /, ""),
        write,
      )
    : null;
  const send = (value: unknown, status = 200) => {
    c.send(res, status, value);
    return true;
  };
  const call = (name: string, ...args: unknown[]) =>
    c.storage.call(name, ...args);
  const query = Object.fromEntries(url.searchParams);
  if (method === "GET") {
    if (route[0] === "datasets" && route.length === 1) {
      const values = await call("listDatasets");
      return send(
        external
          ? values
              .filter((r: Row) => r.kind === "orders")
              .map((r: Row) => ({
                id: r.id,
                name: r.name,
                status: r.status,
                raw_count: r.raw_count,
                created_at: r.created_at,
                date_start: r.metadata.date_start,
                date_end: r.metadata.date_end,
                line_count: r.metadata.line_count,
                source_level: r.raw_count ? "lines" : "aggregated",
              }))
          : values,
      );
    }
    if (
      route[0] === "datasets" &&
      route.length === 3 &&
      ["lines", "daily", "baskets"].includes(route[2])
    )
      return send(await call("datasetRows", route[1], route[2], query));
    if (
      route[0] === "datasets" &&
      route.length === 3 &&
      route[2] === "calendar"
    )
      return send(await call("datasetCalendar", route[1]));
    if (
      route[0] === "datasets" &&
      route.length === 3 &&
      route[2] === "line-feed"
    )
      return send(await call("orderLineFeed", route[1], query));
    if (route[0] === "warehouses" && route.length === 1)
      return send(await call("listWarehouses"));
    if (route[0] === "skus" && route.length === 1)
      return send(await call("listSkus", query.q || ""));
    if (route[0] === "partners" && route.length === 1)
      return send(await call("listPartners"));
    if (route[0] === "releases" && route[1]) {
      const release = await call("getRelease", route[1]);
      if (route.length === 2) return send(release);
      if (
        ["warehouse", "slotting", "inventory", "skus"].includes(route[2]) &&
        route.length === 3
      )
        return send({
          release_id: release.id,
          version: release.version,
          checksum: release.checksum,
          units: release.units,
          data: release[route[2]],
        });
    }
    if (route[0] === "warehouses" && route[1] && route.length <= 3) {
      const id = route[1];
      await call("requireWarehouse", id);
      if (route[2] === "published") return send(await call("published", id));
      if (route[2] === "inventory")
        return send(await call("listInventory", id, query.q || ""));
      if (route[2] === "inventory-snapshot")
        return send(await call("inventorySnapshot", id));
      if (route[2] === "locations")
        return send(await call("listLocations", id));
      if (route[2] === "equipment")
        return send(await call("listEquipment", id));
      if (route[2] === "containers")
        return send(await call("listContainers", id));
      if (route[2] === "tasks") return send(await call("listTasks", id));
      if (route[2] === "reservations")
        return send(await call("listReservations", id));
      if (route[2] === "events")
        return send(
          await call(
            "listEvents",
            id,
            query.after || "",
            Number(query.limit) || 200,
            external ? 0 : Number(query.before) || Number.MAX_SAFE_INTEGER,
          ),
        );
      if (route[2] === "releases") return send(await call("listReleases", id));
      if (route.length === 2 && !external)
        return send(await call("warehouse", id));
    }
    if (!external) {
      if (route[0] === "health") return send(await call("health"));
      if (route[0] === "workspaces") return send(await call("listWorkspaces"));
      if (route[0] === "datasets" && route[1] && route[2] === "lines")
        return send(await call("orderLines", route[1], query));
      if (route[0] === "datasets") return send(await call("listDatasets"));
      if (route[0] === "keys") return send(await call("listKeys"));
    }
  }
  if (method === "POST") {
    const value = await c.body(req);
    if (
      route[0] === "warehouses" &&
      route[1] &&
      route[2] === "inventory" &&
      route.length === 3
    )
      return send(
        await call("postInventory", {
          ...value,
          warehouse_id: route[1],
          ...(key ? { actor: `integration:${key.name}` } : {}),
        }),
        201,
      );
    if (!external) {
      if (
        ["skus", "uoms", "partners"].includes(route[0]) &&
        route[1] === "csv" &&
        route.length === 2
      ) {
        if (typeof value.csv !== "string")
          throw new Error("CSV content is required");
        const rows: Row[] = parse(value.csv, {
          columns: true,
          bom: true,
          skip_empty_lines: true,
          trim: true,
          max_record_size: 65536,
        });
        return send(await call("importMasterRows", route[0], rows));
      }

      if (route[0] === "skus" && route[1] === "import")
        return send(await call("importWorkspaceSkus", value.workspace_id));
      if (route[0] === "skus" && route.length === 1)
        return send(await call("saveSku", value));
      if (route[0] === "partners")
        return send(await call("savePartner", value));
      if (route[0] === "equipment")
        return send(await call("saveEquipment", value));
      if (route[0] === "keys") return send(await call("createKey", value), 201);
      if (route[0] === "warehouses" && route[1] && route[2] === "opening-stock")
        return send(await call("importOpening", route[1], value));
      if (
        route[0] === "warehouses" &&
        route[1] &&
        route[2] === "opening-stock-csv"
      ) {
        if (typeof value.csv !== "string")
          throw new Error("CSV content is required");
        const rows: Row[] = parse(value.csv, {
          columns: true,
          bom: true,
          skip_empty_lines: true,
          trim: true,
          max_record_size: 65536,
        });
        if (
          !rows.length ||
          !["sku", "quantity", "location_id"].every((k) =>
            Object.hasOwn(rows[0], k),
          )
        )
          throw new Error(
            "Opening stock CSV requires sku,quantity,location_id columns containing actual stock counts",
          );
        return send(
          await call("importOpening", route[1], {
            rows,
            actor: value.actor,
            reason: value.reason,
          }),
        );
      }
      if (route[0] === "backups") {
        await mkdir(resolve(c.data, "backups"), { recursive: true });
        return send(
          await call(
            "backup",
            resolve(c.data, "backups", `${randomUUID()}.sqlite`),
          ),
          201,
        );
      }
      if (
        route[0] === "warehouses" &&
        route[1] &&
        ["sync", "publish"].includes(route[2])
      ) {
        const id = route[1],
          w = await call("getWorkspace", id);
        if (!w.project) throw new Error("Save the workspace before continuing");
        if (route[2] === "sync")
          return send(
            await call(
              "syncWarehouse",
              id,
              w.project,
              await c.compute({ command: "geometry", project: w.project }),
              w.name,
              value.time_zone,
            ),
          );
        if (!w.result) throw new Error("Generate a layout before publishing");
        const validation = await c.compute({
          command: "validate",
          project: w.project,
          sku_rows: mergeRows(w.velocity, w.stock, w.attributes),
          settings: w.settings,
          assignments: w.result.assignments,
        });
        return send(
          await call("publish", id, {
            name: value.name,
            expected_revision: value.expected_revision,
            validation,
          }),
          201,
        );
      }
    }
  }
  if (!external && method === "DELETE" && route[0] === "keys" && route[1])
    return send(await call("revokeKey", route[1]));
  return send({ error: "API route or method not found" }, 404);
}
