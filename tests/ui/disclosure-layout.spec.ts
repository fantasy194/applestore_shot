import { expect, test, type Locator } from "@playwright/test";
import { mkdir } from "node:fs/promises";
import { noHorizontalOverflow, uiReviewFixture } from "./ui-review-fixture";

const directory = "artifacts/disclosure-layout";

async function geometry(target: Locator) {
  return target.evaluate((element) => {
    const measure = (selector: string) => {
      const box = element.querySelector(selector)!.getBoundingClientRect();
      return { x: box.x, width: box.width };
    };
    return {
      summary: measure(".monitor-target-settings > summary"),
      heading: measure(".inventory-target-heading"),
    };
  });
}

type Geometry = Awaited<ReturnType<typeof geometry>>;
function expectStable(actual: Geometry, baseline: Geometry) {
  for (const name of ["summary", "heading"] as const) {
    expect(Math.abs(actual[name].x - baseline[name].x)).toBeLessThanOrEqual(1);
    expect(
      Math.abs(actual[name].width - baseline[name].width),
    ).toBeLessThanOrEqual(1);
  }
}

for (const width of [1180, 390, 320]) {
  test(`monitor disclosure stays aligned throughout motion at ${width}px`, async ({
    page,
  }) => {
    await page.emulateMedia({ reducedMotion: "no-preference" });
    await page.setViewportSize({ width, height: 844 });
    await uiReviewFixture(page);
    await page.goto("/#/monitor", { waitUntil: "domcontentloaded" });
    const targets = page.locator(".inventory-target");
    await expect(targets).toHaveCount(2);
    const target = targets.first();
    const summary = target.locator(".monitor-target-settings > summary");
    const details = target.locator(".monitor-target-settings");
    await expect(details).not.toHaveAttribute("open", "");
    await expect(summary.locator("svg.disclosure-chevron")).toHaveCount(1);
    expect(
      await summary.evaluate(
        (element) => getComputedStyle(element).listStyleType,
      ),
    ).toBe("none");
    await summary.scrollIntoViewIfNeeded();
    const before = await geometry(target);
    await summary.click();
    await expect(details).toHaveAttribute("open", "");
    const samples = await target.evaluate(async (element) => {
      const frames = [];
      for (let frame = 0; frame < 24; frame++) {
        await new Promise<void>((resolve) =>
          requestAnimationFrame(() => resolve()),
        );
        const measure = (selector: string) => {
          const box = element.querySelector(selector)!.getBoundingClientRect();
          return { x: box.x, width: box.width };
        };
        frames.push({
          summary: measure(".monitor-target-settings > summary"),
          heading: measure(".inventory-target-heading"),
        });
      }
      await Promise.all(
        element
          .getAnimations({ subtree: true })
          .map((animation) => animation.finished.catch(() => {})),
      );
      return frames;
    });
    for (const frame of samples) expectStable(frame, before);
    expectStable(await geometry(target), before);
    const fields = target.locator(".inventory-purchase-fields");
    const remove = fields.getByRole("button", { name: /移除监控/ });
    const fieldBox = (await fields.boundingBox())!;
    const removeBox = (await remove.boundingBox())!;
    expect(
      Math.abs(fieldBox.x + fieldBox.width - removeBox.x - removeBox.width),
    ).toBeLessThanOrEqual(1);
    expect(removeBox.width).toBeGreaterThanOrEqual(44);
    expect(removeBox.height).toBeGreaterThanOrEqual(44);
    await noHorizontalOverflow(page);
    await mkdir(directory, { recursive: true });
    await page.screenshot({
      path: `${directory}/${width}-monitor-expanded.png`,
      fullPage: true,
    });
    await summary.click();
    await expect(details).not.toHaveAttribute("open", "");
    expectStable(await geometry(target), before);
    await noHorizontalOverflow(page);
  });
}

test("contact disclosure retains input and supports native keyboard with reduced motion", async ({
  page,
}) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.setViewportSize({ width: 390, height: 844 });
  await uiReviewFixture(page);
  await page.goto("/#/settings", { waitUntil: "domcontentloaded" });
  const summary = page
    .locator("summary")
    .filter({ hasText: "本地取货联系人（可选补充）" });
  const details = summary.locator("..");
  await expect(summary).toBeVisible();
  await expect(summary.locator("svg.disclosure-chevron")).toHaveCount(1);
  await summary.focus();
  if (
    !(await details.evaluate((element) => (element as HTMLDetailsElement).open))
  ) {
    await summary.press("Enter");
  }
  await expect(details).toHaveAttribute("open", "");
  const surname = details.getByLabel("取货人姓氏", { exact: true });
  await surname.fill("测试");
  await summary.focus();
  await summary.press("Space");
  await expect(details).not.toHaveAttribute("open", "");
  await summary.press("Enter");
  await expect(details).toHaveAttribute("open", "");
  await expect(surname).toHaveValue("测试");
  const motion = await summary
    .locator(".disclosure-chevron")
    .evaluate((element) => {
      const style = getComputedStyle(element);
      return {
        transition: style.transitionDuration,
        animation: style.animationName,
      };
    });
  expect(
    motion.transition
      .split(",")
      .every((duration) => parseFloat(duration) === 0),
  ).toBe(true);
  expect(motion.animation).toBe("none");
  const contentMotion = await details.evaluate((element) => {
    const style = getComputedStyle(element, "::details-content");
    return {
      transition: style.transitionDuration,
      animation: style.animationName,
    };
  });
  expect(
    contentMotion.transition
      .split(",")
      .every((duration) => parseFloat(duration) === 0),
  ).toBe(true);
  expect(contentMotion.animation).toBe("none");
  await noHorizontalOverflow(page);
  await mkdir(directory, { recursive: true });
  await page.screenshot({
    path: `${directory}/390-contact-expanded.png`,
    fullPage: true,
  });
});
