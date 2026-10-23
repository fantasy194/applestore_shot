import { test, expect, type Page } from "@playwright/test";
import { chooseOption } from "./select-option";

test("配置轮询只读取当前任务结果，不传输整个工作台与历史日志", async ({
  page,
}) => {
  await page.goto("/", { waitUntil: "domcontentloaded" });
  const result = await page.evaluate(async () => {
    const modulePath = "/src/inventory-api.ts";
    const { scopedTaskCall } = await import(modulePath);
    const calls: any[] = [];
    let polls = 0;
    const bridge = {
      start: async () => "scoped-task",
      snapshot: async () => {
        throw new Error("不应读取完整工作台");
      },
      task: async (accountId: string, taskId: string, options: any) => {
        calls.push({ accountId, taskId, options });
        return ++polls === 1
          ? { status: "running" }
          : { status: "succeeded", result: { config: { targets: [] } } };
      },
    };
    return {
      value: await scopedTaskCall(bridge, "local", "inventory_settings_read"),
      calls,
    };
  });
  expect(result.value).toEqual({ config: { targets: [] } });
  expect(result.calls).toHaveLength(2);
  expect(
    result.calls.every(
      (call: any) =>
        call.taskId === "scoped-task" && call.options.includeLogs === false,
    ),
  ).toBe(true);
});

async function fixture(page: Page) {
  await page.addInitScript(() => {
    const root = window as any;
    const accounts = [
      {
        id: "a",
        name: "测试账号",
        email: "owner@example.test",
        preset_sku: "MLH74FE",
        preset_quantity: 1,
      },
    ];
    const targets = [
      {
        id: "t1",
        sku: "MLH74FE",
        quantity: 1,
        store_id: "R001",
        store_name: "Apple 测试一店",
        location: "201101",
        account_id: "a",
        auto_purchase: true,
        concurrency: 3,
        retry_count: 2,
      },
      {
        id: "t2",
        sku: "MLH74FE",
        quantity: 1,
        store_id: "R002",
        store_name: "Apple 测试二店",
        location: "201101",
        account_id: "",
        auto_purchase: false,
      },
    ];
    let config = {
      interval_seconds: 1,
      targets,
      proxy: { enabled: false, pool_size: 10, providers: [] },
    };
    const tasks: any[] = [];
    let update = (_task: any) => {};
    let counter = 0;
    root.started = [];
    root.cancelled = [];
    root.stockEvents = [];
    root.historyCalls = [];
    root.historyInFlight = 0;
    root.historyMaxInFlight = 0;
    root.reportPurchase = (changes: any) => {
      const task = tasks.find((row) => row.action === "purchase");
      Object.assign(task, changes);
      update(structuredClone(task));
    };
    root.reportInventory = (result: any) => {
      const task = tasks.find(
        (row) => row.action === "inventory_monitor" && row.status === "running",
      );
      task.result = result;
      update(structuredClone(task));
    };
    root.__DUO_KILLER_TEST__ = {
      logs: async (params: any) => {
        root.historyCalls.push(params);
        root.historyInFlight++;
        root.historyMaxInFlight = Math.max(
          root.historyMaxInFlight,
          root.historyInFlight,
        );
        try {
          if (root.deferHistory)
            await new Promise((resolve) => {
              root.finishHistory = resolve;
            });
          if (root.historyFails)
            throw new Error("private diagnostics must stay hidden");
          return { entries: structuredClone(root.stockEvents) };
        } finally {
          root.historyInFlight--;
        }
      },
      snapshot: async () =>
        structuredClone({ accounts, tasks, data_dir: "/synthetic" }),
      subscribe: async (callback: any) => {
        update = callback;
        return () => {};
      },
      add: async () => accounts[0],
      remove: async () => {},
      savePreset: async () => accounts[0],
      reply: async () => {},
      cancel: async (_accountId: string, id: string) => {
        root.cancelled.push(id);
        const task = tasks.find((row) => row.id === id);
        Object.assign(task, { status: "cancelled", stage: "监控已停止" });
        update(structuredClone(task));
      },
      start: async (accountId: string, action: string, params: any) => {
        const id = `fixture-${++counter}`;
        const task: any = {
          id,
          account_id: accountId,
          action,
          status: "succeeded",
          stage: "已完成",
          created_at: Date.now() / 1000,
          updated_at: Date.now() / 1000,
          prompt: null,
          result: {},
          logs: [],
          order_submitted: false,
        };
        root.started.push({ id, accountId, action, params });
        if (action === "inventory_settings_save")
          config = structuredClone(params.config);
        if (
          ["inventory_settings_read", "inventory_settings_save"].includes(
            action,
          )
        )
          task.result = { config: structuredClone(config) };
        if (action === "store_search")
          task.result = {
            stores: [
              {
                store_id: "R003",
                store_name: "Apple 测试三店",
                pickup_message: "今日可取货",
              },
            ],
          };
        if (["inventory_check", "inventory_monitor"].includes(action))
          task.result = {
            monitoring: action === "inventory_monitor",
            checked_at: new Date().toISOString(),
            targets: config.targets.map((target, index) => ({
              ...target,
              status: index === 0 ? "available" : "unknown",
              checked_at: new Date().toISOString(),
              ...(index === 1
                ? { error: "官网响应未包含目标门店，库存未知" }
                : { pickup_message: "今日可取货" }),
            })),
            proxy: { dispatchable_count: 0 },
          };
        if (action === "inventory_monitor") {
          task.status = "running";
          task.stage = "每秒检查门店库存";
        }
        if (action === "address_read")
          task.result = {
            config: {
              pickup: {
                search: "201101",
                store_name: "Apple 原门店",
                store_id: "R000",
                id_last4: "1234",
              },
              contact: {
                lastName: "测",
                firstName: "试",
                emailAddress: "owner@example.test",
                fullDaytimePhone: "13800000000",
              },
            },
          };
        if (action === "address_save") task.result = { config: params };
        if (action === "purchase" && params.scheduled_at) {
          task.status = "scheduled";
          task.schedule = { at_ms: params.scheduled_at, ...params };
        } else if (action === "purchase")
          task.result = {
            success: true,
            status: "parallel_complete",
            concurrency: params.concurrency,
            attempts: [
              {
                attempt: 1,
                task_id: "00000000-0000-4000-8000-000000000001",
                status: "pending_payment",
                result: {
                  success: true,
                  status: "pending_payment",
                  order_number: "W12345678",
                  amount_due: "RMB 69",
                  pickup_store: "Apple 测试一店",
                },
              },
              {
                attempt: 2,
                task_id: "00000000-0000-4000-8000-000000000002",
                status: "submission_unknown",
                error: "提交结果未知，请查询订单",
                order_submitted: true,
              },
            ].slice(0, params.concurrency || 1),
          };
        tasks.unshift(task);
        update(structuredClone(task));
        return id;
      },
    };
  });
}

