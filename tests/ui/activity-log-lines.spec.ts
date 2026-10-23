import { expect, test, type Page } from "@playwright/test";
import { readFile } from "node:fs/promises";

const errorMessage = `门店查询失败，保留当前任务并等待人工核对。${"请检查最新响应。".repeat(16)}\n第二行完整诊断：fixture@example.test`;
const newestTimestamp = Date.UTC(2026, 9, 23, 6, 7, 8, 125);

/** All records and accounts are synthetic; no service or user data is read. */
async function fixture(page: Page) {
  await page.route("**/api/**", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ authenticated: true, success: true }),
    }),
  );
  await page.addInitScript(
    ({ errorMessage, newestTimestamp }) => {
      const root = window as any;
      const base = {
        account_id: "synthetic-account-id",
        task_id: "synthetic-task-id",
        category: "inventory",
        action: "inventory_monitor",
        event: "synthetic_event",
      };
      root.logLineRecords = [
        {
          ...base,
          id: 5,
          ts_ms: newestTimestamp,
          level: "error",
          message: errorMessage,
          details: {
            sku: "MJT84CH",
            store_name: "Apple 测试门店",
            reason: "离线合成诊断",
          },
        },
        {
          ...base,
          id: 4,
          ts_ms: newestTimestamp - 1000,
          level: "warning",
          message: "库存尚未确认",
        },
        {
          ...base,
          id: 3,
          ts_ms: newestTimestamp - 2000,
          level: "info",
          message: "监控已开始",
        },
        {
          ...base,
          id: 2,
          ts_ms: newestTimestamp - 86400000,
          level: "debug",
          message: "已读取公开库存响应",
        },
        {
          ...base,
          id: 1,
          ts_ms: "invalid",
          level: "info",
          message: "无有效时间的旧记录",
        },
      ];
      root.logLineCalls = [];
      root.__DUO_KILLER_TEST__ = {
        snapshot: async () => ({
          accounts: [
            {
              id: base.account_id,
              name: "fixture@example.test",
              email: "fixture@example.test",
            },
          ],
          tasks: [],
          data_dir: "/synthetic-log-review",
        }),
        subscribe: async () => () => {},
        logs: async (params: any = {}) => {
          root.logLineCalls.push(params);
          return {
            entries: structuredClone(
              root.logLineRecords.filter(
                (entry: any) =>
                  params.after_id === undefined || entry.id > params.after_id,
              ),
            ),
            has_more: false,
          };
        },
        maintenance: async () => ({
          enabled: false,
          interval_minutes: 10,
          last_runs: [],
        }),
        browser: async () => ({ active: false }),
      };
    },
    { errorMessage, newestTimestamp },
  );
}

