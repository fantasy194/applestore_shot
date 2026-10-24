import { useEffect, useRef, useState } from "react";
import type { Bridge } from "./api";
import type { Data, Task } from "./types";
import "./RemoteBrowser.css";

type BrowserCall = (
  accountId: string,
  taskId: string,
  operation: "status" | "input",
  params?: Data,
) => Promise<Data>;

type Frame = {
  data_url: string;
  width: number;
  height: number;
  sequence: number;
  captured_at_ms: number;
  document_id?: string;
};

const live = (task: Task) => ["running", "waiting"].includes(task.status);
const validFrame = (value: unknown): value is Frame => {
  const frame = value as Frame | undefined;
  return !!(
    frame &&
    typeof frame.data_url === "string" &&
    /^data:image\/(?:jpeg|png);base64,[A-Za-z0-9+/]+=*$/.test(frame.data_url) &&
    frame.data_url.length <= 4_000_000 &&
    Number.isInteger(frame.width) &&
    frame.width > 0 &&
    frame.width <= 4096 &&
    Number.isInteger(frame.height) &&
    frame.height > 0 &&
    frame.height <= 4096 &&
    Number.isInteger(frame.sequence) &&
    frame.sequence >= 0 &&
    Number.isFinite(frame.captured_at_ms)
  );
};

