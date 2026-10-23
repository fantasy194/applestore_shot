import { test, expect, type Page } from "@playwright/test";
import { chooseOption } from "./select-option";
test.describe.configure({ timeout: 45000 });

async function fixture(
  page: Page,
  initiallyAuthenticated = true,
  warm?: Record<string, any>,
) {
  let authenticated = initiallyAuthenticated;
  let password = "fixture-old-password";
  const calls: { path: string; body: any }[] = [];
  await page.addInitScript(() => {
    const root = window as any;
    root.permissionRequests = 0;
    root.pushUnsubscribed = 0;
    Object.defineProperty(Notification, "requestPermission", {
      value: async () => {
        root.permissionRequests++;
        return "denied";
      },
    });
    const registration = {
      pushManager: {
        getSubscription: async () => ({
          endpoint: "https://web.push.apple.com/fixture-only",
          unsubscribe: async () => {
            root.pushUnsubscribed++;
            return true;
          },
        }),
      },
    };
    Object.defineProperty(navigator, "serviceWorker", {
      value: {
        register: async () => registration,
        ready: Promise.resolve(registration),
        getRegistration: async () => registration,
      },
    });
  });
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    const body = route.request().postDataJSON() || {};
    calls.push({ path, body });
    const fulfill = (data: object, status = 200) =>
      route.fulfill({
        status,
        contentType: "application/json",
        body: JSON.stringify(data),
      });
    if (path === "/api/auth/status") return fulfill({ authenticated });
    if (path === "/api/auth/login") {
      if (body.password !== password)
        return fulfill({ error: "访问密码不正确" }, 401);
      authenticated = true;
      return fulfill({ authenticated: true });
    }
    if (!authenticated) return fulfill({ error: "请重新登录" }, 401);
    if (path === "/api/auth/password") {
      password = body.password;
      authenticated = false;
      return fulfill({ success: true });
    }
    if (path === "/api/auth/logout") {
      authenticated = false;
      return fulfill({ authenticated: false });
    }
    if (
      [
        "/api/snapshot",
        "/api/workspace_snapshot",
        "/api/workspace_changes",
      ].includes(path)
    ) {
      const snapshot = {
        accounts: warm
          ? [
              {
                id: "a",
                name: "测试账号",
                email: "owner@example.test",
                pending: false,
              },
            ]
          : [],
        tasks: [],
        data_dir: "/fixture",
      };
      return fulfill(
        path === "/api/workspace_changes"
          ? {
              cursor: "fixture:1",
              reset: true,
              snapshot,
              tasks: [],
              removed_task_ids: [],
            }
          : snapshot,
      );
    }
    if (path === "/api/maintenance_command" && warm) {
      if (body.operation === "configure") warm = { ...warm, ...body.params };
      return fulfill(warm);
    }
    if (path === "/api/push/test" || path === "/api/push/unsubscribe")
      return fulfill({ success: true });
    return fulfill({ error: "Unexpected fixture operation" }, 400);
  });
  return calls;
}

