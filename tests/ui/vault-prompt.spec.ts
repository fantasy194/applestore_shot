import { test, expect, type Page } from "@playwright/test";
import type { Prompt } from "../../src/types";

type Storage = Prompt["credential_storage"];
const encrypted: Storage = {
  backend: "encrypted_file",
  available: true,
  persistent: true,
};

async function fixture(page: Page, storage?: Storage) {
  await page.addInitScript(
    ({ storage }) => {
      const root = window as any;
      const account = {
        id: "fixture-vault-account",
        name: "密码测试账号",
        email: "vault@example.test",
        pending: false,
      };
      const task: any = {
        id: "fixture-password-task",
        account_id: account.id,
        action: "login",
        status: "waiting",
        stage: "等待密码",
        created_at: 1000,
        updated_at: 1000,
        logs: [],
        prompt: {
          id: "fixture-password-prompt",
          kind: "password",
          label: "请输入 Apple 密码",
          credential_storage: storage,
        },
        result: null,
        order_submitted: false,
      };
      let update = (_task: any) => {};
      root.vaultPromptReplies = [];
      root.updateVaultStorage = (next: any) => {
        task.prompt.credential_storage = next;
        task.updated_at++;
        update(structuredClone(task));
      };
      root.__DUO_KILLER_TEST__ = {
        snapshot: async () => ({
          accounts: [account],
          tasks: [structuredClone(task)],
          data_dir: "/synthetic-vault-fixture",
        }),
        subscribe: async (callback: any) => {
          update = callback;
          return () => {};
        },
        reply: async (
          accountId: string,
          taskId: string,
          requestId: string,
          value: unknown,
        ) => {
          root.vaultPromptReplies.push({ accountId, taskId, requestId, value });
        },
        start: async () => {
          throw new Error("Synthetic prompt must not start a task");
        },
        add: async () => {
          throw new Error("Not allowed");
        },
        remove: async () => {},
        savePreset: async () => {},
        cancel: async () => {},
      };
    },
    { storage },
  );
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await expect(
    page.getByRole("heading", { name: "请输入 Apple 密码", exact: true }),
  ).toBeVisible();
}

async function submit(page: Page, remember: boolean) {
  await page
    .getByLabel("密码", { exact: true })
    .fill("synthetic-vault-password");
  await page
    .locator(".prompt-form")
    .getByRole("button", { name: "继续", exact: true })
    .click();
  await expect
    .poll(() => page.evaluate(() => (window as any).vaultPromptReplies))
    .toEqual([
      {
        accountId: "fixture-vault-account",
        taskId: "fixture-password-task",
        requestId: "fixture-password-prompt",
        value: { password: "synthetic-vault-password", remember },
      },
    ]);
}

test("容器加密保存需要明确勾选，默认只在本次运行复用", async ({ page }) => {
  await fixture(page, encrypted);
  await expect(
    page.getByRole("checkbox", {
      name: "加密保存密码，重启后继续使用",
      exact: true,
    }),
  ).not.toBeChecked();
  await expect(page.locator(".prompt-form")).toContainText(
    "服务重启或升级后仍可复用",
  );
  await submit(page, false);
});

test("明确勾选容器加密保存后按当前账号和提示回复 remember=true", async ({
  page,
}) => {
  await fixture(page, encrypted);
  await page
    .getByRole("checkbox", {
      name: "加密保存密码，重启后继续使用",
      exact: true,
    })
    .check();
  await submit(page, true);
});

for (const [name, storage, message] of [
  [
    "未配置",
    { backend: null, available: false, persistent: false },
    "此服务尚未启用密码保存",
  ],
  [
    "存储错误",
    {
      backend: "encrypted_file",
      available: false,
      persistent: false,
      error: "storage_unavailable",
    },
    "密码保存暂不可用",
  ],
  [
    "非持久后端",
    { backend: "encrypted_file", available: true, persistent: false },
    "此服务尚未启用密码保存",
  ],
  ["旧服务缺少状态", undefined, "此服务尚未启用密码保存"],
] as const) {
  test(`${name}时隐藏保存选择，继续只回复 remember=false`, async ({ page }) => {
    await fixture(page, storage);
    await expect(
      page.locator(".prompt-form").getByRole("checkbox"),
    ).toHaveCount(0);
    await expect(page.locator(".prompt-form")).toContainText(message);
    await submit(page, false);
  });
}

for (const backend of ["windows", "macos"] as const) {
  test(`${backend} 原生后端显示系统凭据保险库并保留明确选择`, async ({
    page,
  }) => {
    await fixture(page, { backend, available: true, persistent: true });
    const checkbox = page.getByRole("checkbox", {
      name: "保存到系统凭据保险库",
      exact: true,
    });
    await expect(checkbox).not.toBeChecked();
    await expect(page.locator(".prompt-form")).toContainText(
      "勾选后保存到系统凭据保险库",
    );
    await expect(
      page.getByText("加密保存密码，重启后继续使用", { exact: true }),
    ).toHaveCount(0);
    await checkbox.check();
    await submit(page, true);
  });
}

test("勾选后存储状态失效也不会提交保存授权，390px 提示无溢出", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await fixture(page, encrypted);
  await page
    .getByRole("checkbox", {
      name: "加密保存密码，重启后继续使用",
      exact: true,
    })
    .check();
  await page.locator(".prompt-form").scrollIntoViewIfNeeded();
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth),
  ).toBeLessThanOrEqual(390);
  await page
    .locator(".prompt-form")
    .screenshot({ path: "artifacts/goal-017-vault-prompt-mobile.png" });
  await page.evaluate(() =>
    (window as any).updateVaultStorage({
      backend: "encrypted_file",
      available: false,
      persistent: false,
      error: "storage_unavailable",
    }),
  );
  await expect(page.locator(".prompt-form").getByRole("checkbox")).toHaveCount(
    0,
  );
  await expect(page.locator(".prompt-form")).toContainText("密码保存暂不可用");
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth),
  ).toBeLessThanOrEqual(390);
  await page
    .locator(".prompt-form")
    .screenshot({ path: "artifacts/goal-017-vault-prompt-error-mobile.png" });
  await submit(page, false);
});
