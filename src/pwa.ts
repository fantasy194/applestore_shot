export function webHeaders(): Record<string, string> {
  const access = new URLSearchParams(location.hash.slice(1)).get("access");
  if (access && /^[a-f0-9]{64}$/.test(access)) {
    sessionStorage.setItem("duo-killer-web-access", access);
    history.replaceState(null, "", location.pathname + location.search);
  }
  const token = sessionStorage.getItem("duo-killer-web-access");
  return {
    "Content-Type": "application/json",
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  };
}

export async function pwaCall<T = Record<string, any>>(
  path: string,
  body?: object,
): Promise<T> {
  const response = await fetch(`/api/${path}`, {
    method: body === undefined ? "GET" : "POST",
    headers: webHeaders(),
    credentials: "same-origin",
    cache: "no-store",
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  const result = await response.json().catch(() => ({}));
  if (!response.ok)
    throw new Error(result.error || `请求失败（${response.status}）`);
  return result;
}

export async function registerPwa(): Promise<
  ServiceWorkerRegistration | undefined
> {
  if (!("serviceWorker" in navigator) || !window.isSecureContext) return;
  if (!document.querySelector('link[rel="manifest"]')) {
    const link = document.createElement("link");
    link.rel = "manifest";
    link.href = "/manifest.webmanifest";
    document.head.append(link);
    const icon = document.createElement("link");
    icon.rel = "apple-touch-icon";
    icon.href = "/icons/icon-512.png";
    document.head.append(icon);
    const theme = document.createElement("meta");
    theme.name = "theme-color";
    theme.content = "#171b24";
    document.head.append(theme);
  }
  return navigator.serviceWorker.register("/sw.js", {
    scope: "/",
    updateViaCache: "none",
  });
}

function applicationKey(value: string): Uint8Array<ArrayBuffer> {
  const raw = atob(
    value.replace(/-/g, "+").replace(/_/g, "/") +
      "=".repeat((4 - (value.length % 4)) % 4),
  );
  return Uint8Array.from(raw, (character) => character.charCodeAt(0));
}

export async function enablePush(): Promise<void> {
  if (!("PushManager" in window) || !("Notification" in window))
    throw new Error(
      "此浏览器不支持 Web Push。iPhone 请先在 Safari 添加到主屏幕，再从主屏幕打开并允许通知。",
    );
  // Safari requires permission to be requested directly within the click gesture.
  const permissionRequest = Notification.requestPermission();
  const registration = await registerPwa();
  if (!registration) throw new Error("通知需要 HTTPS 或本机 localhost。");
  const permission = await permissionRequest;
  if (permission !== "granted")
    throw new Error("通知权限尚未允许，请在系统或浏览器设置中开启。");
  const status = await pwaCall("push/status", {});
  const ready = await navigator.serviceWorker.ready;
  let subscription = await ready.pushManager.getSubscription();
  if (subscription?.options.applicationServerKey) {
    const expected = applicationKey(status.public_key);
    const actual = new Uint8Array(subscription.options.applicationServerKey);
    if (
      expected.length !== actual.length ||
      expected.some((byte, index) => byte !== actual[index])
    ) {
      await subscription.unsubscribe();
      subscription = null;
    }
  }
  subscription ??= await ready.pushManager.subscribe({
    userVisibleOnly: true,
    applicationServerKey: applicationKey(status.public_key),
  });
  await pwaCall("push/subscribe", { subscription: subscription.toJSON() });
}

export async function disablePush(): Promise<void> {
  const registration = await navigator.serviceWorker.getRegistration();
  const subscription = await registration?.pushManager.getSubscription();
  if (subscription) {
    await pwaCall("push/unsubscribe", { endpoint: subscription.endpoint });
    await subscription.unsubscribe();
  }
}