for (const width of [1180, 390, 320]) {
  test(`${width}px 日志紧凑显示，完整信息可用键盘或触摸展开`, async ({
    browser,
  }) => {
    const context = await browser.newContext({
      viewport: { width, height: 844 },
      hasTouch: width < 600,
      reducedMotion: "reduce",
    });
    const page = await context.newPage();
    try {
      await fixture(page);
      await page.goto("/#/logs", { waitUntil: "domcontentloaded" });
      await expect(page.locator(".log-entry")).toHaveCount(5);
      await page.getByLabel("自动刷新日志").uncheck();
      const error = page.locator('.log-entry[data-level="error"]');
      const summary = error.locator(".log-line");
      await expect(summary.locator(".log-level")).toHaveText("ERROR");
      await expect(summary.locator(".log-message")).toContainText("MJT84CH");
      await expect(summary.locator(".log-message")).toContainText(
        "Apple 测试门店",
      );
      await expect(summary).not.toContainText("synthetic-task-id");
      await expect(error.locator(".log-body")).not.toBeVisible();
      const time = summary.locator("time");
      await expect(time).toHaveAttribute(
        "datetime",
        new Date(newestTimestamp).toISOString(),
      );
      await expect(time).toHaveText(/\d{2}:\d{2}:\d{2}\.125/);
      expect(await time.getAttribute("title")).toContain("2026");
      const expectedDates = await page.evaluate(
        (timestamp) =>
          [timestamp, timestamp - 86400000].map((value) => {
            const d = new Date(value);
            return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
          }),
        newestTimestamp,
      );
      for (const date of expectedDates)
        await expect(
          page.locator(".log-date").filter({ hasText: date }),
        ).toBeVisible();
      await expect(
        page.locator(".log-entry").filter({ hasText: "无有效时间的旧记录" }),
      ).not.toContainText("Invalid Date");
      const heights = await page
        .locator(".log-line")
        .evaluateAll((rows) =>
          rows.map((row) => row.getBoundingClientRect().height),
        );
      for (const height of heights) {
        expect(height).toBeLessThanOrEqual(width < 600 ? 60 : 36);
        if (width < 600) expect(height).toBeGreaterThanOrEqual(44);
      }
      const colors = await page
        .locator(".log-level")
        .evaluateAll((labels) =>
          Object.fromEntries(
            labels.map((label) => [
              label.textContent,
              getComputedStyle(label).color,
            ]),
          ),
        );
      expect(Object.keys(colors).sort()).toEqual([
        "DEBUG",
        "ERROR",
        "INFO",
        "WARN",
      ]);
      expect(new Set(Object.values(colors)).size).toBe(4);
      expect(
        await summary
          .locator(".log-message")
          .evaluate(
            (el) =>
              el.scrollWidth > el.clientWidth &&
              getComputedStyle(el).textOverflow === "ellipsis",
          ),
      ).toBe(true);
      expect(
        await page.evaluate(() => document.documentElement.scrollWidth),
      ).toBeLessThanOrEqual(width);
      await page.screenshot({
        path: `artifacts/log-lines/${width}-logs.png`,
        fullPage: true,
      });
      if (width < 600) await summary.tap();
      else {
        await summary.focus();
        await page.keyboard.press("Enter");
      }
      await expect(error.locator(".log-body")).toBeVisible();
      await expect(error.locator(".log-full-message")).toContainText(
        errorMessage.replace("fixture@example.test", "fixtu**@example.test"),
      );
      await expect(error.locator(".log-meta")).toContainText(
        "synthetic-task-id",
      );
      await expect(error.locator("pre")).toContainText("离线合成诊断");
      await expect(error.locator(".log-body")).not.toContainText(
        "fixture@example.test",
      );
      expect(
        await page.evaluate(() => document.documentElement.scrollWidth),
      ).toBeLessThanOrEqual(width);
      await page.screenshot({
        path: `artifacts/log-lines/${width}-logs-expanded.png`,
        fullPage: true,
      });
      await summary.focus();
      await page.keyboard.press("Space");
      await expect(error.locator(".log-body")).not.toBeVisible();
    } finally {
      await context.close();
    }
  });
}

test("增量追加不关闭已展开记录，下载保留原始完整日志", async ({ page }) => {
  await fixture(page);
  await page.goto("/#/logs", { waitUntil: "domcontentloaded" });
  await expect(page.locator(".log-entry")).toHaveCount(5);
  const error = page.locator('.log-entry[data-level="error"]');
  await error.locator("summary").click();
  await page.evaluate(() => {
    const root = window as any;
    root.logLineRecords.unshift({
      ...root.logLineRecords[2],
      id: 6,
      message: "同毫秒新增日志",
    });
  });
  await expect(page.locator(".log-entry")).toHaveCount(6);
  await expect(error.locator(".log-body")).toBeVisible();
  expect(
    await page.evaluate(() =>
      (window as any).logLineCalls.some((params: any) => params.after_id === 5),
    ),
  ).toBe(true);
  await page.getByLabel("自动刷新日志").uncheck();
  const downloaded = page.waitForEvent("download");
  await page.getByRole("button", { name: "下载当前日志" }).click();
  const download = await downloaded;
  expect(download.suggestedFilename()).toBe("duo-activity-logs.txt");
  const contents = await readFile((await download.path())!, "utf8");
  expect(contents).toContain(errorMessage);
  expect(contents).toContain("[error] [inventory]");
  expect(contents).toContain("同毫秒新增日志");
});
