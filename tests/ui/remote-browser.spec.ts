import { test, expect, type Page } from "@playwright/test";

async function fixture(page: Page, delay = 40, pending = false) {
  await page.addInitScript(
    ({ delay, pending }) => {
      const root = window as any;
      const account = {
        id: "remote-account",
        name: "远程测试",
        email: "remote@example.test",
        preset_sku: "MLH74FE",
        preset_quantity: 1,
        pending,
      };
      const task: any = {
        id: "remote-task",
        account_id: account.id,
        action: "login",
        status: "running",
        stage: "请在 Apple 官网窗口登录",
        created_at: Date.now() / 1000,
        updated_at: Date.now() / 1000,
        prompt: null,
        result: null,
        order_submitted: false,
        logs: [],
      };
      let update = (_task: any) => {};
      let taskStarted = !pending;
      root.remoteStarted = [];
      root.remoteState = {
        active: true,
        stale: false,
        failStatus: false,
        failInput: false,
        statusCalls: 0,
        concurrent: 0,
        maxConcurrent: 0,
        inputDelay: 0,
      };
      root.remoteInputs = [];
      root.finishRemoteTask = () => {
        task.status = "succeeded";
        task.stage = "官网登录完成";
        task.result = { success: true, logged_in: true };
        update(structuredClone(task));
      };
      const canvas = document.createElement("canvas");
      canvas.width = 1000;
      canvas.height = 640;
      root.__DUO_KILLER_TEST__ = {
        snapshot: async () =>
          structuredClone({
            accounts: [account],
            tasks: taskStarted ? [task] : [],
            data_dir: "/fixture",
          }),
        subscribe: async (callback: any) => {
          update = callback;
          return () => {};
        },
        add: async () => account,
        remove: async () => {},
        savePreset: async () => account,
        reply: async () => {},
        cancel: async () => {},
        start: async (accountId: string, action: string, params: any) => {
          if (pending && accountId === account.id && action === "enroll") {
            taskStarted = true;
            task.action = action;
            task.status = "running";
            task.prompt = null;
            root.remoteStarted.push({ accountId, action, params });
            update(structuredClone(task));
            return task.id;
          }
          return "unused";
        },
        queue: async () => ({
          config: { mode: "sequential", max_concurrency: 1 },
          jobs: [],
          paused: false,
        }),
        logs: async () => ({ entries: [], has_more: false }),
        browser: async (
          accountId: string,
          taskId: string,
          operation: string,
          params: any,
        ) => {
          if (accountId !== account.id || taskId !== task.id)
            throw new Error("fixture identity mismatch");
          const state = root.remoteState;
          if (operation === "input") {
            root.remoteInputs.push(structuredClone(params));
            if (state.inputDelay)
              await new Promise((resolve) =>
                setTimeout(resolve, state.inputDelay),
              );
            if (state.failInput)
              throw new Error("private-input-error-must-not-appear");
            return { ok: true };
          }
          state.statusCalls++;
          state.concurrent++;
          state.maxConcurrent = Math.max(state.maxConcurrent, state.concurrent);
          try {
            await new Promise((resolve) => setTimeout(resolve, delay));
            if (state.failStatus)
              throw new Error("private-status-error-must-not-appear");
            if (!state.active) return { active: false };
            const ctx = canvas.getContext("2d")!;
            ctx.fillStyle = state.statusCalls % 2 ? "#dceafe" : "#e5f8ed";
            ctx.fillRect(0, 0, 1000, 640);
            ctx.fillStyle = "#111";
            ctx.font = "28px sans-serif";
            ctx.fillText("Official login fixture " + state.statusCalls, 40, 80);
            return {
              active: true,
              url_host: "secure.www.apple.com.cn",
              frame: {
                data_url: canvas.toDataURL("image/png"),
                width: 1000,
                height: 640,
                sequence: state.statusCalls,
                captured_at_ms: Date.now() - (state.stale ? 10000 : 0),
              },
            };
          } finally {
            state.concurrent--;
          }
        },
      };
    },
    { delay, pending },
  );
}

