import React from "react";
import { act, create } from "react-test-renderer";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { HealthPage } from "./pages/Management.jsx";
import { EventModal, EventsPage } from "./pages/Daily.jsx";
import { ActionGroup, ActionsMenu, ToastProvider, useToast } from "./ui.jsx";
import { useImageRetry, useResource } from "./hooks.js";
import { api } from "./api.js";

vi.mock("./hooks.js", async importOriginal => ({ ...await importOriginal(), useResource: vi.fn() }));
vi.mock("./api.js", () => ({ api: vi.fn(), assetUrl: path => `/ingress/${path}`, query: () => "" }));
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
let renderer;
beforeEach(() => {
  vi.stubGlobal("window", { addEventListener: vi.fn(), removeEventListener: vi.fn(), setTimeout: vi.fn() });
  api.mockReset();
});
afterEach(async () => { if (renderer) await act(() => renderer.unmount()); renderer = null; vi.useRealTimers(); vi.unstubAllGlobals(); });
function resources(values) {
  useResource.mockImplementation(path => ({ data: values[path] || {}, loading: false, reload: vi.fn() }));
}

describe("actual health contract", () => {
  it("shows successful Frigate and MQTT reports without an invented entity count", () => {
    resources({ health: { version: "6.4.11", engine: { providers: ["CPU"] } }, "system-report": { frigate: { reachable: true, tls_verified: false }, mqtt: { connected: true } } });
    const html = renderToStaticMarkup(<HealthPage />);
    expect(html).not.toContain('class="bad"');
    expect(html).toContain("אימות TLS כבוי");
    expect(html).not.toContain("0 ישויות");
  });
  it("does not treat webhook statistics or missing status as a broker connection", () => {
    resources({ "system-report": { frigate: { reachable: false, http_status: 403 }, mqtt: { webhooks: 0, queued: 0 } } });
    const html = renderToStaticMarkup(<HealthPage />);
    expect(html).toContain("Frigate HTTP 403");
    expect(html).toContain("מצב החיבור לא דווח");
  });
});

describe("HTTP ingress reviews", () => {
  it("keeps event rows as a single button with a noninteractive thumbnail", () => {
    resources({ users: { users: [{ name: "Test", slug: "test", watchlist: "black" }] },
      "activity?": { events: [{ event_id: "row-1", status: "recognized", person: "Test", camera: "Door", start_ts: 1, score: .8 }] } });
    const html = renderToStaticMarkup(<EventsPage />);
    expect(html).toContain('class="event-row blacklist-outline"');
    expect(html).toContain('<span class="event-thumb"');
    expect(html).not.toContain('<button class="event-thumb"');
    expect(html).toContain('aria-label="מתאריך"');
    expect(html).toContain('aria-label="עד תאריך"');
  });

  it("adds an explicitly chosen event face through enrollment, not the audit-label API", async () => {
    resources({ "audit/e1": { event: { status: "unknown", event_id: "e1" } },
      "faces/event/e1/references?": { candidates: [{ token: "selected-face", usable: true }] } });
    await act(() => { renderer = create(<EventModal eventId="e1" people={[{ name: "Test", slug: "test" }]} onClose={vi.fn()} />); });
    await act(() => renderer.root.findByType("select").props.onChange({ target: { value: "Test" } }));
    const open = renderer.root.findAllByType("button").find(x => x.children.includes("בחר תמונות פנים להוספה לאדם שנבחר"));
    await act(() => open.props.onClick());
    expect(api).not.toHaveBeenCalled();
    api.mockResolvedValue({ added: true, reference_count: 1, ready: true });
    await act(async () => { await renderer.root.findAllByType("button").find(x => x.children.includes("הוסף לגלריה")).props.onClick(); });
    expect(api).toHaveBeenCalledWith("faces/enroll", { method: "POST", body: JSON.stringify({ token: "selected-face", person: "test" }) });
  });

  it("can display the original backend error without crypto.randomUUID", async () => {
    vi.stubGlobal("crypto", {});
    function Trigger() { const toast = useToast(); return <button onClick={() => toast("Wait for the event to finish", "error")}>show</button>; }
    await act(() => { renderer = create(<ToastProvider><Trigger /></ToastProvider>); });
    await act(() => renderer.root.findByType(Trigger).findByType("button").props.onClick());
    expect(JSON.stringify(renderer.toJSON())).toContain("Wait for the event to finish");
  });
  it("blocks review while processing then enables it after the event report refreshes", async () => {
    const values = { "audit/e1": { event: { status: "processing", event_id: "e1" } } };
    resources(values);
    const props = { eventId: "e1", people: [{ name: "Test", slug: "test" }], onClose: vi.fn() };
    await act(() => { renderer = create(<EventModal {...props} />); });
    let button = renderer.root.findAllByType("button").find(x => x.children.includes("זה אדם לא מוכר"));
    expect(button.props.disabled).toBe(true);
    await act(() => button.props.onClick());
    expect(api).not.toHaveBeenCalled();
    expect(useResource).toHaveBeenCalledWith("audit/e1", { poll: 3000 });
    values["audit/e1"].event.status = "unknown";
    await act(() => renderer.update(<EventModal {...props} />));
    button = renderer.root.findAllByType("button").find(x => x.children.includes("זה אדם לא מוכר"));
    expect(button.props.disabled).toBe(false);
    expect(JSON.stringify(renderer.toJSON())).toContain("אינו מוסיף תמונת פנים");
  });
});


