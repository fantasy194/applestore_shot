import { test, expect, type Page } from "@playwright/test";

// Synthetic API fixture. Never invokes native commands or Apple endpoints.
async function fixture(
  page: Page,
  withAccounts = true,
  firstEmail = "first@example.test",
) {
  await page.addInitScript(
    ({ withAccounts, firstEmail }) => {
      const accounts = withAccounts
        ? [
            { id: "a", name: "日常账号", email: firstEmail },
            { id: "b", name: "备用账号", email: "second@example.test" },
          ]
        : [];
      const tasks: any[] = [];
      let callback = (task: any) => {};
      let counter = 0;
      const update = (task: any) => {
        if (task.result?.account_removed) {
          const index = accounts.findIndex((a) => a.id === task.account_id);
          if (index >= 0) accounts.splice(index, 1);
        }
        callback(structuredClone(task));
      };
      const root = window as any;
      root.sent = [];
      root.started = [];
      root.removed = [];
      root.finish = (id: string, changes: any) => {
        const t = tasks.find((t) => t.id === id);
        Object.assign(t, changes);
        update(t);
      };
      root.__DUO_KILLER_TEST__ = {
        remove: async (accountId: string) => {
          if (root.removeError) throw new Error(root.removeError);
          if (
            tasks.some(
              (t) =>
                t.account_id === accountId &&
                ["waiting", "running", "queued"].includes(t.status),
            )
          )
            throw new Error("该账号仍有任务执行，请完成或取消后再移除");
          root.removed.push(accountId);
          const index = accounts.findIndex((a) => a.id === accountId);
          if (index >= 0) accounts.splice(index, 1);
          for (let i = tasks.length - 1; i >= 0; i--)
            if (tasks[i].account_id === accountId) tasks.splice(i, 1);
        },
        snapshot: async () =>
          structuredClone({
            accounts,
            tasks,
            data_dir: "/fixture",
          }),
        savePreset: async (
          accountId: string,
          sku: string,
          quantity: number,
        ) => {
          const account = accounts.find((a) => a.id === accountId)!;
          Object.assign(account, {
            preset_sku: sku,
            preset_quantity: quantity,
          });
          return structuredClone(account);
        },
        add: async (email: string) => {
          if (accounts.some((a) => a.email === email))
            throw new Error("这个账号已添加");
          const a = {
            id: "new-" + ++counter,
            name: email,
            email,
            pending: true,
          };
          accounts.push(a);
          return a;
        },
        subscribe: async (cb: any) => {
          callback = cb;
          return () => {};
        },
        start: async (accountId: string, action: string, params: any) => {
          const id = "task-" + ++counter;
          const task: any = {
            id,
            account_id: accountId,
            action,
            status: "running",
            stage: "测试阶段",
            created_at: Date.now() / 1000,
            updated_at: Date.now() / 1000,
            prompt: null,
            result: null,
            order_submitted: false,
            logs: [],
          };
          tasks.unshift(task);
          root.started.push({ accountId, action, id, params });
          if (action === "login" && params.warm_only === true) {
            task.status = "succeeded";
            task.result = { warm_login: true, message: "登录预热完成，未加购、未下单。" };
          } else if (
            action === "login" ||
            action === "enroll" ||
            (action === "me" && root.waitForMe)
          ) {
            task.status = "waiting";
            task.prompt = {
              id: "p-" + id,
              kind: "password",
              label: "输入 Apple 账号密码",
            };
          } else if (action === "cancel_order") {
            task.status = "waiting";
            task.prompt = { id: "cancel-" + id, kind: "confirm", label: "确认取消订单 " + params.order_number,
              details: { operation: "cancel_order", order_number: params.order_number, items: [{ name: "抛光布", quantity: 1, subtotal: "RMB 69" }], total: "RMB 69" } };
          } else if (action === "purchase" && params.prepare_only === true) {
            task.status = "succeeded";
            task.result = { success: true, status: "prepared", message: "准备完成，未创建订单。" };
          } else if (action === "purchase") {
            task.status = "waiting";
            task.prompt = {
              id: "p-" + id,
              kind: "confirm",
              label: "确认商品与金额，创建订单",
              details: {
                items: [
                  {
                    name: "测试商品",
                    sku: params.sku,
                    quantity: params.quantity,
                    subtotal: "RMB 138",
                  },
                ],
                total: "RMB 138",
                address: "测试地址",
              },
            };
          } else if (action === "orders") {
            task.status = "succeeded";
            task.result = {
              success: true,
              orders: [
                {
                  order_number: "TEST001",
                  items: [
                    {
                      name: "测试商品",
                      quantity: 2,
                      delivery_info: "我们正在等待你的付款",
                    },
                  ],
                },
              ],
              message: "订单列表读取完成。",
            };
          } else if (action === "checkout") {
            task.status = "waiting";
            task.prompt = {
              id: "p-" + id,
              kind: "select",
              label: "收货方式",
              default: "HOME",
              options: [
                { value: "HOME", label: "为我送货" },
                { value: "RETAIL", label: "我要自提" },
              ],
            };
          } else if (action === "address_read") {
            task.status = "succeeded";
            task.result = { success: true, config: {} };
          } else if (action === "catalog") {
            task.status = root.catalogFails ? "failed" : "succeeded";
            task.stage = root.catalogFails
              ? "商品页查询失败：HTTP 541"
              : "查询完成";
            task.result = root.catalogFails
              ? null
              : {
                  success: true,
                  queried_at: new Date().toISOString(),
                  items: [
                    {
                      sku: "MK2P4CH/A",
                      name: "iPhone Duo 512GB Star White",
                      price: 17999,
                      can_add_to_bag: false,
                      coming_soon: true,
                      reason: "即将发售",
                    },
                    {
                      sku: "MK2M4CH/A",
                      name: "iPhone Duo 256GB Star White",
                      price: 21499,
                      can_add_to_bag: null,
                      coming_soon: false,
                    },
                  ],
                };
          } else if (action === "me") {
            task.status = "succeeded";
            task.result = {
              success: true,
              logged_in: true,
              name:
                root.meName ?? accounts.find((a) => a.id === accountId)?.name,
              scope: "account",
              apple_id: accounts.find((a) => a.id === accountId)?.email,
            };
          } else {
            task.status = "succeeded";
            task.result = { success: true };
          }
          if (action === "me" && task.result?.logged_in) {
            const a = accounts.find((a) => a.id === accountId)!;
            a.name = task.result.name?.trim() || a.email;
          }
          update(task);
          return id;
        },
        reply: async (
          accountId: string,
          taskId: string,
          requestId: string,
          value: any,
        ) => {
          root.sent.push({ accountId, taskId, requestId, value });
          const task = tasks.find((t) => t.id === taskId);
          if (task.prompt.kind === "password") {
            task.prompt = {
              id: "code-" + task.id,
              kind: "code",
              label: "设备六位验证码：",
              actions: [{ value: "s", label: "改用短信验证码" }],
            };
            task.status = "waiting";
            update(task); // A new prompt can arrive before the previous invoke resolves.
          } else if (
            task.prompt.kind === "code" &&
            ["s", "d"].includes(value)
          ) {
            task.prompt = {
              id: "code-" + value + "-" + task.id,
              kind: "code",
              label: value === "s" ? "短信六位验证码：" : "设备六位验证码：",
              actions:
                value === "s" ? [{ value: "d", label: "改用设备验证码" }] : [],
            };
            update(task);
          } else if (task.action === "enroll" || task.action === "me") {
            const a = accounts.find((a) => a.id === accountId)!;
            Object.assign(a, { pending: false, name: "接口昵称" });
            task.status = "succeeded";
            task.prompt = null;
            task.result = {
              success: true,
              logged_in: true,
              scope: "account",
              name: a.name,
              apple_id: a.email,
            };
            update(task);
          } else if (task.action === "cancel_order") {
            task.status = "succeeded";
            task.prompt = null;
            task.result = { success: value === true, status: value === true ? "order_cancelled" : "cancellation_aborted",
              order_number: root.started.find((t: any) => t.id === taskId).params.order_number,
              message: value === true ? "官网已确认订单取消。" : "已放弃取消，原订单保留。" };
            update(task);
          } else {
            task.status = "succeeded";
            task.prompt = null;
            task.result =
              task.action === "purchase" && value === true
                ? {
                    success: true,
                    status: "pending_payment",
                    order_number: "TEST001",
                    amount_due: "RMB 138",
                  }
                : { success: true };
            update(task);
          }
        },
        cancel: async (accountId: string, taskId: string) => {
          const t = tasks.find((t) => t.id === taskId);
          t.status = "cancelled";
          t.prompt = null;
          update(t);
        },
      };
    },
    { withAccounts, firstEmail },
  );
}

