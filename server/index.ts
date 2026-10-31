import {
  createServer,
  type IncomingMessage,
  type ServerResponse,
} from "node:http";
import { randomUUID } from "node:crypto";
import { createReadStream, createWriteStream } from "node:fs";
import {
  readFile,
  writeFile,
  mkdir,
  rename,
  stat,
  unlink,
} from "node:fs/promises";
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
const data = resolve(root, ".data"),
  publicRoot = resolve(root, "dist");
for (const directory of ["uploads", "assets", "jobs", "workspaces"])
  await mkdir(resolve(data, directory), { recursive: true });
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
const saves = new Map<string, Promise<void>>();
let tail = Promise.resolve();
const safeId = (id: string) => {
  if (!/^[a-f0-9-]{36}$/.test(id) && id !== "example-orders")
    throw new Error("Invalid resource ID");
  return id;
};
const json = async (path: string) => JSON.parse(await readFile(path, "utf8"));
const atomic = async (path: string, value: unknown) => {
  const tmp = path + "." + randomUUID() + ".tmp";
  await writeFile(tmp, JSON.stringify(value));
  await rename(tmp, path);
};
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
function enqueue(progress: string, run: (job: Job) => Promise<Row>): Job {
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
  tail = tail.then(async () => {
    if (job.status === "cancelled") return;
    job.status = "running";
    try {
      const result = await run(job);
      if ((job.status as string) === "cancelled") return;
      await atomic(resolve(data, "jobs", job.id + ".json"), result);
      job.status = "complete";
      job.progress = "Complete";
    } catch (error) {
      if ((job.status as string) !== "cancelled") {
        job.status = "failed";
        job.error = error instanceof Error ? error.message : String(error);
      }
    } finally {
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
  return json(
    id === "example-orders"
      ? resolve(root, "examples/orders.json")
      : resolve(data, "assets", safeId(id) + ".json"),
  );
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
    if (req.method === "GET" && url.pathname === "/api/health") {
      send(res, 200, { status: "ok", engine: "C++20", version: "0.1.0" });
      return;
    }
    if (req.method === "GET" && url.pathname.startsWith("/api/examples/")) {
      send(res, 200, await example(url.pathname.split("/").at(-1)!));
      return;
    }
    if (req.method === "POST" && url.pathname === "/api/compare") {
      const request = await body(req);
      const reference = await json(
        resolve(data, "assets", safeId(String(request.reference_id)) + ".json"),
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
          await json(
            resolve(
              data,
              "assets",
              safeId(String(state.reference.id)) + ".json",
            ),
          )
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
      const job = enqueue(
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
              const reference = await json(
                resolve(
                  data,
                  "assets",
                  safeId(String(request.reference_id)) + ".json",
                ),
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
      try {
        await pipeline(
          req,
          new Transform({
            transform(chunk, _, done) {
              size += chunk.length;
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
      const job = enqueue(
        "Reading " + name,
        async (job) =>
          new Promise((accept, reject) => {
            const worker = new Worker(
              new URL("./import-worker.js", import.meta.url),
              {
                workerData: { path, name, kind, id, output },
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
              if (code)
                reject(
                  new Error(
                    job.status === "cancelled"
                      ? "Cancelled"
                      : "Import worker stopped before completing",
                  ),
                );
            });
          }),
      );
      send(res, 202, { id: job.id });
      return;
    }
    if (url.pathname.startsWith("/api/jobs/")) {
      const id = safeId(url.pathname.split("/").at(-1)!);
      const job = jobs.get(id);
      if (!job) {
        send(res, 404, { error: "Job not found" });
        return;
      }
      if (req.method === "DELETE") {
        if (["running", "queued"].includes(job.status)) {
          job.status = "cancelled";
          job.progress = "Cancelled";
          job.cancel?.();
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
          ...(job.status === "complete"
            ? { result: await json(resolve(data, "jobs", id + ".json")) }
            : {}),
        });
        return;
      }
    }
    if (url.pathname === "/api/workspaces" && req.method === "POST") {
      const id = randomUUID();
      await atomic(resolve(data, "workspaces", id + ".json"), {});
      send(res, 201, { id });
      return;
    }
    if (url.pathname.startsWith("/api/workspaces/")) {
      const id = safeId(url.pathname.split("/").at(-1)!);
      const path = resolve(data, "workspaces", id + ".json");
      await stat(path);
      if (req.method === "GET") {
        send(res, 200, await json(path));
        return;
      }
      if (req.method === "PUT") {
        const state = await body(req);
        if (
          !Number.isSafeInteger(state._save_revision) ||
          state._save_revision < 1
        )
          throw new Error("A positive workspace revision is required");
        let stale = false;
        const save = (saves.get(id) ?? Promise.resolve())
          .catch(() => {})
          .then(async () => {
            const previous = await json(path);
            if (
              Number(state._save_revision ?? 0) <=
              Number(previous._save_revision ?? 0)
            ) {
              stale = true;
              return;
            }
            await atomic(path, state);
          });
        saves.set(id, save);
        try {
          await save;
        } finally {
          if (saves.get(id) === save) saves.delete(id);
        }
        if (stale)
          send(res, 409, {
            error:
              "This workspace has a newer saved version. Reload it or export a copy of your changes.",
          });
        else send(res, 200, { saved_at: new Date().toISOString() });
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
      send(res, 400, {
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
});
server.listen(port, "127.0.0.1", () =>
  console.log(`Layout Generator: http://127.0.0.1:${port}`),
);
for (const signal of ["SIGINT", "SIGTERM"] as const)
  process.on(signal, () => {
    for (const job of jobs.values()) job.cancel?.();
    server.close(() => process.exit(0));
  });
