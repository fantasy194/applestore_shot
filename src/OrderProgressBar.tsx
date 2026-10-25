import { useLayoutEffect, useRef, useState } from "react";
import { ArrowRight, X } from "lucide-react";
import { useAccountPrivacy } from "./AccountPrivacy";
import { SelectField } from "./SelectField";
import { purchaseEvidence as evidence } from "./purchaseEvidence";
import { formatTaskStage, taskWaitingState } from "./taskStage";
import { active, type Account, type Data, type Task } from "./types";

const steps = [
  ["prepare", "准备"],
  ["verify", "核对"],
  ["checkout", "结算"],
  ["submit", "提交"],
  ["payment", "待付款"],
] as const;

const phaseIds = new Set<string>(steps.map(([phase]) => phase));
function stagePhase(stage: string): string {
  const known: Record<string, string> = {
    search: "verify",
    fulfillment: "verify",
    pickup: "verify",
    review: "checkout",
    billing: "checkout",
    checkout: "checkout",
    submit: "submit",
  };
  if (known[stage.trim()]) return known[stage.trim()];
  if (/认证|登录|验证码|设备信任/.test(stage)) return "prepare";
  if (/提交订单|订单提交/.test(stage)) return "submit";
  if (/自提门店|自提时段|取货|可取|核对商品|预选门店/.test(stage))
    return "verify";
  if (/结算|付款方式|结账/.test(stage)) return "checkout";
  if (/准备|预热/.test(stage)) return "prepare";
  return "unknown";
}

function taskCause(task: Task): string {
  if (typeof task.result?.message === "string" && task.result.message.trim())
    return task.result.message;
  if (
    ["failed", "interrupted"].includes(task.status) &&
    task.stage?.trim() &&
    ![
      "失败",
      "已失败",
      "已中断",
      "已完成",
      "search",
      "review",
      "submit",
      "checkout",
    ].includes(task.stage.trim())
  )
    return task.stage;
  const nestedError = (result: Data | null, depth = 0): string => {
    if (
      !result ||
      depth > 3 ||
      (result.account_id && result.account_id !== task.account_id)
    )
      return "";
    if (typeof result.error === "string" && result.error.trim())
      return result.error;
    if (result.success === false && typeof result.message === "string")
      return result.message;
    const children = [
      result.result,
      ...[result.jobs, result.queue_jobs, result.attempts].flatMap((rows) =>
        Array.isArray(rows) ? rows : [],
      ),
    ];
    for (const child of children.slice(0, 100)) {
      const error = nestedError(child, depth + 1);
      if (error) return error;
    }
    return "";
  };
  return nestedError(task.result);
}

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

export function hasUnknownSubmission(task: Task, tasks: Task[]) {
  const result = evidence(task.result, (number) =>
    resolvedNumber(tasks, task, number),
  );
  return (
    result.unknown ||
    task.status === "submission_unknown" ||
    (!active(task) &&
      task.order_submitted &&
      !result.pending &&
      !resolvedTask(task, tasks))
  );
}

function describe(task: Task, tasks: Task[]) {
  const result = evidence(task.result, (number) =>
    resolvedNumber(tasks, task, number),
  );
  const unknown = hasUnknownSubmission(task, tasks);
  const tracePhase = task.purchase_progress?.current;
  const phase =
    tracePhase && phaseIds.has(tracePhase)
      ? tracePhase
      : stagePhase(task.stage || "");
  const cause = taskCause(task);
  if (unknown)
    return {
      tone: "danger",
      phase: phase === "unknown" ? "submit" : phase,
      title: "提交结果待确认",
      detail: cause || "官网尚未确认订单结果，请先查询订单。",
    };
  if (result.pending)
    return {
      tone: "warning",
      phase: "payment",
      title: "订单待付款",
      detail:
        cause ||
        (active(task)
          ? "已有待付款订单，任务仍在处理。请查看任务详情。"
          : "订单已创建，请查看任务并完成付款。"),
    };
  if (["failed", "interrupted"].includes(task.status))
    return {
      tone: "danger",
      phase: phase === "unknown" ? stagePhase(cause) : phase,
      title: task.status === "failed" ? "下单失败" : "下单已中断",
      detail: cause || "任务未返回异常说明，请打开当前任务查看记录。",
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
  if (taskWaitingState(task) === "prepared")
    return {
      tone: "info",
      phase: "prepare",
      title: "等待购买触发",
      detail: "自提准备已保留，有货或点击购买后继续。",
    };
  if (taskWaitingState(task) === "input")
    return {
      tone: "warning",
      phase,
      title: "下单需要操作",
      detail: "打开当前任务，完成所需输入或确认。",
    };
  if (taskWaitingState(task) === "waiting")
    return {
      tone: "info",
      phase,
      title: "等待任务继续",
      detail: "查看当前任务了解最新状态。",
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
      phase,
      title: formatTaskStage(stage),
      detail: "以官网返回的任务状态为准。",
    };
  // Chinese stages are worker-authored status text. Unknown protocol tokens do
  // not imply a completed phase or a percentage of progress.
  return {
    tone: "info",
    phase,
    title: formatTaskStage(stage),
    detail: cause || "状态随当前任务更新。",
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
        Number(taskWaitingState(b) === "input") -
          Number(taskWaitingState(a) === "input") ||
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
  const completed = new Set(
    (task.purchase_progress?.completed || []).filter(
      (phase) => phaseIds.has(phase) && phase !== "payment",
    ),
  );
  if (state.phase === "payment" && orderNumbers(task.result).length > 0)
    completed.add("submit");
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
          <small title={format(state.detail)}>{format(state.detail)}</small>
        </div>
      </div>
      <ol className="order-progress-steps" aria-label="下单步骤">
        {steps.map(([phase, label]) => (
          <li
            key={phase}
            data-state={
              state.phase === phase
                ? state.tone === "danger"
                  ? "error"
                  : state.tone === "warning"
                    ? "pending"
                    : "current"
                : completed.has(phase)
                  ? "done"
                  : "pending"
            }
            data-current={state.phase === phase || undefined}
            aria-current={state.phase === phase ? "step" : undefined}
          >
            {label}
          </li>
        ))}
      </ol>
      <div className="order-progress-actions">
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
        <button
          type="button"
          aria-label="查看当前任务"
          onClick={() => onOpen(task)}
        >
          查看任务 <ArrowRight size={15} aria-hidden="true" />
        </button>
        {active(task) ? (
          <button
            type="button"
            className="text-button"
            aria-label="取消当前下单"
            onClick={() => onCancel(task)}
          >
            取消
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
