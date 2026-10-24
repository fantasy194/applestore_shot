import { test, expect, type Page } from "@playwright/test";

test.setTimeout(60_000);

const LOCAL = "00000000-0000-0000-0000-000000000000";
const ACCOUNT_A = "fixture-account-a";
const ACCOUNT_B = "fixture-account-b";

async function fixture(
  page: Page,
  options: {
    delayedAccountA?: boolean;
    delayedLocalDetail?: boolean;
    failFirstAccountARead?: boolean;
    singleAccount?: boolean;
    delayedInitialSnapshot?: boolean;
    noTaskHistory?: boolean;
  } = {},
) {
  await page.addInitScript(
    ({
      delayedAccountA,
      delayedLocalDetail,
      failFirstAccountARead,
      singleAccount,
      delayedInitialSnapshot,
      noTaskHistory,
    }) => {
      const root = window as any;
      const local = "00000000-0000-0000-0000-000000000000";
      const accounts = [
        {
          id: "fixture-account-a",
          name: "账号甲",
          email: "a@example.test",
          preset_sku: "MLH74FE",
          preset_quantity: 1,
        },
        {
          id: "fixture-account-b",
          name: "账号乙",
          email: "b@example.test",
          preset_sku: "MLH74FE",
          preset_quantity: 1,
        },
      ];
      if (singleAccount) accounts.splice(1);
      const defaults = {
        id: local,
        name: "通用预设",
        email: "",
        preset_sku: "MLH74FE",
        preset_quantity: 1,
      };
      const key = "duo-local-settings-persistence-fixture";
      if (!localStorage.getItem(key)) {
        const configs: Record<string, any> = {};
        for (const [id, name, suffix, mode] of [
          [local, "通用", "03", "official"],
          ["fixture-account-a", "甲", "01", "official"],
          ["fixture-account-b", "乙", "02", "local"],
        ]) {
          configs[id] = {
            contact: {
              lastName: name,
              firstName: "测试",
              emailAddress: `fixture${suffix}@example.test`,
              fullDaytimePhone: `138001380${suffix}`,
            },
            delivery: { state: "上海", city: "上海", district: "测试区" },
            address: { street: `${name}保存的街道 100 号` },
            pickup: {
              selection_mode: "priority",
              search: "201101",
              store_id: `R${suffix}1`,
              store_name: `Apple ${name}第一店`,
              stores: [
                {
                  store_id: `R${suffix}1`,
                  store_name: `Apple ${name}第一店`,
                  search: "201101",
                },
                {
                  store_id: `R${suffix}2`,
                  store_name: `Apple ${name}第二店`,
                  search: "201102",
                },
              ],
              contact_mode: mode,
              time_slot_rule: {
                mode: "window",
                start:
                  id === local
                    ? "10:00"
                    : id === "fixture-account-a"
                      ? "11:00"
                      : "12:00",
                end: "18:00",
                fallback: "stop",
              },
            },
          };
        }
        localStorage.setItem(key, JSON.stringify(configs));
      }
      const configs = () => JSON.parse(localStorage.getItem(key)!);
      const details = new Map<string, any>();
      const tasks: any[] = [];
      let counter = 0;
      let accountAReadFailed = false;
      let localDetailsReleased = false;
      const waitingLocalDetails: (() => void)[] = [];
      root.releaseSettingsLocalDetails = () => {
        localDetailsReleased = true;
        for (const resolve of waitingLocalDetails.splice(0)) resolve();
      };
      let update = (_task: any) => {};
      let snapshotUpdated: ((snapshot: any) => void) | undefined;
      root.settingsCalls = [];
      root.settingsDetailCalls = [];
      root.settingsSnapshotReads = [];
      root.savedSettingsFixture = configs;
      function summary(task: any) {
        return {
          ...task,
          is_summary: true,
          logs: [],
          log_count: 100,
          result: {
            success: task.result.success !== false,
            ...(task.result.message ? { message: task.result.message } : {}),
          },
        };
      }
      function snapshot() {
        const result = structuredClone({
          accounts,
          defaults,
          tasks: tasks.map(summary),
          data_dir: "/fixture",
        });
        root.settingsSnapshotReads.push(result);
        return result;
      }
      function makeTask(accountId: string, action: string, result: any) {
        const task = {
          id: `settings-fixture-${++counter}`,
          account_id: accountId,
          action,
          status: "succeeded",
          stage: "完成",
          created_at: 1000 + counter,
          updated_at: 1000 + counter,
          prompt: null,
          order_submitted: false,
          result,
          logs: ["DETAIL_ONLY_HISTORY_LOG"],
          detail_revision: `revision-${counter}`,
          is_summary: false,
        };
        details.set(task.id, task);
        tasks.unshift(task);
        return task;
      }
      if (!noTaskHistory)
        makeTask("fixture-account-a", "region", {
          success: true,
          region: { state: "旧省份", city: "旧城市", district: "旧地区" },
        });
      // A previous session has already saved these presets, but the Web wire view omits config.
      if (!noTaskHistory)
        for (const id of [local, ...accounts.map((account) => account.id)])
          makeTask(id, "address_read", {
            success: true,
            config: configs()[id],
          });
      root.publishSettingsServerSave = (accountId: string, street: string) => {
        const saved = configs();
        saved[accountId].address = { ...saved[accountId].address, street };
        localStorage.setItem(key, JSON.stringify(saved));
        const task = makeTask(accountId, "address_save", {
          success: true,
          config: saved[accountId],
        });
        update(summary(task));
        snapshotUpdated?.(snapshot());
      };
      root.__DUO_KILLER_TEST__ = {
        snapshot: async () => snapshot(),
        subscribe: async (callback: any, onSnapshot: any) => {
          update = callback;
          snapshotUpdated = onSnapshot;
          if (delayedInitialSnapshot)
            await new Promise((resolve) => setTimeout(resolve, 400));
          onSnapshot?.(snapshot());
          return () => {};
        },
        task: async (accountId: string, taskId: string, options: any = {}) => {
          root.settingsDetailCalls.push({ accountId, taskId, options });
          const task = details.get(taskId);
          if (!task || task.account_id !== accountId)
            throw new Error("任务不属于当前配置范围");
          if (delayedAccountA && accountId === "fixture-account-a")
            await new Promise((resolve) => setTimeout(resolve, 500));
          if (
            delayedLocalDetail &&
            accountId === local &&
            !localDetailsReleased
          )
            await new Promise<void>((resolve) =>
              waitingLocalDetails.push(resolve),
            );
          return structuredClone({
            ...task,
            logs: options.includeLogs ? task.logs : [],
            logs_included: !!options.includeLogs,
          });
        },
        start: async (accountId: string, action: string, params: any = {}) => {
          root.settingsCalls.push({
            accountId,
            action,
            params: structuredClone(params),
          });
          if (!["address_read", "address_save"].includes(action))
            throw new Error(`Unexpected fixture action: ${action}`);
          const saved = configs();
          if (action === "address_save") {
            const { pickup_only, ...patch } = params;
            saved[accountId] = {
              ...saved[accountId],
              ...structuredClone(patch),
            };
            localStorage.setItem(key, JSON.stringify(saved));
          }
          const task = makeTask(accountId, action, {
            success: true,
            config: saved[accountId],
            ...(action === "address_save"
              ? { message: "本地预设已保存。" }
              : {}),
          });
          if (
            failFirstAccountARead &&
            accountId === "fixture-account-a" &&
            action === "address_read" &&
            !accountAReadFailed
          ) {
            accountAReadFailed = true;
            task.status = "failed";
            task.stage = "合成配置读取失败";
            task.result = { success: false, message: "合成配置读取失败" };
          }
          update(summary(task));
          snapshotUpdated?.(snapshot());
          return task.id;
        },
        add: async () => {
          throw new Error("Not allowed");
        },
        remove: async () => {
          throw new Error("Not allowed");
        },
        savePreset: async () => {
          throw new Error("Not needed by this fixture");
        },
        reply: async () => {},
        cancel: async () => {},
      };
    },
    options,
  );
}

