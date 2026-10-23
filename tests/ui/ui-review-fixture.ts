import { expect, type Page } from "@playwright/test";

/** Synthetic review data: never reads accounts, sessions, inventory or orders. */
export async function uiReviewFixture(page: Page, multipleAccounts = false) {
  await page.route("**/api/**", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ authenticated: true, success: true }),
    }),
  );
  await page.addInitScript(
    ({ multipleAccounts }) => {
      const root = window as any;
      const local = "00000000-0000-0000-0000-000000000000";
      const now = Date.now();
      const accounts = [
        {
          id: "review-a",
          name: "日常账号",
          email: "review@example.test",
          preset_sku: "MJT84CH",
          preset_quantity: 1,
        },
      ];
      if (multipleAccounts)
        accounts.push({
          ...accounts[0],
          id: "review-b",
          name: "备用账号",
          email: "second@example.test",
        });
      const makeTask = (
        id: string,
        action: string,
        result: any,
        account_id = "review-a",
      ) => ({
        id,
        account_id,
        action,
        result,
        status: "succeeded",
        stage: "已完成",
        created_at: now / 1000 - 60,
        updated_at: now / 1000,
        prompt: null,
        order_submitted: false,
        logs: [],
      });
      const items = [
        {
          name: "iPhone 18 Pro 256GB 银色",
          sku: "MJT84CH",
          quantity: 1,
          subtotal: "RMB 8,999",
        },
      ];
      const tasks: any[] = [
        makeTask("review-me", "me", {
          success: true,
          logged_in: true,
          scope: "account",
          name: "日常账号",
          email: "review@example.test",
        }),
        makeTask("review-bag", "bag", {
          success: true,
          items,
          total: "RMB 8,999",
          bag_total: "RMB 8,999",
        }),
        makeTask("review-orders", "orders", {
          success: true,
          orders: [
            {
              order_number: "W000000001",
              status: "订单处理中",
              items,
              total: "RMB 8,999",
              placed_date: "2026-10-23",
            },
          ],
        }),
        makeTask("review-catalog", "catalog", {
          success: true,
          items: [
            {
              sku: "MJT84CH",
              name: "iPhone 18 Pro 256GB 银色",
              price: "RMB 8,999",
            },
          ],
        }),
      ];
      let config = {
        interval_seconds: 1,
        targets: [
          {
            id: "target-1",
            sku: "MJT84CH",
            quantity: 1,
            store_id: "R001",
            store_name: "Apple 上海环贸 iapm",
            location: "200000",
            account_id: "review-a",
            auto_purchase: true,
            concurrency: 1,
            retry_count: 0,
          },
          {
            id: "target-2",
            sku: "MJT84CH",
            quantity: 1,
            store_id: "R002",
            store_name: "Apple 南京东路",
            location: "200000",
            account_id: "",
            auto_purchase: false,
            concurrency: 1,
            retry_count: 0,
          },
        ],
        proxy: { enabled: false, pool_size: 10, providers: [] },
      };
      const events = [
        {
          id: 106,
          sku: "MJT84CH",
          store_id: "R001",
          store_name: "Apple 上海环贸 iapm",
          delta: 30000,
        },
        {
          id: 105,
          sku: "MJT84CH",
          store_id: "R002",
          store_name: "Apple 南京东路",
          delta: 30000,
        },
        {
          id: 104,
          sku: "MK2M4CH",
          store_id: "R003",
          store_name: "Apple 浦东",
          delta: 90000,
        },
        {
          id: 103,
          sku: "MLH74FE",
          store_id: "R001",
          store_name: "Apple 上海环贸 iapm",
          delta: 160000,
        },
      ].map((e) => ({
        id: e.id,
        ts_ms: now - e.delta,
        category: "stock",
        level: "info",
        action: "inventory_monitor",
        event: "inventory_available",
        message: "发现门店可取货",
        details: {
          sku: e.sku,
          store_id: e.store_id,
          store_name: e.store_name,
          checked_at_ms: now - e.delta,
        },
      }));
      const queueState: any = {
        mode: "sequential",
        max_concurrency: 1,
        paused: false,
        capacity: 500,
        child_capacity: 10,
        reserved_child_slots: 1,
        running_count: 0,
        queued_count: 2,
        blocked_accounts: [],
        jobs: [
          "queued",
          "queued",
          "succeeded",
          "submission_unknown",
          "cancelled",
        ].map((status, i) => ({
          id: `review-job-${i}`,
          sequence: i + 1,
          enqueued_at_ms: now - i * 20000,
          account_id: "review-a",
          target_id: "target-1",
          sku: i === 2 ? "MK2M4CH" : "MJT84CH",
          quantity: 1,
          store_id: "R001",
          store_name: i % 2 ? "Apple 南京东路" : "Apple 上海环贸 iapm",
          concurrency: 1,
          retry_count: 0,
          status,
          message:
            status === "submission_unknown"
              ? "提交结果未知，请核对官网订单"
              : "等待账号空闲，保留队列位置",
          ...(status === "succeeded"
            ? {
                result: {
                  status: "pending_payment",
                  order_number: "W000000002",
                  pickup_store: "Apple 上海环贸 iapm",
                },
              }
            : {}),
        })),
      };
      let listener = (_task: any) => {};
      let sequence = 0;
      root.uiReviewCalls = [];
      root.__DUO_KILLER_TEST__ = {
        snapshot: async () =>
          structuredClone({
            accounts,
            tasks,
            data_dir: "/synthetic-ui-review",
          }),
        task: async (_a: string, id: string) =>
          structuredClone(tasks.find((t) => t.id === id)),
        subscribe: async (cb: any) => {
          listener = cb;
          return () => {};
        },
        add: async () => accounts[0],
        remove: async () => {},
        cancel: async () => {},
        reply: async () => {},
        savePreset: async (id: string, sku: string, quantity: number) => {
          const a = accounts.find((a) => a.id === id) || {
            id,
            name: "通用预设",
            email: "",
          };
          Object.assign(a, { preset_sku: sku, preset_quantity: quantity });
          return a;
        },
        queue: async (operation: string, params: any = {}) => {
          if (operation === "configure") Object.assign(queueState, params);
          return structuredClone(queueState);
        },
        logs: async (params: any = {}) => ({
          entries: structuredClone(
            params.category === "stock"
              ? events
              : [
                  ...events,
                  {
                    id: 100,
                    ts_ms: now - 180000,
                    account_id: "review-a",
                    level: "warning",
                    category: "queue",
                    event: "submission_unknown",
                    message: "需核对官网订单后继续",
                    details: { sku: "MJT84CH" },
                  },
                ],
          ),
          has_more: false,
        }),
        maintenance: async () => ({
          enabled: false,
          interval_minutes: 10,
          last_runs: [],
        }),
        browser: async () => ({ active: false }),
        start: async (accountId: string, action: string, params: any) => {
          root.uiReviewCalls.push({ accountId, action, params });
          const task: any = makeTask(
            `review-start-${++sequence}`,
            action,
            {},
            accountId,
          );
          if (action === "inventory_settings_save")
            config = structuredClone(params.config);
          if (action.startsWith("inventory_settings"))
            task.result = { config: structuredClone(config) };
          if (action === "address_read")
            task.result = {
              config: {
                pickup: {
                  search: "200000",
                  stores: [
                    { store_id: "R001", store_name: "Apple 上海环贸 iapm" },
                  ],
                  selection_mode: "priority",
                  slot_mode: "earliest",
                  id_last4: "",
                },
                address: {},
                contact: {},
              },
            };
          if (action === "address_save") task.result = { config: params };
          if (action === "store_search")
            task.result = {
              stores: config.targets.map((t) => ({
                ...t,
                pickup_message: "今日可取货",
              })),
            };
          if (action === "login" || action === "enroll") {
            task.status = "waiting";
            task.stage = "等待输入";
            task.prompt = {
              id: "review-password",
              kind: "password",
              label: "输入 Apple 账号密码",
            };
          }
          tasks.unshift(task);
          listener(structuredClone(task));
          return task.id;
        },
      };
    },
    { multipleAccounts },
  );
}

export async function noHorizontalOverflow(page: Page) {
  const diagnostics = await page.evaluate(() =>
    [...document.querySelectorAll("body *")]
      .map((el) => ({
        tag: el.tagName,
        class: el.getAttribute("class"),
        right: Math.round(el.getBoundingClientRect().right),
        text: el.textContent?.slice(0, 50),
      }))
      .filter((el) => el.right > innerWidth + 1),
  );
  await expect
    .poll(
      () =>
        page.evaluate(
          () => document.documentElement.scrollWidth - window.innerWidth,
        ),
      { message: JSON.stringify({ route: page.url(), diagnostics }) },
    )
    .toBeLessThanOrEqual(1);
}
