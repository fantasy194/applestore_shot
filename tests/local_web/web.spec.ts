import { test, expect } from "@playwright/test";
import { spawn, type ChildProcess } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { resolve, join } from "node:path";

let process: ChildProcess;
let directory: string;
let launchUrl: string;
test.beforeAll(async () => {
  directory = await mkdtemp(join(tmpdir(), "duo-killer-web-ui-"));
  const executable = resolve(
    "local-web-release",
    global.process.platform === "win32" ? "duo-killer-web.exe" : "duo-killer-web",
  );
  process = spawn(executable, [
    "--no-open",
    "--port",
    "0",
    "--data-dir",
    directory,
  ], { env: { ...global.process.env, DUO_WEB_PASSWORD: "synthetic-pwa-browser-123" } });
  launchUrl = await new Promise<string>((resolve, reject) => {
    let buffer = "";
    process.once("error", reject);
    process.once("exit", () => reject(new Error("本地 Web 测试服务提前退出")));
    process.stdout!.on("data", (chunk) => {
      buffer += chunk.toString();
      const url = buffer.match(/http:\/\/127\.0\.0\.1:\d+\/#access=[a-f0-9]+/);
      if (url) resolve(url[0]);
    });
  });
});
test.afterAll(async () => {
  if (process && process.exitCode === null) {
    const stopped = new Promise<void>((resolve) =>
      process.once("exit", () => resolve()),
    );
    process.kill("SIGTERM");
    await stopped;
  }
  if (directory) await rm(directory, { recursive: true, force: true });
});

test("packaged UI keeps cancelled enrollment pending across refresh", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(launchUrl);
  await expect(page.getByText("本地 Web", { exact: true })).toBeVisible();
  await expect(page).toHaveURL(/\/$/); // Launch secret is removed from address bar.
  await page
    .getByLabel("Apple 账号", { exact: true })
    .fill("web-ui@example.test");
  await page.getByRole("button", { name: "继续登录", exact: true }).click();
  await expect(page.getByLabel("密码", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "立即下单" })).toHaveCount(0);
  await page.getByRole("button", { name: "取消", exact: true }).click();
  await expect(page.getByRole("heading", { name: "未完成添加" })).toBeVisible();
  await page.reload();
  await expect(
    page.getByRole("button", { name: "继续登录 web-**@example.test" }),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "立即下单" })).toHaveCount(0);
  await page.screenshot({ path: "artifacts/local-web.png", fullPage: true });
  expect(errors).toEqual([]);
});

test("a browser opened without a launch secret cannot read account data", async ({
  page,
}) => {
  await page.goto(launchUrl.split("#")[0]);
  await expect(page.getByLabel("访问密码", { exact: true })).toBeVisible();
  await expect(
    page.getByText("web-**@example.test", { exact: true }),
  ).toHaveCount(0);
  const blocked = await page.request.post(new URL("/api/snapshot", launchUrl).href, { data: {} });
  expect(blocked.status()).toBe(401);
});

test("password login installs the worker and keeps APIs out of offline cache", async ({ page }) => {
  await page.goto(launchUrl.split("#")[0]);
  await page.getByLabel("访问密码", { exact: true }).fill("wrong-password");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("密码不正确");
  await page.getByLabel("访问密码", { exact: true }).fill("synthetic-pwa-browser-123");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page.getByRole("link", { name: "自动监控", exact: true })).toBeVisible();
  const worker = await page.evaluate(async () => {
    const registration = await navigator.serviceWorker.ready;
    const names = await caches.keys();
    const paths: string[] = [];
    for (const name of names) paths.push(...(await (await caches.open(name)).keys()).map((request) => new URL(request.url).pathname));
    return { scope: registration.scope, script: registration.active?.scriptURL, paths };
  });
  expect(worker.script).toMatch(/\/sw\.js$/);
  expect(worker.paths).toContain("/offline.html");
  expect(worker.paths.some((path) => path.startsWith("/api/"))).toBe(false);
  await page.getByText("手机安装、通知与访问密码", { exact: true }).click();
  await page.getByRole("button", { name: "退出 Web 登录", exact: true }).click();
  await expect(page.getByLabel("访问密码", { exact: true })).toBeVisible();
});
