import { useEffect, useRef, useState, type ReactNode } from "react";
import { Check, LoaderCircle, Plus, Trash2, X } from "lucide-react";
import { useAccountPrivacy } from "./AccountPrivacy";
import type { Bridge } from "./api";
import {
  active,
  type Account,
  type Action,
  type Snapshot,
  type Task,
} from "./types";

type Outcome = { ok: boolean; message: string };
type Props = {
  snapshot: Snapshot;
  bridge: Bridge;
  refresh: () => Promise<void>;
  onDone: (ids: string[]) => void;
  onBack?: () => void;
  onRemove: (account: Account) => void;
  renderPrompt: (task: Task, cancel: () => void) => ReactNode;
  renderForm: (
    submit: (email: string) => Promise<void>,
    cancel?: () => void,
  ) => ReactNode;
};

export function AccountGate({
  snapshot,
  bridge,
  refresh,
  onDone,
  onBack,
  onRemove,
  renderPrompt,
  renderForm,
}: Props) {
  const { format } = useAccountPrivacy();
  const [selected, setSelected] = useState<string[]>([]);
  const [hadAccounts] = useState(snapshot.accounts.length > 0);
  const [outcomes, setOutcomes] = useState<Record<string, Outcome>>({});
  const [adding, setAdding] = useState(Boolean(onBack));
  const [running, setRunning] = useState(false);
  const [currentId, setCurrentId] = useState("");
  const [currentAccount, setCurrentAccount] = useState("");
  const [error, setError] = useState("");
  const stopped = useRef(false);
  const mounted = useRef(true);
  const waiter = useRef<{
    id: string;
    resolve: (task: Task | null) => void;
  } | null>(null);
  const task = snapshot.tasks.find((t) => t.id === currentId);
  const account = snapshot.accounts.find((a) => a.id === currentAccount);
  const saved = snapshot.accounts.filter((a) => !a.pending);
  const pending = snapshot.accounts.filter((a) => a.pending);
  const usable = selected.filter((id) => outcomes[id]?.ok);

  useEffect(() => {
    const ids = new Set(snapshot.accounts.map((a) => a.id));
    setSelected((old) => old.filter((id) => ids.has(id)));
    setOutcomes((old) =>
      Object.fromEntries(Object.entries(old).filter(([id]) => ids.has(id))),
    );
  }, [snapshot.accounts]);

  const removeButton = (account: Account) => (
    <button
      className="text-button remove-account-button"
      aria-label={format(`移除 ${account.name}（${account.email}）`)}
      disabled={
        running ||
        snapshot.tasks.some((t) => t.account_id === account.id && active(t))
      }
      onClick={() => onRemove(account)}
    >
      <Trash2 size={15} />
      移除
    </button>
  );

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      stopped.current = true;
      waiter.current?.resolve(null);
      waiter.current = null;
    };
  }, []);
  useEffect(() => {
    const waiting = waiter.current;
    if (!waiting) return;
    const finished = snapshot.tasks.find(
      (t) => t.id === waiting.id && !active(t),
    );
    if (finished) {
      waiter.current = null;
      waiting.resolve(finished);
    }
  }, [snapshot.tasks, currentId]);

  async function execute(
    id: string,
    action: Action,
    params: Record<string, unknown> = {},
  ): Promise<Task | null> {
    setCurrentAccount(id);
    setCurrentId("");
    const taskId = await bridge.start(id, action, params);
    if (stopped.current || !mounted.current) {
      await bridge.cancel(id, taskId);
      return null;
    }
    return new Promise((resolve, reject) => {
      waiter.current = { id: taskId, resolve };
      setCurrentId(taskId);
      // Covers completion before start() returned and a temporarily missed event.
      refresh().catch((e) => {
        if (waiter.current?.id === taskId) {
          waiter.current = null;
          reject(e);
        }
      });
    });
  }
  const confirmed = (t: Task | null) =>
    t?.status === "succeeded" &&
    t.result?.success === true &&
    t.result?.logged_in === true &&
    Boolean(t.result?.apple_id);
  const describe = (t: Task | null): Outcome => ({
    ok: Boolean(confirmed(t)),
    message: confirmed(t)
      ? `${t!.result!.name ? t!.result!.name + " · " : ""}${t!.result!.apple_id}`
      : t?.stage || "未完成检查",
  });
  async function stop() {
    stopped.current = true;
    if (currentId && currentAccount) {
      try {
        await bridge.cancel(currentAccount, currentId);
      } catch (e) {
        setError(String(e));
      }
    }
  }
  async function checkSelected() {
    stopped.current = false;
    setRunning(true);
    setError("");
    const passed: string[] = [];
    for (const id of selected) {
      if (stopped.current || !mounted.current) break;
      if (outcomes[id]?.ok) {
        passed.push(id);
        continue;
      }
      try {
        const result = await execute(id, "me");
        if (!mounted.current) return;
        const outcome = describe(result);
        setOutcomes((s) => ({ ...s, [id]: outcome }));
        if (outcome.ok) passed.push(id);
      } catch (e) {
        setOutcomes((s) => ({ ...s, [id]: { ok: false, message: String(e) } }));
      }
    }
    if (!mounted.current) return;
    setRunning(false);
    setCurrentId("");
    setCurrentAccount("");
    if (!stopped.current && passed.length === selected.length && passed.length)
      onDone(passed);
  }
  async function enroll(id: string, browserLogin = false) {
    stopped.current = false;
    setRunning(true);
    setError("");
    try {
      const result = await execute(id, "enroll", { browser_login: browserLogin });
      if (!mounted.current) return;
      await refresh();
      if (confirmed(result) && !stopped.current) onDone([id]);
      else
        setError(
          `${result?.stage || "登录未完成"}；此账号尚未添加成功，可继续登录。`,
        );
    } catch (e) {
      setError(String(e));
    } finally {
      if (mounted.current) setRunning(false);
    }
  }
  async function add(email: string) {
    try {
      const a = await bridge.add(email);
      await refresh();
      setAdding(false);
      await enroll(a.id);
    } catch (e) {
      setError(String(e));
    }
  }

  return (
    <main className="account-setup">
      <h1>
        {onBack || (!saved.length && !hadAccounts) || adding
          ? "添加并登录账号"
          : "选择本次使用的账号"}
      </h1>
      <p className="muted">
        {onBack || !saved.length
          ? "完成密码、二次验证和商城登录后，账号才会添加成功。"
          : "勾选后依次查询登录信息，需要重新认证时会提示你完成登录。"}
      </p>
      {!onBack && saved.length > 0 && (
        <>
          <div className="account-selection" aria-label="本次使用的账号">
            {saved.map((a) => (
              <div className="account-option-row" key={a.id}>
                <label className="account-option">
                  <input
                    type="checkbox"
                    disabled={running}
                    checked={selected.includes(a.id)}
                    aria-label={format(`使用 ${a.name}（${a.email}）`)}
                    onChange={(e) =>
                      setSelected((s) =>
                        e.target.checked
                          ? [...s, a.id]
                          : s.filter((id) => id !== a.id),
                      )
                    }
                  />
                  <span>
                    <strong>{format(a.name)}</strong>
                    <small>{format(a.email)}</small>
                    {outcomes[a.id] && (
                      <small
                        className={
                          outcomes[a.id].ok ? "success-text" : "error-text"
                        }
                      >
                        {outcomes[a.id].ok ? "已确认：" : "未启用："}
                        {format(outcomes[a.id].message)}
                      </small>
                    )}
                  </span>
                  {running && currentAccount === a.id ? (
                    <LoaderCircle size={16} className="spin" />
                  ) : outcomes[a.id]?.ok ? (
                    <Check size={16} />
                  ) : null}
                </label>
                {removeButton(a)}
              </div>
            ))}
          </div>
          {!running && (
            <div className="button-row">
              <button
                className="primary"
                disabled={!selected.length}
                onClick={checkSelected}
              >
                检查并使用所选账号
              </button>
              {usable.length > 0 && (
                <button onClick={() => onDone(usable)}>
                  使用已通过的 {usable.length} 个账号
                </button>
              )}
            </div>
          )}
        </>
      )}
      {running && (
        <section
          className="setup-progress"
          aria-live="polite"
          id={task ? `task-${task.id}` : undefined}
          tabIndex={-1}
        >
          <h2>{format(account?.name || "正在处理账号")}</h2>
          <p>{format(account?.email || "")}</p>
          {task?.prompt ? (
            renderPrompt(task, stop)
          ) : (
            <p className="loading-line">
              <LoaderCircle size={16} className="spin" />
              {format(task?.stage || "正在启动登录检查")}
            </p>
          )}
          {!task?.prompt && (
            <button onClick={stop} className="operation-cancel">
              <X size={14} />
              取消
            </button>
          )}
        </section>
      )}
      {error && (
        <p role="alert" className="error-text">
          {format(error)}
        </p>
      )}
      {!running && (
        <>
          {pending.length > 0 && (
            <section className="pending-accounts">
              <h2>未完成添加</h2>
              {pending.map((a) => (
                <div className="pending-account" key={a.id}>
                  <span>
                    {format(a.name)}
                    <small>{format(a.email)}</small>
                  </span>
                  <button onClick={() => enroll(a.id)}>
                    继续登录 {format(a.name)}
                  </button>
                  <button onClick={() => enroll(a.id, true)}>
                    使用官网浏览器登录
                  </button>
                  {removeButton(a)}
                </div>
              ))}
            </section>
          )}
          {adding || (!snapshot.accounts.length && !hadAccounts) ? (
            renderForm(
              add,
              saved.length || pending.length
                ? () => setAdding(false)
                : undefined,
            )
          ) : (
            <button className="text-button" onClick={() => setAdding(true)}>
              <Plus size={15} />
              添加新账号
            </button>
          )}
          {onBack && (
            <button className="text-button" onClick={onBack}>
              返回工作台
            </button>
          )}
        </>
      )}
    </main>
  );
}
