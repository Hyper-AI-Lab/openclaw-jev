// RMP's warm gateway caller: OpenClaw's own SDK loaded once, one JSON line per call.
//
// The `openclaw gateway call` CLI starts node and loads its commands, config and plugins on
// every call: 8 to 45 s on a loaded host (Sep 30 2026). Here the load happens once, and a
// sessions.abort takes about a tenth of a second. Only the methods in ALLOWED are served.
//
// argv[2]: the openclaw package directory. stdin: {"id", "method", "params", "timeoutMs"} per line.
// stdout: {"ready": true} once, then {"id", "ok", "result"} or {"id", "ok": false, "error"} per call.
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { createInterface } from "node:readline";
import { pathToFileURL } from "node:url";

const ALLOWED = new Set(["sessions.abort"]);
const packageDir = process.argv[2];
const manifest = JSON.parse(readFileSync(join(packageDir, "package.json"), "utf8"));
const entry = manifest.exports["./plugin-sdk/gateway-runtime"].default;
const { callGatewayFromCli } = await import(pathToFileURL(join(packageDir, entry)).href);

const reply = (message) => process.stdout.write(JSON.stringify(message) + "\n");

async function handle(line) {
  let request;
  try {
    request = JSON.parse(line);
  } catch {
    return;
  }
  const { id, method, params, timeoutMs } = request;
  if (!ALLOWED.has(method)) {
    reply({ id, ok: false, error: `method not allowed: ${method}` });
    return;
  }
  try {
    const result = await callGatewayFromCli(method, { json: true, timeout: String(timeoutMs || 15000) }, params || {});
    reply({ id, ok: true, result });
  } catch (err) {
    reply({ id, ok: false, error: String(err?.message || err).slice(0, 500) });
  }
}

reply({ ready: true });
for await (const line of createInterface({ input: process.stdin })) {
  handle(line);
}
process.exit(0);
