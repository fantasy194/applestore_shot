import { test, expect, type Page } from "@playwright/test";

async function fixture(
  page: Page,
  options: {
    action?: string;
    status?: string;
    warmOnly?: boolean;
    unknown?: boolean;
    scheduled?: boolean;
    missingConfig?: boolean;
    resolvedUnknown?: boolean;
    prepared?: boolean;
  } = {},
) {
  const calls: any[] = [];
  const replies: any[] = [];
  const account = {
    id: "priority-account",
    name: "购买优先测试账号",
    email: "priority@example.test",
    pending: false,
    preset_sku: "MLH74FE",
    preset_quantity: 1,
  };
  const task: any = {
    id: "synthetic-existing-task",
    account_id: account.id,
    action: options.action || "session_maintain",
    status: options.status || "running",
    warm_only: options.warmOnly === true,
    created_at: 1000,
    updated_at: 1000,
    stage: "测试账号任务",
    logs: [],
    prompt:
      options.status === "waiting"
        ? {
            id: "synthetic-code-prompt",
            kind: "code",
            label: "请输入六位验证码",
          }
        : null,
    result: options.unknown
      ? {
          status: "submission_unknown",
          ...(options.resolvedUnknown
            ? { order_number: "SYNTHETIC-ORDER" }
            : {}),
        }
      : null,
    order_submitted: options.unknown === true,
    ...(options.scheduled
      ? {
          status: "scheduled",
          action: "purchase",
          schedule: {
            at_ms: Date.now() + 600000,
            sku: "MLH74FE",
            quantity: 1,
            fulfillment: "HOME",
          },
        }
      : {}),
  };
  const tasks = [task];
  if (options.prepared) {
    Object.assign(task, {
      action: "purchase",
      status: "waiting",
      prompt: {
        id: "synthetic-prepared-prompt",
        kind: "confirm",
        label: "自提准备完成",
        details: {
          operation: "prepared_pickup",
          expires_at: Date.now() / 1000 + 300,
          items: [{ name: "合成商品", quantity: 1, line_total: "RMB 1" }],
          total: "RMB 1",
          store: "合成门店",
        },
      },
    });
    tasks.push({
      ...task,
      id: "synthetic-concurrent-maintenance",
      action: "session_maintain",
      status: "running",
      prompt: null,
      created_at: 999,
    });
  }
  if (options.resolvedUnknown)
    tasks.push({
      ...task,
      id: "synthetic-official-orders",
      action: "orders",
      status: "succeeded",
      created_at: 1001,
      updated_at: 1001,
      prompt: null,
      order_submitted: false,
      result: {
        success: true,
        orders: [
          {
            order_number: "SYNTHETIC-ORDER",
            verified_by: "official_detail",
            items: [{ status: "PAID" }],
          },
        ],
      },
    });
  const snapshot = {
    accounts: [account],
    tasks,
    data_dir: "/synthetic-priority-server",
  };
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    const body = route.request().postDataJSON() || {};
    const fulfill = (json: any, status = 200) =>
      route.fulfill({ status, json });
    if (path === "/api/auth/status") return fulfill({ authenticated: true });
    if (path === "/api/workspace_snapshot") return fulfill(snapshot);
    if (path === "/api/workspace_changes")
      return fulfill({
        cursor: "priority:1",
        reset: true,
        snapshot,
        tasks: [],
        removed_task_ids: [],
      });
    if (path === "/api/maintenance_command")
      return fulfill({
        enabled: true,
        flow: "login",
        interval_minutes: 10,
        last_runs: [
          {
            account_id: account.id,
            remaining_seconds: 600,
            requires_login: true,
            cooldown_until_ms: Date.now() + 3600000,
          },
        ],
      });
    if (path === "/api/reply_task") {
      replies.push(body);
      return fulfill({ success: true });
    }
    if (path === "/api/start_task") {
      calls.push(body);
      if (options.missingConfig)
        return fulfill({ error: "请先保存完整的收货地址或自提配置。" }, 400);
      return fulfill(`synthetic-priority-request-${calls.length}`);
    }
    return fulfill({ success: true });
  });
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await expect(
    page.getByRole("button", { name: "立即下单", exact: true }),
  ).toBeVisible();
  return { calls, replies };
}

for (const [action, warmOnly] of [
  ["session_maintain", false],
  ["login", true],
] as const) {
  for (const status of ["running", "waiting"]) {
    test(`${action} warm_only=${warmOnly} ${status} 时真实购买四入口都能调用API，其他账号操作仍串行`, async ({
      page,
    }) => {
      const { calls } = await fixture(page, { action, warmOnly, status });
      await expect(
        page.getByRole("button", { name: "提前准备（不下单）", exact: true }),
      ).toBeDisabled();
      await expect(
        page.getByRole("button", {
          name: "提前准备自提（不下单）",
          exact: true,
        }),
      ).toBeDisabled();
      await expect(
        page.getByRole("button", { name: "预热登录（不加购）", exact: true }),
      ).toBeDisabled();
      await page.getByRole("button", { name: "立即下单", exact: true }).click();
      await expect.poll(() => calls.length).toBe(1);
      await page
        .getByRole("button", { name: "立即下单（自提）", exact: true })
        .click();
      await expect.poll(() => calls.length).toBe(2);
      await page
        .getByRole("button", { name: "设定定时抢购", exact: true })
        .click();
      await expect.poll(() => calls.length).toBe(3);
      await page.getByRole("button", { name: "购物袋", exact: true }).click();
      await expect(
        page.getByRole("button", { name: "刷新", exact: true }),
      ).toBeDisabled();
      await page
        .getByRole("button", { name: "结算购物袋", exact: true })
        .click();
      await expect.poll(() => calls.length).toBe(4);
      expect(
        calls.map((call) => [
          call.accountId,
          call.action,
          call.params.fulfillment || null,
        ]),
      ).toEqual([
        ["priority-account", "purchase", "HOME"],
        ["priority-account", "purchase", "RETAIL"],
        ["priority-account", "purchase", "HOME"],
        ["priority-account", "checkout", null],
      ]);
      expect(calls[2].params.scheduled_at).toBeGreaterThan(Date.now());
      expect(
        calls.slice(0, 3).every((call) => call.params.prepare_only !== true),
      ).toBe(true);
    });
  }
}

