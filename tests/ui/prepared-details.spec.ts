import { test, expect, type Page } from "@playwright/test";

test.setTimeout(60_000);

async function fixture(
  page: Page,
  ready: boolean,
  verified = ready,
  addressRequired?: boolean,
  waitingReview = false,
) {
  const starts: any[] = [];
  const cancels: any[] = [];
  const account = {
    id: "details-fixture",
    name: "准备状态测试",
    email: "details@example.test",
    pending: false,
    preset_sku: "MLH74FE",
    preset_quantity: 2,
  };
  const prepared = {
    v: 1,
    sku: "MLH74FE",
    quantity: 2,
    fulfillment: "RETAIL",
    config_fingerprint: "a".repeat(64),
    config_file_sha256: "b".repeat(64),
    authorized_store_ids: ["R001", "R002", "R003", "R004"],
    prepared_at_ms: Date.now(),
    expires_at_ms: Date.now() + 300000,
    preparation_stage: ready ? "review" : "fulfillment",
    order_ready: ready,
    review_verified: verified,
    checkout_details_prepared: {
      status: ready ? "details_accepted" : "waiting_stock",
      stage: ready || waitingReview ? "Review" : "Fulfillment",
      local_contact_ready: true,
      local_address_ready: true,
      contact_accepted: ready || waitingReview,
      address_accepted: addressRequired === false ? false : ready,
      ...(addressRequired === undefined
        ? {}
        : { address_required: addressRequired }),
    },
  };
  const task: any = {
    id: "details-prepared-task",
    account_id: account.id,
    action: "purchase",
    status: "waiting",
    created_at: 1000,
    updated_at: 1000,
    logs: [],
    order_submitted: false,
    result: null,
    stage: "正在保留自提准备",
    prompt: {
      id: "details-prompt",
      kind: "prepared_purchase",
      label: "自提准备",
      prepared,
    },
  };
  const snapshot = {
    accounts: [account],
    tasks: [task],
    data_dir: "/synthetic-details",
  };
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    const body = route.request().postDataJSON() || {};
    const send = (json: any) => route.fulfill({ status: 200, json });
    if (path === "/api/auth/status") return send({ authenticated: true });
    if (path === "/api/workspace_snapshot") return send(snapshot);
    if (path === "/api/workspace_changes")
      return send({
        cursor: task.status,
        reset: true,
        snapshot,
        tasks: [],
        removed_task_ids: [],
      });
    if (path === "/api/cancel_task") {
      cancels.push(body);
      task.status = "cancelled";
      task.prompt = null;
      return send({ success: true });
    }
    if (path === "/api/start_task") {
      starts.push(body);
      task.status = "running";
      task.prompt = null;
      return send(task.id);
    }
    return send({ success: true });
  });
  await page.goto("/", { waitUntil: "domcontentloaded" });
  const status = page.getByRole("region", {
    name: "自提准备状态",
    exact: true,
  });
  await expect(status).toBeVisible();
  return { status, starts, cancels, task };
}

