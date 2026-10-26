import React from "react";
import { act, create } from "react-test-renderer";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { FaceExplorer } from "./pages/Faces.jsx";
import { api } from "./api.js";
import { useResource } from "./hooks.js";

vi.mock("./api.js", () => ({ api: vi.fn(), assetUrl: path => `/ingress/${path}`, query: values => new URLSearchParams(values).toString() }));
vi.mock("./hooks.js", async original => ({ ...await original(), useResource: vi.fn(() => ({ data: { users: [] }, reload: vi.fn() })) }));
vi.mock("./EventVideo.jsx", () => ({ EventVideo: () => null }));
vi.mock("./ui.jsx", async original => ({ ...await original(), Modal: ({ children }) => <div>{children}</div> }));

globalThis.IS_REACT_ACT_ENVIRONMENT = true;
let renderer;
beforeEach(() => {
  api.mockReset();
  useResource.mockImplementation(() => ({ data: { users: [] }, reload: vi.fn() }));
  api.mockResolvedValue({ total: 1, cameras: ["Door"], mode: "identity", events: [{ event_id: "event/1", camera: "Door", person: "Alice", start_ts: 1700000000 }] });
});
afterEach(async () => { if (renderer) await act(() => renderer.unmount()); renderer = null; });
async function open() { await act(async () => { renderer = create(<FaceExplorer source={{ person: "alice" }} />); }); }

it("requests newest events first and preserves the results when switching to list view", async () => {
  await open();
  expect(new URLSearchParams(api.mock.calls[0][0].split("?")[1]).get("order")).toBe("desc");
  const grid = renderer.root.findByProps({ "aria-label": "תצוגת רשת" });
  expect(grid.props["aria-pressed"]).toBe(true);
  await act(() => renderer.root.findByProps({ "aria-label": "תצוגת רשימה" }).props.onClick());
  expect(renderer.root.findByProps({ "aria-label": "תצוגת רשימה" }).props["aria-pressed"]).toBe(true);
  expect(renderer.root.findByType("img").props.src).toBe("/ingress/api/activity/event%2F1/image");
  expect(api).toHaveBeenCalledTimes(1);
});

it("uses the actual activity image when an identity event has no face sample, including evidence", async () => {
  await open();
  expect(renderer.root.findByType("img").props.src).toBe("/ingress/api/activity/event%2F1/image");
  await act(() => renderer.root.findAllByType("button").find(button => button.props.className?.includes("face-appearance")).props.onClick());
  expect(renderer.root.findAllByType("img").map(image => image.props.src)).toEqual([
    "/ingress/api/activity/event%2F1/image", "/ingress/api/activity/event%2F1/image",
  ]);
});

it("keeps an existing face crop when supplied by a similarity result", async () => {
  api.mockResolvedValue({ total: 1, cameras: ["Door"], events: [{ event_id: "e1", camera: "Door", start_ts: 1700000000, image: "api/faces/sample/s1/image", similarity: .8 }] });
  await open();
  expect(renderer.root.findByType("img").props.src).toBe("/ingress/api/faces/sample/s1/image");
});

it("keeps compact identity history focused on events with filters collapsed", async () => {
  await act(async () => { renderer = create(<FaceExplorer source={{ person: "alice" }} compact />); });
  expect(renderer.root.findAllByType("h2")).toHaveLength(0);
  expect(renderer.root.findByProps({ className: "face-filter-disclosure" }).props.open).toBeFalsy();
  expect(JSON.stringify(renderer.toJSON())).not.toContain("התאמות לפי דמיון בפנים");
  expect(renderer.root.findByProps({ "aria-label": "אפשרויות היסטוריית פנים" }).props["aria-expanded"]).toBe(false);
});

it("appends older events in vertical timeline without losing visible thumbnails", async () => {
  const rows = Array.from({ length: 50 }, (_, i) => ({ event_id: `e${i}`, camera: "Door", start_ts: 1700000000-i, image: `api/faces/sample/s${i}/image` }));
  api.mockResolvedValueOnce({ total: 51, cameras: ["Door"], events: rows }).mockResolvedValueOnce({ total: 51, cameras: ["Door"], events: [{ event_id: "e50", camera: "Yard", start_ts: 1699999950, image: "api/faces/sample/s50/image" }] });
  await act(async () => { renderer = create(<FaceExplorer source={{ sample_id: "s0" }} initialView="timeline" />); });
  expect(renderer.root.findByProps({ "aria-label": "תצוגת ציר זמן" }).props["aria-pressed"]).toBe(true);
  await act(async () => renderer.root.findByProps({ "aria-label": "טען עוד אירועים" }).props.onClick());
  expect(renderer.root.findAllByType("img")).toHaveLength(51);
  expect(JSON.stringify(renderer.toJSON())).toContain("Yard");
  expect(new URLSearchParams(api.mock.calls[1][0].split("?")[1]).get("offset")).toBe("50");
  api.mockResolvedValueOnce({ total: 1, cameras: ["NewDoor"], events: [{ event_id: "new", camera: "NewDoor", start_ts: 1700000100 }] });
  await act(async () => renderer.root.findAllByType("button").find(button => button.children.includes("רענן תוצאות")).props.onClick());
  expect(new URLSearchParams(api.mock.calls[2][0].split("?")[1]).get("offset")).toBe("0");
  expect(renderer.root.findAllByType("img")).toHaveLength(1);
  expect(JSON.stringify(renderer.toJSON())).toContain("NewDoor");
});

it("can search similar faces from evidence without a sample and asks which face to use", async () => {
  await open();
  await act(() => renderer.root.findAllByType("button").find(button => button.props.className?.includes("face-appearance")).props.onClick());
  api.mockResolvedValueOnce({ samples: [{ sample_id: "s1", image: "api/faces/sample/s1/image" }, { sample_id: "s2", image: "api/faces/sample/s2/image" }] });
  await act(async () => renderer.root.findByProps({ "aria-label": "מצא דומה בציר זמן" }).props.onClick());
  expect(JSON.stringify(renderer.toJSON())).toContain("בחר את הפנים לחיפוש מתוך האירוע");
  expect(renderer.root.findAllByType("img")).toHaveLength(2);
});

it("makes missing history actionable and requests a bounded scan of retained camera images", async () => {
  useResource.mockImplementation(path => ({ data: path === "faces/index" ? { running:false, events_total:100, events_indexed:1, events_missing:99, remote_available:true } : { users:[] }, reload:vi.fn() }));
  api.mockResolvedValue({ total:1,cameras:["Door"],source_event_id:"source",events:[{event_id:"source",camera:"Door",start_ts:1700000000,similarity:1}] });
  await act(async () => { renderer=create(<FaceExplorer source={{sample_id:"s1"}} />); });
  expect(JSON.stringify(renderer.toJSON())).toContain("החיפוש בהיסטוריה חלקי");
  expect(JSON.stringify(renderer.toJSON())).toContain("תמונת החיפוש");
  await act(async () => renderer.root.findAllByType("button").find(button=>button.children.includes("השלם חיפוש בהיסטוריה")).props.onClick());
  expect(api).toHaveBeenCalledWith("faces/index?remote=true&limit=500",{method:"POST"});
});
