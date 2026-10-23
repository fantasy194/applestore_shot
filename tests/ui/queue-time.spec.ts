import { expect, test, type Page } from "@playwright/test";
import { uiReviewFixture, noHorizontalOverflow } from "./ui-review-fixture";

const enqueued = Date.UTC(2026, 9, 23, 1, 2, 3, 123);
const queued = enqueued + 500;
const stockDetected = enqueued - 1500;
const started = enqueued + 10000;
const confirmed = started + 20000;
const legacyMessages = [
  "候选门店模式已关闭",
  "已切换候选门店模式",
  "下单方式已变更",
];

async function fixture(page: Page) {
  await uiReviewFixture(page, true);
  await page.addInitScript(
    ({
      enqueued,
      queued,
      stockDetected,
      started,
      confirmed,
      legacyMessages,
    }) => {
      const root = window as any;
      const bridge = root.__DUO_KILLER_TEST__;
      const base = {
        sequence: 1,
        enqueued_at_ms: enqueued,
        account_id: "review-a",
        sku: "MJT84CH",
        quantity: 1,
        concurrency: 1,
        retry_count: 0,
        store_id: "R001",
        store_name: "Apple 测试门店",
        status: "queued",
      };
      const jobs = [
        {
          ...base,
          id: "all-times",
          status: "succeeded",
          queued_at_ms: queued,
          stock_detected_at_ms: stockDetected,
          checked_at_ms: enqueued + 3000,
          started_at_ms: started,
          order_confirmed_at_ms: confirmed,
        },
        {
          ...base,
          id: "legacy-no-start",
          status: "succeeded",
          checked_at_ms: enqueued + 3000,
        },
        {
          ...base,
          id: "invalid-time",
          enqueued_at_ms: "invalid",
          started_at_ms: "invalid",
          order_confirmed_at_ms: "invalid",
        },
        ...legacyMessages.map((message, i) => ({
          ...base,
          id: `mode-${i}`,
          status: "waiting_stock",
          message,
          account_id: i === 1 ? "review-b" : "review-a",
          ...(i !== 1
            ? {
                pickup_policy: {
                  selection_mode: "priority",
                  stores: [{ store_id: "R001", store_name: "Apple 测试门店" }],
                },
              }
            : {}),
        })),
        {
          ...base,
          id: "removed-account",
          status: "waiting_stock",
          message: legacyMessages[0],
          account_id: "synthetic-removed-account",
        },
      ];
      root.queueTimeJobs = jobs;
      root.queueTimeCalls = [];
      root.queueTimeSaves = [];
      const originalSave = bridge.savePreset;
      bridge.savePreset = async (...args: any[]) => {
        root.queueTimeSaves.push(args);
        return originalSave(...args);
      };
      bridge.queue = async (operation: string, params: any = {}) => {
        root.queueTimeCalls.push({ operation, params });
        return structuredClone({
          mode: "sequential",
          max_concurrency: 1,
          paused: false,
          child_capacity: 10,
          reserved_child_slots: 0,
          queued_count: 5,
          running_count: 0,
          blocked_accounts: [],
          jobs,
        });
      };
    },
    { enqueued, queued, stockDetected, started, confirmed, legacyMessages },
  );
}

