import { WarehouseDatabase } from "./database.js";
import { parentPort, workerData } from "node:worker_threads";
import { createReadStream } from "node:fs";
import { readFile, writeFile } from "node:fs/promises";
import { randomUUID } from "node:crypto";
import { dirname, resolve } from "node:path";
import { parse } from "csv-parse";
import { parse as parseSync } from "csv-parse/sync";
import ExcelJS from "exceljs";
import { attributeRows, type Row } from "../shared/model.js";

const { path, name, kind, id, output, databasePath } = workerData as Record<
  string,
  string
>;
const scalar = (value: any): any =>
  value && typeof value === "object" && !(value instanceof Date)
    ? "result" in value
      ? value.result
      : "text" in value
        ? value.text
        : "richText" in value
          ? value.richText.map((v: any) => v.text).join("")
          : null
    : value;
function dateValue(value: unknown): string | null {
  if (value instanceof Date)
    return Number.isFinite(value.getTime())
      ? value.toISOString().slice(0, 10)
      : null;
  if (typeof value === "number" && Number.isFinite(value))
    return new Date(Date.UTC(1899, 11, 30) + Math.floor(value) * 86400000)
      .toISOString()
      .slice(0, 10);
  const s = String(value ?? "").trim();
  if (!/^\d{4}-\d{2}-\d{2}(?:T.*)?$/.test(s)) return null;
  const d = new Date(s.slice(0, 10) + "T00:00:00Z");
  return Number.isFinite(d.getTime()) &&
    d.toISOString().slice(0, 10) === s.slice(0, 10)
    ? s.slice(0, 10)
    : null;
}
async function* tableRows(): AsyncGenerator<any[]> {
  if (name.toLowerCase().endsWith(".xlsx")) {
    const reader = new ExcelJS.stream.xlsx.WorkbookReader(path, {
      entries: "emit",
      sharedStrings: "cache",
      styles: "cache",
      worksheets: "emit",
    });
    for await (const sheet of reader) {
      for await (const row of sheet)
        yield (row.values as any[]).slice(1).map(scalar);
      break;
    }
  } else {
    const parser = createReadStream(path).pipe(
      parse({
        bom: true,
        relax_column_count: true,
        skip_empty_lines: true,
        max_record_size: 1024 * 1024,
      }),
    );
    for await (const row of parser) yield row;
  }
}
const database =
  databasePath && kind === "orders"
    ? new WarehouseDatabase(databasePath)
    : null;
