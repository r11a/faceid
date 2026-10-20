import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, create } from "react-test-renderer";
import { EventVideo } from "./EventVideo.jsx";
import { ClipSequence } from "./pages/Home.jsx";

vi.mock("./api.js", () => ({ assetUrl: path => `/ingress/${path}`, query: () => "", api: vi.fn() }));

globalThis.IS_REACT_ACT_ENVIRONMENT = true;
let renderer;
afterEach(async () => { if (renderer) await act(() => renderer.unmount()); renderer = null; });

describe("event playback", () => {
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
    await act(() => renderer.root.findByProps({ className: "button primary" }).props.onClick());
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
