import type { Task } from "./types";

/** A retained preparation waits for a new purchase, not for a prompt reply. */
export function taskWaitingState(task: Pick<Task, "status" | "prompt">) {
  if (task.status !== "waiting") return null;
  if (task.prompt?.kind === "prepared_purchase") return "prepared";
  return task.prompt &&
    ["password", "code", "text", "select", "confirm"].includes(task.prompt.kind)
    ? "input"
    : "waiting";
}

/** Presentation only: keep protocol stages unchanged in persisted task data. */
export function formatTaskStage(stage: string): string {
  const value = stage.trim();
  const labels: Record<string, string> = {
    search: "查询自提门店",
    fulfillment: "核对配送与自提方式",
    pickup: "核对自提门店与时段",
    review: "核对结算信息",
    billing: "准备付款方式",
    checkout: "正在结算",
    submit: "正在提交订单",
  };
  return (
    labels[value] ||
    (!value || /^[a-z][a-z0-9_]*$/i.test(value) ? "正在处理下单" : stage)
  );
}
