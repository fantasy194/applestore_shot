import { useEffect, useRef, useState } from "react";
import { Pause, Play, RefreshCw } from "lucide-react";
import type { Bridge } from "./api";
import type { Account, Data } from "./types";
import { useAccountPrivacy } from "./AccountPrivacy";

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

export function PurchaseQueue({
  bridge,
  accounts,
  onTask,
}: {
  bridge: Bridge;
  accounts: Account[];
  onTask: (taskId: string, accountId: string) => void;
}) {
  const { format } = useAccountPrivacy();
  const [state, setState] = useState<Data | null>(null);
  const [mode, setMode] = useState("sequential");
  const [maximum, setMaximum] = useState(1);
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
  const timestamp = (value: number) =>
    new Date(value).toLocaleString("zh-CN", {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
      fractionalSecondDigits: 3,
    });
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
          <details className="ui-panel ui-disclosure queue-policy">
            <summary>
              执行策略 · {state?.paused ? "队列已暂停" : "队列运行中"} ·{" "}
              {state?.mode === "parallel"
                ? `最多 ${state?.max_concurrency ?? 1} 个任务`
                : "逐个执行"}
            </summary>
            <form
              onSubmit={async (event) => {
                event.preventDefault();
                if (
                  await command("configure", { mode, max_concurrency: maximum })
                )
                  setDirty(false);
              }}
            >
              <div className="schedule-fields">
                <label>
                  队列执行方式
                  <select
                    value={mode}
                    disabled={busy}
                    onChange={(event) => {
                      setMode(event.target.value);
                      setDirty(true);
                    }}
                  >
                    <option value="sequential">逐个执行</option>
                    <option value="parallel">固定并发</option>
                  </select>
                </label>
                <label>
                  同时执行任务上限
                  <input
                    type="number"
                    min={1}
                    max={10}
                    required
                    step={1}
                    disabled={busy || mode === "sequential"}
                    value={maximum}
                    onChange={(event) => {
                      setMaximum(Number(event.target.value));
                      setDirty(true);
                    }}
                  />
                </label>
              </div>
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
              const pending =
                job.result?.status === "pending_payment" ||
                job.result?.pending_orders?.length > 0;
              const unknown =
                job.status === "submission_unknown" ||
                job.result?.status === "submission_unknown";
              const store =
                job.result?.pickup_store ||
                (job.pickup_policy?.selection_mode === "priority"
                  ? "候选门店择一"
                  : job.store_name || job.store_id || "自提门店");
              return (
                <article
                  className="queue-job"
                  key={job.id}
                  data-job-id={job.id}
                >
                  <div className="queue-job-heading">
                    <strong>
                      {job.sku} · {store}
                    </strong>
                    <span
                      className={`status ${unknown ? "submission_unknown" : job.status}`}
                    >
                      {unknown
                        ? "提交结果未知"
                        : pending
                          ? "待付款"
                          : states[job.status] || job.status}
                    </span>
                  </div>
                  <div className="queue-job-meta">
                    <span>
                      {accountName(job.account_id)} · 每路 {job.quantity} 件 ·{" "}
                      {job.concurrency ?? 1} 路
                    </span>
                    <time dateTime={new Date(job.enqueued_at_ms).toISOString()}>
                      入队 {timestamp(job.enqueued_at_ms)}
                    </time>
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
                  {!terminal(job.status) && job.message && (
                    <p className="muted">{format(job.message)}</p>
                  )}
                  <details className="ui-disclosure queue-job-details">
                    <summary>
                      {job.status === "failed"
                        ? "失败原因与任务详情"
                        : "任务详情"}
                    </summary>
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
                    {terminal(job.status) && job.message && (
                      <p
                        className={
                          job.status === "failed" || unknown
                            ? "error-text"
                            : "muted"
                        }
                      >
                        {format(job.message)}
                      </p>
                    )}
                  </details>
                  <div className="ui-toolbar">
                    {job.task_id && (
                      <button
                        onClick={() => onTask(job.task_id, job.account_id)}
                      >
                        {pending
                          ? "前往付款"
                          : unknown
                            ? "查看订单任务"
                            : "查看关联任务"}
                      </button>
                    )}
                    <a
                      className="ui-action-link"
                      href={logsHref(
                        job.task_id
                          ? { account_id: job.account_id, task_id: job.task_id }
                          : { account_id: job.account_id, category: "queue" },
                      )}
                    >
                      查看任务日志
                    </a>
                    {!terminal(job.status) && (
                      <button
                        disabled={busy || job.status === "cancelling"}
                        onClick={() => command("cancel", { job_id: job.id })}
                      >
                        取消队列任务
                      </button>
                    )}
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
