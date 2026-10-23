import { expect, test, type Page } from "@playwright/test";
import { uiReviewFixture } from "./ui-review-fixture";

const key = (id = "review-a") => `duo.purchase-draft.v1:${id}`;
async function fixture(
  page: Page,
  options: { multiple?: boolean; seed?: string; storageThrows?: boolean } = {},
) {
  await uiReviewFixture(page, options.multiple);
  await page.addInitScript(({ seed, storageThrows }) => {
    const root = window as any;
    if (seed !== undefined && !sessionStorage.getItem("draft-seeded")) {
      localStorage.setItem("duo.purchase-draft.v1:review-a", seed);
      sessionStorage.setItem("draft-seeded", "yes");
    }
    if (storageThrows) {
      const get = Storage.prototype.getItem;
      const set = Storage.prototype.setItem;
      Storage.prototype.getItem = function (key) {
        if (key.startsWith("duo.purchase-draft"))
          throw new DOMException("Blocked", "SecurityError");
        return get.call(this, key);
      };
      Storage.prototype.setItem = function (key, value) {
        if (key.startsWith("duo.purchase-draft"))
          throw new DOMException("Full", "QuotaExceededError");
        return set.call(this, key, value);
      };
    }
    const api = root.__DUO_KILLER_TEST__;
    const snapshot = api.snapshot;
    api.snapshot = async () => {
      if (!root.draftState) {
        root.draftState = await snapshot();
        if (root.draftState.accounts[1])
          Object.assign(root.draftState.accounts[1], {
            preset_sku: "MK2P4CH",
            preset_quantity: 4,
          });
      }
      return structuredClone(root.draftState);
    };
    const subscribe = api.subscribe;
    api.subscribe = async (task: any, snapshot: any) => {
      root.draftTask = task;
      root.draftSnapshot = snapshot;
      return subscribe(task);
    };
    const start = api.start;
    api.start = async (accountId: string, action: string, params: any) => {
      if (action !== "me") return start(accountId, action, params);
      root.uiReviewCalls.push({ accountId, action, params });
      const account = root.draftState.accounts.find(
        (a: any) => a.id === accountId,
      );
      const task = {
        id: `draft-me-${accountId}`,
        account_id: accountId,
        action,
        status: "succeeded",
        stage: "已确认",
        created_at: Date.now() / 1000,
        updated_at: Date.now() / 1000,
        logs: [],
        prompt: null,
        result: {
          success: true,
          logged_in: true,
          scope: "account",
          apple_id: account.email,
          name: account.name,
        },
      };
      root.draftState.tasks.unshift(task);
      root.draftTask(structuredClone(task));
      return task.id;
    };
    api.task = async (_accountId: string, id: string) =>
      structuredClone(
        root.draftState.tasks.find((task: any) => task.id === id),
      );
  }, options);
}
function purchase(page: Page) {
  return page.locator("section.section").filter({
    has: page.getByRole("heading", { name: "购买商品", exact: true }),
  });
}
async function choose(page: Page, name = "抛光布") {
  await purchase(page).getByRole("combobox", { name: "预制商品" }).click();
  await page.getByRole("option", { name: new RegExp(name) }).click();
}
async function expectDraft(page: Page, sku: string, quantity: number) {
  await expect(
    purchase(page).getByRole("combobox", { name: "预制商品" }),
  ).toContainText(sku);
  await expect(
    purchase(page).getByRole("spinbutton", { name: "数量", exact: true }),
  ).toHaveValue(String(quantity));
}
async function noAutomaticPurchase(page: Page) {
  const calls = await page.evaluate(() => (window as any).uiReviewCalls);
  expect(
    calls.filter((call: any) =>
      ["purchase", "checkout", "save_preset"].includes(call.action),
    ),
  ).toEqual([]);
}

