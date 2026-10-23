import type { Bridge } from "./api";
import { LOCAL_ID, type Action, type Data } from "./types";

// Anonymous discovery/configuration tasks finish independently of account checkout.
export async function inventoryCall(
  bridge: Bridge,
  action: Action,
  params: Data = {},
) {
  return scopedTaskCall(bridge, LOCAL_ID, action, params);
}

export async function scopedTaskCall(
  bridge: Bridge,
  accountId: string,
  action: Action,
  params: Data = {},
) {
  const id = await bridge.start(accountId, action, params);
  const deadline = Date.now() + 45_000;
  while (Date.now() < deadline) {
    const task = bridge.task
      ? await bridge.task(accountId, id, { includeLogs: false })
      : (await bridge.snapshot()).tasks.find(
          (row) => row.id === id && row.account_id === accountId,
        );
    if (task?.status === "succeeded") {
      if (task.result?.success === false)
        throw new Error(task.result.message || task.stage || "操作未成功。");
      return task.result || {};
    }
    if (task && ["failed", "cancelled", "interrupted"].includes(task.status)) {
      throw new Error(task.stage || "查询未完成，请重试。");
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error("查询尚未完成，可在进行中的任务中查看或取消。");
}

export interface InventoryStore {
  store_id: string;
  store_name: string;
  location?: string;
  status?: string;
  pickup_message?: string;
}

export interface InventoryTarget extends InventoryStore {
  id: string;
  sku: string;
  quantity: number;
  account_id: string;
  auto_purchase: boolean;
  concurrency: number;
  retry_count: number;
}

export const inventoryError = (error: unknown) =>
  error instanceof Error ? error.message : String(error);
