import { expect, test, type Page } from "@playwright/test";
import { mkdir, writeFile } from "node:fs/promises";
import { chooseOption } from "./select-option";

async function fixture(page: Page, scenario = "normal") {
  await page.route("**/api/**", (route) =>
    route.fulfill({ json: { authenticated: true, success: true } }),
  );
  await page.addInitScript(
    ({ scenario }) => {
      const root = window as any;
      const accounts = [
        { id: "a", name: "日常账号", email: "first@example.test" },
        { id: "b", name: "备用账号", email: "second@example.test" },
      ];
      root.enqueueConfigs = {
        a: {
          pickup: {
            search: "200000",
            store_name: "Apple 测试一店",
            store_id: "R001",
            id_last4: "123X",
            contact_mode: "local",
            stores: [
              {
                store_id: "R001",
                store_name: "Apple 测试一店",
                search: "200000",
              },
              {
                store_id: "R002",
                store_name: "Apple 测试二店",
                search: "200001",
              },
            ],
            time_slot_rule: {
              mode: "window",
              start: "10:30",
              end: "15:00",
              fallback: "stop",
            },
          },
          contact: { firstName: "Synthetic", fullDaytimePhone: "00000000000" },
        },
        b: {
          pickup: {
            selection_mode: "priority",
            contact_mode: "official",
            stores: [
              {
                store_id: "R009",
                store_name: "Apple 备用门店",
                search: "200009",
              },
            ],
            time_slot_rule: { mode: "earliest" },
          },
        },
      };
      if (scenario === "empty") root.enqueueConfigs.a.pickup = {};
      root.enqueueFailReads = scenario === "failure";
      root.enqueueHoldA = scenario === "late";
      root.enqueueHoldSave = scenario === "saving";
      root.enqueueCalls = [];
      root.enqueueReadObservations = [];
      root.enqueueQueueCalls = [];
      const tasks: any[] = [];
      let listener = (_task: any) => {};
      let sequence = 0;
      const finish = (task: any) => {
        task.status = "succeeded";
        task.result = task.fixtureResult;
        delete task.fixtureResult;
        listener(structuredClone(task));
      };
      root.enqueueRelease = (action: string, account: string) => {
        const task = tasks.find(
          (task) =>
            task.action === action &&
            task.account_id === account &&
            task.status === "running",
        );
        if (task) finish(task);
      };
      if (scenario === "busy")
        tasks.push({
          id: "busy-login",
          account_id: "a",
          action: "login",
          status: "running",
          stage: "等待官网",
          result: null,
          created_at: Date.now() / 1000,
          updated_at: Date.now() / 1000,
          logs: [],
          prompt: null,
          order_submitted: false,
          fixtureResult: { success: true },
        });
      root.__DUO_KILLER_TEST__ = {
        snapshot: async () =>
          structuredClone({
            accounts,
            tasks,
            data_dir: "/synthetic-enqueue-policy",
          }),
        subscribe: async (callback: any) => {
          listener = callback;
          return () => {};
        },
        task: async (account: string, id: string) => {
          const task = tasks.find(
            (task) => task.id === id && task.account_id === account,
          );
          if (task?.action === "address_read" && task.status === "succeeded")
            root.enqueueReadObservations.push(account);
          return structuredClone(task);
        },
        start: async (account: string, action: string, params: any = {}) => {
          root.enqueueCalls.push({
            account,
            action,
            params: structuredClone(params),
          });
          const task: any = {
            id: `config-${++sequence}`,
            account_id: account,
            action,
            status: "succeeded",
            stage: "fixture config",
            result: null,
            created_at: Date.now() / 1000,
            updated_at: Date.now() / 1000,
            logs: [],
            prompt: null,
            order_submitted: false,
          };
          if (action === "address_read") {
            if (root.enqueueFailReads) {
              task.status = "failed";
              task.stage = "fixture read failure";
            } else
              task.result = {
                success: true,
                config: structuredClone(root.enqueueConfigs[account]),
              };
            if (root.enqueueHoldA && account === "a") {
              task.fixtureResult = task.result;
              task.result = null;
              task.status = "running";
            }
          } else if (action === "address_save") {
            root.enqueueConfigs[account].pickup = structuredClone(
              params.pickup,
            );
            task.result = {
              success: true,
              config: structuredClone(root.enqueueConfigs[account]),
            };
            if (root.enqueueHoldSave) {
              task.fixtureResult = task.result;
              task.result = null;
              task.status = "running";
            }
          } else {
            task.status = "failed";
            task.stage = "Unexpected fixture start";
          }
          tasks.unshift(task);
          listener(structuredClone(task));
          return task.id;
        },
        queue: async (operation: string) => {
          root.enqueueQueueCalls.push(operation);
          return {
            mode: "sequential",
            max_concurrency: 1,
            jobs: [],
            paused: true,
          };
        },
        maintenance: async () => ({ enabled: false, last_runs: [] }),
        logs: async () => ({ entries: [], has_more: false }),
        browser: async () => ({ active: false }),
      };
    },
    { scenario },
  );
  await page.goto("/#/queue", { waitUntil: "domcontentloaded" });
  const panel = page.locator(".queue-enqueue-policy");
  await expect(panel).toBeVisible();
  expect(await page.evaluate(() => (window as any).enqueueCalls)).toEqual([]);
  await panel.locator("summary").click();
  return panel;
}

