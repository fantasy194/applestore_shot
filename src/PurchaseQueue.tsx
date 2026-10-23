import { useEffect, useRef, useState } from "react";
import { ExternalLink, Pause, Play, RefreshCw, ScrollText } from "lucide-react";
import type { Bridge } from "./api";
import type { Account, Data, Task } from "./types";
import { useAccountPrivacy } from "./AccountPrivacy";
import { SelectField } from "./SelectField";
import { NumberField } from "./NumberField";
import { presetProducts } from "./products";
import { DisclosureSummary } from "./DisclosureSummary";
import { purchaseEvidence } from "./purchaseEvidence";
import { formatTaskStage } from "./taskStage";
import { QueueEnqueuePolicy } from "./QueueEnqueuePolicy";

const states: Record<string, string> = {
  queued: "等待执行",
  waiting_stock: "等待确认库存",
  dispatched: "准备执行",
  running: "执行中",
  cancelling: "正在取消",
  succeeded: "已完成",
  failed: "失败",
  cancelled: "已取消",
  interrupted: "已中断",
  expired: "已过期",
  blocked: "需要处理",
  submission_unknown: "提交结果未知",
};
const terminal = (status: string) =>
  [
    "succeeded",
    "failed",
    "cancelled",
    "interrupted",
    "expired",
    "submission_unknown",
  ].includes(status);
export const logsHref = (filters: Record<string, string> = {}) =>
  `#/logs${Object.keys(filters).length ? `?${new URLSearchParams(filters)}` : ""}`;

