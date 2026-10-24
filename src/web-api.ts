import type { Bridge } from "./api";
import type { Account, Data, Snapshot, Task } from "./types";
import { connectBrowserStream } from "./browser-stream";
import { webHeaders } from "./pwa";

interface WorkspaceChanges {
  cursor: string;
  reset: boolean;
  snapshot?: Snapshot;
  tasks: Task[];
  removed_task_ids: string[];
  accounts?: Account[];
  defaults?: Account;
}

export function localWebBridge(): Bridge {
  async function request(path: string, init: RequestInit = {}) {
    let response: Response;
    try {
      response = await fetch(`/api/${path}`, {
        ...init,
        headers: webHeaders(),
        credentials: "same-origin",
        cache: "no-store",
      });
    } catch (error) {
      if (init.signal?.aborted) throw error;
      throw new Error("本地服务未连接，请检查服务与 Cloudflare 隧道。");
    }
    if (!response.ok) {
      if (response.status === 401)
        window.dispatchEvent(new Event("duo-auth-expired"));
      if (response.status === 404)
        throw new Error(
          import.meta.env.DEV
            ? "本地服务未提供此接口（404），可能仍在运行旧版本。请在原启动终端按 Ctrl+C，再运行 pnpm dev:web，并从新链接打开页面。"
            : "本地服务未提供此接口（404）。请退出旧的本地 Web 服务，启动最新版本，并从新链接打开页面。",
        );
      const body = await response.json().catch(() => ({}));
      throw new Error(body.error || `本地服务请求失败（${response.status}）`);
    }
    return response;
  }
  async function call<T>(
    name: string,
    args: object = {},
    signal?: AbortSignal,
  ): Promise<T> {
    return (
      await request(name, {
        method: "POST",
        body: JSON.stringify(args),
        signal,
      })
    ).json();
  }
  return {
    snapshot: () => call("workspace_snapshot"),
    task: (accountId, taskId, options = {}) =>
      call("task_detail", { accountId, taskId, ...options }),
    add: (email) => call("add_account", { email }),
    remove: (accountId) => call("remove_account", { accountId }),
    savePreset: (accountId, sku, quantity) =>
      call("save_preset", { accountId, sku, quantity }),
    start: (accountId, action, params) =>
      call("start_task", { accountId, action, params }),
    reply: (accountId, taskId, requestId, value) =>
      call("reply_task", { accountId, taskId, requestId, value }),
    cancel: (accountId, taskId) => call("cancel_task", { accountId, taskId }),
    queue: (operation, params = {}) =>
      call("queue_command", { operation, params }),
    logs: (params = {}) => call("activity_logs", { params }),
    browser: (accountId, taskId, operation, params = {}) =>
      call("browser_command", { accountId, taskId, operation, params }),
    browserStream: (accountId, taskId, onStatus, onError) =>
      connectBrowserStream(
        accountId,
        taskId,
        onStatus,
        onError,
        (after, signal) =>
          call<Data>(
            "browser_command",
            { accountId, taskId, operation: "events", params: { after } },
            signal,
          ),
      ),
    maintenance: (operation, params = {}) =>
      call("maintenance_command", { operation, params }),
    subscribe: async (callback, onSnapshot) => {
      // Quick Tunnels do not support SSE. A bounded one-second poll also works
      // after mobile suspension and reconnects without replaying any mutation.
      const controller = new AbortController();
      let cursor: string | undefined;
      let state: Snapshot | undefined;
      let timer: ReturnType<typeof setTimeout> | undefined;
      async function poll() {
        const changes = await call<WorkspaceChanges>(
          "workspace_changes",
          { cursor },
          controller.signal,
        );
        if (controller.signal.aborted) return;
        if (changes.reset) {
          if (!changes.snapshot) throw new Error("同步状态无效，请刷新页面。");
          state = changes.snapshot;
          for (const task of state.tasks) callback(task);
          onSnapshot?.(state);
        } else {
          if (!state) throw new Error("同步状态已过期，请刷新页面。");
          if (
            changes.tasks.length ||
            changes.removed_task_ids.length ||
            changes.accounts ||
            changes.defaults
          ) {
            const tasks = new Map(state.tasks.map((task) => [task.id, task]));
            for (const id of changes.removed_task_ids) tasks.delete(id);
            for (const task of changes.tasks) tasks.set(task.id, task);
            state = {
              ...state,
              accounts: changes.accounts || state.accounts,
              defaults: changes.defaults || state.defaults,
              tasks: [...tasks.values()].sort(
                (a, b) =>
                  b.created_at - a.created_at || a.id.localeCompare(b.id),
              ),
            };
            for (const task of changes.tasks) callback(task);
            onSnapshot?.(state);
          }
        }
        cursor = changes.cursor;
      }
      await poll();
      const next = () => {
        if (controller.signal.aborted) return;
        timer = setTimeout(() => {
          void poll()
            .catch(() => {})
            .finally(next);
        }, 1000);
      };
      next();
      return () => {
        controller.abort();
        clearTimeout(timer);
      };
    },
  };
}
