import { test, expect, type Page } from "@playwright/test";

async function fixture(
  page: Page,
  options: {
    monitoring?: boolean;
    saveFails?: boolean;
    settingsFail?: boolean;
  } = {},
) {
  await page.addInitScript(({ monitoring, saveFails, settingsFail }) => {
    const root = window as any;
    const account = {
      id: "a",
      name: "测试账号",
      email: "owner@example.test",
      preset_sku: "MLH74FE",
      preset_quantity: 2,
    };
    let checkout: any = { pickup: { search: "201101" }, contact: {} };
    let inventory: any = {
      interval_seconds: 1,
      proxy: {
        enabled: true,
        pool_size: 20,
        providers: [
          {
            id: "existing-provider",
            kind: "generic",
            enabled: true,
            extract_url_configured: true,
          },
        ],
      },
      targets: [
        {
          id: "other-account",
          account_id: "b",
          sku: "MLH74FE",
          quantity: 4,
          store_id: "R777",
          store_name: "其他账号门店",
          location: "100000",
          auto_purchase: true,
          concurrency: 4,
          retry_count: 2,
        },
        {
          id: "reuse",
          account_id: "a",
          sku: "MLH74FE/A",
          quantity: 1,
          store_id: "R002",
          store_name: "第二店",
          location: "201101",
          auto_purchase: false,
          concurrency: 2,
          retry_count: 1,
        },
      ],
    };
    const tasks: any[] = monitoring
      ? [
          {
            id: "existing-monitor",
            account_id: "00000000-0000-0000-0000-000000000000",
            action: "inventory_monitor",
            status: "running",
            stage: "正在监控",
            created_at: 1,
            updated_at: 1,
            result: { monitoring: true },
            logs: [],
          },
        ]
      : [];
    let update = (_task: any) => {};
    let counter = 0;
    root.started = [];
    root.cancelled = [];
    root.policyCommitted = false;
    root.savedCheckout = () => structuredClone(checkout);
    root.savedInventory = () => structuredClone(inventory);
    root.__DUO_KILLER_TEST__ = {
      snapshot: async () =>
        structuredClone({ accounts: [account], tasks, data_dir: "/fixture" }),
      subscribe: async (callback: any) => {
        update = callback;
        return () => {};
      },
      add: async () => account,
      remove: async () => {},
      savePreset: async () => account,
      reply: async () => {},
      cancel: async (_account: string, id: string) => {
        root.cancelled.push(id);
      },
      start: async (accountId: string, action: string, params: any = {}) => {
        const task: any = {
          id: `pref-${++counter}`,
          account_id: accountId,
          action,
          status: "succeeded",
          stage: "完成",
          created_at: Date.now() / 1000 + counter,
          updated_at: Date.now() / 1000 + counter,
          result: {},
          logs: [],
          prompt: null,
          order_submitted: false,
        };
        root.started.push({
          id: task.id,
          accountId,
          action,
          params,
          policyCommitted: root.policyCommitted,
        });
        if (action === "address_read")
          task.result = { config: structuredClone(checkout) };
        if (action === "store_search")
          task.result = {
            stores: [
              {
                store_id: "R001",
                store_name: "Apple 第一店",
                location: "上海",
                status: "unknown",
              },
              {
                store_id: "R002",
                store_name: "Apple 第二店",
                location: "上海",
                status: "unavailable",
              },
              {
                store_id: "R003",
                store_name: "Apple 第三店",
                location: "上海",
                status: "available",
              },
            ],
          };
        if (action === "address_save") {
          if (saveFails) {
            task.status = "failed";
            task.stage = "自提偏好保存失败";
            task.result = null;
          } else {
            const { pickup_only, ...value } = params;
            checkout = { ...checkout, ...structuredClone(value) };
            root.policyCommitted = true;
            task.result = {
              config: structuredClone(checkout),
              message: "自提偏好已保存。",
            };
          }
        }
        if (action === "inventory_settings_read") {
          if (settingsFail) {
            task.status = "failed";
            task.stage = "监控配置读取失败";
            task.result = null;
          } else task.result = { config: structuredClone(inventory) };
        }
        if (action === "inventory_settings_save") {
          inventory = structuredClone(params.config);
          task.result = { config: structuredClone(inventory) };
        }
        if (action === "inventory_monitor") {
          task.status = "running";
          task.stage = "监控已启动";
        }
        tasks.unshift(task);
        update(structuredClone(task));
        return task.id;
      },
    };
  }, options);
}

