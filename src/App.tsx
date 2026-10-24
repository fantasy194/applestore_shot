import { usePurchaseDraft } from "./usePurchaseDraft";
import { DisclosureSummary } from "./DisclosureSummary";
import { OrderProgressBar } from "./OrderProgressBar";
import { formatTaskStage } from "./taskStage";
import {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type FormEvent,
} from "react";
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
import { useTaskDetails } from "./useTaskDetails";
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
import { NumberField } from "./NumberField";
import { ActiveTaskNav } from "./ActiveTaskNav";
import { SchedulePurchase } from "./SchedulePurchase";
import { InventoryMonitor } from "./InventoryMonitor";
import { PwaAccess, PwaSettingsPage } from "./PwaAccess";
import { PurchaseQueue, logsHref } from "./PurchaseQueue";
import { ActivityLogs } from "./ActivityLogs";
import { RemoteBrowser } from "./RemoteBrowser";
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
  return isLocalWeb ? (
    <PwaAccess>
      <WorkspaceApp />
    </PwaAccess>
  ) : (
    <WorkspaceApp />
  );
}

function WorkspaceApp() {
  const { format, setRevealed } = useAccountPrivacy();
  const [route, navigate] = useRoute(isLocalWeb);
  const [wireSnapshot, setSnapshot] = useState(initial);
  const [ready, setReady] = useState(false);
  const [accountId, setAccountId] = useState("");
  const [enabledIds, setEnabledIds] = useState<string[]>([]);
  const [sessionReady, setSessionReady] = useState(false);
  const [tab, setTab] = useState(() =>
    new URLSearchParams(location.search).get("view") === "orders"
      ? "orders"
      : "home",
  );
  const [adding, setAdding] = useState(false);
  const [error, setError] = useState("");
  const [dispatching, setDispatching] = useState(false);
  const [concurrency, setConcurrency] = useState(1);
  const [retryCount, setRetryCount] = useState(0);
  const [invalidPurchaseNumbers, setInvalidPurchaseNumbers] = useState<
    string[]
  >([]);
  const purchaseNumbersValid = invalidPurchaseNumbers.length === 0;
  function purchaseValidity(key: string, valid: boolean) {
    setInvalidPurchaseNumbers((keys) =>
      valid
        ? keys.includes(key)
          ? keys.filter((item) => item !== key)
          : keys
        : keys.includes(key)
          ? keys
          : [...keys, key],
    );
  }
  const [taskTarget, setTaskTarget] = useState<Task | null>(null);
  const [focusTaskId, setFocusTaskId] = useState("");
  const [removingAccount, setRemovingAccount] = useState<Account | null>(null);
  const [accountGateVersion, setAccountGateVersion] = useState(0);
  const [logTaskId, setLogTaskId] = useState("");
  const bridge = api;
  const wireTasks = wireSnapshot.tasks
    .filter((row) => row.account_id === accountId)
    .sort((a, b) => b.created_at - a.created_at || b.updated_at - a.updated_at);
  const visibleTask =
    wireTasks.find((row) => row.id === taskTarget?.id) ||
    wireTasks.find((row) => row.status === "waiting") ||
    wireTasks.find(active) ||
    wireTasks[0];
  const viewActions: Action[] =
    tab === "orders"
      ? ["orders"]
      : tab === "bag"
        ? ["bag", "clear_bag"]
        : tab === "catalog"
          ? ["catalog"]
          : ["me", "enroll", "login"];
  const viewTask = wireTasks.find(
    (row) => viewActions.includes(row.action) && row.status === "succeeded",
  );
  const duoTask = wireSnapshot.tasks
    .filter((row) => row.account_id === LOCAL_ID && row.action === "catalog")
    .sort((a, b) => b.created_at - a.created_at)[0];
  const wantedDetails =
    route === "workspace"
      ? [visibleTask?.id, viewTask?.id]
      : route === "duo"
        ? [duoTask?.id]
        : [];
  const { snapshot, detailError } = useTaskDetails(
    wireSnapshot,
    bridge,
    wantedDetails.filter((id): id is string => !!id),
    logTaskId,
  );
  useEffect(() => {
    if (!bridge) {
      setReady(true);
      return;
    }
    let disposed = false;
    let unlisten: (() => void) | undefined;
    (async () => {
      let initialSnapshot: Snapshot | undefined;
      unlisten = await bridge.subscribe(
        (task) => {
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
        },
        (state) => {
          initialSnapshot = state;
          if (!disposed) setSnapshot(state);
        },
      );
      // The Web subscription already reads the full snapshot before polling.
      const state = initialSnapshot ?? (await bridge.snapshot());
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
  const { sku, quantity, setSku, setQuantity } = usePurchaseDraft(account);
  const tasks = snapshot.tasks
    .filter((t) => t.account_id === accountId)
    .sort((a, b) => b.created_at - a.created_at || b.updated_at - a.updated_at);
  const busy =
    tasks.some((t) => active(t) && t.status !== "scheduled") || dispatching;
  const scheduledTask = tasks.find((t) => t.status === "scheduled");
  const waiting = tasks.find((t) => t.status === "waiting");
  const task =
    tasks.find((t) => t.id === taskTarget?.id) ||
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
    !!account &&
    !account.pending &&
    tasks.some(
      (t) =>
        t.id === taskTarget?.id &&
        t.action !== "enroll" &&
        (active(t) || ["purchase", "checkout"].includes(t.action)),
    );
  const readOnlyTaskView =
    !sessionReady && resumingTask && !!task && !active(task);
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
    } else if (
      [
        "inventory_monitor",
        "inventory_check",
        "inventory_settings_read",
        "inventory_settings_save",
        "store_search",
      ].includes(target.action)
    ) {
      navigate("monitor");
    } else {
      setAccountId(target.account_id);
      setTab(
        target.action === "orders"
          ? "orders"
          : ["bag", "clear_bag", "checkout"].includes(target.action)
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
    setTaskTarget(null);
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
    ) : route === "monitor" ? (
      <InventoryMonitor
        snapshot={snapshot}
        bridge={bridge}
        refresh={refreshSnapshot}
      />
    ) : route === "queue" ? (
      <PurchaseQueue
        bridge={bridge}
        accounts={snapshot.accounts}
        tasks={snapshot.tasks}
        initialAccountId={accountId}
        onConfigure={(targetAccount) => {
          setTaskTarget(null);
          setFocusTaskId("");
          setAccountId(targetAccount);
          navigate("settings");
        }}
        onTask={async (taskId, targetAccount) => {
          const state = await bridge.snapshot();
          setSnapshot(state);
          const selected = state.tasks.find(
            (task) => task.id === taskId && task.account_id === targetAccount,
          );
          if (selected) openTask(selected);
          else setError("关联任务暂未载入，请刷新队列或查看任务日志。");
        }}
      />
    ) : route === "logs" ? (
      <ActivityLogs bridge={bridge} accounts={snapshot.accounts} />
    ) : route === "access" && isLocalWeb ? (
      <PwaSettingsPage />
    ) : null);
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <span>
            DUO Killer <small>{isLocalWeb ? "本地 Web" : "桌面版"}</small>
          </span>
        </div>
        <RouteNav route={route} navigate={navigate} localWeb={isLocalWeb} />
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
                : route === "monitor"
                  ? "自动监控"
                  : route === "queue"
                    ? "执行队列"
                    : route === "logs"
                      ? "运行日志"
                      : route === "access"
                        ? "应用设置"
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
        {ready && bridge && (
          <OrderProgressBar
            tasks={snapshot.tasks}
            accounts={snapshot.accounts}
            onOpen={openTask}
            onCancel={cancel}
          />
        )}
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
                  setTab(
                    new URLSearchParams(location.search).get("view") ===
                      "orders"
                      ? "orders"
                      : "home",
                  );
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
            {readOnlyTaskView && (
              <div className="local-task">
                <p>正在查看已保存的下单任务。</p>
                <button
                  onClick={() => {
                    setTaskTarget(null);
                    setFocusTaskId("");
                    navigate("workspace");
                  }}
                >
                  选择账号
                </button>
              </div>
            )}
            {!readOnlyTaskView && (
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
            )}
            <div className="content-grid">
              <main>
                <div
                  className="view-content"
                  key={tab}
                  hidden={readOnlyTaskView}
                >
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
                            disabled={busy}
                            onClick={() =>
                              start("login", { browser_login: true })
                            }
                          >
                            官网登录恢复（保留账号）
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
                        <SessionMaintenance bridge={bridge} account={account} />
                      </section>
                      <section className="section">
                        <div className="section-title">
                          <h2>抢购准备</h2>
                          <span>不创建订单</span>
                        </div>
                        <div className="button-row">
                          <button
                            disabled={busy}
                            onClick={() => start("login", { warm_only: true })}
                          >
                            预热登录（不加购）
                          </button>
                          <button
                            disabled={busy}
                            onClick={() =>
                              start("login", { renew_device_trust: true })
                            }
                          >
                            重新验证设备（保留账号）
                          </button>
                          <button
                            disabled={
                              busy ||
                              !presetProducts.some(
                                (p) => p.sku === sku.replace(/\/A$/i, ""),
                              ) ||
                              quantity < 1 ||
                              !purchaseNumbersValid
                            }
                            onClick={() =>
                              start("purchase", {
                                sku,
                                quantity,
                                fulfillment: "HOME",
                                prepare_only: true,
                              })
                            }
                          >
                            提前准备（不下单）
                          </button>
                          <button
                            disabled={
                              busy ||
                              !!scheduledTask ||
                              !presetProducts.some(
                                (p) => p.sku === sku.replace(/\/A$/i, ""),
                              ) ||
                              quantity < 1 ||
                              !purchaseNumbersValid
                            }
                            onClick={() =>
                              start("purchase", {
                                sku,
                                quantity,
                                fulfillment: "RETAIL",
                                concurrency: 1,
                                prepare_only: true,
                                keep_ready: true,
                              })
                            }
                          >
                            提前准备自提（不下单）
                          </button>
                          <button
                            className="danger-button"
                            disabled={busy}
                            onClick={() => start("clear_bag")}
                          >
                            <Trash2 size={15} />
                            清空购物袋
                          </button>
                        </div>
                        <p className="muted">
                          商品未开放加购时，可先预热登录。提前准备会按下方选择的商品和数量检查购物袋、填写送货结算信息，但不创建订单。5
                          分钟内下单会重新核对当前结算页，不保证库存。
                        </p>
                        <p className="muted">
                          自提准备按已保存的门店顺序、时段规则和联系人偏好执行，完成后可在当前任务点击“立即下单（已准备自提）”。准备有效期为
                          5
                          分钟，等待期间该账号保留当前任务；继续时重新核对官网后提交，不保证库存。
                        </p>
                      </section>
                      <section className="section">
                        <div className="section-title">
                          <h2>购买商品</h2>
                          <span>按目标总数量更新</span>
                        </div>
                        <form
                          onSubmit={(e) => {
                            e.preventDefault();
                            const fulfillment =
                              (
                                e.nativeEvent as SubmitEvent
                              ).submitter?.getAttribute("data-fulfillment") ===
                              "RETAIL"
                                ? "RETAIL"
                                : "HOME";
                            start("purchase", {
                              sku,
                              quantity,
                              fulfillment,
                              retry_count: retryCount,
                              ...(fulfillment === "RETAIL"
                                ? { concurrency }
                                : {}),
                            });
                          }}
                        >
                          <ProductSelect sku={sku} onChange={setSku} />
                          <div className="purchase-number-grid">
                            <NumberField
                              label="数量"
                              min={1}
                              max={999}
                              required
                              value={quantity}
                              onChange={setQuantity}
                              onValidityChange={(valid) =>
                                purchaseValidity("quantity", valid)
                              }
                            />
                            <NumberField
                              label="自提并发路数"
                              min={1}
                              max={10}
                              required
                              disabled={busy}
                              value={concurrency}
                              onChange={setConcurrency}
                              onValidityChange={(valid) =>
                                purchaseValidity("concurrency", valid)
                              }
                              hint={
                                <>
                                  默认 1 路，每路购买 {quantity}{" "}
                                  件，最多可能生成{" "}
                                  {Number.isInteger(concurrency)
                                    ? concurrency
                                    : 0}{" "}
                                  笔订单。送货使用单路。
                                </>
                              }
                            />
                            <NumberField
                              label="下单失败重试次数"
                              min={0}
                              max={10}
                              required
                              disabled={busy}
                              value={retryCount}
                              onChange={setRetryCount}
                              onValidityChange={(valid) =>
                                purchaseValidity("retry", valid)
                              }
                              hint={
                                <>
                                  默认 0 次额外重试，最多共{" "}
                                  {Number.isInteger(retryCount)
                                    ? retryCount + 1
                                    : 1}{" "}
                                  轮。确认未提交的失败才串行重试，已有订单或提交未知时停止。
                                </>
                              }
                            />
                          </div>
                          <p className="muted">
                            {sku === "MLH74FE"
                              ? "抛光布测试：可选择送货或自提。核对商品、数量、金额及地址或门店后自动创建真实待付款订单，展示支付二维码。"
                              : "立即下单使用本地送货地址；立即下单（自提）使用本地预设门店和取货联系人。按本次选择的商品和数量自动创建待付款订单，金额以官网结算为准；不加 AppleCare+。"}
                          </p>
                          <div className="button-row">
                            <button
                              className="primary"
                              type="submit"
                              disabled={
                                busy ||
                                !!scheduledTask ||
                                !presetProducts.some(
                                  (p) => p.sku === sku.replace(/\/A$/i, ""),
                                ) ||
                                quantity < 1 ||
                                !purchaseNumbersValid
                              }
                            >
                              立即下单
                              <ArrowRight size={16} />
                            </button>
                            <button
                              className="primary"
                              type="submit"
                              data-fulfillment="RETAIL"
                              disabled={
                                busy ||
                                !!scheduledTask ||
                                !presetProducts.some(
                                  (p) => p.sku === sku.replace(/\/A$/i, ""),
                                ) ||
                                quantity < 1 ||
                                !purchaseNumbersValid
                              }
                            >
                              立即下单（自提）
                              <ArrowRight size={16} />
                            </button>
                          </div>
                        </form>
                      </section>
                      <SchedulePurchase
                        key={account.id}
                        sku={sku}
                        quantity={quantity}
                        busy={busy || !purchaseNumbersValid}
                        concurrency={concurrency}
                        retryCount={retryCount}
                        task={scheduledTask}
                        start={start}
                        cancel={cancel}
                      />
                      <section className="section">
                        <div className="section-title">
                          <h2>库存联动抢购</h2>
                        </div>
                        <p className="muted">
                          可同时监控多家门店，有货时立即使用所选账号抢购。人工下单和定时抢购仍可单独使用。
                        </p>
                        <button onClick={() => navigate("monitor")}>
                          配置自动监控
                        </button>
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
                      {latest(["bag", "clear_bag"]) ? (
                        <ItemTable
                          items={latest(["bag", "clear_bag"])!.items || []}
                          total={latest(["bag", "clear_bag"])!.total}
                        />
                      ) : (
                        <InlineEmpty text="点击刷新，查看当前账号的购物袋。" />
                      )}
                      <div className="button-row">
                        <button
                          className="primary"
                          disabled={busy || !!scheduledTask}
                          onClick={() => start("checkout")}
                        >
                          结算购物袋
                          <ArrowRight size={16} />
                        </button>
                        <button
                          className="danger-button"
                          disabled={busy}
                          onClick={() => start("clear_bag")}
                        >
                          <Trash2 size={15} />
                          清空购物袋
                        </button>
                      </div>
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
                        <OrderTable
                          result={latest(["orders"])!}
                          disabled={busy}
                          onCancelOrder={(order_number) =>
                            start("cancel_order", { order_number })
                          }
                        />
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
                      <span
                        className={`status ${["cancel_order", "clear_bag"].includes(task.action) && task.result?.success === false ? "waiting" : task.result?.status === "pending_payment" ? "succeeded" : task.status}`}
                      >
                        {active(task) &&
                          task.status !== "scheduled" &&
                          task.status !== "waiting" &&
                          task.result?.status !== "pending_payment" && (
                            <LoaderCircle size={13} className="spin" />
                          )}
                        {task.result?.status === "order_expired"
                          ? "订单已过期"
                          : task.action === "cancel_order" &&
                              task.result?.success === false
                            ? "尚未取消"
                            : task.action === "clear_bag" &&
                                task.result?.success === false
                              ? "尚未清空"
                              : task.result?.status === "waiting_stock" &&
                                  task.result?.preparation_stage
                                ? "已预备，等待库存"
                                : task.result?.status === "prepared"
                                  ? "准备完成，未下单"
                                  : task.result?.status === "pending_payment"
                                    ? "已取得订单号"
                                    : stateNames[task.status]}
                      </span>
                    </div>
                    <p className="task-account">{format(account.email)}</p>
                    <RemoteBrowser bridge={bridge} task={task} />
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
                          {task.action === "cancel_order" &&
                          task.result?.success === false
                            ? "未确认取消"
                            : task.action === "clear_bag" &&
                                task.result?.success === false
                              ? "尚未清空"
                              : format(
                                  ["purchase", "checkout"].includes(task.action)
                                    ? formatTaskStage(task.stage)
                                    : task.stage,
                                )}
                        </p>
                        {active(task) && (
                          <button
                            className="operation-cancel"
                            onClick={() => cancel(task)}
                          >
                            <X size={14} />
                            {task.result?.status === "pending_payment"
                              ? "停止加载二维码"
                              : "取消"}
                          </button>
                        )}
                      </>
                    )}
                    <PurchaseRetrySummary result={task.result} />
                    {task.is_summary && (
                      <p className="muted" role="status">
                        {detailError.id === task.id
                          ? detailError.message
                          : "正在读取任务详情…"}
                      </p>
                    )}
                    <details
                      className="task-log-details"
                      onToggle={(event) =>
                        setLogTaskId(event.currentTarget.open ? task.id : "")
                      }
                    >
                      <DisclosureSummary>
                        当前任务日志（{task.log_count ?? task.logs?.length ?? 0}{" "}
                        条）
                      </DisclosureSummary>
                      {task.logs?.length ? (
                        <pre>
                          {task.logs.map((line) => format(line)).join("\n")}
                        </pre>
                      ) : (
                        <p className="muted">
                          {task.log_count
                            ? "正在读取此任务日志…"
                            : "此任务尚无日志。"}
                        </p>
                      )}
                      <a
                        className="ui-action-link"
                        href={logsHref({
                          account_id: task.account_id,
                          task_id: task.id,
                        })}
                      >
                        查看此任务完整日志
                      </a>
                    </details>
                    {task.result &&
                      (task.status === "succeeded" ||
                        [
                          "pending_payment",
                          "parallel_running",
                          "parallel_complete",
                          "queue_running",
                          "queue_complete",
                        ].includes(task.result.status)) && (
                        <Result
                          task={task}
                          onOrders={() => {
                            setTab("orders");
                            start("orders");
                          }}
                          onQr={(sourceTask) =>
                            start("qr", { source_task: sourceTask || task.id })
                          }
                          onCancelOrder={(orderNumber) =>
                            start("cancel_order", {
                              order_number:
                                orderNumber || task.result!.order_number,
                            })
                          }
                          terminalStatus={confirmedOrderTerminal(
                            snapshot.tasks,
                            task.account_id,
                            task.result!.order_number,
                          )}
                          terminalStatusFor={(number) =>
                            confirmedOrderTerminal(
                              snapshot.tasks,
                              task.account_id,
                              number,
                            )
                          }
                          disabled={busy || readOnlyTaskView}
                        />
                      )}
                    {task.order_submitted &&
                      ["failed", "cancelled", "interrupted"].includes(
                        task.status,
                      ) && (
                        <button
                          disabled={busy || readOnlyTaskView}
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
          <span>DUO Killer · 0.1.6</span>
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
function OrderTable({
  result,
  disabled,
  onCancelOrder,
}: {
  result: Data;
  disabled: boolean;
  onCancelOrder: (number: string) => void;
}) {
  return (
    <>
      <div className="table-wrap order-table">
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
                    <span className="order-number">
                      {item.detail_url ? (
                        <a
                          href={item.detail_url}
                          target="_blank"
                          rel="noopener noreferrer"
                        >
                          {order.order_number}
                        </a>
                      ) : (
                        order.order_number
                      )}
                    </span>
                    <small>{item.name}</small>
                  </td>
                  <td>{item.quantity}</td>
                  <td>{item.delivery_info || "官网未返回状态"}</td>
                  <td>
                    {i === 0 &&
                      !order.items.every((entry: Data) =>
                        /^(?:PAYMENT_EXPIRED_STORED_ORDER|CANCEL(?:ED|LED)(?:_[A-Z_]+)?)$/.test(
                          entry.status || "",
                        ),
                      ) && (
                        <button
                          disabled={disabled}
                          onClick={() => onCancelOrder(order.order_number)}
                        >
                          取消订单 {order.order_number}
                        </button>
                      )}
                  </td>
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
function SessionMaintenance({
  bridge,
  account,
}: {
  bridge: NonNullable<typeof api>;
  account: Account;
}) {
  const [state, setState] = useState<Data | null>(null);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [now, setNow] = useState(Date.now());
  const [readAt, setReadAt] = useState(Date.now());
  const version = useRef(0);
  const configuring = useRef(false);
  async function configure(patch: Data) {
    if (!bridge.maintenance) return;
    const expected = ++version.current;
    configuring.current = true;
    setSaving(true);
    setError("");
    try {
      const result = await bridge.maintenance("configure", {
        ...patch,
        interval_minutes: 10,
      });
      if (expected === version.current) {
        setState(result);
        setReadAt(Date.now());
      }
    } catch (error) {
      if (expected === version.current) setError(errorText(error));
    } finally {
      if (expected === version.current) {
        configuring.current = false;
        setSaving(false);
      }
    }
  }
  useEffect(() => {
    if (!bridge.maintenance) return;
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      const expected = version.current;
      try {
        const result = await bridge.maintenance!("status");
        if (!disposed && expected === version.current && !configuring.current) {
          setState(result);
          setReadAt(Date.now());
          setError("");
        }
      } catch (error) {
        if (!disposed) setError(errorText(error));
      } finally {
        if (!disposed) timer = setTimeout(poll, 10_000);
      }
    }
    const clock = setInterval(() => setNow(Date.now()), 1000);
    void poll();
    return () => {
      disposed = true;
      clearTimeout(timer);
      clearInterval(clock);
    };
  }, [bridge]);
  if (!bridge.maintenance) return null;
  const current = Array.isArray(state?.last_runs)
    ? state.last_runs.find((run: Data) => run.account_id === account.id)
    : state?.last_runs?.[account.id];
  const flow =
    state?.flow === "pickup_prepare" || state?.flow === "browser_login"
      ? state.flow
      : "login";
  const runningFlow = current?.flow || flow;
  const reason =
    current?.blocked_reason ||
    (current?.requires_login
      ? "requires_login"
      : current?.busy
        ? "account_busy"
        : "");
  const remaining = Number.isFinite(current?.remaining_seconds)
    ? Math.max(
        0,
        current.remaining_seconds -
          Math.floor(Math.max(0, now - readAt) / 1000),
      )
    : current?.next_check_at_ms
      ? Math.max(0, Math.ceil((current.next_check_at_ms - now) / 1000))
      : null;
  const status = !state?.enabled
    ? "空闲预热已关闭"
    : current?.status === "running"
      ? runningFlow === "pickup_prepare"
        ? "正在准备自提，不下单"
        : runningFlow === "browser_login"
          ? "正在恢复官网登录，不加购"
          : "正在预热登录，不加购"
      : reason === "account_busy"
        ? "下单/账号任务进行中，暂缓预热"
        : reason === "order_unresolved"
          ? "订单结果待确认，暂缓预热"
          : reason === "requires_login"
            ? "需要官网登录恢复，暂缓预热"
            : remaining === 0
              ? "空闲时间已满足，等待预热"
              : remaining !== null
                ? `距离预热 ${Math.floor(remaining / 60)}:${String(remaining % 60).padStart(2, "0")}`
                : "等待账号空闲";
  return (
    <section
      className="ui-panel session-maintenance"
      aria-labelledby="session-warm-heading"
    >
      <div className="ui-panel-heading">
        <h3 id="session-warm-heading">空闲预热</h3>
        <span className="session-warm-status" role="status">
          {status}
        </span>
      </div>
      <label className="checkbox-label">
        <input
          type="checkbox"
          disabled={saving || !state}
          checked={state?.enabled === true}
          onChange={(event) =>
            void configure({ enabled: event.target.checked })
          }
        />
        下单空闲10分钟后自动预热
      </label>
      <SelectField
        className="session-warm-flow"
        label="预热流程"
        value={flow}
        disabled={saving || !state}
        options={[
          { value: "login", label: "预热登录（不加购）" },
          { value: "pickup_prepare", label: "提前准备自提（不下单）" },
          { value: "browser_login", label: "官网登录恢复（保留账号）" },
        ]}
        onChange={(value) => void configure({ flow: value })}
      />
      <p className="ui-caption">
        {flow === "pickup_prepare"
          ? "按各账号的商品、数量和自提偏好准备结算；可能加购，不提交订单。浏览器按需无头运行。"
          : flow === "browser_login"
            ? "保留账号和浏览器登录资料，使用现有凭证恢复官网登录；不加购、不下单。浏览器按需无头运行。"
            : "检查账号会话，不加购。浏览器按需无头运行。"}
      </p>
      <p className="session-warm-timing">
        最近预热：
        {current?.last_warm_at_ms
          ? new Date(current.last_warm_at_ms).toLocaleString("zh-CN")
          : "尚未预热"}
      </p>
      {current?.message && (
        <p className="muted session-warm-result">最近结果：{current.message}</p>
      )}
      {current?.status === "waiting_stock" && (
        <p className="muted">已完成可用准备，仍需等待有货门店与时段确认。</p>
      )}
      {error && (
        <p role="alert" className="error-text">
          {error}
        </p>
      )}
      <details className="ui-disclosure">
        <DisclosureSummary>预热说明与会话日志</DisclosureSummary>
        <p>
          账号连续10分钟没有下单操作且空闲后，执行所选流程。切换流程重新计时，正在执行的预热按原流程完成。下单优先；账号忙碌或订单结果待确认时暂缓。
        </p>
        <p>
          登录预热不清空购物袋、不加购。自提准备使用本账号预设，不提交订单；准备记录最多复用5分钟，下单时仍会复核商品、库存、门店和时段。
        </p>
        <p>
          官网登录恢复保留账号和浏览器资料，使用可用凭证完成普通登录。自动预热不弹出可见浏览器；缺少密码、需要验证码或官网验证时暂停并提示处理。手动登录和官网登录恢复仍打开可见窗口。
        </p>
        <a
          className="ui-action-link"
          href={logsHref({
            account_id: account.id,
            category: "authentication",
          })}
        >
          查看会话日志
        </a>
      </details>
    </section>
  );
}

function PurchaseRetrySummary({ result }: { result: Data | null }) {
  const { retry_count, attempts_made, retries_used } =
    result?.retry_summary || {};
  if (
    ![retry_count, attempts_made, retries_used].every(Number.isInteger) ||
    retry_count < 1 ||
    retry_count > 10 ||
    retries_used < 0 ||
    retries_used > retry_count ||
    attempts_made !== retries_used + 1
  )
    return null;
  return (
    <p className="muted purchase-retry-summary">
      已执行 {attempts_made} 轮下单，额外重试 {retries_used} 次（最多{" "}
      {retry_count} 次）
    </p>
  );
}

function confirmedOrderTerminal(
  tasks: Task[],
  accountId: string,
  number: string,
): "cancelled" | "expired" | undefined {
  if (typeof number !== "string" || !number) return;
  for (const task of tasks) {
    if (
      task.account_id !== accountId ||
      !task.result ||
      task.result.success === false
    )
      continue;
    if (task.result?.order_number === number) {
      if (task.result.status === "order_cancelled") return "cancelled";
      if (task.result.status === "order_expired") return "expired";
    }
    if (task.action === "orders") {
      const order = task.result?.orders?.find(
        (o: Data) =>
          o.order_number === number && o.verified_by === "official_detail",
      );
      if (
        order?.items?.length &&
        order.items.every(
          (item: Data) => item.status === "PAYMENT_EXPIRED_STORED_ORDER",
        )
      )
        return "expired";
      if (
        order?.items?.length &&
        order.items.every((item: Data) =>
          /^CANCEL(?:ED|LED)(?:_[A-Z_]+)?$/.test(item.status || ""),
        )
      )
        return "cancelled";
    }
  }
}
function Result({
  task,
  onOrders,
  onQr,
  onCancelOrder,
  terminalStatus,
  terminalStatusFor,
  disabled,
}: {
  task: Task;
  onOrders: () => void;
  onQr: (sourceTask?: string) => void;
  onCancelOrder: (orderNumber?: string) => void;
  terminalStatus?: "cancelled" | "expired";
  terminalStatusFor?: (
    orderNumber: string,
  ) => "cancelled" | "expired" | undefined;
  disabled: boolean;
}) {
  const { format } = useAccountPrivacy();
  const result = task.result!;
  if (["queue_running", "queue_complete"].includes(result.status)) {
    const jobs: Data[] = Array.isArray(result.queue_jobs)
      ? result.queue_jobs
      : [];
    const rootOrders: Data[] = Array.isArray(result.pending_orders)
      ? result.pending_orders
      : [];
    const rootAttempts: Data[] = Array.isArray(result.attempts)
      ? result.attempts
      : [];
    const jobStates: Record<string, string> = {
      queued: "等待执行",
      running: "执行中",
      purchase_retry: "正在重试",
      parallel_running: "执行中",
      parallel_complete: "已返回结果",
      pending_payment: "待付款",
      failed: "失败",
      submission_unknown: "提交结果未知",
      cancelled: "已取消",
      paused: "已暂停",
      interrupted: "已中断",
      succeeded: "已完成",
    };
    const unknown =
      jobs.some(
        (job) =>
          job.status === "submission_unknown" ||
          job.result?.status === "submission_unknown" ||
          job.result?.attempts?.some(
            (attempt: Data) => attempt.status === "submission_unknown",
          ),
      ) ||
      rootAttempts.some((attempt) => attempt.status === "submission_unknown");
    const hasPending =
      rootOrders.length > 0 ||
      jobs.some(
        (job) =>
          job.result?.status === "pending_payment" ||
          job.result?.pending_orders?.length,
      );
    return (
      <div className="queue-results">
        <h3>
          {result.status === "queue_running" && active(task)
            ? "队列下单进行中"
            : "队列下单结果"}
        </h3>
        {result.message ? (
          <p>{format(result.message)}</p>
        ) : result.status === "queue_complete" && !hasPending ? (
          <p className={unknown ? "error-text" : "muted"}>
            {unknown
              ? "存在提交结果未知的目标，请先查询订单确认。"
              : "本批未取得待付款订单，请查看各目标结果。"}
          </p>
        ) : null}
        {jobs.map((job, jobIndex) => {
          const id = job.queue_job_id || job.id;
          const detail: Data = job.result || {};
          const matchingOrders = rootOrders.filter(
            (order) => order.queue_job_id === id,
          );
          const candidates: Data[] = matchingOrders.length
            ? matchingOrders
            : Array.isArray(detail.pending_orders)
              ? detail.pending_orders
              : detail.status === "pending_payment"
                ? [detail]
                : [];
          const orders = candidates.filter(
            (order, index) =>
              candidates.findIndex(
                (row) => row.order_number === order.order_number,
              ) === index,
          );
          const flatAttempts = rootAttempts.filter(
            (attempt) => attempt.queue_job_id === id,
          );
          const attempts: Data[] = flatAttempts.length
            ? flatAttempts
            : Array.isArray(detail.attempts)
              ? detail.attempts
              : [];
          const actualStores = [
            ...new Set(
              [
                detail.pickup_store,
                ...orders.map((order) => order.pickup_store),
                ...attempts.map((attempt) => attempt.result?.pickup_store),
              ].filter(Boolean),
            ),
          ];
          const policy = job.pickup_policy || job.target?.pickup_policy;
          const store =
            actualStores.join(" / ") ||
            (policy?.selection_mode === "priority"
              ? "候选门店择一"
              : job.store_name ||
                job.pickup_store_name ||
                job.target?.pickup_store_name);
          const actualStoreId =
            detail.pickup_store_id ||
            orders.find((order) => order.pickup_store_id)?.pickup_store_id;
          return (
            <section
              className="parallel-attempt queue-job-result"
              key={id || jobIndex}
              data-queue-job-id={id}
            >
              <h4>
                {store || `队列目标 ${id || jobIndex + 1}`} ·{" "}
                {jobStates[job.status || detail.status] ||
                  (job.started ? "处理中" : "等待执行")}
              </h4>
              {job.sku && (
                <p className="muted">
                  {job.sku}
                  {actualStoreId
                    ? ` · ${actualStoreId}`
                    : job.store_id && policy?.selection_mode !== "priority"
                      ? ` · ${job.store_id}`
                      : ""}
                </p>
              )}
              {policy?.selection_mode === "priority" && (
                <p className="muted">
                  预选顺序：
                  {policy.stores
                    ?.map((candidate: Data) => candidate.store_name)
                    .join(" → ")}
                </p>
              )}
              {detail.message && (
                <p
                  className={detail.success === false ? "error-text" : "muted"}
                >
                  {format(detail.message)}
                </p>
              )}
              {detail.error && detail.error !== detail.message && (
                <p className="error-text">{format(detail.error)}</p>
              )}
              {!result.retry_summary && (
                <PurchaseRetrySummary result={detail} />
              )}
              {attempts.map((attempt, index) => (
                <div
                  className="queue-attempt-status"
                  key={attempt.task_id || attempt.id || index}
                >
                  <p
                    className={
                      attempt.status === "failed" ||
                      attempt.status === "submission_unknown"
                        ? "error-text"
                        : "muted"
                    }
                  >
                    第 {attempt.attempt || index + 1} 路 ·{" "}
                    {jobStates[attempt.status] || "处理中"}
                    {attempt.result?.order_number
                      ? ` · ${attempt.result.order_number}`
                      : ""}
                  </p>
                  {(attempt.error || attempt.result?.message) && (
                    <p className="muted">
                      {format(attempt.error || attempt.result?.message)}
                    </p>
                  )}
                </div>
              ))}
              {orders.map((order, index) => {
                const source =
                  order.task_id ||
                  attempts.find(
                    (attempt) =>
                      attempt.result?.order_number === order.order_number,
                  )?.task_id ||
                  job.task_id;
                return (
                  <Result
                    key={order.order_number || source || index}
                    task={{ ...task, id: source || task.id, result: order }}
                    onOrders={onOrders}
                    onQr={() => onQr(source)}
                    onCancelOrder={() => onCancelOrder(order.order_number)}
                    terminalStatus={terminalStatusFor?.(order.order_number)}
                    disabled={disabled || !source}
                  />
                );
              })}
              {!orders.length &&
                !attempts.length &&
                !detail.message &&
                !detail.error && (
                  <p className="muted">
                    {job.status === "failed" || detail.success === false
                      ? "此目标下单未完成，未取得待付款订单。"
                      : job.status === "submission_unknown"
                        ? "订单提交结果未知，请查询订单确认。"
                        : "等待此目标的执行结果。"}
                  </p>
                )}
            </section>
          );
        })}
        <button disabled={disabled} onClick={onOrders}>
          查询全部订单
        </button>
      </div>
    );
  }
  if (
    ["parallel_running", "parallel_complete"].includes(result.status) &&
    Array.isArray(result.attempts)
  )
    return (
      <div className="parallel-results">
        <h3>
          {result.status === "parallel_running"
            ? "正在并发自提下单"
            : "并发自提结果"}{" "}
          · {result.concurrency} 路
        </h3>
        <p className="muted">
          每路独立显示结果。订单提交结果未知时请先查询订单，再决定是否重新抢购。
        </p>
        {result.attempts.map((attempt: Data) => (
          <section
            className="parallel-attempt"
            key={attempt.task_id || attempt.id || attempt.attempt}
          >
            <h4>
              第 {attempt.attempt} 路 ·{" "}
              {attempt.status === "pending_payment"
                ? "待付款"
                : attempt.status === "failed"
                  ? "失败"
                  : attempt.status === "cancelled"
                    ? "已取消"
                    : attempt.status === "submission_unknown"
                      ? "提交结果未知"
                      : "处理中"}
            </h4>
            {attempt.result?.status === "pending_payment" ? (
              <Result
                task={{
                  ...task,
                  id: attempt.task_id || task.id,
                  result: attempt.result,
                }}
                disabled={disabled}
                onOrders={onOrders}
                onQr={() => onQr(attempt.task_id)}
                onCancelOrder={() => onCancelOrder(attempt.result.order_number)}
                terminalStatus={terminalStatusFor?.(
                  attempt.result.order_number,
                )}
              />
            ) : (
              <p
                className={
                  attempt.status === "failed" ||
                  attempt.status === "submission_unknown"
                    ? "error-text"
                    : "muted"
                }
              >
                {format(
                  attempt.error ||
                    attempt.result?.message ||
                    "等待此路执行结果",
                )}
              </p>
            )}
          </section>
        ))}
        <button disabled={disabled} onClick={onOrders}>
          查询全部订单
        </button>
      </div>
    );
  if (terminalStatus && result.status === "pending_payment")
    return (
      <p>
        订单 {result.order_number}{" "}
        {terminalStatus === "expired" ? "已过期" : "已取消"}，付款二维码已隐藏。
      </p>
    );
  if (task.action === "cancel_order")
    return (
      <div>
        <p>{result.message}</p>
        <p>订单 {result.order_number}</p>
        {result.detail_url && (
          <a href={result.detail_url} target="_blank" rel="noopener noreferrer">
            在 Apple 官网查看订单
          </a>
        )}
        <button disabled={disabled} onClick={onOrders}>
          查询订单状态
        </button>
      </div>
    );
  if (result.status === "pending_payment")
    return (
      <div className="payment">
        <h3>苹果已返回订单号，等待付款</h3>
        <p className="muted">
          订单号来自结算结果；账号订单列表中的状态与归属尚未核实。
        </p>
        {typeof result.order_confirmed_elapsed_seconds === "number" && (
          <p className="muted">
            订单确认耗时：约 {result.order_confirmed_elapsed_seconds}{" "}
            秒（含排队，不含二维码）。
          </p>
        )}
        <p>
          订单 {result.order_number || "请查询订单确认"}
          {result.amount_due && ` · ${result.amount_due}`}
        </p>
        {result.items && (
          <ItemTable items={result.items} total={result.order_total} />
        )}
        {result.delivery_address && <p>送货地址：{result.delivery_address}</p>}
        {result.pickup_store && <p>自提门店：{result.pickup_store}</p>}
        {result.pickup_time_slot?.date && (
          <p>
            取货时段：{result.pickup_time_slot.date}{" "}
            {result.pickup_time_slot.start}–{result.pickup_time_slot.end}
          </p>
        )}
        {result.qr_data ? (
          <img
            src={result.qr_data}
            alt={`订单 ${result.order_number || ""} 的支付宝付款二维码`}
            width={208}
            height={208}
          />
        ) : result.qr_loading && task.status === "running" ? (
          <p role="status">
            <LoaderCircle size={16} className="spin" />{" "}
            订单已创建，正在加载支付宝付款二维码…
          </p>
        ) : (
          <>
            <p>{result.qr_error || "重新获取此订单的付款二维码。"}</p>
            <button disabled={disabled} onClick={() => onQr()}>
              获取二维码
            </button>
          </>
        )}
        <p className="muted">请用支付宝扫码。支付完成后查询订单确认状态。</p>
        <button className="primary" disabled={disabled} onClick={onOrders}>
          <RefreshCw size={15} />
          我已支付，查询订单
        </button>
        <button
          disabled={disabled || !result.order_number}
          onClick={() => onCancelOrder()}
        >
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
  const preparedPickup =
    confirmation && prompt.details?.operation === "prepared_pickup";
  const preparedExpiry =
    preparedPickup &&
    typeof prompt.details?.expires_at === "number" &&
    Number.isFinite(prompt.details.expires_at)
      ? new Date(prompt.details.expires_at * 1000).toLocaleTimeString("zh-CN", {
          hour12: false,
        })
      : undefined;
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
          {prompt.details?.operation === "cancel_order" && (
            <p>
              订单 {prompt.details.order_number}。确认后将向 Apple 提交取消。
            </p>
          )}
          {prompt.details?.operation === "clear_bag" && (
            <p>确认后仅移除下列当前购物袋商品，不取消已有订单。</p>
          )}
          {preparedPickup && (
            <p>
              自提准备完成，尚未创建订单。
              {preparedExpiry
                ? `准备有效至 ${preparedExpiry}。`
                : "准备有效期为 5 分钟。"}
              等待期间该账号保留当前任务，继续时重新核对官网后提交。
            </p>
          )}
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
            <>
              <p className="muted">
                密码按账号在本次服务运行中复用。保存由系统凭据保险库决定；容器未配置可用保险库时，重启后需重新输入。
              </p>
              <label className="checkbox">
                <input
                  type="checkbox"
                  checked={remember}
                  onChange={(e) => setRemember(e.target.checked)}
                />
                保存到系统凭据保险库
              </label>
            </>
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
            preparedPickup ? (
              "立即下单（已准备自提）"
            ) : prompt.details?.operation === "clear_bag" ? (
              "确认清空购物袋"
            ) : prompt.details?.operation === "cancel_order" ? (
              "确认取消订单"
            ) : (
              "确认创建订单"
            )
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
          {preparedPickup
            ? "结束准备"
            : prompt.details?.operation === "clear_bag"
              ? "保留购物袋"
              : prompt.details?.operation === "cancel_order"
                ? "保留订单"
                : "取消"}
        </button>
      </div>
    </form>
  );
}
