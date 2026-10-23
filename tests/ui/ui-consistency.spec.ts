import { test, expect } from "@playwright/test";
import { mkdir, writeFile } from "node:fs/promises";
import { uiReviewFixture, noHorizontalOverflow } from "./ui-review-fixture";

const routes = [
  "workspace",
  "settings",
  "monitor",
  "queue",
  "logs",
  "access",
  "duo",
];
const phase = process.env.UI_REVIEW_PHASE || "after";
test("UI review screenshots for desktop and touch layouts", async ({
  page,
}) => {
  test.setTimeout(120000);
  await uiReviewFixture(page);
  const directory = `artifacts/ui-review/${phase}`;
  await mkdir(directory, { recursive: true });
  const metrics: unknown[] = [];
  for (const width of [1180, 390, 320]) {
    await page.setViewportSize({ width, height: width === 1180 ? 800 : 844 });
    for (const route of routes) {
      await page.goto(`/#/${route === "workspace" ? "" : route}`, {
        waitUntil: "domcontentloaded",
      });
      await expect(page.locator("main").first()).toBeVisible();
      if (route === "settings")
        await expect(page.getByText("读取配置…", { exact: true })).toHaveCount(
          0,
        );
      if (route === "monitor")
        await expect(
          page.getByRole("region", { name: "最近有货记录" }),
        ).toContainText("MJT84CH");
      if (route === "queue")
        await expect(page.locator(".queue-job")).toHaveCount(5);
      await page.screenshot({
        path: `${directory}/${width}-${route}.png`,
        fullPage: true,
      });
      if (route === "monitor" || route === "queue") {
        metrics.push({
          width,
          route,
          ...(await page.evaluate(() => ({
            historyHeight: document
              .querySelector(".stock-events")
              ?.getBoundingClientRect().height,
            historyRowHeights: [
              ...document.querySelectorAll(".stock-event-row"),
            ].map((row) => row.getBoundingClientRect().height),
            queueRows: [...document.querySelectorAll(".queue-job")].map(
              (row) => ({
                id: row.getAttribute("data-job-id"),
                height: row.getBoundingClientRect().height,
              }),
            ),
          }))),
        });
      }
      if (phase !== "before") await noHorizontalOverflow(page);
    }
  }
  await writeFile(
    `${directory}/layout-metrics.json`,
    JSON.stringify(metrics, null, 2),
  );
});

test("UI review workspace panels, account and access surfaces", async ({
  page,
}) => {
  test.setTimeout(90000);
  await page.emulateMedia({ reducedMotion: "reduce" });
  await uiReviewFixture(page);
  const directory = `artifacts/ui-review/${phase}`;
  await mkdir(directory, { recursive: true });
  for (const width of [1180, 390, 320]) {
    await page.setViewportSize({ width, height: width === 1180 ? 800 : 844 });
    await page.goto("/", { waitUntil: "domcontentloaded" });
    for (const [tab, name, heading] of [
      ["bag", "购物袋", "整个购物袋"],
      ["orders", "订单", "我的订单"],
      ["catalog", "商品", "预制商品"],
    ]) {
      const current = page.getByRole("button", { name, exact: true });
      await current.click();
      await expect(current).toHaveAttribute("aria-current", "page");
      await expect(page.locator(".tabs button.active")).toHaveCount(1);
      await expect(
        page.getByRole("heading", { name: heading, exact: true }),
      ).toBeVisible();
      await expect
        .poll(() =>
          page.locator(".tabs button").evaluateAll((buttons) =>
            buttons
              .map((button) => ({
                active: button.classList.contains("active"),
                scale: new DOMMatrixReadOnly(
                  getComputedStyle(button, "::after").transform,
                ).a,
              }))
              .every((button) => button.scale === (button.active ? 1 : 0)),
          ),
        )
        .toBe(true);
      await page.screenshot({
        path: `${directory}/${width}-workspace-${tab}.png`,
        fullPage: true,
      });
      if (phase !== "before") await noHorizontalOverflow(page);
    }
    await page.getByRole("button", { name: "添加账号", exact: true }).click();
    await page.screenshot({
      path: `${directory}/${width}-add-account.png`,
      fullPage: true,
    });
    if (phase !== "before") await noHorizontalOverflow(page);
    await page.goto("/", { waitUntil: "domcontentloaded" });
    await page.getByRole("button", { name: "登录", exact: true }).click();
    await expect(
      page.getByRole("heading", { name: "输入 Apple 账号密码" }),
    ).toBeVisible();
    await page.screenshot({
      path: `${directory}/${width}-apple-login-prompt.png`,
      fullPage: true,
    });
    if (phase !== "before") await noHorizontalOverflow(page);
  }
});