test("首次添加账号没有prompt时也能看到并操作官网登录窗口", async ({ page }) => {
  await fixture(page, 40, true);
  await page.goto("/");
  await expect(page.locator(".setup-progress")).toHaveCount(0);
  await page
    .getByRole("button", { name: "使用官网浏览器登录", exact: true })
    .click();
  const progress = page.locator(".setup-progress");
  await expect(progress).toContainText("请在 Apple 官网窗口登录");
  await expect(
    progress.getByAltText("当前账号 Apple 官网窗口的实时画面"),
  ).toBeVisible();
  await expect(progress.locator(".prompt-form")).toHaveCount(0);
  const field = progress.getByLabel("输入到官网", { exact: true });
  await expect(field).toBeEnabled();
  await field.fill("first-login-fixture");
  await progress.getByRole("button", { name: "发送输入", exact: true }).click();
  await expect(field).toHaveValue("");
  expect(
    await page.evaluate(() => (window as any).remoteStarted[0]),
  ).toMatchObject({
    accountId: "remote-account",
    action: "enroll",
    params: { browser_login: true },
  });
  expect(
    await page.evaluate(() => (window as any).remoteInputs[0]),
  ).toMatchObject({
    type: "text",
    text: "first-login-fixture",
  });
});

test("远程画面轮换且缩放点击映射到官网原始坐标", async ({ page }) => {
  await fixture(page);
  await page.goto("/");
  const image = page.getByAltText("当前账号 Apple 官网窗口的实时画面");
  await expect(image).toBeVisible();
  const initial = await image.getAttribute("src");
  await expect.poll(() => image.getAttribute("src")).not.toBe(initial);
  const screen = page.getByRole("button", {
    name: "操作 Apple 官网画面",
    exact: true,
  });
  const bounds = (await screen.boundingBox())!;
  await screen.click({
    position: { x: bounds.width / 4, y: bounds.height / 2 },
  });
  const input = await page.evaluate(() => (window as any).remoteInputs[0]);
  expect(input.type).toBe("click");
  expect(input.x).toBeGreaterThanOrEqual(248);
  expect(input.x).toBeLessThanOrEqual(251);
  expect(input.y).toBeGreaterThanOrEqual(318);
  expect(input.y).toBeLessThanOrEqual(321);
  expect(input.sequence).toBeGreaterThan(0);
});

test("输入只发送给当前窗口后立即清空且键盘滚动可用", async ({ page }) => {
  await fixture(page);
  await page.goto("/");
  const field = page.getByLabel("输入到官网", { exact: true });
  await expect(field).toBeEnabled();
  await expect(field).toHaveAttribute("type", "password");
  await field.fill("fixture-secret-for-login");
  await page.getByRole("button", { name: "发送输入", exact: true }).click();
  await expect(field).toHaveValue("");
  await expect
    .poll(() => page.evaluate(() => (window as any).remoteInputs.length))
    .toBe(1);
  expect(
    await page.evaluate(() => (window as any).remoteInputs[0]),
  ).toMatchObject({
    type: "text",
    text: "fixture-secret-for-login",
  });
  expect(
    await page.evaluate(() =>
      JSON.stringify({
        local: { ...localStorage },
        session: { ...sessionStorage },
      }),
    ),
  ).not.toContain("fixture-secret-for-login");
  await expect(page.locator("body")).not.toContainText(
    "fixture-secret-for-login",
  );
  await page.getByRole("button", { name: "切换输入框", exact: true }).click();
  await page.getByRole("button", { name: "向下滚动", exact: true }).click();
  expect(
    await page.evaluate(() =>
      (window as any).remoteInputs.map((value: any) => value.type),
    ),
  ).toEqual(["text", "key", "scroll"]);
  expect(await page.evaluate(() => (window as any).remoteInputs[1].key)).toBe(
    "Tab",
  );
  expect(
    await page.evaluate(() => (window as any).remoteInputs[2].delta_y),
  ).toBe(400);
});

