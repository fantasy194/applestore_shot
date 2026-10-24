import { test, expect, type Page } from "@playwright/test";

type ChallengeAction = "session_maintain" | "purchase" | "login";

// Synthetic application bridge only. No native commands, credentials or Apple
// requests are used; cancellation and replies are recorded for ownership checks.
async function fixture(
  page: Page,
  options: { action?: ChallengeAction; legacyReason?: string } = {},
) {
  await page.addInitScript(({ action, legacyReason }) => {
    const root = window as any;
    const account = {
      id: "automatic-session-account",
      name: "自动会话测试账号",
      email: "automatic@example.test",
      pending: false,
    };
    let next = 1;
    let current: any = action ? challenge(action, next) : undefined;
    let publish = (_task: any) => {};
    function challenge(kind: string, number: number) {
      return {
        id: `automatic-${kind}-${number}`,
        account_id: account.id,
        action: kind,
        status: "waiting",
        stage: "等待六位验证码",
        created_at: 1000 + number,
        updated_at: 1000 + number,
        logs: [],
        result: null,
        order_submitted: false,
        prompt: {
          id: `automatic-code-${kind}-${number}`,
          kind: "code",
          label: "请输入六位验证码",
        },
      };
    }
    root.automaticSessionReplies = [];
    root.automaticSessionCancels = [];
    root.automaticSessionStarts = [];
    root.nextAutomaticSessionChallenge = () => {
      current = challenge(action!, ++next);
      publish(structuredClone(current));
    };
    root.__DUO_KILLER_TEST__ = {
      snapshot: async () => ({
        accounts: [account],
        tasks: current ? [structuredClone(current)] : [],
        data_dir: "/synthetic-automatic-session",
      }),
      subscribe: async (callback: any) => {
        publish = callback;
        return () => {};
      },
      maintenance: async () => ({
        enabled: true,
        flow: "login",
        interval_minutes: 10,
        last_runs: [
          {
            account_id: account.id,
            status: "failed",
            requires_login: true,
            blocked_reason: legacyReason || null,
            cooldown_until_ms: Date.now() + 3600000,
            remaining_seconds: 600,
            message: "旧版认证失败记录",
          },
        ],
      }),
      reply: async (
        accountId: string,
        taskId: string,
        requestId: string,
        value: unknown,
      ) => {
        root.automaticSessionReplies.push({
          accountId,
          taskId,
          requestId,
          value,
        });
        current.status = "running";
        current.prompt = null;
      },
      cancel: async (accountId: string, taskId: string) => {
        root.automaticSessionCancels.push({ accountId, taskId });
        current.status = "cancelled";
        current.prompt = null;
        publish(structuredClone(current));
      },
      start: async (accountId: string, action: string, params: any) => {
        root.automaticSessionStarts.push({ accountId, action, params });
        throw new Error("Unexpected synthetic task start");
      },
      add: async () => {
        throw new Error("Not allowed");
      },
      remove: async () => {},
      savePreset: async () => {},
    };
  }, options);
  await page.goto("/", { waitUntil: "domcontentloaded" });
}

for (const [width, legacyReason] of [
  [320, "requires_login"],
  [390, "cooldown"],
] as const) {
  test(`${width}px 历史认证失败及旧冷却标记不再遮挡正常预热倒计时`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 844 });
    await fixture(page, { legacyReason });
    const panel = page.getByRole("region", { name: "空闲预热", exact: true });
    await expect(panel.getByRole("status")).toHaveText(
      /^距离预热 (10:00|9:5\d)$/,
    );
    await expect(panel.getByRole("status")).not.toContainText(/需要|手动|冷却/);
    await expect(panel).toContainText("会话失效会自动登录");
    await expect(panel).toContainText("需要六位验证码时，在当前任务输入后继续");
    await expect(panel).toContainText("预热间隔不会阻止购买");
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth),
    ).toBeLessThanOrEqual(width);
    await panel.screenshot({
      path: `artifacts/goal-019-automatic-session-status-${width}.png`,
    });
    expect(
      await page.evaluate(() => (window as any).automaticSessionStarts),
    ).toEqual([]);
  });
}

for (const action of ["session_maintain", "purchase", "login"] as const) {
  for (const width of [320, 390]) {
    test(`${width}px ${action} 在原任务输入前导零验证码后继续，下一次验证仍可取消`, async ({
      page,
    }) => {
      await page.setViewportSize({ width, height: 844 });
      await fixture(page, { action });
      const panel = page.getByRole("region", { name: "当前任务", exact: true });
      const input = panel.getByLabel("六位验证码", { exact: true });
      await expect(input).toHaveAttribute("type", "text");
      await expect(input).toHaveAttribute("inputmode", "numeric");
      await expect(input).toHaveAttribute("autocomplete", "one-time-code");
      await input.fill("012345");
      await expect(input).toHaveValue("012345");
      expect(
        await page.evaluate(() => document.documentElement.scrollWidth),
      ).toBeLessThanOrEqual(width);
      await panel.screenshot({
        path: `artifacts/goal-019-automatic-session-${action}-${width}.png`,
      });
      await panel.getByRole("button", { name: "继续", exact: true }).click();
      await expect
        .poll(() =>
          page.evaluate(() => (window as any).automaticSessionReplies),
        )
        .toEqual([
          {
            accountId: "automatic-session-account",
            taskId: `automatic-${action}-1`,
            requestId: `automatic-code-${action}-1`,
            value: "012345",
          },
        ]);
      await page.evaluate(() =>
        (window as any).nextAutomaticSessionChallenge(),
      );
      await expect(input).toHaveValue("");
      await panel.getByRole("button", { name: "取消", exact: true }).click();
      await expect
        .poll(() =>
          page.evaluate(() => (window as any).automaticSessionCancels),
        )
        .toEqual([
          {
            accountId: "automatic-session-account",
            taskId: `automatic-${action}-2`,
          },
        ]);
      expect(
        await page.evaluate(() => (window as any).automaticSessionStarts),
      ).toEqual([]);
    });
  }
}
