import { test, expect } from "@playwright/test";
import { spawn, type ChildProcess } from "node:child_process";
import { once } from "node:events";
import { mkdtemp, readFile, writeFile, rm } from "node:fs/promises";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { resolve, join } from "node:path";
import { randomUUID } from "node:crypto";

test.describe.configure({ timeout: 120000 });
test.use({ actionTimeout: 10000 });
let launcher: ChildProcess;
let directory: string;
let port: number;
let launchUrl: string;
const probeName = `.dev-web-probe-${randomUUID()}.ts`;
const probe = resolve("src", probeName);

async function start() {
  launcher = spawn(process.execPath, [
    process.env.npm_execpath!,
    "dev:web",
    "--no-open",
    "--port",
    String(port),
    "--data-dir",
    directory,
  ]);
  launchUrl = await new Promise<string>((resolve, reject) => {
    let buffer = "";
    launcher.once("error", reject);
    launcher.once("exit", () => reject(new Error("开发服务提前退出")));
    launcher.stdout!.on("data", (chunk) => {
      buffer += chunk.toString();
      const url = buffer.match(/http:\/\/127\.0\.0\.1:\d+\/#access=[a-f0-9]+/);
      if (url) resolve(url[0]);
    });
  });
}
async function duplicateStart() {
  const duplicate = spawn(process.execPath, [
    process.env.npm_execpath!,
    "dev:web",
    "--no-open",
    "--port",
    String(port),
    "--data-dir",
    directory,
  ]);
  let output = "";
  duplicate.stdout!.on("data", (chunk) => {
    output += chunk.toString();
  });
  const timeout = setTimeout(() => duplicate.kill(), 10000);
  try {
    const [code] = await once(duplicate, "exit");
    expect(code).toBe(0);
    expect(output).toContain("此工作空间已在运行");
    expect(output).toContain(launchUrl);
  } finally {
    clearTimeout(timeout);
    if (duplicate.exitCode === null && duplicate.signalCode === null)
      duplicate.kill();
  }
}
async function stop() {
  if (launcher?.exitCode === null && launcher.signalCode === null) {
    const exited = once(launcher, "exit");
    launcher.stdin!.end();
    await exited;
  }
}
test.beforeAll(async () => {
  directory = await mkdtemp(join(tmpdir(), "duo-killer-dev-web-"));
  const socket = createServer();
  await new Promise<void>((resolve) => socket.listen(0, "127.0.0.1", resolve));
  port = (socket.address() as { port: number }).port;
  await new Promise<void>((resolve) => socket.close(() => resolve()));
  await start();
});
test.afterAll(async () => {
  await stop();
  await rm(probe, { force: true });
  if (directory) await rm(directory, { recursive: true, force: true });
});

test("dev command serves real source tasks, hot updates, and releases its workspace on exit", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(launchUrl);
  await expect(page.getByLabel("Apple 账号", { exact: true })).toBeVisible();
  // A repeated pnpm command reuses this service and exits without killing it.
  await duplicateStart();
  await page.reload();
  await expect(page.getByLabel("Apple 账号", { exact: true })).toBeVisible();
  await expect(page.getByLabel("备注名称")).toHaveCount(0);
  await expect(
    page.locator(".sidebar").getByRole("navigation", { name: "页面导航" }),
  ).toBeVisible();
  await page.screenshot({
    path: "artifacts/dev-login-sidebar.png",
    fullPage: true,
  });
  await page.getByRole("link", { name: "本地配置", exact: true }).click();
  await expect(page).toHaveURL(/#\/settings$/);
  await expect(
    page.getByRole("heading", { name: "快捷送货地址" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "保存地址预设" }),
  ).toBeEnabled();
  await page.getByRole("combobox", { name: "预制商品" }).click();
  await page.getByRole("option", { name: /MK2P4CH/ }).click();
  await page.getByLabel("数量", { exact: true }).fill("3");
  await page.getByRole("button", { name: "保存购买预设", exact: true }).click();
  await expect(page.getByText("购买预设已保存", { exact: true })).toBeVisible();
  for (const [label, value] of Object.entries({
    姓氏: "张",
    名字: "测试",
    通知邮箱: "shipping@example.test",
    手机号: "13800000000",
    "省 / 直辖市": "四川",
    城市: "成都",
    "区 / 县": "锦江区",
    详细地址: "测试路 8 号",
  })) {
    await page.getByLabel(label, { exact: true }).fill(value);
  }
  await page.getByRole("button", { name: "保存地址预设" }).click();
  await expect(
    page.getByRole("status").filter({ hasText: "快捷地址已保存" }),
  ).toBeVisible();
  await page.screenshot({
    path: "artifacts/dev-local-settings.png",
    fullPage: true,
  });
  await page.reload();
  await expect(page.getByLabel("详细地址")).toHaveValue("测试路 8 号");
  await expect(page.getByRole("combobox", { name: "预制商品" })).toContainText("MK2P4CH");
  await page.getByRole("link", { name: "DUO 状态", exact: true }).click();
  await expect(page).toHaveURL(/#\/duo$/);
  const sidebar = await page.locator(".sidebar").boundingBox();
  const content = await page.locator(".duo-page").boundingBox();
  expect(sidebar!.x + sidebar!.width).toBeLessThanOrEqual(content!.x);
  await expect(page.locator("tbody tr")).toHaveCount(8);
  await expect(page.getByRole("button", { name: "刷新状态" })).toBeEnabled();
  await expect(
    page.getByLabel("Apple 账号", { exact: true }),
  ).not.toBeVisible();
  await page.screenshot({
    path: "artifacts/dev-duo-status.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.setViewportSize({ width: 1180, height: 800 });
  await page.getByRole("link", { name: "账号工作台", exact: true }).click();
  await page.getByLabel("Apple 账号", { exact: true }).fill("dev@example.test");
  await page.getByRole("button", { name: "继续登录", exact: true }).click();
  await expect(page.getByLabel("密码", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "取消", exact: true }).click();
  await expect(page.getByRole("heading", { name: "未完成添加" })).toBeVisible();
  // The pending account inherited the saved defaults, without logging into Apple.
  await page.getByRole("link", { name: "本地配置", exact: true }).click();
  await page.getByRole("combobox", { name: "配置范围" }).click();
  await expect(page.getByRole("listbox")).toBeVisible();
  await expect(
    page.getByRole("option", {
      name: "d**@example.test · d**@example.test（待完成登录）",
      exact: true,
    }),
  ).toBeVisible();
  await page
    .getByRole("option", {
      name: "d**@example.test · d**@example.test（待完成登录）",
      exact: true,
    })
    .click();
  await expect(page.getByLabel("详细地址")).toHaveValue("测试路 8 号");
  await expect(page.getByRole("combobox", { name: "预制商品" })).toContainText("MK2P4CH");
  await page.getByRole("link", { name: "账号工作台", exact: true }).click();
  const source = (value: string) =>
    `window.__devProbe = ${JSON.stringify(value)}; if (import.meta.hot) import.meta.hot.accept();\n`;
  await writeFile(probe, source("before"));
  await page.evaluate(async (url) => {
    (window as any).__devPageMarker = "keep";
    await import(/* @vite-ignore */ url);
  }, `/src/${probeName}`);
  await expect
    .poll(() => page.evaluate(() => (window as any).__devProbe))
    .toBe("before");
  await writeFile(probe, source("after"));
  await expect
    .poll(() => page.evaluate(() => (window as any).__devProbe))
    .toBe("after");
  expect(await page.evaluate(() => (window as any).__devPageMarker)).toBe(
    "keep",
  );
  const origin = new URL(launchUrl).origin;
  const blocked = await page.request.post(`${origin}/api/snapshot`, {
    headers: { Origin: "https://foreign.test" },
    data: {},
  });
  expect(blocked.status()).toBe(403);
  // Restart the entire command on the same port/data directory: no orphaned child or file lock.
  await page.goto("about:blank");
  const connectionFile = join(directory, ".dev-web.json");
  const staleConnection = await readFile(connectionFile, "utf8");
  await stop();
  await expect(readFile(connectionFile, "utf8")).rejects.toMatchObject({
    code: "ENOENT",
  });
  // Simulate a connection record left by an abrupt exit. The kernel lock is free.
  await writeFile(connectionFile, staleConnection, { mode: 0o600 });
  await start();
  expect(await readFile(connectionFile, "utf8")).not.toBe(staleConnection);
  await page.goto(launchUrl);
  await expect(
    page.getByRole("button", { name: "继续登录 d**@example.test" }),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "立即下单" })).toHaveCount(0);
  expect(errors).toEqual([]);
  // A hot-updated page can reach a Rust process started before the new command existed.
  // Keep the account and explain how to restart, then verify the real endpoint on retry.
  await page.route("**/api/remove_account", (route) =>
    route.fulfill({ status: 404, body: "" }),
  );
  await page.getByRole("button", { name: /^移除 / }).click();
  const dialog = page.getByRole("dialog", { name: "移除本地账号" });
  await dialog.getByRole("button", { name: "确认移除" }).click();
  await expect(dialog.getByRole("alert")).toContainText("再运行 pnpm dev:web");
  await expect(page.getByRole("heading", { name: "未完成添加" })).toBeVisible();
  await page.unroute("**/api/remove_account");
  await dialog.getByRole("button", { name: "确认移除" }).click();
  await expect(dialog).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "未完成添加" })).toHaveCount(
    0,
  );
  await page.reload();
  await expect(page.getByLabel("Apple 账号", { exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "未完成添加" })).toHaveCount(
    0,
  );
});
