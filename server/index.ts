import {
  createServer,
  type IncomingMessage,
  type ServerResponse,
} from "node:http";
import { randomUUID, createHash } from "node:crypto";
import { Storage } from "./storage.js";
import { management } from "./management.js";
import { createReadStream, createWriteStream } from "node:fs";
import { readFile, mkdir, stat, unlink } from "node:fs/promises";
import { pipeline } from "node:stream/promises";
import { Transform } from "node:stream";
import { spawn, type ChildProcess } from "node:child_process";
import { Worker } from "node:worker_threads";
import { resolve, extname, basename, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { parse } from "csv-parse/sync";
import { dateRangeError, orderDateBounds } from "../shared/dates.js";
import {
  attributeRows,
  compareLayouts,
  mergeRows,
  type Row,
} from "../shared/model.js";

const root = resolve(fileURLToPath(new URL("../../", import.meta.url)));
const data = resolve(process.env.DATA_DIR || resolve(root, ".data")),
  publicRoot = resolve(root, "dist");
for (const directory of ["uploads", "assets", "jobs", "workspaces"])
  await mkdir(resolve(data, directory), { recursive: true });
const storage = new Storage(resolve(data, "app.sqlite"), data);
await storage.ready;
const port = Number(process.env.PORT ?? 4317);
if (!Number.isInteger(port) || port < 1 || port > 65535)
  throw new Error("Invalid PORT");
type Job = {
  id: string;
  status: "queued" | "running" | "complete" | "failed" | "cancelled";
  progress: string;
  error?: string;
  result?: Row;
  cancel?: () => void;
  created_at: string;
};
const jobs = new Map<string, Job>();

let tail = Promise.resolve();
let stopping = false;
const safeId = (id: string) => {
  if (!/^[a-f0-9-]{36}$/.test(id) && id !== "example-orders")
    throw new Error("Invalid resource ID");
  return id;
};
const json = async (path: string) => JSON.parse(await readFile(path, "utf8"));
function send(res: ServerResponse, status: number, value: unknown) {
  res.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
  });
  res.end(JSON.stringify(value));
}
async function body(
  req: IncomingMessage,
  limit = 64 * 1024 * 1024,
): Promise<Row> {
  const chunks: Buffer[] = [];
  let length = 0;
  for await (const part of req) {
    length += part.length;
    if (length > limit) throw new Error("Request exceeds size limit");
    chunks.push(part);
  }
  const result = JSON.parse(Buffer.concat(chunks).toString("utf8"));
  if (!result || typeof result !== "object" || Array.isArray(result))
    throw new Error("Request body must be an object");
  return result;
}
async function enqueue(
  progress: string,
  run: (job: Job) => Promise<Row>,
): Promise<Job> {
  if (stopping)
    throw Object.assign(new Error("Server is shutting down"), { status: 503 });
  if (
    [...jobs.values()].filter(
      (j) => j.status === "queued" || j.status === "running",
    ).length >= 8
  )
    throw new Error("The job queue is full; wait for a job to finish");
  const job: Job = {
    id: randomUUID(),
    status: "queued",
    progress,
    created_at: new Date().toISOString(),
  };
  jobs.set(job.id, job);
  try {
    await storage.call("saveJob", job);
  } catch (error) {
    jobs.delete(job.id);
    throw error;
  }
  tail = tail.then(async () => {
    if (job.status === "cancelled") return;
    job.status = "running";
    try {
      await storage.call("saveJob", job);
      const result = await run(job);
      if ((job.status as string) === "cancelled") return;
      await storage.call(
        "saveJob",
        { ...job, status: "complete", progress: "Complete" },
        result,
      );
      job.result = result;
      job.status = "complete";
      job.progress = "Complete";
    } catch (error) {
      if ((job.status as string) !== "cancelled") {
        job.status = "failed";
        job.error = error instanceof Error ? error.message : String(error);
      }
    } finally {
      if (job.status !== "complete")
        try {
          await storage.call("saveJob", job);
        } catch {
          job.error =
            (job.error || "Job stopped") +
            "; its status could not be persisted";
        }
      delete job.cancel;
    }
  });
  // Completed result files remain available; keep only a bounded number of job descriptors in memory.
  const finished = [...jobs.values()].filter((j) =>
    ["complete", "failed", "cancelled"].includes(j.status),
  );
  for (const stale of finished.slice(0, Math.max(0, finished.length - 100)))
    jobs.delete(stale.id);
  return job;
}
async function ordersAsset(id: string): Promise<Row> {
  safeId(id);
  try {
    return await storage.call("getAsset", id);
  } catch (error: any) {
    if (id !== "example-orders" || error.status !== 404) throw error;
    const value = await json(resolve(root, "examples/orders.json"));
    const metadata = await json(resolve(root, "examples/orders.meta.json"));
    await storage.call("putAsset", id, value, {
      ...metadata,
      kind: "orders",
      source_level: "aggregated",
    });
    return value;
  }
}
async function native(request: Row, job: Job): Promise<Row> {
  return new Promise((accept, reject) => {
    const child = spawn(resolve(root, "build/layout-engine"), [], {
      cwd: root,
      stdio: ["pipe", "pipe", "pipe"],
    });
    let output = "",
      errors = "";
    const timeout = setTimeout(() => {
      child.kill("SIGKILL");
      reject(new Error("Generation exceeded the ten-minute time limit"));
    }, 600000);
    job.cancel = () => child.kill("SIGTERM");
    child.stdout.setEncoding("utf8");
    child.stdout.on("data", (chunk: string) => {
      output += chunk;
      if (output.length > 128 * 1024 * 1024) {
        child.kill("SIGKILL");
        reject(new Error("Engine output exceeds size limit"));
      }
    });
    child.stderr.setEncoding("utf8");
    child.stderr.on("data", (chunk: string) => {
      errors = (errors + chunk).slice(-8192);
    });
    child.on("error", (error) => {
      clearTimeout(timeout);
      reject(error);
    });
    child.stdin.on("error", () => {});
    child.on("close", (code) => {
      clearTimeout(timeout);
      if (job.status === "cancelled") {
        reject(new Error("Cancelled"));
        return;
      }
      try {
        const result = JSON.parse(output);
        if (code || result.error)
          reject(new Error(result.error ?? errors ?? "Engine failed"));
        else accept(result);
      } catch (error) {
        reject(new Error(errors || "Engine returned an invalid response"));
      }
    });
    child.stdin.end(JSON.stringify(request));
  });
}
async function example(kind: string): Promise<Row> {
  if (!["amr", "asrs"].includes(kind)) throw new Error("Unknown example");
  const readCsv = async (name: string): Promise<Row[]> =>
    parse(await readFile(resolve(root, "examples", name), "utf8"), {
      columns: true,
      bom: true,
      skip_empty_lines: true,
    });
  const [project, velocity, stock, attributeSource] = await Promise.all([
    json(resolve(root, `examples/${kind}.grid.json`)),
    readCsv("velocity.csv"),
    readCsv("stock.csv"),
    readCsv("sku-attributes.csv"),
  ]);
  const attributes = attributeRows(attributeSource);
  const selectedStock = kind === "amr" ? stock : [];
  const rows = mergeRows(velocity, selectedStock, attributes);
  let orders: Row | undefined;
  try {
    orders = await json(resolve(root, "examples/orders.meta.json"));
    orders!.id = "example-orders";
  } catch {}
  if (orders && !(await storage.call("hasAsset", "example-orders")))
    await ordersAsset("example-orders");
  return {
    project,
    velocity,
    stock: selectedStock,
    attributes,
    rows,
    orders,
    mapping:
      kind === "amr"
        ? await json(resolve(root, "examples/store-workstation-mapping.json"))
        : {},
    name:
      kind === "amr" ? "AMR · existing warehouse" : "ASRS · existing warehouse",
    stock_source:
      "Imported existing stock targets; recalculate after changing stock or rack settings.",
  };
}
const server = createServer(async (req, res) => {
  try {
    if (stopping) {
      send(res, 503, { error: "Server is shutting down" });
      return;
    }
    const url = new URL(req.url ?? "/", `http://127.0.0.1:${port}`);
    const host = req.headers.host ?? "";
    if (![`127.0.0.1:${port}`, `localhost:${port}`].includes(host)) {
      send(res, 403, { error: "This server accepts local requests only" });
      return;
    }
    if (
      req.headers.origin &&
      ![`http://127.0.0.1:${port}`, `http://localhost:${port}`].includes(
        req.headers.origin,
      )
    ) {
      send(res, 403, { error: "Cross-origin requests are not allowed" });
      return;
    }
    if (
      await management(req, res, url, {
        storage,
        data,
        body,
        send,
        compute: (request) =>
          native(request, {
            id: randomUUID(),
            status: "running",
            progress: "",
            created_at: new Date().toISOString(),
          }),
      })
    )
      return;
    if (req.method === "GET" && url.pathname === "/api/health") {
      send(res, 200, {
        status: "ok",
        engine: "C++20",
        version: "0.1.0",
        database: await storage.call("health"),
      });
      return;
    }
    if (req.method === "GET" && url.pathname.startsWith("/api/examples/")) {
      send(res, 200, await example(url.pathname.split("/").at(-1)!));
      return;
    }
    if (req.method === "POST" && url.pathname === "/api/compare") {
      const request = await body(req);
      const reference = await storage.call(
        "getAsset",
        safeId(String(request.reference_id)),
      );
      send(res, 200, compareLayouts(request.layout, reference.data));
      return;
    }
    if (req.method === "POST" && url.pathname === "/api/export-workspace") {
      const state = await body(req);
      if (state.orders?.id)
        state.orders_data = await ordersAsset(String(state.orders.id));
      if (state.reference?.id)
        state.reference_data = (
          await storage.call("getAsset", safeId(String(state.reference.id)))
        ).data;
      send(res, 200, { schema: "layout_generator_workspace/v1", ...state });
      return;
    }
    if (req.method === "POST" && url.pathname === "/api/engine") {
      const request = await body(req);
      const command = String(request.command ?? "generate");
      if (
        ![
          "geometry",
          "analyze",
          "stock",
          "generate",
          "validate",
          "movement",
          "swap",
        ].includes(command)
      )
        throw new Error("Unsupported engine command");
      const job = await enqueue(
        command === "geometry"
          ? "Checking warehouse geometry"
          : command === "analyze"
            ? "Analyzing order history"
            : command === "stock"
              ? "Calculating stock and rack requirements"
              : command === "movement"
                ? "Estimating handling-unit movements"
                : command === "swap"
                  ? "Validating inventory scenario swap"
                  : "Allocating stock in the C++ engine",
        async (job) => {
          if (request.orders_id) {
            request.orders = await ordersAsset(String(request.orders_id));
            delete request.orders_id;
          }
          if (
            request.orders &&
            ["analyze", "stock", "generate", "movement"].includes(command)
          ) {
            const error = dateRangeError(
              request.settings ?? {},
              orderDateBounds(request.orders),
            );
            if (error) throw new Error(error);
          }
          if (command === "movement" && request.orders)
            request.orders = { baskets: request.orders.baskets };
          const result = await native(request, job);
          if (command === "generate") {
            result.generated_at = new Date().toISOString();
            if (request.reference_id) {
              const reference = await storage.call(
                "getAsset",
                safeId(String(request.reference_id)),
              );
              result.comparison = compareLayouts(result, reference.data);
            }
          }
          return result;
        },
      );
      send(res, 202, { id: job.id });
      return;
    }
    if (req.method === "POST" && url.pathname === "/api/uploads") {
      const kind = url.searchParams.get("kind") ?? "",
        name = basename(url.searchParams.get("name") ?? "file");
      if (
        ![
          "grid",
          "orders",
          "velocity",
          "stock",
          "attributes",
          "reference",
          "mapping",
          "session",
        ].includes(kind)
      )
        throw new Error("Unsupported import kind");
      const ext = extname(name).toLowerCase();
      if (
        !(
          ["orders"].includes(kind)
            ? [".csv", ".xlsx"]
            : ["grid", "reference", "mapping", "session"].includes(kind)
              ? [".json"]
              : [".csv"]
        ).includes(ext)
      )
        throw new Error("File type does not match the selected input");
      if (
        [...jobs.values()].filter((j) =>
          ["queued", "running"].includes(j.status),
        ).length >= 8
      )
        throw new Error("The job queue is full");
      const id = randomUUID(),
        path = resolve(data, "uploads", id + ext),
        output = resolve(data, "assets", id + ".json");
      let size = 0;
      const sourceHash = createHash("sha256");
      try {
        await pipeline(
          req,
          new Transform({
            transform(chunk, _, done) {
              size += chunk.length;
              sourceHash.update(chunk);
              if (size > 256 * 1024 * 1024)
                done(new Error("Uploads may not exceed 256 MB"));
              else done(null, chunk);
            },
          }),
          createWriteStream(path, { flags: "wx" }),
        );
      } catch (error) {
        await unlink(path).catch(() => {});
        throw error;
      }
      const job = await enqueue("Reading " + name, async (job) => {
        const imported: Row = await new Promise((accept, reject) => {
          const worker = new Worker(
            new URL("./import-worker.js", import.meta.url),
            {
              workerData: {
                path,
                name,
                kind,
                id,
                output,
                databasePath: resolve(data, "app.sqlite"),
              },
              resourceLimits: { maxOldGenerationSizeMb: 1536 },
            },
          );
          job.cancel = () => {
            void worker.terminate();
          };
          const timeout = setTimeout(() => {
            void worker.terminate();
            reject(new Error("Import exceeded the ten-minute time limit"));
          }, 600000);
          worker.on("message", (message: Row) => {
            if (message.progress) job.progress = message.progress;
            if (message.error) {
              clearTimeout(timeout);
              reject(new Error(message.error));
            }
            if (message.result) {
              clearTimeout(timeout);
              const result = message.result;
              if (kind === "reference")
                accept({
                  id,
                  name,
                  kind,
                  summary: result.data.summary,
                  assignment_count: result.data.assignments.length,
                });
              else accept(result);
            }
          });
          worker.on("error", (error) => {
            clearTimeout(timeout);
            reject(error);
          });
          worker.on("exit", (code) => {
            clearTimeout(timeout);
            if (code) void storage.call("failDataset", id).catch(() => {});
            if (code)
              reject(
                new Error(
                  job.status === "cancelled"
                    ? "Cancelled"
                    : "Import worker stopped before completing",
                ),
              );
          });
        });
        try {
          if (job.status === "cancelled") throw new Error("Cancelled");
          if (kind === "session" && imported.data.reference?.id) {
            const rid = imported.data.reference.id;
            await storage.call(
              "putAsset",
              rid,
              await json(resolve(data, "assets", rid + ".json")),
              {
                name: imported.data.reference.name || "Workspace reference",
                kind: "reference",
              },
            );
            await unlink(resolve(data, "assets", rid + ".json"));
          }
          const content = await json(output);
          await storage.call("putAsset", id, content, {
            ...imported,
            data: undefined,
            rows: undefined,
            attributes: undefined,
            source_path: path,
            sha256: sourceHash.digest("hex"),
            kind:
              kind === "session" && imported.data.orders?.id === id
                ? "orders"
                : kind,
          });
          await unlink(output);
          return imported;
        } catch (error) {
          await storage.call("failDataset", id);
          throw error;
        }
      });
      send(res, 202, { id: job.id });
      return;
    }
    if (url.pathname.startsWith("/api/jobs/")) {
      const id = safeId(url.pathname.split("/").at(-1)!);
      const job = jobs.get(id) || (await storage.call("getJob", id));
      if (!job) {
        send(res, 404, { error: "Job not found" });
        return;
      }
      if (req.method === "DELETE") {
        if (["running", "queued"].includes(job.status)) {
          job.status = "cancelled";
          job.progress = "Cancelled";
          job.cancel?.();
          await storage.call("saveJob", job);
        }
        send(res, 200, { status: job.status });
        return;
      }
      if (req.method === "GET") {
        send(res, 200, {
          id,
          status: job.status,
          progress: job.progress,
          error: job.error,
          ...(job.status === "complete" ? { result: job.result } : {}),
        });
        return;
      }
    }
    if (url.pathname === "/api/workspaces" && req.method === "POST") {
      send(res, 201, await storage.call("createWorkspace"));
      return;
    }
    if (url.pathname === "/api/workspaces" && req.method === "GET") {
      send(res, 200, await storage.call("listWorkspaces"));
      return;
    }
    if (url.pathname.startsWith("/api/workspaces/")) {
      const id = safeId(url.pathname.split("/").at(-1)!);
      if (req.method === "GET") {
        const state = await storage.call("getWorkspace", id);
        if (
          state.orders?.id === "example-orders" &&
          !(await storage.call("hasAsset", "example-orders"))
        )
          await ordersAsset("example-orders");
        send(res, 200, state);
        return;
      }
      if (req.method === "PUT") {
        send(
          res,
          200,
          await storage.call("saveWorkspace", id, await body(req)),
        );
        return;
      }
    }
    if (url.pathname.startsWith("/api/")) {
      send(res, 404, { error: "API route not found" });
      return;
    }
    if (req.method !== "GET" && req.method !== "HEAD") {
      send(res, 405, { error: "Method not allowed" });
      return;
    }
    let path = resolve(publicRoot, "." + decodeURIComponent(url.pathname));
    if (path !== publicRoot && !path.startsWith(publicRoot + sep)) {
      send(res, 403, { error: "Invalid path" });
      return;
    }
    try {
      if ((await stat(path)).isDirectory()) path = resolve(path, "index.html");
    } catch {
      path = resolve(publicRoot, "index.html");
    }
    const mime: Record<string, string> = {
      ".html": "text/html; charset=utf-8",
      ".js": "text/javascript; charset=utf-8",
      ".css": "text/css; charset=utf-8",
      ".svg": "image/svg+xml",
      ".woff2": "font/woff2",
      ".png": "image/png",
    };
    const size = (await stat(path)).size;
    res.writeHead(200, {
      "Content-Type": mime[extname(path)] ?? "application/octet-stream",
      "Content-Length": size,
      "X-Content-Type-Options": "nosniff",
      "Content-Security-Policy":
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; font-src 'self'; img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'",
      "Referrer-Policy": "no-referrer",
    });
    if (req.method === "HEAD") res.end();
    else await pipeline(createReadStream(path), res);
  } catch (error) {
    if (!res.headersSent)
      send(res, Number((error as any)?.status) || 400, {
        error: error instanceof Error ? error.message : String(error),
      });
    else res.end();
  }
});
server.once("error", (error: NodeJS.ErrnoException) => {
  console.error(
    error.code === "EADDRINUSE"
      ? `Port ${port} is already in use. Stop the running server with Ctrl+C in its terminal, or choose another port: PORT=${port === 65535 ? 4317 : port + 1} ./scripts/run.sh`
      : `Cannot start Layout Generator: ${error.message}`,
  );
  process.exitCode = 1;
  void storage.close();
});
server.on("close", () => {
  void storage.close();
});
const shutdown = async () => {
  if (stopping) return;
  stopping = true;
  const saves: Promise<unknown>[] = [];
  for (const job of jobs.values())
    if (["running", "queued"].includes(job.status)) {
      job.status = "cancelled";
      job.progress = "Server stopped";
      job.cancel?.();
      saves.push(storage.call("saveJob", job));
    }
  await Promise.all(saves);
  await tail;
  server.close();
};
for (const signal of ["SIGINT", "SIGTERM"] as const)
  process.once(signal, () => {
    void shutdown().catch((error) => {
      console.error(error.message);
      process.exit(1);
    });
  });
server.listen(port, "127.0.0.1", () =>
  console.log(`Layout Generator: http://127.0.0.1:${port}`),
);
for (const signal of ["SIGINT", "SIGTERM"] as const)
  process.on(signal, () => {
    for (const job of jobs.values()) job.cancel?.();
    server.close(() => process.exit(0));
  });
