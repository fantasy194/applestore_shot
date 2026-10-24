import { test, expect, type Page } from "@playwright/test";

async function fixture(
  page: Page,
  options: {
    stage?: "review" | "fulfillment";
    action?: "purchase" | "session_maintain";
    blocker?: "code" | "unknown";
  } = {},
) {
  const starts: any[] = [];
  const replies: any[] = [];
  const cancels: any[] = [];
  const account = {
    id: "prepared-fixture-account",
    name: "原准备测试账号",
    email: "prepared@example.test",
    pending: false,
    preset_sku: "MLH74FE",
    preset_quantity: 1,
  };
  const now = Date.now();
  const stage = options.stage || "review";
  const prepared = {
    v: 1,
    sku: "MLH74FE",
    quantity: 1,
    fulfillment: "RETAIL",
    config_fingerprint: "a".repeat(64),
    config_file_sha256: "b".repeat(64),
    authorized_store_ids: ["R001"],
    prepared_at_ms: now - 1000,
    expires_at_ms: now + 299000,
    preparation_stage: stage,
    review_verified: stage === "review",
    order_ready: stage === "review",
  };
  const task: any = {
    id: "original-prepared-task",
    account_id: account.id,
    action: options.action || "purchase",
    status: "waiting",
    created_at: 1000,
    updated_at: 1000,
    logs: [],
    stage: "保留原自提准备",
    order_submitted: false,
    result: null,
    prepared_handoff: null,
    prompt: {
      id: "original-prepared-prompt",
      kind: "prepared_purchase",
      label: "原自提准备",
      prepared,
    },
  };
  const tasks = [task];
  if (options.blocker)
    tasks.push({
      ...task,
      id: "other-account-operation",
      action: options.blocker === "code" ? "login" : "purchase",
      status: options.blocker === "code" ? "waiting" : "failed",
      created_at: 2000,
      updated_at: 2000,
      prepared_handoff: null,
      prompt:
        options.blocker === "code"
          ? {
              id: "ordinary-otp-prompt",
              kind: "code",
              label: "请输入六位验证码",
            }
          : null,
      order_submitted: options.blocker === "unknown",
      result:
        options.blocker === "unknown"
          ? { status: "submission_unknown", order_submitted: true }
          : null,
    });
  const snapshot = {
    accounts: [account],
    tasks,
    data_dir: "/synthetic-prepared-server",
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
        cursor: `prepared:${task.status}`,
        reset: true,
        snapshot,
        tasks: [],
        removed_task_ids: [],
      });
    if (path === "/api/start_task") {
      starts.push(body);
      expect(body.accountId).toBe(account.id);
      expect(body.action).toBe("purchase");
      expect(body.params.fulfillment).toBe("RETAIL");
      expect(body.params.sku).toBe(prepared.sku);
      expect(body.params.quantity).toBe(prepared.quantity);
      task.action = "purchase";
      task.status = "running";
      task.stage = "已接手原自提准备，正在复核并下单";
      task.prompt = null;
      task.prepared_handoff = {
        v: 1,
        account_id: account.id,
        prepared_task_id: task.id,
        request_id: "original-prepared-prompt",
        source: "manual",
        requested_at_ms: Date.now(),
        claimed_at_ms: Date.now(),
      };
      return fulfill(task.id);
    }
    if (path === "/api/reply_task") {
      replies.push(body);
      return fulfill(
        { error: "prepared_purchase 必须经新的购买授权接手" },
        400,
      );
    }
    if (path === "/api/cancel_task") {
      cancels.push(body);
      task.status = "cancelled";
      task.stage = "准备已结束";
      task.prompt = null;
      return fulfill({ success: true });
    }
    return fulfill({ success: true });
  });
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await expect(
    page.getByRole("button", { name: "立即下单（自提）", exact: true }),
  ).toBeVisible();
  return { starts, replies, cancels };
}

