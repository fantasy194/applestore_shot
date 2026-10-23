import { DisclosureSummary } from "./DisclosureSummary";
import { useEffect, useRef, useState } from "react";
import {
  Activity,
  LoaderCircle,
  Plus,
  RefreshCw,
  ListOrdered,
  ScrollText,
  Square,
  Trash2,
} from "lucide-react";
import type { Bridge } from "./api";
import { active, LOCAL_ID, type Data, type Snapshot } from "./types";
import { presetProducts } from "./products";
import { useAccountPrivacy } from "./AccountPrivacy";
import { StoreSearch } from "./StoreSearch";
import { SelectField } from "./SelectField";
import { NumberField } from "./NumberField";
import { randomUUID } from "./uuid";
import {
  inventoryCall,
  inventoryError,
  type InventoryStore,
  type InventoryTarget,
} from "./inventory-api";

const initialConfig = () => ({
  interval_seconds: 1,
  targets: [] as InventoryTarget[],
  proxy: { enabled: false, pool_size: 10, providers: [] as Data[] },
});
const timeMs = (value: unknown) =>
  typeof value === "number"
    ? value < 1e12
      ? value * 1000
      : value
    : Date.parse(String(value));
const dateText = (value: unknown) => {
  if (!value) return "尚未检查";
  const date = new Date(timeMs(value));
  return Number.isNaN(date.valueOf())
    ? "检查时间未知"
    : date.toLocaleString("zh-CN");
};
const providerMessages: Record<string, string> = {
  provider_expiry: "代理供应商返回的有效期无法识别",
  provider_network: "暂时无法连接代理供应商",
  provider_timeout: "代理供应商响应超时",
  provider_http_auth: "代理供应商认证失败，请检查配置或白名单",
  provider_http: "代理供应商请求失败",
  provider_whitelist: "代理供应商白名单设置未完成",
  provider_configuration: "代理供应商配置无效",
  provider_response: "代理供应商返回的数据无法识别",
  provider_response_size: "代理供应商返回的数据过大",
  provider_endpoint: "代理供应商返回的连接地址无效",
  provider_duplicate: "代理供应商返回了重复连接",
};
function stockMessage(error: unknown, proxy?: Data): string {
  if (error === "proxy_pool_empty") {
    const cause =
      typeof proxy?.last_error === "string"
        ? providerMessages[proxy.last_error]
        : undefined;
    return cause
      ? `暂无可用库存代理：${cause}。`
      : "正在补充库存代理，暂未取得有效库存。";
  }
  if (typeof error !== "string") return "";
  if (error.startsWith("provider_"))
    return providerMessages[error] || "代理供应商暂未提供可用连接。";
  return error;
}

