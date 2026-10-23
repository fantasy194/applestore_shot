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
  const [status, setStatus] = useState<Data>({ active: false });
  const [seen, setSeen] = useState(false);
  const [expanded, setExpanded] = useState(true);
  const [connectionError, setConnectionError] = useState("");
  const [inputError, setInputError] = useState("");
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const [imageFailed, setImageFailed] = useState(false);
  const [now, setNow] = useState(Date.now());
  const seenRef = useRef(false);
  const mounted = useRef(true);
  const inputBusy = useRef(false);
  const pollNow = useRef<() => void>(() => {});
  const expandedRef = useRef(expanded);
  const screen = useRef<HTMLButtonElement>(null);
  const pointer = useRef<{ x: number; y: number; id: number } | null>(null);
  const input = useRef<HTMLInputElement>(null);
  const contextKey = `${task.account_id}:${task.id}:${task.status}`;
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
    let timer: ReturnType<typeof setTimeout> | undefined;
    setStatus({ active: false });
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

    async function poll() {
      if (disposed || polling) return;
      polling = true;
      clearTimeout(timer);
      try {
        const next = await browser!(task.account_id, task.id, "status");
        if (disposed) return;
        setNow(Date.now());
        setStatus(next);
        setConnectionError("");
        if (next.active && !seenRef.current) {
          seenRef.current = true;
          setSeen(true);
          setExpanded(true);
        }
        if (!next.active) setText("");
      } catch {
        if (!disposed) {
          setStatus({ active: false });
          setText("");
          setConnectionError("远程窗口连接失败，请稍后重试。");
        }
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
    void poll();
    return () => {
      disposed = true;
      clearTimeout(timer);
      pollNow.current = () => {};
      document.removeEventListener("visibilitychange", visible);
      window.removeEventListener("focus", visible);
    };
  }, [browser, task.id, task.account_id, task.status]);

  useEffect(() => {
    if (!status.active || !live(task)) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [status.active, task.status]);

  const frame = validFrame(status.frame) ? status.frame : undefined;
  const age = frame ? now - frame.captured_at_ms : Infinity;
  const fresh = !!frame && age >= -1000 && age <= 5000;
  const enabled =
    !!browser && live(task) && status.active === true && fresh && !imageFailed;

  useEffect(() => setImageFailed(false), [frame?.sequence, frame?.data_url]);

  async function send(params: Data) {
    if (
      !browser ||
      !enabled ||
      !frame ||
      Date.now() - frame.captured_at_ms > 5000 ||
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
        {!live(task) || !status.active
          ? error || "官网窗口已结束，不能继续输入。"
          : `${typeof status.url_host === "string" ? status.url_host : "Apple 官网"} · ${fresh ? "画面已连接" : "等待新画面"}`}
        {live(task) && status.active && frame && (
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
      {expanded && live(task) && status.active && (
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
                };
                event.currentTarget.setPointerCapture(event.pointerId);
              }}
              onPointerCancel={() => {
                pointer.current = null;
              }}
              onPointerUp={(event) => {
                const start = pointer.current;
                pointer.current = null;
                if (!start || start.id !== event.pointerId) return;
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
                onError={() => setImageFailed(true)}
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
          <form
            className="remote-browser-input"
            onSubmit={(event) => {
              event.preventDefault();
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
                disabled={!enabled || sending}
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
