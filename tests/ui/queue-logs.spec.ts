import { test, expect, type Page } from "@playwright/test";
import { chooseOption } from "./select-option";

async function fixture(page: Page, busy = false, attention = false) {
  await page.addInitScript(
    ({ busy, attention }) => {
      const root = window as any;
      const account = {
        id: "a",
        name: "测试账号",
        email: "owner@example.test",
        preset_sku: "MLH74FE",
      };
      const tasks: any[] = busy
        ? [
            {
              id: "busy-task",
              account_id: "a",
              action: "purchase",
              status: "running",
              stage: "正在处理现有订单",
              created_at: Date.now() / 1000,
              updated_at: Date.now() / 1000,
              prompt: null,
              result: null,
              order_submitted: false,
              logs: ["已确认商品", "正在核对门店"],
            },
          ]
        : [];
      const queueState: any = {
        mode: "sequential",
        max_concurrency: 1,
        paused: false,
        capacity: 500,
        child_capacity: 10,
        reserved_child_slots: 3,
        running_count: 0,
        queued_count: 2,
        blocked_accounts: [],
        jobs: [
          {
            id: "job2",
            sequence: 1,
            enqueued_at_ms: 1792701000124,
            account_id: "a",
            target_id: "target2",
            sku: "MLH74FE",
            quantity: 1,
            store_name: "Apple 第二店",
            store_id: "R002",
            concurrency: 1,
            retry_count: 0,
            status: "queued",
            message: "账号忙碌，保留排队任务",
          },
          {
            id: "job1",
            sequence: 2,
            enqueued_at_ms: 1792701000123,
            account_id: "a",
            target_id: "target1",
            sku: "MLH74FE",
            quantity: 1,
            store_name: "Apple 第一店",
            store_id: "R001",
            concurrency: 1,
            retry_count: 0,
            status: "queued",
            message: "等待账号完成现有任务",
            task_id: busy ? "busy-task" : "",
          },
        ],
      };
      const records = [
        {
          id: 3,
          ts: 1792701000125,
          account_id: "a",
          task_id: "busy-task",
          category: "inventory",
          action: "inventory_monitor",
          level: "warning",
          event: "inventory_unknown",
          message: "门店库存尚未确认",
          details: { store_id: "R001" },
        },
        {
          id: 2,
          ts_ms: 1792701000124,
          account_id: "a",
          task_id: "busy-task",
          category: "queue",
          action: "purchase",
          level: "info",
          event: "queue_waiting",
          message: "忙碌账号任务已保留在队列",
        },
        {
          id: 1,
          ts: 1792701000123,
          account_id: "a",
          task_id: "old-task",
          category: "authentication",
          action: "login",
          level: "info",
          event: "session_expired",
          message: "等待官网登录恢复",
        },
      ];
      if (attention) {
        queueState.blocked_accounts = [
          { account_id: "legacy-auth-account", reason: "authentication" },
          { account_id: "a", reason: "submission_unknown" },
        ];
        queueState.jobs.push(
          {
            ...queueState.jobs[0],
            id: "payment",
            sequence: 3,
            status: "succeeded",
            task_id: "busy-task",
            result: {
              status: "pending_payment",
              pickup_store: "实际取货店",
              pickup_store_id: "R003",
              pickup_time_slot: {
                date: "2026-10-23",
                start: "10:00",
                end: "10:15",
              },
            },
            pickup_policy: {
              selection_mode: "priority",
              stores: [{ store_name: "预选门店", store_id: "R002" }],
            },
          },
          {
            ...queueState.jobs[1],
            id: "unknown",
            sequence: 4,
            enqueued_at_ms: 1792701000125,
            status: "submission_unknown",
            task_id: "busy-task",
            message: "需核对官网订单",
          },
        );
      }
      let maintenance = {
        enabled: false,
        interval_minutes: 30,
        last_runs: [
          {
            account_id: "a",
            status: "requires_login",
            message: "设备信任失效，请恢复官网登录",
            requires_login: true,
          },
        ],
      };
      let listener = (_task: any) => {};
      let concurrent = 0;
      root.queueCalls = [];
      root.logCalls = [];
      root.logRecords = records;
      root.maintenanceCalls = [];
      root.started = [];
      root.removed = [];
      root.queueMaximumInFlight = 0;
      root.__DUO_KILLER_TEST__ = {
        snapshot: async () =>
          structuredClone({ accounts: [account], tasks, data_dir: "/fixture" }),
        subscribe: async (callback: any) => {
          listener = callback;
          return () => {};
        },
        add: async () => account,
        remove: async (id: string) => root.removed.push(id),
        savePreset: async () => account,
        cancel: async () => {},
        reply: async () => {},
        start: async (accountId: string, action: string, params: any) => {
          root.started.push({ accountId, action, params });
          const task = {
            id: "login-task",
            account_id: accountId,
            action,
            status: "running",
            stage: "等待官网登录恢复",
            created_at: Date.now() / 1000,
            updated_at: Date.now() / 1000,
            prompt: null,
            result: null,
            order_submitted: false,
            logs: ["使用此账号原有浏览器配置"],
          };
          tasks.unshift(task);
          listener(structuredClone(task));
          return task.id;
        },
        queue: async (operation: string, params: any = {}) => {
          root.queueCalls.push({ operation, params });
          concurrent++;
          root.queueMaximumInFlight = Math.max(
            root.queueMaximumInFlight,
            concurrent,
          );
          await new Promise((resolve) => setTimeout(resolve, 100));
          if (operation === "configure") Object.assign(queueState, params);
          if (operation === "pause") queueState.paused = true;
          if (operation === "resume") queueState.paused = false;
          if (operation === "cancel")
            queueState.jobs.find(
              (job: any) => job.id === params.job_id,
            ).status = "cancelled";
          concurrent--;
          return structuredClone(queueState);
        },
        logs: async (params: any = {}) => {
          root.logCalls.push(params);
          const matching = records.filter(
            (entry: any) =>
              (!params.before_id || entry.id < params.before_id) &&
              (params.after_id === undefined || entry.id > params.after_id) &&
              ["account_id", "task_id", "category", "level"].every(
                (key) => !params[key] || entry[key] === params[key],
              ),
          );
          const ordered =
            params.after_id === undefined
              ? matching
              : [...matching].sort((a, b) => a.id - b.id);
          return {
            entries: structuredClone(ordered.slice(0, 2)),
            next_before_id: matching.length
              ? matching[Math.min(1, matching.length - 1)].id
              : null,
            has_more: matching.length > 2,
          };
        },
        maintenance: async (operation: string, params: any = {}) => {
          root.maintenanceCalls.push({ operation, params });
          if (operation === "configure")
            maintenance = { ...maintenance, ...params };
          return structuredClone(maintenance);
        },
        browser: async () => ({ active: false }),
      };
    },
    { busy, attention },
  );
}

