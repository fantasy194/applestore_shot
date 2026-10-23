import { expect, test, type Page } from "@playwright/test";
import { mkdir } from "node:fs/promises";
import { noHorizontalOverflow, uiReviewFixture } from "./ui-review-fixture";

async function queueFixture(page: Page) {
  await uiReviewFixture(page);
  await page.addInitScript(() => {
    const root = window as any;
    const jobs = [
      {
        id: "failed",
        status: "failed",
        message: "门店时段已失效，请重新选择时段",
      },
      { id: "running", status: "running", message: "正在核对门店和联系人" },
      {
        id: "payment",
        status: "succeeded",
        result: { status: "pending_payment" },
      },
      {
        id: "unknown",
        status: "submission_unknown",
        message: "官网未返回提交结果",
        result: { status: "pending_payment" },
      },
      { id: "success", status: "succeeded", message: "任务已完成" },
      { id: "cancelled", status: "cancelled", message: "已取消" },
      {
        id: "interrupted",
        status: "interrupted",
        message: "服务中断，请核对订单",
      },
    ].map((job, index) => ({
      account_id: "review-a",
      sku: "MJT84CH",
      store_id: "R001",
      store_name: "Apple 上海环贸 iapm",
      quantity: 1,
      concurrency: 1,
      sequence: index + 1,
      enqueued_at_ms: Date.now() - index * 1000,
      retry_count: 0,
      task_id: `task-${job.id}`,
      ...job,
    }));
    root.queueDensityJobs = jobs;
    root.queueDensityCalls = [];
    root.__DUO_KILLER_TEST__.queue = async (operation: string, params: any) => {
      root.queueDensityCalls.push({ operation, params });
      return structuredClone({
        mode: "sequential",
        max_concurrency: 1,
        jobs,
        queued_count: 0,
        running_count: 1,
        blocked_accounts: [],
      });
    };
  });
}

test("队列直接显示失败原因并增量更新当前步骤，状态颜色不混淆", async ({
  page,
}) => {
  await queueFixture(page);
  await page.goto("/#/queue", { waitUntil: "domcontentloaded" });
  const failed = page.locator('[data-job-id="failed"]');
  await expect(
    failed.getByText("门店时段已失效，请重新选择时段"),
  ).toBeVisible();
  await expect(failed.locator("details")).not.toHaveAttribute("open", "");
  const tones = {
    failed: "danger",
    running: "info",
    payment: "warning",
    unknown: "danger",
    success: "success",
    cancelled: "neutral",
    interrupted: "warning",
  };
  for (const [id, tone] of Object.entries(tones)) {
    const row = page.locator(`[data-job-id="${id}"]`);
    await expect(row).toHaveAttribute("data-tone", tone);
    await expect(row.locator(".status")).toHaveAttribute("data-tone", tone);
  }
  await page.evaluate(() => {
    (window as any).queueDensityJobs.find(
      (job: any) => job.id === "running",
    ).message = "正在提交前复核金额";
  });
  await expect(
    page.locator('[data-job-id="running"] .queue-job-step'),
  ).toHaveText("正在提交前复核金额");
  await expect(page.locator('[data-job-id="payment"] .status')).toHaveText(
    "待付款",
  );
  await expect(page.locator('[data-job-id="unknown"] .status')).toHaveText(
    "提交结果未知",
  );
  const colors = await page
    .locator(".queue-job .status")
    .evaluateAll((statuses) =>
      statuses.map((status) => getComputedStyle(status).color),
    );
  expect(new Set(colors).size).toBeGreaterThanOrEqual(5);
});