function queueTime(value: unknown) {
  if (typeof value !== "number" || !Number.isFinite(value) || value <= 0)
    return null;
  const date = new Date(value);
  if (!Number.isFinite(date.valueOf())) return null;
  const pad = (number: number, width = 2) =>
    String(number).padStart(width, "0");
  const day = `${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
  const clock = `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}.${pad(date.getMilliseconds(), 3)}`;
  return {
    day,
    clock,
    full: `${date.getFullYear()}-${day} ${clock}`,
    iso: date.toISOString(),
  };
}

function QueueJobTimes({ job }: { job: Data }) {
  const enqueued = queueTime(job.queued_at_ms) || queueTime(job.enqueued_at_ms);
  const events = [
    {
      kind: "enqueued",
      label: "入队",
      value: queueTime(job.queued_at_ms)
        ? job.queued_at_ms
        : job.enqueued_at_ms,
    },
    ...(!["manual", "schedule"].includes(job.source)
      ? [
          {
            kind: "stock-detected",
            label: "发现有货",
            value: job.stock_detected_at_ms,
          },
        ]
      : []),
    { kind: "started", label: "开始下单", value: job.started_at_ms },
    {
      kind: "order-confirmed",
      label: "收到订单号",
      value: job.order_confirmed_at_ms,
    },
  ];
  return (
    <div className="queue-job-times" aria-label="任务时间">
      {events.map(({ kind, label, value }) => {
        const time = queueTime(value);
        if (!time)
          return ["enqueued", "stock-detected"].includes(kind) ? (
            <span className="queue-time-missing" key={kind}>
              {label}时间未记录
            </span>
          ) : null;
        return (
          <span className="queue-job-time" key={kind}>
            <time
              data-kind={kind}
              data-same-day={time.day === enqueued?.day || undefined}
              dateTime={time.iso}
              title={`${label} · ${time.full}（本机记录）`}
              aria-label={`${label} ${time.full}`}
            >
              <span className="queue-time-context">
                <span>{label}</span>{" "}
                <span className="queue-time-date">{time.day}</span>
              </span>{" "}
              <strong>
                {time.clock.slice(0, 8)}
                <span className="queue-time-fraction">
                  {time.clock.slice(8)}
                </span>
              </strong>
            </time>
          </span>
        );
      })}
    </div>
  );
}

export function PurchaseQueue({
  bridge,
  accounts,
  tasks,
  initialAccountId,
  onTask,
  onConfigure,
}: {
  bridge: Bridge;
  accounts: Account[];
  tasks: Task[];
  initialAccountId?: string;
  onTask: (taskId: string, accountId: string) => void;
  onConfigure: (accountId: string) => void;
}) {
  const { format } = useAccountPrivacy();
  const [state, setState] = useState<Data | null>(null);
  const [mode, setMode] = useState("sequential");
  const [maximum, setMaximum] = useState(1);
  const [checkFreshness, setCheckFreshness] = useState(true);
  const [maximumValid, setMaximumValid] = useState(true);
  const [dirty, setDirty] = useState(false);
  const [error, setError] = useState("");
  const [connectionError, setConnectionError] = useState("");
  const [busy, setBusy] = useState(false);
  const [refreshVersion, setRefreshVersion] = useState(0);
  const inFlight = useRef(false);
  const initialized = useRef(false);
  useEffect(() => {
    if (!bridge.queue) return;
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      if (!inFlight.current) {
        inFlight.current = true;
        try {
          const result = await bridge.queue!("status");
          if (!disposed) {
            setState(result);
            setConnectionError("");
            if (!initialized.current) {
              setMode(result.mode || "sequential");
              setMaximum(result.max_concurrency ?? 1);
              setCheckFreshness(result.check_inventory_freshness !== false);
              initialized.current = true;
            }
          }
        } catch (error) {
          if (!disposed)
            setConnectionError(
              error instanceof Error ? error.message : String(error),
            );
        } finally {
          inFlight.current = false;
        }
      }
      if (!disposed) timer = setTimeout(poll, 1000);
    }
    void poll();
    return () => {
      disposed = true;
      clearTimeout(timer);
    };
  }, [bridge, refreshVersion]);
  async function command(operation: string, params: Data = {}) {
    if (!bridge.queue) return false;
    setBusy(true);
    setError("");
    const deadline = Date.now() + 10_000;
    while (inFlight.current && Date.now() < deadline)
      await new Promise((resolve) => setTimeout(resolve, 50));
    if (inFlight.current) {
      setBusy(false);
      setError("队列仍在读取，请稍后重试。");
      return false;
    }
    inFlight.current = true;
    try {
      setState(await bridge.queue(operation, params));
      return true;
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error));
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
    return false;
  }
  const jobs: Data[] = [...(state?.jobs || [])].sort(
    (a, b) =>
      (b.enqueued_at_ms ?? 0) - (a.enqueued_at_ms ?? 0) ||
      (b.sequence ?? 0) - (a.sequence ?? 0),
  );
  const accountName = (id: string) =>
    format(accounts.find((account) => account.id === id)?.name || id);
  const completed = jobs.filter((job) => terminal(job.status)).length;
  return (
    <main className="local-page purchase-queue">
      <header className="local-heading">
        <div>
          <h1>执行队列</h1>
          <p>最新在前 · 执行按入队顺序</p>
        </div>
        <div className="ui-toolbar">
          <button
            onClick={() => setRefreshVersion((value) => value + 1)}
            disabled={busy}
          >
            <RefreshCw size={15} />
            刷新队列
          </button>
          {bridge.queue && (
            <button
              disabled={busy || !state}
              onClick={() => command(state?.paused ? "resume" : "pause")}
            >
              {state?.paused ? <Play size={15} /> : <Pause size={15} />}
              {state?.paused ? "恢复队列" : "暂停队列"}
            </button>
          )}
        </div>
      </header>
      {!bridge.queue ? (
        <p role="status">当前服务暂不支持执行队列，请启动最新版本。</p>
      ) : (
        <>
          <div className="ui-metrics" aria-label="队列概况">
            <div className="ui-metric">
              <span>等候</span>
              <strong>{state?.queued_count ?? 0}</strong>
            </div>
            <div className="ui-metric">
              <span>执行中</span>
              <strong>{state?.running_count ?? 0}</strong>
            </div>
            <div className="ui-metric">
              <span>完成</span>
              <strong>{completed}</strong>
            </div>
          </div>
          <QueueEnqueuePolicy
            bridge={bridge}
            accounts={accounts}
            tasks={tasks}
            initialAccountId={initialAccountId}
            onConfigure={onConfigure}
          />
          <details className="ui-panel ui-disclosure queue-policy">
            <DisclosureSummary>
              执行策略 · {state?.paused ? "队列已暂停" : "队列运行中"} ·{" "}
              {state?.mode === "parallel"
                ? `最多 ${state?.max_concurrency ?? 1} 个任务`
                : "逐个执行"}
            </DisclosureSummary>
            <form
              onSubmit={async (event) => {
                event.preventDefault();
                if (
                  await command("configure", {
                    mode,
                    max_concurrency: maximum,
                    check_inventory_freshness: checkFreshness,
                  })
                )
                  setDirty(false);
              }}
            >
              <div className="schedule-fields">
                <SelectField
                  label="队列执行方式"
                  value={mode}
                  disabled={busy}
                  onChange={(value) => {
                    setMode(value);
                    setDirty(true);
                  }}
                  options={[
                    { value: "sequential", label: "逐个执行" },
                    { value: "parallel", label: "固定并发" },
                  ]}
                />
                <NumberField
                  label="同时执行任务上限"
                  min={1}
                  max={10}
                  required
                  disabled={busy || mode === "sequential"}
                  value={maximum}
                  onValidityChange={setMaximumValid}
                  onChange={(value) => {
                    setMaximum(value);
                    setDirty(true);
                  }}
                />
              </div>
              <label className="checkbox-label">
                <input
                  type="checkbox"
                  checked={checkFreshness}
                  disabled={busy}
                  onChange={(event) => {
                    setCheckFreshness(event.target.checked);
                    setDirty(true);
                  }}
                />
                下单前检查库存样本时效
              </label>
              <p className="muted">
                默认开启：超过 10
                秒则保留位置并等待新确认，后续符合条件的任务继续执行。
                关闭后允许使用最近一次有货确认，官网结账仍重新核对；未知库存、无货及授权限制继续生效。
              </p>
              <p className="muted">
                默认逐个执行，可同时执行 1–10
                个任务。修改策略前，请暂停队列并等待执行中的任务结束。
              </p>
              <p className="muted">
                下单执行名额：
                {state?.reserved_child_slots ??
                  state?.reserved_slots ??
                  0} / {state?.child_capacity ?? 10}
                ；按每个任务的下单路数占用，名额不足时继续排队。
              </p>
              <button
                type="submit"
                disabled={
                  busy ||
                  !dirty ||
                  !maximumValid ||
                  !Number.isInteger(maximum) ||
                  maximum < 1 ||
                  maximum > 10
                }
              >
                保存执行策略
              </button>
            </form>
          </details>
          {(error || connectionError) && (
            <p className="error-text" role="alert">
              {error || connectionError}
            </p>
          )}
          {state?.policy_errors?.map((issue: Data) => (
            <section
              className="ui-panel queue-blocked"
              key={`${issue.account_id}:${issue.sku}:${issue.quantity}`}
            >
              <div className="ui-panel-heading">
                <h2>自提偏好需要处理</h2>
                <span>
                  {accountName(issue.account_id)} · {issue.sku} ·{" "}
                  {issue.quantity} 件
                </span>
              </div>
              <p className="error-text" role="alert">
                {format(issue.message || "请检查预选门店与监控配置")}
              </p>
              <div className="ui-toolbar">
                <a className="ui-action-link" href="#/settings">
                  修改自提偏好
                </a>
                <a
                  className="ui-action-link"
                  href={logsHref({
                    account_id: issue.account_id,
                    category: "queue",
                  })}
                >
                  查看原因日志
                </a>
              </div>
            </section>
          ))}
          {state?.blocked_accounts?.map((blocked: Data) => (
            <section
              className="ui-panel queue-blocked"
              key={blocked.account_id}
            >
              <div className="ui-panel-heading">
                <h2>{accountName(blocked.account_id)}</h2>
                <span className="status blocked">需要处理</span>
              </div>
              <p>
                {blocked.reason === "authentication"
                  ? "需要恢复官网登录"
                  : "订单提交结果未知，请先查询订单确认"}
              </p>
              <div className="ui-toolbar">
                {blocked.reason === "authentication" && (
                  <button
                    disabled={busy}
                    onClick={() =>
                      command("resume", { account_id: blocked.account_id })
                    }
                  >
                    登录恢复后继续此账号
                  </button>
                )}
                <a
                  className="ui-action-link"
                  href={logsHref({ account_id: blocked.account_id })}
                >
                  查看账号日志
                </a>
              </div>
            </section>
          ))}
          <section className="ui-panel">
            <div className="ui-panel-heading">
              <h2>任务列表</h2>
              <span>{jobs.length} 项</span>
            </div>
            {!jobs.length && (
              <p className="muted">
                尚无队列任务。有货时，启用自动抢购的目标会加入队列。
              </p>
            )}
            {jobs.map((job) => {
              const evidence = purchaseEvidence(job.result);
              const pending = evidence.pending;
              const unknown =
                job.status === "submission_unknown" || evidence.unknown;
              const modePaused =
                ["queued", "waiting_stock"].includes(job.status) &&
                /候选门店模式已关闭|已切换候选门店模式|下单方式已变更/.test(
                  job.message || "",
                );
              const hasAccount = accounts.some(
                (account) => account.id === job.account_id && !account.pending,
              );
              const priority = job.pickup_policy?.selection_mode === "priority";
              const modeMessage = !hasAccount
                ? "原账号已移除，任务无法继续。请取消此任务，再使用现有账号配置抢购。"
                : priority
                  ? "当前配置与入队时的「预选多店」不一致。恢复该模式和自动抢购授权后，有货时继续；改用固定单店请取消此任务。"
                  : "当前配置与入队时的「固定单店」不一致。恢复该模式和自动抢购授权后，有货时继续；改用预选多店请取消此任务。";
              const store =
                job.result?.pickup_store ||
                (job.pickup_policy?.selection_mode === "priority"
                  ? "候选门店择一"
                  : job.store_name || job.store_id || "自提门店");
              const product = presetProducts.find(
                (item) => item.sku === String(job.sku).replace(/\/A$/i, ""),
              );
              const tone = unknown
                ? "danger"
                : pending
                  ? "warning"
                  : job.status === "failed"
                    ? "danger"
                    : ["interrupted", "expired", "blocked"].includes(job.status)
                      ? "warning"
                      : ["running", "dispatched", "cancelling"].includes(
                            job.status,
                          )
                        ? "info"
                        : job.status === "succeeded"
                          ? "success"
                          : "neutral";
              return (
                <article
                  className="queue-job"
                  key={job.id}
                  data-job-id={job.id}
                  data-tone={tone}
                  data-mode-paused={modePaused || undefined}
                >
                  <div className="queue-job-heading">
                    <div className="queue-job-product">
                      <strong>{product?.name || job.sku}</strong>
                      <span>
                        {job.sku} · {store}
                      </span>
                    </div>
                    <span
                      className={`status ${unknown ? "submission_unknown" : pending ? "pending_payment" : job.status}`}
                      data-tone={tone}
                    >
                      {unknown
                        ? "提交结果未知"
                        : pending
                          ? "待付款"
                          : modePaused
                            ? "等待恢复配置"
                            : states[job.status] || job.status}
                    </span>
                  </div>
                  <QueueJobTimes job={job} />
                  <div className="queue-job-meta">
                    <span>
                      {accountName(job.account_id)} · 每路 {job.quantity} 件 ·{" "}
                      {job.concurrency ?? 1} 路
                    </span>
                  </div>
                  {(pending || unknown) && (
                    <p
                      className={`queue-critical ${unknown ? "error-text" : ""}`}
                      role="status"
                    >
                      {unknown
                        ? "请先查询订单确认提交结果，避免重复下单。"
                        : "订单待付款，请及时打开关联任务完成支付。"}
                    </p>
                  )}
                  {job.message && (
                    <p className="queue-job-step">
                      {format(
                        modePaused
                          ? modeMessage
                          : ["running", "dispatched"].includes(job.status)
                            ? formatTaskStage(job.message)
                            : job.message,
                      )}
                    </p>
                  )}
                  <div className="queue-job-footer">
                    <details className="ui-disclosure queue-job-details">
                      <DisclosureSummary
                        aria-label={
                          job.status === "failed"
                            ? "失败原因与任务详情"
                            : "任务详情"
                        }
                      >
                        详情
                      </DisclosureSummary>
                      <div className="queue-job-detail-body">
                        {!queueTime(job.started_at_ms) && (
                          <p className="muted">
                            {["queued", "waiting_stock", "dispatched"].includes(
                              job.status,
                            )
                              ? "尚未开始下单。"
                              : "此记录没有开始下单时间；入队时间仅表示进入队列。"}
                          </p>
                        )}
                        {job.pickup_policy?.selection_mode === "priority" && (
                          <p className="muted">
                            预选顺序：
                            {job.pickup_policy.stores
                              ?.map(
                                (item: Data) =>
                                  `${item.store_name} (${item.store_id})`,
                              )
                              .join(" → ")}
                          </p>
                        )}
                        {job.result?.pickup_store && (
                          <p>
                            实际取货门店：{job.result.pickup_store}
                            {job.result.pickup_store_id
                              ? ` (${job.result.pickup_store_id})`
                              : ""}
                          </p>
                        )}
                        {job.result?.pickup_time_slot?.date && (
                          <p>
                            取货时段：{job.result.pickup_time_slot.date}{" "}
                            {job.result.pickup_time_slot.start}–
                            {job.result.pickup_time_slot.end}
                          </p>
                        )}
                        <p className="muted">
                          #{job.sequence} ·{" "}
                          {job.source === "manual"
                            ? "手动"
                            : job.source === "schedule"
                              ? "定时"
                              : "库存监控"}
                          {job.target_id ? ` · 目标 ${job.target_id}` : ""} ·
                          失败重试 {job.retry_count ?? 0} 次
                        </p>
                      </div>
                    </details>
                    <div className="ui-toolbar queue-job-actions">
                      {modePaused && hasAccount && (
                        <button
                          className="queue-aux-action"
                          onClick={() => onConfigure(job.account_id)}
                        >
                          查看自提配置
                        </button>
                      )}
                      {job.task_id && (
                        <button
                          className={
                            pending || unknown
                              ? "queue-task-action"
                              : "queue-aux-action"
                          }
                          aria-label={
                            unknown
                              ? "查看订单任务"
                              : pending
                                ? "前往付款"
                                : "查看关联任务"
                          }
                          title={
                            unknown
                              ? "查看订单任务"
                              : pending
                                ? "前往付款"
                                : "查看关联任务"
                          }
                          onClick={() => onTask(job.task_id, job.account_id)}
                        >
                          {unknown ? (
                            "查看订单任务"
                          ) : pending ? (
                            "前往付款"
                          ) : (
                            <ExternalLink size={16} aria-hidden="true" />
                          )}
                        </button>
                      )}
                      <a
                        className="ui-action-link queue-aux-action"
                        aria-label="查看任务日志"
                        title="查看任务日志"
                        href={logsHref(
                          job.task_id
                            ? {
                                account_id: job.account_id,
                                task_id: job.task_id,
                              }
                            : { account_id: job.account_id, category: "queue" },
                        )}
                      >
                        <ScrollText size={15} aria-hidden="true" />
                        日志
                      </a>
                      {!terminal(job.status) && (
                        <button
                          className="queue-aux-action"
                          aria-label="取消队列任务"
                          disabled={busy || job.status === "cancelling"}
                          onClick={() => command("cancel", { job_id: job.id })}
                        >
                          取消
                        </button>
                      )}
                    </div>
                  </div>
                </article>
              );
            })}
          </section>
        </>
      )}
    </main>
  );
}
