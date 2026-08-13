import { useCallback, useEffect, useState } from "react";
import { api } from "./api.js";

export function useResource(path, { poll = 0, enabled = true } = {}) {
  const [state, setState] = useState({ data: null, error: null, loading: true });
  const load = useCallback(async () => {
    if (!enabled) return;
    setState((current) => ({ ...current, loading: current.data === null, error: null }));
    try { setState({ data: await api(path), error: null, loading: false }); }
    catch (error) { setState((current) => ({ ...current, error, loading: false })); }
  }, [path, enabled]);
  useEffect(() => {
    load();
    if (!poll) return undefined;
    const timer = window.setInterval(load, poll);
    return () => window.clearInterval(timer);
  }, [load, poll]);
  return { ...state, reload: load, setData: (data) => setState({ data, error: null, loading: false }) };
}
