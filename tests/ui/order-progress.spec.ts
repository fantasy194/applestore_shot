import { expect, test, type Page } from "@playwright/test";
import { mkdir } from "node:fs/promises";
import { chooseOption } from "./select-option";
import { noHorizontalOverflow } from "./ui-review-fixture";

// Workspace-only synthetic bridge: no Apple account, order or payment requests.
async function fixture(page: Page, scenario = "running") {
  await page.route("**/api/**", (route) =>
    route.fulfill({ json: { authenticated: true, success: true } }),
  );
  await page.addInitScript(
    ({ scenario }) => {
      const root = window as any;
      const now = Date.now() / 1000;
      const accounts = [
        { id: "a", name: "日常账号", email: "first@example.test" },
        ...(scenario === "multiple"
          ? [{ id: "b", name: "备用账号", email: "second@example.test" }]
          : []),
      ];
      const make = (id: string, account_id = "a") => ({
        id,
        account_id,
        action: "purchase",
        status: "running",
        stage: "search",
        created_at: now - 10,
        updated_at: now,
        prompt: null,
        result: null,
        order_submitted: false,
        logs: [],
      });
      const task: any = make("order-1");
      if (scenario === "pending")
        Object.assign(task, {
          status: "succeeded",
          stage: "已完成",
          order_submitted: true,
          result: { status: "pending_payment", order_number: "W000000001" },
        });
      if (scenario === "unknown")
        Object.assign(task, {
          status: "succeeded",
          stage: "本批处理已结束",
          order_submitted: true,
          result: {
            status: "queue_complete",
            pending_orders: [{ order_number: "W000000001" }],
            queue_jobs: [{ status: "submission_unknown" }],
          },
        });
      if (scenario === "failure")
        Object.assign(task, { status: "failed", stage: "门店暂无可取时段" });
      if (scenario.startsWith("summary_"))
        Object.assign(task, {
          status: "succeeded",
          stage: "本批处理已结束",
          is_summary: true,
          queue_job_ids: ["target-1", "target-2"],
          order_submitted: true,
          result: {
            status: "queue_complete",
            purchase_attention: {
              pending_order_numbers: ["W000000001"],
              unknown_order_numbers:
                scenario === "summary_unknown" ? ["W000000002"] : [],
              pending_unidentified: false,
              unknown_unidentified: scenario === "summary_unidentified",
            },
          },
        });
      if (["payment_unknown", "submitted_unknown"].includes(scenario))
        Object.assign(task, {
          status: "succeeded",
          order_submitted: true,
          result: {
            status: "queue_complete",
            pending_orders: [{ order_number: "W000000001" }],
            jobs: [
              scenario === "payment_unknown"
                ? { status: "payment_unknown" }
                : { status: "failed", order_submitted: true },
            ],
          },
        });
      if (scenario === "scheduled")
        Object.assign(task, {
          status: "scheduled",
          stage: "等待定时",
          schedule: { at_ms: Date.now() + 60000 },
        });
      const tasks: any[] = [task];
      if (scenario === "multiple")
        tasks.push({
          ...make("order-2", "b"),
          stage: "正在提交订单",
          updated_at: now - 1,
        });
      let listener = (_task: any) => {};
      root.orderProgressCalls = [];
      root.orderProgressReads = [];
      root.orderProgressUpdate = (id: string, patch: any) => {
        const current = tasks.find((row) => row.id === id);
        Object.assign(current, patch, { updated_at: Date.now() / 1000 });
        listener(structuredClone(current));
      };
      root.orderProgressPush = (task: any) => {
        tasks.unshift(task);
        listener(structuredClone(task));
      };
      root.__DUO_KILLER_TEST__ = {
        snapshot: async () =>
          structuredClone({
            accounts,
            tasks,
            data_dir: "/synthetic-order-progress",
          }),
        task: async (_account: string, id: string) => {
          root.orderProgressReads.push(id);
          return structuredClone(tasks.find((row) => row.id === id));
        },
        subscribe: async (callback: any) => {
          listener = callback;
          return () => {};
        },
        cancel: async (account: string, id: string) => {
          root.orderProgressCalls.push({ operation: "cancel", account, id });
        },
        start: async (_account: string, action: string) => {
          root.orderProgressCalls.push({ operation: "start", action });
          return "unexpected-start";
        },
        maintenance: async () => ({ enabled: false, last_runs: [] }),
        queue: async () => ({ jobs: [], paused: true }),
        logs: async () => ({ entries: [], has_more: false }),
        browser: async () => ({ active: false }),
      };
    },
    { scenario },
  );
  await page.goto("/#/duo", { waitUntil: "domcontentloaded" });
  await expect(
    page.getByRole("region", { name: "下单进度", exact: true }),
  ).toBeVisible();
}