async function importOrders() {
  database?.beginRawImport(id, name);
  let sourceRow = 0;
  let rawRows: Row[] = [];
  let header: Map<string, number> | null = null,
    lines = 0,
    ignored = 0,
    seen = 0;
  const daily = new Map<string, Row>(),
    baskets = new Map<
      string,
      { date: string; store: string; skus: Set<string> }
    >();
  const skuSet = new Set<string>(),
    storeSet = new Set<string>();
  let first = "",
    last = "";
  const columnAliases: Record<string, string> = {
    sku: "item or sku",
    quantity_ea: "quantity (in ea)",
    quantity: "quantity (in ea)",
    store: "store id",
    length: "length (m)",
    width: "width (m)",
    height: "height (m)",
    weight: "weight (kg)",
  };
  for await (const values of tableRows()) {
    sourceRow++;
    if (!header) {
      const names = values
        .map((v) =>
          String(v ?? "")
            .trim()
            .toLowerCase(),
        )
        .map((n) => columnAliases[n] ?? n);
      if (
        ["date", "item or sku", "quantity (in ea)"].every((n) =>
          names.includes(n),
        )
      ) {
        header = new Map(names.map((n, i) => [n, i]));
      }
      if (++seen > 100)
        throw new Error(
          "No header found with Date, Item or SKU, Quantity (in EA)",
        );
      continue;
    }
    const value = (key: string) => values[header!.get(key) ?? -1];
    const sku = String(value("item or sku") ?? "").trim(),
      date = dateValue(value("date")),
      store = String(value("store id") ?? "").trim();
    if (!sku || !date) {
      ignored++;
      continue;
    }
    let qty = Number(value("quantity (in ea)") ?? 0);
    if (!Number.isFinite(qty)) qty = 0;
    let conversion = Number(value("uom conversionqty") ?? 1);
    if (!Number.isFinite(conversion) || conversion <= 0) conversion = 1;
    if (database) {
      rawRows.push({
        source_row: sourceRow,
        date,
        sku,
        store,
        quantity_ea: qty,
        conversion,
        source_time:
          value("time") instanceof Date
            ? value("time").toISOString().slice(11, 23)
            : typeof value("time") === "number"
              ? new Date(Math.round((value("time") % 1) * 86400000))
                  .toISOString()
                  .slice(11, 23)
              : String(value("time") ?? ""),
        uom: String(value("uom") ?? ""),
        order_reference: String(
          value("order id") ?? value("order no") ?? value("order number") ?? "",
        ),
      });
      if (rawRows.length >= 1000) {
        database.appendOrderLines(id, rawRows);
        rawRows = [];
      }
    }
    skuSet.add(sku);
    if (skuSet.size > 10000)
      throw new Error("At most 10,000 distinct SKUs are supported");
    const key = JSON.stringify([date, sku]);
    let entry = daily.get(key);
    if (!entry) {
      entry = {
        date,
        sku,
        quantity: 0,
        raw_quantity: 0,
        frequency: 0,
        conversion: 0,
        requirements: {},
      };
      daily.set(key, entry);
    }
    entry.quantity += Math.max(0, qty);
    entry.raw_quantity += qty;
    entry.frequency++;
    entry.conversion = Math.max(entry.conversion, conversion);
    for (const [physical, source] of [
      ["length", "length (m)"],
      ["width", "width (m)"],
      ["height", "height (m)"],
      ["weight", "weight (kg)"],
    ]) {
      const v = Number(value(source));
      if (Number.isFinite(v) && v > 0)
        entry.requirements[`max_item_${physical}`] = Math.max(
          entry.requirements[`max_item_${physical}`] ?? 0,
          v,
        );
    }
    if (store) {
      storeSet.add(store);
      const k = JSON.stringify([date, store]);
      let basket = baskets.get(k);
      if (!basket) {
        basket = { date, store, skus: new Set() };
        baskets.set(k, basket);
      }
      basket.skus.add(sku);
    }
    if (!first || date < first) first = date;
    if (!last || date > last) last = date;
    lines++;
    if (lines % 10000 === 0)
      parentPort!.postMessage({
        progress: `Read ${lines.toLocaleString()} order lines`,
        lines,
      });
    if (lines > 2000000 || baskets.size > 100000)
      throw new Error(
        "Order file exceeds 2 million lines or 100,000 store/day groups",
      );
  }
  if (database && rawRows.length) database.appendOrderLines(id, rawRows);
  if (!header || !lines) throw new Error("No valid order rows found");
  const data = {
    daily: [...daily.values()].sort(
      (a, b) => a.date.localeCompare(b.date) || a.sku.localeCompare(b.sku),
    ),
    baskets: [...baskets.values()]
      .sort(
        (a, b) =>
          a.date.localeCompare(b.date) || a.store.localeCompare(b.store),
      )
      .map((b) => ({ ...b, skus: [...b.skus].sort() })),
  };
  await writeFile(output, JSON.stringify(data));
  return {
    id,
    name,
    kind,
    line_count: lines,
    ignored_rows: ignored,
    sku_count: skuSet.size,
    store_count: storeSet.size,
    store_day_count: baskets.size,
    date_start: first,
    date_end: last,
    stores: [...storeSet].sort(),
  };
}
try {
  let result: Row;
  if (kind === "orders") result = await importOrders();
  else {
    const raw = await readFile(path, "utf8");
    if (["grid", "reference", "mapping", "session"].includes(kind)) {
      const data = JSON.parse(raw);
      if (!data || typeof data !== "object" || Array.isArray(data))
        throw new Error("JSON file must contain an object");
      if (kind === "grid" && (!data.grid || !Array.isArray(data.markers)))
        throw new Error("File is not a warehouse grid project");
      if (kind === "reference" && !Array.isArray(data.assignments))
        throw new Error("Reference file must contain layout assignments");
      if (kind === "session") {
        if (
          data.schema !== "layout_generator_workspace/v1" ||
          !data.project?.grid
        )
          throw new Error("File is not a Layout Generator workspace");
        if (data.orders_data) {
          await writeFile(output, JSON.stringify(data.orders_data));
          delete data.orders_data;
          data.orders = { ...data.orders, id };
        }
        if (data.reference_data) {
          const referenceId = randomUUID();
          await writeFile(
            resolve(dirname(output), referenceId + ".json"),
            JSON.stringify({ data: data.reference_data }),
          );
          data.reference = { ...data.reference, id: referenceId };
          delete data.reference_data;
        }
      }
      result = { id, name, kind, data };
    } else {
      const rows: Row[] = parseSync(raw, {
        columns: true,
        bom: true,
        skip_empty_lines: true,
        trim: true,
        max_record_size: 1024 * 1024,
      });
      if (!rows.length || !Object.hasOwn(rows[0], "sku"))
        throw new Error("CSV must contain a sku column and at least one row");
      result = { id, name, kind, rows };
      if (kind === "attributes") result.attributes = attributeRows(rows);
    }
    if (
      kind !== "session" ||
      !result.data.orders?.id ||
      result.data.orders.id !== id
    )
      await writeFile(output, JSON.stringify(result));
  }
  parentPort!.postMessage({ result });
} catch (error) {
  database?.failDataset(id);
  parentPort!.postMessage({
    error: error instanceof Error ? error.message : String(error),
  });
  process.exitCode = 1;
} finally {
  database?.close();
}
