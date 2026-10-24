import { test, expect, type Page } from "@playwright/test";

async function fixture(
  page: Page,
  options: { performance?: any; nonFinite?: boolean; waiting?: boolean } = {},
) {
  await page.addInitScript(({ performance, nonFinite, waiting }) => {
    const root = window as any;
    const account = {
      id: "timing-fixture-account",
      name: "计时测试账号",
      email: "timing@example.test",
      pending: false,
      preset_sku: "MLH74FE",
      preset_quantity: 1,
    };
    if (nonFinite)
      performance = {
        durations_ms: {
          handoff_to_submit: NaN,
          execution_to_submit: Infinity,
          execution_to_order_confirmed: -Infinity,
          revalidation: -1,
        },
      };
    const task = {
      id: "timing-fixture-task",
      account_id: account.id,
      action: "purchase",
      status: waiting ? "waiting" : "failed",
      created_at: 1000,
      updated_at: 1000,
      logs: [],
      stage: waiting ? "保留原自提准备" : "离线流程已结束",
      order_submitted: false,
      prompt: waiting
        ? {
            id: "timing-prepared-prompt",
            kind: "prepared_purchase",
            label: "原自提准备",
            prepared: {
              v: 1,
              sku: "MLH74FE",
              quantity: 1,
              fulfillment: "RETAIL",
              config_fingerprint: "a".repeat(64),
              config_file_sha256: "b".repeat(64),
              authorized_store_ids: ["R001"],
              prepared_at_ms: Date.now() - 3600000,
              expires_at_ms: Date.now() + 300000,
              preparation_stage: "review",
              review_verified: true,
              order_ready: true,
            },
          }
        : null,
      result: {
        success: waiting === true,
        message: "合成计时样本",
        ...(performance === undefined ? {} : { performance }),
      },
    };
    root.timingRequests = [];
    root.__DUO_KILLER_TEST__ = {
      snapshot: async () => ({
        accounts: [account],
        tasks: [structuredClone(task)],
        data_dir: "/synthetic-timing-fixture",
      }),
      subscribe: async () => () => {},
      start: async (...args: any[]) => {
        root.timingRequests.push(args);
        throw new Error("Timing view must not start a purchase");
      },
      reply: async () => {
        throw new Error("Timing view must not reply to prompts");
      },
      cancel: async () => {},
      add: async () => account,
      remove: async () => {},
      savePreset: async () => {},
    };
  }, options);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await expect(
    page.getByRole("region", { name: "当前任务", exact: true }),
  ).toBeVisible();
}

const measured = {
  prepared_at_ms: 100,
  claimed_at_ms: 3600100,
  durations_ms: {
    handoff_to_submit: 1250,
    execution_to_submit: 2525,
    execution_to_order_confirmed: 0,
    revalidation: 50,
    prepared_wait: 3600000,
    unrelated_phase: 9999999,
  },
};

test("下单计时只展示实际支持的非负测量值，明确测得0才显示0秒", async ({
  page,
}) => {
  await fixture(page, { performance: measured });
  const timing = page.locator("details.task-logs");
  await expect(timing).toHaveCount(1);
  await expect(timing).not.toHaveAttribute("open", "");
  await timing.getByText("本次下单耗时", { exact: true }).click();
  await expect(timing.locator("dt")).toHaveText([
    "准备接手至提交",
    "开始执行至提交",
    "开始执行至订单确认",
    "结账复核",
  ]);
  await expect(timing.locator("dd")).toHaveText([
    "1.25 秒",
    "2.52 秒",
    "0.00 秒",
    "0.05 秒",
  ]);
  await expect(timing).toContainText(
    "不包含此前的预热等待；提交请求与官网确认分别计时。",
  );
  await expect(timing).not.toContainText("3600");
  await expect(timing).not.toContainText("9999");
  expect(await page.evaluate(() => (window as any).timingRequests)).toEqual([]);
});

for (const [name, performance] of [
  ["缺少整个performance", undefined],
  [
    "只有时间戳",
    { prepared_at_ms: 100, claimed_at_ms: 3600100, submit_at_ms: 3600200 },
  ],
  ["空测量对象", { durations_ms: {} }],
  ["无效测量容器", { durations_ms: "0" }],
  [
    "缺失和负数及字符串",
    {
      durations_ms: {
        handoff_to_submit: "0",
        execution_to_submit: -1,
        execution_to_order_confirmed: null,
        revalidation: false,
      },
    },
  ],
  ["只有此前准备等待", { durations_ms: { prepared_wait: 3600000 } }],
] as const) {
  test(`${name}不生成虚构的0秒或下单耗时面板`, async ({ page }) => {
    await fixture(page, { performance });
    await expect(page.getByText("本次下单耗时", { exact: true })).toHaveCount(
      0,
    );
    await expect(
      page.getByRole("region", { name: "当前任务", exact: true }),
    ).not.toContainText("0.00 秒");
  });
}

test("NaN和正负Infinity均不当作有效测量", async ({ page }) => {
  await fixture(page, { nonFinite: true });
  await expect(page.getByText("本次下单耗时", { exact: true })).toHaveCount(0);
});

test("有效和无效测量混合时只显示有效字段，不给缺失字段补0", async ({
  page,
}) => {
  await fixture(page, {
    performance: {
      durations_ms: {
        handoff_to_submit: 1250,
        execution_to_submit: -10,
        execution_to_order_confirmed: "50",
      },
    },
  });
  const timing = page.locator("details.task-logs");
  await timing.getByText("本次下单耗时", { exact: true }).click();
  await expect(timing.locator("dt")).toHaveText(["准备接手至提交"]);
  await expect(timing.locator("dd")).toHaveText(["1.25 秒"]);
  await expect(timing).not.toContainText("0.00 秒");
});

test("等待中的准备时间不会从准备时间戳或prepared_wait补算成下单耗时", async ({
  page,
}) => {
  await fixture(page, {
    waiting: true,
    performance: {
      prepared_at_ms: Date.now() - 3600000,
      durations_ms: { prepared_wait: 3600000 },
    },
  });
  await expect(
    page.getByRole("region", { name: "自提准备状态", exact: true }),
  ).toContainText("自提准备就绪");
  await expect(page.getByText("本次下单耗时", { exact: true })).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "立即下单（自提）", exact: true }),
  ).toBeEnabled();
});

for (const width of [320, 390]) {
  test(`${width}px 下单耗时展开可读且无横向溢出`, async ({ page }) => {
    await page.setViewportSize({ width, height: 844 });
    await fixture(page, { performance: measured });
    const timing = page.locator("details.task-logs");
    await timing.getByText("本次下单耗时", { exact: true }).click();
    await expect(timing.locator("dd")).toHaveCount(4);
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth),
    ).toBeLessThanOrEqual(width);
    await timing.screenshot({
      path: `artifacts/goal-0113-purchase-timing-${width}.png`,
    });
  });
}
