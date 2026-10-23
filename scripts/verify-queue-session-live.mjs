// Read-only HTTPS acceptance. Never starts Apple login, purchases, maintenance,
// monitoring, proxy extraction, queue mutations or a remote browser session.
// Private API bodies, account identities, frames and credentials stay in memory.
import { chromium } from "@playwright/test";
import { randomUUID } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { parseArgs } from "node:util";
import { pwaDirectory } from "./pwa-paths.mjs";

let stage = "configuration";
let browser;
const ensure = (value) => {
  if (!value) throw new Error("read_only_acceptance_failed");
};

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
  ensure(/^https:\/\/[a-z0-9-]+\.trycloudflare\.com$/.test(origin));
  const { password } = JSON.parse(
    await readFile(values["password-file"], "utf8"),
  );
  ensure(typeof password === "string" && password.length >= 8);

  const report = { origin, at: new Date().toISOString(), checks: [] };
  const missingAccount = randomUUID();
  const missingTask = randomUUID();
  const reads = [
    ["queue_command", { operation: "status", params: {} }],
    ["activity_logs", { params: { limit: 25 } }],
    ["maintenance_command", { operation: "status", params: {} }],
    [
      "browser_command",
      {
        accountId: missingAccount,
        taskId: missingTask,
        operation: "status",
        params: {},
      },
    ],
  ];

  stage = "browser_start";
  browser = await chromium.launch();
  const context = await browser.newContext({
    viewport: { width: 390, height: 844 },
  });
  const page = await context.newPage();
  page.setDefaultTimeout(20_000);
  page.setDefaultNavigationTimeout(45_000);
  let pageErrors = 0;
  page.on("pageerror", () => {
    // Error strings may contain API or private page data. Keep only the count.
    pageErrors++;
  });
  const post = (path, data, requestOrigin = origin) =>
    context.request.post(`${origin}/api/${path}`, {
      data,
      headers: { Origin: requestOrigin },
      timeout: 20_000,
      maxRedirects: 0,
    });
  const query = async (path, data) => {
    const response = await post(path, data);
    ensure(response.status() === 200);
    return response.json();
  };
  const fitsPhone = async () =>
    page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    );

  stage = "unauthenticated_isolation";
  for (const [path, body] of reads) {
    const response = await post(path, body);
    ensure(response.status() === 401);
  }
  report.checks.push("four_new_apis_unauthenticated_401");

  stage = "pwa_password_login";
  await page.goto(origin, { waitUntil: "domcontentloaded" });
  await page.getByLabel("访问密码", { exact: true }).fill(password);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await page.getByRole("link", { name: "执行队列", exact: true }).waitFor();
  const cookie = (await context.cookies()).find(
    (item) => item.name === "duo_session",
  );
  ensure(cookie?.secure && cookie.httpOnly && cookie.sameSite === "Strict");
  report.checks.push("https_password_login_secure_cookie");

  stage = "workspace_initial_snapshot";
  await page.getByText("正在读取本地配置", { exact: true }).waitFor({
    state: "hidden", timeout: 60_000,
  });

  stage = "foreign_origin_isolation";
  for (const [path, body] of reads) {
    const response = await post(path, body, "https://foreign.invalid");
    ensure(response.status() === 403);
  }
  report.checks.push("four_new_apis_foreign_origin_403");

  stage = "queue_read";
  const queue = await query("queue_command", {
    operation: "status",
    params: {},
  });
  ensure(
    ["sequential", "parallel"].includes(queue.mode) &&
      Number.isInteger(queue.max_concurrency) &&
      queue.max_concurrency >= 1 &&
      queue.max_concurrency <= 10 &&
      typeof queue.paused === "boolean" &&
      Array.isArray(queue.jobs),
  );
  report.queue_mode = queue.mode;
  report.queue_max_concurrency = queue.max_concurrency;
  report.queue_jobs = queue.jobs.length;
  report.queue_inflight_jobs = queue.jobs.filter((job) =>
    ["dispatched", "running", "cancelling"].includes(job.status),
  ).length;
  report.checks.push("queue_status_schema_read");

  stage = "queue_page_navigation";
  await page.getByRole("link", { name: "执行队列", exact: true }).click();
  await page.getByRole("heading", { name: "执行队列", exact: true }).waitFor();
  await page.locator(".queue-policy > summary").click();
  // The wrapping label includes the select's option text in its accessible
  // name. Match its stable leading label while still requiring one combobox.
  const mode = page.getByRole("combobox", { name: /^队列执行方式/ });
  stage = "queue_page_controls";
  ensure((await mode.count()) === 1);
  const readyButton = page.getByRole("button", {
    name: queue.paused ? "恢复队列" : "暂停队列",
    exact: true,
  });
  await readyButton.waitFor();
  const deadline = Date.now() + 15_000;
  while (
    ((await mode.inputValue()) !== queue.mode ||
      !(await readyButton.isEnabled())) &&
    Date.now() < deadline
  )
    await page.waitForTimeout(100);
  ensure((await mode.inputValue()) === queue.mode);
  ensure(await readyButton.isEnabled());
  stage = "queue_page_errors";
  ensure((await page.locator(".purchase-queue [role=alert]").count()) === 0);
  stage = "queue_page_geometry";
  ensure(await fitsPhone());
  report.checks.push("queue_page_readable_mobile_width_390");

  stage = "activity_logs_read";
  const logs = await query("activity_logs", { params: { limit: 25 } });
  ensure(Array.isArray(logs.entries));
  const categories = [
    "inventory",
    "stock",
    "queue",
    "authentication",
    "proxy",
    "purchase",
    "operation",
  ];
  const businessEntries = logs.entries.filter(
    (entry) =>
      categories.includes(entry.category) &&
      (typeof entry.message === "string" || typeof entry.event === "string"),
  );
  ensure(businessEntries.length > 0);
  report.log_sample_entries = logs.entries.length;
  report.business_log_sample_entries = businessEntries.length;
  report.log_category_counts = Object.fromEntries(
    categories.map((category) => [
      category,
      logs.entries.filter((entry) => entry.category === category).length,
    ]),
  );
  report.checks.push("activity_log_api_contains_business_records");

  stage = "activity_logs_page";
  await page.getByRole("link", { name: "运行日志", exact: true }).click();
  await page.getByRole("heading", { name: "运行日志", exact: true }).waitFor();
  await page.locator(".activity-logs .log-entry").first().waitFor();
  ensure((await page.locator(".activity-logs [role=alert]").count()) === 0);
  ensure(await fitsPhone());
  report.checks.push("activity_log_page_readable_mobile_width_390");

  stage = "maintenance_read";
  const maintenance = await query("maintenance_command", {
    operation: "status",
    params: {},
  });
  ensure(
    typeof maintenance.enabled === "boolean" &&
      maintenance.mode === "idle_warm_login" &&
      maintenance.interval_minutes === 10 &&
      Array.isArray(maintenance.last_runs) &&
      !maintenance.settings_error,
  );
  report.maintenance_enabled = maintenance.enabled;
  report.maintenance_interval_minutes = maintenance.interval_minutes;
  report.maintenance_accounts = maintenance.last_runs.length;
  report.checks.push("maintenance_status_read");
  report.checks.push(
    maintenance.enabled && maintenance.interval_minutes === 10
      ? "maintenance_idle_warm_login_10m_enabled"
      : "maintenance_existing_configuration_preserved",
  );

  stage = "missing_browser_isolation";
  const missing = await post("browser_command", reads[3][1]);
  ensure(missing.status() === 400);
  const missingBody = await missing.json();
  ensure(
    missingBody &&
      Object.keys(missingBody).length === 1 &&
      typeof missingBody.error === "string" &&
      missingBody.error.length <= 2000 &&
      !/[a-f0-9]{64}/i.test(missingBody.error) &&
      !/token|127\.0\.0\.1|localhost|browser-remote|Bearer|[A-Z]:\\/i.test(
        missingBody.error,
      ),
  );
  report.checks.push("missing_browser_task_400_without_private_capability");

  stage = "page_errors";
  ensure(pageErrors === 0);
  report.checks.push("no_page_errors", "read_only_acceptance");

  stage = "report";
  await mkdir("artifacts", { recursive: true });
  await writeFile(
    "artifacts/queue-session-live.json",
    JSON.stringify(report, null, 2),
  );
  console.log(JSON.stringify(report));
}

try {
  await main();
} catch {
  // Never print Playwright's raw call log: fill() errors can contain a password.
  console.error(
    JSON.stringify({ ok: false, stage, error: "只读公网验收未完成。" }),
  );
  process.exitCode = 1;
} finally {
  await browser?.close().catch(() => {});
}