test("应用设置独立导航，无账号即可打开且帮助默认折叠", async ({ page }) => {
  await fixture(page);
  await page.goto("/#/access", { waitUntil: "domcontentloaded" });
  await expect(
    page.getByRole("heading", { name: "应用设置", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("link", { name: "应用设置", exact: true }),
  ).toHaveAttribute("aria-current", "page");
  await expect(page.locator(".topbar")).toContainText("应用设置");
  await expect(page.locator(".pwa-controls")).toHaveCount(0);
  await expect(page.locator(".access-page [style]")).toHaveCount(0);
  await page.screenshot({
    path: "artifacts/access-desktop-ui.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth),
  ).toBeLessThanOrEqual(390);
  await page.screenshot({
    path: "artifacts/access-mobile-ui.png",
    fullPage: true,
  });
  await expect(
    page.getByText("iPhone：使用 Safari 打开", { exact: false }),
  ).not.toBeVisible();
  await page.getByText("查看安装方法", { exact: true }).click();
  await expect(
    page.getByText("iPhone：使用 Safari 打开", { exact: false }),
  ).toBeVisible();
  expect(await page.evaluate(() => (window as any).permissionRequests)).toBe(0);
  await page.getByRole("link", { name: "本地配置", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "本地配置", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("手机安装、通知与访问密码", { exact: true }),
  ).toHaveCount(0);
});

test("网页初始化复用订阅快照，不重复下载完整任务历史", async ({ page }) => {
  const calls = await fixture(page);
  await page.goto("/#/access", { waitUntil: "domcontentloaded" });
  await expect(
    page.getByRole("heading", { name: "应用设置", exact: true }),
  ).toBeVisible();
  expect(
    calls.filter((call) => call.path === "/api/workspace_changes"),
  ).toHaveLength(1);
  expect(calls.some((call) => call.path === "/api/snapshot")).toBe(false);
});

test("通知操作只由点击触发，测试和停用使用mock API", async ({ page }) => {
  const calls = await fixture(page);
  await page.goto("/#/access", { waitUntil: "domcontentloaded" });
  await page.getByRole("button", { name: "测试通知", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("测试通知已提交");
  expect(calls.some((call) => call.path === "/api/push/test")).toBe(true);
  await page.getByRole("button", { name: "停用通知", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("此设备已停用通知");
  expect(calls.some((call) => call.path === "/api/push/unsubscribe")).toBe(
    true,
  );
  await page.getByRole("button", { name: "启用通知", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("通知权限尚未允许");
  expect(await page.evaluate(() => (window as any).permissionRequests)).toBe(1);
});

test("修改访问密码返回登录页并接受新密码，退出同样清除会话", async ({
  page,
}) => {
  const calls = await fixture(page);
  await page.goto("/#/access", { waitUntil: "domcontentloaded" });
  await page
    .getByLabel("新访问密码", { exact: true })
    .fill("fixture-new-password");
  await page.getByRole("button", { name: "修改访问密码", exact: true }).click();
  await expect(page.getByLabel("访问密码", { exact: true })).toBeVisible();
  expect(
    calls.find((call) => call.path === "/api/auth/password")?.body,
  ).toEqual({ password: "fixture-new-password" });
  await page
    .getByLabel("访问密码", { exact: true })
    .fill("fixture-new-password");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "应用设置", exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "退出 Web 登录", exact: true })
    .click();
  await expect(page.getByLabel("访问密码", { exact: true })).toBeVisible();
  expect(calls.some((call) => call.path === "/api/auth/logout")).toBe(true);
});

test("未登录不能进入设置，登录失效保留验证入口", async ({ page }) => {
  await fixture(page, false);
  await page.goto("/#/access", { waitUntil: "domcontentloaded" });
  await expect(page.getByLabel("访问密码", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "应用设置", exact: true }),
  ).toHaveCount(0);
  await page
    .getByLabel("访问密码", { exact: true })
    .fill("fixture-old-password");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "应用设置", exact: true }),
  ).toBeVisible();
  await page.evaluate(() =>
    window.dispatchEvent(new Event("duo-auth-expired")),
  );
  await expect(page.getByRole("alert")).toContainText("登录已失效");
  await expect(page.getByLabel("访问密码", { exact: true })).toHaveValue("");
});

test("空闲预热显示倒计时与最近结果，说明保持折叠", async ({ page }) => {
  const calls = await fixture(page, true, {
    enabled: true,
    mode: "idle_warm_login",
    interval_minutes: 10,
    last_runs: [
      {
        account_id: "a",
        remaining_seconds: 120,
        last_warm_at_ms: Date.now() - 3600000,
        message: "上次预热已完成",
        status: "healthy",
        blocked_reason: null,
      },
    ],
  });
  await page.goto("/", { waitUntil: "domcontentloaded" });
  const panel = page.getByRole("region", { name: "空闲预热", exact: true });
  await expect(panel.getByRole("status")).toHaveText("距离预热 2:00");
  await expect(panel.getByRole("status")).toHaveText(/^距离预热 1:5\d$/);
  await expect(panel).toContainText("最近结果：上次预热已完成");
  await expect(
    panel.getByText("预热不清空购物袋", { exact: false }),
  ).not.toBeVisible();
  expect(calls.some((call) => call.path === "/api/start_task")).toBe(false);
});

test("账号忙碌时显示暂缓预热，兼容按账号存储的状态", async ({ page }) => {
  await fixture(page, true, {
    enabled: true,
    mode: "idle_warm_login",
    interval_minutes: 10,
    last_runs: {
      a: {
        account_id: "a",
        remaining_seconds: 600,
        busy: true,
        blocked_reason: "account_busy",
        message: "等待当前任务结束",
      },
    },
  });
  await page.goto("/", { waitUntil: "domcontentloaded" });
  const panel = page.getByRole("region", { name: "空闲预热", exact: true });
  await expect(panel.getByRole("status")).toHaveText(
    "下单/账号任务进行中，暂缓预热",
  );
  await expect(panel.getByLabel("下单空闲10分钟后自动预热")).toBeChecked();
});

test("空闲预热可选择自提准备，运行中仍显示原流程且不自动发起任务", async ({
  page,
}) => {
  const calls = await fixture(page, true, {
    enabled: true,
    flow: "login",
    interval_minutes: 10,
    last_runs: [
      {
        account_id: "a",
        status: "running",
        flow: "login",
        busy: true,
        blocked_reason: "account_busy",
        message: "登录预热中",
      },
    ],
  });
  await page.goto("/", { waitUntil: "domcontentloaded" });
  const panel = page.getByRole("region", { name: "空闲预热", exact: true });
  await expect(
    panel.getByRole("combobox", { name: "预热流程", exact: true }),
  ).toHaveText("预热登录（不加购）");
  await chooseOption(
    panel.getByRole("combobox", { name: "预热流程", exact: true }),
    "提前准备自提（不下单）",
  );
  await expect(
    panel.getByRole("combobox", { name: "预热流程", exact: true }),
  ).toHaveText("提前准备自提（不下单）");
  await expect(panel.getByRole("status")).toHaveText("正在预热登录，不加购");
  await expect(panel).toContainText("可能加购，不提交订单");
  expect(
    calls.find(
      (call) =>
        call.path === "/api/maintenance_command" &&
        call.body.operation === "configure",
    )?.body.params,
  ).toEqual({ flow: "pickup_prepare", interval_minutes: 10 });
  expect(calls.some((call) => call.path === "/api/start_task")).toBe(false);
});
