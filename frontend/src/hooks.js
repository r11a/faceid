import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api.js";
import { subscribeRealtime } from "./realtime.js";

export function createResourceLoader(path, update, fetchResource = api) {
  let controller = null;
  let disposed = false;
  let refreshPending = false;
  return {
    async load(replace = true) {
      if (disposed) return;
      if (!replace && controller) { refreshPending = true; return; }
      controller?.abort();
      const request = new AbortController();
      controller = request;
      update(current => ({ ...current, loading: current.data === null, error: null }));
      try {
        const data = await fetchResource(path, { signal: request.signal });
        if (!request.signal.aborted) update({ data, error: null, loading: false });
      } catch (error) {
        if (!request.signal.aborted) update(current => ({ ...current, error, loading: false }));
      } finally {
        if (controller === request) {
          controller = null;
          if (refreshPending && !disposed) {
            refreshPending = false;
            void this.load(false);
          }
        }
      }
    },
    dispose() { disposed = true; controller?.abort(); },
  };
}

export function resourceState(state, path, enabled) {
  if (state.path !== path || state.enabled !== enabled) return { data: null, error: null, loading: enabled };
  return { data: state.data, error: state.error, loading: state.loading };
}

export function useResource(path, { poll = 0, enabled = true, realtime = false } = {}) {
  const [state, setState] = useState({ path, enabled, data: null, error: null, loading: enabled });
  const request = useRef(null);
  const load = useCallback(() => request.current?.load(false), []);
  useEffect(() => {
    if (!enabled) {
      setState({ path, enabled, data: null, error: null, loading: false });
      return undefined;
    }
    const update = change => setState(current => ({
      ...(typeof change === "function" ? change(current) : change), path, enabled,
    }));
    const loader = createResourceLoader(path, update);
    request.current = loader;
    update({ data: null, error: null, loading: true });
    load();
    const timer = poll ? window.setInterval(load, poll) : null;
    const unsubscribe = realtime ? subscribeRealtime(load) : null;
    return () => {
      loader.dispose(); request.current = null;
      window.clearInterval(timer); unsubscribe?.();
    };
  }, [path, enabled, load, poll, realtime]);
  return { ...resourceState(state, path, enabled), reload: load,
    setData: (data) => setState({ path, enabled, data, error: null, loading: false }) };
}


// A confirmed event can arrive just before its image is persisted. Retry only
// transient image loading, without changing identity or refreshing whole pages.
export function useImageRetry(src) {
  const [state, setState] = useState({ src, attempt: 0, failed: false });
  const timer = useRef(null);
  const current = state.src === src ? state : { src, attempt: 0, failed: false };
  useEffect(() => {
    setState(value => value.src === src ? value : { src, attempt: 0, failed: false });
    return () => { clearTimeout(timer.current); timer.current = null; };
  }, [src]);
  const onError = () => {
    if (timer.current !== null) return;
    setState({ ...current, failed: true });
    if (current.attempt < 2 && src && !src.startsWith("data:")) {
      timer.current = setTimeout(() => {
        timer.current = null;
        setState({ src, attempt: current.attempt + 1, failed: false });
      }, current.attempt ? 1200 : 400);
    }
  };
  const url = src && current.attempt && !src.startsWith("data:")
    ? `${src}${src.includes("?") ? "&" : "?"}faceid_retry=${current.attempt}` : src;
  return { url, failed: current.failed, onError };
}
