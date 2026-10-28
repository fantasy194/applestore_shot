import { expect, test, type Page } from "@playwright/test";

type Options = {
  action: "purchase" | "checkout" | "session_maintain";
  owner?: "pending" | "missing" | "local";
  otherAccount?: boolean;
};

async function fixture(page: Page, options: Options) {
  await page.addInitScript((options) => {
    const root = window as any;
    const local = "00000000-0000-0000-0000-000000000000";
    const pending = {
      id: options.owner === "local" ? local : "pending-owner",
      name: "待验证账号",
      email: "pending@example.test",
      pending: true,
    };
    const other = {
      id: "verified-other",
      name: "其他账号",
      email: "other@example.test",
      pending: false,
    };
    const task: any = {
      id: "pending-purchase",
      account_id: options.owner === "missing" ? "missing-owner" : pending.id,
      action: options.action,
      status: "waiting",
      stage: "等待密码",
      created_at: Date.now() / 1000 - 1,
      updated_at: Date.now() / 1000,
      order_submitted: false,
      logs: [],
      result: null,
      prompt: {
        id: "password-prompt",
        kind: "password",
        label: "输入 Apple 账号密码",
      },
    };
    const accounts = options.otherAccount ? [other, pending] : [pending];
    let publish = (_task: any) => {};
    const emit = (changes: any) => {
      Object.assign(task, changes, { updated_at: Date.now() / 1000 });
      publish(structuredClone(task));
    };
    root.pendingReplies = [];
    root.pendingStarts = [];
    root.completePendingTask = () =>
      emit({
        status: "failed",
        prompt: null,
        stage: "本次认证被 Apple 拒绝",
        result: { status: "failed", success: false },
      });
    root.__DUO_KILLER_TEST__ = {
      snapshot: async () => ({
        accounts: structuredClone(accounts),
        tasks: [structuredClone(task)],
        data_dir: "/synthetic-pending-purchase",
      }),
      subscribe: async (callback: any) => {
        publish = callback;
        return () => {};
      },
      reply: async (
        accountId: string,
        taskId: string,
        requestId: string,
        value: unknown,
      ) => {
        root.pendingReplies.push({ accountId, taskId, requestId, value });
        if (requestId === "password-prompt") {
          emit({
            status: "waiting",
            stage: "等待验证码",
            prompt: {
              id: "code-prompt",
              kind: "code",
              label: "请输入六位验证码",
            },
          });
        } else emit({ status: "running", stage: "继续执行", prompt: null });
      },
      start: async (accountId: string, action: string) => {
        root.pendingStarts.push({ accountId, action });
        throw new Error("Unexpected synthetic task start");
      },
      browser: async () => ({ active: false }),
      maintenance: async () => ({ enabled: false, last_runs: [] }),
      queue: async () => ({ paused: true, jobs: [] }),
      logs: async () => ({ entries: [], has_more: false }),
      cancel: async () => {},
      add: async () => {
        throw new Error("Unexpected account creation");
      },
      remove: async () => {},
      savePreset: async () => {},
    };
  }, options);
  await page.goto("/", { waitUntil: "domcontentloaded" });
}

for (const [action, width, otherAccount] of [
  ["purchase", 320, false],
  ["checkout", 1180, true],
] as const) {
  test(`${width}px ${action} 可打开待验证账号的现有任务并完成当前提示`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 844 });
    await fixture(page, { action, otherAccount });
    const progress = page.getByRole("region", {
      name: "下单进度",
      exact: true,
    });
    await progress.getByRole("button", { name: "查看当前任务" }).click();
    const panel = page.getByRole("region", { name: "当前任务", exact: true });
    await expect(panel).toHaveAttribute("id", "task-pending-purchase");
    await expect(
      page.getByRole("heading", { name: "待验证账号" }),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: "立即下单", exact: true }),
    ).toBeHidden();
    await expect(
      page.getByRole("button", { name: "登录账号", exact: true }),
    ).toBeHidden();
    await panel.screenshot({
      path: `artifacts/goal-0124-pending-purchase-${action}-${width}.png`,
    });
    await panel.getByLabel("密码", { exact: true }).fill("synthetic-password");
    await panel.getByRole("button", { name: "继续", exact: true }).click();
    const code = panel.getByLabel("六位验证码", { exact: true });
    await expect(code).toBeVisible();
    await code.fill("012345");
    await panel.getByRole("button", { name: "继续", exact: true }).click();
    await expect
      .poll(() => page.evaluate(() => (window as any).pendingReplies))
      .toEqual([
        {
          accountId: "pending-owner",
          taskId: "pending-purchase",
          requestId: "password-prompt",
          value: { password: "synthetic-password", remember: false },
        },
        {
          accountId: "pending-owner",
          taskId: "pending-purchase",
          requestId: "code-prompt",
          value: "012345",
        },
      ]);
    await page.evaluate(() => (window as any).completePendingTask());
    await expect(panel).toContainText("本次认证被 Apple 拒绝");
    expect(
      (
        await page.evaluate(() =>
          (window as any).__DUO_KILLER_TEST__.snapshot(),
        )
      ).accounts.find((a: any) => a.id === "pending-owner").pending,
    ).toBe(true);
    expect(await page.evaluate(() => (window as any).pendingStarts)).toEqual(
      [],
    );
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth),
    ).toBeLessThanOrEqual(width);
    await page.getByRole("button", { name: "选择账号", exact: true }).click();
    await expect(panel).toHaveCount(0);
    await expect(
      page.getByRole("button", { name: "立即下单", exact: true }),
    ).toBeHidden();
  });
}

for (const owner of ["missing", "local"] as const) {
  test(`${owner} 账号不会通过待验证任务入口显示凭据提示`, async ({ page }) => {
    await fixture(page, { action: "purchase", owner });
    await page
      .getByRole("region", { name: "下单进度" })
      .getByRole("button", { name: "查看当前任务" })
      .click();
    await expect(page.getByRole("region", { name: "当前任务" })).toHaveCount(0);
    await expect(page.getByLabel("密码", { exact: true })).toHaveCount(0);
    expect(await page.evaluate(() => (window as any).pendingReplies)).toEqual(
      [],
    );
  });
}

test("待验证账号的非购买任务不会以购买入口开放", async ({ page }) => {
  await fixture(page, { action: "session_maintain" });
  await page.getByRole("button", { name: "1 个任务进行中" }).click();
  await expect(page.getByRole("region", { name: "当前任务" })).toHaveCount(0);
  await expect(page.getByLabel("密码", { exact: true })).toHaveCount(0);
  expect(await page.evaluate(() => (window as any).pendingReplies)).toEqual([]);
});
