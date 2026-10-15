import { useEffect, useLayoutEffect, useState, type FormEvent } from "react";
import {
  Plus,
  ShoppingBag,
  ListOrdered,
  MapPin,
  PackageSearch,
  LayoutDashboard,
  ArrowRight,
  RefreshCw,
  LoaderCircle,
  X,
  Eye,
  EyeOff,
  Check,
  FolderLock,
  UserRound,
  KeyRound,
  Trash2,
} from "lucide-react";
import { api, isLocalWeb } from "./api";
import {
  DuoStatus,
  LocalSettings,
  ProductSelect,
  RouteNav,
  useRoute,
} from "./LocalPages";
import { presetProducts } from "./products";
import { AccountPrivacyToggle, useAccountPrivacy } from "./AccountPrivacy";
import { AccountGate } from "./AccountGate";
import { RemoveAccountDialog } from "./RemoveAccountDialog";
import { SelectField } from "./SelectField";
import { ActiveTaskNav } from "./ActiveTaskNav";
import {
  active,
  LOCAL_ID,
  actionNames,
  stateNames,
  type Account,
  type Action,
  type Data,
  type Prompt,
  type Snapshot,
  type Task,
} from "./types";

const tabs = [
  ["home", "工作台", LayoutDashboard],
  ["bag", "购物袋", ShoppingBag],
  ["orders", "订单", ListOrdered],
  ["catalog", "商品", PackageSearch],
] as const;
const initial: Snapshot = {
  accounts: [],
  tasks: [],
  data_dir: "",
};
const errorText = (e: unknown) =>
  typeof e === "string"
    ? e
    : e instanceof Error
      ? e.message
      : "操作未完成，请重试。";

