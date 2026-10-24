import { useEffect, useState } from "react";
import { Clock3 } from "lucide-react";
import { SelectField } from "./SelectField";
import { presetProducts } from "./products";
import type { Action, Data, Task } from "./types";

export function beijingInput(timestamp: number) {
  return new Date(timestamp + 8 * 3600_000).toISOString().slice(0, 19);
}
function parseBeijing(value: string) {
  const ms = Date.parse(value + "+08:00");
  const complete = value.length === 16 ? value + ":00" : value;
  return Number.isFinite(ms) && beijingInput(ms) === complete ? ms : NaN;
}
const displayTime = (ms: number) =>
  new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(ms);

export function SchedulePurchase({
  sku,
  quantity,
  retryCount = 0,
  busy,
  task,
  start,
  cancel,
}: {
  sku: string;
  quantity: number;
  concurrency?: number;
  retryCount?: number;
  busy: boolean;
  task?: Task;
  start: (action: Action, params: Data) => Promise<void>;
  cancel: (task: Task) => void;
}) {
  const [target, setTarget] = useState(() =>
    beijingInput(Date.now() + 300_000),
  );
  const [fulfillment, setFulfillment] = useState("HOME");
  const [now, setNow] = useState(Date.now());
  const [error, setError] = useState("");
  useEffect(() => {
    if (!task) return;
    const timer = setInterval(() => setNow(Date.now()), 250);
    return () => clearInterval(timer);
  }, [task?.id]);
  const selected = task?.schedule;
  const product = presetProducts.find(
    (p) => p.sku === (selected?.sku || sku).replace(/\/A$/i, ""),
  );
  const remaining = selected
    ? Math.max(0, Math.ceil((selected.at_ms - now) / 1000))
    : 0;
  return (
    <section className="section">
      <div className="section-title">
        <h2>定时抢购</h2>
        <span>北京时间 · 单次触发</span>
      </div>
      <p>
        {product?.name || selected?.sku || sku} ·{" "}
        {selected?.quantity ?? quantity} 件
        {selected &&
          ` · ${selected.fulfillment === "RETAIL" ? "指定门店自提" : "送货"}`}
        {(selected?.fulfillment === "RETAIL" ||
          (!selected && fulfillment === "RETAIL")) &&
          " · 同账号顺序执行，每次1路，按上述数量购买"}
        {` · 失败额外重试 ${selected ? (selected.retry_count ?? 0) : retryCount} 次，最多共 ${(selected ? (selected.retry_count ?? 0) : retryCount) + 1} 轮`}
      </p>
      {selected && task ? (
        <div>
          <p>计划时间：{displayTime(selected.at_ms)}（北京时间）</p>
          <p role="status">
            {remaining > 0
              ? `距离开始 ${Math.floor(remaining / 3600)} 小时 ${Math.floor(remaining / 60) % 60} 分 ${remaining % 60} 秒`
              : "到达计划时间，等待本地服务确认触发结果…"}
          </p>
          <button className="danger-button" onClick={() => cancel(task)}>
            取消定时抢购
          </button>
          <p className="muted">
            此任务的商品、数量、收货方式和失败重试次数已固定；修改上方购买选项不会改变本次定时。
          </p>
        </div>
      ) : (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            if (
              !Number.isInteger(retryCount) ||
              retryCount < 0 ||
              retryCount > 10
            ) {
              setError("下单失败重试次数必须为 0–10 的整数。");
              return;
            }
            const at = parseBeijing(target);
            if (!Number.isFinite(at) || at <= Date.now() + 1000) {
              setError("请设置至少一秒后的有效北京时间。");
              return;
            }
            setError("");
            start("purchase", {
              sku,
              quantity,
              fulfillment,
              scheduled_at: at,
              retry_count: retryCount,
              ...(fulfillment === "RETAIL" ? { concurrency: 1 } : {}),
            });
          }}
        >
          <div className="schedule-fields">
            <label>
              开始时间（北京时间）
              <input
                type="datetime-local"
                step="1"
                required
                value={target}
                disabled={busy}
                onChange={(e) => setTarget(e.target.value)}
              />
            </label>
            <SelectField
              label="定时收货方式"
              value={fulfillment}
              disabled={busy}
              onChange={setFulfillment}
              options={[
                { value: "HOME", label: "送货（本地地址）" },
                { value: "RETAIL", label: "自提（本地预设门店）" },
              ]}
            />
          </div>
          {error && (
            <p className="error-text" role="alert">
              {error}
            </p>
          )}
          <button
            className="primary"
            type="submit"
            disabled={
              busy ||
              !product ||
              !Number.isInteger(quantity) ||
              quantity < 1 ||
              !Number.isInteger(retryCount) ||
              retryCount < 0 ||
              retryCount > 10
            }
          >
            <Clock3 size={16} />
            设定定时抢购
          </button>
        </form>
      )}
      <p className="muted">
        到点执行与“立即下单”相同的流程，成功后创建真实待付款订单并展示付款码。请提前保存地址或自提预设、预热登录，并保持启动程序运行、电脑唤醒。关网页不影响等待；服务退出或重启会停止定时。到点账号忙碌或迟到超过
        3
        秒时停止，不补发。确认未提交的失败才按设定次数串行重试，已有订单或提交未知时停止。
      </p>
    </section>
  );
}