test("legacy actual mode remains fixed and save preserves complete pickup without unrelated side effects", async ({
  page,
}) => {
  const panel = await fixture(page);
  await expect(panel.locator(".queue-enqueue-current")).toContainText(
    "各监控门店分别入队",
  );
  await chooseOption(
    panel.getByRole("combobox", { name: "账号入队方式" }),
    "预选多店，按顺序择一（默认）",
  );
  const before = await page.evaluate(() =>
    structuredClone((window as any).enqueueConfigs.a.pickup),
  );
  await panel.getByRole("button", { name: "保存入队方式" }).click();
  await expect(panel.getByRole("status")).toContainText("入队方式已保存");
  const calls = await page.evaluate(() => (window as any).enqueueCalls);
  expect(calls.map((call: any) => call.action)).toEqual([
    "address_read",
    "address_read",
    "address_save",
  ]);
  expect(calls[2]).toEqual({
    account: "a",
    action: "address_save",
    params: {
      pickup_only: true,
      pickup: { ...before, selection_mode: "priority" },
    },
  });
  expect(calls[2].params).not.toHaveProperty("contact");
  expect(
    await page.evaluate(() =>
      (window as any).enqueueQueueCalls.filter(
        (operation: string) => operation !== "status",
      ),
    ),
  ).toEqual([]);
});

test("new empty config recommends priority but cannot save without authorized stores", async ({
  page,
}) => {
  const panel = await fixture(page, "empty");
  await expect(panel.locator(".queue-enqueue-current")).toContainText(
    "尚未配置",
  );
  await expect(
    panel.getByRole("combobox", { name: "账号入队方式" }),
  ).toHaveText("预选多店，按顺序择一（默认）");
  await expect(
    panel.getByRole("button", { name: "保存入队方式" }),
  ).toBeDisabled();
  await expect(
    panel.getByRole("button", { name: "配置门店与顺序" }),
  ).toBeEnabled();
  await chooseOption(
    panel.getByRole("combobox", { name: "账号入队方式" }),
    "各监控门店分别入队（兼容）",
  );
  await expect(
    panel.getByRole("button", { name: "保存入队方式" }),
  ).toBeDisabled();
  expect(
    await page.evaluate(() =>
      (window as any).enqueueCalls.some(
        (call: any) => call.action === "address_save",
      ),
    ),
  ).toBe(false);
});