async function enterWorkspace(page: Page) {
  await page.getByRole("checkbox", { name: /使用 日常账号/ }).check();
  await page.getByRole("checkbox", { name: /使用 备用账号/ }).check();
  await page.getByRole("button", { name: "检查并使用所选账号" }).click();
  await expect(page.getByRole("button", { name: "立即下单" })).toBeVisible();
}

test("reopening a single enrolled account keeps its preset without another login task", async ({ page }) => {
  await fixture(page);
  await page.addInitScript(async () => {
    const api = (window as any).__DUO_KILLER_TEST__;
    await api.remove("b");
    await api.savePreset("a", "MK2M4CH", 1);
  });
  await page.goto("/");
  await expect(page.getByRole("button", { name: "立即下单" })).toBeVisible();
  await expect(page.getByRole("combobox", { name: "预制商品" })).toContainText("MK2M4CH");
  await expect(page.getByLabel("数量", { exact: true })).toHaveValue("1");
  expect(await page.evaluate(() => (window as any).started)).toHaveLength(0);
  await page.reload();
  await expect(page.getByRole("button", { name: "立即下单" })).toBeVisible();
  await expect(page.getByRole("combobox", { name: "预制商品" })).toContainText("MK2M4CH");
  expect(await page.evaluate(() => (window as any).started)).toHaveLength(0);
});

test("prepare button requests preparation without opening order confirmation", async ({ page }) => {
  await fixture(page);
  await page.addInitScript(async () => {
    await (window as any).__DUO_KILLER_TEST__.remove("b");
    await (window as any).__DUO_KILLER_TEST__.savePreset("a", "MK2M4CH", 1);
  });
  await page.goto("/");
  await page.getByRole("button", { name: "提前准备（不下单）", exact: true }).click();
  await expect(page.getByText("准备完成，未创建订单。", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "创建订单", exact: true })).toHaveCount(0);
  const tasks = await page.evaluate(() => (window as any).started);
  expect(tasks).toHaveLength(1);
  expect(tasks[0].params.prepare_only).toBe(true);
});

test("warm login does not depend on product availability and does not purchase", async ({ page }) => {
  await fixture(page);
  await page.addInitScript(async () => { await (window as any).__DUO_KILLER_TEST__.remove("b"); });
  await page.goto("/");
  await page.getByRole("button", { name: "预热登录（不加购）", exact: true }).click();
  await expect(page.getByText("登录预热完成，未加购、未下单。", { exact: true })).toBeVisible();
  const calls = await page.evaluate(() => (window as any).started);
  expect(calls).toHaveLength(1);
  expect(calls[0].action).toBe("login");
  expect(calls[0].params).toEqual({ warm_only: true });
});

test("waiting checkout can be reopened and settings do not enqueue address reads", async ({ page }) => {
  await fixture(page);
  await page.addInitScript(() => {
    (window as any).__DUO_KILLER_TEST__.start("a", "purchase", {});
  });
  await page.goto("/");
  await page.getByRole("button", { name: "1 个任务进行中", exact: true }).click();
  await expect(page.getByRole("button", { name: "确认创建订单", exact: true })).toBeVisible();
  await page.getByRole("link", { name: "本地配置", exact: true }).click();
  await expect(page.getByRole("heading", { name: "本地配置", exact: true })).toBeVisible();
  expect((await page.evaluate(() => (window as any).started)).filter((t: any) => t.action === "address_read")).toHaveLength(0);
});