test("监控概览保留真实历史，历史有货不计入当前库存", async ({ page }) => {
  await fixture(page);
  await page.addInitScript(() => {
    (window as any).stockEvents = [
      {
        id: 1,
        category: "stock",
        event: "inventory_available",
        details: {
          sku: "MLH74FE",
          store_id: "R099",
          store_name: "Apple 历史门店",
          checked_at_ms: Date.now() - 86400000,
        },
      },
    ];
  });
  await page.goto("/#/monitor");
  const history = page.getByRole("region", { name: "最近有货记录" });
  await expect(history.getByText("Apple 历史门店")).toBeVisible();
  await expect(history).toContainText("MLH74FE · R099");
  await expect(history.locator("time")).toHaveAttribute("datetime", /T/);
  await expect(history).toContainText("不代表当前库存");
  await expect(
    page
      .locator(".ui-metric")
      .filter({ hasText: "当前有货" })
      .locator("strong"),
  ).toHaveText("0");
  await expect(page.getByLabel("抢购账号").first()).not.toBeVisible();
  await expect(page.getByLabel("商品 SKU", { exact: true })).not.toBeVisible();
  expect(await page.evaluate(() => (window as any).historyCalls[0])).toEqual({
    category: "stock",
    limit: 12,
  });
  await page.setViewportSize({ width: 1440, height: 980 });
  await page.screenshot({
    path: "artifacts/monitor-history-desktop.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth),
  ).toBeLessThanOrEqual(390);
  await page.screenshot({
    path: "artifacts/monitor-history-mobile.png",
    fullPage: true,
  });
});

test("有货历史读取无重叠，失败提示不泄露诊断并能恢复", async ({ page }) => {
  await page.clock.install();
  await fixture(page);
  await page.addInitScript(() => {
    (window as any).deferHistory = true;
  });
  await page.goto("/#/monitor");
  await expect
    .poll(() => page.evaluate(() => (window as any).historyCalls.length))
    .toBe(1);
  await page.clock.fastForward(5200);
  expect(await page.evaluate(() => (window as any).historyCalls.length)).toBe(
    1,
  );
  await page.evaluate(() => {
    const root = window as any;
    root.historyFails = true;
    root.deferHistory = false;
    root.finishHistory();
  });
  await expect(
    page.getByText("暂时无法读取有货记录，稍后重试。"),
  ).toBeVisible();
  await expect(page.getByText("暂无有货记录")).toBeVisible();
  await expect(
    page.getByText("private diagnostics must stay hidden"),
  ).toHaveCount(0);
  await page.evaluate(() => {
    (window as any).historyFails = false;
  });
  await page.clock.fastForward(5001);
  await expect(page.getByText("暂时无法读取有货记录，稍后重试。")).toHaveCount(
    0,
    { timeout: 7000 },
  );
  expect(await page.evaluate(() => (window as any).historyMaxInFlight)).toBe(1);
});

test("多门店监控区分有货与未知，并保留停止入口", async ({ page }) => {
  await fixture(page);
  await page.goto("/#/monitor", { waitUntil: "domcontentloaded" });
  await expect(
    page.getByRole("heading", { name: "自动监控", exact: true }),
  ).toBeVisible();
  await expect(page.locator(".inventory-target")).toHaveCount(2);
  await expect(page.locator(".stock-status.unknown")).toHaveCount(2);
  await page.getByRole("button", { name: "保存并开始监控" }).click();
  await expect(
    page.getByRole("status", { name: "" }).filter({ hasText: "监控运行中" }),
  ).toBeVisible();
  await expect(page.locator(".stock-status.available")).toHaveText("有库存");
  await expect(page.locator(".stock-status.unknown")).toHaveCount(1);
  await expect(page.locator(".inventory-target").nth(1)).toContainText(
    "官网响应未包含目标门店，库存未知",
  );
  await expect(
    page.getByRole("button", { name: "添加代理供应商" }),
  ).not.toBeVisible();
  await page.getByRole("button", { name: "停止监控", exact: true }).click();
  await expect(page.locator(".inventory-run-state")).toHaveText("监控已停止");
  expect(await page.evaluate(() => (window as any).cancelled.length)).toBe(1);
});

test("监控运行时仍可查询官网门店，查询不会修改目标或中断监控", async ({
  page,
}) => {
  await fixture(page);
  await page.goto("/#/monitor", { waitUntil: "domcontentloaded" });
  await page.getByRole("button", { name: "保存并开始监控" }).click();
  await expect(
    page.getByRole("button", { name: "停止监控", exact: true }),
  ).toBeVisible();
  const baseline = await page.evaluate(() => (window as any).started.length);
  await page.getByText("添加门店与商品", { exact: true }).click();
  const search = page.locator(".monitor-add-stores");
  await chooseOption(
    search.getByRole("combobox", { name: "监控商品", exact: true }),
    "自定义 SKU",
  );
  await search.getByLabel("商品 SKU", { exact: true }).fill("MQ9R3CH");
  await search.getByLabel("每路购买数量", { exact: true }).fill("2");
  await search.getByLabel("门店搜索城市 / 邮编").fill("上海");
  await search.getByRole("button", { name: "从官网读取门店" }).click();
  await expect(
    search.getByText("Apple 测试三店", { exact: true }),
  ).toBeVisible();
  await expect(
    search.getByRole("checkbox", { name: /Apple 测试三店/ }),
  ).toBeDisabled();
  await expect(
    search.getByRole("button", { name: "添加选中的 0 家门店" }),
  ).toBeDisabled();
  await expect(search).toContainText("查询不会修改监控目标");
  await expect(
    search.getByRole("link", { name: "自提门店与时段偏好" }),
  ).toHaveAttribute("href", "#/settings");
  await expect(page.locator(".inventory-target")).toHaveCount(2);
  await expect(
    page.getByRole("button", { name: "停止监控", exact: true }),
  ).toBeVisible();
  expect(
    await page.evaluate(
      (offset) => (window as any).started.slice(offset),
      baseline,
    ),
  ).toEqual([
    expect.objectContaining({
      action: "store_search",
      params: { sku: "MQ9R3CH", location: "上海" },
    }),
  ]);
  expect(await page.evaluate(() => (window as any).cancelled)).toEqual([]);
});

test("读取官网门店后加入目标并保存账号与并发配置", async ({ page }) => {
  await fixture(page);
  await page.goto("/#/monitor", { waitUntil: "domcontentloaded" });
  await expect(page.locator(".inventory-target")).toHaveCount(2);
  await page.getByText("添加门店与商品", { exact: true }).click();
  await page.getByLabel("门店搜索城市 / 邮编").fill("201101");
  await page.getByRole("button", { name: "从官网读取门店" }).click();
  await page.getByRole("checkbox", { name: /Apple 测试三店/ }).check();
  await page.getByRole("button", { name: "添加选中的 1 家门店" }).click();
  await expect(page.locator(".inventory-target")).toHaveCount(3);
  const target = page.locator(".inventory-target").last();
  for (const card of await page.locator(".inventory-target").all())
    await card.getByText("抢购设置", { exact: true }).click();
  await expect(
    page
      .locator(".inventory-target")
      .first()
      .getByRole("spinbutton", { name: "并发路数", exact: true }),
  ).toHaveValue("3");
  await expect(
    page
      .locator(".inventory-target")
      .nth(1)
      .getByRole("spinbutton", { name: "并发路数", exact: true }),
  ).toHaveValue("1");
  await expect(
    target.getByRole("spinbutton", { name: "并发路数", exact: true }),
  ).toHaveValue("1");
  await expect(
    target.getByRole("spinbutton", { name: "下单失败重试次数", exact: true }),
  ).toHaveValue("0");
  await expect(
    page
      .locator(".inventory-target")
      .nth(1)
      .getByRole("spinbutton", { name: "下单失败重试次数", exact: true }),
  ).toHaveValue("0");
  await expect(
    page
      .locator(".inventory-target")
      .first()
      .getByRole("spinbutton", { name: "下单失败重试次数", exact: true }),
  ).toHaveValue("2");
  await chooseOption(
    target.getByRole("combobox", { name: "抢购账号", exact: true }),
    "测试账号 · own**@example.test",
  );
  await target
    .getByRole("spinbutton", { name: "并发路数", exact: true })
    .fill("2");
  await target
    .getByRole("spinbutton", { name: "下单失败重试次数", exact: true })
    .fill("2");
  await target.getByRole("checkbox", { name: "有货立即自提抢购" }).check();
  await page.getByRole("button", { name: "保存配置", exact: true }).click();
  await expect(
    page.getByRole("status").filter({ hasText: "监控配置已保存" }),
  ).toBeVisible();
  const saved = await page.evaluate(
    () =>
      (window as any).started.findLast(
        (row: any) => row.action === "inventory_settings_save",
      ).params.config,
  );
  expect(saved.targets[2]).toMatchObject({
    store_id: "R003",
    store_name: "Apple 测试三店",
    account_id: "a",
    auto_purchase: true,
    concurrency: 2,
    retry_count: 2,
    quantity: 1,
  });
  expect(saved.targets[0].concurrency).toBe(3);
  expect(saved.targets[1].concurrency).toBe(1);
  expect(saved.targets[0].retry_count).toBe(2);
  expect(saved.targets[1].retry_count).toBe(0);
});

test("人工自提默认一路", async ({ page }) => {
  await fixture(page);
  await page.goto("/");
  await expect(
    page.getByRole("spinbutton", { name: "自提并发路数", exact: true }),
  ).toHaveValue("1");
  await expect(
    page.getByRole("spinbutton", { name: "下单失败重试次数", exact: true }),
  ).toHaveValue("0");
  await page
    .getByRole("button", { name: "立即下单（自提）", exact: true })
    .click();
  expect(
    await page.evaluate(
      () =>
        (window as any).started.find((row: any) => row.action === "purchase")
          .params.concurrency,
    ),
  ).toBe(1);
  expect(
    await page.evaluate(
      () =>
        (window as any).started.find((row: any) => row.action === "purchase")
          .params.retry_count,
    ),
  ).toBe(0);
});

test("自提定时默认一路", async ({ page }) => {
  await fixture(page);
  await page.goto("/");
  await chooseOption(
    page.getByRole("combobox", { name: "定时收货方式", exact: true }),
    "自提（本地预设门店）",
  );
  await page.getByRole("button", { name: "设定定时抢购" }).click();
  expect(
    await page.evaluate(
      () =>
        (window as any).started.find((row: any) => row.action === "purchase")
          .params.concurrency,
    ),
  ).toBe(1);
  expect(
    await page.evaluate(
      () =>
        (window as any).started.find((row: any) => row.action === "purchase")
          .params.retry_count,
    ),
  ).toBe(0);
});

test("人工自提可显式设置三路，展示各路结果并使用对应付款任务", async ({
  page,
}) => {
  await fixture(page);
  await page.goto("/");
  await expect(
    page.getByRole("spinbutton", { name: "自提并发路数", exact: true }),
  ).toHaveValue("1");
  await page
    .getByRole("spinbutton", { name: "自提并发路数", exact: true })
    .fill("3");
  await page
    .getByRole("button", { name: "立即下单（自提）", exact: true })
    .click();
  const purchase = await page.evaluate(() =>
    (window as any).started.find((row: any) => row.action === "purchase"),
  );
  expect(purchase.params).toMatchObject({
    sku: "MLH74FE",
    quantity: 1,
    fulfillment: "RETAIL",
    concurrency: 3,
  });
  await expect(
    page.getByRole("heading", { name: "第 1 路 · 待付款" }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "第 2 路 · 提交结果未知" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "获取二维码", exact: true }).click();
  const qr = await page.evaluate(() =>
    (window as any).started.find((row: any) => row.action === "qr"),
  );
  expect(qr.params.source_task).toBe("00000000-0000-4000-8000-000000000001");
});

test("自提定时保留配置路数，送货不附加并发", async ({ page }) => {
  await fixture(page);
  await page.goto("/");
  await page
    .getByRole("spinbutton", { name: "自提并发路数", exact: true })
    .fill("2");
  await chooseOption(
    page.getByRole("combobox", { name: "定时收货方式", exact: true }),
    "自提（本地预设门店）",
  );
  await page.getByRole("button", { name: "设定定时抢购" }).click();
  const purchase = await page.evaluate(() =>
    (window as any).started.find((row: any) => row.action === "purchase"),
  );
  expect(purchase.params).toMatchObject({
    fulfillment: "RETAIL",
    concurrency: 2,
  });
  expect(purchase.params.scheduled_at).toBeGreaterThan(Date.now());
});

for (const fulfillment of ["HOME", "RETAIL"] as const) {
  test(`手动${fulfillment}下单携带两次额外重试`, async ({ page }) => {
    await fixture(page);
    await page.goto("/");
    await page
      .getByRole("spinbutton", { name: "下单失败重试次数", exact: true })
      .fill("2");
    await page
      .getByRole("button", {
        name: fulfillment === "RETAIL" ? "立即下单（自提）" : "立即下单",
        exact: true,
      })
      .click();
    expect(
      await page.evaluate(
        () =>
          (window as any).started.find((row: any) => row.action === "purchase")
            .params,
      ),
    ).toMatchObject({ fulfillment, retry_count: 2 });
  });

  test(`定时${fulfillment}下单冻结两次额外重试`, async ({ page }) => {
    await fixture(page);
    await page.goto("/");
    await page
      .getByRole("spinbutton", { name: "下单失败重试次数", exact: true })
      .fill("2");
    await chooseOption(
      page.getByRole("combobox", { name: "定时收货方式", exact: true }),
      fulfillment === "RETAIL" ? "自提（本地预设门店）" : "送货（本地地址）",
    );
    await page.getByRole("button", { name: "设定定时抢购" }).click();
    expect(
      await page.evaluate(
        () =>
          (window as any).started.find((row: any) => row.action === "purchase")
            .params,
      ),
    ).toMatchObject({ fulfillment, retry_count: 2 });
    await page
      .getByRole("spinbutton", { name: "下单失败重试次数", exact: true })
      .fill("0");
    const schedule = page.locator("section").filter({
      has: page.getByRole("heading", { name: "定时抢购", exact: true }),
    });
    await expect(schedule).toContainText("失败额外重试 2 次，最多共 3 轮");
  });
}

test("自提预设从官网选择门店后保存准确门店标识", async ({ page }) => {
  await fixture(page);
  await page.goto("/#/settings");
  await page.getByRole("button", { name: "从官网读取门店" }).click();
  await page.getByRole("radio", { name: /Apple 测试三店/ }).check();
  await page.getByRole("button", { name: "保存自提预设" }).click();
  const saved = await page.evaluate(
    () =>
      (window as any).started.findLast(
        (row: any) => row.action === "address_save",
      ).params,
  );
  expect(saved.pickup).toMatchObject({
    search: "201101",
    store_id: "R003",
    store_name: "Apple 测试三店",
  });
});

test("窄屏监控页面保持页面宽度", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await fixture(page);
  await page.goto("/#/monitor");
  await expect(page.locator(".inventory-target")).toHaveCount(2);
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth),
  ).toBeLessThanOrEqual(390);
  await expect(
    page.getByRole("button", { name: "保存并开始监控" }),
  ).toBeVisible();
  await page.screenshot({
    path: "artifacts/inventory-mobile.png",
    fullPage: true,
  });
});

