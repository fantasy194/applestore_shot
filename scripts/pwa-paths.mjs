import { existsSync } from "node:fs";
import { readFile } from "node:fs/promises";
import { homedir } from "node:os";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

export const project = fileURLToPath(new URL("../", import.meta.url));

export function pwaDirectory(explicit) {
  if (explicit) return resolve(explicit);
  const base = process.platform === "win32"
    ? process.env.APPDATA
    : join(homedir(), "Library/Application Support");
  const candidates = [
    join(project, ".local-data"),
    ...(base ? [join(base, "com.niulai.local-web-dev"), join(base, "com.niulai.local-web")] : []),
  ];
  return candidates.find((directory) => existsSync(join(directory, "accounts.json"))) || candidates[0];
}

export async function serverConnection(directory) {
  const run = JSON.parse(await readFile(join(directory, "pwa-run.json"), "utf8"));
  if (!/^http:\/\/127\.0\.0\.1:[1-9][0-9]{0,4}$/.test(run.origin) || Number(new URL(run.origin).port) > 65535) {
    throw new Error("本机 PWA 运行记录无效，未连接其他地址。");
  }
  const log = await readFile(join(directory, "server.log"), "utf8");
  const token = [...log.matchAll(/(http:\/\/127\.0\.0\.1:\d+)\/#access=([a-f0-9]{64})/g)]
    .reverse().find((match) => match[1] === run.origin)?.[2];
  if (!token) throw new Error("没有当前 PWA 的本机启动令牌。");
  return { ...run, token };
}

export async function localCall(connection, path, body = {}) {
  const response = await fetch(`${connection.origin}/api/${path}`, {
    method: "POST", headers: { "Content-Type": "application/json", Origin: connection.origin,
      Authorization: `Bearer ${connection.token}` }, body: JSON.stringify(body),
    redirect: "error", signal: AbortSignal.timeout(8000),
  });
  if (!response.ok) throw new Error(`本机 PWA 请求失败（${response.status}）。`);
  return response.json();
}
