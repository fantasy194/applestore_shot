import { invoke, isTauri } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import type { Account, Action, Data, Snapshot, Task } from "./types";
import { localWebBridge } from "./web-api";
export const isLocalWeb = import.meta.env.MODE === "local-web";
export interface Bridge {
  snapshot(): Promise<Snapshot>;
  task?(
    accountId: string,
    taskId: string,
    options?: { includeLogs?: boolean },
  ): Promise<Task>;
  add(email: string): Promise<Account>;
  remove(accountId: string): Promise<void>;
  savePreset(
    accountId: string,
    sku: string,
    quantity: number,
  ): Promise<Account>;
  start(accountId: string, action: Action, params: Data): Promise<string>;
  reply(
    accountId: string,
    taskId: string,
    requestId: string,
    value: unknown,
  ): Promise<void>;
  cancel(accountId: string, taskId: string): Promise<void>;
  queue?(operation: string, params?: Data): Promise<Data>;
  logs?(params?: Data): Promise<Data>;
  browser?(
    accountId: string,
    taskId: string,
    operation: string,
    params?: Data,
  ): Promise<Data>;
  maintenance?(operation: string, params?: Data): Promise<Data>;
  subscribe(
    callback: (task: Task) => void,
    onSnapshot?: (snapshot: Snapshot) => void,
  ): Promise<() => void>;
}
declare global {
  interface Window {
    __DUO_KILLER_TEST__?: Bridge;
  }
}
const native: Bridge = {
  snapshot: () => invoke("snapshot"),
  task: (accountId, taskId, options = {}) =>
    invoke("task_detail", { accountId, taskId, ...options }),
  add: (email) => invoke("add_account", { email }),
  remove: (accountId) => invoke("remove_account", { accountId }),
  savePreset: (accountId, sku, quantity) =>
    invoke("save_preset", { accountId, sku, quantity }),
  start: (accountId, action, params) =>
    invoke("start_task", { accountId, action, params }),
  reply: (accountId, taskId, requestId, value) =>
    invoke("reply_task", { accountId, taskId, requestId, value }),
  cancel: (accountId, taskId) => invoke("cancel_task", { accountId, taskId }),
  queue: (operation, params = {}) =>
    invoke("queue_command", { operation, params }),
  logs: (params = {}) => invoke("activity_logs", { params }),
  browser: (accountId, taskId, operation, params = {}) =>
    invoke("browser_command", { accountId, taskId, operation, params }),
  maintenance: (operation, params = {}) =>
    invoke("maintenance_command", { operation, params }),
  subscribe: async (callback) =>
    listen<Task>("task-updated", (event) => callback(event.payload)),
};
// Browser previews have no access to account data. Test injection is removed in production builds.
export const api: Bridge | undefined =
  import.meta.env.DEV && window.__DUO_KILLER_TEST__
    ? window.__DUO_KILLER_TEST__
    : isTauri()
      ? native
      : isLocalWeb
        ? localWebBridge()
        : undefined;