test("failed reads disable save and support explicit retry", async ({
  page,
}) => {
  const panel = await fixture(page, "failure");
  await expect(panel.getByRole("alert")).toContainText("fixture read failure");
  await expect(
    panel.getByRole("button", { name: "保存入队方式" }),
  ).toBeDisabled();
  await page.evaluate(() => {
    (window as any).enqueueFailReads = false;
  });
  await panel.getByRole("button", { name: "重新读取入队配置" }).click();
  await expect(panel.locator(".queue-enqueue-current")).toContainText(
    "各监控门店分别入队",
  );
  await expect(
    panel.getByRole("combobox", { name: "账号入队方式" }),
  ).toBeEnabled();
});

test("late read from another account cannot replace the selected account policy", async ({
  page,
}) => {
  const panel = await fixture(page, "late");
  await expect
    .poll(() => page.evaluate(() => (window as any).enqueueCalls.length))
    .toBe(1);
  await chooseOption(
    panel.getByRole("combobox", { name: "入队策略账号" }),
    "备用账号 · seco**@example.test",
  );
  await expect(panel.locator(".queue-enqueue-current")).toContainText(
    "预选多店",
  );
  await expect(panel).toContainText("Apple 备用门店");
  await page.evaluate(() => {
    (window as any).enqueueHoldA = false;
    (window as any).enqueueRelease("address_read", "a");
  });
  await expect
    .poll(() => page.evaluate(() => (window as any).enqueueReadObservations))
    .toContain("a");
  await expect(panel).toContainText("Apple 备用门店");
  await expect(panel).not.toContainText("Apple 测试一店");
});

test("save preflight reloads an externally changed pickup and requires a new confirmation", async ({
  page,
}) => {
  const panel = await fixture(page);
  await expect(panel.locator(".queue-enqueue-current")).toContainText(
    "各监控门店分别入队",
  );
  await chooseOption(
    panel.getByRole("combobox", { name: "账号入队方式" }),
    "预选多店，按顺序择一（默认）",
  );
  await page.evaluate(() => {
    (window as any).enqueueConfigs.a.pickup.time_slot_rule.mode = "latest";
  });
  await panel.getByRole("button", { name: "保存入队方式" }).click();
  await expect(panel.getByRole("status")).toContainText("配置已更新");
  expect(
    await page.evaluate(() =>
      (window as any).enqueueCalls.some(
        (call: any) => call.action === "address_save",
      ),
    ),
  ).toBe(false);
  await expect(
    panel.getByRole("combobox", { name: "账号入队方式" }),
  ).toHaveText("各监控门店分别入队（兼容）");
});

test("busy account defers reads and save locks account switching while retaining candidate stores", async ({
  page,
}) => {
  const panel = await fixture(page, "busy");
  await expect(panel.getByRole("status")).toContainText("账号有任务进行中");
  expect(await page.evaluate(() => (window as any).enqueueCalls)).toEqual([]);
  await page.evaluate(() => (window as any).enqueueRelease("login", "a"));
  await expect(panel.locator(".queue-enqueue-current")).toContainText(
    "各监控门店分别入队",
  );
  await chooseOption(
    panel.getByRole("combobox", { name: "入队策略账号" }),
    "备用账号 · seco**@example.test",
  );
  await expect(panel).toContainText("Apple 备用门店");
  await chooseOption(
    panel.getByRole("combobox", { name: "账号入队方式" }),
    "各监控门店分别入队（兼容）",
  );
  await expect(panel).toContainText("手动与定时自提固定门店：Apple 备用门店");
  await page.evaluate(() => {
    (window as any).enqueueHoldSave = true;
  });
  await panel.getByRole("button", { name: "保存入队方式" }).click();
  await expect(
    panel.getByRole("combobox", { name: "入队策略账号" }),
  ).toBeDisabled();
  await page.evaluate(() =>
    (window as any).enqueueRelease("address_save", "b"),
  );
  await expect(panel.getByRole("status")).toContainText("入队方式已保存");
  const payload = await page.evaluate(
    () =>
      (window as any).enqueueCalls.find(
        (call: any) => call.action === "address_save",
      ).params.pickup,
  );
  expect(payload).toMatchObject({
    selection_mode: "fixed",
    store_id: "R009",
    store_name: "Apple 备用门店",
    stores: [
      { store_id: "R009", store_name: "Apple 备用门店", search: "200009" },
    ],
  });
});