test("UI review multiple account selection", async ({ page }) => {
  await uiReviewFixture(page, true);
  const directory = `artifacts/ui-review/${phase}`;
  await mkdir(directory, { recursive: true });
  for (const width of [1180, 390]) {
    await page.setViewportSize({ width, height: width === 1180 ? 800 : 844 });
    await page.goto("/", { waitUntil: "domcontentloaded" });
    await expect(
      page.getByRole("button", { name: "检查并使用所选账号" }),
    ).toBeVisible();
    await page.screenshot({
      path: `${directory}/${width}-account-selection.png`,
      fullPage: true,
    });
    if (phase !== "before") await noHorizontalOverflow(page);
  }
});

test("UI review Web access login", async ({ page }) => {
  await page.route("**/api/**", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ authenticated: false }),
    }),
  );
  const directory = `artifacts/ui-review/${phase}`;
  await mkdir(directory, { recursive: true });
  for (const width of [1180, 390]) {
    await page.setViewportSize({ width, height: width === 1180 ? 800 : 844 });
    await page.goto("/", { waitUntil: "domcontentloaded" });
    await expect(page.getByLabel("访问密码", { exact: true })).toBeVisible();
    await page.screenshot({
      path: `${directory}/${width}-web-login.png`,
      fullPage: true,
    });
    if (phase !== "before") await noHorizontalOverflow(page);
  }
});

test.describe("touch controls", () => {
  test.use({ hasTouch: true, isMobile: true });
  test("shared dropdown keyboard focus and touch number boundaries", async ({
    page,
  }) => {
    await uiReviewFixture(page);
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto("/#/queue", { waitUntil: "domcontentloaded" });
    await page.locator(".queue-policy > summary").click();
    const mode = page.getByRole("combobox", { name: "队列执行方式" });
    const count = page.getByRole("spinbutton", { name: "同时执行任务上限" });
    const decrease = page.getByRole("button", {
      name: "减少同时执行任务上限",
      exact: true,
    });
    const increase = page.getByRole("button", {
      name: "增加同时执行任务上限",
      exact: true,
    });
    await expect(count).toBeDisabled();
    await expect(decrease).toBeDisabled();
    await expect(increase).toBeDisabled();
    await mode.focus();
    await mode.press("ArrowDown");
    await expect(page.getByRole("listbox")).toBeVisible();
    await mkdir(`artifacts/ui-review/${phase}`, { recursive: true });
    await page.screenshot({
      path: `artifacts/ui-review/${phase}/390-queue-dropdown.png`,
      fullPage: true,
    });
    await mode.press("Escape");
    await expect(page.getByRole("listbox")).toHaveCount(0);
    await expect(mode).toBeFocused();
    await mode.press("End");
    await mode.press("Enter");
    await expect(mode).toContainText("固定并发");
    await expect(count).toBeEnabled();
    await expect(decrease).toBeDisabled();
    await increase.tap();
    await expect(count).toHaveValue("2");
    await count.fill("");
    await expect(count).toHaveValue("");
    await count.press("Enter");
    await expect(page.locator(".queue-policy > summary")).toContainText(
      "逐个执行",
    );
    await expect(
      page.getByRole("button", { name: "保存执行策略" }),
    ).toBeDisabled();
    await count.fill("99");
    await count.press("Tab");
    await expect(count).toHaveValue("10");
    await expect(increase).toBeDisabled();
    await count.fill("0");
    await count.press("Tab");
    await expect(count).toHaveValue("1");
    await expect(decrease).toBeDisabled();
    await count.fill("3");
    await page.getByRole("button", { name: "保存执行策略" }).click();
    await expect(page.locator(".queue-policy > summary")).toContainText(
      "最多 3 个任务",
    );
    for (const button of [decrease, increase]) {
      const box = await button.boundingBox();
      expect(box?.height).toBeGreaterThanOrEqual(44);
      expect(box?.width).toBeGreaterThanOrEqual(44);
    }
    await mode.tap();
    const list = page.getByRole("listbox");
    const bounds = await list.boundingBox();
    expect(bounds).not.toBeNull();
    expect(bounds!.x).toBeGreaterThanOrEqual(0);
    expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(390);
    await mode.press("Tab");
    await expect(list).toHaveCount(0);
    await noHorizontalOverflow(page);
  });
  test("touch scrolling a long menu preserves selection and viewport placement", async ({
    page,
  }) => {
    await uiReviewFixture(page);
    await page.setViewportSize({ width: 390, height: 600 });
    await mkdir(`artifacts/ui-review/${phase}`, { recursive: true });
    await page.goto("/#/logs", { waitUntil: "domcontentloaded" });
    await page.locator(".log-filter-panel > summary").click();
    const category = page.getByRole("combobox", { name: "日志类别" });
    const selectedBefore = await category.textContent();
    await category.scrollIntoViewIfNeeded();
    // Put the trigger near the bottom to exercise the upwards mobile menu.
    await page.evaluate(() =>
      window.scrollTo(
        0,
        Math.max(
          0,
          document.querySelector(".log-filter-panel")!.getBoundingClientRect()
            .top +
            scrollY -
            120,
        ),
      ),
    );
    await category.tap();
    const menu = page.getByRole("listbox", { name: "日志类别" });
    const bounds = await menu.boundingBox();
    expect(bounds).not.toBeNull();
    const triggerBounds = await category.boundingBox();
    expect(bounds!.y + bounds!.height).toBeLessThanOrEqual(triggerBounds!.y);
    await page.screenshot({
      path: `artifacts/ui-review/${phase}/390-touch-menu-upwards.png`,
    });
    const viewport = await page.evaluate(() => ({
      width: visualViewport!.width,
      height: visualViewport!.height,
      top: visualViewport!.offsetTop,
      left: visualViewport!.offsetLeft,
    }));
    expect(bounds!.x).toBeGreaterThanOrEqual(viewport.left);
    expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(
      viewport.left + viewport.width,
    );
    expect(bounds!.y).toBeGreaterThanOrEqual(viewport.top);
    expect(bounds!.y + bounds!.height).toBeLessThanOrEqual(
      viewport.top + viewport.height,
    );
    const cdp = await page.context().newCDPSession(page);
    const x = bounds!.x + bounds!.width / 2;
    const startY = bounds!.y + bounds!.height - 25;
    await cdp.send("Input.dispatchTouchEvent", {
      type: "touchStart",
      touchPoints: [{ x, y: startY }],
    });
    for (let step = 1; step <= 8; step++) {
      await cdp.send("Input.dispatchTouchEvent", {
        type: "touchMove",
        touchPoints: [{ x, y: startY - step * 20 }],
      });
      await page.waitForTimeout(30);
    }
    await cdp.send("Input.dispatchTouchEvent", {
      type: "touchEnd",
      touchPoints: [],
    });
    await expect(menu).toBeVisible();
    await expect(category).toHaveText(selectedBefore!);
    await expect
      .poll(() => menu.evaluate((el) => el.scrollTop))
      .toBeGreaterThan(0);
    await expect(category).toBeFocused();
    await category.press("Tab");
    await expect(menu).toHaveCount(0);
    await category.tap();
    const purchase = page.getByRole("option", { name: "下单", exact: true });
    await purchase.scrollIntoViewIfNeeded();
    await expect(menu).toBeVisible();
    await page.screenshot({
      path: `artifacts/ui-review/${phase}/390-touch-before-option.png`,
    });
    await purchase.tap();
    await expect(category).toContainText("下单");
    await expect(menu).toHaveCount(0);
    await expect(category).toBeFocused();
    await cdp.detach();
  });
});

