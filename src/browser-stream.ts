import type { Data } from "./types";
import { webHeaders } from "./pwa";

// Desktop receives the same incremental worker events over its private bridge.
// Invoke cannot be aborted, so disposal fences its pending result and next read.
export function connectNativeBrowserStream(
  read: (after: number) => Promise<Data>,
  onStatus: (status: Data) => void,
  onError: () => void,
): () => void {
  let disposed = false;
  let after = 0;
  let attempt = 0;
  let timer: ReturnType<typeof setTimeout> | undefined;
  async function next() {
    let delay = 0;
    try {
      const status = await read(after);
      if (disposed) return;
      if (Number.isSafeInteger(status.revision))
        after = Number(status.revision);
      attempt = 0;
      onStatus(status);
      // Before browser registration there is no worker to wait on.
      if (!status.active) delay = 1000;
    } catch {
      if (disposed) return;
      onError();
      delay = Math.min(4000, 250 * 2 ** Math.min(attempt++, 4));
    }
    if (!disposed) timer = setTimeout(() => void next(), delay);
  }
  void next();
  return () => {
    disposed = true;
    clearTimeout(timer);
  };
}

// One account/task socket owns one preview. Reconnects only read state; inputs
// continue through the existing authenticated command endpoint and never replay.
export function connectBrowserStream(
  accountId: string,
  taskId: string,
  onStatus: (status: Data) => void,
  onError: () => void,
  readFallback?: (after: number, signal: AbortSignal) => Promise<Data>,
): () => void {
  let disposed = false;
  let socket: WebSocket | undefined;
  let retry: ReturnType<typeof setTimeout> | undefined;
  let watchdog: ReturnType<typeof setInterval> | undefined;
  let attempt = 0;
  let received = performance.now();
  let generation = 0;
  let authentication: AbortController | undefined;
  let fallback: AbortController | undefined;
  let usingFallback = false;
  let streamFailures = 0;
  let connectedSince = performance.now();
  let after = 0;

  function disconnect() {
    generation++;
    authentication?.abort();
    authentication = undefined;
    clearInterval(watchdog);
    const previous = socket;
    socket = undefined;
    if (previous) {
      previous.onopen =
        previous.onmessage =
        previous.onerror =
        previous.onclose =
          null;
      previous.close();
    }
  }
  async function pollFallback() {
    if (disposed || !usingFallback || !readFallback) return;
    const controller = new AbortController();
    fallback = controller;
    const deadline = setTimeout(() => controller.abort(), 15000);
    let delay = 100;
    try {
      const status = await readFallback(after, controller.signal);
      if (disposed || controller.signal.aborted) return;
      if (Number.isSafeInteger(status.revision))
        after = Number(status.revision);
      received = performance.now();
      onStatus(status);
      if (!status.active) delay = 1000;
    } catch {
      if (disposed) return;
      onError();
      delay = 1000;
    } finally {
      clearTimeout(deadline);
      if (fallback === controller) fallback = undefined;
      if (!disposed && usingFallback)
        retry = setTimeout(() => {
          retry = undefined;
          void pollFallback();
        }, delay);
    }
  }
  function reconnect() {
    if (disposed || retry || usingFallback) return;
    disconnect();
    onError();
    // Some reverse proxies reject upgrades or buffer a stream. Continue reading
    // the same incremental events over authenticated HTTP; never retry inputs.
    streamFailures++;
    if (readFallback && streamFailures >= 2) {
      usingFallback = true;
      after = 0; // A replacement transport must receive its own initial frame.
      void pollFallback();
      return;
    }
    retry = setTimeout(
      () => {
        retry = undefined;
        connect();
      },
      Math.min(4000, 250 * 2 ** Math.min(attempt++, 4)),
    );
  }
  async function connect() {
    if (disposed) return;
    const selectedGeneration = generation;
    const controller = new AbortController();
    authentication = controller;
    const deadline = setTimeout(() => controller.abort(), 5000);
    const url = new URL("/api/browser-stream", location.href);
    url.protocol = location.protocol === "https:" ? "wss:" : "ws:";
    url.searchParams.set("account_id", accountId);
    url.searchParams.set("task_id", taskId);
    try {
      const response = await fetch("/api/browser-stream-session", {
        method: "POST",
        headers: webHeaders(),
        credentials: "same-origin",
        cache: "no-store",
        signal: controller.signal,
      });
      if (disposed || selectedGeneration !== generation) return;
      if (!response.ok) {
        if (response.status === 401)
          window.dispatchEvent(new Event("duo-auth-expired"));
        throw new Error("Preview authentication failed");
      }
      const current = new WebSocket(url);
      socket = current;
      received = performance.now();
      connectedSince = received;
      current.onmessage = (event) => {
        if (disposed || socket !== current) return;
        try {
          if (typeof event.data !== "string" || event.data.length > 4_100_000)
            throw new Error("Invalid preview message");
          const value: unknown = JSON.parse(event.data);
          if (!value || typeof value !== "object" || Array.isArray(value))
            throw new Error("Invalid preview status");
          const status = value as Data;
          if (
            typeof status.active !== "boolean" ||
            typeof status.connected !== "boolean" ||
            typeof status.frame_current !== "boolean" ||
            typeof status.frame_updated !== "boolean" ||
            !Number.isSafeInteger(status.revision) ||
            Number(status.revision) < 0
          )
            throw new Error("Invalid preview status");
          received = performance.now();
          if (received - connectedSince >= 15000) streamFailures = 0;
          attempt = 0;
          onStatus(status);
        } catch {
          reconnect();
        }
      };
      current.onclose = current.onerror = reconnect;
      watchdog = setInterval(() => {
        if (performance.now() - received > 15000) reconnect();
      }, 1000);
    } catch {
      if (!disposed && selectedGeneration === generation) reconnect();
    } finally {
      clearTimeout(deadline);
      if (authentication === controller) authentication = undefined;
    }
  }
  const resume = () => {
    if (disposed || document.hidden) return;
    // Focusing the local text field must not tear down a healthy stream.
    if (usingFallback) return;
    if (socket && performance.now() - received <= 5000) return;
    clearTimeout(retry);
    retry = undefined;
    disconnect();
    void connect();
  };
  document.addEventListener("visibilitychange", resume);
  window.addEventListener("focus", resume);
  void connect();
  return () => {
    disposed = true;
    clearTimeout(retry);
    fallback?.abort();
    disconnect();
    document.removeEventListener("visibilitychange", resume);
    window.removeEventListener("focus", resume);
  };
}
