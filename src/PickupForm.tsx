import { useEffect, useState } from "react";
import type { Account, Action, Data } from "./types";
import { active, LOCAL_ID } from "./types";
import type { Bridge } from "./api";
import { StoreSearch } from "./StoreSearch";
import {
  inventoryCall,
  inventoryError,
  scopedTaskCall,
  type InventoryStore,
} from "./inventory-api";

type PreferredStore = { store_id: string; store_name: string; search: string };
type TimeRule = {
  mode: "earliest" | "latest" | "window";
  start: string;
  end: string;
  fallback: "earliest" | "stop";
};
const earliest = (): TimeRule => ({
  mode: "earliest",
  start: "09:00",
  end: "21:00",
  fallback: "stop",
});

export function PickupForm({
  account,
  bridge,
  busy,
  result,
  refresh = async () => {},
}: {
  account: Account;
  bridge: Bridge;
  busy: boolean;
  result?: Data;
  start: (action: Action, params?: Data) => Promise<void>;
  refresh?: () => Promise<void>;
}) {
  const [pickup, setPickup] = useState({
    search: "",
    store_name: "",
    store_id: "",
    id_last4: "",
  });
  const [selectionMode, setSelectionMode] = useState<"fixed" | "priority">(
    "fixed",
  );
  const [stores, setStores] = useState<PreferredStore[]>([]);
  const [rule, setRule] = useState<TimeRule>(earliest);
  const [contactMode, setContactMode] = useState<"local" | "official">("local");
  const [contact, setContact] = useState<Data>({ emailAddress: account.email });
  const [contactTouched, setContactTouched] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const saved = JSON.stringify(result?.config);
  useEffect(() => {
    if (!result?.config) return;
    const config = result.config;
    const prior = config.pickup || {};
    setPickup({
      search: prior.search || "",
      store_name: prior.store_name || "",
      store_id: prior.store_id || "",
      id_last4: prior.id_last4 || "",
    });
    setSelectionMode(
      prior.selection_mode === "priority" ? "priority" : "fixed",
    );
    setStores(
      Array.isArray(prior.stores)
        ? prior.stores
        : prior.store_id
          ? [
              {
                store_id: prior.store_id,
                store_name: prior.store_name || prior.store_id,
                search: prior.search || "",
              },
            ]
          : [],
    );
    setRule({ ...earliest(), ...prior.time_slot_rule });
    setContactMode(prior.contact_mode === "official" ? "official" : "local");
    setContact({ emailAddress: account.email, ...config.contact });
    setContactTouched(false);
  }, [saved, account.email]);
  const disabled = busy || saving;
  const selectedStores: PreferredStore[] =
    selectionMode === "priority"
      ? stores
      : pickup.store_id
        ? [
            {
              store_id: pickup.store_id,
              store_name: pickup.store_name,
              search: pickup.search,
            },
          ]
        : [];
  const sku = (account.preset_sku || "MLH74FE")
    .toUpperCase()
    .replace(/\/A$/, "");
  function payload() {
    if (selectionMode === "priority" && !stores.length)
      throw new Error("请先从官网添加至少一家预选门店。");
    if (
      selectionMode === "fixed" &&
      (!pickup.search.trim() || !pickup.store_name.trim())
    )
      throw new Error("请填写固定门店与搜索城市或邮编。");
    if (
      rule.mode === "window" &&
      (!/^\d{2}:\d{2}$/.test(rule.start) ||
        !/^\d{2}:\d{2}$/.test(rule.end) ||
        rule.start >= rule.end)
    )
      throw new Error("请填写开始时间早于结束时间的有效时段。");
    const anchor = selectedStores[0];
    const policy: Data = {
      ...pickup,
      selection_mode: selectionMode,
      stores: selectedStores,
      time_slot_rule: { ...rule },
      contact_mode: contactMode,
    };
    if (selectionMode === "priority" && anchor)
      Object.assign(policy, {
        search: anchor.search,
        store_id: anchor.store_id,
        store_name: anchor.store_name,
      });
    if (!policy.store_id) delete policy.store_id;
    const includeContact =
      contactMode === "local" ||
      contactTouched ||
      ["firstName", "lastName", "fullDaytimePhone"].some(
        (key) => !!contact[key],
      );
    return {
      pickup_only: true,
      pickup: policy,
      ...(includeContact ? { contact: { ...contact } } : {}),
    };
  }
  async function savePolicy() {
    const saved = await scopedTaskCall(
      bridge,
      account.id,
      "address_save",
      payload(),
    );
    await refresh();
    return saved;
  }
  async function run(monitor: boolean) {
    setSaving(true);
    setError("");
    setNotice("");
    try {
      if (monitor && (account.id === LOCAL_ID || account.pending))
        throw new Error("请选择已完成登录的具体账号后启用自动监控。");
      if (monitor && !selectedStores.length)
        throw new Error("请从官网读取并选择门店编号，再启用自动监控。");
      const saved = await savePolicy();
      if (!monitor) {
        setNotice(saved.message || "自提偏好已保存。");
        return;
      }
      const current = await inventoryCall(bridge, "inventory_settings_read");
      const config: Data = current.config;
      if (!config || !Array.isArray(config.targets))
        throw new Error("未能读取现有监控配置，未授权自动抢购。请重试。");
      const targets: Data[] = config.targets.map((target: Data) => ({
        ...target,
      }));
      for (const store of selectedStores) {
        const existing = targets.find(
          (target) =>
            target.account_id === account.id &&
            String(target.sku).toUpperCase().replace(/\/A$/, "") === sku &&
            target.store_id === store.store_id,
        );
        const details = {
          sku,
          quantity: account.preset_quantity || 1,
          store_id: store.store_id,
          store_name: store.store_name,
          location: store.search,
          account_id: account.id,
          auto_purchase: true,
        };
        if (existing)
          Object.assign(existing, details, {
            concurrency: existing.concurrency ?? 1,
            retry_count: existing.retry_count ?? 0,
          });
        else
          targets.push({
            ...details,
            id: crypto.randomUUID(),
            concurrency: 1,
            retry_count: 0,
          });
      }
      await inventoryCall(bridge, "inventory_settings_save", {
        config: { ...config, targets },
      });
      const state = await bridge.snapshot();
      if (
        !state.tasks.some(
          (task) => task.action === "inventory_monitor" && active(task),
        )
      )
        await bridge.start(LOCAL_ID, "inventory_monitor", {});
      await refresh();
      setNotice(
        "自提偏好已保存，已授权监控预选门店；同组有货门店按预设顺序择一自动下单。",
      );
    } catch (error) {
      setError(inventoryError(error));
    } finally {
      setSaving(false);
    }
  }
  function choose(found: InventoryStore[]) {
    if (selectionMode === "fixed") {
      const store = found[0];
      if (store)
        setPickup({
          ...pickup,
          store_name: store.store_name,
          store_id: store.store_id,
        });
      return;
    }
    const next = [...stores];
    for (const store of found)
      if (!next.some((prior) => prior.store_id === store.store_id))
        next.push({
          store_id: store.store_id,
          store_name: store.store_name,
          search: pickup.search || store.location || "",
        });
    setStores(next);
  }
  function move(index: number, direction: number) {
    const next = [...stores];
    [next[index], next[index + direction]] = [
      next[index + direction],
      next[index],
    ];
    setStores(next);
  }
  return (
    <section className="section pickup-preferences">
      <div className="section-title">
        <h2>自提门店与时段偏好</h2>
        <span>门店与时段偏好</span>
      </div>
      <p className="muted">
        固定单店保留原有下单方式；预选多店时，按下方顺序选择本次实际有货且符合时段规则的门店。官网要求的联系人字段和验证仍须完成。
      </p>
      <label>
        门店选择方式
        <select
          disabled={disabled}
          value={selectionMode}
          onChange={(event) => {
            const next = event.target.value as "fixed" | "priority";
            setSelectionMode(next);
            if (next === "priority") setContactMode("official");
            setNotice("");
          }}
        >
          <option value="fixed">固定单店</option>
          <option value="priority">预选多店，按顺序择一</option>
        </select>
      </label>
      <StoreSearch
        bridge={bridge}
        sku={sku}
        location={pickup.search}
        onLocation={(search) => setPickup({ ...pickup, search, store_id: "" })}
        disabled={disabled}
        multiple={selectionMode === "priority"}
        onChoose={choose}
      />
      {selectionMode === "priority" && (
        <div aria-label="预选自提门店顺序">
          {!stores.length && (
            <p className="muted">
              从官网多选门店后添加。门店顺序优先，再按该店的时段规则选择。
            </p>
          )}
          {stores.map((store, index) => (
            <article className="inventory-target" key={store.store_id}>
              <strong>
                {index + 1}. {store.store_name}
              </strong>
              <p className="muted">
                {store.store_id} · {store.search}
              </p>
              <div className="button-row">
                <button
                  type="button"
                  disabled={disabled || index === 0}
                  aria-label={`上移 ${store.store_name}`}
                  onClick={() => move(index, -1)}
                >
                  上移
                </button>
                <button
                  type="button"
                  disabled={disabled || index === stores.length - 1}
                  aria-label={`下移 ${store.store_name}`}
                  onClick={() => move(index, 1)}
                >
                  下移
                </button>
                <button
                  type="button"
                  disabled={disabled}
                  aria-label={`删除预选 ${store.store_name}`}
                  onClick={() =>
                    setStores(stores.filter((_, i) => i !== index))
                  }
                >
                  删除
                </button>
              </div>
            </article>
          ))}
        </div>
      )}
      {error && (
        <p role="alert" className="error-text">
          {error}
        </p>
      )}
      {notice && <p role="status">{notice}</p>}
      <form
        onSubmit={(event) => {
          event.preventDefault();
          void run(false);
        }}
      >
        {selectionMode === "fixed" && (
          <div className="address-fields">
            <label>
              自提城市 / 邮编
              <input
                disabled={disabled}
                value={pickup.search}
                placeholder="例如：上海 / 201101"
                onChange={(event) =>
                  setPickup({
                    ...pickup,
                    search: event.target.value,
                    store_id: "",
                  })
                }
              />
            </label>
            <label>
              自提门店
              <input
                disabled={disabled}
                value={pickup.store_name}
                placeholder="官网门店名称，例如：七宝"
                onChange={(event) =>
                  setPickup({
                    ...pickup,
                    store_name: event.target.value,
                    store_id: "",
                  })
                }
              />
            </label>
          </div>
        )}
        {selectionMode === "fixed" && pickup.store_id && (
          <p className="muted">
            已选择官网门店：{pickup.store_name} · {pickup.store_id}
          </p>
        )}
        <div className="schedule-fields">
          <label>
            自提时段规则
            <select
              disabled={disabled}
              value={rule.mode}
              onChange={(event) =>
                setRule({
                  ...rule,
                  mode: event.target.value as TimeRule["mode"],
                })
              }
            >
              <option value="earliest">最早可用日期与时段</option>
              <option value="latest">最晚可用日期与时段</option>
              <option value="window">指定时间窗口</option>
            </select>
          </label>
          {rule.mode === "window" && (
            <>
              <label>
                可接受开始时间
                <input
                  type="time"
                  required
                  disabled={disabled}
                  value={rule.start}
                  onChange={(event) =>
                    setRule({ ...rule, start: event.target.value })
                  }
                />
              </label>
              <label>
                可接受结束时间
                <input
                  type="time"
                  required
                  disabled={disabled}
                  value={rule.end}
                  onChange={(event) =>
                    setRule({ ...rule, end: event.target.value })
                  }
                />
              </label>
              <label>
                无匹配时段
                <select
                  disabled={disabled}
                  value={rule.fallback}
                  onChange={(event) =>
                    setRule({
                      ...rule,
                      fallback: event.target.value as TimeRule["fallback"],
                    })
                  }
                >
                  <option value="stop">
                    尝试下一预选门店，全部无匹配时停止
                  </option>
                  <option value="earliest">接受当前优先门店窗外最早时段</option>
                </select>
              </label>
            </>
          )}
        </div>
        <label>
          取货联系人来源
          <select
            disabled={disabled}
            value={contactMode}
            onChange={(event) =>
              setContactMode(event.target.value as "local" | "official")
            }
          >
            <option value="official">优先使用本次官网保存的联系人</option>
            <option value="local">使用本地取货联系人</option>
          </select>
        </label>
        <details open={contactMode === "local"}>
          <summary>本地取货联系人（可选补充）</summary>
          <p className="muted">
            可先保存门店与时段偏好。使用官网联系人时优先读取本次结账已有资料，本地资料仅作补充。
          </p>
          <div className="address-fields">
            <label>
              取货证件号码后四位
              <input
                type="password"
                aria-label="取货证件号码后四位"
                autoComplete="off"
                disabled={disabled}
                maxLength={4}
                pattern="[A-Za-z0-9]{4}"
                value={pickup.id_last4}
                placeholder="仅后四位，不填完整证件号"
                onChange={(event) =>
                  setPickup({ ...pickup, id_last4: event.target.value })
                }
              />
            </label>
            {(
              [
                ["lastName", "取货人姓氏"],
                ["firstName", "取货人名字"],
                ["fullDaytimePhone", "取货手机号"],
                ["emailAddress", "取货通知邮箱"],
              ] as const
            ).map(([key, label]) => (
              <label key={key}>
                {label}
                <input
                  disabled={disabled}
                  type={
                    key === "emailAddress"
                      ? "email"
                      : key === "fullDaytimePhone"
                        ? "tel"
                        : "text"
                  }
                  value={contact[key] || ""}
                  onChange={(event) => {
                    setContact({ ...contact, [key]: event.target.value });
                    setContactTouched(true);
                  }}
                />
              </label>
            ))}
          </div>
        </details>
        <div className="button-row">
          <button type="submit" className="primary" disabled={disabled}>
            保存自提预设
          </button>
          <button
            type="button"
            disabled={disabled || account.id === LOCAL_ID || account.pending}
            onClick={() => void run(true)}
          >
            监控预选门店，有货自动下单
          </button>
        </div>
      </form>
      <p className="muted">
        自动监控使用本账号预设商品 {sku} · 每路 {account.preset_quantity || 1}{" "}
        件。新增监控默认 1 路、失败重试 0
        次。已有监控不中断，其他配置与目标保留；同组按预选顺序择一抢购。
      </p>
    </section>
  );
}
