import { test, expect } from "@playwright/test";

test.beforeEach(async ({ page }) => {
  await page.route("**/transport-fixture", (route) =>
    route.fulfill({
      contentType: "text/html",
      body: "<!doctype html><title>Transport fixture</title>",
    }),
  );
});

test("实时通道先建立认证会话，断线重连且清理后不再连接", async ({ page }) => {
  await page.goto("/transport-fixture", { waitUntil: "domcontentloaded" });
  await page.evaluate(async () => {
    const root = window as any;
    root.streamSockets = [];
    root.streamAuthRequests = [];
    root.streamStatuses = [];
    root.streamErrors = 0;
    const originalFetch = window.fetch;
    window.fetch = async (input, init) => {
      if (String(input) === "/api/browser-stream-session") {
        root.streamAuthRequests.push(init);
        return new Response(null, { status: 204 });
      }
      return originalFetch(input, init);
    };
    class Socket {
      onmessage: any;
      onclose: any;
      onerror: any;
      closed = false;
      constructor(public url: string) {
        root.streamSockets.push(this);
      }
      close() {
        this.closed = true;
      }
    }
    root.WebSocket = Socket;
    const path = "/src/browser-stream.ts";
    const { connectBrowserStream } = await import(/* @vite-ignore */ path);
    root.stopBrowserStream = connectBrowserStream(
      "account one",
      "task one",
      (status: any) => root.streamStatuses.push(status),
      () => root.streamErrors++,
    );
  });
  await expect
    .poll(() => page.evaluate(() => (window as any).streamSockets.length))
    .toBe(1);
  const initial = await page.evaluate(() => {
    const root = window as any;
    return {
      url: root.streamSockets[0].url,
      request: root.streamAuthRequests[0],
    };
  });
  expect(initial.request.method).toBe("POST");
  expect(initial.request.credentials).toBe("same-origin");
  const url = new URL(initial.url);
  expect(url.searchParams.get("account_id")).toBe("account one");
  expect(url.searchParams.get("task_id")).toBe("task one");
  expect([...url.searchParams.keys()].sort()).toEqual([
    "account_id",
    "task_id",
  ]);
  await page.evaluate(() => {
    const root = window as any;
    root.streamSockets[0].onmessage({
      data: JSON.stringify({
        active: true,
        connected: true,
        frame_current: true,
        frame_updated: false,
        frame: null,
        revision: 1,
      }),
    });
    root.streamSockets[0].onclose();
  });
  await expect
    .poll(() => page.evaluate(() => (window as any).streamSockets.length))
    .toBe(2);
  expect(await page.evaluate(() => (window as any).streamStatuses.length)).toBe(
    1,
  );
  await page.evaluate(() => {
    const root = window as any;
    root.streamSockets[1].onmessage({ data: "x".repeat(4_100_001) });
  });
  await expect
    .poll(() => page.evaluate(() => (window as any).streamSockets.length))
    .toBe(3);
  await page.evaluate(() => {
    const root = window as any;
    root.stopBrowserStream();
    window.dispatchEvent(new Event("focus"));
  });
  await page.waitForTimeout(600);
  expect(await page.evaluate(() => (window as any).streamSockets.length)).toBe(
    3,
  );
  expect(
    await page.evaluate(() =>
      (window as any).streamSockets.every((socket: any) => socket.closed),
    ),
  ).toBe(true);
});

test("认证请求晚于通道清理返回不能创建连接", async ({ page }) => {
  await page.goto("/transport-fixture", { waitUntil: "domcontentloaded" });
  await page.evaluate(async () => {
    const root = window as any;
    root.streamSocketCount = 0;
    const originalFetch = window.fetch;
    window.fetch = async (input, init) => {
      if (String(input) === "/api/browser-stream-session") {
        await new Promise<void>((resolve) => {
          root.releaseStreamAuth = resolve;
        });
        return new Response(null, { status: 204 });
      }
      return originalFetch(input, init);
    };
    root.WebSocket = class {
      constructor() {
        root.streamSocketCount++;
      }
    };
    const path = "/src/browser-stream.ts";
    const { connectBrowserStream } = await import(/* @vite-ignore */ path);
    const stop = connectBrowserStream(
      "account",
      "task",
      () => {},
      () => {},
    );
    stop();
    root.releaseStreamAuth();
  });
  await page.waitForTimeout(100);
  expect(await page.evaluate(() => (window as any).streamSocketCount)).toBe(0);
});

