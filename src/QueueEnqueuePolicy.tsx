import { useEffect, useRef, useState } from "react";
import type { Bridge } from "./api";
import { active, type Account, type Data, type Task } from "./types";
import { scopedTaskCall } from "./inventory-api";
import { useAccountPrivacy } from "./AccountPrivacy";
import { DisclosureSummary } from "./DisclosureSummary";
import { SelectField } from "./SelectField";

type Mode = "priority" | "fixed";
type Loaded = { account: string; pickup: Data; mode: Mode | null };
const record = (value: unknown): value is Data =>
  !!value && typeof value === "object" && !Array.isArray(value);
const storeValid = (store: Data | undefined) =>
  !!store &&
  /^R\d{1,8}$/.test(store.store_id || "") &&
  typeof store.store_name === "string" &&
  !!store.store_name.trim() &&
  typeof store.search === "string" &&
  !!store.search.trim();
function actualMode(pickup: Data): Mode | null {
  if (pickup.selection_mode === "priority" || pickup.selection_mode === "fixed")
    return pickup.selection_mode;
  if (pickup.selection_mode !== undefined)
    throw new Error("已保存的入队方式无效，请打开本地自提配置核对。");
  return pickup.search || pickup.store_name || pickup.store_id ? "fixed" : null;
}
function stable(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stable).join(",")}]`;
  if (record(value))
    return `{${Object.keys(value)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${stable(value[key])}`)
      .join(",")}}`;
  return JSON.stringify(value) ?? "null";
}

export function QueueEnqueuePolicy({
  bridge,
  accounts,
  tasks,
  initialAccountId,
  onConfigure,
}: {
  bridge: Bridge;
  accounts: Account[];
  tasks: Task[];
  initialAccountId?: string;
  onConfigure: (id: string) => void;
}) {
  const { format } = useAccountPrivacy();
  const available = accounts.filter((account) => !account.pending);
  const [accountId, setAccountId] = useState(
    initialAccountId || available[0]?.id || "",
  );
  const [open, setOpen] = useState(false);
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const [draft, setDraft] = useState<Mode>("priority");
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const version = useRef(0);
  const scope = useRef(accountId);
  scope.current = accountId;
  const ownReads = useRef(new Set<string>());
  const currentTasks = useRef(tasks);
  currentTasks.current = tasks;
  const accountBusy = tasks.some(
    (task) =>
      task.account_id === accountId &&
      active(task) &&
      task.action !== "inventory_monitor" &&
      !(task.action === "address_read" && ownReads.current.has(task.id)),
  );
  const current = loaded?.account === accountId ? loaded : null;
  const pickup = current?.pickup || {};
  const stores: Data[] = Array.isArray(pickup.stores) ? pickup.stores : [];
  const validStores =
    stores.length >= 1 &&
    stores.length <= 30 &&
    stores.every(storeValid) &&
    new Set(stores.map((store) => store.store_id)).size === stores.length;
  const fixedStore = storeValid(pickup) ? pickup : stores.find(storeValid);
  const canConfigureMode = draft === "priority" ? validStores : !!fixedStore;
  const disabled = loading || saving || accountBusy;

  function selectAccount(id: string) {
    if (saving) return;
    ++version.current;
    scope.current = id;
    setAccountId(id);
    setLoaded(null);
    setDraft("priority");
    setLoading(false);
    setError("");
    setNotice("");
  }
  useEffect(() => {
    if (saving) return;
    if (!available.some((account) => account.id === accountId))
      selectAccount(available[0]?.id || "");
  }, [accounts, accountId, saving]);

  async function readPickup(id: string) {
    const tracked: Bridge = {
      ...bridge,
      start: async (owner, action, params) => {
        const taskId = await bridge.start(owner, action, params);
        if (action === "address_read") ownReads.current.add(taskId);
        return taskId;
      },
    };
    const result = await scopedTaskCall(tracked, id, "address_read");
    if (
      !record(result.config) ||
      (result.config.pickup !== undefined && !record(result.config.pickup))
    )
      throw new Error("未能完整读取本地自提配置，请重试。");
    const pickup: Data = structuredClone(result.config.pickup || {});
    return { account: id, pickup, mode: actualMode(pickup) };
  }
  async function load() {
    if (!accountId || accountBusy || saving || loading) return;
    const id = accountId;
    const request = ++version.current;
    setLoading(true);
    setError("");
    setNotice("");
    try {
      const next = await readPickup(id);
      if (scope.current !== id || version.current !== request) return;
      setLoaded(next);
      setDraft(next.mode || "priority");
    } catch (cause) {
      if (scope.current === id && version.current === request) {
        setLoaded(null);
        setError(cause instanceof Error ? cause.message : String(cause));
      }
    } finally {
      if (scope.current === id && version.current === request)
        setLoading(false);
    }
  }
  useEffect(() => {
    if (
      open &&
      accountId &&
      !accountBusy &&
      !loading &&
      !saving &&
      !current &&
      !error
    )
      void load();
  }, [open, accountId, accountBusy, loading, saving, current, error, bridge]);

  async function save() {
    if (!current || disabled || !canConfigureMode || draft === current.mode)
      return;
    const id = accountId;
    const request = ++version.current;
    const before = current;
    const nextMode = draft;
    setSaving(true);
    setError("");
    setNotice("");
    try {
      const fresh = await readPickup(id);
      if (scope.current !== id || version.current !== request) return;
      if (stable(fresh.pickup) !== stable(before.pickup)) {
        setLoaded(fresh);
        setDraft(fresh.mode || "priority");
        setNotice("本地自提配置已更新，已重新载入。请核对后再次选择并保存。");
        return;
      }
      if (
        currentTasks.current.some(
          (task) =>
            task.account_id === id &&
            active(task) &&
            task.action !== "inventory_monitor" &&
            !(task.action === "address_read" && ownReads.current.has(task.id)),
        )
      )
        throw new Error("该账号正在处理其他任务，请空闲后再保存。");
      const next = { ...fresh.pickup, selection_mode: nextMode };
      if (nextMode === "fixed" && !storeValid(next) && fixedStore)
        Object.assign(next, {
          store_id: fixedStore.store_id,
          store_name: fixedStore.store_name,
          search: fixedStore.search,
        });
      const result = await scopedTaskCall(bridge, id, "address_save", {
        pickup_only: true,
        pickup: next,
      });
      if (scope.current !== id || version.current !== request) return;
      if (!record(result.config) || !record(result.config.pickup))
        throw new Error("保存结果未包含完整自提配置，请重新读取核对。");
      const saved = structuredClone(result.config.pickup);
      setLoaded({ account: id, pickup: saved, mode: actualMode(saved) });
      setDraft(actualMode(saved) || "priority");
      setNotice("入队方式已保存，已同步此账号的本地自提偏好。");
    } catch (cause) {
      if (scope.current === id && version.current === request)
        setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      if (scope.current === id && version.current === request) setSaving(false);
    }
  }

  return (
    <details
      className="ui-panel ui-disclosure queue-enqueue-policy"
      onToggle={(event) => {
        const next = event.currentTarget.open;
        setOpen(next);
        if (!saving) {
          ++version.current;
          setLoaded(null);
          setLoading(false);
          setError("");
          setNotice("");
        }
      }}
    >
      <DisclosureSummary>入队方式 · 按账号设置</DisclosureSummary>
      <SelectField
        label="入队策略账号"
        value={accountId}
        disabled={saving || !available.length}
        options={available.map((account) => ({
          value: account.id,
          label: format(`${account.name} · ${account.email}`),
        }))}
        onChange={selectAccount}
      />
      {!available.length ? (
        <p className="muted">添加账号后，可设置该账号的入队方式。</p>
      ) : (
        <>
          <p className="queue-enqueue-current">
            当前方式：
            {current
              ? current.mode === "priority"
                ? "预选多店，按顺序择一"
                : current.mode === "fixed"
                  ? "各监控门店分别入队（兼容）"
                  : "尚未配置"
              : "尚未读取"}
          </p>
          {loading && <p role="status">正在读取本地自提配置…</p>}
          {accountBusy && (
            <p role="status">
              该账号有任务进行中，空闲后再读取或修改入队方式。
            </p>
          )}
          {error && (
            <p role="alert" className="error-text">
              {error}
            </p>
          )}
          {notice && <p role="status">{notice}</p>}
          <SelectField
            label="账号入队方式"
            value={draft}
            disabled={disabled || !current}
            onChange={(value) => {
              setDraft(value as Mode);
              setNotice("");
            }}
            options={[
              { value: "priority", label: "预选多店，按顺序择一（默认）" },
              { value: "fixed", label: "各监控门店分别入队（兼容）" },
            ]}
          />
          <p>
            按顺序择一：同账号、同商品和数量的候选门店只生成一次购买意图。分别入队：每个已授权监控门店各自入队，可能各生成订单。
          </p>
          {current && (
            <p>
              {draft === "priority"
                ? validStores
                  ? `预选顺序：${stores.map((store) => store.store_name).join(" → ")}`
                  : "请先在本地配置中添加有效预选门店和顺序。"
                : fixedStore
                  ? `手动与定时自提固定门店：${fixedStore.store_name}`
                  : "请先在本地配置中保存有效固定门店。"}
            </p>
          )}
          <p>
            此设置同步同账号手动和定时自提。已入队任务保留原规则和位置，不自动转换或重新执行。
          </p>
          <div className="ui-toolbar">
            <button
              type="button"
              disabled={
                disabled ||
                !current ||
                !canConfigureMode ||
                draft === current.mode
              }
              onClick={() => void save()}
            >
              保存入队方式
            </button>
            <button
              type="button"
              disabled={disabled || !accountId}
              onClick={() => onConfigure(accountId)}
            >
              配置门店与顺序
            </button>
            {!current && (
              <button
                type="button"
                disabled={disabled}
                onClick={() => void load()}
              >
                重新读取入队配置
              </button>
            )}
          </div>
        </>
      )}
    </details>
  );
}