export function InventoryMonitor({
  snapshot,
  bridge,
  refresh,
  compact = false,
}: {
  snapshot: Snapshot;
  bridge: Bridge;
  refresh: () => Promise<void>;
  compact?: boolean;
}) {
  const { format } = useAccountPrivacy();
  const [config, setConfig] = useState(initialConfig);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [sku, setSku] = useState("MLH74FE");
  const [quantity, setQuantity] = useState(1);
  const [location, setLocation] = useState("");
  const [now, setNow] = useState(Date.now());
  const [stockEvents, setStockEvents] = useState<Data[]>([]);
  const [historyExpanded, setHistoryExpanded] = useState(false);
  const [historyError, setHistoryError] = useState(false);
  const [invalidNumbers, setInvalidNumbers] = useState<string[]>([]);
  function numberValidity(key: string, valid: boolean) {
    setInvalidNumbers((keys) =>
      valid
        ? keys.includes(key)
          ? keys.filter((item) => item !== key)
          : keys
        : keys.includes(key)
          ? keys
          : [...keys, key],
    );
  }
  const invalidConfig = invalidNumbers.some(
    (key) =>
      key === "pool" ||
      config.targets.some((target) => key.startsWith(`${target.id}:`)) ||
      config.proxy.providers.some(
        (provider) => key === `provider:${provider.id}`,
      ),
  );
  const [monitorDetail, setMonitorDetail] = useState<{
    id: string;
    result: Data | null;
  } | null>(null);
  const [stoppedMonitorId, setStoppedMonitorId] = useState("");
  const monitorTasks = snapshot.tasks
    .filter((task) => task.action === "inventory_monitor")
    .sort((a, b) => b.created_at - a.created_at);
  const monitor = monitorTasks.find(active);
  const stoppableMonitor =
    monitor ||
    (["failed", "interrupted"].includes(monitorTasks[0]?.status) &&
    monitorTasks[0]?.id !== stoppedMonitorId
      ? monitorTasks[0]
      : undefined);
  const latestCheck = snapshot.tasks
    .filter((task) =>
      ["inventory_monitor", "inventory_check"].includes(task.action),
    )
    .sort((a, b) => b.updated_at - a.updated_at)[0];
  const selectedTask = monitor || latestCheck;
  const selectedRef = useRef(selectedTask);
  selectedRef.current = selectedTask;
  const result =
    monitorDetail?.id === selectedTask?.id
      ? monitorDetail?.result
      : selectedTask?.result;
  const running = !!monitor;
  useEffect(() => {
    if (!bridge.task) return;
    let disposed = false;
    let inFlight = false;
    let revision = "";
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      if (disposed || inFlight) return;
      const task = selectedRef.current;
      const nextRevision = task
        ? `${task.id}:${task.detail_revision ?? task.updated_at}`
        : "";
      if (
        document.visibilityState !== "hidden" &&
        task &&
        task.is_summary &&
        nextRevision !== revision
      ) {
        inFlight = true;
        try {
          const detail = await bridge.task!(task.account_id, task.id, {
            includeLogs: false,
          });
          if (!disposed && selectedRef.current?.id === task.id) {
            setMonitorDetail({ id: task.id, result: detail.result });
            revision = nextRevision;
          }
        } catch {
          // Keep the last sample; its checked_at still expires to unknown.
        } finally {
          inFlight = false;
        }
      }
      if (!disposed) timer = setTimeout(poll, 500);
    }
    const visible = () => {
      if (document.visibilityState !== "hidden") {
        clearTimeout(timer);
        revision = "";
        void poll();
      }
    };
    document.addEventListener("visibilitychange", visible);
    void poll();
    return () => {
      disposed = true;
      clearTimeout(timer);
      document.removeEventListener("visibilitychange", visible);
    };
  }, [bridge]);
  useEffect(() => {
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    let inFlight = false;
    async function readHistory() {
      if (disposed || inFlight) return;
      if (document.visibilityState === "hidden") {
        timer = setTimeout(readHistory, 5000);
        return;
      }
      inFlight = true;
      try {
        const page = bridge.logs
          ? await bridge.logs({ category: "stock", limit: 12 })
          : { entries: [] };
        if (disposed) return;
        setStockEvents(
          (Array.isArray(page.entries) ? page.entries : [])
            .filter(
              (entry: Data) =>
                entry.event === "inventory_available" &&
                typeof entry.details?.sku === "string" &&
                typeof entry.details?.store_id === "string" &&
                typeof entry.details?.store_name === "string" &&
                Number.isFinite(
                  new Date(Number(entry.details?.checked_at_ms)).getTime(),
                ) &&
                Number(entry.details.checked_at_ms) > 0,
            )
            .sort(
              (a: Data, b: Data) =>
                Number(b.details.checked_at_ms) -
                Number(a.details.checked_at_ms),
            )
            .slice(0, 12),
        );
        setHistoryError(false);
      } catch {
        if (!disposed) setHistoryError(true);
      } finally {
        inFlight = false;
        if (!disposed) timer = setTimeout(readHistory, 5000);
      }
    }
    const visible = () => {
      if (document.visibilityState !== "hidden") {
        clearTimeout(timer);
        void readHistory();
      }
    };
    document.addEventListener("visibilitychange", visible);
    void readHistory();
    return () => {
      disposed = true;
      clearTimeout(timer);
      document.removeEventListener("visibilitychange", visible);
    };
  }, [bridge]);
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);
  useEffect(() => {
    let disposed = false;
    inventoryCall(bridge, "inventory_settings_read")
      .then((result) => {
        if (!disposed) {
          const saved = result.config || initialConfig();
          setConfig({
            ...initialConfig(),
            ...saved,
            targets: (saved.targets || []).map((target: InventoryTarget) => ({
              ...target,
              concurrency: target.concurrency ?? 1,
              retry_count: target.retry_count ?? 0,
            })),
            proxy: { ...initialConfig().proxy, ...saved.proxy },
          });
        }
      })
      .catch((error) => {
        if (!disposed) setError(inventoryError(error));
      })
      .finally(() => {
        if (!disposed) setLoading(false);
      });
    return () => {
      disposed = true;
    };
  }, [bridge]);
  function change(next: typeof config) {
    setConfig(next);
    setDirty(true);
    setNotice("");
  }
  function updateTarget(id: string, patch: Partial<InventoryTarget>) {
    change({
      ...config,
      targets: config.targets.map((target) =>
        target.id === id ? { ...target, ...patch } : target,
      ),
    });
  }
  function addStores(stores: InventoryStore[]) {
    if (busy || running) return;
    const targets = [...config.targets];
    for (const store of stores) {
      if (
        targets.some(
          (target) => target.sku === sku && target.store_id === store.store_id,
        )
      )
        continue;
      targets.push({
        id: randomUUID(),
        sku: sku.toUpperCase().replace(/\/A$/, ""),
        quantity,
        store_id: store.store_id,
        store_name: store.store_name,
        location,
        account_id: "",
        auto_purchase: false,
        concurrency: 1,
        retry_count: 0,
      });
    }
    change({ ...config, targets });
  }
  async function save() {
    if (invalidConfig) throw new Error("请先填写有效的数量和范围。");
    if (
      config.targets.some(
        (target) =>
          !Number.isInteger(target.retry_count ?? 0) ||
          (target.retry_count ?? 0) < 0 ||
          (target.retry_count ?? 0) > 10,
      )
    )
      throw new Error("下单失败重试次数必须为 0–10 的整数。");
    const result = await inventoryCall(bridge, "inventory_settings_save", {
      config,
    });
    if (result.config) setConfig(result.config);
    setDirty(false);
    setNotice("监控配置已保存");
    await refresh();
  }
  async function perform(operation: () => Promise<void>) {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await operation();
    } catch (error) {
      setError(inventoryError(error));
    } finally {
      setBusy(false);
    }
  }
  const body = (
    <>
      <header className="local-heading monitor-header">
        <div>
          <h1>{compact ? "门店库存监控" : "自动监控"}</h1>
          <p>每秒检查门店库存</p>
        </div>
        <span
          className={`inventory-run-state ${running ? "is-running" : ""}`}
          role="status"
        >
          <Activity size={15} />
          {running ? "监控运行中" : "监控已停止"}
        </span>
      </header>
      {loading ? (
        <p role="status">
          <LoaderCircle className="spin" size={16} />
          读取监控配置…
        </p>
      ) : (
        <>
          <div className="ui-metrics monitor-overview" aria-label="监控概览">
            <div className="ui-metric">
              <span>门店组合</span>
              <strong>{config.targets.length}</strong>
            </div>
            <div className="ui-metric">
              <span>当前有货</span>
              <strong>
                {
                  config.targets.filter((target) => {
                    const sample = result?.targets?.find(
                      (row: Data) =>
                        row.id === target.id ||
                        (row.sku?.replace(/\/A$/, "") ===
                          target.sku.replace(/\/A$/, "") &&
                          row.store_id === target.store_id),
                    );
                    return (
                      sample?.status === "available" &&
                      !!sample.checked_at &&
                      Number.isFinite(timeMs(sample.checked_at)) &&
                      now >= timeMs(sample.checked_at) &&
                      now - timeMs(sample.checked_at) <= 10000 &&
                      !(!running && result?.monitoring === true)
                    );
                  }).length
                }
              </strong>
            </div>
            <div className="ui-metric">
              <span>自动抢购</span>
              <strong>
                {config.targets.filter((target) => target.auto_purchase).length}
              </strong>
            </div>
          </div>
          <div className="ui-toolbar monitor-controls button-row inventory-actions">
            <button
              className="primary"
              disabled={
                busy || running || invalidConfig || !config.targets.length
              }
              onClick={() =>
                perform(async () => {
                  if (dirty) await save();
                  await bridge.start(LOCAL_ID, "inventory_monitor", {});
                  await refresh();
                })
              }
            >
              <Activity size={16} />
              保存并开始监控
            </button>
            <button
              disabled={busy || !stoppableMonitor}
              onClick={() =>
                perform(async () => {
                  if (stoppableMonitor) {
                    await bridge.cancel(
                      stoppableMonitor.account_id,
                      stoppableMonitor.id,
                    );
                    setStoppedMonitorId(stoppableMonitor.id);
                  }
                  await refresh();
                  setNotice("监控已停止，应用重启后保持关闭。");
                })
              }
            >
              <Square size={15} />
              停止监控
            </button>
            <button
              disabled={
                busy || running || invalidConfig || !config.targets.length
              }
              onClick={() =>
                perform(async () => {
                  if (dirty) await save();
                  await inventoryCall(bridge, "inventory_check");
                  await refresh();
                })
              }
            >
              <RefreshCw size={15} />
              检查一次
            </button>
            <button
              disabled={busy || running || invalidConfig || !dirty}
              onClick={() => perform(save)}
            >
              保存配置
            </button>
            <a className="ui-action-link" href="#/queue">
              <ListOrdered size={16} />
              执行队列
            </a>
            <a className="ui-action-link" href="#/logs?category=inventory">
              <ScrollText size={16} />
              监控日志
            </a>
          </div>
          <p className="ui-caption">
            运行中的监控会在应用重启后恢复，按原授权发现有货并下单；停止监控后保持关闭。
          </p>
          {error && (
            <p role="alert" className="error-text">
              {error}
            </p>
          )}
          {notice && (
            <p role="status" className="success-text">
              {notice}
            </p>
          )}
          {!monitor && monitorTasks[0]?.status === "failed" && (
            <p role="alert" className="error-text">
              上次监控停止：{monitorTasks[0].stage}
            </p>
          )}
          <section className="ui-panel stock-events" aria-label="最近有货记录">
            <div className="ui-panel-heading">
              <h2>最近有货</h2>
              <div className="ui-toolbar">
                <small className="ui-caption">历史记录 · 不代表当前库存</small>
                {stockEvents.length > 3 && (
                  <button
                    className="text-button"
                    onClick={() => setHistoryExpanded((value) => !value)}
                  >
                    {historyExpanded
                      ? "收起记录"
                      : `查看全部 ${stockEvents.length} 条`}
                  </button>
                )}
              </div>
            </div>
            {historyError && (
              <p className="muted" role="status">
                暂时无法读取有货记录，稍后重试。
              </p>
            )}
            {!stockEvents.length ? (
              <p className="muted stock-event-empty">暂无有货记录</p>
            ) : (
              <ol className="stock-event-list">
                {stockEvents
                  .slice(0, historyExpanded ? 12 : 3)
                  .map((entry, index) => (
                    <li
                      className="stock-event-row"
                      key={
                        entry.id ??
                        `${entry.details.store_id}-${entry.details.checked_at_ms}-${index}`
                      }
                    >
                      <time
                        dateTime={new Date(
                          Number(entry.details.checked_at_ms),
                        ).toISOString()}
                        title={dateText(Number(entry.details.checked_at_ms))}
                      >
                        <strong>
                          {new Date(
                            Number(entry.details.checked_at_ms),
                          ).toLocaleTimeString("zh-CN", { hour12: false })}
                        </strong>
                        <span>
                          {new Date(
                            Number(entry.details.checked_at_ms),
                          ).toLocaleDateString("zh-CN")}
                        </span>
                      </time>
                      <div className="stock-event-product">
                        <strong>
                          {presetProducts.find(
                            (product) =>
                              product.sku ===
                              String(entry.details.sku).replace(/\/A$/i, ""),
                          )?.name || entry.details.sku}
                        </strong>
                        <span>
                          {entry.details.sku} · {entry.details.store_id}
                        </span>
                      </div>
                      <span className="stock-event-store">
                        {entry.details.store_name}
                      </span>
                    </li>
                  ))}
              </ol>
            )}
          </section>
          <section className="ui-panel inventory-targets">
            <div className="ui-panel-heading">
              <h2>监控门店</h2>
              <span>{config.targets.length} 个商品 / 门店组合</span>
            </div>
            {!config.targets.length && (
              <p className="muted">
                先从官网读取门店，添加需要监控的商品与门店。
              </p>
            )}
            <div className="monitor-target-list">
              {config.targets.map((target) => {
                const stock = result?.targets?.find(
                  (row: Data) =>
                    row.id === target.id ||
                    (row.sku?.replace(/\/A$/, "") ===
                      target.sku.replace(/\/A$/, "") &&
                      row.store_id === target.store_id),
                );
                const hasSample =
                  !!stock?.checked_at &&
                  Number.isFinite(timeMs(stock.checked_at));
                const stale =
                  hasSample &&
                  (now < timeMs(stock.checked_at) ||
                    now - timeMs(stock.checked_at) > 10_000 ||
                    (!running && result?.monitoring === true));
                const liveStatus =
                  stale || !hasSample ? "unknown" : stock?.status || "unknown";
                const message =
                  stockMessage(stock?.error, result?.proxy) ||
                  stock?.pickup_message ||
                  (result?.checked_at
                    ? "本次检查未取得有效库存。"
                    : "等待官网确认实际库存");
                const officialMessage =
                  typeof stock?.pickup_message === "string"
                    ? stock.pickup_message.trim()
                    : "";
                const showOfficialMessage =
                  !!officialMessage && officialMessage !== message;
                const confirmedStock = ["available", "unavailable"].includes(
                  stock?.status,
                );
                const status =
                  liveStatus === "available"
                    ? "有库存"
                    : liveStatus === "unavailable"
                      ? "暂无库存"
                      : "未知";
                return (
                  <article className="inventory-target" key={target.id}>
                    <div className="inventory-target-heading">
                      <div>
                        <strong>{target.store_name}</strong>
                        <span>
                          {presetProducts.find(
                            (product) =>
                              product.sku === target.sku.replace(/\/A$/, ""),
                          )?.name || target.sku}{" "}
                          · {target.sku} · 每路 {target.quantity} 件
                        </span>
                        <small>
                          {target.store_id} · {target.location}
                        </small>
                      </div>
                      <span className={`stock-status ${liveStatus}`}>
                        {status}
                      </span>
                    </div>
                    <p className="muted inventory-stock-message">
                      {stale ? "上次结果已过时，当前库存未知。" : ""}
                      {message}
                      {showOfficialMessage && (
                        <>
                          <br />
                          官网提示：{officialMessage}
                        </>
                      )}
                      <br />
                      {hasSample
                        ? `${confirmedStock ? "有效库存采样" : "官网响应"}：${dateText(stock.checked_at)}`
                        : "尚未取得有效库存"}
                      {!hasSample && result?.checked_at
                        ? ` · 本次尝试：${dateText(result.checked_at)}`
                        : ""}
                    </p>
                    <p className="monitor-target-summary muted">
                      {target.auto_purchase
                        ? `自动抢购 · ${format(snapshot.accounts.find((account) => account.id === target.account_id)?.name || "未选账号")} · ${target.concurrency ?? 1} 路`
                        : "仅监控"}
                    </p>
                    <details className="ui-disclosure monitor-target-settings">
                      <DisclosureSummary>抢购设置</DisclosureSummary>
                      <div className="inventory-purchase-fields">
                        <label className="checkbox-label">
                          <input
                            type="checkbox"
                            checked={target.auto_purchase}
                            disabled={busy || running}
                            onChange={(event) =>
                              updateTarget(target.id, {
                                auto_purchase: event.target.checked,
                              })
                            }
                          />
                          有货立即自提抢购
                        </label>
                        <SelectField
                          label="抢购账号"
                          value={target.account_id}
                          disabled={busy || running}
                          options={[
                            {
                              value: "",
                              label: "仅监控，请选择账号后启用抢购",
                            },
                            ...snapshot.accounts
                              .filter((account) => !account.pending)
                              .map((account) => ({
                                value: account.id,
                                label: format(
                                  `${account.name} · ${account.email}`,
                                ),
                              })),
                          ]}
                          onChange={(value) =>
                            updateTarget(target.id, { account_id: value })
                          }
                        />
                        <NumberField
                          label="并发路数"
                          min={1}
                          max={10}
                          required
                          value={target.concurrency ?? 1}
                          disabled={busy || running}
                          onValidityChange={(valid) =>
                            numberValidity(`${target.id}:concurrency`, valid)
                          }
                          onChange={(value) =>
                            updateTarget(target.id, { concurrency: value })
                          }
                        />
                        <NumberField
                          label="下单失败重试次数"
                          min={0}
                          max={10}
                          required
                          value={target.retry_count ?? 0}
                          disabled={busy || running}
                          onValidityChange={(valid) =>
                            numberValidity(`${target.id}:retry`, valid)
                          }
                          onChange={(value) =>
                            updateTarget(target.id, { retry_count: value })
                          }
                        />
                        <button
                          className="icon-button"
                          aria-label={`移除监控 ${target.store_name} ${target.sku}`}
                          disabled={busy || running}
                          onClick={() =>
                            change({
                              ...config,
                              targets: config.targets.filter(
                                (row) => row.id !== target.id,
                              ),
                            })
                          }
                        >
                          <Trash2 size={16} />
                        </button>
                      </div>
                      {target.auto_purchase && !target.account_id && (
                        <p className="error-text">
                          请选择已登录账号后再开始自动抢购。
                        </p>
                      )}
                      {target.auto_purchase && (
                        <p className="muted">
                          启用后会创建真实订单，可能产生待付款订单。
                        </p>
                      )}
                    </details>
                  </article>
                );
              })}
            </div>
          </section>
          <details
            className="ui-panel ui-disclosure monitor-add-stores"
            open={!config.targets.length}
          >
            <DisclosureSummary className="ui-panel-heading">
              <h2>添加门店与商品</h2>
            </DisclosureSummary>
            <div className="inventory-product-fields">
              <SelectField
                label="监控商品"
                value={
                  presetProducts.some((product) => product.sku === sku)
                    ? sku
                    : "custom"
                }
                disabled={busy}
                options={[
                  ...presetProducts.map((product) => ({
                    value: product.sku,
                    label: `${product.name} · ${product.sku}`,
                  })),
                  { value: "custom", label: "自定义 SKU" },
                ]}
                onChange={(value) => setSku(value === "custom" ? "" : value)}
              />
              <label>
                商品 SKU
                <input
                  required
                  value={sku}
                  disabled={busy}
                  onChange={(event) => setSku(event.target.value.toUpperCase())}
                  placeholder="例如：MLH74FE"
                />
              </label>
              <NumberField
                label="每路购买数量"
                min={1}
                max={99}
                required
                value={quantity}
                disabled={busy}
                onValidityChange={(valid) => numberValidity("quantity", valid)}
                onChange={setQuantity}
              />
            </div>
            <StoreSearch
              bridge={bridge}
              sku={sku}
              location={location}
              onLocation={setLocation}
              onChoose={addStores}
              disabled={
                busy ||
                invalidNumbers.includes("quantity") ||
                !Number.isInteger(quantity) ||
                quantity < 1 ||
                quantity > 99
              }
              chooseDisabled={running}
              multiple
            />
            {running && (
              <p className="muted">
                监控期间可查询官网门店；查询不会修改监控目标。请前往
                <a href="#/settings">自提门店与时段偏好</a>
                保存预选门店并授权自动监控，或停止监控后在此添加。
              </p>
            )}
          </details>
          <section className="ui-panel">
            <details className="ui-disclosure inventory-proxy-settings">
              <DisclosureSummary>
                动态代理池{" "}
                <span>
                  {config.proxy.enabled ? "已启用" : "未启用"}
                  {result?.proxy
                    ? ` · 可派发 ${result.proxy.dispatchable_count ?? 0}`
                    : ""}
                </span>
              </DisclosureSummary>
              <p className="muted">
                仅代理匿名库存和门店查询。登录、验证码、下单及支付使用账号原有连接。无可用代理时显示未知，失败连接会替换；1
                秒为检查间隔，网络耗时会影响新结果到达。
              </p>
              <div className="inventory-proxy-fields">
                <label className="checkbox-label">
                  <input
                    type="checkbox"
                    checked={config.proxy.enabled}
                    disabled={busy || running}
                    onChange={(event) =>
                      change({
                        ...config,
                        proxy: {
                          ...config.proxy,
                          enabled: event.target.checked,
                        },
                      })
                    }
                  />
                  启用动态库存代理池
                </label>
                <NumberField
                  label="连接池容量"
                  min={1}
                  max={100}
                  value={config.proxy.pool_size}
                  disabled={busy || running}
                  onValidityChange={(valid) => numberValidity("pool", valid)}
                  onChange={(value) =>
                    change({
                      ...config,
                      proxy: { ...config.proxy, pool_size: value },
                    })
                  }
                />
              </div>
              {config.proxy.providers.map((provider, index) => {
                const update = (patch: Data) =>
                  change({
                    ...config,
                    proxy: {
                      ...config.proxy,
                      providers: config.proxy.providers.map((row, i) =>
                        i === index ? { ...row, ...patch } : row,
                      ),
                    },
                  });
                return (
                  <fieldset className="inventory-provider" key={provider.id}>
                    <legend>供应商 {index + 1}</legend>
                    <div className="inventory-proxy-fields">
                      <SelectField
                        label="供应商类型"
                        value={provider.kind}
                        disabled={busy || running}
                        options={[
                          { value: "xydaili", label: "星月 / XY" },
                          { value: "dmdaili", label: "大漠 / DM" },
                          { value: "generic", label: "通用提取接口" },
                        ]}
                        onChange={(value) => update({ kind: value })}
                      />
                      <NumberField
                        label="代理有效期（秒）"
                        min={20}
                        max={3600}
                        value={provider.ttl_seconds || 180}
                        disabled={busy || running}
                        onValidityChange={(valid) =>
                          numberValidity(`provider:${provider.id}`, valid)
                        }
                        onChange={(value) => update({ ttl_seconds: value })}
                      />
                    </div>
                    {(
                      [
                        ["extract_url", "提取接口 URL"],
                        ["whitelist_url", "白名单接口 URL（可选）"],
                        ["username", "代理用户名（可选）"],
                        ["password", "代理密码（可选）"],
                      ] as const
                    ).map(([key, label]) => (
                      <label key={key}>
                        {label}
                        <input
                          type="password"
                          autoComplete="new-password"
                          value={provider[key] || ""}
                          disabled={busy || running}
                          placeholder={
                            provider[`${key}_configured`]
                              ? "已保存，留空保持原值"
                              : "尚未设置"
                          }
                          onChange={(event) =>
                            update({ [key]: event.target.value })
                          }
                        />
                      </label>
                    ))}
                    <div className="button-row">
                      <label className="checkbox-label">
                        <input
                          type="checkbox"
                          checked={provider.enabled !== false}
                          disabled={busy || running}
                          onChange={(event) =>
                            update({ enabled: event.target.checked })
                          }
                        />
                        启用此供应商
                      </label>
                      <button
                        type="button"
                        disabled={busy || running}
                        onClick={() =>
                          change({
                            ...config,
                            proxy: {
                              ...config.proxy,
                              providers: config.proxy.providers.filter(
                                (_, i) => i !== index,
                              ),
                            },
                          })
                        }
                      >
                        移除供应商
                      </button>
                    </div>
                  </fieldset>
                );
              })}
              <p className="muted">
                失败重试次数为首次下单以外的额外次数，默认
                0。确认未提交的失败才串行重试，已有订单或提交未知时停止。
              </p>
              <button
                disabled={busy || running || config.proxy.providers.length >= 4}
                onClick={() =>
                  change({
                    ...config,
                    proxy: {
                      ...config.proxy,
                      providers: [
                        ...config.proxy.providers,
                        {
                          id: randomUUID(),
                          kind: "xydaili",
                          enabled: true,
                          ttl_seconds: 180,
                        },
                      ],
                    },
                  })
                }
              >
                <Plus size={15} />
                添加代理供应商
              </button>
              {(result?.proxy?.last_error || result?.proxy?.error) && (
                <p className="error-text">
                  {stockMessage(
                    result.proxy.last_error || result.proxy.error,
                    result.proxy,
                  )}
                </p>
              )}
            </details>
          </section>
          <details className="ui-panel ui-disclosure monitor-instructions">
            <DisclosureSummary>运行说明</DisclosureSummary>
            <p className="muted">
              监控期间目标配置锁定，官网门店查询仍可使用。请保持本地服务运行及电脑唤醒；服务重启后须重新开始监控。
            </p>
            <p className="muted">
              自动抢购按账号自提偏好选择门店和时段，每路按所设数量购买，可能生成多笔待付款订单。确认未提交的失败才重试，已有订单或提交未知时停止。
            </p>
            <a className="ui-action-link" href="#/settings">
              自提门店与时段偏好
            </a>
          </details>
        </>
      )}
    </>
  );
  return compact ? (
    <div className="inventory-monitor compact-monitor">{body}</div>
  ) : (
    <main className="local-page inventory-monitor">{body}</main>
  );
}
