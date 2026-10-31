import test from "node:test";
import assert from "node:assert/strict";
import { createServer } from "node:net";
import { once } from "node:events";
import { spawnSync } from "node:child_process";

test("occupied ports stop the launcher before building and report a clear server error", async (t) => {
  const server = createServer();
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  t.after(() => server.close());
  const address = server.address();
  assert.ok(address && typeof address !== "string");
  const env = { ...process.env, PORT: String(address.port) };
  for (const [command, args] of [
    [process.execPath, ["scripts/check-port.mjs"]],
    ["bash", ["scripts/run.sh"]],
    [process.execPath, ["dist-server/server/index.js"]],
  ] as const) {
    const result = spawnSync(command, [...args], {
      env,
      encoding: "utf8",
      timeout: 5000,
    });
    assert.equal(result.error, undefined);
    assert.equal(result.status, 1);
    assert.match(result.stderr, /already in use/);
    assert.match(result.stderr, /Ctrl\+C/);
    assert.ok(
      result.stderr.includes(
        `PORT=${address.port === 65535 ? 4317 : address.port + 1}`,
      ),
    );
    assert.doesNotMatch(result.stderr, /Unhandled/);
    assert.doesNotMatch(result.stdout, /build:native|vite/);
    assert.equal(server.listening, true);
  }
});

test("the preflight releases an available custom port", async () => {
  const server = createServer();
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const address = server.address();
  assert.ok(address && typeof address !== "string");
  await new Promise<void>((resolve) => server.close(() => resolve()));
  const result = spawnSync(process.execPath, ["scripts/check-port.mjs"], {
    env: { ...process.env, PORT: String(address.port) },
    encoding: "utf8",
    timeout: 5000,
  });
  assert.equal(result.status, 0, result.stderr);
  server.listen(address.port, "127.0.0.1");
  await once(server, "listening");
  await new Promise<void>((resolve) => server.close(() => resolve()));
});

test("the launcher preflight rejects invalid port values", () => {
  for (const port of ["0", "65536", "invalid"]) {
    const result = spawnSync(process.execPath, ["scripts/check-port.mjs"], {
      env: { ...process.env, PORT: port },
      encoding: "utf8",
      timeout: 5000,
    });
    assert.equal(result.status, 1);
    assert.match(result.stderr, /between 1 and 65535/);
  }
});