test("enqueue policy panel and long option menu fit desktop and touch layouts", async ({
  browser,
}) => {
  test.setTimeout(60000);
  const directory = "artifacts/queue-enqueue-policy";
  await mkdir(directory, { recursive: true });
  const diagnostics: unknown[] = [];
  for (const width of [1180, 390, 320]) {
    const context = await browser.newContext({
      viewport: { width, height: 844 },
      hasTouch: width < 650,
      isMobile: width < 650,
      reducedMotion: "reduce",
    });
    const page = await context.newPage();
    try {
      const panel = await fixture(page);
      await expect(panel.locator(".queue-enqueue-current")).toContainText(
        "各监控门店分别入队",
      );
      const overflow = () =>
        page.evaluate(() => document.documentElement.scrollWidth - innerWidth);
      await expect.poll(overflow).toBeLessThanOrEqual(1);
      if (width < 650) {
        const targets = await panel.locator("button").evaluateAll((buttons) =>
          buttons.map((button) => ({
            label: button.getAttribute("aria-label") || button.textContent,
            height: button.getBoundingClientRect().height,
            width: button.getBoundingClientRect().width,
          })),
        );
        for (const target of targets) {
          expect(
            target.height,
            `${width}px touch target ${target.label}`,
          ).toBeGreaterThanOrEqual(44);
          expect(
            target.width,
            `${width}px touch target ${target.label}`,
          ).toBeGreaterThanOrEqual(44);
        }
      }
      await page.screenshot({
        path: `${directory}/${width}-expanded.png`,
        fullPage: true,
      });
      const field = panel.getByRole("combobox", { name: "账号入队方式" });
      await field.scrollIntoViewIfNeeded();
      if (width < 650) await field.tap();
      else await field.click();
      const menu = page.getByRole("listbox", { name: "账号入队方式" });
      await expect(menu).toBeVisible();
      await expect(
        page.getByRole("option", {
          name: "预选多店，按顺序择一（默认）",
          exact: true,
        }),
      ).toBeVisible();
      await expect(
        page.getByRole("option", {
          name: "各监控门店分别入队（兼容）",
          exact: true,
        }),
      ).toBeVisible();
      const menuRect = await menu.boundingBox();
      expect(menuRect).not.toBeNull();
      expect(menuRect!.x).toBeGreaterThanOrEqual(8);
      expect(menuRect!.y).toBeGreaterThanOrEqual(0);
      expect(menuRect!.x + menuRect!.width).toBeLessThanOrEqual(width - 8);
      expect(menuRect!.y + menuRect!.height).toBeLessThanOrEqual(844);
      const options = await menu.getByRole("option").evaluateAll((rows) =>
        rows.map((row) => {
          const text = row.querySelector("span")!;
          return {
            text: text.textContent,
            height: row.getBoundingClientRect().height,
            contentWidth: text.scrollWidth,
            visibleWidth: text.clientWidth,
            whiteSpace: getComputedStyle(text).whiteSpace,
            overflowWrap: getComputedStyle(text).overflowWrap,
          };
        }),
      );
      for (const option of options) {
        expect(
          option.contentWidth,
          `${width}px option ${option.text}`,
        ).toBeLessThanOrEqual(option.visibleWidth + 1);
        expect(option.whiteSpace).not.toBe("nowrap");
        if (width < 650) expect(option.height).toBeGreaterThanOrEqual(44);
      }
      await expect.poll(overflow).toBeLessThanOrEqual(1);
      await page.screenshot({
        path: `${directory}/${width}-mode-dropdown.png`,
      });
      diagnostics.push({
        width,
        menu: menuRect,
        options,
        overflow: await overflow(),
      });
    } finally {
      await context.close();
    }
  }
  await writeFile(
    `${directory}/layout-metrics.json`,
    JSON.stringify(diagnostics, null, 2),
  );
});
