// A bounded maintenance check, or --official-window for a real PWA preview.
// Never supplies credentials, remote input, bag, checkout or payment actions.
import { chromium } from "@playwright/test";
import { readFile, writeFile, mkdir } from "node:fs/promises";
import { pwaDirectory, serverConnection, localCall } from "./pwa-paths.mjs";

let browser;
let ownedTask;
let stage = "initialization";
try {
  const officialWindow = process.argv.includes("--official-window");
  const access = JSON.parse(
    await readFile(".local-runtime/pwa-access.json", "utf8"),
  );
  const connection = await serverConnection(pwaDirectory());
  const before = await localCall(connection, "snapshot");
  const accounts = before.accounts.filter((account) => !account.pending);
  if (accounts.length !== 1)
    throw new Error(
      "Select exactly one existing account for this bounded check",
    );
  const account = accounts[0];
  const active = (task) =>
    ["queued", "running", "waiting", "scheduled"].includes(task.status);
  if (
    before.tasks.some((task) => task.account_id === account.id && active(task))
  )
    throw new Error("Account is busy; no maintenance was started");
  const maintenance = await localCall(connection, "maintenance_command", {
    operation: "status",
    params: {},
  });
  const previous = maintenance.last_runs.find(
    (run) => run.account_id === account.id,
  );
  if (
    !officialWindow &&
    (previous?.requires_login || previous?.cooldown_until_ms > Date.now())
  )
    throw new Error(
      "Account needs official login recovery; no repeated maintenance was started",
    );

  browser = await chromium.launch();
  const context = await browser.newContext({
    viewport: { width: 390, height: 844 },
  });
  const post = (path, body) =>
    context.request.post(`${access.origin}/api/${path}`, {
      data: body,
      headers: { Origin: access.origin },
      timeout: 15_000,
      maxRedirects: 0,
    });
  stage = "pwa_login";
  const login = await post("auth/login", { password: access.password });
  if (login.status() !== 200) throw new Error("PWA login unavailable");
  const page = await context.newPage();
  await page.goto(access.origin, {
    waitUntil: "domcontentloaded",
    timeout: 30_000,
  });
  await page
    .getByRole("link", { name: "执行队列", exact: true })
    .waitFor({ timeout: 15_000 });

  stage = officialWindow ? "official_window_preview" : "read_only_maintenance";
  const started = officialWindow
    ? await localCall(connection, "start_task", {
        accountId: account.id,
        action: "login",
        params: { browser_login: true },
      })
    : await localCall(connection, "maintenance_command", {
        operation: "run",
        params: { account_id: account.id },
      });
  const taskId = officialWindow
    ? started
    : started.last_runs.find((run) => run.account_id === account.id)?.task_id;
  if (!taskId) throw new Error("No maintenance task was created");
  ownedTask = { connection, accountId: account.id, taskId };
  const report = {
    mode: officialWindow ? "official_window" : "maintenance",
    read_only: !officialWindow,
    password_or_otp_supplied: false,
    remote_input_sent: false,
    checkout_attempted: false,
    actual_browser_frames: 0,
    preview_sequence_advanced: false,
    actual_pwa_image_rendered: false,
    timed_out: false,
  };
  let sequence;
  let terminal;
  let previewComplete = false;
  const deadline = Date.now() + (officialWindow ? 25_000 : 55_000);
  while (Date.now() < deadline) {
    const snapshot = await localCall(connection, "snapshot");
    const task = snapshot.tasks.find((item) => item.id === taskId);
    if (!task) throw new Error("Maintenance task disappeared");
    if (!active(task)) {
      terminal = task;
      break;
    }
    const response = await post("browser_command", {
      accountId: account.id,
      taskId,
      operation: "status",
      params: {},
    });
    if (response.status() === 200) {
      const status = await response.json();
      if (status.active && status.frame?.data_url?.startsWith("data:image/")) {
        report.actual_browser_frames++;
        if (sequence && status.frame.sequence > sequence)
          report.preview_sequence_advanced = true;
        sequence = status.frame.sequence;
      }
    } else if (response.status() !== 400)
      throw new Error("Preview API rejected the authenticated task");
    report.actual_pwa_image_rendered ||= await page
      .locator(".remote-browser img")
      .evaluateAll((images) =>
        images.some((image) => image.complete && image.naturalWidth > 0),
      );
    if (
      officialWindow &&
      report.preview_sequence_advanced &&
      report.actual_pwa_image_rendered
    ) {
      previewComplete = true;
      break;
    }
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  if (!terminal) {
    report.timed_out = !previewComplete;
    await localCall(connection, "cancel_task", {
      accountId: account.id,
      taskId,
    });
    for (let index = 0; index < 80; index++) {
      terminal = (await localCall(connection, "snapshot")).tasks.find(
        (task) => task.id === taskId && !active(task),
      );
      if (terminal) break;
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
  }
  const after = await localCall(connection, "snapshot");
  report.account_preserved = after.accounts.some(
    (item) => item.id === account.id,
  );
  report.task_status = terminal?.status || "unfinished";
  report.maintenance_status = officialWindow
    ? null
    : terminal?.result?.status || "unfinished";
  if (officialWindow)
    report.official_account_verified = terminal?.result?.logged_in === true;
  report.order_submitted = terminal?.order_submitted === true;
  const refreshed = await localCall(connection, "maintenance_command", {
    operation: "status",
    params: {},
  });
  report.requires_login =
    refreshed.last_runs.find((run) => run.account_id === account.id)
      ?.requires_login === true;
  const missing = await post("browser_command", {
    accountId: account.id,
    taskId,
    operation: "status",
    params: {},
  });
  report.preview_disabled_after_task = missing.status() === 400;
  await mkdir("artifacts", { recursive: true });
  await writeFile(
    `artifacts/${officialWindow ? "official-window" : "session-maintenance"}-live.json`,
    JSON.stringify(report, null, 2),
  );
  console.log(JSON.stringify(report));
  if (
    !report.account_preserved ||
    report.order_submitted ||
    !terminal ||
    report.timed_out ||
    (officialWindow &&
      (!report.preview_sequence_advanced || !report.actual_pwa_image_rendered))
  )
    process.exitCode = 1;
} catch {
  console.error(
    JSON.stringify({
      ok: false,
      stage,
      error: "只读会话现场检查未完成；未记录私有错误内容。",
    }),
  );
  process.exitCode = 1;
} finally {
  if (ownedTask) {
    try {
      const snapshot = await localCall(ownedTask.connection, "snapshot");
      const own = snapshot.tasks.find((task) => task.id === ownedTask.taskId);
      if (own && ["queued", "running", "waiting"].includes(own.status))
        await localCall(ownedTask.connection, "cancel_task", {
          accountId: ownedTask.accountId,
          taskId: ownedTask.taskId,
        });
    } catch {
      // The server may already be exiting; never target another task or profile.
    }
  }
  await browser?.close().catch(() => {});
}
