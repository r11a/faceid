import React from "react";
import { act, create } from "react-test-renderer";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CamerasPage, LiveFrame } from "./Security.jsx";

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
  recognition_mode: "event", recognition_interval_seconds: .5, recognition_rearm_seconds: 2,
};
beforeEach(() => {
  api.mockReset().mockResolvedValue({});
  Object.assign(resource, { loading: false, error: null, reload: vi.fn(), data: { cameras: [{ ...camera }] } });
});
afterEach(async () => { if (renderer) await act(() => renderer.unmount()); renderer = null; });
const mount = async () => { await act(() => { renderer = create(<CamerasPage />); }); };

describe("camera controls", () => {
  it("shows the live source, resolution and reason small faces cannot be recognized", async () => {
    resource.data.cameras[0].recognition_mode = "presence";
    resource.data.performance = { camera_sampling: { cameras: { "front/door": {
      state: "faces_rejected", source: "detect_stream", frame_width: 320, frame_height: 240,
      visible_faces: 2, usable_faces: 0, largest_face_px: 12, min_face_px: 48,
      rejected_reasons: { small_face: 2 }, frame_processing_ms: 340,
    } } } };
    await mount();
    const content = renderer.toJSON().map(text).join("");
    expect(content).toContain("320×240");
    expect(content).toContain("12px");
    expect(content).toContain("48px");
    expect(content).toContain("פנים קטנות מדי");
  });

  it("saves the selected recognition mode and timing without changing security settings", async () => {
    await mount();
    const selector = renderer.root.findByProps({ "aria-label": "מתי לזהות פנים" });
    expect(selector.findAllByType("option").map(option => option.props.value)).toEqual(["event", "presence", "continuous"]);
    await act(() => selector.props.onChange({ target: { value: "presence" } }));
    const interval = renderer.root.findByProps({ "aria-label": "מרווח בין בדיקות (שניות)" });
    const rearm = renderer.root.findByProps({ "aria-label": "היעדרות פנים לפני הופעה חדשה (שניות)" });
    await act(() => interval.props.onChange({ target: { value: "0.75" } }));
    await act(() => rearm.props.onChange({ target: { value: "3" } }));
    expect(api).not.toHaveBeenCalled();
    const save = renderer.root.findAllByType("button").find(node => text(node) === "שמירת הגדרות");
    await act(async () => { await save.props.onClick(); });
    expect(JSON.parse(api.mock.calls[0][1].body)).toEqual({ ...camera, recognition_mode: "presence", recognition_interval_seconds: .75, recognition_rearm_seconds: 3 });
  });

  it("offers fast recognition only for event mode and restores its saved preference", async () => {
    resource.data.cameras[0].fast_recognition = true;
    await mount();
    const selector = renderer.root.findByProps({ "aria-label": "מתי לזהות פנים" });
    const fastInput = () => renderer.root.findAllByType("label").find(node => text(node).includes("זיהוי מהיר"))?.findByType("input");
    expect(fastInput().props.checked).toBe(true);
    for (const value of ["presence", "continuous"]) {
      await act(() => selector.props.onChange({ target: { value } }));
      expect(fastInput()).toBeUndefined();
    }
    await act(() => selector.props.onChange({ target: { value: "event" } }));
    expect(fastInput().props.checked).toBe(true);
  });

  it("defaults old cameras to event mode and reloads settings when selecting a camera", async () => {
    delete resource.data.cameras[0].recognition_mode;
    resource.data.cameras.push({ ...camera, camera: "yard", recognition_mode: "continuous", recognition_interval_seconds: 1 });
    await mount();
    expect(renderer.root.findByProps({ "aria-label": "מתי לזהות פנים" }).props.value).toBe("event");
    const yard = renderer.root.findAllByType("button").find(node => text(node).startsWith("yard"));
    await act(() => yard.props.onClick());
    expect(renderer.root.findByProps({ "aria-label": "מתי לזהות פנים" }).props.value).toBe("continuous");
    expect(renderer.root.findByProps({ "aria-label": "מרווח בין בדיקות (שניות)" }).props.value).toBe(1);
  });

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

describe("stream observation settings", () => {
  it("makes stream sources event-only while retaining snapshot mode choices", async () => {
    resource.data.cameras[0].recognition_mode = "continuous";
    await mount();
    await act(() => renderer.root.findByProps({"aria-label":"Observation source"}).props.onChange({target:{value:"frigate_restream"}}));
    let mode = renderer.root.findByProps({"aria-label":"מתי לזהות פנים"});
    expect(mode.props.disabled).toBe(true);
    expect(mode.props.value).toBe("event");
    await act(async () => renderer.root.findAllByType("button").find(node => text(node) === "שמירת הגדרות").props.onClick());
    expect(JSON.parse(api.mock.calls[0][1].body).recognition_mode).toBe("event");
    await act(() => renderer.root.findByProps({"aria-label":"Observation source"}).props.onChange({target:{value:"snapshots"}}));
    mode = renderer.root.findByProps({"aria-label":"מתי לזהות פנים"});
    expect(mode.props.disabled).toBe(false);
    expect(mode.props.value).toBe("continuous");
  });
  it("defaults to snapshots and requires explicit trust", async () => {
    await mount();
    const source=renderer.root.findByProps({"aria-label":"Observation source"});
    expect(source.props.value).toBe("snapshots");
    await act(() => source.props.onChange({target:{value:"frigate_restream"}}));
    expect(renderer.root.findByProps({"aria-label":"Trust local video network"}).props.checked).toBe(false);
    expect(api).not.toHaveBeenCalled();
  });
  it("retains a saved direct secret when the masked input is empty", async () => {
    Object.assign(resource.data.cameras[0],{observation_source:"direct_rtsp",trust_local_video_network:true,direct_rtsp_configured:true});
    await mount();
    expect(renderer.root.findByProps({"aria-label":"Direct RTSP address"}).props.value).toBe("");
    const save=renderer.root.findAllByType("button").find(node=>text(node)==="שמירת הגדרות");
    await act(async()=>save.props.onClick());
    const body=JSON.parse(api.mock.calls[0][1].body);
    expect(body.direct_rtsp_url).toBeUndefined();expect(body.clear_direct_rtsp).toBeUndefined();
    expect(api.mock.calls[0][1].headers["X-Access1-Pairing"]).toBe("1");
  });
  it("shows explicit clear only after changing away from direct source", async () => {
    Object.assign(resource.data.cameras[0],{observation_source:"direct_rtsp",direct_rtsp_configured:true});
    await mount();
    expect(renderer.root.findAllByProps({"aria-label":"Clear saved RTSP address"})).toHaveLength(0);
    await act(()=>renderer.root.findByProps({"aria-label":"Observation source"}).props.onChange({target:{value:"snapshots"}}));
    await act(()=>renderer.root.findByProps({"aria-label":"Clear saved RTSP address"}).props.onChange({target:{checked:true}}));
    await act(async()=>renderer.root.findAllByType("button").find(node=>text(node)==="שמירת הגדרות").props.onClick());
    expect(JSON.parse(api.mock.calls[0][1].body).clear_direct_rtsp).toBe(true);
  });
});


describe("live size calibration", () => {
  afterEach(() => vi.useRealTimers());
  it("serializes measurement and ignores a response after changing camera", async () => {
    vi.useFakeTimers();
    let finish;
    api.mockImplementation(() => new Promise(resolve => { finish = resolve; }));
    await act(() => { renderer = create(<LiveFrame camera="door" controls />); });
    await act(() => renderer.root.findByType("button").props.onClick());
    expect(api).toHaveBeenCalledTimes(1);
    await act(async () => vi.advanceTimersByTimeAsync(2000));
    expect(api).toHaveBeenCalledTimes(1);
    const oldFinish = finish;
    const signal = api.mock.calls[0][1].signal;
    await act(() => renderer.update(<LiveFrame camera="yard" controls />));
    expect(signal.aborted).toBe(true);
    await act(async () => oldFinish({ preview:"old-image",width:320,height:240,faces:[],min_face_px:48 }));
    expect(renderer.root.findByType("img").props.src).not.toBe("old-image");
    await act(async () => finish({ preview:"data:image/jpeg;base64,NEW",width:640,height:480,
      faces:[{box:[10,20,110,120],face_px:100,usable:true}],min_face_px:80 }));
    expect(renderer.root.findByType("img").props.src).toBe("data:image/jpeg;base64,NEW");
    expect(text(renderer.toJSON())).toContain("100 px");
    await act(() => renderer.root.findByType("button").props.onClick());
    const count = api.mock.calls.length;
    await act(async () => vi.advanceTimersByTimeAsync(1000));
    expect(api).toHaveBeenCalledTimes(count);
  });
  it("resumes preview after stopping before the first measurement completes", async () => {
    api.mockImplementation(() => new Promise(() => {}));
    await act(() => { renderer = create(<LiveFrame camera="door" controls />); });
    const first = renderer.root.findByType("img").props.src;
    await act(() => renderer.root.findByType("button").props.onClick());
    await act(() => renderer.root.findByType("button").props.onClick());
    expect(api.mock.calls[0][1].signal.aborted).toBe(true);
    expect(renderer.root.findByType("img").props.src).not.toBe(first);
  });
  it("requests the next preview only after image completion", async () => {
    vi.useFakeTimers();
    await act(() => { renderer = create(<LiveFrame camera="door" controls />); });
    const first = renderer.root.findByType("img").props.src;
    await act(async () => vi.advanceTimersByTimeAsync(1000));
    expect(renderer.root.findByType("img").props.src).toBe(first);
    await act(() => renderer.root.findByType("img").props.onLoad({currentTarget:{naturalWidth:640,naturalHeight:480}}));
    await act(async () => vi.advanceTimersByTimeAsync(500));
    expect(renderer.root.findByType("img").props.src).not.toBe(first);
    expect(api).not.toHaveBeenCalled();
  });
});
