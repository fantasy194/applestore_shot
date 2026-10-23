import { expect, test } from "@playwright/test";
import { mkdir } from "node:fs/promises";
import { noHorizontalOverflow, uiReviewFixture } from "./ui-review-fixture";

for (const width of [320, 1180]) {
  test(`${width}px监控间隔和队列时效开关可保存且无横向溢出`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 844 });
    await uiReviewFixture(page);
    await page.goto("/#/monitor", { waitUntil: "domcontentloaded" });
    const interval = page.getByRole("spinbutton", {
      name: "监控检查间隔（秒）",
    });
    await expect(interval).toHaveValue("1");
    await interval.fill("30");
    await interval.blur();
    await page.getByRole("button", { name: "保存配置", exact: true }).click();
    await expect(
      page.getByText("监控配置已保存", { exact: true }),
    ).toBeVisible();
    const saved = await page.evaluate(() =>
      (window as any).uiReviewCalls.find(
        (item: any) => item.action === "inventory_settings_save",
      ),
    );
    expect(saved.params.config.interval_seconds).toBe(30);
    await interval.fill("");
    await expect(
      page.getByRole("button", { name: "保存并开始监控" }),
    ).toBeDisabled();
    await interval.fill("30");
    await interval.blur();
    await noHorizontalOverflow(page);
    await mkdir("artifacts/ui-review/monitor-queue-settings", {
      recursive: true,
    });
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({
      path: `artifacts/ui-review/monitor-queue-settings/monitor-${width}.png`,
      fullPage: true,
    });
    await page.goto("/#/queue", { waitUntil: "domcontentloaded" });
    await page.locator(".queue-policy > summary").click();
    const check = page.getByRole("checkbox", {
      name: "下单前检查库存样本时效",
    });
    await expect(check).toBeChecked();
    await check.uncheck();
    await page.getByRole("button", { name: "保存执行策略" }).click();
    await expect(
      page.getByRole("button", { name: "保存执行策略" }),
    ).toBeDisabled();
    const state = await page.evaluate(() =>
      (window as any).__DUO_KILLER_TEST__.queue("status"),
    );
    expect(state.check_inventory_freshness).toBe(false);
    await noHorizontalOverflow(page);
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({
      path: `artifacts/ui-review/monitor-queue-settings/queue-${width}.png`,
      fullPage: true,
    });
  });
}