for (const action of ["purchase", "session_maintain"] as const) {
  for (const stage of ["review", "fulfillment"] as const) {
    test(`${action} ${stage} 准备不阻止新购买授权，统一start接手后禁止重复购买`, async ({
      page,
    }) => {
      const { starts, replies, cancels } = await fixture(page, {
        action,
        stage,
      });
      const status = page.getByRole("region", {
        name: "自提准备状态",
        exact: true,
      });
      await expect(status.getByRole("heading")).toHaveText(
        stage === "review" ? "自提准备就绪" : "已保留自提准备",
      );
      await expect(status).toContainText("尚未创建订单");
      if (stage === "fulfillment")
        await expect(status).toContainText("下单时仍需确认门店及有效时段");
      else await expect(status).not.toContainText("仍需确认门店及有效时段");
      await expect(status.locator("input,textarea,select,form")).toHaveCount(0);
      await expect(status.getByRole("button")).toHaveCount(1);
      await expect(
        status.getByRole("button", { name: "结束准备", exact: true }),
      ).toBeVisible();
      await expect(
        status.getByRole("button", { name: /继续|下单|确认/ }),
      ).toHaveCount(0);
      await expect(
        page.getByRole("button", { name: "立即下单", exact: true }),
      ).toBeEnabled();
      await page
        .getByRole("button", { name: "立即下单（自提）", exact: true })
        .click();
      await expect.poll(() => starts.length).toBe(1);
      expect(starts[0].params).not.toHaveProperty("prepared_claim");
      expect(starts[0].params).not.toHaveProperty("prepared_task_id");
      expect(starts[0].params).not.toHaveProperty("skip_confirmation");
      await expect(status).toHaveCount(0);
      await expect(
        page.getByRole("region", { name: "当前任务", exact: true }),
      ).toContainText("已接手原自提准备，正在复核并下单");
      await expect(
        page.getByRole("button", { name: "立即下单", exact: true }),
      ).toBeDisabled();
      await expect(
        page.getByRole("button", { name: "立即下单（自提）", exact: true }),
      ).toBeDisabled();
      await page.getByRole("button", { name: "购物袋", exact: true }).click();
      await expect(
        page.getByRole("button", { name: "结算购物袋", exact: true }),
      ).toBeDisabled();
      expect(replies).toEqual([]);
      expect(cancels).toEqual([]);
    });
  }
}

test("结束准备只取消原账号的原任务，不回复prepared提示或创建购买", async ({
  page,
}) => {
  const { starts, replies, cancels } = await fixture(page);
  await page
    .getByRole("region", { name: "自提准备状态", exact: true })
    .getByRole("button", { name: "结束准备", exact: true })
    .click();
  await expect
    .poll(() => cancels)
    .toEqual([
      {
        accountId: "prepared-fixture-account",
        taskId: "original-prepared-task",
      },
    ]);
  expect(starts).toEqual([]);
  expect(replies).toEqual([]);
});

for (const blocker of ["code", "unknown"] as const) {
  test(`有独立${blocker}门槛时准备候选不能放开重复购买`, async ({ page }) => {
    const { starts, replies } = await fixture(page, { blocker });
    await expect(
      page.getByRole("button", { name: "立即下单", exact: true }),
    ).toBeDisabled();
    await expect(
      page.getByRole("button", { name: "立即下单（自提）", exact: true }),
    ).toBeDisabled();
    await expect(
      page.getByRole("button", { name: "设定定时抢购", exact: true }),
    ).toBeDisabled();
    expect(starts).toEqual([]);
    expect(replies).toEqual([]);
  });
}

for (const [width, stage] of [
  [320, "fulfillment"],
  [390, "review"],
] as const) {
  test(`${width}px ${stage} 准备状态不出现通用输入和提交按钮且无溢出`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 844 });
    await fixture(page, { stage });
    const status = page.getByRole("region", {
      name: "自提准备状态",
      exact: true,
    });
    await expect(status).toContainText(
      stage === "review" ? "自提准备就绪" : "仍需确认门店及有效时段",
    );
    await expect(status.locator("input,textarea,select,form")).toHaveCount(0);
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth),
    ).toBeLessThanOrEqual(width);
    await status.screenshot({
      path: `artifacts/goal-0113-prepared-handoff-${width}.png`,
    });
    await expect(
      page.getByRole("button", { name: "立即下单（自提）", exact: true }),
    ).toBeEnabled();
  });
}