test("new account is only added after password and OTP succeed", async ({
  page,
}) => {
  await fixture(page, false);
  await page.goto("/");
  await page.getByLabel("Apple 账号", { exact: true }).fill("new@example.test");
  await page.getByRole("button", { name: "继续登录", exact: true }).click();
  await expect(page.getByLabel("密码", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "立即下单" })).toHaveCount(0);
  await page.getByLabel("密码", { exact: true }).fill("fixture-password");
  await page.getByRole("button", { name: "继续", exact: true }).click();
  await expect(page.getByRole("button", { name: "立即下单" })).toHaveCount(0);
  await page.getByLabel("六位验证码").fill("012345");
  await page.getByRole("button", { name: "继续", exact: true }).click();
  await expect(page.getByRole("button", { name: "立即下单" })).toBeVisible();
  const state = await page.evaluate(() =>
    (window as any).__DUO_KILLER_TEST__.snapshot(),
  );
  expect(state.accounts[0].pending).toBe(false);
  expect(state.accounts[0].name).toBe("接口昵称");
  await expect(
    page.locator(".sidebar").getByRole("button", { name: /接口昵称/ }),
  ).toBeVisible();
});

test("credentials and leading-zero codes stay with the selected account", async ({
  page,
}) => {
  await fixture(page);
  await page.goto("/");
  await enterWorkspace(page);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await page.getByLabel("密码", { exact: true }).fill("fixture-password");
  await page.getByRole("button", { name: "显示密码" }).click();
  await expect(page.getByLabel("密码", { exact: true })).toHaveAttribute(
    "type",
    "text",
  );
  await page.getByRole("button", { name: "继续", exact: true }).click();
  await expect(page.getByLabel("六位验证码")).toBeVisible();
  await page.getByRole("button", { name: /备用账号 seco/ }).click();
  await expect(page.getByLabel("六位验证码")).toHaveCount(0);
  await page.getByRole("button", { name: "检查登录", exact: true }).click();
  await expect(page.getByText("备用账号 · seco**@example.test")).toBeVisible();
  await page.getByRole("button", { name: /日常账号 fir/ }).click();
  await page.getByLabel("六位验证码").fill("012345");
  await page.getByRole("button", { name: "继续", exact: true }).click();
  expect(await page.evaluate(() => (window as any).sent)).toEqual([
    {
      accountId: "a",
      taskId: "task-3",
      requestId: "p-task-3",
      value: { password: "fixture-password", remember: false },
    },
    {
      accountId: "a",
      taskId: "task-3",
      requestId: "code-task-3",
      value: "012345",
    },
  ]);
});

test("OTP method buttons switch within the same account without submitting a code", async ({
  page,
}) => {
  await fixture(page);
  await page.goto("/");
  await enterWorkspace(page);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await page.getByLabel("密码", { exact: true }).fill("fixture-password");
  await page.getByRole("button", { name: "继续", exact: true }).click();
  const code = page.getByLabel("六位验证码", { exact: true });
  await expect(code).toHaveValue("");
  await expect(page.getByText(/输入 [sd] 改用/)).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "改用设备验证码" }),
  ).toHaveCount(0);
  await page.getByRole("button", { name: "改用短信验证码" }).click();
  await expect(
    page.getByRole("heading", { name: "短信六位验证码：" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "改用短信验证码" }),
  ).toHaveCount(0);
  await page.getByRole("button", { name: "改用设备验证码" }).click();
  await expect(
    page.getByRole("heading", { name: "设备六位验证码：" }),
  ).toBeVisible();
  await expect(page.getByRole("group", { name: "切换验证方式" })).toHaveCount(
    0,
  );
  await code.fill("012");
  await page.getByRole("button", { name: "继续", exact: true }).click();
  expect(await page.evaluate(() => (window as any).sent)).toHaveLength(3);
  await code.fill("012345");
  await page.getByRole("button", { name: "继续", exact: true }).click();
  const replies = await page.evaluate(() => (window as any).sent);
  expect(replies.slice(1).map((r: any) => r.value)).toEqual([
    "s",
    "d",
    "012345",
  ]);
  expect(
    replies.every((r: any) => r.accountId === "a" && r.taskId === "task-3"),
  ).toBe(true);
});

test("order confirmation is explicit and can be rejected", async ({ page }) => {
  await fixture(page);
  await page.goto("/");
  await enterWorkspace(page);
  await page.getByRole("button", { name: "立即下单" }).click();
  await expect(
    page.getByRole("cell", { name: "测试商品 MK2M4CH" }),
  ).toBeVisible();
  await expect(page.getByText("总支付", { exact: true })).toBeVisible();
  expect(await page.evaluate(() => (window as any).sent)).toHaveLength(0);
  await page.getByRole("button", { name: "取消", exact: true }).click();
  expect((await page.evaluate(() => (window as any).sent))[0].value).toBe(
    false,
  );
});