for (const width of [1180, 390, 320]) {
  test(`${width}px 队列直接显示真实时间，旧记录不伪造下单时间`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 844 });
    await fixture(page);
    await page.goto("/#/queue", { waitUntil: "domcontentloaded" });
    await expect(page.locator(".queue-job")).toHaveCount(7);
    const complete = page.locator('[data-job-id="all-times"]');
    const times = complete.locator(".queue-job-times");
    for (const [kind, label, value] of [
      ["enqueued", "入队", queued],
      ["stock-detected", "发现有货", stockDetected],
      ["started", "开始下单", started],
      ["order-confirmed", "收到订单号", confirmed],
    ] as const) {
      const time = times.locator(`time[data-kind="${kind}"]`);
      await expect(time).toBeVisible();
      await expect(times).toContainText(label);
      await expect(time).toHaveAttribute(
        "datetime",
        new Date(value).toISOString(),
      );
      const expected = await page.evaluate((value) => {
        const date = new Date(value);
        const pad = (n: number, length = 2) => String(n).padStart(length, "0");
        return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}.${pad(date.getMilliseconds(), 3)}`;
      }, value);
      await expect(time).toContainText(expected);
      expect(await time.getAttribute("title")).toContain("2026");
      expect(await time.getAttribute("aria-label")).toContain("2026");
      expect(
        await time.evaluate((element) => !!element.closest("details")),
      ).toBe(false);
    }
    const clock = times.locator('time[data-kind="enqueued"]');
    await expect(clock.locator(".queue-time-fraction")).toHaveText(".623");
    const detected = times.locator('time[data-kind="stock-detected"]');
    expect(
      await clock.evaluate((element) =>
        Array.from(
          element.closest(".queue-job-times")!.querySelectorAll("time"),
        ).map((time) => time.getAttribute("data-kind")),
      ),
    ).toEqual(["enqueued", "stock-detected", "started", "order-confirmed"]);
    const typography = await clock.evaluate((element) => {
      const size = (selector: string) =>
        parseFloat(getComputedStyle(element.querySelector(selector)!).fontSize);
      return {
        clock: size("strong"),
        fraction: size(".queue-time-fraction"),
        context: size(".queue-time-context"),
      };
    });
    expect(typography.clock - typography.fraction).toBeGreaterThanOrEqual(4);
    expect(typography.clock - typography.context).toBeGreaterThanOrEqual(4);
    const positions = await page.locator(".queue-job").evaluateAll((jobs) =>
      jobs.map((job) => {
        const time = job
          .querySelector(".queue-job-times")!
          .getBoundingClientRect();
        const product = job
          .querySelector(".queue-job-heading")!
          .getBoundingClientRect();
        return {
          timeLeft: time.left,
          timeRight: time.right,
          timeBottom: time.bottom,
          productLeft: product.left,
          productTop: product.top,
        };
      }),
    );
    for (const position of positions) {
      if (width > 800) {
        expect(position.timeRight).toBeLessThanOrEqual(position.productLeft);
        expect(
          Math.abs(position.timeLeft - positions[0].timeLeft),
        ).toBeLessThanOrEqual(1);
      } else {
        expect(position.timeBottom).toBeLessThanOrEqual(position.productTop);
      }
    }
    const legacy = page.locator('[data-job-id="legacy-no-start"]');
    await expect(
      legacy.locator('.queue-job-times time[data-kind="enqueued"]'),
    ).toBeVisible();
    await expect(legacy.locator('time[data-kind="enqueued"]')).toHaveAttribute(
      "datetime",
      new Date(enqueued).toISOString(),
    );
    await expect(legacy.locator(".queue-job-times")).toContainText(
      "发现有货时间未记录",
    );
    await expect(
      legacy.locator('time[data-kind="stock-detected"]'),
    ).toHaveCount(0);
    await expect(
      legacy.locator(
        'time[data-kind="started"], time[data-kind="order-confirmed"]',
      ),
    ).toHaveCount(0);
    const previousPolls = await page.evaluate(
      () => (window as any).queueTimeCalls.length,
    );
    await page.evaluate(() => {
      const root = window as any;
      for (const job of root.queueTimeJobs)
        job.checked_at_ms = Date.UTC(2030, 0, 1);
    });
    await expect
      .poll(() => page.evaluate(() => (window as any).queueTimeCalls.length))
      .toBeGreaterThan(previousPolls);
    await expect(detected).toHaveAttribute(
      "datetime",
      new Date(stockDetected).toISOString(),
    );
    await expect(legacy.locator(".queue-job-times")).toContainText(
      "发现有货时间未记录",
    );
    const invalid = page.locator('[data-job-id="invalid-time"]');
    await expect(invalid.locator(".queue-job-times")).toContainText("未记录");
    await expect(invalid).not.toContainText("Invalid Date");
    for (let i = 0; i < legacyMessages.length; i++) {
      const job = page.locator(`[data-job-id="mode-${i}"]`);
      await expect(job.locator(".status")).toHaveText("等待恢复配置");
      const reason = job.locator(".queue-job-step");
      await expect(reason).toContainText("当前配置与入队时");
      await expect(reason).toContainText(i === 1 ? "固定单店" : "预选多店");
      await expect(reason).toContainText("恢复该模式和自动抢购授权");
      await expect(reason).toContainText("取消此任务");
      await expect(
        job.getByRole("button", { name: "查看自提配置", exact: true }),
      ).toBeVisible();
    }
    const removed = page.locator('[data-job-id="removed-account"]');
    await expect(removed).toContainText(/原账号.*移除|账号.*不存在/);
    await expect(
      removed.getByRole("button", { name: "查看自提配置", exact: true }),
    ).toHaveCount(0);
    await noHorizontalOverflow(page);
    await complete.screenshot({
      path: `artifacts/queue-time/${width}-time-row.png`,
    });
    await legacy.screenshot({
      path: `artifacts/queue-time/${width}-time-row-legacy.png`,
    });
    await page.screenshot({
      path: `artifacts/queue-time/${width}-queue.png`,
      fullPage: true,
    });
  });
}

test("查看自提配置定位原账号，切换账号不保存、不下单、不恢复队列", async ({
  page,
}) => {
  await fixture(page);
  await page.goto("/#/queue", { waitUntil: "domcontentloaded" });
  await expect(page.locator(".queue-job")).toHaveCount(7);
  for (const [jobId, accountName, accountId] of [
    ["mode-1", "备用账号", "review-b"],
    ["mode-0", "日常账号", "review-a"],
  ]) {
    await page
      .locator(`[data-job-id="${jobId}"]`)
      .getByRole("button", { name: "查看自提配置", exact: true })
      .click();
    await expect(
      page.getByRole("heading", { name: "本地配置", exact: true }),
    ).toBeVisible();
    await expect(
      page.getByRole("combobox", { name: "配置范围", exact: true }),
    ).toContainText(accountName);
    await expect
      .poll(() =>
        page.evaluate(
          (id) =>
            (window as any).uiReviewCalls.some(
              (call: any) =>
                call.accountId === id && call.action === "address_read",
            ),
          accountId,
        ),
      )
      .toBe(true);
    await page.goto("/#/queue", { waitUntil: "domcontentloaded" });
    await expect(page.locator(".queue-job")).toHaveCount(7);
  }
  expect(
    await page.evaluate(() =>
      (window as any).queueTimeCalls.filter(
        (call: any) => call.operation !== "status",
      ),
    ),
  ).toEqual([]);
  expect(await page.evaluate(() => (window as any).queueTimeSaves)).toEqual([]);
  expect(
    await page.evaluate(() =>
      (window as any).uiReviewCalls.filter(
        (call: any) =>
          !["address_read", "inventory_settings_read"].includes(call.action),
      ),
    ),
  ).toEqual([]);
});
