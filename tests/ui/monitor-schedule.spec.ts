import { test, expect, type Page } from "@playwright/test";
import { chooseOption } from "./select-option";

async function fixture(page: Page, paused = false) {
  await page.addInitScript(
    ({ paused }) => {
      const root = window as any;
      let config = JSON.parse(
        localStorage.getItem("schedule-fixture") || "null",
      ) || {
        interval_seconds: 7,
        targets: [
          {
            id: "target",
            sku: "MLH74FE",
            quantity: 1,
            store_id: "R001",
            store_name: "Apple 测试门店",
            location: "201101",
            account_id: "",
            auto_purchase: false,
            retry_count: 0,
          },
        ],
        proxy: { enabled: false, pool_size: 12, providers: [] },
      };
      const tasks: any[] = [];
      let update = (_: any) => {};
      root.scheduleCalls = [];
      root.scheduleCancels = [];
      root.__DUO_KILLER_TEST__ = {
        snapshot: async () =>
          structuredClone({ accounts: [], tasks, data_dir: "/synthetic" }),
        subscribe: async (callback: any) => {
          update = callback;
          return () => {};
        },
        logs: async () => ({ entries: [] }),
        cancel: async (_: string, id: string) => {
          root.scheduleCancels.push(id);
          const task = tasks.find((task) => task.id === id);
          task.status = "cancelled";
          update(structuredClone(task));
        },
        start: async (account_id: string, action: string, params: any) => {
          root.scheduleCalls.push({ action, params });
          if (action === "inventory_settings_save") {
            config = structuredClone(params.config);
            localStorage.setItem("schedule-fixture", JSON.stringify(config));
          }
          const task = {
            id: `task-${tasks.length}`,
            account_id,
            action,
            status: action === "inventory_monitor" ? "running" : "succeeded",
            stage: "测试",
            created_at: Date.now() / 1000,
            updated_at: Date.now() / 1000,
            logs: [],
            prompt: null,
            result: action.startsWith("inventory_settings")
              ? { config: structuredClone(config) }
              : {
                  monitoring: true,
                  checked_at: null,
                  targets: [],
                  schedule: config.schedule,
                  schedule_state: {
                    paused,
                    reason:
                      config.schedule?.mode === "include"
                        ? "outside_window"
                        : "excluded_window",
                    timezone: "Asia/Shanghai",
                    next_transition_at_ms: Date.parse(
                      "2026-10-26T05:00:00+08:00",
                    ),
                  },
                },
          };
          tasks.unshift(task);
          update(structuredClone(task));
          return task.id;
        },
      };
    },
    { paused },
  );
  await page.goto("/#/monitor");
  await expect(
    page.getByRole("checkbox", { name: "启用每日检测时段" }),
  ).toBeVisible();
}

test("旧配置默认全天，暂停规则保存、重载且不修改其他配置", async ({ page }) => {
  await fixture(page);
  await expect(page.getByLabel("启用每日检测时段")).not.toBeChecked();
  await expect(page.getByText("全天检测，不限制每日时段。")).toBeVisible();
  await page.getByLabel("启用每日检测时段").check();
  await expect(page.getByLabel("开始时间（北京时间）")).toHaveValue("01:00");
  await expect(page.getByLabel("结束时间（北京时间）")).toHaveValue("05:00");
  await page.getByRole("button", { name: "保存配置", exact: true }).click();
  const saved = await page.evaluate(() =>
    JSON.parse(localStorage.getItem("schedule-fixture")!),
  );
  expect(saved.schedule).toEqual({
    enabled: true,
    mode: "exclude",
    start: "01:00",
    end: "05:00",
    timezone: "Asia/Shanghai",
  });
  expect(saved.interval_seconds).toBe(7);
  expect(saved.proxy.pool_size).toBe(12);
  expect(saved.targets[0].store_id).toBe("R001");
  await page.reload();
  await expect(page.getByLabel("启用每日检测时段")).toBeChecked();
  await expect(page.getByLabel("检测时段规则")).toHaveText("时段内暂停");
});

