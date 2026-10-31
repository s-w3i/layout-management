import { createServer } from "node:net";

const port = Number(process.env.PORT ?? 4317);
if (!Number.isInteger(port) || port < 1 || port > 65535) {
  console.error("PORT must be a whole number between 1 and 65535.");
  process.exit(1);
}

const probe = createServer();
probe.once("error", (error) => {
  console.error(
    error.code === "EADDRINUSE"
      ? `Port ${port} is already in use. If Layout Generator is running, open http://127.0.0.1:${port} or stop it with Ctrl+C in its terminal before restarting. To use another port: PORT=${port === 65535 ? 4317 : port + 1} ./scripts/run.sh`
      : `Cannot use port ${port}: ${error.message}`,
  );
  process.exitCode = 1;
});
probe.listen(port, "127.0.0.1", () => probe.close());