describe("shared secondary actions", () => {
  it("keeps the primary action visible and opens the others only on demand", async () => {
    const primary = vi.fn(), secondary = vi.fn();
    await act(() => { renderer = create(<ActionGroup><><button onClick={secondary}>Search</button><button className="button primary" onClick={primary}>Add person</button></></ActionGroup>); });
    expect(renderer.root.findAllByProps({ role: "menu" })).toHaveLength(0);
    expect(renderer.root.findAllByType("button").map(node => node.children.filter(x => typeof x === "string").join(""))).toContain("Add person");
    const trigger = renderer.root.findByProps({ "aria-haspopup": "menu" });
    await act(() => trigger.props.onClick());
    expect(trigger.props["aria-expanded"]).toBe(true);
    const action = renderer.root.findByProps({ role: "menuitem" });
    await act(() => action.props.onClick());
    expect(secondary).toHaveBeenCalledOnce(); expect(primary).not.toHaveBeenCalled();
    await act(() => renderer.root.findByProps({ role: "menu" }).props.onClick({ target: { closest: () => ({}) } }));
    expect(renderer.root.findAllByProps({ role: "menu" })).toHaveLength(0);
  });
  it("closes with Escape without closing the surrounding dialog", async () => {
    await act(() => { renderer = create(<ActionsMenu><button disabled>Unavailable</button><button>Settings</button></ActionsMenu>); });
    await act(() => renderer.root.findByProps({ "aria-haspopup": "menu" }).props.onClick());
    const event = { key: "Escape", currentTarget: { querySelectorAll: () => [] }, preventDefault: vi.fn(), stopPropagation: vi.fn() };
    vi.stubGlobal("document", { activeElement: null });
    await act(() => renderer.root.findByProps({ role: "menu" }).props.onKeyDown(event));
    expect(event.stopPropagation).toHaveBeenCalledOnce();
    expect(renderer.root.findAllByProps({ role: "menu" })).toHaveLength(0);
  });
});


describe("actual notification timing", () => {
  it("shows measured event and publish latency without inventing camera latency", () => {
    resources({ health: { performance: { mqtt_to_recognition: { p95_ms: 543.2 }, confirmed_to_notification: { p95_ms: 12.4 } } }, "system-report": {} });
    const html = renderToStaticMarkup(<HealthPage />);
    expect(html).toContain("543 ms"); expect(html).toContain("12 ms");
    expect(html).toContain("מקבלת האירוע ב־MQTT");
    expect(html).toContain("עד הכנסה לתור הפרסום");
  });
});


describe("transient image delivery", () => {
  function Probe({ src }) { const image = useImageRetry(src); return <img src={image.url} data-failed={image.failed} onError={image.onError} />; }
  it("retries a not-yet-persisted image without reloading the page and stays bounded", async () => {
    vi.useFakeTimers();
    await act(() => { renderer = create(<Probe src="api/activity/new/image" />); });
    await act(() => renderer.root.findByType("img").props.onError());
    expect(renderer.root.findByType("img").props["data-failed"]).toBe(true);
    await act(() => vi.advanceTimersByTime(400));
    expect(renderer.root.findByType("img").props.src).toContain("faceid_retry=1");
    await act(() => renderer.root.findByType("img").props.onError());
    await act(() => vi.advanceTimersByTime(1200));
    expect(renderer.root.findByType("img").props.src).toContain("faceid_retry=2");
    await act(() => renderer.root.findByType("img").props.onError());
    expect(vi.getTimerCount()).toBe(0);
  });
  it("cancels a pending retry when switching to a different person's image", async () => {
    vi.useFakeTimers();
    await act(() => { renderer = create(<Probe src="person/alice.jpg" />); });
    await act(() => renderer.root.findByType("img").props.onError());
    await act(() => renderer.update(<Probe src="person/bob.jpg" />));
    await act(() => vi.advanceTimersByTime(2000));
    expect(renderer.root.findByType("img").props.src).toBe("person/bob.jpg");
    expect(renderer.root.findByType("img").props["data-failed"]).toBe(false);
    expect(vi.getTimerCount()).toBe(0);
  });
});
