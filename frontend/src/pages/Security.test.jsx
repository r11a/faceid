import React from "react";
import { act, create } from "react-test-renderer";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CamerasPage } from "./Security.jsx";

const { api, resource } = vi.hoisted(() => ({ api: vi.fn(), resource: {} }));
vi.mock("../api.js", () => ({ api, assetUrl: path => path }));
vi.mock("../hooks.js", () => ({ useResource: () => resource }));
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
let renderer;
const text = node => typeof node === "string" ? node : (node.children || []).map(text).join("");
const camera = {
  camera: "front/door", enabled: true, role: "entry", mode: "intercom",
  min_face_px: 120, night_min_face_px: 140, burst_frames: 5,
  liveness_mode: "required", require_second_factor: true,
  fast_recognition: false, high_resolution: true, critical: false,
};
beforeEach(() => {
  api.mockReset().mockResolvedValue({});
  Object.assign(resource, { loading: false, error: null, reload: vi.fn(), data: { cameras: [{ ...camera }] } });
});
afterEach(async () => { if (renderer) await act(() => renderer.unmount()); renderer = null; });
const mount = async () => { await act(() => { renderer = create(<CamerasPage />); }); };

describe("camera controls", () => {
  it("exposes the immediate enable switch with its camera and checked state", async () => {
    await mount();
    const control = renderer.root.findByProps({ role: "switch" });
    expect(control.props["aria-label"]).toContain(camera.camera);
    expect(control.props["aria-checked"]).toBe(true);
    await act(async () => { await control.props.onClick(); });
    expect(api).toHaveBeenCalledWith("cameras/front%2Fdoor/enabled", { method: "POST", body: JSON.stringify({ enabled: false }) });
    expect(resource.reload).toHaveBeenCalledOnce();
  });

  it("saves grouped options only on save and preserves security and camera thresholds", async () => {
    await mount();
    const group = renderer.root.findByProps({ role: "group", "aria-label": "אפשרויות זיהוי" });
    const inputs = group.findAllByType("input");
    expect(inputs.map(input => input.props.checked)).toEqual([false, true, false]);
    await act(() => inputs[0].props.onChange({ target: { checked: true } }));
    await act(() => inputs[2].props.onChange({ target: { checked: true } }));
    expect(api).not.toHaveBeenCalled();
    const save = renderer.root.findAllByType("button").find(node => text(node) === "שמירת הגדרות");
    await act(async () => { await save.props.onClick(); });
    expect(api).toHaveBeenCalledWith("cameras/front%2Fdoor/profile", {
      method: "POST", body: JSON.stringify({ ...camera, fast_recognition: true, critical: true }),
    });
    expect(resource.reload).toHaveBeenCalledOnce();
  });
});