async function addStores(page: Page, names = ["Apple 第一店", "Apple 第二店"]) {
  await page.getByLabel("门店选择方式").selectOption("priority");
  await page.getByRole("button", { name: "从官网读取门店" }).click();
  for (const name of names)
    await page.getByRole("checkbox", { name: new RegExp(name) }).check();
  await page
    .getByRole("button", { name: `添加选中的 ${names.length} 家门店` })
    .click();
}

test("多选门店按点击顺序加入并可重排删除，窗口规则可免本地联系人保存", async ({
  page,
}) => {
  await fixture(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/#/settings", { waitUntil: "domcontentloaded" });
  await addStores(page, ["Apple 第三店", "Apple 第一店"]);
  const list = page.getByLabel("预选自提门店顺序");
  await expect(list.locator("article strong")).toHaveText([
    "1. Apple 第三店",
    "2. Apple 第一店",
  ]);
  await list.getByRole("button", { name: "上移 Apple 第一店" }).click();
  await expect(list.locator("article strong")).toHaveText([
    "1. Apple 第一店",
    "2. Apple 第三店",
  ]);
  await list.getByRole("button", { name: "下移 Apple 第一店" }).click();
  await list.getByRole("button", { name: "删除预选 Apple 第三店" }).click();
  await expect(list.locator("article strong")).toHaveText(["1. Apple 第一店"]);
  await page.getByLabel("自提时段规则").selectOption("window");
  await page.getByLabel("可接受开始时间").fill("10:30");
  await page.getByLabel("可接受结束时间").fill("15:00");
  await page.getByLabel("无匹配时段").selectOption("stop");
  await expect(page.getByLabel("取货联系人来源")).toHaveValue("official");
  await expect(
    page.getByLabel("取货手机号", { exact: true }),
  ).not.toBeVisible();
  await page.getByRole("button", { name: "保存自提预设", exact: true }).click();
  await expect(
    page.getByRole("status").filter({ hasText: "自提偏好已保存。" }),
  ).toBeVisible();
  const params = await page.evaluate(
    () =>
      (window as any).started.findLast(
        (call: any) => call.action === "address_save",
      ).params,
  );
  expect(params.pickup).toMatchObject({
    selection_mode: "priority",
    contact_mode: "official",
    stores: [
      { store_id: "R001", store_name: "Apple 第一店", search: "201101" },
    ],
    time_slot_rule: {
      mode: "window",
      start: "10:30",
      end: "15:00",
      fallback: "stop",
    },
  });
  expect(params.contact).toBeUndefined();
  await page.getByRole("link", { name: "账号工作台", exact: true }).click();
  await page.getByRole("link", { name: "本地配置", exact: true }).click();
  await expect(page.getByLabel("自提时段规则")).toHaveValue("window");
  await expect(page.getByLabel("可接受开始时间")).toHaveValue("10:30");
  await expect(page.getByLabel("预选自提门店顺序")).toContainText("R001");
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth),
  ).toBeLessThanOrEqual(390);
  await page.screenshot({
    path: "artifacts/pickup-preferences-mobile.png",
    fullPage: true,
  });
});