for (const width of [320, 1180]) {
  for (const ready of [false, true]) {
    test(`${width}px ${ready ? "官网已接受" : "仅本地配置就绪"}准备状态与操作`, async ({
      page,
    }) => {
      await page.setViewportSize({ width, height: 1000 });
      const { status, starts, cancels } = await fixture(page, ready);
      await expect(status.getByRole("heading")).toHaveText(
        ready ? "自提准备就绪" : "已保留自提准备",
      );
      await expect(status).toContainText("MLH74FE × 2");
      await expect(status).toContainText("R001、R002、R003 等4家");
      await expect(status).toContainText("每 5 分钟向官网刷新准备");
      await expect(status).toContainText("尚未创建订单");
      const progress = page.getByRole("region", {
        name: "下单进度",
        exact: true,
      });
      await expect(progress).toContainText("等待购买触发");
      await expect(progress).toContainText("自提准备已保留");
      await expect(progress).not.toContainText("下单需要操作");
      await expect(progress).not.toContainText("所需输入或确认");
      if (ready) {
        await expect(status).toContainText("自提联系人：官网已接受");
        await expect(status).toContainText("地址：官网已接受");
        await expect(status).not.toContainText("不能直接提交");
      } else {
        await expect(status).toContainText(
          "自提联系人：本地配置就绪，尚未确认官网受理",
        );
        await expect(status).toContainText(
          "地址：本地配置就绪，尚未确认官网受理",
        );
        await expect(status).toContainText("不能直接提交");
        await expect(status).not.toContainText("官网已接受");
      }
      await expect(
        status.getByRole("button", { name: "结束准备", exact: true }),
      ).toBeEnabled();
      await expect(
        page.getByRole("button", { name: "立即下单（自提）", exact: true }),
      ).toBeEnabled();
      await expect(
        page.getByRole("button", { name: "立即下单", exact: true }),
      ).toBeEnabled();
      await expect
        .poll(() =>
          page.evaluate(
            () => document.documentElement.scrollWidth <= innerWidth,
          ),
        )
        .toBe(true);
      await status.scrollIntoViewIfNeeded();
      await page.screenshot({
        path: `artifacts/prepared-details-${ready ? "review" : "fulfillment"}-${width}.png`,
      });
      if (ready) {
        await page
          .getByRole("button", { name: "立即下单（自提）", exact: true })
          .click();
        await expect.poll(() => starts.length).toBe(1);
        expect(starts[0].params.fulfillment).toBe("RETAIL");
      } else {
        await status
          .getByRole("button", { name: "结束准备", exact: true })
          .click();
        await expect.poll(() => cancels.length).toBe(1);
        expect(cancels[0].taskId).toBe("details-prepared-task");
      }
    });
  }
}

test("Review缺少本轮复核证明时不标记可提交", async ({ page }) => {
  const { status } = await fixture(page, true, false);
  await expect(status.getByRole("heading")).toHaveText("已保留自提准备");
  await expect(status).toContainText("不能直接提交");
});

test("官网确认全自提时显示无需送货地址，不假称地址已受理", async ({ page }) => {
  const { status } = await fixture(page, true, true, false);
  await expect(status).toContainText("地址：当前自提无需送货地址");
  await expect(status).not.toContainText("地址：官网已接受");
  await expect(status).not.toContainText("地址：本地配置就绪");
  await expect(status).toContainText("自提联系人：官网已接受");
  await expect(
    page.getByRole("region", { name: "下单进度", exact: true }),
  ).toContainText("等待购买触发");
});

test("已填写但官网暂不可提交的Review保留信息等待，不自动下单", async ({
  page,
}) => {
  const { status, starts } = await fixture(page, false, false, false, true);
  await expect(status.getByRole("heading")).toHaveText("已保留自提准备");
  await expect(status).toContainText("结算信息已保留，等待官网允许提交");
  await expect(status).toContainText("当前不能直接提交");
  await expect(status).toContainText("自提联系人：官网已接受");
  await expect(status).toContainText("当前自提无需送货地址");
  await expect(
    page.getByRole("region", { name: "下单进度", exact: true }),
  ).toContainText("等待购买触发");
  expect(starts).toEqual([]);
});

test("准备等待不需人工，转入真实验证码后仍提示输入", async ({ page }) => {
  const { task } = await fixture(page, false);
  const progress = page.getByRole("region", { name: "下单进度", exact: true });
  await expect(progress).toContainText("等待购买触发");
  task.prompt = {
    id: "real-code-prompt",
    kind: "code",
    label: "请输入六位验证码",
  };
  task.updated_at = Date.now();
  await expect(progress).toContainText("下单需要操作");
  await expect(
    page.getByRole("button", { name: "继续", exact: true }),
  ).toBeVisible();
});