export default function App() {
  const { format, setRevealed } = useAccountPrivacy();
  const [route, navigate] = useRoute();
  const [snapshot, setSnapshot] = useState(initial);
  const [ready, setReady] = useState(false);
  const [accountId, setAccountId] = useState("");
  const [enabledIds, setEnabledIds] = useState<string[]>([]);
  const [sessionReady, setSessionReady] = useState(false);
  const [tab, setTab] = useState("home");
  const [adding, setAdding] = useState(false);
  const [error, setError] = useState("");
  const [dispatching, setDispatching] = useState(false);
  const [sku, setSku] = useState("MK2M4CH");
  const [quantity, setQuantity] = useState(2);
  const [taskTarget, setTaskTarget] = useState<Task | null>(null);
  const [focusTaskId, setFocusTaskId] = useState("");
  const [removingAccount, setRemovingAccount] = useState<Account | null>(null);
  const [accountGateVersion, setAccountGateVersion] = useState(0);
  const bridge = api;
  useEffect(() => {
    if (!bridge) {
      setReady(true);
      return;
    }
    let disposed = false;
    let unlisten: (() => void) | undefined;
    (async () => {
      unlisten = await bridge.subscribe((task) => {
        const removed =
          task.status === "failed" && task.result?.account_removed === true;
        if (removed) {
          setError(task.stage);
          setEnabledIds((ids) => ids.filter((id) => id !== task.account_id));
          setAccountId((id) => (id === task.account_id ? "" : id));
        }
        setSnapshot((s) => ({
          ...s,
          tasks: [task, ...s.tasks.filter((t) => t.id !== task.id)],
          accounts: removed
            ? s.accounts.filter((a) => a.id !== task.account_id)
            : task.status === "succeeded" &&
                ["me", "enroll"].includes(task.action) &&
                task.result?.success === true &&
                task.result?.logged_in === true &&
                task.result?.scope === "account"
              ? s.accounts.map((a) =>
                  a.id === task.account_id
                    ? {
                        ...a,
                        name:
                          typeof task.result?.name === "string" &&
                          task.result.name.trim()
                            ? task.result.name.trim()
                            : a.email,
                        pending: task.action === "enroll" ? false : a.pending,
                      }
                    : a,
                )
              : s.accounts,
        }));
      });
      const state = await bridge.snapshot();
      if (!disposed) {
        setSnapshot(state);
        // A single enrolled account is already chosen. Reopening the UI must
        // not enqueue another login check behind its unfinished checkout.
        // Worker actions still verify the Apple session before using it.
        const savedAccounts = state.accounts.filter((a) => !a.pending);
        if (savedAccounts.length === 1) {
          setAccountId(savedAccounts[0].id);
          setEnabledIds([savedAccounts[0].id]);
          setSessionReady(true);
        }
        setReady(true);
      } else unlisten();
    })().catch((e) => {
      if (!disposed) {
        setError(errorText(e));
        setReady(true);
      }
    });
    return () => {
      disposed = true;
      unlisten?.();
    };
  }, [bridge]);
  const account = snapshot.accounts.find((a) => a.id === accountId);
  useLayoutEffect(() => setRevealed(false), [accountId, setRevealed]);
  useEffect(() => {
    setSku(account?.preset_sku || "MK2M4CH");
    setQuantity(account?.preset_quantity || 2);
  }, [account?.id, account?.preset_sku, account?.preset_quantity]);
  const tasks = snapshot.tasks
    .filter((t) => t.account_id === accountId)
    .sort((a, b) => b.created_at - a.created_at || b.updated_at - a.updated_at);
  const busy = tasks.some(active) || dispatching;
  const waiting = tasks.find((t) => t.status === "waiting");
  const task =
    tasks.find((t) => t.id === taskTarget?.id && active(t)) ||
    waiting ||
    tasks.find(active) ||
    tasks[0];
  const latest = (actions: Action[]) =>
    tasks.find((t) => actions.includes(t.action) && t.status === "succeeded")
      ?.result ?? undefined;
  const person = latest(["me", "enroll"]);
  const activeTasks = snapshot.tasks
    .filter(active)
    .sort((a, b) => a.created_at - b.created_at || a.id.localeCompare(b.id));
  const allActive = activeTasks.length;
  const resumingTask =
    !account?.pending &&
    tasks.some(
      (t) => t.id === taskTarget?.id && active(t) && t.action !== "enroll",
    );
  const viewingGate =
    (!sessionReady && !resumingTask) ||
    (adding && (!taskTarget || taskTarget.action === "enroll"));
  const settingsTarget =
    taskTarget &&
    ["address_read", "address_save", "region"].includes(taskTarget.action)
      ? taskTarget
      : null;
  function openTask(target: Task) {
    setTaskTarget(target);
    setFocusTaskId(target.id);
    if (["address_read", "address_save", "region"].includes(target.action)) {
      navigate("settings");
    } else if (target.account_id === LOCAL_ID && target.action === "catalog") {
      navigate("duo");
    } else {
      setAccountId(target.account_id);
      setTab(
        target.action === "orders"
          ? "orders"
          : ["bag", "checkout"].includes(target.action)
            ? "bag"
            : target.action === "catalog"
              ? "catalog"
              : "home",
      );
      navigate("workspace");
    }
  }
  useEffect(() => {
    if (!focusTaskId) return;
    const frame = requestAnimationFrame(() => {
      const panel = document.getElementById(`task-${focusTaskId}`);
      if (!panel || !panel.getClientRects().length) return;
      panel.scrollIntoView({ block: "center", behavior: "instant" });
      panel.focus({ preventScroll: true });
      setFocusTaskId("");
    });
    return () => cancelAnimationFrame(frame);
  });
  async function start(action: Action, params: Data = {}) {
    if (!bridge || !account) return;
    setError("");
    setDispatching(true);
    try {
      await bridge.start(account.id, action, params);
      const state = await bridge.snapshot();
      setSnapshot(state);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setDispatching(false);
    }
  }
  async function answer(task: Task, value: unknown) {
    if (!bridge || !task.prompt) return;
    const answeredPrompt = task.prompt.id;
    await bridge.reply(task.account_id, task.id, task.prompt.id, value);
    setSnapshot((s) => ({
      ...s,
      tasks: s.tasks.map((t) =>
        t.id === task.id && t.prompt?.id === answeredPrompt
          ? { ...t, prompt: null, status: "running", stage: "继续执行" }
          : t,
      ),
    }));
  }
  const cancel = (t: Task) =>
    bridge?.cancel(t.account_id, t.id).catch((e) => setError(errorText(e)));
  async function refreshSnapshot() {
    if (bridge) setSnapshot(await bridge.snapshot());
  }
  async function removeAccount(target: Account) {
    if (!bridge) return;
    await bridge.remove(target.id);
    if (snapshot.accounts.length === 1) setAccountGateVersion((v) => v + 1);
    const remaining = enabledIds.filter((id) => id !== target.id);
    setSnapshot((s) => ({
      ...s,
      accounts: s.accounts.filter((a) => a.id !== target.id),
      tasks: s.tasks.filter((t) => t.account_id !== target.id),
    }));
    setEnabledIds(remaining);
    if (accountId === target.id) setAccountId(remaining[0] || "");
    if (!remaining.length) {
      setSessionReady(false);
      setAdding(false);
    }
    setTaskTarget((t) => (t?.account_id === target.id ? null : t));
    setFocusTaskId("");
    setError("");
    setRevealed(false);
  }
  const publicPage =
    bridge &&
    (route === "settings" ? (
      <LocalSettings
        key={settingsTarget?.id}
        snapshot={snapshot}
        bridge={bridge}
        refresh={refreshSnapshot}
        initialAccount={settingsTarget?.account_id || accountId || undefined}
      />
    ) : route === "duo" ? (
      <DuoStatus
        snapshot={snapshot}
        bridge={bridge}
        refresh={refreshSnapshot}
      />
    ) : null);
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <span>
            DUO Killer <small>{isLocalWeb ? "本地 Web" : "桌面版"}</small>
          </span>
        </div>
        <RouteNav route={route} navigate={navigate} />
        {sessionReady && (
          <>
            <div className="sidebar-heading">
              <span>我的账号</span>
              <button
                className="icon-button"
                aria-label="添加账号"
                disabled={!bridge}
                onClick={() => {
                  setTaskTarget(null);
                  setAdding(true);
                  navigate("workspace");
                }}
              >
                <Plus size={17} />
              </button>
            </div>
            <nav className="accounts" aria-label="切换账号">
              {snapshot.accounts
                .filter((a) => enabledIds.includes(a.id) && !a.pending)
                .map((a) => {
                  const running = snapshot.tasks.find(
                    (t) => t.account_id === a.id && active(t),
                  );
                  return (
                    <button
                      key={a.id}
                      title={format(`${a.name}（${a.email}）`)}
                      className={`account ${a.id === accountId ? "selected" : ""}`}
                      onClick={() => {
                        setAccountId(a.id);
                        navigate("workspace");
                        setError("");
                      }}
                      aria-current={a.id === accountId ? "true" : undefined}
                    >
                      <span className="avatar">
                        {a.name.slice(0, 1).toUpperCase()}
                      </span>
                      <span className="account-copy">
                        <strong>{format(a.name)}</strong>
                        <small>{format(a.email)}</small>
                      </span>
                      {running && (
                        <span
                          className={`dot ${running.status}`}
                          title={stateNames[running.status]}
                        />
                      )}
                    </button>
                  );
                })}
            </nav>
            <button
              className="text-button select-accounts-button"
              title="重新选择使用账号"
              aria-label="重新选择使用账号"
              disabled={allActive > 0}
              onClick={() => setSessionReady(false)}
            >
              <UserRound size={16} />
              <span>重新选择使用账号</span>
            </button>
          </>
        )}
        {!sessionReady && (
          <p className="sidebar-note">
            添加账号后，在这里切换。
            <br />
            各账号的会话独立保存。
          </p>
        )}
        <div className="sidebar-footer">
          <FolderLock size={16} />
          <div>仅保存在本机</div>
        </div>
      </aside>
      <div className="workspace">
        <header className="topbar">
          <span>
            {route === "settings"
              ? "本地配置"
              : route === "duo"
                ? "DUO 状态"
                : format(account?.name || "账号管理")}
          </span>
          <div className="topbar-tools">
            {(route !== "workspace" || !sessionReady || !account) && (
              <AccountPrivacyToggle />
            )}
            {allActive ? (
              <ActiveTaskNav
                tasks={activeTasks}
                accounts={snapshot.accounts}
                onSelect={openTask}
              />
            ) : (
              <span className="local-badge">本地工作空间</span>
            )}
          </div>
        </header>
        {error && (
          <div className="error-banner" role="alert">
            {format(error)}
            <button
              className="icon-button"
              onClick={() => setError("")}
              aria-label="关闭错误"
            >
              <X size={16} />
            </button>
          </div>
        )}
        {ready && bridge && (!sessionReady || adding) && (
          <div
            className="page-content"
            hidden={route !== "workspace" || !viewingGate}
          >
            <div className="setup-content">
              <AccountGate
                key={accountGateVersion}
                snapshot={snapshot}
                bridge={bridge}
                refresh={refreshSnapshot}
                onRemove={setRemovingAccount}
                onBack={sessionReady ? () => setAdding(false) : undefined}
                onDone={(ids) => {
                  setEnabledIds((old) =>
                    sessionReady ? [...new Set([...old, ...ids])] : ids,
                  );
                  setAccountId(ids[0]);
                  setSessionReady(true);
                  setAdding(false);
                  setTab("home");
                  navigate("workspace");
                  setError("");
                }}
                renderForm={(submit, cancel) => (
                  <AccountForm onSubmit={submit} onCancel={cancel} />
                )}
                renderPrompt={(task, cancel) => (
                  <PromptForm
                    key={task.prompt!.id}
                    prompt={task.prompt!}
                    onReply={(value) => answer(task, value)}
                    onCancel={cancel}
                  />
                )}
              />
            </div>
          </div>
        )}
        {!ready ? (
          <div className="empty">
            <LoaderCircle className="spin" />
            正在读取本地配置
          </div>
        ) : !bridge ? (
          <div className="empty">
            <FolderLock size={36} />
            <h1>请在桌面应用中打开</h1>
            <p>
              浏览器预览不会访问你的账号。启动DUO Killer
              桌面版后，即可管理本机任务。
            </p>
            <code>pnpm desktop</code>
          </div>
        ) : publicPage ? (
          <div className="page-content">{publicPage}</div>
        ) : viewingGate ? null : !account ? (
          <div className="empty">
            <p>当前没有启用的账号，可选择左侧其他账号或自行添加。</p>
            <button
              onClick={() => {
                setAdding(true);
                navigate("workspace");
              }}
            >
              添加账号
            </button>
          </div>
        ) : (
          <>
            <div className="page-heading">
              <div>
                <h1>{format(account.name)}</h1>
                <div className="account-identity">
                  <span>{format(account.email)}</span>
                  <AccountPrivacyToggle />
                </div>
              </div>
              <div className="identity">
                {person?.logged_in ? (
                  <>
                    <span className="status-check">
                      <Check size={13} />
                    </span>
                    {format(person.name || "已确认账号")}
                    <small>上次检查结果</small>
                  </>
                ) : (
                  <>
                    <UserRound size={16} />
                    <span>登录状态待检查</span>
                  </>
                )}
              </div>
            </div>
            <nav className="tabs" aria-label="功能">
              {tabs.map(([id, label, Icon]) => (
                <button
                  key={id}
                  aria-current={tab === id ? "page" : undefined}
                  className={tab === id ? "active" : ""}
                  onClick={() => setTab(id)}
                >
                  <Icon size={16} />
                  {label}
                </button>
              ))}
            </nav>
            <div className="content-grid">
              <main>
                <div className="view-content" key={tab}>
                  {tab === "home" && (
                    <>
                      <section className="section">
                        <div className="section-title">
                          <h2>账号与登录</h2>
                          <span>
                            {latest(["me", "enroll", "login"])
                              ?.device_trust_saved
                              ? "本机设备信任已保存"
                              : "设备验证后自动记住本机"}
                          </span>
                        </div>
                        <p className="muted">
                          登录时优先复用本机设备信任；信任失效或 Apple
                          再次要求验证时，才需输入验证码。首次登录会打开一个小型浏览器窗口。
                        </p>
                        <div className="button-row">
                          <button disabled={busy} onClick={() => start("me")}>
                            <RefreshCw size={15} />
                            检查登录
                          </button>
                          <button
                            disabled={busy}
                            onClick={() => start("login")}
                          >
                            登录
                          </button>
                          <button
                            className="text-button"
                            disabled={busy}
                            onClick={() => start("login", { sms: true })}
                          >
                            使用短信验证
                          </button>
                          <button
                            className="text-button remove-account-button"
                            disabled={busy}
                            title={
                              busy
                                ? "请完成或取消该账号的任务后再移除"
                                : "清除这个账号的本机数据"
                            }
                            onClick={() => setRemovingAccount(account)}
                          >
                            <Trash2 size={15} />
                            移除账号
                          </button>
                        </div>
                      </section>
                      <section className="section">
                        <div className="section-title">
                          <h2>购买商品</h2>
                          <span>按目标总数量更新</span>
                        </div>
                        <form
                          onSubmit={(e) => {
                            e.preventDefault();
                            start("purchase", { sku, quantity });
                          }}
                        >
                          <ProductSelect sku={sku} onChange={setSku} />
                          <div className="buy-fields">
                            <label>
                              数量
                              <input
                                type="number"
                                required
                                min={1}
                                max={999}
                                value={quantity}
                                onChange={(e) =>
                                  setQuantity(Number(e.target.value))
                                }
                              />
                            </label>
                          </div>
                          <p className="muted">
                            {sku === "MLH74FE"
                              ? "抛光布测试：自动送货并填写地址、联系方式，核对商品、数量、金额和地址后直接创建真实待付款订单，展示支付二维码。"
                              : "固定送货，优先使用本地地址和联系方式。白色 256GB、1 台且总额不超过 15,999 元时自动创建订单；型号、数量或金额不符会停止。"}
                          </p>
                          <div className="button-row">
                            <button
                              className="primary"
                              type="submit"
                              disabled={busy || !presetProducts.some((p) => p.sku === sku.replace(/\/A$/i, "")) || quantity < 1}
                            >
                              立即下单
                              <ArrowRight size={16} />
                            </button>
                            <button
                              type="button"
                              disabled={busy}
                              onClick={() => start("login", { warm_only: true })}
                            >
                              预热登录（不加购）
                            </button>
                            <button type="button" disabled={busy} onClick={() => start("login", { renew_device_trust: true })}>
                              重新验证设备（保留账号）
                            </button>
                            <button
                              type="button"
                              disabled={busy || !presetProducts.some((p) => p.sku === sku.replace(/\/A$/i, "")) || quantity !== 1}
                              onClick={() => start("purchase", { sku, quantity, prepare_only: true })}
                            >
                              提前准备（不下单）
                            </button>
                          </div>
                          <p className="muted">商品未开放加购时，可先预热登录，不加购、不下单；结算仍可能要求认证。提前准备需要商品可加购，会填写结算信息但不创建订单。5 分钟内下单优先读取当前结算页并重新核对，不保证库存。</p>
                        </form>
                      </section>
                    </>
                  )}
                  {tab === "bag" && (
                    <section className="section">
                      <div className="section-title">
                        <h2>整个购物袋</h2>
                        <button disabled={busy} onClick={() => start("bag")}>
                          <RefreshCw size={15} />
                          刷新
                        </button>
                      </div>
                      <p className="muted">
                        读取所选账号的实时购物袋，结算时可选择配送或自提。
                      </p>
                      {latest(["bag"]) ? (
                        <ItemTable
                          items={latest(["bag"])!.items || []}
                          total={latest(["bag"])!.total}
                        />
                      ) : (
                        <InlineEmpty text="点击刷新，查看当前账号的购物袋。" />
                      )}
                      <button
                        className="primary"
                        disabled={busy}
                        onClick={() => start("checkout")}
                      >
                        结算购物袋
                        <ArrowRight size={16} />
                      </button>
                    </section>
                  )}
                  {tab === "orders" && (
                    <section className="section">
                      <div className="section-title">
                        <h2>我的订单</h2>
                        <button disabled={busy} onClick={() => start("orders")}>
                          <RefreshCw size={15} />
                          查询订单
                        </button>
                      </div>
                      <p className="muted">
                        支付完成后主动查询，确认 Apple 返回的最新订单状态。
                      </p>
                      {latest(["orders"]) ? (
                        <OrderTable result={latest(["orders"])!} disabled={busy} onCancelOrder={(order_number) => start("cancel_order", { order_number })} />
                      ) : (
                        <InlineEmpty text="点击查询订单，读取官网订单列表。" />
                      )}
                    </section>
                  )}
                  {tab === "catalog" && (
                    <section className="section">
                      <div className="section-title">
                        <h2>预制商品</h2>
                        <button onClick={() => navigate("duo")}>
                          查看 DUO 状态
                        </button>
                      </div>
                      <Catalog
                        result={{
                          items: presetProducts,
                          note: "预设参考价格；实际金额以结账页为准。",
                        }}
                        onSelect={(value) => {
                          setSku(value);
                          setTab("home");
                        }}
                      />
                    </section>
                  )}
                  {tab === "catalog" && (
                    <section className="section">
                      <div className="section-title">
                        <h2>官网商品</h2>
                      </div>
                      <div className="button-row">
                        <button
                          disabled={busy}
                          onClick={() => start("catalog")}
                        >
                          全部推荐
                        </button>
                        <button
                          disabled={busy}
                          onClick={() => start("catalog", { mode: "phone" })}
                        >
                          iPhone
                        </button>
                        <button
                          disabled={busy}
                          onClick={() => start("catalog", { mode: "duo" })}
                        >
                          iPhone Duo
                        </button>
                      </div>
                      <p className="muted">
                        商品与可加购状态来自官网，不代表当前库存。
                      </p>
                      {latest(["catalog"]) ? (
                        <Catalog
                          result={latest(["catalog"])!}
                          onSelect={(value) => {
                            setSku(value);
                            setTab("home");
                          }}
                        />
                      ) : (
                        <InlineEmpty text="选择一个分类，查询商品与 SKU。" />
                      )}
                    </section>
                  )}
                </div>
                {task && (
                  <section
                    className={`task-detail ${task.status}`}
                    id={`task-${task.id}`}
                    tabIndex={-1}
                    key={task.id}
                    data-active={active(task) || undefined}
                    aria-label="当前任务"
                  >
                    <div className="section-title">
                      <h2>{actionNames[task.action]}</h2>
                      <span className={`status ${task.result?.status === "pending_payment" ? "succeeded" : task.status}`}>
                        {active(task) && task.status !== "waiting" && task.result?.status !== "pending_payment" && (
                          <LoaderCircle size={13} className="spin" />
                        )}
                        {task.result?.status === "pending_payment" ? "订单已创建" : stateNames[task.status]}
                      </span>
                    </div>
                    <p className="task-account">{format(account.email)}</p>
                    {task.prompt ? (
                      <PromptForm
                        key={task.prompt.id}
                        prompt={task.prompt}
                        onReply={(value) => answer(task, value)}
                        onCancel={() => cancel(task)}
                      />
                    ) : (
                      <>
                        <p
                          className={
                            task.status === "failed" ? "error-text" : "muted"
                          }
                          role={task.status === "failed" ? "alert" : undefined}
                        >
                          {format(task.stage)}
                        </p>
                        {active(task) && (
                          <button
                            className="operation-cancel"
                            onClick={() => cancel(task)}
                          >
                            <X size={14} />
                            {task.result?.status === "pending_payment" ? "停止加载二维码" : "取消"}
                          </button>
                        )}
                      </>
                    )}
                    {task.result && (task.status === "succeeded" || task.result.status === "pending_payment") && (
                      <Result
                        task={task}
                        onOrders={() => {
                          setTab("orders");
                          start("orders");
                        }}
                        onQr={() => start("qr", { source_task: task.id })}
                        onCancelOrder={() => start("cancel_order", { order_number: task.result!.order_number })}
                        cancelled={snapshot.tasks.some((t) => t.account_id === task.account_id && t.result?.status === "order_cancelled" && t.result?.order_number === task.result!.order_number)}
                        disabled={busy}
                      />
                    )}
                    {task.order_submitted &&
                      ["failed", "cancelled", "interrupted"].includes(
                        task.status,
                      ) && (
                        <button
                          disabled={busy}
                          onClick={() => {
                            setTab("orders");
                            start("orders");
                          }}
                        >
                          查询订单，确认提交结果
                        </button>
                      )}
                  </section>
                )}
              </main>
            </div>
          </>
        )}
        <footer className="workspace-footer">
          <span>DUO Killer · 0.1.0</span>
          <span>会话与地址按账号独立保存</span>
        </footer>
        {removingAccount && (
          <RemoveAccountDialog
            account={removingAccount}
            onRemove={() => removeAccount(removingAccount)}
            onClose={() => setRemovingAccount(null)}
          />
        )}
      </div>
    </div>
  );
}

