import React from "react";
import { act, create } from "react-test-renderer";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { HealthPage } from "./pages/Management.jsx";
import { EventModal } from "./pages/Daily.jsx";
import { ToastProvider, useToast } from "./ui.jsx";
import { useResource } from "./hooks.js";
import { api } from "./api.js";

vi.mock("./hooks.js", () => ({ useResource: vi.fn() }));
vi.mock("./api.js", () => ({ api: vi.fn(), assetUrl: path => `/ingress/${path}`, query: () => "" }));
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
let renderer;
beforeEach(() => {
  vi.stubGlobal("window", { addEventListener: vi.fn(), removeEventListener: vi.fn(), setTimeout: vi.fn() });
  api.mockReset();
});
afterEach(async () => { if (renderer) await act(() => renderer.unmount()); renderer = null; vi.unstubAllGlobals(); });
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
