import { Fragment, useEffect, useRef, useState } from "react";
import { Download, RefreshCw, SlidersHorizontal } from "lucide-react";
import type { Bridge } from "./api";
import type { Account, Data } from "./types";
import { useAccountPrivacy } from "./AccountPrivacy";
import { SelectField } from "./SelectField";
import { DisclosureSummary } from "./DisclosureSummary";

const categoryNames: Record<string, string> = {
  inventory: "库存监控",
  stock: "有货事件",
  queue: "执行队列",
  authentication: "登录/会话",
  proxy: "代理池",
  purchase: "下单",
  operation: "操作",
};
function logTime(entry: Data) {
  const date = new Date(Number(entry.ts_ms ?? entry.ts));
  if (!Number.isFinite(date.valueOf()))
    return {
      day: "日期未知",
      time: "时间未知",
      full: "时间未知",
      iso: undefined,
    };
  const pad = (value: number, width = 2) => String(value).padStart(width, "0");
  const day = `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
  const time = `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}.${pad(date.getMilliseconds(), 3)}`;
  return { day, time, full: `${day} ${time}`, iso: date.toISOString() };
}
function logLevel(entry: Data) {
  const raw = String(entry.level || "info").toLowerCase();
  if (raw === "warn" || raw === "warning")
    return { tone: "warning", text: "WARN" };
  if (["error", "fatal", "critical"].includes(raw))
    return { tone: "error", text: "ERROR" };
  if (raw === "debug") return { tone: "debug", text: "DEBUG" };
  return { tone: "info", text: raw.toUpperCase() };
}
function logMessage(entry: Data) {
  const message = String(entry.message || entry.event || "无说明");
  const context = [
    entry.details?.store_name || entry.details?.store_id,
    entry.details?.sku,
  ].filter(
    (value): value is string =>
      typeof value === "string" && !!value && !message.includes(value),
  );
  return [message, ...context].join(" · ");
}

const readFilters = () => {
  const query = new URLSearchParams(location.hash.split("?")[1] || "");
  return {
    account_id: query.get("account_id") || "",
    task_id: query.get("task_id") || "",
    category: query.has("category")
      ? query.get("category") || ""
      : query.has("account_id") || query.has("task_id")
        ? ""
        : "inventory",
    level: query.get("level") || "",
  };
};
export function ActivityLogs({
  bridge,
  accounts,
}: {
  bridge: Bridge;
  accounts: Account[];
}) {
  const { format } = useAccountPrivacy();
  const [filters, setFilters] = useState(readFilters);
  const [entries, setEntries] = useState<Data[]>([]);
  const [cursor, setCursor] = useState<string | number | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [automatic, setAutomatic] = useState(true);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [filtersOpen, setFiltersOpen] = useState(() =>
    location.hash.includes("?"),
  );
  const inFlight = useRef(false);
  const automaticRef = useRef(automatic);
  automaticRef.current = automatic;
  const generation = useRef(0);
  const newestId = useRef<number | null>(null);
  const countRef = useRef(0);
  countRef.current = entries.length;
  const filterKey = JSON.stringify(filters);
  useEffect(() => {
    const update = () => {
      const next = readFilters();
      setFilters(next);
      if (location.hash.includes("?")) setFiltersOpen(true);
    };
    window.addEventListener("hashchange", update);
    return () => window.removeEventListener("hashchange", update);
  }, []);
  async function load(before: string | number | null, expected: number) {
    if (!bridge.logs || inFlight.current) return false;
    inFlight.current = true;
    setLoading(true);
    try {
      const params: Data = { limit: 40 };
      Object.entries(filters).forEach(([key, value]) => {
        if (value) params[key] = value;
      });
      if (before !== null) params.before_id = before;
      else if (newestId.current !== null) params.after_id = newestId.current;
      const result = await bridge.logs(params);
      if (expected !== generation.current) return true;
      const next = Array.isArray(result.entries) ? result.entries : [];
      for (const entry of next) {
        if (Number.isSafeInteger(Number(entry.id)))
          newestId.current = Math.max(newestId.current ?? 0, Number(entry.id));
      }
      setEntries((previous) => {
        const all = [...next, ...previous];
        return all
          .filter(
            (entry, index) =>
              all.findIndex((row) => row.id === entry.id) === index,
          )
          .sort(
            (a, b) =>
              Number(b.ts_ms ?? b.ts ?? 0) - Number(a.ts_ms ?? a.ts ?? 0) ||
              Number(b.id) - Number(a.id),
          );
      });
      if (before !== null || params.after_id === undefined) {
        setCursor(result.next_before_id ?? null);
        setHasMore(result.has_more === true);
      }
      setError("");
    } catch (error) {
      if (expected === generation.current)
        setError(error instanceof Error ? error.message : String(error));
    } finally {
      inFlight.current = false;
      if (expected === generation.current) setLoading(false);
    }
    return true;
  }
  useEffect(() => {
    const expected = ++generation.current;
    setEntries([]);
    setCursor(null);
    setHasMore(false);
    newestId.current = null;
    let disposed = false;
    let polling = false;
    let loadedOnce = false;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      if (disposed || polling) return;
      if (
        document.visibilityState === "hidden" ||
        (loadedOnce && !automaticRef.current) ||
        countRef.current >= 400
      ) {
        if (!disposed) timer = setTimeout(poll, 1000);
        return;
      }
      polling = true;
      const loaded = await load(null, expected);
      polling = false;
      if (loaded) loadedOnce = true;
      if (!disposed) timer = setTimeout(poll, loaded ? 2000 : 100);
    }
    const visible = () => {
      if (document.visibilityState !== "hidden") {
        clearTimeout(timer);
        void poll();
      }
    };
    document.addEventListener("visibilitychange", visible);
    void poll();
    return () => {
      document.removeEventListener("visibilitychange", visible);
      disposed = true;
      clearTimeout(timer);
      ++generation.current;
    };
  }, [bridge, filterKey, refresh]);
  const categories = [
    "inventory",
    "stock",
    "queue",
    "authentication",
    "proxy",
    "purchase",
    "operation",
  ];
  const readable = (entry: Data) =>
    `${new Date(Number(entry.ts_ms ?? entry.ts)).toLocaleString("zh-CN")} [${entry.level || "info"}] [${entry.category || "task"}] ${entry.message || entry.event || ""}`;
  function download() {
    const blob = new Blob([entries.map(readable).join("\n")], {
      type: "text/plain;charset=utf-8",
    });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "duo-activity-logs.txt";
    link.click();
    URL.revokeObjectURL(url);
  }
  return (
    <main className="local-page activity-logs">
      <header className="local-heading">
        <div>
          <h1>运行日志</h1>
          <p>最新在前 · 点按记录展开完整内容</p>
        </div>
        <button
          disabled={loading}
          onClick={() => setRefresh((value) => value + 1)}
        >
          <RefreshCw size={15} />
          刷新日志
        </button>
      </header>
      {!bridge.logs ? (
        <p>当前服务暂不支持完整日志，请启动最新版本。</p>
      ) : (
        <>
          <div className="ui-toolbar" aria-label="重点日志">
            <button
              className={filters.category === "inventory" ? "primary" : ""}
              onClick={() =>
                setFilters({
                  account_id: "",
                  task_id: "",
                  category: "inventory",
                  level: "",
                })
              }
            >
              自动监控
            </button>
            <button
              className={filters.category === "queue" ? "primary" : ""}
              onClick={() =>
                setFilters({
                  account_id: "",
                  task_id: "",
                  category: "queue",
                  level: "",
                })
              }
            >
              队列下单
            </button>
          </div>
          <details
            className="ui-panel ui-disclosure log-filter-panel"
            open={filtersOpen}
            onToggle={(event) => setFiltersOpen(event.currentTarget.open)}
          >
            <DisclosureSummary>
              <span>
                <SlidersHorizontal size={16} />
                筛选日志
              </span>
              <small>
                {Object.values(filters).filter(Boolean).length
                  ? `已选 ${Object.values(filters).filter(Boolean).length} 项条件`
                  : "全部记录"}
              </small>
            </DisclosureSummary>
            <div className="log-filters">
              <SelectField
                label="日志账号"
                value={filters.account_id}
                onChange={(value) =>
                  setFilters({ ...filters, account_id: value })
                }
                options={[
                  { value: "", label: "全部账号" },
                  ...accounts.map((account) => ({
                    value: account.id,
                    label: format(account.name),
                  })),
                ]}
              />
              <label>
                日志任务编号
                <input
                  value={filters.task_id}
                  placeholder="全部任务"
                  onChange={(event) =>
                    setFilters({
                      ...filters,
                      task_id: event.target.value.trim(),
                    })
                  }
                />
              </label>
              <SelectField
                label="日志类别"
                value={filters.category}
                onChange={(value) =>
                  setFilters({ ...filters, category: value })
                }
                options={[
                  { value: "", label: "全部类别" },
                  ...categories.map((category) => ({
                    value: category,
                    label: categoryNames[category] || category,
                  })),
                ]}
              />
              <SelectField
                label="日志级别"
                value={filters.level}
                onChange={(value) => setFilters({ ...filters, level: value })}
                options={[
                  { value: "", label: "全部级别" },
                  { value: "info", label: "信息" },
                  { value: "warning", label: "警告" },
                  { value: "error", label: "错误" },
                  { value: "debug", label: "调试" },
                ]}
              />
            </div>
          </details>
          <div className="ui-toolbar log-toolbar">
            <label className="checkbox-label">
              <input
                type="checkbox"
                checked={automatic}
                onChange={(event) => setAutomatic(event.target.checked)}
              />
              自动刷新日志
            </label>
            <button disabled={!entries.length} onClick={download}>
              <Download size={15} />
              下载当前日志
            </button>
            <span className="ui-caption">已加载 {entries.length} 条</span>
          </div>
          {error && (
            <p role="alert" className="error-text">
              {error}
            </p>
          )}
          {!entries.length && (
            <p role="status" className="muted">
              {loading ? "正在读取日志…" : "没有符合筛选条件的日志。"}
            </p>
          )}
          <div className="log-entries" role="region" aria-label="运行日志记录">
            {entries.map((entry, index) => {
              const time = logTime(entry);
              const level = logLevel(entry);
              const message = format(logMessage(entry));
              const account = format(
                accounts.find((row) => row.id === entry.account_id)?.name ||
                  entry.account_id ||
                  "通用工作空间",
              );
              return (
                <Fragment key={entry.id}>
                  {(index === 0 ||
                    logTime(entries[index - 1]).day !== time.day) && (
                    <h2 className="log-date">{time.day}</h2>
                  )}
                  <details
                    className="log-entry"
                    data-level={level.tone}
                    data-log-id={entry.id}
                  >
                    <DisclosureSummary className="log-line">
                      <time
                        className="log-time"
                        dateTime={time.iso}
                        title={time.full}
                      >
                        {time.time}
                      </time>
                      <span className="log-level" data-level={level.tone}>
                        {level.text}
                      </span>
                      <span className="log-message">
                        {message.replace(/\s+/g, " ")}
                      </span>
                    </DisclosureSummary>
                    <div className="log-body">
                      <p className="log-full-message">{message}</p>
                      <dl className="log-meta">
                        <dt>时间</dt>
                        <dd>{time.full}</dd>
                        <dt>账号</dt>
                        <dd>{account}</dd>
                        <dt>类别</dt>
                        <dd>
                          {categoryNames[entry.category] ||
                            entry.category ||
                            "普通任务"}
                        </dd>
                        {entry.task_id && (
                          <>
                            <dt>任务</dt>
                            <dd>{format(String(entry.task_id))}</dd>
                          </>
                        )}
                        {entry.action && (
                          <>
                            <dt>操作</dt>
                            <dd>{format(String(entry.action))}</dd>
                          </>
                        )}
                        {entry.event && (
                          <>
                            <dt>事件</dt>
                            <dd>{format(String(entry.event))}</dd>
                          </>
                        )}
                      </dl>
                      {entry.details &&
                        Object.keys(entry.details).length > 0 && (
                          <pre>
                            {format(JSON.stringify(entry.details, null, 2))}
                          </pre>
                        )}
                    </div>
                  </details>
                </Fragment>
              );
            })}
          </div>
          {entries.length >= 400 && (
            <p role="status" className="muted">
              自动追加已暂停；已加载的历史记录仍保留。调整筛选或刷新以读取最新记录。
            </p>
          )}
          {hasMore && entries.length < 400 && (
            <button
              disabled={loading || cursor === null}
              onClick={() => void load(cursor, generation.current)}
            >
              加载更早日志
            </button>
          )}
        </>
      )}
    </main>
  );
}