async function scope(page: Page, name: "通用" | "甲" | "乙") {
  await page.getByLabel("配置范围", { exact: true }).click();
  await page
    .getByRole("option", {
      name: name === "通用" ? /通用预设/ : name === "甲" ? /账号甲/ : /账号乙/,
    })
    .click();
}

async function expectSaved(
  page: Page,
  name: "通用" | "甲" | "乙",
  overrides: { start?: string; street?: string; mode?: string } = {},
) {
  await expect(page.getByLabel("详细地址", { exact: true })).toHaveValue(
    overrides.street || `${name}保存的街道 100 号`,
  );
  await expect(
    page.getByRole("combobox", { name: "门店选择方式", exact: true }),
  ).toHaveText("预选多店，按顺序择一");
  const list = page.getByLabel("预选自提门店顺序");
  await expect(list.locator("article strong")).toHaveText([
    `1. Apple ${name}第一店`,
    `2. Apple ${name}第二店`,
  ]);
  await expect(
    page.getByRole("combobox", { name: "自提时段规则", exact: true }),
  ).toHaveText("指定时间窗口");
  await expect(page.getByLabel("可接受开始时间", { exact: true })).toHaveValue(
    overrides.start ||
      (name === "通用" ? "10:00" : name === "甲" ? "11:00" : "12:00"),
  );
  await expect(page.getByLabel("可接受结束时间", { exact: true })).toHaveValue(
    "18:00",
  );
  await expect(
    page.getByRole("combobox", { name: "取货联系人来源", exact: true }),
  ).toHaveText(
    (overrides.mode || (name === "乙" ? "local" : "official")) === "local"
      ? "使用本地取货联系人"
      : "优先使用本次官网保存的联系人",
  );
  await expect(page.getByLabel("姓氏", { exact: true })).toHaveValue(name);
  await expect(page.getByLabel("省 / 直辖市", { exact: true })).toHaveValue(
    "上海",
  );
  await expect(page.getByLabel("城市", { exact: true })).toHaveValue("上海");
  await expect(page.getByLabel("区 / 县", { exact: true })).toHaveValue(
    "测试区",
  );
}

