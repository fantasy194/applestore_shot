import { spawn } from "node:child_process";
import { readFile, writeFile, mkdir, rm } from "node:fs/promises";
import { existsSync } from "node:fs";
import { resolve, join } from "node:path";
import { parseArgs } from "node:util";

const { values } = parseArgs({
  options: {
    origin: { type: "string", default: "http://127.0.0.1:17653" },
    "data-dir": { type: "string", default: ".local-data" },
    "log-file": { type: "string" },
    cloudflared: { type: "string" },
    "no-open": { type: "boolean", default: false },
  },
});
const origin = values.origin;
if (
  !/^http:\/\/127\.0\.0\.1:[1-9][0-9]{0,4}$/.test(origin) ||
  Number(new URL(origin).port) > 65535
)
  throw new Error("仅允许本机 http://127.0.0.1:<端口>。");
const directory = resolve(values["data-dir"]);
const log = resolve(values["log-file"] || join(directory, "server.log"));
const lines = await readFile(log, "utf8");
const token = [
  ...lines.matchAll(/(http:\/\/127\.0\.0\.1:\d+)\/#access=([a-f0-9]{64})/g),
]
  .reverse()
  .find((match) => match[1] === origin)?.[2];
if (!token)
  throw new Error(
    "启动日志中没有当前服务的本机启动链接，请先启动本地 Web，或指定 --log-file。",
  );
async function api(path, body) {
  const response = await fetch(`${origin}/api/${path}`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      Origin: origin,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
    redirect: "error",
    signal: AbortSignal.timeout(7000),
  });
  if (!response.ok)
    throw new Error(
      `本地服务未接受隧道设置（${response.status}），请使用最新版本。`,
    );
  return response.json();
}
await api("snapshot", {});
const html = await (
  await fetch(origin, { signal: AbortSignal.timeout(5000) })
).text();
if (html.includes("/@vite/client") || html.includes('src="/src/main.tsx"'))
  throw new Error("请连接构建后的本地 Web，不要将 Vite 开发服务器暴露到公网。");
const bundled = resolve(
  ".tools",
  process.platform === "win32" ? "cloudflared.exe" : "cloudflared",
);
const executable =
  values.cloudflared || (existsSync(bundled) ? bundled : "cloudflared");
await mkdir(directory, { recursive: true, mode: 0o700 });
let child,
  url,
  stopping = false,
  ready = false,
  startupTimer;
async function stop(code = 0) {
  if (stopping) return;
  stopping = true;
  clearTimeout(startupTimer);
  process.exitCode = code;
  try {
    if (ready) await api("tunnel/register", { origin: null, expected_origin: url });
  } catch {
    /* An already stopped server needs no unregistration. */
  }
  if (child && child.exitCode === null) child.kill();
  try {
    const saved = JSON.parse(
      await readFile(join(directory, "tunnel.json"), "utf8"),
    );
    if (saved.pid === process.pid)
      await rm(join(directory, "tunnel.json"), { force: true });
  } catch {
    /* Preserve another session's metadata. */
  }
}
process.once("SIGINT", () => void stop());
process.once("SIGTERM", () => void stop());
if (!process.stdin.isTTY) {
  process.stdin.resume();
  process.stdin.once("end", () => void stop());
}
child = spawn(
  executable,
  ["tunnel", "--no-autoupdate", "--protocol", "http2", "--url", origin],
  { windowsHide: true, stdio: ["ignore", "pipe", "pipe"] },
);
child.once("error", (error) => {
  console.error(
    `无法启动 cloudflared：${error.message}。请安装 cloudflared 或指定 --cloudflared。`,
  );
  void stop(1);
});
child.once("exit", (code) => {
  if (!stopping) {
    console.error(`Cloudflare 隧道已退出（${code}）。`);
    void stop(code || 1);
  }
});
startupTimer = setTimeout(() => {
  if (!ready) {
    console.error("Quick Tunnel 启动超时，请检查网络和 cloudflared 配置。");
    void stop(1);
  }
}, 60000);
let buffer = "";
async function output(chunk) {
  buffer = (buffer + chunk.toString()).slice(-16000);
  const found = buffer.match(/https:\/\/[a-z0-9-]+\.trycloudflare\.com/);
  if (!found || url || stopping) return;
  url = found[0];
  try {
    await api("tunnel/register", { origin: url });
    ready = true;
    clearTimeout(startupTimer);
    await writeFile(
      join(directory, "tunnel.json"),
      JSON.stringify({
        origin: url,
        local_origin: origin,
        pid: process.pid,
        tunnel_pid: child.pid,
      }),
      { mode: 0o600 },
    );
    console.log(
      `DUO Killer · Quick Tunnel\n${url}\n使用本机默认访问密码登录。添加到主屏幕后启用通知。\n保持本机服务与此隧道运行；按 Ctrl+C 关闭隧道。重启会更换网址。`,
    );
    if (!values["no-open"]) {
      if (process.platform === "win32")
        spawn("rundll32.exe", ["url.dll,FileProtocolHandler", url], {
          windowsHide: true,
          stdio: "ignore",
        }).on("error", () => {});
      else if (process.platform === "darwin")
        spawn("open", [url], { stdio: "ignore" }).on("error", () => {});
    }
  } catch (error) {
    console.error(error.message);
    await stop(1);
  }
}
child.stdout.on("data", (chunk) => void output(chunk));
child.stderr.on("data", (chunk) => void output(chunk));