test("过期画面与终态任务都不能继续输入", async ({ page }) => {
  await fixture(page);
  await page.goto("/");
  const screen = page.getByRole("button", {
    name: "操作 Apple 官网画面",
    exact: true,
  });
  await expect(screen).toBeEnabled();
  await page.evaluate(() => {
    (window as any).remoteState.stale = true;
  });
  await expect(screen).toBeDisabled();
  await expect(page.getByLabel("输入到官网", { exact: true })).toBeDisabled();
  await expect(
    page.getByText("画面暂未更新，等待新画面后才能操作。"),
  ).toBeVisible();
  await page.evaluate(() => {
    (window as any).finishRemoteTask();
  });
  await expect(screen).toHaveCount(0);
  const count = await page.evaluate(
    () => (window as any).remoteState.statusCalls,
  );
  await page.waitForTimeout(1600);
  expect(
    await page.evaluate(() => (window as any).remoteState.statusCalls),
  ).toBe(count);
  expect(await page.evaluate(() => (window as any).remoteInputs.length)).toBe(
    0,
  );
});

test("手机远程画面不溢出且支持滑动滚动与折叠", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await fixture(page);
  await page.goto("/");
  const screen = page.getByRole("button", {
    name: "操作 Apple 官网画面",
    exact: true,
  });
  await expect(screen).toBeEnabled();
  await screen.scrollIntoViewIfNeeded();
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth),
  ).toBeLessThanOrEqual(390);
  const rect = (await screen.boundingBox())!;
  await page.mouse.move(rect.x + rect.width / 2, rect.y + rect.height * 0.8);
  await page.mouse.down();
  await page.mouse.move(rect.x + rect.width / 2, rect.y + rect.height * 0.3, {
    steps: 4,
  });
  await page.mouse.up();
  const input = await page.evaluate(() => (window as any).remoteInputs[0]);
  expect(input.type).toBe("scroll");
  expect(input.delta_y).toBeGreaterThan(300);
  await page.getByRole("button", { name: "收起远程窗口", exact: true }).click();
  await expect(screen).toHaveCount(0);
  await page.getByRole("button", { name: "展开远程窗口", exact: true }).click();
  await expect(screen).toBeVisible();
});

test("慢请求不会重叠且输入错误可见并不显示私密错误正文", async ({ page }) => {
  await fixture(page, 1000);
  await page.goto("/");
  const field = page.getByLabel("输入到官网", { exact: true });
  await expect(field).toBeEnabled();
  await page.evaluate(() => {
    (window as any).remoteState.failInput = true;
  });
  await field.fill("fixture-secret");
  await page.getByRole("button", { name: "发送输入", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("操作未送达官网窗口");
  await expect(field).toHaveValue("");
  await page.waitForTimeout(2200);
  await expect(page.getByRole("alert")).toContainText("操作未送达官网窗口");
  await expect(page.locator("body")).not.toContainText(
    "private-input-error-must-not-appear",
  );
  expect(
    await page.evaluate(() => (window as any).remoteState.maxConcurrent),
  ).toBe(1);
});

test("远程连接失败会显示可读原因并能恢复", async ({ page }) => {
  await fixture(page);
  await page.goto("/");
  const screen = page.getByRole("button", {
    name: "操作 Apple 官网画面",
    exact: true,
  });
  await expect(screen).toBeEnabled();
  await page.evaluate(() => {
    (window as any).remoteState.failStatus = true;
  });
  await expect(page.getByRole("alert")).toHaveText(
    "远程窗口连接失败，请稍后重试。",
  );
  await expect(page.locator("body")).not.toContainText(
    "private-status-error-must-not-appear",
  );
  await page.evaluate(() => {
    (window as any).remoteState.failStatus = false;
  });
  await expect(screen).toBeEnabled();
  await expect(page.getByRole("alert")).toHaveCount(0);
});
