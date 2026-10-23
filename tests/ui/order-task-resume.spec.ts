import { expect, test, type Page } from "@playwright/test";

async function fixture(page: Page, pending: boolean, stage: string) {
  await page.route("**/api/**", (route) =>
    route.fulfill({ json: { authenticated: true, success: true } }),
  );
  await page.addInitScript(
    ({ pending, stage }) => {
      const root = window as any;
      const accounts = [
        { id: "a", name: "日常账号", email: "first@example.test" },
        { id: "b", name: "备用账号", email: "second@example.test" },
      ];
      const task = {
        id: "saved-order-b",
        account_id: "b",
        action: "purchase",
        status: pending ? "succeeded" : "failed",
        stage,
        created_at: Date.now() / 1000 - 20,
        updated_at: Date.now() / 1000,
        prompt: null,
        logs: [],
        order_submitted: pending,
        result: pending
          ? {
              status: "pending_payment",
              order_number: "W000000002",
              amount_due: "RMB 8,999",
            }
          : null,
      };
      root.resumeStarted = [];
      root.__DUO_KILLER_TEST__ = {
        snapshot: async () =>
          structuredClone({
            accounts,
            tasks: [task],
            data_dir: "/synthetic-saved-order",
          }),
        task: async () => structuredClone(task),
        subscribe: async () => () => {},
        start: async (account: string, action: string) => {
          root.resumeStarted.push({ account, action });
          return "unexpected-start";
        },
        maintenance: async () => ({ enabled: false, last_runs: [] }),
        browser: async () => ({ active: false }),
        queue: async () => ({ paused: true, jobs: [] }),
        logs: async () => ({ entries: [], has_more: false }),
      };
    },
    { pending, stage },
  );
  await page.goto("/#/duo", { waitUntil: "domcontentloaded" });
}

for (const pending of [true, false]) {
  test(`two-account ${pending ? "pending payment" : "failed"} task opens read-only without login or another purchase`, async ({
    page,
  }) => {
    await fixture(page, pending, pending ? "订单已创建" : "search");
    const progress = page.getByRole("region", {
      name: "下单进度",
      exact: true,
    });
    await progress
      .getByRole("button", { name: "查看当前任务", exact: true })
      .click();
    const detail = page.getByRole("region", { name: "当前任务", exact: true });
    await expect(detail).toBeVisible();
    await expect(
      page.getByRole("heading", { name: "备用账号", exact: true }),
    ).toBeVisible();
    await expect(detail).toHaveAttribute("id", "task-saved-order-b");
    await expect(
      page.getByText("正在查看已保存的下单任务。", { exact: true }),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: "立即下单", exact: true }),
    ).toBeHidden();
    await expect(
      page.getByRole("button", { name: "登录账号", exact: true }),
    ).toBeHidden();
    await expect(detail).not.toContainText("first@example.test");
    if (pending) {
      await expect(detail).toContainText("W000000002");
      await expect(detail).toContainText("待付款");
    } else {
      await expect(detail).toContainText("查询自提门店");
      await expect(detail).not.toContainText("search");
    }
    expect(await page.evaluate(() => (window as any).resumeStarted)).toEqual(
      [],
    );
    await page.getByRole("button", { name: "选择账号", exact: true }).click();
    await expect(
      page.getByText("正在查看已保存的下单任务。", { exact: true }),
    ).toHaveCount(0);
    await expect(
      page.getByRole("region", { name: "当前任务", exact: true }),
    ).toHaveCount(0);
    expect(await page.evaluate(() => (window as any).resumeStarted)).toEqual(
      [],
    );
  });
}

test("unknown purchase stage has a readable fallback without changing the stored stage", async ({
  page,
}) => {
  await fixture(page, false, "undocumented_protocol_step");
  await page
    .getByRole("region", { name: "下单进度", exact: true })
    .getByRole("button", { name: "查看当前任务", exact: true })
    .click();
  const detail = page.getByRole("region", { name: "当前任务", exact: true });
  await expect(detail).toContainText("正在处理下单");
  await expect(detail).not.toContainText("undocumented_protocol_step");
  expect(
    await page.evaluate(
      async () =>
        (await (window as any).__DUO_KILLER_TEST__.snapshot()).tasks[0].stage,
    ),
  ).toBe("undocumented_protocol_step");
  expect(await page.evaluate(() => (window as any).resumeStarted)).toEqual([]);
});

test("a complete English purchase failure remains readable", async ({
  page,
}) => {
  await fixture(page, false, "HTTP 541: service unavailable");
  await page
    .getByRole("region", { name: "下单进度", exact: true })
    .getByRole("button", { name: "查看当前任务", exact: true })
    .click();
  const detail = page.getByRole("region", { name: "当前任务", exact: true });
  await expect(detail).toContainText("HTTP 541: service unavailable");
  expect(await page.evaluate(() => (window as any).resumeStarted)).toEqual([]);
});