for (const monitoring of [true, false]) {
  test(`先保存偏好再联动监控，保留其他配置及已有路数：monitoring=${monitoring}`, async ({
    page,
  }) => {
    await fixture(page, { monitoring });
    await page.goto("/#/settings", { waitUntil: "domcontentloaded" });
    await addStores(page, ["Apple 第二店", "Apple 第一店"]);
    await page.getByLabel("自提时段规则").selectOption("latest");
    await page
      .getByRole("button", { name: "监控预选门店，有货自动下单" })
      .click();
    await expect(
      page.getByRole("status").filter({ hasText: "已授权监控预选门店" }),
    ).toBeVisible();
    const calls = await page.evaluate(() => (window as any).started);
    expect(
      calls.find((call: any) => call.action === "store_search"),
    ).toBeTruthy();
    expect(
      calls.find((call: any) => call.action === "inventory_settings_read")
        .policyCommitted,
    ).toBe(true);
    expect(
      calls.filter((call: any) => call.action === "inventory_monitor"),
    ).toHaveLength(monitoring ? 0 : 1);
    expect(await page.evaluate(() => (window as any).cancelled)).toEqual([]);
    const config = await page.evaluate(() => (window as any).savedInventory());
    expect(config.proxy).toMatchObject({
      enabled: true,
      pool_size: 20,
      providers: [{ id: "existing-provider" }],
    });
    expect(config.targets).toHaveLength(3);
    expect(
      config.targets.find((target: any) => target.id === "other-account"),
    ).toMatchObject({
      account_id: "b",
      quantity: 4,
      concurrency: 4,
      retry_count: 2,
    });
    expect(
      config.targets.find((target: any) => target.id === "reuse"),
    ).toMatchObject({
      account_id: "a",
      sku: "MLH74FE",
      quantity: 2,
      auto_purchase: true,
      concurrency: 2,
      retry_count: 1,
    });
    expect(
      config.targets.find((target: any) => target.store_id === "R001"),
    ).toMatchObject({
      account_id: "a",
      quantity: 2,
      concurrency: 1,
      retry_count: 0,
      auto_purchase: true,
    });
    expect(
      (
        await page.evaluate(() => (window as any).savedCheckout())
      ).pickup.stores.map((store: any) => store.store_id),
    ).toEqual(["R002", "R001"]);
  });
}

for (const failure of ["saveFails", "settingsFail"] as const) {
  test(`保存或读取失败时不扩大自动下单授权：${failure}`, async ({ page }) => {
    await fixture(page, { [failure]: true, monitoring: true });
    await page.goto("/#/settings", { waitUntil: "domcontentloaded" });
    await addStores(page);
    await page
      .getByRole("button", { name: "监控预选门店，有货自动下单" })
      .click();
    await expect(page.getByRole("alert")).toContainText(
      failure === "saveFails" ? "自提偏好保存失败" : "监控配置读取失败",
    );
    const calls = await page.evaluate(() => (window as any).started);
    expect(
      calls.some(
        (call: any) =>
          call.action === "inventory_settings_save" ||
          call.action === "inventory_monitor",
      ),
    ).toBe(false);
    if (failure === "saveFails")
      expect(
        calls.some((call: any) => call.action === "inventory_settings_read"),
      ).toBe(false);
  });
}

test("固定模式也可先保存门店偏好，手机页面无横向溢出", async ({ page }) => {
  await fixture(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/#/settings", { waitUntil: "domcontentloaded" });
  await page.getByLabel("自提门店", { exact: true }).fill("Apple 固定店");
  await page.getByRole("button", { name: "保存自提预设", exact: true }).click();
  await expect(
    page.getByRole("status").filter({ hasText: "自提偏好已保存。" }),
  ).toBeVisible();
  const config = await page.evaluate(() => (window as any).savedCheckout());
  expect(config.pickup.selection_mode).toBe("fixed");
  expect(config.pickup.time_slot_rule.mode).toBe("earliest");
  expect(config.contact.fullDaytimePhone).toBeUndefined();
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth),
  ).toBeLessThanOrEqual(390);
});
