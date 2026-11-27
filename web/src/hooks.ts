import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "./api";

export interface Loaded<T> {
  data: T | undefined;
  error: ApiError | null;
  loading: boolean;
  reload: () => void;
  setData: (v: T | ((old: T | undefined) => T)) => void;
}

/**
 * Loads data when `deps` change and, if `everyMs` is set, again on that
 * interval (the interval can depend on the data: return 0 to stop). Old data
 * stays on screen while a refresh runs.
 */
export function useApi<T>(
  load: () => Promise<T>,
  deps: unknown[],
  everyMs?: number | ((data: T | undefined) => number),
): Loaded<T> {
  const [data, setData] = useState<T | undefined>();
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  const loadRef = useRef(load);
  loadRef.current = load;
  const dataRef = useRef<T | undefined>(undefined);
  dataRef.current = data;

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const run = async () => {
      try {
        const d = await loadRef.current();
        if (cancelled) return;
        setData(d);
        dataRef.current = d;
        setError(null);
      } catch (e) {
        if (cancelled) return;
        setError(e instanceof ApiError ? e : new ApiError(0, "error", String(e)));
      } finally {
        if (!cancelled) {
          setLoading(false);
          const ms = typeof everyMs === "function" ? everyMs(dataRef.current) : everyMs;
          if (ms && ms > 0) timer = setTimeout(run, ms);
        }
      }
    };
    setLoading(true);
    run();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);

  const reload = useCallback(() => setTick((t) => t + 1), []);
  return { data, error, loading, reload, setData: setData as Loaded<T>["setData"] };
}

/** Runs an action and tracks busy and error state for a button or form. */
export function useAction<A extends unknown[], R>(fn: (...args: A) => Promise<R>) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const run = useCallback(
    async (...args: A): Promise<R | undefined> => {
      setBusy(true);
      setError(null);
      try {
        return await fn(...args);
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
        return undefined;
      } finally {
        setBusy(false);
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [fn],
  );
  return { run, busy, error, clearError: () => setError(null) };
}

export const POLL = 5000;