for (const [lastError, message] of [
  ["provider_expiry", "代理供应商返回的有效期无法识别"],
  ["provider_network", "暂时无法连接代理供应商"],
  [null, "正在补充库存代理"],
] as const) {
  test(`无库存样本时说明代理池原因：${lastError || "warming"}`, async ({
    page,
  }) => {
    await fixture(page);
    await page.goto("/#/monitor");
    await page.getByRole("button", { name: "保存并开始监控" }).click();
    await page.evaluate(
      (lastError) =>
        (window as any).reportInventory({
          monitoring: true,
          checked_at: new Date().toISOString(),
          targets: [
            {
              id: "t1",
              status: "unknown",
              checked_at: null,
              error: "proxy_pool_empty",
            },
          ],
          proxy: {
            enabled: true,
            dispatchable_count: 0,
            maintaining: true,
            last_error: lastError,
          },
        }),
      lastError,
    );
    const target = page.locator(".inventory-target").first();
    await expect(target.locator(".stock-status")).toHaveText("未知");
    await expect(target).toContainText(message);
    await expect(target).toContainText("尚未取得有效库存");
    await expect(target).toContainText("本次尝试：");
    await expect(target).not.toContainText("上次结果已过时");
    await expect(target).not.toContainText("proxy_pool_empty");
    await expect(target).not.toContainText("尚未检查");
    await expect(target).not.toContainText("暂无库存");
  });
}

