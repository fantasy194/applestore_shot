import { test, expect } from "@playwright/test";

test("候选池下单展示实际选店和时段，不把库存发现门店当作成交门店", async ({
  page,
}) => {
  await page.addInitScript(() => {
    const root = window as any;
    const policy = {
      selection_mode: "priority",
      stores: [
        { store_id: "R001", store_name: "优先一店", search: "210000" },
        { store_id: "R002", store_name: "实际二店", search: "210000" },
      ],
      time_slot_rule: { mode: "earliest" },
      contact_mode: "official",
    };
    const order = {
      success: true,
      status: "pending_payment",
      order_number: "W123456789",
      task_id: "child-task",
      queue_job_id: "priority-job",
      pickup_store: "实际二店",
      pickup_store_id: "R002",
      pickup_time_slot: {
        date: "2026-10-24",
        start: "10:00",
        end: "10:30",
        label: "上午",
        store_id: "R002",
      },
      amount_due: "RMB 145",
    };
    const job = {
      id: "priority-job",
      queue_job_id: "priority-job",
      sequence: 1,
      enqueued_at_ms: 1792701000123,
      account_id: "a",
      sku: "MLH74FE",
      quantity: 1,
      concurrency: 1,
      status: "succeeded",
      started: true,
      store_id: "R001",
      store_name: "库存发现一店",
      pickup_policy: policy,
      result: order,
    };
    const task = {
      id: "queue-task",
      account_id: "a",
      action: "purchase",
      status: "succeeded",
      stage: "已完成",
      created_at: 1792701000,
      updated_at: 1792701001,
      order_submitted: true,
      logs: [],
      result: {
        status: "queue_complete",
        success: true,
        order_submitted: true,
        queue_jobs: [job],
        pending_orders: [order],
        attempts: [],
      },
    };
    root.__DUO_KILLER_TEST__ = {
      snapshot: async () =>
        structuredClone({
          accounts: [
            {
              id: "a",
              name: "测试账号",
              email: "fixture@example.test",
              preset_sku: "MLH74FE",
              preset_quantity: 1,
            },
          ],
          tasks: [task],
          data_dir: "/fixture",
        }),
      subscribe: async () => () => {},
      queue: async (operation: string) => {
        if (operation !== "status")
          throw new Error("fixture forbids queue mutations");
        return structuredClone({
          mode: "sequential",
          max_concurrency: 1,
          paused: false,
          jobs: [job],
          blocked_accounts: [],
          policy_errors: root.policyErrors || [],
          child_capacity: 10,
          reserved_child_slots: 0,
        });
      },
      start: async () => {
        throw new Error("fixture forbids starting any operation");
      },
    };
  });
  await page.goto("/");
  const result = page.locator(".queue-job-result");
  await expect(
    result.getByRole("heading", { name: "实际二店 · 已完成" }),
  ).toBeVisible();
  await expect(result.getByRole("heading", { level: 4 })).not.toContainText(
    "库存发现一店",
  );
  await expect(result).toContainText("自提门店：实际二店");
  await expect(result).toContainText("取货时段：2026-10-24 10:00–10:30");
  await expect(result).toContainText("优先一店 → 实际二店");
  await page.getByRole("link", { name: "执行队列", exact: true }).click();
  const queue = page.locator('.queue-job[data-job-id="priority-job"]');
  await expect(queue.locator(".queue-job-heading strong")).toHaveText("抛光布");
  await expect(queue.locator(".queue-job-heading")).toContainText(
    "MLH74FE · 实际二店",
  );
  await queue.locator('summary[aria-label="任务详情"]').click();
  await expect(queue).toContainText("优先一店 (R001) → 实际二店 (R002)");
  await expect(queue).toContainText("取货时段：2026-10-24 10:00–10:30");
  await page.evaluate(() => {
    (window as any).policyErrors = [
      {
        account_id: "a",
        sku: "MLH74FE",
        quantity: 1,
        message: "同一候选池的并发数或失败重试次数不一致，请统一配置后再执行",
      },
    ];
  });
  await expect(
    page.getByRole("heading", { name: "自提偏好需要处理" }),
  ).toBeVisible();
  await expect(page.getByRole("alert")).toContainText(
    "并发数或失败重试次数不一致",
  );
  await expect(
    page.getByRole("link", { name: "修改自提偏好" }),
  ).toHaveAttribute("href", "#/settings");
  await page.evaluate(() => {
    (window as any).policyErrors = [];
  });
  await expect(
    page.getByRole("heading", { name: "自提偏好需要处理" }),
  ).toHaveCount(0);
});

