import React from "react";
import { act, create } from "react-test-renderer";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App.jsx";

const controls = vi.hoisted(() => ({ role: "admin", crashHealth: false }));
vi.mock("./api.js", () => ({ api: vi.fn() }));
vi.mock("./hooks.js", () => ({ useResource: path => ({
  loading: false, error: null,
  data: path === "health" ? { status: "ok", queue: 7 } : { name: "Operator", role: controls.role },
}) }));
vi.mock("./AlertCenter.jsx", () => ({ AlertCenter: "mock-alert-center" }));
vi.mock("./ui.jsx", () => ({ ToastProvider: ({ children }) => children, Badge: ({ children }) => children }));
vi.mock("./pages/Daily.jsx", () => ({ EventsPage: "mock-events", PeoplePage: "mock-people", ReviewPage: "mock-review" }));
vi.mock("./pages/Home.jsx", () => ({ DashboardPage: "mock-home" }));
vi.mock("./pages/Faces.jsx", () => ({ FacesPage: "mock-faces" }));
vi.mock("./pages/Access1.jsx", () => ({ Access1Page: "mock-access1" }));
vi.mock("./pages/Security.jsx", () => ({ CamerasPage: "mock-live", GuestsPage: "mock-guests", IntercomPage: "mock-intercom", LivenessPage: "mock-liveness" }));
vi.mock("./pages/Management.jsx", async () => {
  const { createElement } = await import("react");
  return { AutomationsPage: "mock-automations", HealthPage: () => {
    if (controls.crashHealth) throw new Error("Health page failed");
    return createElement("mock-health");
  } };
});
vi.mock("./pages/Advanced.jsx", () => ({ AdvancedPage: "mock-advanced", CalibrationPage: "mock-calibration", LearningPage: "mock-learning", SettingsPage: "mock-settings" }));

globalThis.IS_REACT_ACT_ENVIRONMENT = true;
let renderer, listeners, inputFocus, previousFocus;
const text = node => typeof node === "string" ? node : (node.children || []).map(text).join("");
const nav = className => renderer.root.findByProps({ className });
const namedButton = (parent, name) => parent.findAllByType("button").find(node => text(node).startsWith(name));
const click = async node => { expect(node).toBeDefined(); await act(() => node.props.onClick()); };
const dispatch = async (type, event = {}) => {
  await act(() => { for (const listener of listeners.get(type) || []) listener(event); });
};
const mount = async () => {
  await act(() => { renderer = create(<App />, { createNodeMock: node => node.type === "input" ? { focus: inputFocus } : null }); });
};
const expectPage = id => expect(renderer.root.findAllByType(`mock-${id}`)).toHaveLength(1);

beforeEach(() => {
  controls.role = "admin"; controls.crashHealth = false;
  listeners = new Map(); inputFocus = vi.fn(); previousFocus = vi.fn();
  const location = { hash: "#/home", reload: vi.fn() };
  vi.stubGlobal("location", location);
  vi.stubGlobal("window", { location,
    addEventListener: vi.fn((type, listener) => {
      if (!listeners.has(type)) listeners.set(type, new Set());
      listeners.get(type).add(listener);
    }),
    removeEventListener: vi.fn((type, listener) => listeners.get(type)?.delete(listener)),
  });
  vi.stubGlobal("document", { documentElement: { dataset: {} }, activeElement: { focus: previousFocus }, getElementById: vi.fn() });
  vi.stubGlobal("localStorage", { getItem: vi.fn(() => null), setItem: vi.fn() });
});
afterEach(async () => {
  if (renderer) await act(() => renderer.unmount());
  renderer = null;
  vi.restoreAllMocks(); vi.unstubAllGlobals();
});