test("recent availability keeps separate stores with time and model first", async ({
  page,
}) => {
  await uiReviewFixture(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/#/monitor", { waitUntil: "domcontentloaded" });
  const history = page.getByRole("region", { name: "最近有货记录" });
  const entries = history.getByRole("listitem");
  await expect(entries).toHaveCount(3);
  await expect(entries.nth(0)).toContainText("Apple 上海环贸 iapm");
  await expect(entries.nth(1)).toContainText("Apple 南京东路");
  await expect(entries.nth(0).locator("time")).toHaveAttribute("datetime", /T/);
  expect(await entries.nth(0).locator("time").getAttribute("datetime")).toBe(
    await entries.nth(1).locator("time").getAttribute("datetime"),
  );
  await expect(
    entries.nth(0).locator(".stock-event-product strong"),
  ).toHaveText("iPhone 18 Pro 256GB 银色");
  await expect(entries.nth(0).locator("time strong")).toHaveText(
    /\d{2}:\d{2}:\d{2}/,
  );
  await expect(history).toContainText("不代表当前库存");
  await history.getByRole("button", { name: "查看全部 4 条" }).click();
  await expect(entries).toHaveCount(4);
  await expect(entries.nth(3)).toContainText("抛光布");
  await noHorizontalOverflow(page);
});

test("expanded monitor and log forms keep mobile control geometry", async ({
  page,
}) => {
  await uiReviewFixture(page);
  const directory = `artifacts/ui-review/${phase}`;
  await mkdir(directory, { recursive: true });
  for (const width of [1180, 390, 320]) {
    await page.setViewportSize({ width, height: width === 1180 ? 800 : 844 });
    for (const route of ["monitor", "logs"]) {
      await page.goto(`/#/${route}`, { waitUntil: "domcontentloaded" });
      if (route === "monitor")
        await expect(
          page.getByRole("region", { name: "最近有货记录" }),
        ).toContainText("MJT84CH");
      const closed = page.locator("main details:not([open]) > summary");
      while (await closed.count()) await closed.first().click();
      await page.screenshot({
        path: `${directory}/${width}-${route}-expanded.png`,
        fullPage: true,
      });
      await expect(page.locator("select")).toHaveCount(0);
      await noHorizontalOverflow(page);
    }
  }
});