test("global order progress translates the actual stage and stays available on narrow screens", async ({
  page,
}) => {
  await fixture(page);
  const bar = page.getByRole("region", { name: "下单进度", exact: true });
  await expect(bar).toHaveAttribute("data-phase", "verify");
  await expect(bar).toHaveAttribute("data-tone", "info");
  await expect(bar.getByRole("status")).toContainText("查询自提门店");
  await expect(bar).not.toContainText("search");
  await expect(bar).not.toContainText("first@example.test");
  await mkdir("artifacts/ui-review/order-progress", { recursive: true });
  for (const width of [1180, 390, 320]) {
    await page.setViewportSize({ width, height: 844 });
    await noHorizontalOverflow(page);
    await expect(bar).toBeInViewport();
    const stepHeights = await bar
      .locator(".order-progress-steps li")
      .evaluateAll((items) =>
        items.map((item) => item.getBoundingClientRect().height),
      );
    expect(stepHeights.every((height) => height <= 20)).toBe(true);
    if (width === 1180)
      expect((await bar.boundingBox())!.height).toBeLessThanOrEqual(84);
    await bar.screenshot({
      path: `artifacts/ui-review/order-progress/${width}-running.png`,
    });
  }
  await bar.getByRole("button", { name: "查看当前任务", exact: true }).click();
  await expect(page).not.toHaveURL(/#\/duo/);
  await bar.getByRole("button", { name: "取消当前下单", exact: true }).click();
  await expect
    .poll(() => page.evaluate(() => (window as any).orderProgressCalls))
    .toEqual([{ operation: "cancel", account: "a", id: "order-1" }]);
});

test("successful task with an unpaid order stays warning and terminal dismissal does not cancel", async ({
  page,
}) => {
  await fixture(page, "pending");
  const bar = page.getByRole("region", { name: "下单进度", exact: true });
  await expect(bar).toHaveAttribute("data-tone", "warning");
  await expect(bar).toHaveAttribute("data-phase", "payment");
  await expect(bar.locator('li[data-state="done"]')).toHaveText(["提交"]);
  await expect(bar.locator('li[aria-current="step"]')).toHaveAttribute(
    "data-state",
    "pending",
  );
  await expect(bar.getByRole("status")).toContainText("订单待付款");
  await expect(bar).not.toContainText("付款完成");
  await expect(bar.getByRole("button", { name: "取消当前下单" })).toHaveCount(
    0,
  );
  await bar.getByRole("button", { name: "隐藏下单提示" }).click();
  await expect(bar).toHaveCount(0);
  expect(await page.evaluate(() => (window as any).orderProgressCalls)).toEqual(
    [],
  );
});

test("真实阶段轨迹仅将已完成阶段标绿，失败阶段及原因保留", async ({ page }) => {
  await fixture(page);
  const bar = page.getByRole("region", { name: "下单进度", exact: true });
  await page.evaluate(() =>
    (window as any).orderProgressUpdate("order-1", {
      purchase_progress: { current: "verify", completed: ["prepare"] },
    }),
  );
  await expect(bar.locator('li[data-state="done"]')).toHaveText(["准备"]);
  await expect(bar.locator('li[aria-current="step"]')).toHaveAttribute(
    "data-state",
    "current",
  );
  await page.evaluate(() =>
    (window as any).orderProgressUpdate("order-1", {
      stage: "review",
      purchase_progress: {
        current: "checkout",
        completed: ["prepare", "verify"],
      },
    }),
  );
  await expect(bar.locator('li[data-state="done"]')).toHaveText([
    "准备",
    "核对",
  ]);
  await page.evaluate(() =>
    (window as any).orderProgressUpdate("order-1", {
      status: "failed",
      stage: "结算失败",
      result: { message: "结账重新认证失败：HTTP 541，请在官网登录恢复后重试" },
    }),
  );
  await expect(bar).toHaveAttribute("data-phase", "checkout");
  await expect(bar.locator('li[data-state="error"]')).toHaveText(["结算"]);
  await expect(bar.getByRole("status")).toContainText(
    "结账重新认证失败：HTTP 541",
  );
  await expect(bar.locator(".order-progress-status small")).toHaveAttribute(
    "title",
    "结账重新认证失败：HTTP 541，请在官网登录恢复后重试",
  );
  await page.setViewportSize({ width: 1180, height: 844 });
  expect((await bar.boundingBox())!.height).toBeLessThanOrEqual(84);
  for (const width of [1180, 390, 320]) {
    await page.setViewportSize({ width, height: 844 });
    await noHorizontalOverflow(page);
    await bar.screenshot({
      path: `artifacts/ui-review/order-progress/${width}-failure.png`,
    });
  }
});

test("旧任务仅定位明确失败阶段，未知与部分轨迹不会虚构完成", async ({
  page,
}) => {
  await fixture(page, "failure");
  const bar = page.getByRole("region", { name: "下单进度", exact: true });
  await expect(bar.locator('li[data-state="done"]')).toHaveCount(0);
  await expect(bar.locator('li[data-state="error"]')).toHaveText(["核对"]);
  await expect(bar.getByRole("status")).toContainText("门店暂无可取时段");
  await page.evaluate(() =>
    (window as any).orderProgressUpdate("order-1", {
      stage: "预选门店目前均不可取货，请稍后重试",
      result: null,
    }),
  );
  await expect(bar.locator('li[data-state="error"]')).toHaveText(["核对"]);
  await page.evaluate(() =>
    (window as any).orderProgressUpdate("order-1", {
      stage: "结账重新认证失败，HTTP 541",
      result: null,
    }),
  );
  await expect(bar.locator('li[data-state="error"]')).toHaveText(["准备"]);
  await page.evaluate(() =>
    (window as any).orderProgressUpdate("order-1", {
      stage: "失败",
      result: {
        jobs: [
          { account_id: "other", error: "其他账号原因不能展示" },
          { error: "本次下单服务异常" },
        ],
      },
    }),
  );
  await expect(bar.getByRole("status")).toContainText("本次下单服务异常");
  await expect(bar).not.toContainText("其他账号原因不能展示");
  await expect(bar.locator('li[data-state="error"]')).toHaveCount(0);
  await page.evaluate(() =>
    (window as any).orderProgressUpdate("order-1", {
      status: "running",
      stage: "review",
      result: null,
      purchase_progress: { current: "checkout", completed: [] },
    }),
  );
  await expect(bar).toHaveAttribute("data-phase", "checkout");
  await expect(bar.locator('li[data-state="done"]')).toHaveCount(0);
  await page.evaluate(() =>
    (window as any).orderProgressUpdate("order-1", {
      stage: "search",
      purchase_progress: { current: "verify", completed: ["prepare"] },
    }),
  );
  await expect(bar.locator('li[data-state="done"]')).toHaveText(["准备"]);
  await expect(bar.locator("li").filter({ hasText: "结算" })).toHaveAttribute(
    "data-state",
    "pending",
  );
});

test("unknown submission takes priority over pending evidence and failure uses danger", async ({
  page,
}) => {
  await fixture(page, "unknown");
  const bar = page.getByRole("region", { name: "下单进度", exact: true });
  await expect(bar).toHaveAttribute("data-tone", "danger");
  await expect(bar.getByRole("status")).toContainText("提交结果待确认");
  await expect(bar).not.toContainText("付款完成");
  await page.evaluate(() =>
    (window as any).orderProgressUpdate("order-1", {
      result: null,
      order_submitted: false,
      status: "failed",
      stage: "门店暂无可取时段",
    }),
  );
  await expect(bar.getByRole("status")).toContainText("下单失败");
  await expect(bar).toHaveAttribute("data-tone", "danger");
});

test("multiple account orders can be selected without mixing identity or task actions", async ({
  page,
}) => {
  await fixture(page, "multiple");
  const bar = page.getByRole("region", { name: "下单进度", exact: true });
  await expect(bar).toContainText("2 个下单任务");
  expect((await bar.boundingBox())!.height).toBeLessThanOrEqual(84);
  await chooseOption(
    bar.getByRole("combobox", { name: "查看下单任务" }),
    "备用账号 · 购买商品 · 正在提交订单",
  );
  await expect(bar.locator(".order-progress-context")).toContainText(
    "备用账号",
  );
  await expect(bar.getByRole("status")).toContainText("正在提交订单");
  await bar.getByRole("button", { name: "取消当前下单" }).click();
  await expect
    .poll(() => page.evaluate(() => (window as any).orderProgressCalls))
    .toEqual([{ operation: "cancel", account: "b", id: "order-2" }]);
});

test("official resolution removes an old payment notice only for its own account and order", async ({
  page,
}) => {
  await fixture(page, "pending");
  const bar = page.getByRole("region", { name: "下单进度", exact: true });
  const resolution = {
    id: "verified-cancel",
    action: "cancel_order",
    status: "succeeded",
    stage: "已取消",
    created_at: Date.now() / 1000 + 1,
    updated_at: Date.now() / 1000 + 1,
    prompt: null,
    logs: [],
    order_submitted: false,
    result: {
      success: true,
      status: "order_cancelled",
      order_number: "W000000001",
    },
  };
  await page.evaluate(
    (resolution) =>
      (window as any).orderProgressPush({
        ...resolution,
        account_id: "other-account",
      }),
    resolution,
  );
  await expect(bar).toBeVisible();
  await page.evaluate(
    (resolution) =>
      (window as any).orderProgressPush({
        ...resolution,
        id: "own-verified-cancel",
        account_id: "a",
      }),
    resolution,
  );
  await expect(bar).toHaveCount(0);
  expect(await page.evaluate(() => (window as any).orderProgressCalls)).toEqual(
    [],
  );
});

test("scheduled order has no invented completion and unknown stage has no invented phase", async ({
  page,
}) => {
  await fixture(page, "scheduled");
  const bar = page.getByRole("region", { name: "下单进度", exact: true });
  await expect(bar).toHaveAttribute("data-tone", "neutral");
  await expect(bar.getByRole("status")).toContainText("等待定时下单");
  await expect(bar.getByRole("progressbar")).toHaveCount(0);
  await page.evaluate(() =>
    (window as any).orderProgressUpdate("order-1", {
      status: "running",
      stage: "undocumented_protocol_step",
      schedule: undefined,
    }),
  );
  await expect(bar).toHaveAttribute("data-phase", "unknown");
  await expect(bar.getByRole("status")).toContainText("正在处理下单");
  await expect(bar.locator("[aria-current=step]")).toHaveCount(0);
});

for (const scenario of [
  "summary_pending",
  "summary_unknown",
  "summary_unidentified",
  "payment_unknown",
  "submitted_unknown",
]) {
  test(`order attention remains accurate for ${scenario} without fetching task details`, async ({
    page,
  }) => {
    await fixture(page, scenario);
    const bar = page.getByRole("region", { name: "下单进度", exact: true });
    await expect(bar).toHaveAttribute(
      "data-tone",
      scenario === "summary_pending" ? "warning" : "danger",
    );
    await expect(bar.getByRole("status")).toContainText(
      scenario === "summary_pending" ? "订单待付款" : "提交结果待确认",
    );
    expect(
      await page.evaluate(() => (window as any).orderProgressReads),
    ).toEqual([]);
    if (scenario === "summary_pending") {
      await page.evaluate(() =>
        (window as any).orderProgressPush({
          id: "verified-summary-order",
          account_id: "a",
          action: "orders",
          status: "succeeded",
          created_at: Date.now() / 1000,
          updated_at: Date.now() / 1000,
          stage: "查询完成",
          logs: [],
          prompt: null,
          result: {
            orders: [
              {
                order_number: "W000000001",
                verified_by: "official_detail",
                items: [{ status: "PAID" }],
              },
            ],
          },
        }),
      );
      await expect(bar).toHaveCount(0);
    }
  });
}
