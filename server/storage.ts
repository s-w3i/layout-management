import { Worker } from "node:worker_threads";

export class Storage {
  private worker: Worker;
  private pending = new Map<
    number,
    { resolve: (result: any) => void; reject: (error: Error) => void }
  >();
  private next = 0;
  private stopped: Error | null = null;
  readonly ready: Promise<void>;
  constructor(
    path: string,
    legacy?: string,
    workerUrl = new URL("./storage-worker.js", import.meta.url),
  ) {
    this.worker = new Worker(workerUrl, {
      workerData: { path, legacy },
    });
    this.ready = new Promise((resolve, reject) => {
      this.worker.on("message", (message) => {
        if (message.ready) resolve();
        if (message.startup_error) reject(new Error(message.startup_error));
        const request = this.pending.get(message.id);
        if (!request) return;
        this.pending.delete(message.id);
        if (message.error)
          request.reject(
            Object.assign(new Error(message.error), { status: message.status }),
          );
        else request.resolve(message.result);
      });
      this.worker.on("error", (error) => {
        reject(error);
        this.fail(error);
      });
      this.worker.on("exit", (code) => {
        const error = new Error(`Database worker stopped (${code})`);
        reject(error);
        this.fail(error);
      });
    });
  }
  private fail(error: Error) {
    this.stopped = Object.assign(error, { status: 503 });
    for (const request of this.pending.values()) request.reject(error);
    this.pending.clear();
  }
  async call<T = any>(method: string, ...args: unknown[]): Promise<T> {
    await this.ready;
    if (this.stopped) throw this.stopped;
    if (method === "saveJob") {
      const { cancel, result, ...state } = args[0] as any;
      args[0] = state;
    }
    return new Promise((resolve, reject) => {
      const id = ++this.next;
      this.pending.set(id, { resolve, reject });
      this.worker.postMessage({ id, method, args });
    });
  }
  async close() {
    await this.call("close");
    await this.worker.terminate();
  }
}