function AccountForm({
  onSubmit,
  onCancel,
}: {
  onSubmit: (email: string) => Promise<void>;
  onCancel?: () => void;
}) {
  const [email, setEmail] = useState("");
  const [saving, setSaving] = useState(false);
  return (
    <form
      className="account-form"
      onSubmit={async (e) => {
        e.preventDefault();
        setSaving(true);
        await onSubmit(email);
        setSaving(false);
      }}
    >
      <label>
        Apple 账号
        <input
          required
          autoFocus
          type="text"
          autoComplete="username"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          placeholder="邮箱或手机号"
        />
      </label>
      <div className="button-row">
        <button className="primary" disabled={saving} type="submit">
          <Plus size={15} />
          继续登录
        </button>
        {onCancel && (
          <button type="button" className="text-button" onClick={onCancel}>
            取消
          </button>
        )}
      </div>
    </form>
  );
}
function InlineEmpty({ text }: { text: string }) {
  return <p className="inline-empty">{text}</p>;
}
function ItemTable({ items, total }: { items: Data[]; total?: string }) {
  return (
    <>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>商品</th>
              <th>数量</th>
              <th className="number">小计</th>
            </tr>
          </thead>
          <tbody>
            {items.map((item, i) => (
              <tr key={i}>
                <td>
                  {item.name || item.sku || item.part}
                  <small>{item.sku || item.part}</small>
                </td>
                <td>{item.quantity}</td>
                <td className="number">{item.subtotal || "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {!items.length && <InlineEmpty text="购物袋为空。" />}
      </div>
      {total && (
        <div className="total">
          <span>总支付</span>
          <strong>{total}</strong>
        </div>
      )}
    </>
  );
}
function OrderTable({ result, disabled, onCancelOrder }: { result: Data; disabled: boolean; onCancelOrder: (number: string) => void }) {
  return (
    <>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>订单号 / 商品</th>
              <th>数量</th>
              <th>状态 / 配送</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {(result.orders || []).flatMap((order: Data) =>
              (order.items || []).map((item: Data, i: number) => (
                <tr key={order.order_number + "-" + i}>
                  <td>
                    <span className="order-number">{order.order_number}</span>
                    <small>{item.name}</small>
                  </td>
                  <td>{item.quantity}</td>
                  <td>{item.delivery_info || "官网未返回状态"}</td>
                  <td>{i === 0 && <button disabled={disabled} onClick={() => onCancelOrder(order.order_number)}>取消订单 {order.order_number}</button>}</td>
                </tr>
              )),
            )}
          </tbody>
        </table>
      </div>
      <p className="muted">{result.message}</p>
    </>
  );
}
function Catalog({
  result,
  onSelect,
}: {
  result: Data;
  onSelect: (sku: string) => void;
}) {
  const rows = (result.items || []).flatMap((item: Data) =>
    item.skus?.length
      ? item.skus.map((s: Data) => ({ ...s, name: s.name || item.name }))
      : item.sku
        ? [item]
        : [{ ...item, sku: "" }],
  );
  return (
    <>
      <div className="table-wrap catalog-table">
        <table>
          <thead>
            <tr>
              <th>商品 / SKU</th>
              <th>价格</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {rows.map((r: Data, i: number) => (
              <tr key={i}>
                <td>
                  {r.name}
                  <small>{r.sku || r.error || "需在官网选择配置"}</small>
                </td>
                <td>
                  {r.price != null ? `${r.price} ${r.currency || "CNY"}` : "—"}
                  {r.can_add_to_bag === false && <small>未开放加购</small>}
                </td>
                <td>
                  {r.sku && (
                    <button onClick={() => onSelect(r.sku)}>选择</button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="muted">{result.note}</p>
    </>
  );
}
function Result({
  task,
  onOrders,
  onQr,
  onCancelOrder,
  cancelled,
  disabled,
}: {
  task: Task;
  onOrders: () => void;
  onQr: () => void;
  onCancelOrder: () => void;
  cancelled: boolean;
  disabled: boolean;
}) {
  const { format } = useAccountPrivacy();
  const result = task.result!;
  if (cancelled && result.status === "pending_payment") return <p>订单 {result.order_number} 已取消，付款二维码已隐藏。</p>;
  if (task.action === "cancel_order") return <div>
    <p>{result.message}</p>
    <p>订单 {result.order_number}</p>
    {result.detail_url && <a href={result.detail_url} target="_blank" rel="noopener noreferrer">在 Apple 官网查看订单</a>}
    <button disabled={disabled} onClick={onOrders}>查询订单状态</button>
  </div>;
  if (result.status === "pending_payment")
    return (
      <div className="payment">
        <h3>订单已创建，等待付款</h3>
        {typeof result.order_confirmed_elapsed_seconds === "number" && (
          <p className="muted">订单确认耗时：约 {result.order_confirmed_elapsed_seconds} 秒（含排队，不含二维码）。</p>
        )}
        <p>
          订单 {result.order_number || "请查询订单确认"}
          {result.amount_due && ` · ${result.amount_due}`}
        </p>
        {result.items && <ItemTable items={result.items} total={result.order_total} />}
        {result.delivery_address && <p>送货地址：{result.delivery_address}</p>}
        {result.qr_data ? (
          <img
            src={result.qr_data}
            alt={`订单 ${result.order_number || ""} 的支付宝付款二维码`}
            width={208}
            height={208}
          />
        ) : result.qr_loading && task.status === "running" ? (
          <p role="status"><LoaderCircle size={16} className="spin" /> 订单已创建，正在加载支付宝付款二维码…</p>
        ) : (
          <>
            <p>{result.qr_error || "重新获取此订单的付款二维码。"}</p>
            <button disabled={disabled} onClick={onQr}>
              获取二维码
            </button>
          </>
        )}
        <p className="muted">请用支付宝扫码。支付完成后查询订单确认状态。</p>
        <button className="primary" disabled={disabled} onClick={onOrders}>
          <RefreshCw size={15} />
          我已支付，查询订单
        </button>
        <button disabled={disabled || !result.order_number} onClick={onCancelOrder}>
          取消订单
        </button>
      </div>
    );
  if (task.action === "me")
    return (
      <div className="result-line">
        <Check size={16} />
        {result.logged_in
          ? format(
              `${result.name ? result.name + " · " : ""}${result.apple_id || result.email || "账号已登录"}`,
            )
          : result.message || "尚未登录"}
      </div>
    );
  if (task.action === "login")
    return (
      <div className="result-line">
        <Check size={16} />
        {result.warm_login ? result.message : "商城会话已保存，可继续选购。"}
      </div>
    );
  if (task.action === "add")
    return (
      <div className="result-line">
        <Check size={16} />
        {result.part} × {result.items?.[0]?.quantity} · 购物袋总额{" "}
        {result.bag_total}
      </div>
    );
  if (result.message) return <p>{result.message}</p>;
  return null;
}

function PromptForm({
  prompt,
  onReply,
  onCancel,
}: {
  prompt: Prompt;
  onReply: (value: unknown) => Promise<void>;
  onCancel: () => void;
}) {
  const initialSelection =
    prompt.kind === "select"
      ? prompt.options?.findIndex((o) => o.value === prompt.default)
      : -1;
  const { format } = useAccountPrivacy();
  const [value, setValue] = useState(
    prompt.kind === "select"
      ? initialSelection !== undefined && initialSelection >= 0
        ? String(initialSelection)
        : ""
      : String(prompt.default ?? ""),
  );
  const [show, setShow] = useState(false);
  const [remember, setRemember] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const submit = async (answer: unknown) => {
    setBusy(true);
    setError("");
    try {
      await onReply(answer);
    } catch (e) {
      setError(errorText(e));
      setBusy(false);
    }
  };
  const confirmation = prompt.kind === "confirm";
  const password = prompt.kind === "password";
  return (
    <form
      className="prompt-form"
      onSubmit={(e: FormEvent) => {
        e.preventDefault();
        let answer: unknown = value;
        if (confirmation) answer = true;
        else if (password) answer = { password: value, remember };
        else if (prompt.kind === "select")
          answer = prompt.options?.find((_, i) => String(i) === value)?.value;
        submit(answer);
      }}
    >
      <h3>{format(prompt.label)}</h3>
      {confirmation ? (
        <>
          {prompt.details?.operation === "cancel_order" && <p>订单 {prompt.details.order_number}。确认后将向 Apple 提交取消。</p>}
          <ItemTable
            items={prompt.details?.items || []}
            total={prompt.details?.total}
          />
          {prompt.details?.store && <p>自提门店：{prompt.details.store}</p>}
          {prompt.details?.address && <p>送货地址：{prompt.details.address}</p>}
        </>
      ) : prompt.kind === "select" ? (
        <SelectField
          label="选择一个选项"
          autoFocus
          required
          disabled={busy}
          value={value}
          onChange={setValue}
          options={(prompt.options || []).map((o, i) => ({
            value: String(i),
            label: format(o.label),
          }))}
        />
      ) : (
        <>
          <label>
            {password
              ? "密码"
              : prompt.kind === "code"
                ? "六位验证码"
                : "输入内容"}
            <div className="input-with-action">
              <input
                autoFocus
                required={!["text"].includes(prompt.kind)}
                value={value}
                type={password && !show ? "password" : "text"}
                inputMode={prompt.kind === "code" ? "numeric" : undefined}
                pattern={prompt.kind === "code" ? "[0-9]{6}" : undefined}
                maxLength={prompt.kind === "code" ? 6 : undefined}
                autoComplete={
                  password
                    ? "current-password"
                    : prompt.kind === "code"
                      ? "one-time-code"
                      : "off"
                }
                onChange={(e) =>
                  setValue(
                    prompt.kind === "code"
                      ? e.target.value.replace(/[^0-9]/g, "")
                      : e.target.value,
                  )
                }
                spellCheck={false}
              />
              {password && (
                <button
                  type="button"
                  className="icon-button"
                  aria-label={show ? "隐藏密码" : "显示密码"}
                  onClick={() => setShow(!show)}
                >
                  {show ? <EyeOff size={17} /> : <Eye size={17} />}
                </button>
              )}
            </div>
          </label>
          {prompt.kind === "code" && !!prompt.actions?.length && (
            <div className="button-row" role="group" aria-label="切换验证方式">
              {prompt.actions.map((action) => (
                <button
                  key={action.value}
                  type="button"
                  disabled={busy}
                  onClick={() => submit(action.value)}
                >
                  {action.label}
                </button>
              ))}
            </div>
          )}
          {prompt.kind === "code" && (
            <p className="muted">
              验证通过后会保存 Apple
              下发的设备信任，供下次登录复用。六位验证码不会保存。
            </p>
          )}
          {password && (
            <label className="checkbox">
              <input
                type="checkbox"
                checked={remember}
                onChange={(e) => setRemember(e.target.checked)}
              />
              保存到系统凭据保险库
            </label>
          )}
        </>
      )}
      {error && (
        <p className="error-text" role="alert">
          {error}
        </p>
      )}
      <div className="button-row">
        <button
          className="primary"
          disabled={busy || (prompt.kind === "select" && value === "")}
          type="submit"
        >
          {busy ? (
            <LoaderCircle size={15} className="spin" />
          ) : confirmation ? (
            prompt.details?.operation === "cancel_order" ? "确认取消订单" : "确认创建订单"
          ) : (
            "继续"
          )}
        </button>
        <button
          disabled={busy}
          type="button"
          className="operation-cancel"
          onClick={confirmation ? () => submit(false) : onCancel}
        >
          <X size={14} />
          {prompt.details?.operation === "cancel_order" ? "保留订单" : "取消"}
        </button>
      </div>
    </form>
  );
}
