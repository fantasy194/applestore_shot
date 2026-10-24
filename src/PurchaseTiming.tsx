import type { Data } from "./types";
import { DisclosureSummary } from "./DisclosureSummary";

export function PurchaseTiming({ performance }: { performance?: Data }) {
  const durations = performance?.durations_ms;
  if (!durations || typeof durations !== "object") return null;
  const rows = [
    ["handoff_to_submit", "准备接手至提交"],
    ["execution_to_submit", "开始执行至提交"],
    ["execution_to_order_confirmed", "开始执行至订单确认"],
    ["revalidation", "结账复核"],
  ].filter(([key]) => {
    const value = durations[key];
    return typeof value === "number" && Number.isFinite(value) && value >= 0;
  });
  if (!rows.length) return null;
  return (
    <details className="task-logs">
      <DisclosureSummary>本次下单耗时</DisclosureSummary>
      <dl>
        {rows.map(([key, label]) => (
          <div key={key}>
            <dt>{label}</dt>
            <dd>{(durations[key] / 1000).toFixed(2)} 秒</dd>
          </div>
        ))}
      </dl>
      <p className="muted">
        不包含此前的预热等待；提交请求与官网确认分别计时。
      </p>
    </details>
  );
}
