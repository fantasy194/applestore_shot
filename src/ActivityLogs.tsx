import { useEffect, useRef, useState } from "react";
import { Download, RefreshCw, SlidersHorizontal } from "lucide-react";
import type { Bridge } from "./api";
import type { Account, Data } from "./types";
import { useAccountPrivacy } from "./AccountPrivacy";

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
          <p>按需读取记录，后台保留完整历史</p>
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
            <summary>
              <span>
                <SlidersHorizontal size={16} />
                筛选日志
              </span>
              <small>
                {Object.values(filters).filter(Boolean).length
                  ? `已选 ${Object.values(filters).filter(Boolean).length} 项条件`
                  : "全部记录"}
              </small>
            </summary>
            <div className="log-filters">
              <label>
                日志账号
                <select
                  value={filters.account_id}
                  onChange={(event) =>
                    setFilters({ ...filters, account_id: event.target.value })
                  }
                >
                  <option value="">全部账号</option>
                  {accounts.map((account) => (
                    <option key={account.id} value={account.id}>
                      {format(account.name)}
                    </option>
                  ))}
                </select>
              </label>
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
              <label>
                日志类别
                <select
                  value={filters.category}
                  onChange={(event) =>
                    setFilters({ ...filters, category: event.target.value })
                  }
                >
                  <option value="">全部类别</option>
                  {categories.map((category) => (
                    <option key={category} value={category}>
                      {
                        (
                          {
                            inventory: "库存监控",
                            stock: "有货事件",
                            queue: "执行队列",
                            authentication: "登录/会话",
                            proxy: "代理池",
                            purchase: "下单",
                            operation: "操作",
                          } as Record<string, string>
                        )[category]
                      }
                    </option>
                  ))}
                </select>
              </label>
              <label>
                日志级别
                <select
                  value={filters.level}
                  onChange={(event) =>
                    setFilters({ ...filters, level: event.target.value })
                  }
                >
                  <option value="">全部级别</option>
                  <option value="info">信息</option>
                  <option value="warning">警告</option>
                  <option value="error">错误</option>
                  <option value="debug">调试</option>
                </select>
              </label>
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
          <div className="log-entries" aria-label="运行日志记录">
            {entries.map((entry) => (
              <article
                className={`ui-panel log-entry ${entry.level || "info"}`}
                key={entry.id}
              >
                <div>
                  <time>
                    {new Date(Number(entry.ts_ms ?? entry.ts)).toLocaleString(
                      "zh-CN",
                    )}
                  </time>
                  <span>
                    {entry.level || "info"} · {entry.category || "task"}
                  </span>
                </div>
                <p>{format(entry.message || entry.event || "无说明")}</p>
                <small>
                  {format(
                    accounts.find((account) => account.id === entry.account_id)
                      ?.name ||
                      entry.account_id ||
                      "通用工作空间",
                  )}
                  {entry.task_id ? ` · ${entry.task_id}` : ""}
                  {entry.action ? ` · ${entry.action}` : ""}
                </small>
                {entry.details && Object.keys(entry.details).length > 0 && (
                  <details className="ui-disclosure log-details">
                    <summary>查看日志详情</summary>
                    <pre>{format(JSON.stringify(entry.details, null, 2))}</pre>
                  </details>
                )}
              </article>
            ))}
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