for (const scenario of [
  { label: "真实购买执行中", action: "purchase", status: "running" },
  {
    label: "真实登录等待验证码",
    action: "login",
    status: "waiting",
    warmOnly: false,
  },
  {
    label: "提交结果未知",
    action: "purchase",
    status: "failed",
    unknown: true,
  },
  {
    label: "维护含异常提交证据",
    action: "session_maintain",
    status: "running",
    unknown: true,
  },
  { label: "已存在定时抢购", scheduled: true },
]) {
  test(`${scenario.label}不会因购买优先而开放重复购买`, async ({ page }) => {
    const { calls } = await fixture(page, scenario);
    await expect(
      page.getByRole("button", { name: "立即下单", exact: true }),
    ).toBeDisabled();
    await expect(
      page.getByRole("button", { name: "立即下单（自提）", exact: true }),
    ).toBeDisabled();
    if (!scenario.scheduled)
      await expect(
        page.getByRole("button", { name: "设定定时抢购", exact: true }),
      ).toBeDisabled();
    await page.getByRole("button", { name: "购物袋", exact: true }).click();
    await expect(
      page.getByRole("button", { name: "结算购物袋", exact: true }),
    ).toBeDisabled();
    expect(calls).toEqual([]);
  });
}

test("已由后续官网订单确认的旧未知记录不会永久封锁购买", async ({ page }) => {
  const { calls } = await fixture(page, {
    action: "purchase",
    status: "failed",
    unknown: true,
    resolvedUnknown: true,
  });
  await page.getByRole("button", { name: "立即下单", exact: true }).click();
  await expect.poll(() => calls.length).toBe(1);
});

test("购买优先仍呈现服务端缺配置拒绝，不伪造默认收货配置", async ({ page }) => {
  const { calls } = await fixture(page, { missingConfig: true });
  await page
    .getByRole("button", { name: "立即下单（自提）", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText(
    "请先保存完整的收货地址或自提配置。",
  );
  expect(calls).toHaveLength(1);
  expect(calls[0].params).not.toHaveProperty("contact");
  expect(calls[0].params).not.toHaveProperty("pickup");
});

for (const width of [320, 390]) {
  test(`${width}px 维护等待验证码时购买入口可用且无横向溢出`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 844 });
    const { calls } = await fixture(page, { status: "waiting" });
    const purchase = page.locator("section.section").filter({
      has: page.getByRole("button", { name: "立即下单", exact: true }),
    });
    await expect(
      purchase.getByRole("button", { name: "立即下单", exact: true }),
    ).toBeEnabled();
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth),
    ).toBeLessThanOrEqual(width);
    await purchase.screenshot({
      path: `artifacts/goal-0111-purchase-priority-${width}.png`,
    });
    await purchase
      .getByRole("button", { name: "立即下单", exact: true })
      .click();
    await expect.poll(() => calls.length).toBe(1);
  });
}

test("已准备自提继续直接回复原任务，不受另一个维护任务的忙碌状态影响", async ({
  page,
}) => {
  const { calls, replies } = await fixture(page, { prepared: true });
  await expect(
    page.getByRole("button", { name: "立即下单", exact: true }),
  ).toBeDisabled();
  await page
    .getByRole("button", { name: "立即下单（已准备自提）", exact: true })
    .click();
  await expect
    .poll(() => replies)
    .toEqual([
      {
        accountId: "priority-account",
        taskId: "synthetic-existing-task",
        requestId: "synthetic-prepared-prompt",
        value: true,
      },
    ]);
  expect(calls).toEqual([]);
});

for (const width of [320, 390]) {
  test(`${width}px 真实购买执行中仍显示禁用购买按钮`, async ({ page }) => {
    await page.setViewportSize({ width, height: 844 });
    const { calls } = await fixture(page, {
      action: "purchase",
      status: "running",
    });
    const panel = page.locator("section.section").filter({
      has: page.getByRole("button", { name: "立即下单", exact: true }),
    });
    await expect(
      panel.getByRole("button", { name: "立即下单", exact: true }),
    ).toBeDisabled();
    await expect(
      panel.getByRole("button", { name: "立即下单（自提）", exact: true }),
    ).toBeDisabled();
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth),
    ).toBeLessThanOrEqual(width);
    await panel.screenshot({
      path: `artifacts/goal-0111-purchase-busy-${width}.png`,
    });
    expect(calls).toEqual([]);
  });
}