test("purchase draft survives task completion, snapshot preset change and reload", async ({
  page,
}) => {
  await fixture(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await expectDraft(page, "MJT84CH", 1);
  await choose(page);
  await purchase(page)
    .getByRole("spinbutton", { name: "数量", exact: true })
    .fill("7");
  await expectDraft(page, "MLH74FE", 7);
  await page.evaluate(() => {
    const root = window as any;
    const task = {
      id: "synthetic-purchase",
      account_id: "review-a",
      action: "purchase",
      status: "succeeded",
      stage: "模拟结束",
      result: { success: true },
      created_at: Date.now() / 1000,
      updated_at: Date.now() / 1000,
      logs: [],
      prompt: null,
    };
    root.draftState.tasks.unshift(task);
    root.draftTask(task);
    Object.assign(root.draftState.accounts[0], {
      preset_sku: "MK2P4CH",
      preset_quantity: 99,
    });
    root.draftSnapshot(structuredClone(root.draftState));
  });
  await expectDraft(page, "MLH74FE", 7);
  expect(
    await page.evaluate((key) => JSON.parse(localStorage.getItem(key)!), key()),
  ).toEqual({ version: 1, sku: "MLH74FE", quantity: 7 });
  await page.reload({ waitUntil: "domcontentloaded" });
  await expectDraft(page, "MLH74FE", 7);
  await noAutomaticPurchase(page);
});

test("untouched draft follows account presets until the user explicitly chooses", async ({
  page,
}) => {
  await fixture(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await expectDraft(page, "MJT84CH", 1);
  await page.evaluate(() => {
    const root = window as any;
    Object.assign(root.draftState.accounts[0], {
      preset_sku: "MLH74FE",
      preset_quantity: 3,
    });
    root.draftSnapshot(structuredClone(root.draftState));
  });
  await expectDraft(page, "MLH74FE", 3);
  expect(
    await page.evaluate((key) => localStorage.getItem(key), key()),
  ).toBeNull();
  await choose(page, "256GB Star White");
  await purchase(page)
    .getByRole("spinbutton", { name: "数量", exact: true })
    .fill("5");
  await page.evaluate(() => {
    const root = window as any;
    Object.assign(root.draftState.accounts[0], {
      preset_sku: "MJT84CH",
      preset_quantity: 9,
    });
    root.draftSnapshot(structuredClone(root.draftState));
  });
  await expectDraft(page, "MK2M4CH", 5);
  await noAutomaticPurchase(page);
});

test("catalog selection uses the same browser draft", async ({ page }) => {
  await fixture(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.getByRole("button", { name: "商品", exact: true }).click();
  const catalog = page.locator("section.section").filter({
    has: page.getByRole("heading", { name: "预制商品", exact: true }),
  });
  await catalog
    .getByRole("row")
    .filter({ hasText: "抛光布" })
    .getByRole("button", { name: "选择", exact: true })
    .click();
  await expectDraft(page, "MLH74FE", 1);
  await page.reload({ waitUntil: "domcontentloaded" });
  await expectDraft(page, "MLH74FE", 1);
  await noAutomaticPurchase(page);
});

test("account switches restore isolated drafts without copying A into B", async ({
  page,
}) => {
  await fixture(page, { multiple: true });
  await page.goto("/", { waitUntil: "domcontentloaded" });
  // Account setup is explicit fixture-only session verification, never a purchase.
  const checkboxes = page.getByRole("checkbox");
  for (const checkbox of await checkboxes.all()) await checkbox.check();
  await page
    .getByRole("button", { name: "检查并使用所选账号", exact: true })
    .click();
  await expectDraft(page, "MJT84CH", 1);
  await choose(page);
  await purchase(page)
    .getByRole("spinbutton", { name: "数量", exact: true })
    .fill("7");
  const accounts = page.getByRole("navigation", { name: "切换账号" });
  await accounts.getByRole("button", { name: /备用账号/ }).click();
  await expectDraft(page, "MK2P4CH", 4);
  await purchase(page)
    .getByRole("spinbutton", { name: "数量", exact: true })
    .fill("3");
  await accounts.getByRole("button", { name: /日常账号/ }).click();
  await expectDraft(page, "MLH74FE", 7);
  await accounts.getByRole("button", { name: /备用账号/ }).click();
  await expectDraft(page, "MK2P4CH", 3);
  expect(
    await page.evaluate(
      (keys) => keys.map((key) => JSON.parse(localStorage.getItem(key)!)),
      [key(), key("review-b")],
    ),
  ).toEqual([
    { version: 1, sku: "MLH74FE", quantity: 7 },
    { version: 1, sku: "MK2P4CH", quantity: 3 },
  ]);
  await noAutomaticPurchase(page);
});

for (const [name, seed] of [
  ["corrupt JSON", "{bad"],
  [
    "wrong version",
    JSON.stringify({ version: 2, sku: "MLH74FE", quantity: 7 }),
  ],
  [
    "malicious SKU",
    JSON.stringify({
      version: 1,
      sku: "<img src=x onerror=alert(1)>",
      quantity: 7,
    }),
  ],
  ["too many", JSON.stringify({ version: 1, sku: "MLH74FE", quantity: 1000 })],
  ["fractional", JSON.stringify({ version: 1, sku: "MLH74FE", quantity: 1.5 })],
  ["zero", JSON.stringify({ version: 1, sku: "MLH74FE", quantity: 0 })],
  [
    "sensitive extra fields",
    JSON.stringify({
      version: 1,
      sku: "MLH74FE",
      quantity: 7,
      token: "synthetic-rejected",
    }),
  ],
]) {
  test(`invalid browser draft safely falls back: ${name}`, async ({ page }) => {
    await fixture(page, { seed });
    await page.goto("/", { waitUntil: "domcontentloaded" });
    await expectDraft(page, "MJT84CH", 1);
    await noAutomaticPurchase(page);
  });
}

test("storage exceptions preserve usable in-memory choices without automatic actions", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await fixture(page, { storageThrows: true });
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await expectDraft(page, "MJT84CH", 1);
  await choose(page);
  await purchase(page)
    .getByRole("spinbutton", { name: "数量", exact: true })
    .fill("9");
  await expectDraft(page, "MLH74FE", 9);
  await page.getByRole("button", { name: "购物袋", exact: true }).click();
  await page.getByRole("button", { name: "工作台", exact: true }).click();
  await expectDraft(page, "MLH74FE", 9);
  expect(errors).toEqual([]);
  await noAutomaticPurchase(page);
});
