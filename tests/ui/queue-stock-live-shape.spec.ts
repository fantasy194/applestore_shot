import { expect, test } from "@playwright/test";
import { uiReviewFixture, noHorizontalOverflow } from "./ui-review-fixture";

// Only structural fields and timestamps from the observed 0.1.5 response;
// account/task identities, SKU and store are synthetic. No credentials or orders.
const job = {
  id: "observed-interrupted-shape",
  sequence: 1,
  enqueued_at_ms: 1792813343668,
  stock_detected_at_ms: 1792813343668,
  queued_at_ms: 1792813343865,
  checked_at_ms: 1792813343668,
  started_at_ms: null,
  order_confirmed_at_ms: null,
  account_id: "review-a",
  target_id: "fixture-target",
  sku: "MLH74FE/A",
  quantity: 1,
  store_id: "R001",
  store_name: "Apple 测试门店",
  location: "200000",
  concurrency: 1,
  retry_count: 0,
  status: "interrupted",
  stock_status: "available",
  sample_rejected_as_stale: false,
  task_id: "fixture-task",
  message: "本地服务重启，已派发任务已中断，未自动重放；请先查询官网订单",
  result: null,
  order_submitted: false,
  assignment: 1,
  authorization_confirmed: true,
  authorized_stores: [],
  episode_key: "review-a|MLH74FE/A|R001",
};

for (const width of [1180, 390, 320]) {
  test(`${width}px 现场有货时间字段经真实API形状持续显示`, async ({ page }) => {
    await page.setViewportSize({ width, height: 844 });
    await uiReviewFixture(page);
    const queue = {
      mode: "sequential",
      max_concurrency: 1,
      check_inventory_freshness: true,
      paused: true,
      capacity: 500,
      running_count: 0,
      queued_count: 0,
      child_capacity: 10,
      reserved_child_slots: 0,
      blocked_accounts: [],
      policy_errors: [],
      jobs: [job],
    };
    let polls = 0;
    const snapshot = {
      accounts: [
        { id: "review-a", name: "日常账号", email: "review@example.test" },
      ],
      tasks: [],
      data_dir: "synthetic-fixture",
    };
    // Also exercise the compiled local-web bridge, where DEV injection is absent.
    await page.route("**/api/**", async (route) => {
      const path = new URL(route.request().url()).pathname;
      let result: unknown = { authenticated: true, success: true };
      if (path.endsWith("queue_command")) {
        polls++;
        result = queue;
      } else if (path.endsWith("workspace_snapshot")) result = snapshot;
      else if (path.endsWith("workspace_changes"))
        result = {
          cursor: "fixture:1",
          reset: true,
          snapshot,
          tasks: [],
          removed_task_ids: [],
        };
      await route.fulfill({ json: result });
    });
    await page.addInitScript((queue) => {
      const root = window as any;
      if (root.__DUO_KILLER_TEST__) {
        root.observedQueuePolls = 0;
        root.__DUO_KILLER_TEST__.queue = async () => {
          root.observedQueuePolls++;
          return structuredClone(queue);
        };
      }
    }, queue);
    await page.goto("/#/queue", { waitUntil: "domcontentloaded" });
    const row = page.locator(`[data-job-id="${job.id}"]`);
    const stock = row.locator('time[data-kind="stock-detected"]');
    await expect(stock).toBeVisible();
    await expect(stock).toHaveAttribute(
      "datetime",
      new Date(job.stock_detected_at_ms).toISOString(),
    );
    await expect(stock).toHaveAttribute("aria-label", /发现有货/);
    await expect(row).not.toContainText("发现有货时间未记录");
    await expect(
      row.locator(
        'time[data-kind="started"],time[data-kind="order-confirmed"]',
      ),
    ).toHaveCount(0);
    await expect(row.locator('time[data-kind="enqueued"]')).toHaveAttribute(
      "datetime",
      new Date(job.queued_at_ms).toISOString(),
    );
    await page.getByRole("button", { name: "刷新队列" }).click();
    await expect
      .poll(
        async () =>
          polls ||
          (await page.evaluate(() => (window as any).observedQueuePolls || 0)),
      )
      .toBeGreaterThan(1);
    await expect(stock).toHaveAttribute(
      "datetime",
      new Date(job.stock_detected_at_ms).toISOString(),
    );
    await noHorizontalOverflow(page);
    await row.screenshot({ path: `artifacts/goal-016-stock-${width}.png` });
  });
}