for (const scenario of ["single", "queue"] as const) {
  for (const width of [390, 1180]) {
    test(`已返回订单号但付款待核实：${scenario} ${width}px`, async ({
      page,
    }) => {
      await page.setViewportSize({ width, height: 800 });
      await page.addInitScript((kind) => {
        const root = window as any;
        const unverified = {
          success: false,
          status: "verification_required",
          order_number: "W123456789",
          order_submitted: true,
          message: "付款金额与结算金额不一致，请核对。",
          payment_url: "https://example.test/unverified-payment",
          qr_data: "data:image/png;base64,ZmFrZQ==",
        };
        const job = {
          queue_job_id: "verification-job",
          store_id: "R001",
          store_name: "测试门店",
          sku: "MLH74FE",
          status: "verification_required",
          started: true,
          result: unverified,
        };
        const task = {
          id: "verification-task",
          account_id: "a",
          action: "purchase",
          status: "failed",
          stage: "付款金额与结算金额不一致，请核对。",
          created_at: 1792701000,
          updated_at: 1792701001,
          order_submitted: true,
          logs: [],
          result:
            kind === "queue"
              ? {
                  success: false,
                  status: "queue_complete",
                  order_submitted: true,
                  queue_jobs: [job],
                  pending_orders: [],
                  attempts: [],
                }
              : unverified,
        };
        root.__DUO_KILLER_TEST__ = {
          snapshot: async () =>
            structuredClone({
              accounts: [
                {
                  id: "a",
                  name: "测试账号",
                  email: "fixture@example.test",
                  preset_sku: "MLH74FE",
                  preset_quantity: 1,
                },
              ],
              tasks: [task],
              data_dir: "/fixture",
            }),
          subscribe: async () => () => {},
          queue: async (operation: string) => {
            if (operation !== "status") throw new Error("fixture is read-only");
            return { jobs: [], blocked_accounts: [], policy_errors: [] };
          },
          start: async () => {
            throw new Error("fixture forbids starting any operation");
          },
        };
      }, scenario);

      await page.goto("/", { waitUntil: "domcontentloaded" });
      const task = page.locator(".task-detail");
      await expect(task.locator(".section-title .status")).toHaveText("失败");
      await expect(task).toContainText("付款金额与结算金额不一致，请核对。");
      await expect(task).toContainText("订单 W123456789 · 付款信息待核实。");
      await expect(
        page.getByRole("button", { name: "立即下单（自提）", exact: true }),
      ).toBeEnabled();
      await expect(
        page.getByRole("region", { name: "下单进度" }).getByRole("status"),
      ).toContainText("付款信息待核实");
      if (scenario === "queue") {
        const job = task.locator('[data-queue-job-id="verification-job"]');
        await expect(job).toContainText("付款信息待核实");
        await expect(job).not.toContainText("未取得待付款订单");
      }
      await expect(
        task.getByRole("img", { name: /支付宝付款二维码/ }),
      ).toHaveCount(0);
      await expect(
        task.getByRole("button", { name: "获取二维码" }),
      ).toHaveCount(0);
      await expect(
        task.getByRole("button", { name: "我已支付，查询订单" }),
      ).toHaveCount(0);
      await expect(
        task.locator('a[href="https://example.test/unverified-payment"]'),
      ).toHaveCount(0);
      await expect
        .poll(() =>
          page.evaluate(
            () => document.documentElement.scrollWidth - innerWidth,
          ),
        )
        .toBeLessThanOrEqual(0);
    });
  }
}