test("队列最新倒序展示，执行策略折叠且保留暂停取消", async ({ page }) => {
  await fixture(page, true);
  await page.goto("/#/queue", { waitUntil: "domcontentloaded" });
  await expect(page.locator(".queue-job")).toHaveCount(2);
  expect(
    await page
      .locator(".queue-job")
      .evaluateAll((rows) =>
        rows.map((row) => row.getAttribute("data-job-id")),
      ),
  ).toEqual(["job2", "job1"]);
  await expect(page.locator(".queue-job").first()).toHaveAttribute(
    "data-job-id",
    "job2",
  );
  await expect(page.locator(".queue-job").first()).toContainText(
    "账号忙碌，保留排队任务",
  );
  await expect(page.locator(".queue-job").first()).toContainText(".124");
  await expect(
    page.getByText("最新在前 · 执行按入队顺序", { exact: true }),
  ).toBeVisible();
  await expect(page.getByLabel("队列执行方式")).not.toBeVisible();
  await page.locator(".queue-policy > summary").click();
  await expect(
    page.getByRole("combobox", { name: "队列执行方式", exact: true }),
  ).toHaveText("逐个执行");
  await expect(
    page.getByText(
      "下单执行名额：3 / 10；每个账号的下单任务占用1个名额，名额不足时继续排队。",
      { exact: true },
    ),
  ).toBeVisible();
  await expect(page.locator(".queue-policy")).toContainText(
    "不同账号同时执行1–10个任务；同账号顺序执行",
  );
  await chooseOption(
    page.getByRole("combobox", { name: "队列执行方式", exact: true }),
    "固定并发",
  );
  await page
    .getByRole("spinbutton", { name: "同时执行任务上限", exact: true })
    .fill("3");
  await page.getByRole("button", { name: "保存执行策略" }).click();
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (window as any).queueCalls.find(
            (call: any) => call.operation === "configure",
          )?.params,
      ),
    )
    .toEqual({ mode: "parallel", max_concurrency: 3 });
  await page.getByRole("button", { name: "暂停队列" }).click();
  await expect(page.locator(".queue-policy > summary")).toContainText(
    "队列已暂停",
  );
  await page.getByRole("button", { name: "恢复队列" }).click();
  await expect(page.getByRole("button", { name: "暂停队列" })).toBeVisible();
  await page
    .locator(".queue-job")
    .last()
    .getByRole("button", { name: "取消队列任务" })
    .click();
  await expect(page.locator(".queue-job").last()).toContainText("已取消");
  expect(await page.evaluate(() => (window as any).queueMaximumInFlight)).toBe(
    1,
  );
  await page
    .locator(".queue-job")
    .last()
    .getByRole("button", { name: "查看关联任务" })
    .click();
  const current = page.getByRole("region", { name: "当前任务" });
  await expect(current).toContainText("正在处理现有订单");
  await current.getByText("当前任务日志（2 条）", { exact: true }).click();
  await expect(current.locator("pre")).toContainText("正在核对门店");
});