test("轻量 Web 快照首次进入本地配置通过单项详情恢复已保存地址与自提偏好", async ({
  page,
}) => {
  await fixture(page);
  await page.goto("/#/settings", { waitUntil: "domcontentloaded" });
  await expectSaved(page, "通用");
  const reads = await page.evaluate(
    () => (window as any).settingsSnapshotReads,
  );
  expect(
    reads.every((snapshot: any) =>
      snapshot.tasks.every(
        (task: any) =>
          task.is_summary && !task.result.config && task.logs.length === 0,
      ),
    ),
  ).toBe(true);
  const detailCalls = await page.evaluate(
    () => (window as any).settingsDetailCalls,
  );
  expect(detailCalls.some((call: any) => call.accountId === LOCAL)).toBe(true);
  expect(
    detailCalls.every((call: any) => call.options.includeLogs === false),
  ).toBe(true);
});

test("保存后切页和刷新均从按需详情恢复自提时段与送货地址", async ({ page }) => {
  await fixture(page);
  await page.goto("/#/settings", { waitUntil: "domcontentloaded" });
  await expectSaved(page, "通用");
  await page.getByLabel("可接受开始时间", { exact: true }).fill("13:30");
  await page.getByRole("button", { name: "保存自提预设", exact: true }).click();
  await expect
    .poll(() =>
      page.evaluate(
        (id) =>
          (window as any).savedSettingsFixture()[id].pickup.time_slot_rule
            .start,
        LOCAL,
      ),
    )
    .toBe("13:30");
  await page.getByLabel("详细地址", { exact: true }).fill("通用新街道 202 号");
  await page.getByRole("button", { name: "保存地址预设", exact: true }).click();
  await expect
    .poll(() =>
      page.evaluate(
        (id) => (window as any).savedSettingsFixture()[id].address.street,
        LOCAL,
      ),
    )
    .toBe("通用新街道 202 号");
  await page.getByRole("link", { name: "账号工作台", exact: true }).click();
  await page.getByRole("link", { name: "本地配置", exact: true }).click();
  await expectSaved(page, "通用", {
    start: "13:30",
    street: "通用新街道 202 号",
  });
  await page.reload({ waitUntil: "domcontentloaded" });
  await expectSaved(page, "通用", {
    start: "13:30",
    street: "通用新街道 202 号",
  });
  expect(
    await page.evaluate(() =>
      (window as any).settingsCalls.every((call: any) =>
        ["address_read", "address_save"].includes(call.action),
      ),
    ),
  ).toBe(true);
});

test("账号与通用配置范围切换恢复各自保存值，迟到详情不会串入另一账号", async ({
  page,
}) => {
  await fixture(page, { delayedAccountA: true });
  await page.goto("/#/settings", { waitUntil: "domcontentloaded" });
  await expectSaved(page, "通用");
  await scope(page, "甲");
  await expectSaved(page, "甲");
  await scope(page, "乙");
  await expectSaved(page, "乙");
  await expect(page.getByLabel("取货人姓氏", { exact: true })).toHaveValue(
    "乙",
  );
  await expect(page.getByLabel("取货手机号", { exact: true })).toHaveValue(
    "13800138002",
  );
  await scope(page, "通用");
  await expectSaved(page, "通用");
  await scope(page, "甲");
  await scope(page, "乙");
  await page.waitForTimeout(700);
  await expectSaved(page, "乙");
  const calls = await page.evaluate(() => (window as any).settingsDetailCalls);
  expect(calls.some((call: any) => call.accountId === ACCOUNT_A)).toBe(true);
  expect(calls.some((call: any) => call.accountId === ACCOUNT_B)).toBe(true);
});

