import React from "react";
import { act, create } from "react-test-renderer";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SetupPage, setupApi } from "./Setup.jsx";

const { api, resources } = vi.hoisted(() => ({ api: vi.fn(), resources: {} }));
vi.mock("../api.js", () => ({ api, assetUrl: path => `/${path}` }));
vi.mock("../hooks.js", () => ({ useResource: path => resources[path] }));
vi.mock("../ui.jsx", async original => ({ ...(await original()), useToast: () => vi.fn() }));
vi.mock("./Security.jsx", () => ({ LiveFrame: "mock-live-frame" }));
vi.mock("./Daily.jsx", () => ({ PeoplePage: "mock-people" }));
vi.mock("./Access1.jsx", () => ({ Access1Page: "mock-access1" }));

globalThis.IS_REACT_ACT_ENVIRONMENT = true;
let renderer;
const text = node => typeof node === "string" ? node : (node.children || []).map(text).join("");
const buttons = () => renderer.root.findAllByType("button");
const button = name => buttons().find(node => text(node).includes(name));
const click = async name => { await act(async () => { await button(name).props.onClick(); }); };
const resource = data => ({ data, loading: false, error: null, reload: vi.fn() });

const discovery = {
  system: { gpu_device: true, backend: "openvino_gpu" },
  mqtt: { host: "core-mosquitto", port: 1883, configured: true, user_set: true, password_set: true },
  frigate: { url: "https://ccab4aaf-frigate:8971", configured: true, reachable: true, verify_tls: false, topic_prefix: "frigate",
    topic_prefix_matches: true, username_set: true, password_set: true,
    cameras: [{ camera: "Enterance", restream: "Enterance", detect: { width: 1280, height: 720 }, used_by_faceid: true, warnings: [] },
      { camera: "Balcony", restream: null, detect: { width: 640, height: 360 }, used_by_faceid: false, warnings: ["no_restream"] }] },
  manual_fallback: false,
};
const status = { state: "migrated", cameras: [{ camera: "Enterance", status: "validated", changes: [] }], rollback_available: false, setup_mode: [] };
const effective = {
  setup_mode: [], connection: { frigate: { url: "https://f" }, mqtt: { host: "core-mosquitto" } },
  engine: { active_backend: "openvino_gpu", native_device: "GPU.0" }, gallery: { persons: 4, reference_photos: 42 },
  access1: { enabled: true, state: "ready" },
  cameras: { Enterance: { enabled: true, box_association: true, decode_source: "global", decode: { gpu_decode: true, decode_width: 1920, decode_step: 3 } },
    Balcony: { enabled: false, box_association: true, decode_source: "global", decode: { gpu_decode: true, decode_width: 1920, decode_step: 3 } } },
};

const mount = async () => { await act(() => { renderer = create(<SetupPage navigate={vi.fn()} />); }); };

const storage = new Map();
beforeEach(() => {
  storage.clear();
  vi.stubGlobal("sessionStorage", { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value) });
  api.mockReset();
  api.mockResolvedValue({});
  Object.assign(resources, {
    "setup/status": resource(status), "setup/discover": resource(discovery), "setup/effective": resource(effective),
    "setup/walk-test": resource({ active: false, camera: null, results: [] }), health: resource({ persons: 4 }),
  });
});
afterEach(async () => { if (renderer) await act(() => renderer.unmount()); renderer = null; });

