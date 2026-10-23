// One bounded anonymous store discovery through the running monitor. All
// preference changes stay in browser memory; no account, order or monitor
// configuration is saved, and no existing task is stopped.
import { chromium } from "@playwright/test";
import { createHash } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { parseArgs } from "node:util";
import { pwaDirectory } from "./pwa-paths.mjs";

let stage = "configuration";
let browser;
const ensure = (condition) => {
  if (!condition) throw new Error("pickup_acceptance_failed");
};
const LOCAL_ID = "00000000-0000-0000-0000-000000000000";
const active = (task) =>
  ["running", "waiting", "queued", "scheduled"].includes(task.status);
const signature = (targets) =>
  JSON.stringify(
    (targets || []).map((row) => [
      row.id,
      row.sku,
      row.quantity,
      row.store_id,
      row.location,
      row.account_id,
      row.auto_purchase,
      row.concurrency,
      row.retry_count,
    ]),
  );

async function main() {
  const { values } = parseArgs({
    options: {
      "data-dir": { type: "string" },
      "password-file": {
        type: "string",
        default: ".local-runtime/pwa-access.json",
      },
    },
  });
  const directory = pwaDirectory(values["data-dir"]);
  const { origin } = JSON.parse(
    await readFile(join(directory, "tunnel.json"), "utf8"),
  );
  const { password } = JSON.parse(
    await readFile(values["password-file"], "utf8"),
  );
  ensure(/^https:\/\/[a-z0-9-]+\.trycloudflare\.com$/.test(origin));
  const report = { origin, at: new Date().toISOString(), checks: [] };
  const files = async (snapshot) =>
    Promise.all(
      [LOCAL_ID, ...snapshot.accounts.map((row) => row.id)].map(async (id) => {
        try {
          const raw = await readFile(
            id === LOCAL_ID
              ? join(directory, "settings", "checkout.json")
              : join(directory, "accounts", id, "checkout.json"),
          );
          return createHash("sha256").update(raw).digest("hex");
        } catch (error) {
          if (error.code === "ENOENT") return null;
          throw error;
        }
      }),
    );
  browser = await chromium.launch();
  const context = await browser.newContext({
    viewport: { width: 390, height: 844 },
  });
  const page = await context.newPage();
  page.setDefaultTimeout(20_000);
  let pageErrors = 0;
  page.on("pageerror", () => pageErrors++);
  const query = async (command, data = {}) => {
    const response = await context.request.post(`${origin}/api/${command}`, {
      data,
      headers: { Origin: origin },
      timeout: 20_000,
      maxRedirects: 0,
    });
    ensure(response.status() === 200);
    return response.json();
  };
  stage = "pwa_login";
  await page.goto(origin, { waitUntil: "domcontentloaded", timeout: 45_000 });
  await page.getByLabel("访问密码", { exact: true }).fill(password);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await page.getByRole("link", { name: "本地配置", exact: true }).waitFor();
  const before = await query("snapshot");
  const monitors = before.tasks.filter(
    (task) => task.action === "inventory_monitor" && active(task),
  );
  ensure(monitors.length === 1);
  const monitor = monitors[0];
  const sample = monitor.result?.targets?.find(
    (target) => target.sku && target.location,
  );
  ensure(sample);
  const preserved = await files(before);
  const monitorTargets = signature(monitor.result.targets);
  report.checks.push("existing_monitor_running");

  stage = "unsaved_preferences_ui";
  await page.getByRole("link", { name: "本地配置", exact: true }).click();
  const preferences = page.locator(".pickup-preferences");
  await preferences
    .getByRole("heading", { name: "自提门店与时段偏好", exact: true })
    .waitFor();
  await preferences
    .getByRole("combobox", { name: /^门店选择方式/ })
    .selectOption("priority");
  ensure(
    (await preferences
      .getByRole("combobox", { name: /^取货联系人来源/ })
      .inputValue()) === "official",
  );
  await preferences
    .getByRole("combobox", { name: /^自提时段规则/ })
    .selectOption("window");
  await preferences.getByLabel("可接受开始时间", { exact: true }).fill("10:00");
  await preferences.getByLabel("可接受结束时间", { exact: true }).fill("16:00");
  await preferences
    .getByRole("combobox", { name: /^无匹配时段/ })
    .selectOption("stop");
  ensure(
    await preferences
      .getByRole("button", { name: "保存自提预设", exact: true })
      .isEnabled(),
  );
  ensure(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  );
  report.checks.push(
    "ordered_store_and_window_controls_mobile_390",
    "official_contact_without_local_required_fields",
  );

  stage = "running_monitor_discovery_ui";
  await page.getByRole("link", { name: "自动监控", exact: true }).click();
  await page
    .getByRole("heading", { name: "添加门店与商品", exact: true })
    .waitFor();
  await page
    .getByText("读取监控配置…", { exact: true })
    .waitFor({ state: "hidden", timeout: 30_000 });
  await page.getByLabel("商品 SKU", { exact: true }).fill(sample.sku);
  await page
    .getByLabel("门店搜索城市 / 邮编", { exact: true })
    .fill(sample.location);
  const search = page.getByRole("button", {
    name: "从官网读取门店",
    exact: true,
  });
  ensure(await search.isEnabled());
  const started = page.waitForResponse((response) => {
    if (new URL(response.url()).pathname !== "/api/start_task") return false;
    try {
      return response.request().postDataJSON().action === "store_search";
    } catch {
      return false;
    }
  });
  await search.click();
  const startedResponse = await started;
  ensure(startedResponse.status() === 200);
  const queryTaskId = await startedResponse.json();
  await page
    .getByLabel("官网门店结果", { exact: true })
    .waitFor({ timeout: 45_000 });
  ensure((await page.locator(".store-search-results input").count()) > 0);

  stage = "monitor_and_contact_preservation";
  const deadline = Date.now() + 12_000;
  let after, sameMonitor, task;
  do {
    after = await query("snapshot");
    sameMonitor = after.tasks.find((row) => row.id === monitor.id);
    task = after.tasks.find((row) => row.id === queryTaskId);
    if (
      sameMonitor?.status === "running" &&
      task?.status === "succeeded" &&
      sameMonitor.result?.checked_at !== monitor.result?.checked_at
    )
      break;
    await page.waitForTimeout(250);
  } while (Date.now() < deadline);
  ensure(sameMonitor?.status === "running" && task?.status === "succeeded");
  ensure(sameMonitor.result?.checked_at !== monitor.result?.checked_at);
  ensure(signature(sameMonitor.result.targets) === monitorTargets);
  ensure(
    after.tasks.filter(
      (row) => row.action === "inventory_monitor" && active(row),
    ).length === 1,
  );
  ensure(task.result?.stores?.length > 0 && task.result?.success === true);
  ensure(JSON.stringify(await files(after)) === JSON.stringify(preserved));
  const log = await query("activity_logs", {
    params: { task_id: queryTaskId, category: "inventory", limit: 100 },
  });
  ensure(
    log.entries.some((entry) => entry.event === "inventory_shared_query_end"),
  );
  report.store_count = task.result.stores.length;
  report.monitor_target_count = sameMonitor.result.targets.length;
  report.dispatchable_count = sameMonitor.result.proxy?.dispatchable_count;
  report.checks.push(
    "official_stores_through_same_running_monitor_pool",
    "monitor_advanced_without_restart",
    "monitor_targets_and_checkout_files_preserved",
    "shared_query_audit_record",
  );
  ensure(pageErrors === 0);
  report.checks.push("no_page_errors");
  await mkdir("artifacts", { recursive: true });
  await writeFile(
    "artifacts/pickup-preferences-live-verification.json",
    JSON.stringify(report, null, 2),
  );
  console.log(JSON.stringify(report));
}

try {
  await main();
} catch {
  console.error(
    JSON.stringify({
      ok: false,
      stage,
      error: "pickup_preferences_live_acceptance_failed",
    }),
  );
  process.exitCode = 1;
} finally {
  await browser?.close();
}
