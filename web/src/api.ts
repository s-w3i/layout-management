import type { Row } from "../../shared/model";
export async function api(
  path: string,
  options: RequestInit = {},
): Promise<any> {
  const response = await fetch(path, {
    ...options,
    headers: {
      ...(options.body && typeof options.body === "string"
        ? { "Content-Type": "application/json" }
        : {}),
      ...options.headers,
    },
  });
  const data = await response.json();
  if (!response.ok)
    throw new Error(data.error ?? `Request failed (${response.status})`);
  return data;
}
export async function pollJob(
  id: string,
  onProgress: (text: string, id: string) => void,
  signal?: AbortSignal,
): Promise<Row> {
  while (true) {
    if (signal?.aborted) throw new DOMException("Cancelled", "AbortError");
    const job = await api(`/api/jobs/${id}`, { signal });
    onProgress(job.progress, id);
    if (job.status === "complete") return job.result;
    if (job.status === "failed")
      throw new Error(job.error ?? "The operation failed");
    if (job.status === "cancelled")
      throw new DOMException("Cancelled", "AbortError");
    await new Promise<void>((resolve, reject) => {
      const abort = () => {
        clearTimeout(timer);
        reject(new DOMException("Cancelled", "AbortError"));
      };
      const timer = setTimeout(() => {
        signal?.removeEventListener("abort", abort);
        resolve();
      }, 500);
      signal?.addEventListener("abort", abort, { once: true });
    });
  }
}
export async function engine(
  request: Row,
  onProgress: (text: string, id: string) => void,
  signal?: AbortSignal,
) {
  const job = await api("/api/engine", {
    method: "POST",
    body: JSON.stringify(request),
    signal,
  });
  return pollJob(job.id, onProgress, signal);
}
export async function upload(
  file: File,
  kind: string,
  onProgress: (text: string, id: string) => void,
): Promise<Row> {
  const job = await api(
    `/api/uploads?kind=${encodeURIComponent(kind)}&name=${encodeURIComponent(file.name)}`,
    { method: "POST", body: file },
  );
  return pollJob(job.id, onProgress);
}
export function download(
  name: string,
  data: unknown,
  type = "application/json",
) {
  const blob = new Blob(
    [typeof data === "string" ? data : JSON.stringify(data, null, 2)],
    { type },
  );
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
