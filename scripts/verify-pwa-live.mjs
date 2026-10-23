// Read-only public acceptance: no Apple login, checkout or push subscription.
import { chromium } from "@playwright/test";
import { readFile, writeFile, mkdir } from "node:fs/promises";
import { join } from "node:path";
import { parseArgs } from "node:util";
import { pwaDirectory } from "./pwa-paths.mjs";

const { values } = parseArgs({ options: { "data-dir": { type: "string" }, "password-file": { type: "string", default: ".local-runtime/pwa-access.json" } } });
const directory = pwaDirectory(values["data-dir"]);
const { origin } = JSON.parse(await readFile(join(directory, "tunnel.json"), "utf8"));
if (!/^https:\/\/[a-z0-9-]+\.trycloudflare\.com$/.test(origin)) throw new Error("隧道记录无效。");
const { password } = JSON.parse(await readFile(values["password-file"], "utf8"));
const report = { origin, at: new Date().toISOString(), checks: [] };
const browser = await chromium.launch();
try {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 } });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(origin, { waitUntil: "domcontentloaded", timeout: 45000 });
  await page.getByLabel("访问密码", { exact: true }).waitFor();
  const blocked = await context.request.post(`${origin}/api/snapshot`, { data: {}, headers: { Origin: origin } });
  if (blocked.status() !== 401) throw new Error("未登录 API 隔离失败。");
  report.checks.push("unauthenticated_api_401");
  await page.getByLabel("访问密码", { exact: true }).fill(password);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await page.getByRole("link", { name: "自动监控", exact: true }).waitFor();
  const cookie = (await context.cookies()).find((item) => item.name === "duo_session");
  if (!cookie?.secure || !cookie.httpOnly || cookie.sameSite !== "Strict") throw new Error("HTTPS 登录 Cookie 属性无效。");
  report.checks.push("https_password_login_secure_cookie");
  // Existing workspaces can have hundreds of task records. Wait for the real
  // initial snapshot state before timing navigation over the public tunnel.
  await page.getByText("正在读取本地配置", { exact: true }).waitFor({ state: "hidden", timeout: 60000 });
  await page.getByRole("link", { name: "自动监控", exact: true }).click();
  await page.getByRole("heading", { name: "自动监控", exact: true }).waitFor();
  await page.getByText("添加门店与商品", { exact: true }).waitFor();
  await page.getByRole("heading", { name: "最近有货", exact: true }).waitFor();
  await page.getByText("读取监控配置…").waitFor({ state: "hidden", timeout: 30000 });
  const worker = await page.evaluate(async () => {
    const registration = await navigator.serviceWorker.ready;
    const paths = [];
    for (const name of await caches.keys()) paths.push(...(await (await caches.open(name)).keys()).map((request) => new URL(request.url).pathname));
    return { script: registration.active?.scriptURL, paths, width: document.documentElement.scrollWidth, viewport: innerWidth };
  });
  if (!worker.script?.endsWith("/sw.js") || !worker.paths.includes("/offline.html") || worker.paths.some((path) => path.startsWith("/api/"))) throw new Error("Service Worker 或离线缓存边界无效。");
  if (worker.width > worker.viewport) throw new Error("手机页面横向溢出。");
  report.checks.push("service_worker_active_no_api_cache", "mobile_width_390");
  const shutdown = await context.request.post(`${origin}/api/shutdown`, { data: {}, headers: { Origin: origin } });
  if (shutdown.status() !== 403) throw new Error("公网入口错误地允许停止本机。");
  report.checks.push("remote_shutdown_forbidden");
  await mkdir("artifacts", { recursive: true });
  await page.screenshot({ path: "artifacts/pwa-live-mobile.png", fullPage: true });
  await page.getByRole("link", { name: "应用设置", exact: true }).click();
  await page.getByRole("heading", { name: "应用设置", exact: true }).waitFor();
  await page.getByLabel("新访问密码", { exact: true }).waitFor();
  if (await page.locator(".pwa-controls").count()) throw new Error("旧页底设置未移除。");
  await page.screenshot({ path: "artifacts/pwa-settings-live-mobile.png", fullPage: true });
  report.checks.push("application_settings_integrated_navigation");
  await context.setOffline(true);
  await page.goto(origin, { waitUntil: "domcontentloaded" });
  const offline = await page.textContent("body");
  if (!offline?.includes("离线")) throw new Error("离线壳未显示。");
  report.checks.push("offline_fallback");
  if (errors.length) throw new Error(`页面错误：${errors.join("；")}`);
  report.checks.push("no_page_errors");
  await writeFile("artifacts/pwa-live-verification.json", JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report));
} finally { await browser.close(); }
