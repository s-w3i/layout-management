import { parentPort, workerData } from "node:worker_threads";
import { WarehouseDatabase } from "./database.js";

try {
  const database = new WarehouseDatabase(workerData.path);
  if (workerData.legacy) {
    database.migrateLegacy(workerData.legacy);
    database.recoverJobs();
  }
  parentPort!.postMessage({ ready: true });
  parentPort!.on("message", ({ id, method, args }) => {
    try {
      const operation = (database as any)[method];
      if (typeof operation !== "function" || method === "constructor")
        throw new Error("Unknown database operation");
      const result = operation.apply(database, args);
      parentPort!.postMessage({ id, result });
    } catch (error: any) {
      parentPort!.postMessage({
        id,
        error: error.message,
        status: error.status || 400,
      });
    }
  });
} catch (error: any) {
  parentPort!.postMessage({ startup_error: error.message });
}
