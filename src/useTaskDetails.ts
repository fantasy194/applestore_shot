import { useEffect, useMemo, useRef, useState } from "react";
import type { Bridge } from "./api";
import type { Snapshot, Task } from "./types";

// Keep full results only for visible panels. Live metadata never pulls old logs.
export function useTaskDetails(
  wire: Snapshot,
  bridge: Bridge | undefined,
  wantedIds: string[],
  logTaskId: string,
) {
  const [cache, setCache] = useState<Map<string, Task>>(() => new Map());
  const [error, setError] = useState({ id: "", message: "" });
  const cached = useRef(cache);
  const inFlight = useRef(false);
  const mounted = useRef(true);
  const cooldown = useRef(new Map<string, number>());
  const wanted = wire.tasks.filter(
    (task) => task.is_summary && wantedIds.includes(task.id),
  );
  const latest = useRef({ wanted, logTaskId });
  latest.current = { wanted, logTaskId };
  const key = wanted
    .map(
      (task) => `${task.id}:${task.detail_revision}:${task.id === logTaskId}`,
    )
    .join("|");
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  useEffect(() => {
    if (!bridge?.task || !key) return;
    let disposed = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function read() {
      if (disposed) return;
      if (document.visibilityState === "hidden" || inFlight.current) {
        timer = setTimeout(read, 250);
        return;
      }
      const target = latest.current.wanted.find((task) => {
        const detail = cached.current.get(task.id);
        return (
          (!detail ||
            detail.detail_revision !== task.detail_revision ||
            (task.id === latest.current.logTaskId &&
              detail.logs_included === false)) &&
          (cooldown.current.get(task.id) || 0) <= Date.now()
        );
      });
      if (!target) {
        if (
          latest.current.wanted.some(
            (task) => (cooldown.current.get(task.id) || 0) > Date.now(),
          )
        )
          timer = setTimeout(read, 500);
        return;
      }
      inFlight.current = true;
      try {
        const detail = await bridge!.task!(target.account_id, target.id, {
          includeLogs: target.id === latest.current.logTaskId,
        });
        if (
          mounted.current &&
          latest.current.wanted.some(
            (task) =>
              task.id === detail.id && task.account_id === detail.account_id,
          )
        ) {
          const next = new Map(cached.current);
          next.delete(detail.id);
          next.set(detail.id, detail);
          while (next.size > 32) next.delete(next.keys().next().value!);
          cached.current = next;
          setCache(next);
          cooldown.current.delete(detail.id);
          setError((previous) =>
            previous.id === detail.id ? { id: "", message: "" } : previous,
          );
        }
      } catch {
        cooldown.current.set(target.id, Date.now() + 2000);
        if (mounted.current)
          setError({
            id: target.id,
            message: "任务详情暂时无法读取，正在重试。",
          });
      } finally {
        inFlight.current = false;
        if (!disposed) timer = setTimeout(read, 100);
      }
    }
    void read();
    return () => {
      disposed = true;
      clearTimeout(timer);
    };
  }, [bridge, key]);
  const snapshot = useMemo(
    () => ({
      ...wire,
      tasks: wire.tasks.map((task) => {
        const detail = cache.get(task.id);
        if (
          !task.is_summary ||
          !detail ||
          detail.account_id !== task.account_id ||
          detail.detail_revision !== task.detail_revision
        )
          return task;
        return {
          ...detail,
          ...task,
          is_summary: false,
          result: detail.result,
          logs: detail.logs,
          logs_included: detail.logs_included,
        };
      }),
    }),
    [wire, cache],
  );
  return { snapshot, detailError: error };
}