test("orders use a table and workspace fits minimum window width", async ({
  page,
}) => {
  await fixture(page);
  await page.goto("/");
  await enterWorkspace(page);
  await page.getByRole("button", { name: "订单", exact: true }).click();
  await page.getByRole("button", { name: "查询订单", exact: true }).click();
  await expect(
    page.getByRole("cell", { name: "我们正在等待你的付款" }),
  ).toBeVisible();
  await page.setViewportSize({ width: 820, height: 620 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({ path: "artifacts/ui-orders.png", fullPage: true });
});

test("desktop workspace screenshot", async ({ page }) => {
  await fixture(page);
  await page.goto("/");
  await enterWorkspace(page);
  await page.getByRole("button", { name: "检查登录", exact: true }).click();
  await expect(page.locator(".task-detail")).toHaveCSS("opacity", "1");
  await page.screenshot({
    path: "artifacts/ui-workspace.png",
    fullPage: true,
    animations: "disabled",
  });
});

test("purchase presets belong to one account", async ({ page }) => {
  await fixture(page);
  await page.goto("/");
  await enterWorkspace(page);
  await page.getByRole("link", { name: "本地配置", exact: true }).click();
  await page.getByRole("combobox", { name: "预制商品" }).click();
  await page.getByRole("option", { name: /抛光布/ }).click();
  await page.getByLabel("数量", { exact: true }).fill("3");
  await page.getByRole("button", { name: "保存购买预设", exact: true }).click();
  await expect(page.getByText("购买预设已保存", { exact: true })).toBeVisible();
  await page.getByRole("link", { name: "账号工作台", exact: true }).click();
  await page.getByRole("button", { name: /备用账号 seco/ }).click();
  await expect(page.getByRole("combobox", { name: "预制商品" })).toContainText("MK2M4CH");
  await page.getByRole("button", { name: /日常账号 fir/ }).click();
  await expect(page.getByRole("combobox", { name: "预制商品" })).toContainText("MLH74FE");
  await expect(page.getByLabel("数量", { exact: true })).toHaveValue("3");
});

test("delivery choices keep the real option value and default", async ({
  page,
}) => {
  await fixture(page);
  await page.goto("/");
  await enterWorkspace(page);
  await page.getByRole("button", { name: "购物袋", exact: true }).click();
  await page.getByRole("button", { name: "结算购物袋" }).click();
  await expect(page.getByRole("combobox", { name: "选择一个选项" })).toHaveText(
    "为我送货",
  );
  await page.getByRole("combobox", { name: "选择一个选项" }).click();
  await page.getByRole("option", { name: "我要自提", exact: true }).click();
  await page.getByRole("button", { name: "继续", exact: true }).click();
  expect((await page.evaluate(() => (window as any).sent))[0].value).toBe(
    "RETAIL",
  );
});

test("payment result offers an order query without creating another order", async ({
  page,
}) => {
  await fixture(page);
  await page.goto("/");
  await enterWorkspace(page);
  await page.getByRole("button", { name: "立即下单" }).click();
  await page.screenshot({ path: "artifacts/ui-confirm.png", fullPage: true });
  await page.getByRole("button", { name: "确认创建订单", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "订单已创建，等待付款" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "我已支付，查询订单" }).click();
  await expect(
    page.getByRole("cell", { name: "我们正在等待你的付款" }),
  ).toBeVisible();
  expect(await page.evaluate(() => (window as any).sent)).toHaveLength(1);
});

test("cancel order asks explicit confirmation without creating another purchase", async ({ page }) => {
  await fixture(page);
  await page.goto("/");
  await enterWorkspace(page);
  await page.getByRole("button", { name: "立即下单", exact: true }).click();
  await page.getByRole("button", { name: "确认创建订单", exact: true }).click();
  await page.getByRole("button", { name: "取消订单", exact: true }).click();
  await expect(page.getByRole("button", { name: "确认取消订单", exact: true })).toBeVisible();
  const cancelTask = (await page.evaluate(() => (window as any).started)).find((t: any) => t.action === "cancel_order");
  expect((await page.evaluate(() => (window as any).sent)).filter((t: any) => t.taskId === cancelTask.id)).toHaveLength(0);
  await page.getByRole("button", { name: "确认取消订单", exact: true }).click();
  await expect(page.getByText("官网已确认订单取消。", { exact: true })).toBeVisible();
  expect((await page.evaluate(() => (window as any).started)).filter((t: any) => t.action === "purchase")).toHaveLength(1);
});

test("created order is visible while QR is loading and remains after QR failure", async ({ page }) => {
  await fixture(page);
  await page.goto("/");
  await enterWorkspace(page);
  await page.getByRole("button", { name: "立即下单", exact: true }).click();
  await page.getByRole("button", { name: "确认创建订单", exact: true }).click();
  await page.evaluate(() => {
    const root = window as any;
    const id = root.started.find((t: any) => t.action === "purchase").id;
    root.finish(id, { status: "running", order_submitted: true, result: {
      status: "pending_payment", order_number: "EARLY-001", amount_due: "RMB 69", qr_loading: true,
      order_confirmed_elapsed_seconds: 18 } });
  });
  await expect(page.getByRole("heading", { name: "订单已创建，等待付款" })).toBeVisible();
  await expect(page.getByText("订单 EARLY-001 · RMB 69")).toBeVisible();
  await expect(page.getByText("订单确认耗时：约 18 秒（含排队，不含二维码）。")).toBeVisible();
  await expect(page.getByText("订单已创建", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "停止加载二维码", exact: true })).toBeVisible();
  await expect(page.getByRole("status")).toContainText("正在加载支付宝付款二维码");
  await page.evaluate(() => {
    const root = window as any;
    const id = root.started.find((t: any) => t.action === "purchase").id;
    root.finish(id, { status: "succeeded", result: {
      status: "pending_payment", order_number: "EARLY-001", amount_due: "RMB 69", qr_loading: false,
      qr_error: "二维码读取超时，订单保留" } });
  });
  await expect(page.getByText("订单 EARLY-001 · RMB 69")).toBeVisible();
  await expect(page.getByText("二维码读取超时，订单保留")).toBeVisible();
  await expect(page.getByRole("button", { name: "获取二维码", exact: true })).toBeEnabled();
  expect((await page.evaluate(() => (window as any).started)).filter((t: any) => t.action === "purchase")).toHaveLength(1);
});

test("startup selects nothing, checks chosen accounts sequentially, and resets on reload", async ({
  page,
}) => {
  await fixture(page);
  await page.goto("/");
  await expect(
    page.getByRole("button", { name: "检查并使用所选账号" }),
  ).toBeDisabled();
  expect(await page.evaluate(() => (window as any).started)).toEqual([]);
  await page.evaluate(() => {
    (window as any).waitForMe = true;
  });
  await page.getByRole("checkbox", { name: /使用 日常账号/ }).check();
  await page.getByRole("checkbox", { name: /使用 备用账号/ }).check();
  await page.getByRole("button", { name: "检查并使用所选账号" }).click();
  await expect(page.getByLabel("密码", { exact: true })).toBeVisible();
  expect(
    await page.evaluate(() =>
      (window as any).started.map((t: any) => t.accountId),
    ),
  ).toEqual(["a"]);
  await page.getByLabel("密码", { exact: true }).fill("fixture");
  await page.getByRole("button", { name: "继续", exact: true }).click();
  expect(await page.evaluate(() => (window as any).started)).toHaveLength(1);
  await page.getByLabel("六位验证码").fill("012345");
  await page.getByRole("button", { name: "继续", exact: true }).click();
  await expect(page.getByLabel("密码", { exact: true })).toBeVisible();
  expect(
    await page.evaluate(() =>
      (window as any).started.map((t: any) => t.accountId),
    ),
  ).toEqual(["a", "b"]);
  await page.getByLabel("密码", { exact: true }).fill("fixture");
  await page.getByRole("button", { name: "继续", exact: true }).click();
  await page.getByLabel("六位验证码").fill("123456");
  await page.getByRole("button", { name: "继续", exact: true }).click();
  await expect(page.getByRole("button", { name: "立即下单" })).toBeVisible();
  await page.reload();
  await expect(
    page.getByRole("checkbox", { name: /使用 日常账号/ }),
  ).not.toBeChecked();
  expect(await page.evaluate(() => (window as any).started)).toEqual([]);
  await page.screenshot({
    path: "artifacts/startup-selection.png",
    fullPage: true,
  });
});

test("unselected accounts are never requested or activated", async ({
  page,
}) => {
  await fixture(page);
  await page.goto("/");
  await page.getByRole("checkbox", { name: /使用 备用账号/ }).check();
  await page.getByRole("button", { name: "检查并使用所选账号" }).click();
  await expect(page.getByRole("button", { name: "立即下单" })).toBeVisible();
  expect(
    await page.evaluate(() =>
      (window as any).started.map((t: any) => t.accountId),
    ),
  ).toEqual(["b"]);
  await expect(page.getByRole("button", { name: /日常账号 fir/ })).toHaveCount(
    0,
  );
});

test("cancelled enrollment remains pending and can be retried", async ({
  page,
}) => {
  await fixture(page, false);
  await page.goto("/");
  await page.getByLabel("Apple 账号", { exact: true }).fill("new@example.test");
  await page.getByRole("button", { name: "继续登录", exact: true }).click();
  await expect(page.getByLabel("密码", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "取消", exact: true }).click();
  await expect(page.getByRole("heading", { name: "未完成添加" })).toBeVisible();
  expect(
    (await page.evaluate(() => (window as any).__DUO_KILLER_TEST__.snapshot()))
      .accounts[0].pending,
  ).toBe(true);
  await expect(page.getByRole("button", { name: "立即下单" })).toHaveCount(0);
  await page.getByRole("button", { name: "继续登录 n**@example.test" }).click();
  await expect(page.getByLabel("密码", { exact: true })).toBeVisible();
});

test("failed account stays disabled while the next selected account can complete", async ({
  page,
}) => {
  await fixture(page);
  await page.goto("/");
  await page.evaluate(() => {
    (window as any).waitForMe = true;
  });
  await page.getByRole("checkbox", { name: /使用 日常账号/ }).check();
  await page.getByRole("checkbox", { name: /使用 备用账号/ }).check();
  await page.getByRole("button", { name: "检查并使用所选账号" }).click();
  await expect(page.getByLabel("密码", { exact: true })).toBeVisible();
  await page.evaluate(() => {
    const root = window as any;
    root.waitForMe = false;
    root.finish(root.started[0].id, {
      status: "failed",
      prompt: null,
      stage: "密码错误",
    });
  });
  await expect(page.getByText("未启用：密码错误")).toBeVisible();
  await page.getByRole("button", { name: "使用已通过的 1 个账号" }).click();
  await expect(page.getByRole("button", { name: "立即下单" })).toBeVisible();
  await expect(page.getByRole("button", { name: /日常账号 fir/ })).toHaveCount(
    0,
  );
});

test("public DUO route needs no account and retains clearly dated results on failure", async ({
  page,
}) => {
  await fixture(page, false);
  await page.goto("/#/duo");
  await expect(page.getByRole("heading", { name: "DUO 状态" })).toBeVisible();
  await expect(page.locator("tbody tr")).toHaveCount(2);
  await expect(
    page.getByLabel("Apple 账号", { exact: true }),
  ).not.toBeVisible();
  expect(await page.evaluate(() => (window as any).started.length)).toBe(0);
  await page.getByRole("button", { name: "刷新状态" }).click();
  const closed = page.locator("tr").filter({ hasText: "MK2P4CH" });
  const unknown = page.locator("tr").filter({ hasText: "MK2M4CH" });
  await expect(closed).toContainText("未开放");
  await expect(unknown).toContainText("未知");
  await expect(unknown).toContainText("许可待确认");
  expect(
    await page.evaluate(() =>
      (window as any).started.every(
        (t: any) =>
          t.action === "catalog" &&
          t.accountId === "00000000-0000-0000-0000-000000000000",
      ),
    ),
  ).toBe(true);
  await page.evaluate(() => {
    (window as any).catalogFails = true;
  });
  await page.getByRole("button", { name: "刷新状态" }).click();
  await expect(page.getByRole("alert")).toContainText("541");
  await expect(
    page.getByText("刷新失败，下方保留上次成功结果，并非本次实时状态。"),
  ).toBeVisible();
  await expect(closed).toContainText("未开放");
});

test("preset products fill purchase fields without submitting an order", async ({
  page,
}) => {
  await fixture(page);
  await page.goto("/");
  await enterWorkspace(page);
  await page.getByRole("combobox", { name: "预制商品" }).click();
  await page.getByRole("option", { name: /MK2P4CH/ }).click();
  await expect(page.getByRole("combobox", { name: "预制商品" })).toContainText("MK2P4CH");
  await page.getByRole("combobox", { name: "预制商品" }).click();
  await page.getByRole("option", { name: /抛光布/ }).click();
  await expect(page.getByRole("combobox", { name: "预制商品" })).toContainText("MLH74FE");
  const purchase = page.locator("section").filter({
    has: page.getByRole("heading", { name: "购买商品", exact: true }),
  });
  await expect(purchase.getByRole("button")).toHaveCount(1);
  await expect(
    purchase.getByRole("button", { name: "立即下单" }),
  ).toBeVisible();
  await expect(page.getByText("任务记录", { exact: true })).toHaveCount(0);
  await expect(page.getByText("查看过程记录", { exact: true })).toHaveCount(0);
  expect(
    await page.evaluate(() =>
      (window as any).started.some(
        (t: any) => t.action === "purchase" || t.action === "add",
      ),
    ),
  ).toBe(false);
  await page.getByRole("link", { name: "DUO 状态", exact: true }).click();
  await expect(page.locator("tbody tr")).toHaveCount(2);
  await expect(
    page.getByRole("cell", { name: "抛光布", exact: true }),
  ).toHaveCount(0);
});

test("me refresh updates the nickname in the sidebar and configuration selector", async ({
  page,
}) => {
  await fixture(page);
  await page.goto("/");
  await expect(page.getByLabel("备注名称")).toHaveCount(0);
  await enterWorkspace(page);
  await page.evaluate(() => {
    (window as any).meName = "最新昵称";
  });
  await page.getByRole("button", { name: "检查登录", exact: true }).click();
  await expect(
    page
      .locator(".sidebar")
      .getByRole("button", { name: /最新昵称 fir\*\*@example.test/ }),
  ).toBeVisible();
  await page.getByRole("link", { name: "本地配置", exact: true }).click();
  await expect(page.getByLabel("配置范围")).toContainText(
    "最新昵称 · fir**@example.test",
  );
  await page.getByRole("link", { name: "账号工作台", exact: true }).click();
  await page.evaluate(() => {
    (window as any).meName = "";
  });
  await page.getByRole("button", { name: "检查登录", exact: true }).click();
  await expect(page.locator(".sidebar strong").first()).toHaveText(
    "fir**@example.test",
  );
});

test("failed reauthentication removes accounts during startup without opening the add form", async ({
  page,
}) => {
  await fixture(page);
  await page.addInitScript(() => {
    (window as any).waitForMe = true;
  });
  await page.goto("/");
  await page.getByRole("checkbox", { name: /使用 日常账号/ }).check();
  await page.getByRole("checkbox", { name: /使用 备用账号/ }).check();
  await page.getByRole("button", { name: "检查并使用所选账号" }).click();
  for (const accountId of ["a", "b"]) {
    await expect
      .poll(() =>
        page.evaluate(
          (id) => (window as any).started.some((t: any) => t.accountId === id),
          accountId,
        ),
      )
      .toBe(true);
    await page.evaluate((accountId) => {
      const root = window as any;
      const task = root.started.find((t: any) => t.accountId === accountId);
      root.finish(task.id, {
        status: "failed",
        prompt: null,
        stage: "当前用户已失效，请重新登录。账号已移除，可自行重新添加。",
        result: { account_removed: true },
      });
    }, accountId);
  }
  await expect(page.getByRole("alert")).toContainText(
    "当前用户已失效，请重新登录",
  );
  await expect(page.getByRole("checkbox")).toHaveCount(0);
  await expect(page.getByLabel("Apple 账号", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "添加新账号" })).toBeVisible();
  expect(await page.evaluate(() => (window as any).started.length)).toBe(2);
});

