import { test, expect, type Page } from "@playwright/test";
import { chooseOption } from "./select-option";

test.setTimeout(60_000);

async function fixture(page: Page, running = false) {
  const calls: { path: string; body: any }[] = [];
  const account = {
    id: "interval-fixture",
    name: "预热间隔测试",
    email: "interval@example.test",
    pending: false,
  };
  const settings: any = {
    enabled: true,
    interval_minutes: 5,
    prepared_inventory_interval_minutes: 5,
    flow: "pickup_prepare",
    last_runs: [
      {
        account_id: account.id,
        flow: "pickup_prepare",
        status: running ? "running" : "waiting",
        remaining_seconds: 240,
        last_warm_at_ms: Date.now() - 60000,
      },
    ],
  };
  const task = {
    id: "interval-task",
    account_id: account.id,
    action: "session_maintain",
    status: running ? "running" : "succeeded",
    stage: "准备自提",
    created_at: 1000,
    updated_at: 1000,
    prompt: null,
    result: null,
    order_submitted: false,
    logs: [],
  };
  const snapshot = {
    accounts: [account],
    tasks: [task],
    data_dir: "/synthetic-interval",
  };
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    const body = route.request().postDataJSON() || {};
    calls.push({ path, body });
    const send = (json: any) => route.fulfill({ status: 200, json });
    if (path === "/api/auth/status") return send({ authenticated: true });
    if (path === "/api/workspace_snapshot") return send(snapshot);
    if (path === "/api/workspace_changes")
      return send({
        cursor: "interval",
        reset: true,
        snapshot,
        tasks: [],
        removed_task_ids: [],
      });
    if (path === "/api/maintenance_command") {
      if (body.operation === "configure") {
        const minutes = body.params.interval_minutes;
        expect(Number.isInteger(minutes) && minutes >= 1 && minutes <= 60).toBe(
          true,
        );
        if (Object.hasOwn(body.params, "prepared_inventory_interval_minutes")) {
          const inventory = body.params.prepared_inventory_interval_minutes;
          expect(
            Number.isInteger(inventory) && inventory >= 1 && inventory <= 60,
          ).toBe(true);
        }
        Object.assign(settings, body.params);
      }
      return send(settings);
    }
    return send({ success: true });
  });
  await page.goto("/", { waitUntil: "domcontentloaded" });
  const panel = page.getByRole("region", { name: "空闲预热", exact: true });
  const input = panel.getByRole("spinbutton", {
    name: "空闲预热间隔（分钟）",
    exact: true,
  });
  await expect(input).toBeEnabled();
  const inventoryInput = panel.getByRole("spinbutton", {
    name: "准备内库存刷新间隔（分钟）",
    exact: true,
  });
  return { panel, input, inventoryInput, calls, settings };
}

for (const width of [320, 1180]) {
  test(`${width}px 默认5分钟可保存重载，运行中准备不被取消`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 1000 });
    const { panel, input, inventoryInput, calls } = await fixture(page, true);
    await expect(input).toHaveValue("5");
    await expect(inventoryInput).toHaveValue("5");
    await expect(panel.getByRole("status")).toHaveText("正在准备自提，不下单");
    await expect(panel).toContainText("不是库存检测频率或会话有效期");
    await input.fill("7");
    await panel
      .getByRole("button", { name: "保存预热间隔", exact: true })
      .click();
    await expect(panel.getByRole("checkbox")).toHaveAccessibleName(
      "下单空闲7分钟后自动预热",
    );
    await expect(panel.getByRole("status")).toHaveText("正在准备自提，不下单");
    await inventoryInput.fill("1");
    await panel
      .getByRole("button", { name: "保存准备内刷新间隔", exact: true })
      .click();
    await expect(panel).toContainText("等待选店时每 1 分钟直连刷新门店库存");
    await page.reload({ waitUntil: "domcontentloaded" });
    await expect(input).toHaveValue("7");
    await expect(inventoryInput).toHaveValue("1");
    await expect(panel.getByRole("status")).toHaveText("正在准备自提，不下单");
    expect(
      calls.filter(({ path }) =>
        ["/api/cancel_task", "/api/start_task"].includes(path),
      ),
    ).toHaveLength(0);
    await panel.scrollIntoViewIfNeeded();
    await expect
      .poll(() =>
        page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
      )
      .toBe(true);
    expect(
      await input.evaluate((element) => getComputedStyle(element).fontSize),
    ).toBe(width === 320 ? "16px" : "14px");
    await page.screenshot({
      path: `artifacts/maintenance-interval-${width}.png`,
    });
    await panel.screenshot({
      path: `artifacts/maintenance-interval-panel-${width}.png`,
    });
  });
}

test("空值、小数及超出1–60范围的草稿不能保存", async ({ page }) => {
  const { panel, input, inventoryInput, calls } = await fixture(page);
  for (const field of [input, inventoryInput])
    for (const draft of ["", "0", "61", "1.5"]) {
      await field.fill(draft);
      await expect(field).toHaveAttribute("aria-invalid", "true");
      await expect(
        panel.getByRole("button", { name: "保存预热间隔", exact: true }),
      ).toBeDisabled();
      await expect(
        panel.getByRole("button", { name: "保存准备内刷新间隔", exact: true }),
      ).toBeDisabled();
      await expect(panel.getByRole("checkbox")).toBeDisabled();
    }
  expect(
    calls.filter(({ body }) => body.operation === "configure"),
  ).toHaveLength(0);
});

test("1与60边界可保存，启用切换不写入未保存的间隔草稿", async ({ page }) => {
  const { panel, input, inventoryInput, settings, calls } = await fixture(page);
  for (const minutes of [1, 60]) {
    await input.fill(String(minutes));
    await panel
      .getByRole("button", { name: "保存预热间隔", exact: true })
      .click();
    await expect.poll(() => settings.interval_minutes).toBe(minutes);
    await expect(input).toHaveValue(String(minutes));
  }
  for (const minutes of [1, 60]) {
    await inventoryInput.fill(String(minutes));
    await panel
      .getByRole("button", { name: "保存准备内刷新间隔", exact: true })
      .click();
    await expect
      .poll(() => settings.prepared_inventory_interval_minutes)
      .toBe(minutes);
    expect(settings.interval_minutes).toBe(60);
  }
  await input.fill("12");
  await inventoryInput.fill("13");
  await panel.getByRole("checkbox").click();
  await expect(panel.getByRole("checkbox")).not.toBeChecked();
  await expect.poll(() => settings.enabled).toBe(false);
  expect(settings.interval_minutes).toBe(60);
  expect(settings.prepared_inventory_interval_minutes).toBe(60);
  await expect(input).toHaveValue("12");
  await expect(inventoryInput).toHaveValue("13");
  const writes = calls.filter(({ body }) => body.operation === "configure");
  expect(writes.at(-1)?.body.params).toEqual({
    interval_minutes: 60,
    enabled: false,
  });
  await chooseOption(
    panel.getByRole("combobox", { name: "预热流程", exact: true }),
    "预热登录（不加购）",
  );
  await expect.poll(() => settings.flow).toBe("login");
  expect(settings.interval_minutes).toBe(60);
  expect(settings.prepared_inventory_interval_minutes).toBe(60);
  await expect(input).toHaveValue("12");
  await expect(inventoryInput).toHaveValue("13");
});