test("相同入队时间按序号倒序，付款和未知结果常显且保留处理入口", async ({
  page,
}) => {
  await fixture(page, true, true);
  await page.goto("/#/queue", { waitUntil: "domcontentloaded" });
  await expect(page.locator(".queue-job")).toHaveCount(4);
  expect(
    await page
      .locator(".queue-job")
      .evaluateAll((rows) =>
        rows.map((row) => row.getAttribute("data-job-id")),
      ),
  ).toEqual(["unknown", "payment", "job2", "job1"]);
  const payment = page.locator('[data-job-id="payment"]');
  await expect(payment.locator(".queue-job-heading strong")).toHaveText(
    "抛光布",
  );
  await expect(payment.locator(".queue-job-heading")).toContainText(
    "MLH74FE · 实际取货店",
  );
  await expect(payment.getByText("待付款", { exact: true })).toBeVisible();
  await expect(payment.getByRole("button", { name: "前往付款" })).toBeVisible();
  await expect(
    payment.getByText("订单待付款，请及时打开关联任务完成支付。", {
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    payment.getByText("预选顺序：", { exact: false }),
  ).not.toBeVisible();
  await payment.locator("summary").click();
  await expect(payment.getByText("预选顺序：", { exact: false })).toBeVisible();
  await expect(payment).toContainText("实际取货门店：实际取货店 (R003)");
  await expect(payment).toContainText("2026-10-23 10:00–10:15");
  const unknown = page.locator('[data-job-id="unknown"]');
  await expect(
    unknown.getByText("提交结果未知", { exact: true }),
  ).toBeVisible();
  await expect(
    unknown.getByText("请先查询订单确认提交结果，避免重复下单。", {
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    unknown.getByRole("button", { name: "取消队列任务" }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "登录恢复后继续此账号" }),
  ).toHaveCount(0);
  await expect(page.getByText("需要恢复官网登录", { exact: true })).toHaveCount(
    0,
  );
  await expect(page.locator(".queue-blocked")).toHaveCount(1);
  await expect(page.locator(".queue-blocked")).toContainText(
    "订单提交结果未知，请先查询订单确认",
  );
  await expect(page.getByRole("link", { name: "查看账号日志" })).toHaveClass(
    /ui-action-link/,
  );
  await page.screenshot({
    path: "artifacts/queue-desktop-ui.png",
    fullPage: true,
  });
});

test("完整日志可分页和筛选，并从任务进入对应过滤记录", async ({ page }) => {
  await fixture(page, true);
  await page.goto("/#/logs?category=", { waitUntil: "domcontentloaded" });
  await expect(page.locator(".log-entry")).toHaveCount(2);
  await page.getByLabel("自动刷新日志").uncheck();
  await page.getByRole("button", { name: "加载更早日志" }).click();
  await expect(page.locator(".log-entry")).toHaveCount(3);
  await expect(page.locator(".log-entry").last()).toContainText(
    "等待官网登录恢复",
  );
  expect(
    await page.evaluate(() =>
      (window as any).logCalls.some((call: any) => call.before_id === 2),
    ),
  ).toBe(true);
  await expect(page.getByLabel("日志类别")).toBeVisible();
  await chooseOption(
    page.getByRole("combobox", { name: "日志类别", exact: true }),
    "库存监控",
  );
  await chooseOption(
    page.getByRole("combobox", { name: "日志级别", exact: true }),
    "警告",
  );
  await chooseOption(
    page.getByRole("combobox", { name: "日志账号", exact: true }),
    "测试账号",
  );
  await page.getByLabel("日志任务编号").fill("busy-task");
  await expect(page.locator(".log-entry")).toHaveCount(1);
  await expect(page.locator(".log-entry")).toContainText("门店库存尚未确认");
  await expect
    .poll(() => page.evaluate(() => (window as any).logCalls.at(-1)))
    .toMatchObject({
      account_id: "a",
      task_id: "busy-task",
      category: "inventory",
      level: "warning",
    });
  await page.goto("/#/", { waitUntil: "domcontentloaded" });
  const current = page.getByRole("region", { name: "当前任务" });
  await current.getByText("当前任务日志（2 条）", { exact: true }).click();
  await current.getByRole("link", { name: "查看此任务完整日志" }).click();
  await expect(page.getByLabel("日志任务编号")).toHaveValue("busy-task");
  await expect(
    page.getByRole("combobox", { name: "日志账号", exact: true }),
  ).toHaveText("测试账号");
});

test("日志默认聚焦监控，以ID增量读取同一毫秒记录并保留旧页", async ({
  page,
}) => {
  await fixture(page);
  await page.goto("/#/logs", { waitUntil: "domcontentloaded" });
  await expect(page.locator(".log-entry")).toHaveCount(1);
  await expect(page.getByLabel("日志类别")).not.toBeVisible();
  expect(await page.evaluate(() => (window as any).logCalls[0])).toMatchObject({
    category: "inventory",
    limit: 40,
  });
  await page.evaluate(() => {
    const root = window as any;
    root.logRecords.unshift({
      ...root.logRecords[0],
      id: 4,
      message: "同毫秒新增监控日志",
    });
  });
  await expect(page.locator(".log-entry")).toHaveCount(2);
  expect(
    await page.evaluate(() =>
      (window as any).logCalls.some((call: any) => call.after_id === 3),
    ),
  ).toBe(true);
  await page.getByRole("button", { name: "队列下单", exact: true }).click();
  await expect(page.locator(".log-entry")).toHaveCount(1);
  await expect(page.locator(".log-entry")).toContainText(
    "忙碌账号任务已保留在队列",
  );
  expect(
    await page.evaluate(() => (window as any).logCalls.at(-1)),
  ).toMatchObject({ category: "queue", limit: 40 });
});

test("保留账号的官网登录恢复和已存30分钟空闲预热设置", async ({ page }) => {
  await fixture(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await expect(page.getByRole("heading", { name: "空闲预热" })).toBeVisible();
  await expect(
    page.getByText("设备信任失效，请恢复官网登录", { exact: false }),
  ).toBeVisible();
  await page.getByLabel("下单空闲30分钟后自动预热").check();
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (window as any).maintenanceCalls.find(
            (call: any) => call.operation === "configure",
          )?.params,
      ),
    )
    .toEqual({ enabled: true, interval_minutes: 30 });
  await page
    .getByRole("button", { name: "官网登录恢复（保留账号）", exact: true })
    .click();
  expect(await page.evaluate(() => (window as any).started)).toEqual([
    { accountId: "a", action: "login", params: { browser_login: true } },
  ]);
  expect(await page.evaluate(() => (window as any).removed)).toEqual([]);
});

for (const route of ["queue", "logs"] as const) {
  test(`390px ${route}页面无横向溢出`, async ({ page }) => {
    await fixture(page, true);
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(`/#/${route}`, { waitUntil: "domcontentloaded" });
    await expect(
      page.locator(route === "queue" ? ".queue-job" : ".log-entry"),
    ).toHaveCount(route === "queue" ? 2 : 1);
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth),
    ).toBeLessThanOrEqual(390);
    if (route === "queue")
      await page.screenshot({
        path: "artifacts/queue-mobile-ui.png",
        fullPage: true,
      });
  });
}
