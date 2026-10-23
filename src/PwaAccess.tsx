import { useEffect, useState, type ReactNode, type FormEvent } from "react";
import { Bell, KeyRound, Smartphone } from "lucide-react";
import { disablePush, enablePush, pwaCall, registerPwa } from "./pwa";

export function PwaSettingsPage() {
  const [busy, setBusy] = useState("");
  const [password, setPassword] = useState("");
  const [notification, setNotification] = useState({
    message: "",
    failed: false,
  });
  const [access, setAccess] = useState({ message: "", failed: false });
  async function notify(
    operation: string,
    action: () => Promise<unknown>,
    success: string,
  ) {
    setBusy(operation);
    setNotification({ message: "", failed: false });
    try {
      await action();
      setNotification({ message: success, failed: false });
    } catch (error) {
      setNotification({
        message: error instanceof Error ? error.message : String(error),
        failed: true,
      });
    } finally {
      setBusy("");
    }
  }
  async function endSession(operation: "password" | "logout") {
    setBusy(operation);
    setAccess({ message: "", failed: false });
    try {
      await pwaCall(
        `auth/${operation}`,
        operation === "password" ? { password } : {},
      );
      setPassword("");
      sessionStorage.removeItem("duo-killer-web-access");
      window.dispatchEvent(new Event("duo-auth-expired"));
    } catch (error) {
      setAccess({
        message: error instanceof Error ? error.message : String(error),
        failed: true,
      });
    } finally {
      setBusy("");
    }
  }
  return (
    <main className="local-page access-page">
      <header className="local-heading access-heading">
        <div>
          <h1>应用设置</h1>
          <p>管理此设备的通知、访问密码和应用安装。</p>
        </div>
      </header>
      <section
        className="ui-panel access-panel"
        aria-labelledby="access-notifications"
      >
        <div className="ui-panel-heading">
          <Bell className="access-panel-icon" size={20} />
          <h2 id="access-notifications">通知</h2>
        </div>
        <p className="access-copy">
          有货时立即提醒；待付款订单每分钟提醒，最长 30 分钟。
        </p>
        <div className="ui-toolbar access-actions">
          <button
            className="primary"
            disabled={!!busy}
            onClick={() =>
              void notify(
                "enable",
                enablePush,
                "此设备已启用库存和待付款通知。",
              )
            }
          >
            {busy === "enable" ? "正在启用…" : "启用通知"}
          </button>
          <button
            disabled={!!busy}
            onClick={() =>
              void notify(
                "test",
                () => pwaCall("push/test", {}),
                "测试通知已提交，请查看设备通知中心。",
              )
            }
          >
            测试通知
          </button>
          <button
            disabled={!!busy}
            onClick={() =>
              void notify("disable", disablePush, "此设备已停用通知。")
            }
          >
            停用通知
          </button>
        </div>
        {notification.message && (
          <p
            className={`access-feedback${notification.failed ? " error-text" : ""}`}
            role={notification.failed ? "alert" : "status"}
          >
            {notification.message}
          </p>
        )}
        <details className="ui-disclosure access-help">
          <summary>通知使用说明</summary>
          <p>
            需要保持本机服务、隧道和电脑运行。实际投递由浏览器和系统决定；每台设备分别启用通知。
          </p>
          <p>iPhone 请先添加到主屏幕，再从应用图标打开并允许通知。</p>
        </details>
      </section>
      <section
        className="ui-panel access-panel"
        aria-labelledby="access-password"
      >
        <div className="ui-panel-heading">
          <KeyRound className="access-panel-icon" size={20} />
          <h2 id="access-password">访问密码</h2>
        </div>
        <p className="access-copy">
          保护手机和浏览器访问。修改后需要重新登录。
        </p>
        <form
          className="access-password-form"
          onSubmit={(event) => {
            event.preventDefault();
            void endSession("password");
          }}
        >
          <label className="access-password-field">
            新访问密码
            <input
              type="password"
              minLength={8}
              maxLength={128}
              autoComplete="new-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              required
              disabled={!!busy}
            />
          </label>
          <div className="ui-toolbar access-actions">
            <button type="submit" disabled={!!busy}>
              {busy === "password" ? "正在修改…" : "修改访问密码"}
            </button>
            <button
              type="button"
              disabled={!!busy}
              onClick={() => void endSession("logout")}
            >
              退出 Web 登录
            </button>
          </div>
        </form>
        {access.message && (
          <p
            className={`access-feedback${access.failed ? " error-text" : ""}`}
            role={access.failed ? "alert" : "status"}
          >
            {access.message}
          </p>
        )}
      </section>
      <section
        className="ui-panel access-panel"
        aria-labelledby="access-install"
      >
        <div className="ui-panel-heading">
          <Smartphone className="access-panel-icon" size={20} />
          <h2 id="access-install">安装应用</h2>
        </div>
        <p className="access-copy">添加到主屏幕，方便查看监控和处理付款。</p>
        <details className="ui-disclosure access-help">
          <summary>查看安装方法</summary>
          <p>
            iPhone：使用 Safari
            打开，在分享菜单选择“添加到主屏幕”，再从图标启动。
          </p>
          <p>
            Android 或桌面浏览器：在浏览器菜单选择“安装应用”或“添加到主屏幕”。
          </p>
          <p>
            Quick Tunnel
            重启后网址会改变，请打开新入口重新登录、安装并订阅通知。
          </p>
        </details>
      </section>
    </main>
  );
}

export function PwaAccess({ children }: { children: ReactNode }) {
  const fixture = import.meta.env.DEV && Boolean(window.__DUO_KILLER_TEST__);
  const [authenticated, setAuthenticated] = useState(fixture);
  const [checking, setChecking] = useState(!fixture);
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    if (fixture) return;
    let live = true;
    void registerPwa().catch(() => {});
    void pwaCall("auth/status")
      .then((result) => {
        if (live) setAuthenticated(result.authenticated === true);
      })
      .catch((error) => {
        if (live) setError(error.message);
      })
      .finally(() => {
        if (live) setChecking(false);
      });
    const expired = () => {
      setAuthenticated(false);
      setPassword("");
      setError("登录已失效，请重新输入访问密码。");
    };
    window.addEventListener("duo-auth-expired", expired);
    return () => {
      live = false;
      window.removeEventListener("duo-auth-expired", expired);
    };
  }, [fixture]);
  async function login(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await pwaCall("auth/login", { password });
      setPassword("");
      setAuthenticated(true);
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }
  if (fixture || authenticated) return children;
  return (
    <main className="access-gate">
      <h1>DUO Killer</h1>
      <p>输入本机服务的访问密码，打开库存监控与账号工作台。</p>
      {checking ? (
        <p role="status">正在连接本机服务…</p>
      ) : (
        <form
          className="access-gate-form"
          onSubmit={(event) => void login(event)}
        >
          <label>
            访问密码
            <input
              autoFocus
              type="password"
              autoComplete="current-password"
              maxLength={128}
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              required
            />
          </label>
          <button type="submit" disabled={busy}>
            {busy ? "登录中…" : "登录"}
          </button>
        </form>
      )}
      {error && (
        <p role="alert" className="access-feedback error-text">
          {error}
        </p>
      )}
      <p className="access-gate-note">
        首次默认密码在本机启动日志中显示；本机启动链接可直接进入后修改。
      </p>
    </main>
  );
}
export default PwaAccess;