describe("setup wizard", () => {
  it("sends setup writes with the setup confirm header", async () => {
    await setupApi("apply", { cameras: {} });
    expect(api).toHaveBeenCalledWith("setup/apply", { method: "POST",
      headers: { "Content-Type": "application/json", "X-FaceID-Setup": "1" }, body: JSON.stringify({ cameras: {} }) });
  });

  it("walks through the six steps in order", async () => {
    await mount();
    const steps = renderer.root.findByProps({ "aria-label": "שלבי ההגדרה" }).findAllByType("button").map(text);
    expect(steps).toEqual(["1. חיבור", "2. בחירת מצלמות", "3. בדיקת תמונה", "4. רישום אנשים", "5. Access1 (אופציונלי)", "6. סקירה ובדיקה"]);
    expect(text(renderer.root)).toContain("מחובר: https://ccab4aaf-frigate:8971");
    await click("הבא: בחירת מצלמות");
    expect(text(renderer.root)).toContain("אין Restream ב־Frigate");
  });

  it("turns a camera on through the existing camera switch", async () => {
    await mount();
    await click("2. בחירת מצלמות");
    const toggle = renderer.root.findByProps({ "aria-label": "זיהוי פנים ב־Balcony" });
    await act(async () => { await toggle.props.onClick(); });
    expect(api).toHaveBeenCalledWith("cameras/Balcony/enabled", { method: "POST", body: JSON.stringify({ enabled: true }) });
  });

  it("probes, recommends and saves only after confirmation; validates the view", async () => {
    api.mockImplementation(path => Promise.resolve(path.endsWith("/probe")
      ? { ok: true, codec: "hevc", width: 2592, height: 1944, fps: 25,
          recommendation: { gpu_decode: true, decode_width: 1920, decode_step: 3, delivered_fps: 8.3, notes: [] } }
      : { applied: ["camera_profiles.json"], restart_required: false }));
    await mount();
    await click("3. בדיקת תמונה");
    await click("בדיקת הזרם");
    expect(api.mock.calls.some(([path]) => path === "setup/apply")).toBe(false);
    expect(text(renderer.root)).toContain("HEVC · 2592×1944 · 25 fps");
    await click("שמור המלצה");
    expect(JSON.parse(api.mock.calls.find(([path]) => path === "setup/apply")[1].body))
      .toEqual({ cameras: { Enterance: { gpu_decode: true, decode_width: 1920, decode_step: 3 } } });
    await click("אשר מצלמה");
    expect(api.mock.calls.some(([path]) => path === "setup/cameras/Enterance/validate")).toBe(true);
  });

  it("shows the live picture with face measurement for the chosen camera", async () => {
    await mount();
    await click("3. בדיקת תמונה");
    const frame = renderer.root.findByType("mock-live-frame");
    expect(frame.props).toEqual({ camera: "Enterance", controls: true });
  });

  it("keeps enrollment and Access1 pairing inside the wizard and remembers the step", async () => {
    await mount();
    await click("4. רישום אנשים");
    expect(renderer.root.findAllByType("mock-people")).toHaveLength(1);
    expect(button("הקודם")).toBeDefined();
    await click("הבא: Access1");
    expect(renderer.root.findAllByType("mock-access1")).toHaveLength(1);
    await act(() => renderer.unmount()); renderer = null;
    await mount();
    expect(renderer.root.findAllByType("mock-access1")).toHaveLength(1);
  });

  it("falls back to manual values when the stream cannot be probed", async () => {
    api.mockImplementation(path => Promise.resolve(path.endsWith("/probe") ? { ok: false, reason: "timeout", manual: true } : {}));
    await mount();
    await click("3. בדיקת תמונה");
    await click("בדיקת הזרם");
    expect(text(renderer.root)).toContain("לא ניתן לבדוק את הזרם (timeout)");
  });

  it("runs the walk test on enabled cameras and shows that Access1 was not asked", async () => {
    resources["setup/walk-test"] = resource({ active: true, camera: "Enterance", remaining_seconds: 90,
      results: [{ event_id: "e1", person: "David Levy", score: 0.75, liveness_confirmed: true, recognized_after_seconds: 2.6 }] });
    await mount();
    await click("6. סקירה ובדיקה");
    const options = renderer.root.findAllByType("option").map(text);
    expect(options).toEqual(["Enterance"]);
    expect(text(renderer.root)).toContain("David Levy · 75%");
    expect(text(renderer.root)).toContain("בקשה ל־Access1 לא נשלחה");
    await click("עצור");
    expect(api.mock.calls.some(([path]) => path === "setup/walk-test/stop")).toBe(true);
  });

  it("asks for a Home Assistant administrator when the server refuses", async () => {
    resources["setup/status"] = { data: null, loading: false, error: Object.assign(new Error("x"), { status: 403 }), reload: vi.fn() };
    await mount();
    expect(text(renderer.root)).toContain("נדרש מנהל Home Assistant");
  });
});

describe("setup wizard 6.9.3", () => {
  const site = { format: "faceid-site", version: 1, settings: { stream_warm_seconds: 60 }, cameras: {} };
  const choose = async () => {
    const input = renderer.root.findByProps({ type: "file" });
    await act(async () => { await input.props.onChange({ target: { files: [{ text: async () => JSON.stringify(site) }] } }); });
  };

  it("says on step 3 that measurement still lets the door open", async () => {
    await mount();
    await click("3. בדיקת תמונה");
    expect(text(renderer.root)).toContain("מדידה בלבד: הדלת עדיין נפתחת בזיהוי רגיל.");
  });

  it("shows a walk test banner with stop on every step while it runs", async () => {
    resources["setup/walk-test"] = resource({ active: true, camera: "Enterance", remaining_seconds: 70, results: [] });
    await mount();
    await click("3. בדיקת תמונה");
    expect(text(renderer.root)).toContain("בדיקת הליכה פועלת ב־Enterance: הדלת לא תיפתח");
    await click("עצור בדיקה");
    expect(api).toHaveBeenCalledWith("setup/walk-test/stop", expect.objectContaining({ method: "POST" }));
  });

  it("previews a site file and applies it only after confirmation", async () => {
    api.mockImplementation((path, options) => Promise.resolve(path === "setup/import"
      ? (JSON.parse(options.body).apply
        ? { applied: ["settings.json"], restart_required: true, changes: true }
        : { changes: true, settings: { stream_warm_seconds: { from: 0, to: 60 } }, cameras: {}, skipped: ["Garage"], warnings: [], applied: [] })
      : {}));
    await mount();
    await click("6. סקירה ובדיקה");
    await choose();
    const imports = () => api.mock.calls.filter(([path]) => path === "setup/import").map(([, options]) => JSON.parse(options.body));
    expect(imports()).toEqual([{ site, apply: false }]);
    expect(text(renderer.root)).toContain("stream_warm_seconds: 0 ← 60");
    expect(text(renderer.root)).toContain("לא נמצאו ב־Frigate ולא יובאו: Garage");
    await click("החל את השינויים");
    expect(imports()[1]).toEqual({ site, apply: true });
    expect(button("הפעל מחדש את FaceID")).toBeDefined();
  });

  it("reports when an imported site file changes nothing", async () => {
    api.mockImplementation(path => Promise.resolve(path === "setup/import"
      ? { changes: false, settings: {}, cameras: {}, skipped: [], warnings: [], applied: [] } : {}));
    await mount();
    await click("6. סקירה ובדיקה");
    await choose();
    expect(text(renderer.root)).toContain("אין שינויים: ההגדרות כבר זהות.");
    expect(button("החל את השינויים")).toBeUndefined();
  });
});
