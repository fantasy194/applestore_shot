import { spawn, spawnSync } from "node:child_process";
import { once } from "node:events";
import { existsSync } from "node:fs";
import { appendFile, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { parseArgs } from "node:util";
import { localCall, project, pwaDirectory, serverConnection } from "./pwa-paths.mjs";

const { values } = parseArgs({ options: {
  port: { type: "string", default: "17653" }, "data-dir": { type: "string" },
  "no-open": { type: "boolean", default: false }, "no-tunnel": { type: "boolean", default: false },
  "skip-build": { type: "boolean", default: false }, cloudflared: { type: "string" },
  background: { type: "boolean", default: false },
} });
const port = Number(values.port);
if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error("端口必须为 1–65535 的整数。");
const directory = pwaDirectory(values["data-dir"]);
const logPath = join(directory, "server.log");
const runPath = join(directory, "pwa-run.json");
const python = join(project, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
const env = { ...process.env, PYTHONPATH: "", PYTHONUTF8: "1" };
let backend, tunnel, preparing, connection, stopping = false, stopPromise;
let logWrites = Promise.resolve();

async function prepare(command, args) {
  if (stopping) throw new Error("已停止启动。");
  preparing = spawn(command, args, { cwd: project, env, windowsHide: true, stdio: "inherit" });
  const child = preparing;
  const code = await new Promise((resolve, reject) => { child.once("error", reject); child.once("exit", resolve); });
  preparing = undefined;
  if (code !== 0) throw new Error(`${command} 执行失败。`);
}

async function stop(code = 0) {
  if (stopPromise) return stopPromise;
  stopping = true;
  process.exitCode = code;
  stopPromise = (async () => {
    preparing?.kill();
    const tunnelDone = tunnel && tunnel.exitCode === null ? once(tunnel, "exit").catch(() => {}) : Promise.resolve();
    tunnel?.stdin?.end();
    if (connection && backend && backend.exitCode === null) {
      try { await localCall(connection, "shutdown"); } catch { /* Already stopping. */ }
    }
    if (backend && backend.exitCode === null) {
      const ended = once(backend, "exit").catch(() => {});
      const timer = setTimeout(() => backend.kill(), 12000);
      await ended; clearTimeout(timer);
    }
    if (tunnel && tunnel.exitCode === null) {
      const timer = setTimeout(() => tunnel.kill(), 10000);
      await tunnelDone; clearTimeout(timer);
    }
    await logWrites;
    try {
      const saved = JSON.parse(await readFile(runPath, "utf8"));
      if (saved.pid === process.pid) await rm(runPath, { force: true });
    } catch { /* Preserve another launcher's metadata. */ }
    if (!process.stdin.isTTY) process.stdin.destroy();
  })();
  return stopPromise;
}
process.once("SIGINT", () => void stop());
process.once("SIGTERM", () => void stop());
if (!process.stdin.isTTY && !values.background) { process.stdin.resume(); process.stdin.once("end", () => void stop()); }

async function main() {
  try {
    const existing = await serverConnection(directory);
    const snapshot = await localCall(existing, "snapshot");
    if (Array.isArray(snapshot.accounts)) {
      console.log(`PWA 已在运行：${existing.origin}\n数据目录：${directory}\n使用 pnpm pwa:stop 停止后重新启动。`);
      await stop(); return;
    }
  } catch { /* Stale metadata is not a running service. */ }
  await mkdir(directory, { recursive: true, mode: 0o700 });
  const workerReady = existsSync(python) && spawnSync(python, ["-c", "import requests,srp,playwright,qrcode,keyring,cryptography,pywebpush,tzdata"], { env, windowsHide: true, stdio: "ignore" }).status === 0;
  if (!workerReady) await prepare(process.execPath, ["scripts/python.mjs", "scripts/setup_worker.py"]);
  const www = join(project, ".local-runtime", "www");
  const executable = join(project, "local-web", "target", "release", process.platform === "win32" ? "duo-killer-web.exe" : "duo-killer-web");
  if (!values["skip-build"]) {
    await prepare(process.execPath, [join(project, "node_modules", "typescript", "bin", "tsc"), "--noEmit"]);
    await prepare(process.execPath, [join(project, "node_modules", "vite", "bin", "vite.js"), "build", "--mode", "local-web", "--outDir", www, "--emptyOutDir"]);
    await prepare("cargo", ["build", "--release", "--locked", "--manifest-path", join(project, "local-web", "Cargo.toml")]);
  }
  if (!existsSync(executable) || !existsSync(join(www, "index.html"))) throw new Error("没有已构建的本地 Web，请去掉 --skip-build 后重试。");
  if (stopping) return;
  await appendFile(logPath, `\nPWA start ${new Date().toISOString()}\n`, { mode: 0o600 });
  backend = spawn(executable, ["--no-open", "--source-worker", "--web-root", www, "--port", String(port), "--data-dir", directory],
    { cwd: project, env, windowsHide: true, stdio: ["pipe", "pipe", "pipe"] });
  let buffer = "";
  const launched = new Promise((resolve, reject) => {
    backend.once("error", reject);
    backend.once("exit", (code) => { reject(new Error(`本地 PWA 已退出（${code}）。请检查 ${logPath}`)); if (!stopping) void stop(code || 0); });
    const collect = (chunk) => {
      const text = chunk.toString(); process.stdout.write(text);
      logWrites = logWrites.then(() => appendFile(logPath, text, { mode: 0o600 }));
      buffer = (buffer + text).slice(-32000);
      const match = buffer.match(/(http:\/\/127\.0\.0\.1:\d+)\/#access=([a-f0-9]{64})/);
      if (match && !connection) { connection = { origin: match[1], token: match[2] }; resolve(); }
    };
    backend.stdout.on("data", collect); backend.stderr.on("data", collect);
  });
  const timer = setTimeout(() => { if (!connection) void stop(1); }, 45000);
  try { await launched; } finally { clearTimeout(timer); }
  await logWrites;
  await writeFile(runPath, JSON.stringify({ origin: connection.origin, pid: process.pid, backend_pid: backend.pid, data_dir: directory }), { mode: 0o600 });
  console.log(`数据目录：${directory}\n密码与启动链接保存在：${logPath}`);
  if (values["no-tunnel"]) return;
  const args = ["scripts/quick-tunnel.mjs", "--origin", connection.origin, "--data-dir", directory, "--log-file", logPath];
  if (values["no-open"]) args.push("--no-open");
  if (values.cloudflared) args.push("--cloudflared", values.cloudflared);
  tunnel = spawn(process.execPath, args, { cwd: project, env, windowsHide: true, stdio: ["pipe", "inherit", "inherit"] });
  tunnel.once("error", (error) => { console.error(error.message); void stop(1); });
  tunnel.once("exit", (code) => { if (!stopping) void stop(code || 0); });
}
try { await main(); } catch (error) { console.error(error.message); await stop(1); }