test("紧凑队列保留付款、未知提交入口以及安全的取消边界", async ({ page }) => {
  await queueFixture(page);
  await page.goto("/#/queue", { waitUntil: "domcontentloaded" });
  const payment = page.locator('[data-job-id="payment"]');
  await expect(payment.getByRole("button", { name: "前往付款" })).toBeVisible();
  await expect(
    payment.getByText("订单待付款，请及时打开关联任务完成支付。"),
  ).toBeVisible();
  const unknown = page.locator('[data-job-id="unknown"]');
  await expect(
    unknown.getByRole("button", { name: "查看订单任务" }),
  ).toBeVisible();
  await expect(unknown.getByRole("button", { name: "前往付款" })).toHaveCount(
    0,
  );
  await expect(
    unknown.getByText("请先查询订单确认提交结果，避免重复下单。"),
  ).toBeVisible();
  for (const id of [
    "failed",
    "payment",
    "unknown",
    "success",
    "cancelled",
    "interrupted",
  ])
    await expect(
      page
        .locator(`[data-job-id="${id}"]`)
        .getByRole("button", { name: "取消队列任务" }),
    ).toHaveCount(0);
  await expect(
    page
      .locator('[data-job-id="running"]')
      .getByRole("button", { name: "取消队列任务" }),
  ).toBeVisible();
});

test("队列合并订单证据优先显示未知与待付款，运行阶段使用中文", async ({
  page,
}) => {
  await queueFixture(page);
  await page.goto("/#/queue", { waitUntil: "domcontentloaded" });
  await page.evaluate(() => {
    const jobs = (window as any).queueDensityJobs;
    jobs.find((job: any) => job.id === "failed").result = {
      pending_orders: [
        { order_number: "W123456789", status: "pending_payment" },
      ],
    };
    jobs.find((job: any) => job.id === "running").message = "search";
  });
  const pending = page.locator('[data-job-id="failed"]');
  await expect(pending).toHaveAttribute("data-tone", "warning");
  await expect(pending.locator(".status")).toHaveText("待付款");
  await expect(pending.getByRole("button", { name: "前往付款" })).toBeVisible();
  await expect(pending.locator(".queue-job-step")).toHaveText(
    "门店时段已失效，请重新选择时段",
  );
  const stage = page.locator('[data-job-id="running"] .queue-job-step');
  await expect(stage).not.toHaveText("search");
  await expect(stage).toContainText("门店");
  await page.evaluate(() => {
    (window as any).queueDensityJobs.find(
      (job: any) => job.id === "failed",
    ).result = {
      pending_orders: [
        { order_number: "W123456789", status: "pending_payment" },
      ],
      jobs: [{ result: { status: "payment_unknown", order_submitted: true } }],
    };
  });
  await expect(pending).toHaveAttribute("data-tone", "danger");
  await expect(pending.locator(".status")).toHaveText("提交结果未知");
  await expect(
    pending.getByRole("button", { name: "查看订单任务" }),
  ).toBeVisible();
  await expect(pending.getByRole("button", { name: "前往付款" })).toHaveCount(
    0,
  );
  await page.evaluate(() => {
    (window as any).queueDensityJobs.find(
      (job: any) => job.id === "failed",
    ).result = {
      jobs: [{ result: { order_submitted: true } }],
    };
  });
  await expect(pending).toHaveAttribute("data-tone", "danger");
  await expect(pending.locator(".status")).toHaveText("提交结果未知");
});

for (const width of [320, 390, 1180]) {
  test(`${width}px队列辅助操作紧凑，详情可键盘展开且保留时间`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 844 });
    await queueFixture(page);
    await page.goto("/#/queue", { waitUntil: "domcontentloaded" });
    const row = page.locator('[data-job-id="running"]');
    await expect(row.getByText("正在核对门店和联系人")).toBeVisible();
    await expect(
      row.locator('.queue-job-times time[data-kind="enqueued"]'),
    ).toBeVisible();
    await expect(row.getByRole("link", { name: "查看任务日志" })).toBeVisible();
    await mkdir("artifacts/ui-review/queue-density", { recursive: true });
    await page.screenshot({
      path: `artifacts/ui-review/queue-density/${width}.png`,
      fullPage: true,
    });
    const bounds = await row.boundingBox();
    const times = await row.locator(".queue-job-times").boundingBox();
    expect(bounds!.height - times!.height - 12).toBeLessThanOrEqual(
      width === 1180 ? 170 : 220,
    );
    const details = row.locator("details");
    await details.locator("summary").focus();
    await page.keyboard.press("Enter");
    await expect(details).toHaveAttribute("open", "");
    await expect(
      row.locator('.queue-job-times time[data-kind="enqueued"]'),
    ).toBeVisible();
    await page.keyboard.press("Space");
    await expect(details).not.toHaveAttribute("open", "");
    await noHorizontalOverflow(page);
  });
}
