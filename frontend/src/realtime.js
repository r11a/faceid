import { assetUrl } from "./api.js";

const listeners = new Set();
let stream = null;
let timer = null;

export function subscribeRealtime(listener) {
  listeners.add(listener);
  if (!stream && typeof EventSource !== "undefined") {
    stream = new EventSource(assetUrl("api/updates"));
    stream.addEventListener("revision", () => {
      // A fixed debounce window also bounds latency during continuous writes.
      if (timer !== null) return;
      timer = setTimeout(() => {
        timer = null;
        for (const notify of listeners) notify();
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
