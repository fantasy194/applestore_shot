import { test, expect } from "@playwright/test";

const task = (id: string, stage = "等待", revision = "one") => ({
  id,
  account_id: "account-a",
  action: "inventory_monitor",
  status: "running",
  stage,
  created_at: 10,
  updated_at: 10,
  prompt: null,
  result: null,
  order_submitted: false,
  logs: [],
  is_summary: true,
  detail_revision: revision,
  log_count: 100,
});

test("Web feed merges deltas, removals, account changes and server resets without full snapshots", async ({
  page,
}) => {
  const bodies: unknown[] = [];
  const a = { id: "account-a", email: "a@example.test", name: "A" };
  const b = { id: "account-b", email: "b@example.test", name: "B" };
  const messages = [
    {
      cursor: "generation:1",
      reset: true,
      snapshot: { accounts: [a], tasks: [task("old")], data_dir: "fixture" },
      tasks: [],
      removed_task_ids: [],
    },
    {
      cursor: "generation:2",
      reset: false,
      tasks: [task("new", "新库存", "two")],
      removed_task_ids: ["old"],
      accounts: [a, b],
    },
    {
      cursor: "replacement:1",
      reset: true,
      snapshot: { accounts: [b], tasks: [], data_dir: "fixture" },
      tasks: [],
      removed_task_ids: [],
    },
  ];
  await page.route("**/api/workspace_changes", async (route) => {
    bodies.push(route.request().postDataJSON());
    const message = messages[Math.min(bodies.length - 1, 2)];
    await route.fulfill({ json: message });
  });
  await page.route("**/api/snapshot", () => {
    throw new Error("feed must not read a full snapshot");
  });
  await page.goto("/");
  await page.evaluate(async () => {
    const modulePath = "/src/web-api.ts";
    const { localWebBridge } = await import(/* @vite-ignore */ modulePath);
    const root = window as any;
    root.feedSnapshots = [];
    root.feedTasks = [];
    root.stopFeed = await localWebBridge().subscribe(
      (task: unknown) => root.feedTasks.push(task),
      (snapshot: unknown) => root.feedSnapshots.push(snapshot),
    );
  });
  await expect
    .poll(() => page.evaluate(() => (window as any).feedSnapshots.length))
    .toBeGreaterThanOrEqual(3);
  const state = await page.evaluate(() => {
    const root = window as any;
    root.stopFeed();
    return { snapshots: root.feedSnapshots, tasks: root.feedTasks };
  });
  expect(state.snapshots[0].tasks.map((t: any) => t.id)).toEqual(["old"]);
  expect(state.snapshots[1].tasks.map((t: any) => t.id)).toEqual(["new"]);
  expect(state.snapshots[1].accounts.map((a: any) => a.id)).toEqual([
    "account-a",
    "account-b",
  ]);
  expect(state.snapshots[2].tasks).toEqual([]);
  expect(state.snapshots[2].accounts.map((a: any) => a.id)).toEqual([
    "account-b",
  ]);
  expect(state.tasks.map((t: any) => t.id)).toEqual(["old", "new"]);
  expect(bodies.slice(0, 3)).toEqual([
    {},
    { cursor: "generation:1" },
    { cursor: "generation:2" },
  ]);
});

test("Web feed retries from its last cursor and cancellation prevents delivery or more polls", async ({
  page,
}) => {
  let requests = 0;
  const bodies: unknown[] = [];
  let releasePending: (() => void) | undefined;
  await page.route("**/api/workspace_changes", async (route) => {
    requests += 1;
    bodies.push(route.request().postDataJSON());
    if (requests === 1) {
      await route.fulfill({
        json: {
          cursor: "generation:1",
          reset: true,
          snapshot: {
            accounts: [],
            tasks: [task("first")],
            data_dir: "fixture",
          },
          tasks: [],
          removed_task_ids: [],
        },
      });
    } else if (requests === 2) {
      await route.abort("connectionfailed");
    } else {
      await new Promise<void>((resolve) => {
        releasePending = resolve;
      });
      await route
        .fulfill({
          json: {
            cursor: "generation:2",
            reset: false,
            tasks: [task("must-not-arrive")],
            removed_task_ids: [],
          },
        })
        .catch(() => {});
    }
  });
  await page.goto("/");
  await page.evaluate(async () => {
    const modulePath = "/src/web-api.ts";
    const { localWebBridge } = await import(/* @vite-ignore */ modulePath);
    const root = window as any;
    root.feedTasks = [];
    root.stopFeed = await localWebBridge().subscribe((task: unknown) =>
      root.feedTasks.push(task),
    );
  });
  await expect.poll(() => requests).toBe(3);
  // The next request waits for this in-flight response rather than overlapping it.
  await page.waitForTimeout(1200);
  expect(requests).toBe(3);
  await page.evaluate(() => (window as any).stopFeed());
  releasePending?.();
  await page.waitForTimeout(1200);
  expect(requests).toBe(3);
  expect(bodies).toEqual([
    {},
    { cursor: "generation:1" },
    { cursor: "generation:1" },
  ]);
  expect(
    await page.evaluate(() => (window as any).feedTasks.map((t: any) => t.id)),
  ).toEqual(["first"]);
});

test("Explicit Web task reads preserve account scope and can omit logs", async ({
  page,
}) => {
  const bodies: unknown[] = [];
  await page.route("**/api/task_detail", async (route) => {
    const body = route.request().postDataJSON();
    bodies.push(body);
    await route.fulfill({
      json: {
        ...task(body.taskId),
        is_summary: false,
        logs: [],
        result: { targets: [{ sku: "TEST", store_id: "R001" }] },
        logs_included: false,
      },
    });
  });
  await page.goto("/");
  const result = await page.evaluate(async () => {
    const modulePath = "/src/web-api.ts";
    const { localWebBridge } = await import(/* @vite-ignore */ modulePath);
    return localWebBridge().task("account-a", "monitor-id", {
      includeLogs: false,
    });
  });
  expect(bodies).toEqual([
    { accountId: "account-a", taskId: "monitor-id", includeLogs: false },
  ]);
  expect(result.result.targets[0].sku).toBe("TEST");
  expect(result.logs).toEqual([]);
});
