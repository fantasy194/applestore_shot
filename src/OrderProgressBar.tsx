import { useLayoutEffect, useRef, useState } from "react";
import { ArrowRight, X } from "lucide-react";
import { useAccountPrivacy } from "./AccountPrivacy";
import { SelectField } from "./SelectField";
import { purchaseEvidence as evidence } from "./purchaseEvidence";
import { formatTaskStage } from "./taskStage";
import { active, type Account, type Data, type Task } from "./types";

const steps = [
  ["prepare", "准备"],
  ["verify", "核对"],
  ["checkout", "结算"],
  ["submit", "提交"],
  ["payment", "待付款"],
] as const;

const resolvedStatuses = new Set([
  "order_paid",
  "paid",
  "order_cancelled",
  "order_expired",
  "cancelled",
  "expired",
  "completed",
]);
function resolvedNumber(tasks: Task[], owner: Task, number: unknown) {
  if (typeof number !== "string" || !number) return false;
  return tasks.some((task) => {
    if (
      task.account_id !== owner.account_id ||
      task.created_at < owner.created_at ||
      !task.result ||
      task.result.success === false
    )
      return false;
    if (
      task.result.order_number === number &&
      resolvedStatuses.has(task.result.status)
    )
      return true;
    if (task.action !== "orders") return false;
    const order = task.result.orders?.find(
      (row: Data) =>
        row.order_number === number && row.verified_by === "official_detail",
    );
    return (
      !!order?.items?.length &&
      order.items.every((item: Data) =>
        /^(?:PAYMENT_EXPIRED_STORED_ORDER|CANCEL(?:ED|LED)(?:_[A-Z_]+)?|PAID|PROCESSING|PREPARING_TO_SHIP|SHIPPED|DELIVERED|READY_FOR_PICKUP|PICKED_UP|PICKUP_COMPLETE|COMPLETE|PAYMENT_COMPLETE)$/.test(
          item.status || "",
        ),
      )
    );
  });
}

function orderNumbers(result: Data | null, depth = 0): string[] {
  if (!result || depth > 3) return [];
  return [
    typeof result.order_number === "string" ? result.order_number : "",
    ...(Array.isArray(result.purchase_attention?.pending_order_numbers)
      ? result.purchase_attention.pending_order_numbers
      : []),
    ...(Array.isArray(result.purchase_attention?.unknown_order_numbers)
      ? result.purchase_attention.unknown_order_numbers
      : []),
    ...[
      ...(Array.isArray(result.pending_orders) ? result.pending_orders : []),
      ...(Array.isArray(result.queue_jobs) ? result.queue_jobs : []),
      ...(Array.isArray(result.attempts) ? result.attempts : []),
      ...(Array.isArray(result.jobs) ? result.jobs : []),
      ...(result.result ? [result.result] : []),
    ]
      .slice(0, 100)
      .flatMap((row) => orderNumbers(row, depth + 1)),
  ].filter(Boolean);
}

function resolvedTask(task: Task, tasks: Task[]) {
  if (
    evidence(task.result, (number) => resolvedNumber(tasks, task, number))
      .unknown
  )
    return false;
  if (
    task.result?.success !== false &&
    resolvedStatuses.has(task.result?.status)
  )
    return true;
  const numbers = orderNumbers(task.result);
  return (
    numbers.length > 0 &&
    numbers.every((number) => resolvedNumber(tasks, task, number))
  );
}

function describe(task: Task, tasks: Task[]) {
  const result = evidence(task.result, (number) =>
    resolvedNumber(tasks, task, number),
  );
  const unknown =
    result.unknown ||
    task.status === "submission_unknown" ||
    (!active(task) &&
      task.order_submitted &&
      !result.pending &&
      !resolvedTask(task, tasks));
  if (unknown)
    return {
      tone: "danger",
      phase: "unknown",
      title: "提交结果待确认",
      detail: "请先核对官网订单，再决定后续操作。",
    };
  if (result.pending)
    return {
      tone: "warning",
      phase: "payment",
      title: "订单待付款",
      detail: active(task)
        ? "已有待付款订单，任务仍在处理。请查看任务详情。"
        : "订单已创建，请查看任务并完成付款。",
    };
  if (["failed", "interrupted"].includes(task.status))
    return {
      tone: "danger",
      phase: "unknown",
      title: task.status === "failed" ? "下单失败" : "下单已中断",
      detail: "查看当前任务，核对原因与订单状态。",
    };
  if (task.status === "scheduled")
    return {
      tone: "neutral",
      phase: "prepare",
      title: "等待定时下单",
      detail: task.schedule
        ? `计划时间 ${new Date(task.schedule.at_ms).toLocaleString("zh-CN")}`
        : "尚未开始下单。",
    };
  if (task.status === "queued")
    return {
      tone: "neutral",
      phase: "prepare",
      title: "下单排队中",
      detail: "等待执行名额。",
    };
  if (task.status === "waiting")
    return {
      tone: "warning",
      phase: "unknown",
      title: "下单需要操作",
      detail: "打开当前任务，完成所需输入或确认。",
    };
  const stage = task.stage?.trim() || "";
  const known: Record<string, [string, string]> = {
    search: ["verify", "查询自提门店"],
    fulfillment: ["verify", "核对配送与自提方式"],
    pickup: ["verify", "核对自提门店与时段"],
    review: ["checkout", "核对结算信息"],
    billing: ["checkout", "准备付款方式"],
    checkout: ["checkout", "正在结算"],
    submit: ["submit", "正在提交订单"],
  };
  const mapped = known[stage];
  if (mapped)
    return {
      tone: "info",
      phase: mapped[0],
      title: formatTaskStage(stage),
      detail: "以官网返回的任务状态为准。",
    };
  // Chinese stages are worker-authored status text. Unknown protocol tokens do
  // not imply a completed phase or a percentage of progress.
  const phase = /正在提交订单/.test(stage)
    ? "submit"
    : /自提门店|自提时段|核对商品/.test(stage)
      ? "verify"
      : /结算|付款方式/.test(stage)
        ? "checkout"
        : "unknown";
  return {
    tone: "info",
    phase,
    title: formatTaskStage(stage),
    detail: "状态随当前任务更新。",
  };
}

