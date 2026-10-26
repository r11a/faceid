import React from "react";
import { act, create } from "react-test-renderer";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { PeoplePage } from "./Daily.jsx";
import { useResource } from "../hooks.js";
import { api } from "../api.js";

vi.mock("../hooks.js", async original => ({ ...await original(), useResource: vi.fn() }));
vi.mock("../api.js", () => ({ api: vi.fn(), assetUrl: path => `/ingress/${path}`, query: params => new URLSearchParams(params).toString() }));
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
let renderer;
const person = { slug: "dana", name: "Dana", count: 2, state: "ready", state_label: "מוכן", statistics: { last_camera: "Entrance", last_seen: 100 } };
beforeEach(() => {
  vi.stubGlobal("window", { addEventListener: vi.fn(), removeEventListener: vi.fn() });
  useResource.mockImplementation(path => ({ data: path === "users" ? { users: [person] } : {}, loading: false, reload: vi.fn() }));
  api.mockImplementation(async path => path.startsWith("persons/") ? { gallery: { photos: 2, images: ["reference.jpg"] } } : { events: [], cameras: [], total: 0 });
});
afterEach(async () => { if (renderer) await act(() => renderer.unmount()); renderer = null; vi.unstubAllGlobals(); });

it("switches people to a list and still opens their events before reference photos", async () => {
  await act(() => { renderer = create(<PeoplePage navigate={vi.fn()} />); });
  const list = renderer.root.findByProps({ "aria-label": "תצוגת רשימה" });
  expect(list.props["aria-pressed"]).toBe(false);
  await act(() => list.props.onClick());
  expect(renderer.root.findByProps({ "aria-label": "תצוגת רשימה" }).props["aria-pressed"]).toBe(true);
  await act(async () => renderer.root.findByProps({ className: "person-main" }).props.onClick());
  const events = renderer.root.findByProps({ role: "tab", id: "person-events-tab" });
  expect(events.props["aria-selected"]).toBe(true);
  expect(renderer.root.findAllByType("img").some(image => image.props.src.endsWith("reference.jpg"))).toBe(false);
  await act(() => renderer.root.findByProps({ role: "tab", id: "person-references-tab" }).props.onClick());
  expect(renderer.root.findAllByType("img").some(image => image.props.src.endsWith("reference.jpg"))).toBe(true);
  await act(() => renderer.root.findAllByType("button").find(button => button.children.includes("הוספת תמונות ועריכה")).props.onClick());
  expect(renderer.root.findAllByType("input").some(input => input.props.type === "file")).toBe(true);
  expect(renderer.root.findAllByProps({ role: "dialog" })).toHaveLength(1);
});