test("只有真实旧库存样本才显示过时提示及采样时间", async ({ page }) => {
  await fixture(page);
  await page.goto("/#/monitor");
  await page.getByRole("button", { name: "保存并开始监控" }).click();
  await page.evaluate(() =>
    (window as any).reportInventory({
      monitoring: true,
      checked_at: new Date().toISOString(),
      targets: [
        {
          id: "t1",
          status: "available",
          checked_at: new Date(Date.now() - 60_000).toISOString(),
          pickup_message: "今日可取货",
        },
      ],
      proxy: { enabled: false },
    }),
  );
  const target = page.locator(".inventory-target").first();
  await expect(target.locator(".stock-status")).toHaveText("未知");
  await expect(target).toContainText("上次结果已过时，当前库存未知");
  await expect(target).toContainText("有效库存采样：");
  await expect(target).not.toContainText("尚未取得有效库存");
  await expect(target).not.toContainText("本次尝试：");
});

test("官网未明确库存时保留供应提示并标明官网响应时间", async ({ page }) => {
  await fixture(page);
  await page.goto("/#/monitor");
  await page.getByRole("button", { name: "保存并开始监控" }).click();
  await page.evaluate(() =>
    (window as any).reportInventory({
      monitoring: true,
      checked_at: new Date().toISOString(),
      targets: [
        {
          id: "t1",
          status: "unknown",
          checked_at: new Date().toISOString(),
          error: "本次查询没有该商品的明确库存状态。",
          pickup_message: "请于10月23日查看具体供应状况",
        },
      ],
      proxy: { enabled: true, dispatchable_count: 7, last_error: null },
    }),
  );
  const target = page.locator(".inventory-target").first();
  await expect(target.locator(".stock-status")).toHaveText("未知");
  await expect(target).toContainText("本次查询没有该商品的明确库存状态。");
  await expect(target).toContainText("官网提示：请于10月23日查看具体供应状况");
  await expect(target).toContainText("官网响应：");
  await expect(target).not.toContainText("有效库存采样：");
  await expect(target).not.toContainText("暂无库存");
  await expect(target).not.toContainText("上次结果已过时");
});