export function OrderProgressBar({
  tasks,
  accounts,
  onOpen,
  onCancel,
}: {
  tasks: Task[];
  accounts: Account[];
  onOpen: (task: Task) => void;
  onCancel: (task: Task) => void;
}) {
  const { format } = useAccountPrivacy();
  const [selected, setSelected] = useState("");
  const [dismissed, setDismissed] = useState<string[]>([]);
  const section = useRef<HTMLElement>(null);
  const orders = tasks
    .filter((task) => ["purchase", "checkout"].includes(task.action))
    .sort((a, b) => b.updated_at - a.updated_at || b.created_at - a.created_at);
  const live = orders
    .filter(active)
    .sort(
      (a, b) =>
        Number(b.status === "waiting") - Number(a.status === "waiting") ||
        b.updated_at - a.updated_at,
    );
  const latest = orders[0];
  const terminal =
    latest &&
    !active(latest) &&
    !resolvedTask(latest, tasks) &&
    (evidence(latest.result, (number) => resolvedNumber(tasks, latest, number))
      .unknown ||
      evidence(latest.result, (number) => resolvedNumber(tasks, latest, number))
        .pending ||
      ["failed", "interrupted", "submission_unknown"].includes(latest.status) ||
      latest.order_submitted)
      ? latest
      : null;
  const key = (task: Task) => `${task.id}:${task.updated_at}:${task.status}`;
  const candidates = live.length
    ? live
    : terminal && !dismissed.includes(key(terminal))
      ? [terminal]
      : [];
  const task =
    candidates.find((candidate) => candidate.id === selected) || candidates[0];
  useLayoutEffect(() => {
    const element = section.current;
    const parent = element?.parentElement;
    if (!element || !parent) return;
    const measure = () =>
      parent.style.setProperty(
        "--order-progress-height",
        `${element.getBoundingClientRect().height}px`,
      );
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => {
      observer.disconnect();
      parent.style.removeProperty("--order-progress-height");
    };
  }, [task?.id, task?.updated_at, candidates.length]);
  if (!task) return null;
  const state = describe(task, tasks);
  const accountName = (current: Task) => {
    const account = accounts.find((item) => item.id === current.account_id);
    return format(account?.name || account?.email || "下单账号");
  };
  const actionName = (current: Task) =>
    current.queue_job_ids?.length ||
    current.result?.status?.startsWith("queue_")
      ? "队列下单"
      : current.action === "checkout"
        ? "购物袋结算"
        : "购买商品";
  return (
    <section
      ref={section}
      className="order-progress"
      data-tone={state.tone}
      data-phase={state.phase}
      aria-label="下单进度"
    >
      <div className="order-progress-main">
        <div className="order-progress-context">
          <span>{accountName(task)}</span>
          <span>{actionName(task)}</span>
          {candidates.length > 1 && <span>{candidates.length} 个下单任务</span>}
        </div>
        <div className="order-progress-status" role="status" aria-live="polite">
          <strong>{format(state.title)}</strong>
          <small>{state.detail}</small>
        </div>
      </div>
      {candidates.length > 1 && (
        <SelectField
          className="order-progress-picker"
          label="查看下单任务"
          value={task.id}
          onChange={setSelected}
          options={candidates.map((candidate) => ({
            value: candidate.id,
            label: `${accountName(candidate)} · ${actionName(candidate)} · ${describe(candidate, tasks).title}`,
          }))}
        />
      )}
      <ol className="order-progress-steps" aria-label="下单步骤">
        {steps.map(([phase, label]) => (
          <li
            key={phase}
            data-current={state.phase === phase || undefined}
            aria-current={state.phase === phase ? "step" : undefined}
          >
            {label}
          </li>
        ))}
      </ol>
      <div className="order-progress-actions">
        <button type="button" onClick={() => onOpen(task)}>
          查看当前任务 <ArrowRight size={15} aria-hidden="true" />
        </button>
        {active(task) ? (
          <button
            type="button"
            className="text-button"
            onClick={() => onCancel(task)}
          >
            取消当前下单
          </button>
        ) : (
          <button
            type="button"
            className="icon-button"
            aria-label="隐藏下单提示"
            onClick={() => setDismissed((keys) => [...keys, key(task)])}
          >
            <X size={16} aria-hidden="true" />
          </button>
        )}
      </div>
    </section>
  );
}
