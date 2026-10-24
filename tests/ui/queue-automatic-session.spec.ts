import { test, expect } from "@playwright/test";

for (const width of [320, 390]) {
  test(`${width}px 旧认证锁不再显示手动解锁，提交未知账号和订单保护仍保留`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 844 });
    const accounts = [
      {
        id: "old-auth-account",
        name: "旧认证账号",
        email: "old-auth@example.test",
        pending: false,
      },
      {
        id: "unknown-order-account",
        name: "订单待核实账号",
        email: "unknown@example.test",
        pending: false,
      },
    ];
    // Actual queue_command wire shape: blocked_accounts is a list of account
    // and reason pairs, independent of the complete queue job records.
    const queue = {
      mode: "sequential",
      max_concurrency: 1,
      paused: false,
      capacity: 500,
      child_capacity: 10,
      reserved_child_slots: 0,
      queued_count: 0,
      running_count: 0,
      blocked_accounts: [
        { account_id: "old-auth-account", reason: "authentication" },
        { account_id: "unknown-order-account", reason: "submission_unknown" },
      ],
      policy_errors: [],
      jobs: [
        {
          id: "unknown-order-job",
          sequence: 2,
          enqueued_at_ms: 1792813343668,
          queued_at_ms: 1792813343865,
          stock_detected_at_ms: 1792813343668,
          account_id: "unknown-order-account",
          target_id: "synthetic-unknown-target",
          sku: "MLH74FE/A",
          quantity: 1,
          concurrency: 1,
          retry_count: 0,
          store_id: "R001",
          store_name: "Apple 测试门店",
          status: "submission_unknown",
          task_id: "synthetic-unknown-task",
          message: "订单提交结果待核实",
          result: { status: "submission_unknown", order_submitted: true },
          order_submitted: true,
          assignment: 1,
          authorization_confirmed: true,
          authorized_stores: [],
        },
      ],
    };
    const snapshot = {
      accounts,
      tasks: [],
      data_dir: "/synthetic-queue-server",
    };
    const operations: string[] = [];
    await page.route("**/api/**", async (route) => {
      const path = new URL(route.request().url()).pathname;
      let result: unknown = { authenticated: true, success: true };
      if (path === "/api/queue_command") {
        operations.push(route.request().postDataJSON().operation);
        result = queue;
      } else if (path === "/api/workspace_snapshot") result = snapshot;
      else if (path === "/api/workspace_changes")
        result = {
          cursor: "queue-automatic:1",
          reset: true,
          snapshot,
          tasks: [],
          removed_task_ids: [],
        };
      await route.fulfill({ json: result });
    });
    await page.goto("/#/queue", { waitUntil: "domcontentloaded" });
    const blocked = page.locator(".queue-blocked");
    await expect(blocked).toHaveCount(1);
    await expect(blocked).toContainText("订单待核实账号");
    await expect(blocked).not.toContainText("旧认证账号");
    await expect(blocked).toContainText("订单提交结果未知，请先查询订单确认");
    await expect(blocked.getByText("需要处理", { exact: true })).toBeVisible();
    await expect(
      blocked.getByRole("link", { name: "查看账号日志", exact: true }),
    ).toHaveAttribute("href", /account_id=unknown-order-account/);
    await expect(
      page.getByText("需要恢复官网登录", { exact: true }),
    ).toHaveCount(0);
    await expect(
      page.getByRole("button", { name: "登录恢复后继续此账号", exact: true }),
    ).toHaveCount(0);
    const job = page.locator('[data-job-id="unknown-order-job"]');
    await expect(job.getByText("提交结果未知", { exact: true })).toBeVisible();
    await expect(job).toContainText("请先查询订单确认提交结果，避免重复下单。");
    await expect(
      job.getByRole("button", { name: "取消队列任务", exact: true }),
    ).toHaveCount(0);
    await expect(
      job.getByRole("button", { name: "查看订单任务", exact: true }),
    ).toBeVisible();
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth),
    ).toBeLessThanOrEqual(width);
    await page.screenshot({
      path: `artifacts/goal-019-queue-automatic-${width}.png`,
      fullPage: true,
    });
    await blocked.screenshot({
      path: `artifacts/goal-019-queue-unknown-guard-${width}.png`,
    });
    await page.getByRole("button", { name: "刷新队列", exact: true }).click();
    await expect(blocked).toHaveCount(1);
    await page.reload({ waitUntil: "domcontentloaded" });
    await expect(blocked).toHaveCount(1);
    await expect(
      page.getByRole("button", { name: "登录恢复后继续此账号", exact: true }),
    ).toHaveCount(0);
    expect(operations.every((operation) => operation === "status")).toBe(true);
  });
}