for (const scenario of ["retrying", "succeeded", "failed"] as const) {
  test(`当前任务展示重试执行次数：${scenario}`, async ({ page }) => {
    await fixture(page);
    await page.goto("/");
    await page
      .getByRole("spinbutton", { name: "下单失败重试次数", exact: true })
      .fill("2");
    await page
      .getByRole("button", { name: "立即下单（自提）", exact: true })
      .click();
    await page.evaluate((scenario) => {
      const summary = {
        retry_count: 2,
        attempts_made: scenario === "failed" ? 3 : 2,
        retries_used: scenario === "failed" ? 2 : 1,
        stop_reason:
          scenario === "failed"
            ? "exhausted"
            : scenario === "succeeded"
              ? "order_created"
              : "retrying",
      };
      const result =
        scenario === "succeeded"
          ? {
              status: "parallel_complete",
              concurrency: 2,
              retry_summary: summary,
              attempts: [1, 2].map((attempt) => ({
                attempt,
                task_id: `child-${attempt}`,
                status: "pending_payment",
                result: {
                  status: "pending_payment",
                  order_number: `W1234567${attempt}`,
                  retry_summary: summary,
                },
              })),
            }
          : {
              status: "purchase_retry",
              message:
                scenario === "failed" ? "重试次数已用完" : "正在进行下一轮下单",
              retry_summary: summary,
            };
      (window as any).reportPurchase({
        status: scenario === "retrying" ? "running" : scenario,
        stage:
          scenario === "failed"
            ? "下单失败，重试次数已用完"
            : scenario === "succeeded"
              ? "下单完成"
              : "正在进行第二轮下单",
        result,
      });
    }, scenario);
    const task = page.getByRole("region", { name: "当前任务" });
    await expect(task.locator(".purchase-retry-summary")).toHaveCount(1);
    await expect(task.locator(".purchase-retry-summary")).toHaveText(
      `已执行 ${scenario === "failed" ? 3 : 2} 轮下单，额外重试 ${scenario === "failed" ? 2 : 1} 次（最多 2 次）`,
    );
    await expect(task).toHaveClass(
      new RegExp(`\\b${scenario === "retrying" ? "running" : scenario}\\b`),
    );
    if (scenario === "failed") {
      await expect(task.locator(".section-title > .status")).toHaveText("失败");
      await expect(task.locator(".section-title > .status")).not.toHaveClass(
        /succeeded/,
      );
      await expect(task.getByRole("alert")).toContainText("重试次数已用完");
    }
  });
}

