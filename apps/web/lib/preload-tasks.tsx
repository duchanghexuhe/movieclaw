"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import {
  getPreloadPresets,
  listPreloadTasks,
  type PreloadPreset,
  type PreloadTaskView,
} from "@/lib/api/preload";
import { useSession } from "@/lib/session";

interface PreloadTasksContextValue {
  tasks: PreloadTaskView[];
  /** 档位表（省流/高清），任务卡显示档位短名用；静态配置，进页面取一次。 */
  presets: PreloadPreset[];
  refresh: () => void;
  upsert: (task: PreloadTaskView) => void;
}

const PreloadTasksContext = createContext<PreloadTasksContextValue | null>(null);
const POLL_MS = 15_000;

/**
 * 全站唯一的预转码任务数据源：活动页的任务视角、侧栏角标与设置页共用。
 * 预转码没有 SSE 事件流（Worker 靠租约续命、状态变化本来就慢），15 秒轮询
 * 加回前台即刷兜底足够；取消/重试后调用方经 upsert 即时校准，不等下一轮。
 */
export function PreloadTasksProvider({ children }: { children: React.ReactNode }) {
  const { session } = useSession();
  const enabled = session.role !== "member";
  const [tasks, setTasks] = useState<PreloadTaskView[]>([]);
  const [presets, setPresets] = useState<PreloadPreset[]>([]);
  const inFlight = useRef(false);
  const refreshQueued = useRef(false);

  const refresh = useCallback(() => {
    refreshQueued.current = true;
    if (inFlight.current) return;
    inFlight.current = true;
    void (async () => {
      while (refreshQueued.current) {
        refreshQueued.current = false;
        try {
          const next = await listPreloadTasks();
          setTasks(next);
        } catch {
          // 瞬时断线保留最近快照，下一轮轮询自动校准。
        }
      }
      inFlight.current = false;
    })();
  }, []);

  const upsert = useCallback((task: PreloadTaskView) => {
    setTasks((current) =>
      [task, ...current.filter((item) => item.id !== task.id)].toSorted(
        (left, right) =>
          Date.parse(right.updated_at ?? right.created_at ?? "") -
          Date.parse(left.updated_at ?? left.created_at ?? ""),
      ),
    );
  }, []);

  useEffect(() => {
    if (!enabled) {
      setTasks([]);
      return;
    }
    refresh();
    getPreloadPresets()
      .then(setPresets)
      .catch(() => undefined);
    const timer = window.setInterval(refresh, POLL_MS);
    const onFocus = () => refresh();
    window.addEventListener("focus", onFocus);
    return () => {
      refreshQueued.current = false;
      window.clearInterval(timer);
      window.removeEventListener("focus", onFocus);
    };
  }, [enabled, refresh]);

  const value = useMemo(() => ({ tasks, presets, refresh, upsert }), [tasks, presets, refresh, upsert]);
  return <PreloadTasksContext.Provider value={value}>{children}</PreloadTasksContext.Provider>;
}

export function usePreloadTasks(): PreloadTasksContextValue {
  const value = useContext(PreloadTasksContext);
  if (!value) throw new Error("usePreloadTasks 必须在 PreloadTasksProvider 内使用");
  return value;
}