test("expired active account stays on the current route while other accounts remain usable", async ({
  page,
}) => {
  await fixture(page);
  await page.goto("/");
  await enterWorkspace(page);
  await page.getByRole("link", { name: "DUO 状态", exact: true }).click();
  await page.evaluate(() => {
    const root = window as any;
    const task = root.started.find((t: any) => t.accountId === "a");
    root.finish(task.id, {
      status: "failed",
      prompt: null,
      stage: "当前用户已失效，请重新登录。账号已移除。",
      result: { account_removed: true },
    });
  });
  await expect(page).toHaveURL(/#\/duo$/);
  await expect(page.getByRole("alert")).toContainText("账号已移除");
  await expect(
    page.locator(".sidebar").getByRole("button", { name: /日常账号/ }),
  ).toHaveCount(0);
  await expect(
    page.locator(".sidebar").getByRole("button", { name: /备用账号/ }),
  ).toBeVisible();
  await expect(page.getByLabel("Apple 账号", { exact: true })).toHaveCount(0);
  await page
    .locator(".sidebar")
    .getByRole("button", { name: /备用账号/ })
    .click();
  await expect(page.getByRole("button", { name: "立即下单" })).toBeVisible();
});

test("account identity is masked by default and reveals only until account switch or reload", async ({
  page,
}) => {
  await fixture(page, true, "sample17@example.test");
  await page.goto("/");
  await enterWorkspace(page);
  const identity = page.locator(".account-identity");
  await expect(identity).toContainText("sampl**@example.test");
  await expect(page.locator("body")).not.toContainText("sample17@example.test");
  await expect(page.locator(".sidebar .account").first()).toHaveAttribute(
    "title",
    "日常账号（sampl**@example.test）",
  );
  const reveal = page.getByRole("button", {
    name: "显示完整账号",
    exact: true,
  });
  await reveal.focus();
  await reveal.press("Enter");
  await expect(identity).toContainText("sample17@example.test");
  await expect(
    page.getByRole("button", { name: "隐藏完整账号" }),
  ).toHaveAttribute("aria-pressed", "true");
  await page.getByRole("button", { name: "隐藏完整账号" }).click();
  await expect(identity).toContainText("sampl**@example.test");
  await page.getByRole("button", { name: "显示完整账号", exact: true }).click();
  await page.locator(".sidebar .account").nth(1).click();
  await expect(identity).toContainText("seco**@example.test");
  await expect(
    page.getByRole("button", { name: "显示完整账号", exact: true }),
  ).toBeVisible();
  await page.reload();
  await expect(page.locator(".account-selection")).toContainText(
    "sampl**@example.test",
  );
  const state = await page.evaluate(() =>
    (window as any).__DUO_KILLER_TEST__.snapshot(),
  );
  expect(state.accounts[0].email).toBe("sample17@example.test");
});

test("cancel remains at the operation's top right for prompts and running states on narrow screens", async ({
  page,
}) => {
  await fixture(page);
  await page.goto("/");
  await enterWorkspace(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("button", { name: "登录", exact: true }).click();
  const panel = page.getByRole("region", { name: "当前任务" });
  const cancel = panel.getByRole("button", { name: "取消", exact: true });
  for (const phase of ["waiting", "running"]) {
    await expect(cancel).toBeVisible();
    const box = (await panel.boundingBox())!;
    const button = (await cancel.boundingBox())!;
    expect(button.y - box.y).toBeLessThanOrEqual(21);
    expect(box.x + box.width - button.x - button.width).toBeLessThanOrEqual(21);
    if (phase === "waiting")
      await page.evaluate(() => {
        const root = window as any;
        const login = root.started.find((t: any) => t.action === "login");
        root.finish(login.id, { status: "running", prompt: null });
      });
  }
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "artifacts/ui-cancel-mobile.png",
    fullPage: true,
  });
  await cancel.click();
  await expect(panel.getByText("已取消", { exact: true })).toBeVisible();
  await expect(cancel).toHaveCount(0);
});

