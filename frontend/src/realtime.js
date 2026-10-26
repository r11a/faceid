import { assetUrl } from "./api.js";

const listeners = new Map();
let stream = null;
let timer = null;

export function subscribeRealtime(listener, { immediate = false } = {}) {
  listeners.set(listener, immediate);
  if (!stream && typeof EventSource !== "undefined") {
    stream = new EventSource(assetUrl("api/updates"));
    stream.addEventListener("revision", () => {
      for (const [notify, urgent] of listeners) if (urgent) notify();
      // A fixed debounce window also bounds latency during continuous writes.
      if (timer !== null) return;
      timer = setTimeout(() => {
        timer = null;
        for (const [notify, urgent] of listeners) if (!urgent) notify();
      }, 150);
    });
  }
  return () => {
    listeners.delete(listener);
    if (!listeners.size) {
      stream?.close(); stream = null;
      clearTimeout(timer); timer = null;
    }
  };
}