for (const selectedJob of ["store-a", "store-b"]) {
  test(`队列两店付款码和各路失败均可见，${selectedJob}按独立任务取码`, async ({
    page,
  }) => {
    await fixture(page);
    await page.goto("/", { waitUntil: "domcontentloaded" });
    await page
      .getByRole("button", { name: "立即下单（自提）", exact: true })
      .click();
    const emit = async (status: "running" | "failed", images: boolean) =>
      page.evaluate(
        ({ status, images }) => {
          const image =
            "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aD1sAAAAASUVORK5CYII=";
          const orders = [
            {
              queue_job_id: "store-a",
              task_id: "00000000-0000-4000-8000-000000000011",
              status: "pending_payment",
              order_number: "W12345671",
              pickup_store: "Apple 第一店",
              ...(images ? { qr_data: image } : {}),
            },
            {
              queue_job_id: "store-b",
              task_id: "00000000-0000-4000-8000-000000000022",
              status: "pending_payment",
              order_number: "W12345672",
              pickup_store: "Apple 第二店",
              ...(images ? { qr_data: image } : {}),
            },
          ];
          (window as any).reportPurchase({
            status,
            stage:
              status === "failed"
                ? "队列后续任务失败，保留已有订单"
                : "队列正在执行",
            result: {
              status: status === "failed" ? "queue_complete" : "queue_running",
              success: true,
              queue_jobs: [
                {
                  queue_job_id: "store-a",
                  store_name: "Apple 第一店",
                  sku: "MLH74FE",
                  status: "pending_payment",
                  started: true,
                  result: {
                    ...orders[0],
                    retry_summary: {
                      retry_count: 2,
                      attempts_made: 2,
                      retries_used: 1,
                    },
                  },
                },
                {
                  queue_job_id: "store-b",
                  store_name: "Apple 第二店",
                  sku: "MLH74FE",
                  status: "parallel_complete",
                  started: true,
                  result: { pending_orders: [orders[1]] },
                },
              ],
              pending_orders: orders,
              attempts: [
                {
                  queue_job_id: "store-b",
                  task_id: orders[1].task_id,
                  attempt: 1,
                  status: "pending_payment",
                  result: orders[1],
                },
                {
                  queue_job_id: "store-b",
                  task_id: "unknown-child",
                  attempt: 2,
                  status: "submission_unknown",
                  error: "第二路提交结果未知，请查询订单",
                },
                {
                  queue_job_id: "store-b",
                  task_id: "failed-child",
                  attempt: 3,
                  status: "failed",
                  error: "第三路未能完成下单",
                },
              ],
            },
          });
        },
        { status, images },
      );
    await emit("running", true);
    const current = page.getByRole("region", { name: "当前任务" });
    await expect(
      current.getByRole("heading", { name: "队列下单进行中" }),
    ).toBeVisible();
    await expect(
      current.getByRole("img", { name: /支付宝付款二维码/ }),
    ).toHaveCount(2);
    await emit("failed", true);
    await expect(current.locator(".section-title > .status")).toHaveText(
      "失败",
    );
    await expect(current.locator(".queue-job-result")).toHaveCount(2);
    await expect(current).toContainText("Apple 第一店");
    await expect(current).toContainText("Apple 第二店");
    await expect(current).toContainText("第二路提交结果未知，请查询订单");
    await expect(current).toContainText("第三路未能完成下单");
    await expect(current.locator(".purchase-retry-summary")).toHaveCount(1);
    await expect(
      current.getByRole("img", { name: /支付宝付款二维码/ }),
    ).toHaveCount(2);
    await emit("failed", false);
    await current
      .locator(`[data-queue-job-id="${selectedJob}"]`)
      .getByRole("button", { name: "获取二维码", exact: true })
      .click();
    expect(
      await page.evaluate(
        () =>
          (window as any).started.find((call: any) => call.action === "qr")
            .params.source_task,
      ),
    ).toBe(
      selectedJob === "store-a"
        ? "00000000-0000-4000-8000-000000000011"
        : "00000000-0000-4000-8000-000000000022",
    );
  });
}

test("队列全部失败时保留失败说明且不显示虚构待付款订单", async ({ page }) => {
  await fixture(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page
    .getByRole("button", { name: "立即下单（自提）", exact: true })
    .click();
  await page.evaluate(() =>
    (window as any).reportPurchase({
      status: "failed",
      stage: "本轮下单均失败",
      result: {
        status: "queue_complete",
        success: false,
        message: "两个目标均未能创建订单",
        queue_jobs: [
          {
            queue_job_id: "failed",
            store_name: "Apple 失败门店",
            status: "failed",
            started: true,
            result: {
              success: false,
              status: "failed",
              message: "指定门店不可取货",
            },
          },
        ],
        pending_orders: [],
        attempts: [],
      },
    }),
  );
  const current = page.getByRole("region", { name: "当前任务" });
  await expect(current.locator(".section-title > .status")).toHaveText("失败");
  await expect(current).toContainText("两个目标均未能创建订单");
  await expect(current).toContainText("指定门店不可取货");
  await expect(
    current.getByRole("img", { name: /支付宝付款二维码/ }),
  ).toHaveCount(0);
});