test("interaction feedback respects reduced motion", async ({ page }) => {
  await fixture(page);
  await page.goto("/");
  await enterWorkspace(page);
  const toggle = page.getByRole("button", {
    name: "显示完整账号",
    exact: true,
  });
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await expect(toggle).toHaveCSS(
    "transition-duration",
    "0.18s, 0.18s, 0.18s, 0.12s, 0.12s",
  );
  await expect(toggle.locator("span")).toHaveCSS(
    "animation-name",
    "feedback-in",
  );
  await page.emulateMedia({ reducedMotion: "reduce" });
  await expect(toggle).toHaveCSS("transition-duration", "0s");
  await expect(toggle.locator("span")).toHaveCSS("animation-name", "none");
  await expect(page.locator(".task-detail")).toHaveCSS(
    "animation-name",
    "none",
  );
  await expect(page.locator(".task-detail")).toHaveCSS("opacity", "1");
  await toggle.click();
  await expect(page.locator(".account-identity")).toContainText(
    "first@example.test",
  );
});

test("shared dropdown supports keyboard, dismissal and narrow viewport placement", async ({
  page,
}) => {
  await fixture(page);
  await page.goto("/");
  await enterWorkspace(page);
  await expect(page).toHaveTitle("DUO Killer");
  const select = page.getByRole("combobox", { name: "预制商品" });
  await select.focus();
  await select.press("ArrowDown");
  await expect(select).toHaveAttribute("aria-expanded", "true");
  await select.press("End");
  await expect(page.getByRole("combobox", { name: "预制商品" })).toContainText("MK2M4CH");
  await select.press("Escape");
  await expect(page.getByRole("listbox")).toHaveCount(0);
  await expect(select).toBeFocused();
  await select.press("ArrowDown");
  await select.press("End");
  await select.press("Enter");
  await expect(page.getByRole("combobox", { name: "预制商品" })).toContainText("MLH74FE");
  await select.click();
  await expect(page.getByRole("option", { name: /抛光布/ })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await select.press("Tab");
  await expect(page.getByRole("listbox")).toHaveCount(0);
  await page.setViewportSize({ width: 390, height: 620 });
  await select.click();
  const box = (await page.getByRole("listbox").boundingBox())!;
  expect(box.x).toBeGreaterThanOrEqual(8);
  expect(box.y).toBeGreaterThanOrEqual(8);
  expect(box.x + box.width).toBeLessThanOrEqual(382);
  expect(box.y + box.height).toBeLessThanOrEqual(612);
  await page.screenshot({
    path: "artifacts/ui-dropdown-mobile.png",
    animations: "disabled",
  });
  await page.locator(".topbar").click();
  await expect(page.getByRole("listbox")).toHaveCount(0);
  const colors = await page.evaluate(() =>
    [".app-shell", ".sidebar"].map(
      (selector) =>
        getComputedStyle(document.querySelector(selector)!).backgroundColor,
    ),
  );
  expect(colors[0]).toBe(colors[1]);
});

test("task navigation returns to one task from another page and hides when done", async ({
  page,
}) => {
  await fixture(page);
  await page.goto("/");
  await enterWorkspace(page);
  const trigger = page.getByRole("button", {
    name: "1 个任务进行中",
    exact: true,
  });
  await expect(trigger).toHaveCount(0);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(trigger).toBeVisible();
  await expect(page.locator(".sidebar-footer")).toHaveText("仅保存在本机");
  await page.getByRole("button", { name: /备用账号 seco/ }).click();
  await page.getByRole("link", { name: "本地配置", exact: true }).click();
  await trigger.click();
  await expect(page).toHaveURL(/#\/$/);
  await expect(page.locator(".task-detail")).toBeFocused();
  await expect(page.locator(".task-account")).toHaveText("fir**@example.test");
  await expect(page.getByLabel("密码", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "取消", exact: true }).click();
  await expect(trigger).toHaveCount(0);
  expect(
    (await page.evaluate(() => (window as any).started)).filter(
      (t: any) => t.action === "login",
    ),
  ).toHaveLength(1);
});

test("task navigation selects among accounts with masked labels and keyboard dismissal", async ({
  page,
}) => {
  await fixture(page);
  await page.goto("/");
  await enterWorkspace(page);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await page.getByRole("button", { name: /备用账号 seco/ }).click();
  await page.getByRole("button", { name: "登录", exact: true }).click();
  const trigger = page.getByRole("button", {
    name: "2 个任务进行中",
    exact: true,
  });
  const list = page.getByRole("list", { name: "进行中的任务" });
  await trigger.focus();
  await page.keyboard.press("Enter");
  await expect(list).toBeVisible();
  await expect(list).toContainText("fir**@example.test");
  await expect(list).not.toContainText("first@example.test");
  await page.keyboard.press("Escape");
  await expect(list).toHaveCount(0);
  await expect(trigger).toBeFocused();
  await page.setViewportSize({ width: 390, height: 760 });
  await trigger.click();
  await expect(list).toBeInViewport();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "artifacts/task-navigation-mobile.png",
    animations: "disabled",
  });
  await page.keyboard.press("Tab");
  await page.keyboard.press("Enter");
  await expect(list).toHaveCount(0);
  await expect(page.locator(".task-detail")).toBeFocused();
  await expect(page.locator(".task-account")).toHaveText("fir**@example.test");
  await expect(trigger).toBeInViewport();
  await trigger.click();
  await list.getByRole("button", { name: /备用账号/ }).click();
  await expect(page.locator(".task-account")).toHaveText("seco**@example.test");
  await page.getByRole("button", { name: "取消", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "1 个任务进行中", exact: true }),
  ).toBeVisible();
});