test("仅时段内检测支持跨午夜，空值或相同时间阻止保存和启动", async ({
  page,
}) => {
  await fixture(page);
  await page.getByLabel("启用每日检测时段").check();
  await chooseOption(page.getByLabel("检测时段规则"), "仅时段内检测");
  await page.getByLabel("开始时间（北京时间）").fill("23:00");
  await page.getByLabel("结束时间（北京时间）").fill("02:00");
  await page.getByRole("button", { name: "保存配置", exact: true }).click();
  expect(
    await page.evaluate(
      () => JSON.parse(localStorage.getItem("schedule-fixture")!).schedule,
    ),
  ).toMatchObject({ mode: "include", start: "23:00", end: "02:00" });
  await page.getByLabel("结束时间（北京时间）").fill("23:00");
  await expect(page.getByRole("alert")).toContainText(
    "开始时间和结束时间不能相同",
  );
  await expect(
    page.getByRole("button", { name: "保存配置", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "保存并开始监控" }),
  ).toBeDisabled();
  await page.getByLabel("结束时间（北京时间）").fill("");
  await expect(page.getByRole("alert")).toContainText("请完整填写");
  await expect(
    page.getByRole("button", { name: "保存并开始监控" }),
  ).toBeDisabled();
  await page.getByLabel("启用每日检测时段").uncheck();
  await expect(
    page.getByRole("button", { name: "保存并开始监控" }),
  ).toBeEnabled();
  await page.getByRole("button", { name: "保存配置", exact: true }).click();
  await expect(page.getByText("监控配置已保存", { exact: true })).toBeVisible();
  expect(
    await page.evaluate(
      () => JSON.parse(localStorage.getItem("schedule-fixture")!).schedule,
    ),
  ).toEqual({
    enabled: false,
    mode: "include",
    start: "23:00",
    end: "05:00",
    timezone: "Asia/Shanghai",
  });
});

for (const mode of ["exclude", "include"]) {
  test(`时段规则 ${mode} 暂停仍运行，使用北京时间显示恢复时间并允许停止`, async ({
    page,
  }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    await fixture(page, true);
    await page.getByLabel("启用每日检测时段").check();
    if (mode === "include")
      await chooseOption(page.getByLabel("检测时段规则"), "仅时段内检测");
    await page.getByRole("button", { name: "保存并开始监控" }).click();
    await expect(page.locator(".inventory-run-state")).toHaveText("按时段暂停");
    await expect(
      page.getByText(/下次恢复检测：2026\/10\/26 05:00（北京时间）/),
    ).toBeVisible();
    await expect(page.getByLabel("开始时间（北京时间）")).toBeDisabled();
    await expect(
      page.getByText("修改时段请先停止监控", { exact: false }),
    ).toBeVisible();
    await expect(
      page
        .locator(".ui-metric")
        .filter({ hasText: "当前有货" })
        .locator("strong"),
    ).toHaveText("0");
    await page.getByRole("button", { name: "停止监控", exact: true }).click();
    await expect(page.locator(".inventory-run-state")).toHaveText("监控已停止");
    expect(
      await page.evaluate(() => (window as any).scheduleCancels.length),
    ).toBe(1);
  });
}

for (const width of [320, 390]) {
  test(`每日时段设置 ${width}px 无横向溢出`, async ({ page }) => {
    await page.setViewportSize({ width, height: 844 });
    await fixture(page);
    await page.getByLabel("启用每日检测时段").check();
    await expect(page.getByLabel("结束时间（北京时间）")).toBeVisible();
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth),
    ).toBeLessThanOrEqual(width);
    await page.screenshot({
      path: `artifacts/monitor-schedule-${width}.png`,
      fullPage: true,
    });
  });
}
