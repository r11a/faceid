import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, create } from "react-test-renderer";
import { EventVideo } from "./EventVideo.jsx";
import { api } from "./api.js";
import { ClipSequence } from "./pages/Home.jsx";

vi.mock("./api.js", () => ({ assetUrl: path => `/ingress/${path}`, query: () => "", api: vi.fn() }));

globalThis.IS_REACT_ACT_ENVIRONMENT = true;
let renderer;
afterEach(async () => { if (renderer) await act(() => renderer.unmount()); renderer = null; vi.useRealTimers(); api.mockReset(); });

describe("event playback", () => {
  it("retries only fresh recording readiness and stops after three automatic attempts", async () => {
    vi.useFakeTimers();
    api.mockResolvedValue({ retryable: true, message: "Recording segments not ready (HTTP 400)" });
    await act(() => { renderer = create(<EventVideo eventId="fresh" />); });
    for (let attempt = 0; attempt < 4; attempt++) {
      await act(async () => { renderer.root.findByType("video").props.onError(); });
      expect(JSON.stringify(renderer.toJSON())).toContain("HTTP 400");
      await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    }
    expect(api).toHaveBeenCalledTimes(4);
    expect(vi.getTimerCount()).toBe(0);
    expect(renderer.root.findAllByProps({ role: "alert" })).toHaveLength(1);
  });

  it("shows a permission failure without retrying it", async () => {
    vi.useFakeTimers();
    api.mockResolvedValue({ retryable: false, message: "Camera permission denied (HTTP 403)" });
    await act(() => { renderer = create(<EventVideo eventId="restricted" />); });
    await act(async () => { renderer.root.findByType("video").props.onError(); });
    await act(async () => { await vi.advanceTimersByTimeAsync(20000); });
    expect(api).toHaveBeenCalledTimes(1);
    expect(JSON.stringify(renderer.toJSON())).toContain("HTTP 403");
  });

  it("cancels a scheduled recording retry when playback is closed", async () => {
    vi.useFakeTimers();
    api.mockResolvedValue({ retryable: true, message: "Recording not ready" });
    await act(() => { renderer = create(<EventVideo eventId="closing" />); });
    await act(async () => renderer.root.findByType("video").props.onError());
    expect(vi.getTimerCount()).toBe(1);
    await act(() => renderer.unmount()); renderer = null;
    expect(vi.getTimerCount()).toBe(0);
    await act(async () => { await vi.advanceTimersByTimeAsync(20000); });
    expect(api).toHaveBeenCalledTimes(1);
  });

  it("ignores a diagnostic response that arrives after playback closes", async () => {
    vi.useFakeTimers();
    let resolveStatus;
    api.mockImplementation(() => new Promise(resolve => { resolveStatus = resolve; }));
    await act(() => { renderer = create(<EventVideo eventId="pending" />); });
    await act(() => renderer.root.findByType("video").props.onError());
    await act(() => renderer.unmount()); renderer = null;
    await act(async () => resolveStatus({ retryable: true, message: "Recording not ready" }));
    expect(vi.getTimerCount()).toBe(0);
    expect(api).toHaveBeenCalledTimes(1);
  });

  it("reports an unavailable clip once per attempt and retries without skipping direct playback", async () => {
    const unavailable = vi.fn();
    await act(() => { renderer = create(<EventVideo eventId="missing" nextEventId="next" onUnavailable={unavailable} />); });
    const fail = renderer.root.findByType("video").props.onError;
    await act(() => { fail(); fail(); });
    expect(unavailable).toHaveBeenCalledTimes(1);
    expect(renderer.root.findByProps({ role: "alert" })).toBeTruthy();
    await act(() => renderer.root.findByType("button").props.onClick());
    await act(() => renderer.root.findByType("video").props.onError());
    expect(unavailable).toHaveBeenCalledTimes(2);
  });

  it("preloads only next metadata and leaves current playback alone if that metadata is missing", async () => {
    const unavailable = vi.fn();
    await act(() => { renderer = create(<EventVideo eventId="current" nextEventId="missing-next" onUnavailable={unavailable} />); });
    expect(renderer.root.findAllByType("video")).toHaveLength(1);
    await act(() => renderer.root.findByType("video").props.onLoadedData());
    const videos = renderer.root.findAllByType("video");
    expect(videos).toHaveLength(2);
    expect(videos[1].props.preload).toBe("metadata");
    expect(videos[1].props.onError).toBeUndefined();
    expect(unavailable).not.toHaveBeenCalled();
  });

  it("skips a missing middle clip once, keeps the playing snapshot stable, and stops at the end", async () => {
    const events = [1, 2, 3].map(index => ({ event_id: `qa-${index}`, start_ts: index }));
    await act(() => { renderer = create(<ClipSequence events={events} people={[]} />); });
    await act(() => renderer.root.findAllByType("button").find(button => button.children.includes("נגן רצף")).props.onClick());
    await act(() => renderer.root.findByType("video").props.onEnded());
    expect(renderer.root.findByType("video").props.src).toContain("qa-2/clip");
    const fail = renderer.root.findByType("video").props.onError;
    await act(() => { fail(); fail(); });
    expect(renderer.root.findByType("video").props.src).toContain("qa-3/clip");
    await act(() => renderer.update(<ClipSequence events={[...events, { event_id: "new", start_ts: 4 }]} people={[]} />));
    expect(renderer.root.findAllByType("li")).toHaveLength(3);
    expect(renderer.root.findByType("video").props.src).toContain("qa-3/clip");
    expect(renderer.root.findAllByProps({ role: "status" }).some(node => node.children.join("").includes("1 קליפים"))).toBe(true);
    await act(() => renderer.root.findByType("video").props.onEnded());
    expect(renderer.root.findAllByType("video")).toHaveLength(0);
    expect(renderer.root.findAllByProps({ role: "status" }).some(node => node.children.join("").includes("הרצף הסתיים"))).toBe(true);
  });

  it("previews the newest appearance and plays a chosen thumbnail in chronological order", async () => {
    const events = [3, 1, 2].map(index => ({ event_id: `qa-${index}`, start_ts: index, camera: `Camera ${index}` }));
    const findSimilar = vi.fn();
    await act(() => { renderer = create(<ClipSequence events={events} onFindSimilar={findSimilar} />); });
    expect(renderer.root.findByProps({ className: "home-video-stage" }).findByType("img").props.src).toContain("qa-3/image");
    const entries = renderer.root.findAllByType("li");
    expect(entries.map(entry => entry.findByType("b").children.join(""))).toEqual(["Camera 1", "Camera 2", "Camera 3"]);
    expect(entries[2].findByType("button").props["aria-current"]).toBe("step");
    await act(() => renderer.root.findByProps({ className: "button primary" }).props.onClick());
    expect(renderer.root.findByType("video").props.src).toContain("qa-3/clip");
    await act(() => entries[1].findByType("button").props.onClick());
    expect(renderer.root.findByType("video").props.src).toContain("qa-2/clip");
    expect(renderer.root.findByProps({ className: "home-video-caption" }).findByType("b").children).toContain("Camera 2");
    await act(() => renderer.root.findByProps({ className: "text-button home-sequence-search" }).props.onClick());
    expect(findSimilar).toHaveBeenCalledWith(events[2]);
    await act(() => renderer.root.findByType("video").props.onEnded());
    expect(renderer.root.findByType("video").props.src).toContain("qa-3/clip");
  });

  it("starts the selected event immediately without requesting the next clip before it loads", () => {
    const html = renderToStaticMarkup(<EventVideo eventId="selected/event" nextEventId="next-event" />);
    expect(html).toContain('src="/ingress/api/activity/selected%2Fevent/clip"');
    expect(html).toContain("autoPlay");
    expect(html).toContain("playsInline");
    expect(html).toContain('preload="auto"');
    expect(html).toContain('role="status"');
    expect(html.match(/<video/g)).toHaveLength(1);
    expect(html).not.toContain("next-event");
  });
});
