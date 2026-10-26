import { afterEach, expect, test, vi } from "vitest";

vi.stubGlobal("window", { location: { pathname: "/" } });
const { subscribeRealtime } = await import("./realtime.js");
const { createResourceLoader, resourceState } = await import("./hooks.js");
afterEach(() => { vi.useRealTimers(); });

test("consumers share a stream and coalesce bursts until final unsubscribe", () => {
  vi.useFakeTimers();
  const streams = [];
  vi.stubGlobal("EventSource", class {
    constructor() { this.close = vi.fn(); streams.push(this); }
    addEventListener(_, listener) { this.listener = listener; }
  });
  const one = vi.fn(), two = vi.fn();
  const stopOne = subscribeRealtime(one), stopTwo = subscribeRealtime(two);
  expect(streams).toHaveLength(1);
  streams[0].listener(); streams[0].listener();
  vi.advanceTimersByTime(200);
  expect(one).toHaveBeenCalledTimes(1); expect(two).toHaveBeenCalledTimes(1);
  stopOne(); expect(streams[0].close).not.toHaveBeenCalled();
  stopTwo(); expect(streams[0].close).toHaveBeenCalledTimes(1);
});

test("overlapping requests abort old requests and ignore stale and disposed results", async () => {
  const pending = [];
  const fetcher = (path, options) => new Promise(resolve => pending.push({ resolve, signal: options.signal }));
  const update = vi.fn();
  const loader = createResourceLoader("dashboard", update, fetcher);
  const first = loader.load(), second = loader.load();
  expect(pending[0].signal.aborted).toBe(true);
  pending[1].resolve({ value: "new" }); await second;
  pending[0].resolve({ value: "old" }); await first;
  expect(update).toHaveBeenLastCalledWith({ data: { value: "new" }, error: null, loading: false });
  const third = loader.load(); loader.dispose();
  const calls = update.mock.calls.length;
  pending[2].resolve({ value: "disposed" }); await third;
  expect(update).toHaveBeenCalledTimes(calls);
});

test("invalidation during a slow request allows it to finish then refreshes once", async () => {
  const pending = [];
  const fetcher = () => new Promise(resolve => pending.push(resolve));
  const update = vi.fn();
  const loader = createResourceLoader("dashboard", update, fetcher);
  const first = loader.load(false);
  loader.load(false); loader.load(false);
  expect(pending).toHaveLength(1);
  pending[0]({ value: "first" }); await first;
  expect(update).toHaveBeenCalledWith({ data: { value: "first" }, error: null, loading: false });
  expect(pending).toHaveLength(2);
  loader.dispose(); pending[1]({ value: "last" });
});

test("a path or enabled change hides the previous person's data before effects run", () => {
  const state = { path: "faces/timeline?person=alice", enabled: true, data: { events: ["alice"] }, loading: false, error: null };
  expect(resourceState(state, "faces/timeline?person=bob", true)).toEqual({ data: null, error: null, loading: true });
  expect(resourceState(state, state.path, false)).toEqual({ data: null, error: null, loading: false });
  expect(resourceState(state, state.path, true).data.events).toEqual(["alice"]);
});


test("alert subscribers react immediately while visual refreshes stay coalesced", () => {
  vi.useFakeTimers();
  let stream;
  vi.stubGlobal("EventSource", class {
    constructor() { stream = this; this.close = vi.fn(); }
    addEventListener(_, listener) { this.listener = listener; }
  });
  const alert = vi.fn(), visual = vi.fn();
  const stopAlert = subscribeRealtime(alert, { immediate: true });
  const stopVisual = subscribeRealtime(visual);
  stream.listener();
  expect(alert).toHaveBeenCalledOnce(); expect(visual).not.toHaveBeenCalled();
  vi.advanceTimersByTime(150);
  expect(alert).toHaveBeenCalledOnce(); expect(visual).toHaveBeenCalledOnce();
  stopAlert(); stopVisual();
});