test("桌面与Web消费相同增量状态且清理后丢弃迟到结果", async ({ page }) => {
  await page.goto("/transport-fixture", { waitUntil: "domcontentloaded" });
  await page.evaluate(async () => {
    const root = window as any;
    root.nativeReads = [];
    root.nativeStatuses = [];
    const path = "/src/browser-stream.ts";
    const { connectNativeBrowserStream } = await import(
      /* @vite-ignore */ path
    );
    root.stopNativeStream = connectNativeBrowserStream(
      async (after: number) => {
        root.nativeReads.push(after);
        if (after === 0)
          return {
            active: true,
            connected: true,
            frame_current: true,
            document_id: "same-document",
            revision: 1,
            frame_updated: true,
            frame: { sequence: 1 },
          };
        await new Promise<void>((resolve) => {
          root.releaseNativeRead = resolve;
        });
        return {
          active: true,
          connected: true,
          frame_current: true,
          document_id: "same-document",
          revision: 2,
          frame_updated: false,
          frame: null,
        };
      },
      (status: any) => root.nativeStatuses.push(status),
      () => {},
    );
  });
  await expect
    .poll(() => page.evaluate(() => (window as any).nativeReads))
    .toEqual([0, 1]);
  await page.evaluate(() => {
    const root = window as any;
    root.stopNativeStream();
    root.releaseNativeRead();
  });
  await page.waitForTimeout(100);
  expect(await page.evaluate(() => (window as any).nativeStatuses.length)).toBe(
    1,
  );
  expect(await page.evaluate(() => (window as any).nativeReads)).toEqual([
    0, 1,
  ]);
});

test("代理拒绝WebSocket后降级同一增量事件且清理中止HTTP读取", async ({
  page,
}) => {
  await page.goto("/transport-fixture", { waitUntil: "domcontentloaded" });
  await page.evaluate(async () => {
    const root = window as any;
    root.fallbackSockets = [];
    root.fallbackReads = [];
    root.fallbackReadTimes = [];
    root.fallbackStatuses = [];
    const originalFetch = window.fetch;
    window.fetch = async (input, init) =>
      String(input) === "/api/browser-stream-session"
        ? new Response(null, { status: 204 })
        : originalFetch(input, init);
    root.WebSocket = class {
      onclose: any;
      close() {}
      constructor() {
        root.fallbackSockets.push(this);
      }
    };
    const path = "/src/browser-stream.ts";
    const { connectBrowserStream } = await import(/* @vite-ignore */ path);
    root.stopFallback = connectBrowserStream(
      "account",
      "task",
      (status: any) => root.fallbackStatuses.push(status),
      () => {},
      async (after: number, signal: AbortSignal) => {
        root.fallbackReads.push(after);
        root.fallbackReadTimes.push(performance.now());
        if (after === 0)
          return {
            active: true,
            revision: 7,
            frame_updated: true,
            frame: { sequence: 7 },
          };
        root.pendingFallbackSignal = signal;
        await new Promise<void>((resolve) => {
          root.releaseFallback = resolve;
        });
        return { active: true, revision: 8, frame_updated: false, frame: null };
      },
    );
  });
  await expect
    .poll(() => page.evaluate(() => (window as any).fallbackSockets.length))
    .toBe(1);
  await page.evaluate(() => {
    const socket = (window as any).fallbackSockets[0];
    // One initial status must not hide repeatedly failing upgraded streams.
    socket.onmessage({
      data: JSON.stringify({
        active: true,
        connected: true,
        frame_current: true,
        frame_updated: false,
        revision: 1,
      }),
    });
    socket.onclose();
  });
  await expect
    .poll(() => page.evaluate(() => (window as any).fallbackSockets.length))
    .toBe(2);
  await page.evaluate(() => (window as any).fallbackSockets[1].onclose());
  await expect
    .poll(() => page.evaluate(() => (window as any).fallbackReads))
    .toEqual([0, 7]);
  await page.evaluate(() => {
    const root = window as any;
    window.dispatchEvent(new Event("focus"));
    root.stopFallback();
    root.releaseFallback();
  });
  await page.waitForTimeout(100);
  expect(
    await page.evaluate(() => (window as any).pendingFallbackSignal.aborted),
  ).toBe(true);
  expect(
    await page.evaluate(() => (window as any).fallbackSockets.length),
  ).toBe(2);
  expect(
    await page.evaluate(() =>
      (window as any).fallbackStatuses.map((status: any) => status.revision),
    ),
  ).toEqual([1, 7]);
  expect(
    await page.evaluate(() => {
      const times = (window as any).fallbackReadTimes;
      return times[1] - times[0];
    }),
  ).toBeGreaterThanOrEqual(90);
});

