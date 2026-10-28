import React from "react";
import { act, create } from "react-test-renderer";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { AlertCenter } from "./AlertCenter.jsx";
import { api } from "./api.js";
import { subscribeRealtime } from "./realtime.js";

vi.mock("react-dom", () => ({ createPortal: child => child }));
vi.mock("./api.js", () => ({ api: vi.fn(), assetUrl: path => `/ingress/${path}` }));
vi.mock("./realtime.js", () => ({ subscribeRealtime: vi.fn() }));
vi.mock("./hooks.js", () => ({ useResource: () => ({ data: { alerts: [] } }) }));
vi.mock("./pages/Daily.jsx", () => ({ EventModal: () => null }));
vi.mock("./ui.jsx", () => ({ Modal: ({ children }) => <div>{children}</div>, Empty: () => null, ErrorState: () => null, Loading: () => null, dateTime: value => value }));

globalThis.IS_REACT_ACT_ENVIRONMENT = true;
let renderer, notify, unsubscribe;
const alert = (id, reasons = []) => ({ id, event_id: `e${id}`, person: `Person ${id}`, camera: "Door", reasons, image_url: `api/activity/e${id}/image`, notified_ts: Date.now() / 1000 });
beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal("document", { body: {} });
  vi.stubGlobal("localStorage", { getItem: vi.fn(() => "true"), setItem: vi.fn() });
  unsubscribe = vi.fn();
  subscribeRealtime.mockImplementation(callback => { notify = callback; return unsubscribe; });
  api.mockResolvedValue({ alerts: [], cursor: 9 });
});
afterEach(async () => {
  if (renderer) await act(() => renderer.unmount());
  renderer = null;
  vi.useRealTimers(); vi.unstubAllGlobals(); vi.clearAllMocks();
});
async function mount() { await act(async () => { renderer = create(<AlertCenter />); }); }
async function receive(alerts, cursor = alerts.at(-1)?.id) {
  api.mockResolvedValueOnce({ alerts, cursor });
  await act(async () => { await notify(); });
}
const popups = () => renderer.root.findAllByProps({ role: "alert" });

it("baselines without replay, queues each recognition, uses ingress evidence, and expires in five seconds", async () => {
  await mount();
  expect(api.mock.calls[0][0]).toBe("alerts?include_recognitions=true");
  expect(popups()).toHaveLength(0);
  await receive([alert(10), alert(11)]);
  expect(popups()[0].findByType("b").children.join("")).toContain("Person 10");
  expect(popups()[0].findByType("img").props.src).toBe("/ingress/api/activity/e10/image");
  await act(async () => { vi.advanceTimersByTime(5000); });
  expect(popups()[0].findByType("b").children.join("")).toContain("Person 11");
  await act(async () => { vi.advanceTimersByTime(5000); });
  expect(popups()).toHaveLength(0);
});

it("bounds bursts, deduplicates queued events, rejects remote image URLs, and cleans up timers", async () => {
  await mount();
  await receive([alert(10)]);
  await receive(Array.from({ length: 35 }, (_, index) => alert(index + 10)));
  expect(popups()[0].findAllByType("small").at(-1).children.join("")).toContain("19");
  await act(async () => { renderer.root.findByProps({ "aria-label": "סגירת התראה" }).props.onClick(); });
  expect(popups()[0].findByType("b").children.join("")).toContain("Person 26");
  await receive([{ ...alert(50), image_url: "https://example.com/private.jpg" }]);
  await act(async () => { renderer.unmount(); }); renderer = null;
  expect(unsubscribe).toHaveBeenCalledOnce();
  expect(vi.getTimerCount()).toBe(0);
});

it("keeps existing critical alerts persistent when disabled and tolerates preference storage failure", async () => {
  localStorage.getItem.mockImplementation(() => { throw new Error("blocked"); });
  await mount();
  expect(api.mock.calls[0][0]).toBe("alerts");
  await receive([alert(10, ["critical_camera"])]);
  await act(async () => { vi.advanceTimersByTime(6000); });
  expect(popups()).toHaveLength(1);
  await act(() => renderer.root.findByProps({ title: "התראות זיהוי" }).props.onClick());
  localStorage.setItem.mockImplementation(() => { throw new Error("blocked"); });
  await act(async () => renderer.root.findByType("input").props.onChange({ target: { checked: true } }));
  expect(renderer.root.findByType("input").props.checked).toBe(true);
  expect(renderer.root.findAllByProps({ role: "status" })).toHaveLength(1);
  expect(api.mock.calls.at(-1)[0]).toBe("alerts?include_recognitions=true");
});

it("refuses remote photos and clears queued normal notifications when disabled", async () => {
  await mount();
  await receive([{ ...alert(10), image_url: "https://camera.local/private.jpg" }, alert(11)]);
  expect(popups()[0].findAllByType("img")).toHaveLength(0);
  await act(() => renderer.root.findByProps({ title: "התראות זיהוי" }).props.onClick());
  await act(async () => renderer.root.findByType("input").props.onChange({ target: { checked: false } }));
  expect(localStorage.setItem).toHaveBeenCalledWith("faceid.recognitionPopups", "false");
  expect(popups()).toHaveLength(0);
  expect(api.mock.calls.at(-1)[0]).toBe("alerts");
});