export function RemoteBrowser({
  bridge,
  task,
}: {
  bridge: Bridge;
  task: Task;
}) {
  // Optional to keep older desktop bridges and preview fixtures compatible.
  const browser = (bridge as Bridge & { browser?: BrowserCall }).browser;
  const browserStream = bridge.browserStream;
  const contextKey = `${task.account_id}:${task.id}:${live(task) ? "live" : task.status}`;
  const [status, setStatus] = useState<Data>({ active: false });
  const [displayed, setDisplayed] = useState<{
    context: string;
    frame: Frame;
  }>();
  const [seen, setSeen] = useState(false);
  const [expanded, setExpanded] = useState(true);
  const [connectionError, setConnectionError] = useState("");
  const [inputError, setInputError] = useState("");
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const [imageFailed, setImageFailed] = useState(false);
  const [now, setNow] = useState(performance.now());
  const lastReceipt = useRef(-Infinity);
  const latestStatus = useRef<Data>({ active: false });
  const seenRef = useRef(false);
  const mounted = useRef(true);
  const inputBusy = useRef(false);
  const pollNow = useRef<() => void>(() => {});
  const expandedRef = useRef(expanded);
  const displayedRef = useRef<typeof displayed>(undefined);
  const decoding = useRef<{ sequence: number; generation: number } | undefined>(
    undefined,
  );
  const pendingDisplay = useRef<typeof displayed>(undefined);
  const frameGeneration = useRef(0);
  const screen = useRef<HTMLButtonElement>(null);
  const pointer = useRef<{
    x: number;
    y: number;
    id: number;
    sequence: number;
    context: string;
  } | null>(null);
  const input = useRef<HTMLInputElement>(null);
  const currentContext = useRef(contextKey);
  currentContext.current = contextKey;
  const error = connectionError || inputError;
  expandedRef.current = expanded;

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  useEffect(() => {
    let disposed = false;
    let polling = false;
    let stopStream: (() => void) | undefined;
    let documentId: unknown;
    let timer: ReturnType<typeof setTimeout> | undefined;
    setStatus({ active: false });
    latestStatus.current = { active: false };
    lastReceipt.current = -Infinity;
    setDisplayed(undefined);
    displayedRef.current = undefined;
    pendingDisplay.current = undefined;
    decoding.current = undefined;
    frameGeneration.current++;
    pointer.current = null;
    setSeen(false);
    seenRef.current = false;
    setExpanded(true);
    setConnectionError("");
    setInputError("");
    setText("");
    setSending(false);
    setImageFailed(false);
    inputBusy.current = false;
    if (!browser || !live(task)) return;

    function prepareFrame(frame: Frame) {
      const previous = displayedRef.current;
      if (
        (previous?.context === contextKey &&
          previous.frame.sequence >= frame.sequence) ||
        (decoding.current && decoding.current.sequence >= frame.sequence)
      )
        return;
      const generation = frameGeneration.current;
      const candidate = { sequence: frame.sequence, generation };
      decoding.current = candidate;
      const image = new Image();
      image.src = frame.data_url;
      void image
        .decode()
        .then(() => {
          if (
            disposed ||
            currentContext.current !== contextKey ||
            frameGeneration.current !== generation ||
            (displayedRef.current &&
              displayedRef.current.frame.sequence >= frame.sequence) ||
            (pendingDisplay.current &&
              pendingDisplay.current.frame.sequence >= frame.sequence)
          )
            return;
          if (!image.naturalWidth || !image.naturalHeight) return;
          const next = { context: contextKey, frame };
          if (pointer.current) pendingDisplay.current = next;
          else {
            displayedRef.current = next;
            setDisplayed(next);
          }
          setImageFailed(false);
          setNow(performance.now());
        })
        .catch(() => {
          if (
            !disposed &&
            frameGeneration.current === generation &&
            decoding.current === candidate
          )
            setImageFailed(true);
        });
    }

    function accept(next: Data) {
      if (disposed || currentContext.current !== contextKey) return;
      const modern = typeof next.connected === "boolean";
      if (modern && typeof next.document_id !== "string") return;
      const changedDocument = modern && next.document_id !== documentId;
      if (changedDocument) documentId = next.document_id;
      lastReceipt.current = performance.now();
      latestStatus.current = next;
      setNow(performance.now());
      setStatus({ ...next, context_key: contextKey });
      setConnectionError("");
      if (next.active && !seenRef.current) {
        seenRef.current = true;
        setSeen(true);
        setExpanded(true);
      }
      if (
        !next.active ||
        changedDocument ||
        (next.frame_updated === true && !next.frame)
      ) {
        frameGeneration.current++;
        decoding.current = undefined;
        displayedRef.current = undefined;
        pendingDisplay.current = undefined;
        setDisplayed(undefined);
        pointer.current = null;
        if (!next.active) setText("");
      }
      if (
        next.active &&
        validFrame(next.frame) &&
        (!modern || next.frame.document_id === documentId)
      )
        prepareFrame(next.frame);
    }
    function failed() {
      if (!disposed && currentContext.current === contextKey) {
        lastReceipt.current = -Infinity;
        setNow(performance.now());
        setConnectionError("远程窗口连接失败，请稍后重试。");
      }
    }
    async function poll() {
      if (disposed || polling || browserStream) return;
      polling = true;
      clearTimeout(timer);
      try {
        accept(await browser!(task.account_id, task.id, "status"));
      } catch {
        failed();
      } finally {
        polling = false;
        if (!disposed) {
          const slow = document.hidden || !document.hasFocus();
          timer = setTimeout(
            poll,
            slow ? 2500 : expandedRef.current ? 750 : 1500,
          );
        }
      }
    }
    pollNow.current = () => void poll();
    const visible = () => {
      if (!document.hidden) void poll();
    };
    document.addEventListener("visibilitychange", visible);
    window.addEventListener("focus", visible);
    if (browserStream)
      stopStream = browserStream(task.account_id, task.id, accept, failed);
    else void poll();
    return () => {
      disposed = true;
      stopStream?.();
      frameGeneration.current++;
      clearTimeout(timer);
      pollNow.current = () => {};
      document.removeEventListener("visibilitychange", visible);
      window.removeEventListener("focus", visible);
    };
  }, [browser, browserStream, contextKey]);

  useEffect(() => {
    if (!status.active || !live(task)) return;
    const timer = setInterval(() => setNow(performance.now()), 1000);
    return () => clearInterval(timer);
  }, [status.active, task.status]);

  const active = status.context_key === contextKey && status.active === true;
  const frame = displayed?.context === contextKey ? displayed.frame : undefined;
  const connected = now - lastReceipt.current <= 5000 && !connectionError;
  const modern = typeof status.connected === "boolean";
  const fresh =
    !!frame &&
    connected &&
    (modern
      ? status.connected === true &&
        status.frame_current === true &&
        frame.document_id === status.document_id
      : Date.now() - frame.captured_at_ms >= -1000 &&
        Date.now() - frame.captured_at_ms <= 5000);
  const enabled = !!browser && live(task) && active && fresh;

  function publishPendingFrame() {
    const next = pendingDisplay.current;
    pendingDisplay.current = undefined;
    if (next?.context !== contextKey) return;
    displayedRef.current = next;
    setDisplayed(next);
    setNow(performance.now());
  }

  async function send(params: Data) {
    if (
      !browser ||
      !enabled ||
      !frame ||
      performance.now() - lastReceipt.current > 5000 ||
      (modern &&
        (latestStatus.current.connected !== true ||
          latestStatus.current.frame_current !== true ||
          frame.document_id !== latestStatus.current.document_id)) ||
      inputBusy.current
    ) {
      return;
    }
    inputBusy.current = true;
    setSending(true);
    setInputError("");
    const selectedTask = task.id;
    const selectedContext = contextKey;
    try {
      await browser(task.account_id, selectedTask, "input", {
        ...params,
        sequence: frame.sequence,
        ...(frame.document_id ? { document_id: frame.document_id } : {}),
      });
      if (mounted.current && currentContext.current === selectedContext)
        pollNow.current();
    } catch {
      if (mounted.current && currentContext.current === selectedContext)
        setInputError(
          "操作未送达官网窗口，请等待画面更新后重试；输入内容未保留。",
        );
    } finally {
      if (mounted.current && currentContext.current === selectedContext) {
        inputBusy.current = false;
        setSending(false);
      }
    }
  }

  const sendRef = useRef(send);
  sendRef.current = send;
  useEffect(() => {
    const element = screen.current;
    if (!element || !enabled || !expanded) return;
    const scroll = (event: WheelEvent) => {
      event.preventDefault();
      void sendRef.current({
        type: "scroll",
        delta_x: Math.max(-2000, Math.min(2000, Math.round(event.deltaX))),
        delta_y: Math.max(-2000, Math.min(2000, Math.round(event.deltaY))),
      });
    };
    element.addEventListener("wheel", scroll, { passive: false });
    return () => element.removeEventListener("wheel", scroll);
  }, [enabled, expanded]);

  if (!browser || (!seen && !status.active && !error)) return null;

  return (
    <section className="remote-browser" aria-label="官网登录远程窗口">
      <div className="remote-browser-heading">
        <h3>Apple 官网窗口</h3>
        <button
          type="button"
          className="text-button"
          aria-expanded={expanded}
          onClick={() => setExpanded((value) => !value)}
        >
          {expanded ? "收起远程窗口" : "展开远程窗口"}
        </button>
      </div>
      <p className="muted" role="status">
        {!live(task) || !active
          ? error || "官网窗口已结束，不能继续输入。"
          : `${typeof status.url_host === "string" ? status.url_host : "Apple 官网"} · ${fresh ? "画面已连接" : "等待新画面"}`}
        {live(task) && active && frame && (
          <span>
            {" "}
            · 更新于 {new Date(frame.captured_at_ms).toLocaleTimeString()}
          </span>
        )}
      </p>
      {error && (
        <p className="error-text" role="alert">
          {error}
        </p>
      )}
      {expanded && live(task) && active && (
        <>
          {frame ? (
            <button
              ref={screen}
              type="button"
              className="remote-browser-screen"
              aria-label="操作 Apple 官网画面"
              disabled={!enabled || sending}
              style={{ aspectRatio: `${frame.width} / ${frame.height}` }}
              onPointerDown={(event) => {
                pointer.current = {
                  x: event.clientX,
                  y: event.clientY,
                  id: event.pointerId,
                  sequence: frame.sequence,
                  context: contextKey,
                };
                event.currentTarget.setPointerCapture(event.pointerId);
              }}
              onPointerCancel={() => {
                pointer.current = null;
                publishPendingFrame();
              }}
              onPointerUp={(event) => {
                const start = pointer.current;
                pointer.current = null;
                publishPendingFrame();
                if (
                  !start ||
                  start.id !== event.pointerId ||
                  start.sequence !== frame.sequence ||
                  start.context !== contextKey
                )
                  return;
                const rect = event.currentTarget.getBoundingClientRect();
                if (!rect.width || !rect.height) return;
                const dx = start.x - event.clientX,
                  dy = start.y - event.clientY;
                if (Math.hypot(dx, dy) > 10) {
                  void send({
                    type: "scroll",
                    delta_x: Math.round((dx * frame.width) / rect.width),
                    delta_y: Math.round((dy * frame.height) / rect.height),
                  });
                } else {
                  void send({
                    type: "click",
                    x: Math.min(
                      frame.width - 1,
                      Math.max(
                        0,
                        Math.floor(
                          ((event.clientX - rect.left) * frame.width) /
                            rect.width,
                        ),
                      ),
                    ),
                    y: Math.min(
                      frame.height - 1,
                      Math.max(
                        0,
                        Math.floor(
                          ((event.clientY - rect.top) * frame.height) /
                            rect.height,
                        ),
                      ),
                    ),
                  });
                  input.current?.focus();
                }
              }}
            >
              <img
                src={frame.data_url}
                alt="当前账号 Apple 官网窗口的实时画面"
                width={frame.width}
                height={frame.height}
                draggable={false}
              />
            </button>
          ) : (
            <p className="muted">正在读取官网画面…</p>
          )}
          {(!fresh || imageFailed) && frame && (
            <p className="muted">画面暂未更新，等待新画面后才能操作。</p>
          )}
          <p className="muted remote-browser-hint">
            点击画面选择官网输入框，再在下方输入并发送；滑动画面可滚动。密码和验证码直接送到此官网窗口。
          </p>
          {(status.signin_available === true ||
            typeof status.signin_status === "string") && (
            <div className="remote-browser-signin">
              <button
                className="primary"
                type="button"
                disabled={
                  !enabled ||
                  sending ||
                  status.signin_available !== true ||
                  status.signin_status === "running" ||
                  status.signin_status === "submitted"
                }
                onClick={() => void send({ type: "signin" })}
              >
                {status.signin_status === "running"
                  ? "正在使用现有凭证登录…"
                  : "使用现有凭证登录"}
              </button>
              {(status.signin_status === "manual" ||
                status.signin_status === "missing_credentials") && (
                <p className="muted">
                  没有可用的登录凭证，请在官网窗口输入密码。
                </p>
              )}
              {status.signin_status === "verification" && (
                <p className="muted">请在官网窗口完成验证。</p>
              )}
            </div>
          )}
          <form
            className="remote-browser-input"
            onSubmit={(event) => {
              event.preventDefault();
              if (!enabled || sending || inputBusy.current) return;
              const value = text;
              setText("");
              if (value) void send({ type: "text", text: value });
            }}
          >
            <label>
              输入到官网
              <input
                ref={input}
                type="password"
                autoComplete="off"
                autoCapitalize="none"
                spellCheck={false}
                maxLength={4096}
                value={text}
                onChange={(event) => setText(event.target.value)}
              />
            </label>
            <button type="submit" disabled={!enabled || sending || !text}>
              发送输入
            </button>
          </form>
          <div className="remote-browser-keys" aria-label="官网键盘操作">
            {[
              ["Tab", "切换输入框"],
              ["Enter", "回车"],
              ["Space", "空格"],
              ["Backspace", "退格"],
              ["Control+A", "全选"],
              ["Escape", "关闭弹层"],
            ].map(([key, label]) => (
              <button
                key={key}
                type="button"
                disabled={!enabled || sending}
                onClick={() => void send({ type: "key", key })}
              >
                {label}
              </button>
            ))}
            <button
              type="button"
              disabled={!enabled || sending}
              onClick={() =>
                void send({ type: "scroll", delta_x: 0, delta_y: -400 })
              }
            >
              向上滚动
            </button>
            <button
              type="button"
              disabled={!enabled || sending}
              onClick={() =>
                void send({ type: "scroll", delta_x: 0, delta_y: 400 })
              }
            >
              向下滚动
            </button>
          </div>
        </>
      )}
    </section>
  );
}