test("聚焦输入控件保留健康实时连接", async ({ page }) => {
  await page.goto("/transport-fixture", { waitUntil: "domcontentloaded" });
  await page.evaluate(async () => {
    const root = window as any;
    root.focusSockets = [];
    const originalFetch = window.fetch;
    window.fetch = async (input, init) =>
      String(input) === "/api/browser-stream-session"
        ? new Response(null, { status: 204 })
        : originalFetch(input, init);
    root.WebSocket = class {
      onmessage: any;
      closed = false;
      constructor() {
        root.focusSockets.push(this);
      }
      close() {
        this.closed = true;
      }
    };
    const path = "/src/browser-stream.ts";
    const { connectBrowserStream } = await import(/* @vite-ignore */ path);
    root.stopFocusStream = connectBrowserStream(
      "account",
      "task",
      () => {},
      () => {},
    );
  });
  await expect
    .poll(() => page.evaluate(() => (window as any).focusSockets.length))
    .toBe(1);
  await page.evaluate(() => {
    const root = window as any;
    root.focusSockets[0].onmessage({
      data: JSON.stringify({
        active: true,
        connected: true,
        frame_current: true,
        frame_updated: false,
        revision: 1,
      }),
    });
    window.dispatchEvent(new Event("focus"));
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await page.waitForTimeout(100);
  expect(await page.evaluate(() => (window as any).focusSockets.length)).toBe(
    1,
  );
  expect(
    await page.evaluate(() => (window as any).focusSockets[0].closed),
  ).toBe(false);
  await page.evaluate(() => (window as any).stopFocusStream());
});

test("慢代理六秒状态空窗暂停期间不反复拆连接，十五秒后才重建", async ({
  page,
}) => {
  await page.goto("/transport-fixture", { waitUntil: "domcontentloaded" });
  await page.clock.install();
  await page.evaluate(async () => {
    const root = window as any;
    root.slowSockets = [];
    const originalFetch = window.fetch;
    window.fetch = async (input, init) =>
      String(input) === "/api/browser-stream-session"
        ? new Response(null, { status: 204 })
        : originalFetch(input, init);
    root.WebSocket = class {
      onmessage: any;
      closed = false;
      constructor() {
        root.slowSockets.push(this);
      }
      close() {
        this.closed = true;
      }
    };
    const path = "/src/browser-stream.ts";
    const { connectBrowserStream } = await import(/* @vite-ignore */ path);
    root.stopSlowStream = connectBrowserStream(
      "account",
      "task",
      () => {},
      () => {},
    );
  });
  await expect
    .poll(() => page.evaluate(() => (window as any).slowSockets.length))
    .toBe(1);
  await page.clock.fastForward(6000);
  expect(await page.evaluate(() => (window as any).slowSockets[0].closed)).toBe(
    false,
  );
  await page.evaluate(() =>
    (window as any).slowSockets[0].onmessage({
      data: JSON.stringify({
        active: true,
        connected: true,
        frame_current: true,
        frame_updated: false,
        revision: 1,
      }),
    }),
  );
  await page.clock.fastForward(15000);
  expect(await page.evaluate(() => (window as any).slowSockets[0].closed)).toBe(
    false,
  );
  await page.clock.fastForward(2000);
  await page.clock.fastForward(500);
  await expect
    .poll(() => page.evaluate(() => (window as any).slowSockets.length))
    .toBe(2);
  await page.evaluate(() => (window as any).stopSlowStream());
});