describe("workspace navigation", () => {
  it("opens the Analytics-style management directory and routes each topic", async () => {
    await mount(); await click(namedButton(nav("workspace-primary-nav"), "ניהול"));
    expect(location.hash).toBe("/management");
    const directory = nav("workspace-directory-items");
    expect(directory.findAllByType("button")).toHaveLength(8);
    expect(directory.findAllByProps({ className: "workspace-category-icon tone-orange" }).length).toBeGreaterThan(0);
    await click(namedButton(directory, "בקרת כניסה")); expectPage("access1");
  });

  it("keeps every screen reachable through its workspace and expert disclosure", async () => {
    await mount();
    const groups = [
      ["פעילות", [["home", "מבט כללי"], ["review", "דורש טיפול"], ["events", "אירועים ותחקור"]]],
      ["אנשים", [["people", "אנשים"], ["faces", "גלריה וחיפוש"]]],
      ["מצלמות", [["live", "מצלמות"], ["intercom", "כניסה ואינטרקום"]]],
      ["ניהול", [["health", "תקינות המערכת"], ["access1", "בקרת כניסה"], ["guests", "אורחים"], ["automations", "אוטומציות"], ["settings", "הגדרות"]]],
    ];
    for (const [workspace, screens] of groups) {
      await click(namedButton(nav("workspace-primary-nav"), workspace));
      for (const [id, title] of screens) {
        await click(namedButton(nav("workspace-subnav"), title));
        expectPage(id); expect(location.hash).toBe(`/${id}`);
      }
    }
    await click(renderer.root.findByProps({ className: "workspace-expert-toggle" }));
    for (const [id, title] of [["liveness", "הגנה מזיוף"], ["learning", "למידה מבוקרת"], ["calibration", "כיול דיוק"], ["advanced", "כלים מתקדמים"]]) {
      await click(namedButton(nav("workspace-subnav"), title)); expectPage(id);
    }
    expect(renderer.root.findAllByType("mock-alert-center")).toHaveLength(1);
  });

  it.each([["#faces?person=alice", "faces"], ["#/faces?unknown_id=u1", "faces"], ["#/events/old-event", "events"], ["#/access1", "access1"]])("preserves legacy deep link %s", async (hash, page) => {
    location.hash = hash; await mount(); expectPage(page); expect(location.hash).toBe(hash);
  });

  it("handles browser hash navigation and discloses a directly linked expert screen", async () => {
    await mount(); location.hash = "#/calibration?camera=front";
    await dispatch("hashchange"); expectPage("calibration");
    expect(renderer.root.findByProps({ className: "workspace-expert-toggle" }).props["aria-expanded"]).toBe(true);
    expect(namedButton(nav("workspace-primary-nav"), "ניהול").props["aria-current"]).toBe("page");
    location.hash = "#/missing"; await dispatch("hashchange"); expectPage("home");
  });

  it("collapses expert navigation without leaving the active expert form", async () => {
    location.hash = "#/calibration?camera=front"; await mount();
    await click(renderer.root.findByProps({ className: "workspace-expert-toggle" }));
    expect(renderer.root.findByProps({ className: "workspace-expert-toggle" }).props["aria-expanded"]).toBe(false);
    expectPage("calibration"); expect(location.hash).toBe("#/calibration?camera=front");
    expect(namedButton(nav("workspace-subnav"), "כיול דיוק").props["aria-current"]).toBe("page");
    expect(namedButton(nav("workspace-subnav"), "הגנה מזיוף")).toBeUndefined();
    await click(renderer.root.findByProps({ className: "workspace-expert-toggle" }));
    expect(namedButton(nav("workspace-subnav"), "הגנה מזיוף")).toBeDefined();
  });

  it("retains administrative screens while backend authorization remains authoritative", async () => {
    controls.role = "viewer"; await mount();
    await click(namedButton(nav("workspace-primary-nav"), "ניהול"));
    await click(namedButton(nav("workspace-subnav"), "בקרת כניסה")); expectPage("access1");
    await click(namedButton(nav("workspace-subnav"), "הגדרות")); expectPage("settings");
  });

  it("recovers from a page failure by navigating to another screen", async () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    controls.crashHealth = true; location.hash = "#/health"; await mount();
    expect(renderer.root.findAllByProps({ className: "page-crash" })).toHaveLength(1);
    await click(namedButton(nav("workspace-primary-nav"), "פעילות")); expectPage("home");
    expect(renderer.root.findAllByProps({ className: "page-crash" })).toHaveLength(0);
  });
});