test("task navigation preserves account setup while visiting another active task", async ({
  page,
}) => {
  await fixture(page);
  await page.goto("/");
  await enterWorkspace(page);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await page.getByRole("button", { name: "添加账号", exact: true }).click();
  await page
    .getByLabel("Apple 账号", { exact: true })
    .fill("third@example.test");
  await page.getByRole("button", { name: "继续登录", exact: true }).click();
  await expect(page.getByLabel("密码", { exact: true })).toBeVisible();
  const trigger = page.getByRole("button", {
    name: "2 个任务进行中",
    exact: true,
  });
  const list = page.getByRole("list", { name: "进行中的任务" });
  await trigger.click();
  await list.getByRole("button", { name: /日常账号/ }).click();
  await expect(page.locator(".task-detail")).toBeFocused();
  await trigger.click();
  await list.getByRole("button", { name: /添加并登录账号/ }).click();
  await expect(page.locator(".setup-progress")).toBeFocused();
  await expect(page.getByLabel("密码", { exact: true })).toBeVisible();
  const starts = await page.evaluate(() => (window as any).started);
  expect(starts.filter((t: any) => t.action === "enroll")).toHaveLength(1);
  expect(starts.filter((t: any) => t.action === "login")).toHaveLength(1);
});

test("task navigation returns public tasks to their own page", async ({
  page,
}) => {
  await fixture(page);
  await page.goto("/#/duo");
  await page.getByRole("button", { name: "刷新状态", exact: true }).click();
  await page.evaluate(() => {
    const root = window as any;
    root.finish(root.started.at(-1).id, { status: "running", result: null });
  });
  await page.getByRole("link", { name: "账号工作台", exact: true }).click();
  await page
    .getByRole("button", { name: "1 个任务进行中", exact: true })
    .click();
  await expect(page).toHaveURL(/#\/duo$/);
  await expect(page.locator(".local-task")).toBeFocused();
  expect(await page.evaluate(() => (window as any).started)).toHaveLength(1);
});

test("removing a saved account requires confirmation and clears gate selection", async ({
  page,
}) => {
  await fixture(page);
  await page.goto("/");
  await page.getByRole("checkbox", { name: /使用 日常账号/ }).check();
  await page.getByRole("button", { name: /移除 日常账号/ }).click();
  const dialog = page.getByRole("dialog", { name: "移除本地账号" });
  await expect(dialog).toBeVisible();
  await expect(dialog).not.toContainText("first@example.test");
  await expect(dialog.getByRole("button", { name: "保留账号" })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(dialog).not.toBeVisible();
  expect(await page.evaluate(() => (window as any).removed)).toEqual([]);
  await page.getByRole("button", { name: /移除 日常账号/ }).click();
  await dialog.getByRole("button", { name: "确认移除" }).click();
  await expect(
    page.getByRole("checkbox", { name: /使用 日常账号/ }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("checkbox", { name: /使用 备用账号/ }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "检查并使用所选账号" }),
  ).toBeDisabled();
  expect(await page.evaluate(() => (window as any).removed)).toEqual(["a"]);
});

test("removal failure keeps account and allows retry; the last removal returns to enrollment", async ({
  page,
}) => {
  await fixture(page);
  await page.goto("/");
  await page.evaluate(() => {
    (window as any).removeError = "无法清除系统保存的密码";
  });
  await page.getByRole("button", { name: /移除 日常账号/ }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByRole("button", { name: "确认移除" }).click();
  await expect(dialog.getByRole("alert")).toContainText(
    "无法清除系统保存的密码",
  );
  expect(await page.evaluate(() => (window as any).removed)).toEqual([]);
  await page.evaluate(() => {
    (window as any).removeError = "";
  });
  await dialog.getByRole("button", { name: "确认移除" }).click();
  await expect(dialog).not.toBeVisible();
  await page.getByRole("button", { name: /移除 备用账号/ }).click();
  await dialog.getByRole("button", { name: "确认移除" }).click();
  await expect(page.getByRole("textbox", { name: "Apple 账号" })).toBeVisible();
});

test("workspace removal is blocked during tasks and switches to remaining account", async ({
  page,
}) => {
  await fixture(page);
  await page.goto("/");
  await enterWorkspace(page);
  await page.getByRole("button", { name: "立即下单" }).click();
  await expect(
    page.getByRole("button", { name: "移除账号", exact: true }),
  ).toBeDisabled();
  await page.getByRole("button", { name: "取消", exact: true }).click();
  await page.getByRole("button", { name: "移除账号", exact: true }).click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "确认移除" })
    .click();
  await expect(
    page.getByRole("heading", { name: "备用账号", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("navigation", { name: "切换账号" }),
  ).not.toContainText("日常账号");
  expect(await page.evaluate(() => (window as any).removed)).toEqual(["a"]);
  await page.getByRole("button", { name: "移除账号", exact: true }).click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "确认移除" })
    .click();
  await expect(page.getByRole("textbox", { name: "Apple 账号" })).toBeVisible();
});

test("unfinished enrollment can be removed after cancelling login", async ({
  page,
}) => {
  await fixture(page, false);
  await page.goto("/");
  await page
    .getByRole("textbox", { name: "Apple 账号" })
    .fill("draft@example.test");
  await page.getByRole("button", { name: "继续登录", exact: true }).click();
  await expect(page.getByLabel("密码", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "取消", exact: true }).click();
  await page.getByRole("button", { name: /移除 draft|移除 dra/ }).click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "确认移除" })
    .click();
  await expect(page.getByRole("heading", { name: "未完成添加" })).toHaveCount(
    0,
  );
  await expect(page.getByRole("textbox", { name: "Apple 账号" })).toBeVisible();
});
