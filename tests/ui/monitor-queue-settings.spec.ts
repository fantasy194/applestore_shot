import { expect, test } from "@playwright/test";
import { mkdir } from "node:fs/promises";
import { noHorizontalOverflow, uiReviewFixture } from "./ui-review-fixture";

for (const width of [320, 1180]) {
  test(`${width}px监控间隔和队列并发可保存，库存信号只负责入队`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 844 });
    await uiReviewFixture(page);
    await page.addInitScript(() => {
      const root = window as any;
      const queue = root.__DUO_KILLER_TEST__.queue;
      root.queueSettingsCalls = [];
      root.__DUO_KILLER_TEST__.queue = async (
        operation: string,
        params: any,
      ) => {
        root.queueSettingsCalls.push({ operation, params });
        return queue(operation, params);
      };
    });
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
    await expect(
      page.getByRole("checkbox", { name: "下单前检查库存样本时效" }),
    ).toHaveCount(0);
    await expect(page.locator(".queue-policy")).toContainText(
      "后续监控显示无货、未知或样本过期不会阻止排队执行",
    );
    await page.getByRole("combobox", { name: "队列执行方式" }).click();
    await page.getByRole("option", { name: "固定并发", exact: true }).click();
    await page.getByRole("spinbutton", { name: "同时执行任务上限" }).fill("2");
    await page.getByRole("button", { name: "保存执行策略" }).click();
    await expect(
      page.getByRole("button", { name: "保存执行策略" }),
    ).toBeDisabled();
    const savedPolicy = await page.evaluate(
      () =>
        (window as any).queueSettingsCalls.find(
          (call: any) => call.operation === "configure",
        )?.params,
    );
    expect(savedPolicy).toEqual({ mode: "parallel", max_concurrency: 2 });
    await noHorizontalOverflow(page);
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({
      path: `artifacts/ui-review/monitor-queue-settings/queue-${width}.png`,
      fullPage: true,
    });
  });
}