describe("screen search and theme", () => {
  it("contains Escape so an underlying editor keeps its unsaved changes", async () => {
    await mount(); await click(renderer.root.findByProps({ className: "workspace-search-toggle" }));
    const event = { key: "Escape", preventDefault: vi.fn(), stopPropagation: vi.fn() };
    await act(() => renderer.root.findByProps({ role: "dialog" }).props.onKeyDown(event));
    expect(event.stopPropagation).toHaveBeenCalledOnce();
    expect(renderer.root.findAllByProps({ role: "dialog" })).toHaveLength(0);
  });
  it("opens with Ctrl/Cmd K, focuses search, restores focus on Escape, and removes listeners", async () => {
    await mount(); const preventDefault = vi.fn();
    await dispatch("keydown", { key: "k", ctrlKey: true, preventDefault });
    expect(renderer.root.findAllByProps({ role: "dialog" })).toHaveLength(1);
    expect(inputFocus).toHaveBeenCalled(); expect(preventDefault).toHaveBeenCalled();
    await dispatch("keydown", { key: "Escape" });
    expect(renderer.root.findAllByProps({ role: "dialog" })).toHaveLength(0); expect(previousFocus).toHaveBeenCalled();
    await dispatch("keydown", { key: "K", metaKey: true, preventDefault });
    expect(renderer.root.findAllByProps({ role: "dialog" })).toHaveLength(1);
    await dispatch("keydown", { key: "k", metaKey: true, preventDefault });
    expect(renderer.root.findAllByProps({ role: "dialog" })).toHaveLength(0);
    await act(() => renderer.unmount()); renderer = null;
    expect([...listeners.values()].every(value => value.size === 0)).toBe(true);
  });

  it("searches every screen including experts and opens a matching result with Enter", async () => {
    await mount(); await click(renderer.root.findByProps({ className: "workspace-search-toggle" }));
    expect(nav("workspace-search-results").findAllByType("button")).toHaveLength(17);
    const input = renderer.root.findByType("input");
    await act(() => input.props.onChange({ target: { value: "Access1" } }));
    expect(nav("workspace-search-results").findAllByType("button")).toHaveLength(1);
    await act(() => input.props.onKeyDown({ key: "Enter", preventDefault: vi.fn() })); expectPage("access1");
    expect(renderer.root.findAllByProps({ role: "dialog" })).toHaveLength(0);
  });

  it("ranks an event screen title before a home subtitle match when pressing Enter", async () => {
    await mount(); await click(renderer.root.findByProps({ className: "workspace-search-toggle" }));
    const input = renderer.root.findByType("input");
    await act(() => input.props.onChange({ target: { value: "אירועים" } }));
    expect(text(nav("workspace-search-results").findAllByType("button")[0])).toContain("אירועים ותחקור");
    await act(() => input.props.onKeyDown({ key: "Enter", preventDefault: vi.fn() }));
    expectPage("events"); expect(location.hash).toBe("/events");
  });

  it("loads the saved theme and persists an explicit theme change", async () => {
    localStorage.getItem.mockReturnValue("light"); await mount();
    expect(document.documentElement.dataset.theme).toBe("light");
    await click(renderer.root.findByProps({ "aria-label": "מעבר למצב כהה" }));
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(localStorage.setItem).toHaveBeenLastCalledWith("faceid-theme", "dark");
  });

  it("keeps navigation and theme changes usable when local storage is blocked", async () => {
    localStorage.getItem.mockImplementation(() => { throw new Error("Storage denied"); });
    localStorage.setItem.mockImplementation(() => { throw new Error("Storage denied"); });
    await mount(); expectPage("home"); expect(document.documentElement.dataset.theme).toBe("dark");
    await click(renderer.root.findByProps({ "aria-label": "מעבר למצב בהיר" }));
    expect(document.documentElement.dataset.theme).toBe("light");
    await click(namedButton(nav("workspace-primary-nav"), "אנשים")); expectPage("people");
  });
});