test("配置详情读取期间和读取失败时禁止保存，显式重试成功后恢复", async ({
  page,
}) => {
  await fixture(page, {
    delayedLocalDetail: true,
    failFirstAccountARead: true,
  });
  await page.goto("/#/settings", { waitUntil: "domcontentloaded" });
  await expect(
    page.getByRole("button", { name: "保存地址预设", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "保存自提预设", exact: true }),
  ).toBeDisabled();
  await expect(page.getByLabel("详细地址", { exact: true })).toBeDisabled();
  await page.evaluate(() => (window as any).releaseSettingsLocalDetails());
  await expectSaved(page, "通用");
  await expect(
    page.getByRole("button", { name: "保存地址预设", exact: true }),
  ).toBeEnabled();
  await scope(page, "甲");
  await expect(
    page.getByRole("alert").filter({ hasText: "本地配置读取失败，保存已暂停" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "保存地址预设", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "保存自提预设", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByRole("button", {
      name: "监控预选门店，有货自动下单",
      exact: true,
    }),
  ).toBeDisabled();
  await page
    .getByRole("button", { name: "重新读取本地配置", exact: true })
    .click();
  await expectSaved(page, "甲");
  await expect(
    page.getByRole("button", { name: "保存地址预设", exact: true }),
  ).toBeEnabled();
  await expect(
    page.getByRole("button", { name: "保存自提预设", exact: true }),
  ).toBeEnabled();
  const writes = await page.evaluate(() =>
    (window as any).settingsCalls.filter(
      (call: any) => call.action === "address_save",
    ),
  );
  expect(writes).toEqual([]);
});

test("单账号冷启动等待服务端快照就绪，刷新和从工作台进入选择同一账号范围", async ({
  page,
}) => {
  await fixture(page, { singleAccount: true, delayedInitialSnapshot: true });
  await page.goto("/#/settings", { waitUntil: "domcontentloaded" });
  await expectSaved(page, "甲");
  await expect(
    page.getByRole("combobox", { name: "配置范围", exact: true }),
  ).toContainText("账号甲");
  expect(
    await page.evaluate(() =>
      (window as any).settingsCalls.every(
        (call: any) => call.accountId === "fixture-account-a",
      ),
    ),
  ).toBe(true);
  await page.reload({ waitUntil: "domcontentloaded" });
  await expectSaved(page, "甲");
  await page.getByRole("link", { name: "账号工作台", exact: true }).click();
  await page.getByRole("link", { name: "本地配置", exact: true }).click();
  await expectSaved(page, "甲");
});

test("没有历史任务也读取服务端配置，不能把缺少摘要当成空配置", async ({
  page,
}) => {
  await fixture(page, { singleAccount: true, noTaskHistory: true });
  await page.goto("/#/settings", { waitUntil: "domcontentloaded" });
  await expectSaved(page, "甲");
  expect(
    await page.evaluate(() =>
      (window as any).settingsCalls
        .filter((call: any) => call.action === "address_read")
        .map((call: any) => call.accountId),
    ),
  ).toEqual(["fixture-account-a"]);
});

test("真实 Web bridge 在空浏览器存储下从服务端接口恢复配置，保存后原地刷新仍一致", async ({
  page,
}) => {
  // The fixture database lives in this test process, outside the browser. This
  // exercises auth, workspace changes, start_task and task_detail HTTP paths.
  const calls: { path: string; body: any }[] = [];
  const tasks: any[] = [];
  const details = new Map<string, any>();
  let counter = 0;
  let config: any = {
    contact: {
      lastName: "甲",
      firstName: "测试",
      emailAddress: "fixture01@example.test",
      fullDaytimePhone: "13800138001",
    },
    delivery: { state: "上海", city: "上海", district: "测试区" },
    address: { street: "甲保存的街道 100 号" },
    pickup: {
      selection_mode: "priority",
      search: "201101",
      store_id: "R011",
      store_name: "Apple 甲第一店",
      stores: [
        { store_id: "R011", store_name: "Apple 甲第一店", search: "201101" },
        { store_id: "R012", store_name: "Apple 甲第二店", search: "201102" },
      ],
      contact_mode: "official",
      time_slot_rule: {
        mode: "window",
        start: "11:00",
        end: "18:00",
        fallback: "stop",
      },
    },
  };
  const account = {
    id: ACCOUNT_A,
    name: "账号甲",
    email: "a@example.test",
    pending: false,
    preset_sku: "MLH74FE",
    preset_quantity: 1,
  };
  function snapshot() {
    return {
      accounts: [account],
      defaults: { id: LOCAL, name: "通用预设", email: "" },
      tasks,
      data_dir: "/fixture-server",
    };
  }
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    const body = route.request().postDataJSON() || {};
    calls.push({ path, body });
    const fulfill = (json: any, status = 200) =>
      route.fulfill({
        status,
        contentType: "application/json",
        body: JSON.stringify(json),
      });
    if (path === "/api/auth/status") return fulfill({ authenticated: true });
    if (path === "/api/workspace_changes")
      return fulfill({
        cursor: `fixture:${counter}`,
        reset: true,
        snapshot: snapshot(),
        tasks: [],
        removed_task_ids: [],
      });
    if (path === "/api/workspace_snapshot") return fulfill(snapshot());
    if (path === "/api/task_detail") {
      const detail = details.get(body.taskId);
      expect(body.accountId).toBe(ACCOUNT_A);
      expect(body.includeLogs).toBe(false);
      return fulfill(detail);
    }
    if (path === "/api/start_task") {
      expect(body.accountId).toBe(ACCOUNT_A);
      expect(["address_read", "address_save"]).toContain(body.action);
      if (body.action === "address_save") {
        const { pickup_only, ...patch } = body.params;
        config = { ...config, ...structuredClone(patch) };
      }
      const task = {
        id: `http-settings-${++counter}`,
        account_id: ACCOUNT_A,
        action: body.action,
        status: "succeeded",
        stage: "完成",
        created_at: 1000 + counter,
        updated_at: 1000 + counter,
        prompt: null,
        order_submitted: false,
        logs: [],
        logs_included: false,
        result: { success: true, config: structuredClone(config) },
        detail_revision: `http-r${counter}`,
        is_summary: false,
      };
      details.set(task.id, task);
      tasks.unshift({
        ...task,
        is_summary: true,
        result: { success: true },
        logs: [],
      });
      return fulfill(task.id);
    }
    return fulfill({ error: "Unexpected fixture request" }, 400);
  });
  await page.goto("/#/settings", { waitUntil: "domcontentloaded" });
  await expectSaved(page, "甲");
  expect(await page.evaluate(() => localStorage.length)).toBe(0);
  await page
    .getByLabel("详细地址", { exact: true })
    .fill("服务端保存的新地址 171 号");
  await page.getByRole("button", { name: "保存地址预设", exact: true }).click();
  await expect
    .poll(() => config.address.street)
    .toBe("服务端保存的新地址 171 号");
  await page.evaluate(() => {
    localStorage.clear();
    sessionStorage.clear();
  });
  await page.reload({ waitUntil: "domcontentloaded" });
  await expectSaved(page, "甲", { street: "服务端保存的新地址 171 号" });
  await page.getByRole("link", { name: "账号工作台", exact: true }).click();
  await page.getByRole("link", { name: "本地配置", exact: true }).click();
  await expectSaved(page, "甲", { street: "服务端保存的新地址 171 号" });
  expect(calls.some((call) => call.path === "/api/task_detail")).toBe(true);
  expect(calls.some((call) => call.path === "/api/snapshot")).toBe(false);
  expect(
    calls.filter(
      (call) =>
        call.path === "/api/start_task" && call.body.action === "address_save",
    ),
  ).toHaveLength(1);
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth),
  ).toBeLessThanOrEqual(390);
  await page.screenshot({
    path: "artifacts/goal-017-settings-http-mobile.png",
    fullPage: true,
  });
});

test("旧配置详情迟到时不能覆盖刚保存的新配置", async ({ page }) => {
  await fixture(page, { delayedLocalDetail: true });
  await page.goto("/#/settings", { waitUntil: "domcontentloaded" });
  await expect
    .poll(() => page.evaluate(() => (window as any).settingsDetailCalls.length))
    .toBeGreaterThan(0);
  await page.evaluate((id) => {
    const root = window as any;
    root.publishSettingsServerSave(id, "刚保存的新地址 177 号");
    root.releaseSettingsLocalDetails();
  }, LOCAL);
  await expectSaved(page, "通用", { street: "刚保存的新地址 177 号" });
  await page.waitForTimeout(500);
  await expect(page.getByLabel("详细地址", { exact: true })).toHaveValue(
    "刚保存的新地址 177 号",
  );
  expect(
    await page.evaluate(() =>
      (window as any).settingsCalls.filter(
        (call: any) => call.action === "address_save",
      ),
    ),
  ).toEqual([]);
});
