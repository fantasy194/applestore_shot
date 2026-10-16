import { useEffect, useState } from "react";
import {
  LoaderCircle,
  RefreshCw,
  Settings2,
  Radio,
  LayoutDashboard,
} from "lucide-react";
import type { Bridge } from "./api";
import {
  active,
  LOCAL_ID,
  localDefaults,
  type Account,
  type Action,
  type Data,
  type Snapshot,
  type Task,
} from "./types";
import { duoProducts, presetProducts, priceText } from "./products";
import { useAccountPrivacy } from "./AccountPrivacy";
import { AddressForm } from "./AddressForm";
import { SelectField } from "./SelectField";

export type Route = "workspace" | "settings" | "duo";
const readRoute = (): Route =>
  location.hash === "#/settings"
    ? "settings"
    : location.hash === "#/duo"
      ? "duo"
      : "workspace";
export function useRoute() {
  const [route, setRoute] = useState<Route>(readRoute);
  useEffect(() => {
    const update = () => setRoute(readRoute());
    window.addEventListener("hashchange", update);
    return () => window.removeEventListener("hashchange", update);
  }, []);
  return [
    route,
    (next: Route) => {
      location.hash = next === "workspace" ? "/" : `/${next}`;
      setRoute(next);
    },
  ] as const;
}
export function RouteNav({
  route,
  navigate,
}: {
  route: Route;
  navigate: (route: Route) => void;
}) {
  return (
    <nav className="route-nav" aria-label="页面导航">
      {(
        [
          ["workspace", "账号工作台", LayoutDashboard],
          ["settings", "本地配置", Settings2],
          ["duo", "DUO 状态", Radio],
        ] as const
      ).map(([id, label, Icon]) => (
        <a
          key={id}
          title={label}
          aria-label={label}
          href={id === "workspace" ? "#/" : `#/${id}`}
          aria-current={route === id ? "page" : undefined}
          onClick={(e) => {
            e.preventDefault();
            navigate(id);
          }}
        >
          <Icon size={16} />
          <span>{label}</span>
        </a>
      ))}
    </nav>
  );
}
export function ProductSelect({
  sku,
  onChange,
}: {
  sku: string;
  onChange: (sku: string) => void;
}) {
  const known = presetProducts.some((p) => p.sku === sku.replace(/\/A$/i, ""));
  return (
    <>
      <SelectField
        label="预制商品"
        value={known ? sku.replace(/\/A$/i, "") : ""}
        onChange={onChange}
        options={presetProducts.map((p) => ({
          value: p.sku,
          label: `${p.name} · ${p.sku}`,
        }))}
      />
      {sku.replace(/\/A$/i, "").toUpperCase() === "MJT84CH" && (
        <p className="muted">
          此预设只加购手机，不主动添加 AppleCare+。结算时会核对实际商品、数量、支付方式，以及当前账号已启用的金额上限。
        </p>
      )}
    </>
  );
}
type PageProps = {
  snapshot: Snapshot;
  bridge: Bridge;
  refresh: () => Promise<void>;
};
function TaskStatus({ task, cancel }: { task?: Task; cancel: () => void }) {
  if (!task) return null;
  return (
    <div
      className={`local-task ${task.status === "failed" ? "error-text" : "muted"}`}
      id={`task-${task.id}`}
      tabIndex={-1}
      role={task.status === "failed" ? "alert" : "status"}
    >
      {active(task) && <LoaderCircle size={15} className="spin" />}
      <span>{task.result?.message || task.stage}</span>
      {active(task) && <button onClick={cancel}>取消</button>}
    </div>
  );
}
export function LocalSettings({
  snapshot,
  bridge,
  refresh,
  initialAccount = LOCAL_ID,
}: PageProps & { initialAccount?: string }) {
  const { format } = useAccountPrivacy();
  const [scope, setScope] = useState(initialAccount);
  const [requestId, setRequestId] = useState("");
  const account =
    scope === LOCAL_ID
      ? snapshot.defaults || localDefaults
      : snapshot.accounts.find((a) => a.id === scope) || localDefaults;
  const tasks = snapshot.tasks.filter((t) => t.account_id === account.id);
  const recent = (actions: Action[]) =>
    tasks
      .filter((t) => actions.includes(t.action))
      .sort(
        (a, b) => b.created_at - a.created_at || b.updated_at - a.updated_at,
      );
  const addressTasks = recent(["address_read", "address_save", "region"]);
  const requested = tasks.find((t) => t.id === requestId);
  const result = (
    requested?.status === "succeeded" && requested.result?.config
      ? requested
      : recent(["address_read", "address_save"]).find(
          (t) => t.status === "succeeded",
        )
  )?.result;
  const [dispatching, setDispatching] = useState(false);
  const [error, setError] = useState("");
  const busy = dispatching || tasks.some(active);
  async function start(action: Action, params: Data = {}) {
    setError("");
    setDispatching(true);
    try {
      setRequestId(await bridge.start(account.id, action, params));
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setDispatching(false);
    }
  }
  return (
    <main className="local-page">
      <header className="local-heading">
        <h1>本地配置</h1>
        <p>提前设置地址和购买偏好，保存在本机。</p>
      </header>
      <SelectField
        className="scope-select"
        label="配置范围"
        value={account.id}
        disabled={busy}
        onChange={(value) => {
          setScope(value);
          setError("");
        }}
        options={[
          { value: LOCAL_ID, label: "通用预设 · 新账号默认" },
          ...snapshot.accounts.map((a) => ({
            value: a.id,
            label: `${format(`${a.name} · ${a.email}`)}${a.pending ? "（待完成登录）" : ""}`,
          })),
        ]}
      />
      <p className="muted scope-note">
        {account.id === LOCAL_ID
          ? "新添加的账号会复制通用预设，已有账号的配置不变。无需登录即可保存。"
          : "仅修改此账号的本地配置，无需先登录。"}
      </p>
      {error && (
        <p className="error-text" role="alert">
          {error}
        </p>
      )}
      <PurchasePreset
        key={`purchase-${account.id}`}
        account={account}
        bridge={bridge}
        refresh={refresh}
      />
      <AddressForm
        key={`address-${account.id}`}
        account={account}
        busy={busy}
        result={result || undefined}
        region={
          recent(["region"]).find((t) => t.status === "succeeded")?.result
            ?.region
        }
        start={start}
      />
      <TaskStatus
        task={requested || addressTasks[0]}
        cancel={() => {
          const current = requested || addressTasks[0];
          if (current)
            bridge
              .cancel(account.id, current.id)
              .catch((e) => setError(String(e)));
        }}
      />
    </main>
  );
}
function PurchasePreset({
  account,
  bridge,
  refresh,
}: {
  account: Account;
  bridge: Bridge;
  refresh: () => Promise<void>;
}) {
  const [sku, setSku] = useState(account.preset_sku || "MK2M4CH");
  const [quantity, setQuantity] = useState(account.preset_quantity || 1);
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const changeSku = (value: string) => {
    setSku(value);
    setStatus("");
  };
  return (
    <section className="section">
      <div className="section-title">
        <h2>购买预设</h2>
      </div>
      <form
        onSubmit={async (e) => {
          e.preventDefault();
          setSaving(true);
          setError("");
          setStatus("");
          try {
            await bridge.savePreset(account.id, sku, quantity);
            await refresh();
            setStatus("购买预设已保存");
          } catch (e) {
            setError(String(e));
          } finally {
            setSaving(false);
          }
        }}
      >
        <ProductSelect sku={sku} onChange={changeSku} />
        <div className="buy-fields">
          <label>
            数量
            <input
              required
              type="number"
              min={1}
              max={999}
              value={quantity}
              onChange={(e) => {
                setQuantity(Number(e.target.value));
                setStatus("");
              }}
            />
          </label>
        </div>
        <p className="muted">
          预制商品仅用于快捷选择，价格与可购买状态以官网实时结果及结账金额为准。
        </p>
        <div className="button-row">
          <button type="submit" disabled={saving || !presetProducts.some((p) => p.sku === sku.replace(/\/A$/i, ""))}>
            保存购买预设
          </button>
          <span role="status">{status}</span>
        </div>
        {error && (
          <p className="error-text" role="alert">
            {error}
          </p>
        )}
      </form>
    </section>
  );
}
export function DuoStatus({ snapshot, bridge, refresh }: PageProps) {
  const [requestId, setRequestId] = useState("");
  const tasks = snapshot.tasks
    .filter((t) => t.account_id === LOCAL_ID && t.action === "catalog")
    .sort((a, b) => b.created_at - a.created_at || b.updated_at - a.updated_at);
  const task = tasks.find((t) => t.id === requestId) || tasks[0];
  const result = tasks.find((t) => t.status === "succeeded")?.result;
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState("");
  const busy = starting || tasks.some(active);
  const live: Data[] = result?.items || [];
  const normalize = (sku: string) => sku.toUpperCase().replace(/\/A$/, "");
  const rows = duoProducts.map((p) => ({
      preset: p,
      item: live.find((i) => normalize(i.sku) === p.sku),
    }));
  return (
    <main className="local-page duo-page">
      <header className="local-heading">
        <div>
          <h1>DUO 状态</h1>
          <p>公开发售状态 · 无需登录 Apple 账号</p>
        </div>
        <button
          className="primary"
          disabled={busy}
          onClick={async () => {
            setStarting(true);
            setError("");
            try {
              setRequestId(
                await bridge.start(LOCAL_ID, "catalog", { mode: "duo" }),
              );
              await refresh();
            } catch (e) {
              setError(String(e));
            } finally {
              setStarting(false);
            }
          }}
        >
          {busy ? (
            <LoaderCircle size={15} className="spin" />
          ) : (
            <RefreshCw size={15} />
          )}
          刷新状态
        </button>
      </header>
      <p className="muted">
        内置 {duoProducts.length}
        款预制商品。点击刷新读取官网选购页；未知表示官网未提供明确的加购许可，不代表有货。
      </p>
      {error && (
        <p role="alert" className="error-text">
          {error}
        </p>
      )}
      <TaskStatus
        task={task}
        cancel={() => {
          if (task)
            bridge.cancel(LOCAL_ID, task.id).catch((e) => setError(String(e)));
        }}
      />
      <div className="duo-meta">
        <span>
          {result?.queried_at
            ? `上次成功查询：${new Date(result.queried_at).toLocaleString("zh-CN")}`
            : "尚未查询官网 · 当前为预制资料"}
        </span>
        <a
          href="https://www.apple.com.cn/shop/buy-iphone/iphone-duo"
          target="_blank"
          rel="noreferrer"
        >
          官网选购页 ↗
        </a>
      </div>
      {task?.status === "failed" && result && (
        <p className="error-text">
          刷新失败，下方保留上次成功结果，并非本次实时状态。
        </p>
      )}
      {result?.purchase_message && (
        <p className="duo-message">{result.purchase_message}</p>
      )}
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>SKU</th>
              <th>型号 / 配置</th>
              <th>价格</th>
              <th>发售 / 加购状态</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(({ preset, item }) => {
              const status =
                item?.can_add_to_bag === false
                  ? "未开放"
                  : item?.can_add_to_bag === true
                    ? "可加购"
                    : "未知";
              return (
                <tr key={preset?.sku || item?.sku}>
                  <td>
                    <code>{preset?.sku || item?.sku}</code>
                  </td>
                  <td>
                    {item?.name && item.name !== "iPhone Duo"
                      ? item.name
                      : preset?.name || item?.name}
                  </td>
                  <td className="price-cell">
                    {priceText(item?.price ?? preset?.price)}
                    <small>
                      {item?.price != null ? "官网价格" : "预设参考价"}
                    </small>
                  </td>
                  <td>
                    <span
                      className={`release-status ${status === "未开放" ? "closed" : ""}`}
                    >
                      {status}
                    </span>
                    <small>
                      {item
                        ? item.coming_soon === false
                          ? "官网未标记为即将发售；许可待确认"
                          : item.reason
                        : result
                          ? "本次响应未包含此 SKU"
                          : "待刷新官网"}
                    </small>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="muted">只读查询，不进行加购，也不查询门店或配送库存。</p>
    </main>
  );
}
