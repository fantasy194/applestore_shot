import { test, expect } from "@playwright/test";

test("历史仅用摘要，当前结果和展开的任务日志按需读取", async ({ page }) => {
  await page.addInitScript(() => {
    const root = window as any;
    const account = { id: "a", name: "测试账号", email: "owner@example.test" };
    let revision = 1;
    const current = () => ({
      id: "visible",
      account_id: "a",
      action: "purchase",
      status: "succeeded",
      stage: "已完成可用准备，等待库存",
      created_at: 2000,
      updated_at: 2000,
      prompt: null,
      order_submitted: false,
      logs: [],
      log_count: 2,
      is_summary: true,
      detail_revision: `r${revision}`,
      result: {
        success: true,
        status: "waiting_stock",
        preparation_stage: "fulfillment",
        message: "已完成可用准备，等待库存",
      },
    });
    const history = Array.from({ length: 200 }, (_, index) => ({
      ...current(),
      id: `history-${index}`,
      created_at: index,
      action: "address_read",
      stage: "历史操作",
      result: null,
    }));
    let listener = (_task: any) => {};
    root.detailCalls = [];
    root.detailInFlight = 0;
    root.detailMaximum = 0;
    root.changeDetail = () => {
      revision++;
      listener(current());
    };
    root.__DUO_KILLER_TEST__ = {
      snapshot: async () => ({
        accounts: [account],
        tasks: [current(), ...history],
        data_dir: "/fixture",
      }),
      subscribe: async (callback: any) => {
        listener = callback;
        return () => {};
      },
      task: async (accountId: string, taskId: string, options: any) => {
        root.detailCalls.push({ accountId, taskId, options });
        root.detailInFlight++;
        root.detailMaximum = Math.max(root.detailMaximum, root.detailInFlight);
        await new Promise((resolve) => setTimeout(resolve, 100));
        root.detailInFlight--;
        return {
          ...current(),
          is_summary: false,
          logs_included: options.includeLogs,
          logs: options.includeLogs
            ? [`仅此任务日志 ${revision}`, "不会下载历史任务日志"]
            : [],
          result: {
            ...current().result,
            order_ready: false,
            review_verified: false,
          },
        };
      },
      add: async () => account,
      remove: async () => {},
      savePreset: async () => account,
      start: async () => "unused",
      cancel: async () => {},
      reply: async () => {},
    };
  });
  await page.goto("/", { waitUntil: "domcontentloaded" });
  const current = page.getByRole("region", { name: "当前任务", exact: true });
  await expect(current).toContainText("已预备，等待库存");
  await expect
    .poll(() => page.evaluate(() => (window as any).detailCalls.length))
    .toBe(1);
  expect(await page.evaluate(() => (window as any).detailCalls[0])).toEqual({
    accountId: "a",
    taskId: "visible",
    options: { includeLogs: false },
  });
  await expect(current.locator("pre")).toHaveCount(0);
  await current.getByText("当前任务日志（2 条）", { exact: true }).click();
  await expect(current.locator("pre")).toContainText("仅此任务日志 1");
  await page.evaluate(() => (window as any).changeDetail());
  await expect(current.locator("pre")).toContainText("仅此任务日志 2");
  await current.getByText("当前任务日志（2 条）", { exact: true }).click();
  await page.evaluate(() => (window as any).changeDetail());
  await expect
    .poll(() =>
      page.evaluate(
        () => (window as any).detailCalls.at(-1).options.includeLogs,
      ),
    )
    .toBe(false);
  expect(
    await page.evaluate(() =>
      (window as any).detailCalls.every(
        (call: any) => call.taskId === "visible",
      ),
    ),
  ).toBe(true);
  expect(await page.evaluate(() => (window as any).detailMaximum)).toBe(1);
});