async function scheduledAssociationFixture(
  page: Page,
  targetMode: "same" | "other" | "missing",
) {
  await page.addInitScript(
    ({ targetMode }) => {
      const root = window as any;
      const account = {
        id: "scheduled-owner",
        name: "定时接手账号",
        email: "scheduled@example.test",
        pending: false,
        preset_sku: "MLH74FE",
        preset_quantity: 1,
      };
      const source = {
        id: "scheduled-source-task",
        account_id: account.id,
        action: "purchase",
        status: "running",
        created_at: 2000,
        updated_at: 2000,
        stage: "定时已触发，原自提准备正在下单",
        logs: [],
        prompt: null,
        result: null,
        order_submitted: false,
        schedule: {
          at_ms: Date.now() - 1000,
          sku: "MLH74FE",
          quantity: 1,
          fulfillment: "RETAIL",
          triggered_at_ms: Date.now() - 500,
        },
        prepared_handoff: {
          prepared_task_id: "actual-prepared-task",
          source: "scheduled",
        },
      };
      const actual = {
        ...source,
        id: "actual-prepared-task",
        account_id: targetMode === "other" ? "another-account" : account.id,
        created_at: 1000,
        stage: "原自提准备接手后正在复核",
        schedule: undefined,
        result: { performance: { durations_ms: { revalidation: 1250 } } },
        prepared_handoff: {
          prepared_task_id: "actual-prepared-task",
          source: "scheduled",
          source_task_id: source.id,
        },
      };
      const tasks = targetMode === "missing" ? [source] : [source, actual];
      root.scheduledAssociationWrites = [];
      root.__DUO_KILLER_TEST__ = {
        snapshot: async () => ({
          accounts: [account],
          tasks: structuredClone(tasks),
          data_dir: "/synthetic-scheduled-association",
        }),
        subscribe: async () => () => {},
        start: async (...args: any[]) => {
          root.scheduledAssociationWrites.push(args);
          throw new Error("Navigation must not create a purchase");
        },
        reply: async (...args: any[]) => {
          root.scheduledAssociationWrites.push(args);
          throw new Error("Navigation must not reply to a prompt");
        },
        cancel: async () => {},
        add: async () => account,
        remove: async () => {},
        savePreset: async () => {},
      };
    },
    { targetMode },
  );
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await expect(
    page.getByRole("region", { name: "当前任务", exact: true }),
  ).toHaveAttribute("id", "task-scheduled-source-task");
}

test("定时S通过同账号exact id打开接手P，耗时只取实际执行任务不复制给S", async ({
  page,
}) => {
  await scheduledAssociationFixture(page, "same");
  const current = page.getByRole("region", { name: "当前任务", exact: true });
  await expect(current).toContainText("定时已触发，原自提准备正在下单");
  await expect(current.getByText("本次下单耗时", { exact: true })).toHaveCount(
    0,
  );
  await current
    .getByRole("button", { name: "查看实际下单任务", exact: true })
    .click();
  await expect(current).toHaveAttribute("id", "task-actual-prepared-task");
  await expect(current).toContainText("原自提准备接手后正在复核");
  await expect(
    current.getByRole("button", { name: "查看实际下单任务", exact: true }),
  ).toHaveCount(0);
  await current.getByText("本次下单耗时", { exact: true }).click();
  await expect(current.locator("details.task-logs dt")).toHaveText([
    "结账复核",
  ]);
  await expect(current.locator("details.task-logs dd")).toHaveText(["1.25 秒"]);
  expect(
    await page.evaluate(() => (window as any).scheduledAssociationWrites),
  ).toEqual([]);
});

for (const targetMode of ["other", "missing"] as const) {
  test(`定时接手目标${targetMode}时不提供越账号或失效跳转`, async ({
    page,
  }) => {
    await scheduledAssociationFixture(page, targetMode);
    const current = page.getByRole("region", { name: "当前任务", exact: true });
    await expect(
      current.getByRole("button", { name: "查看实际下单任务", exact: true }),
    ).toHaveCount(0);
    expect(
      await page.evaluate(() => (window as any).scheduledAssociationWrites),
    ).toEqual([]);
  });
}
