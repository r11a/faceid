import React from "react";
import { act, create } from "react-test-renderer";
import { afterEach, expect, it, vi } from "vitest";
import { DashboardPage } from "./pages/Home.jsx";
import { useResource } from "./hooks.js";
vi.mock("./api.js", () => ({ api: vi.fn(), assetUrl: path => path, query: values => new URLSearchParams(Object.entries(values).filter(([,v]) => v !== "" && v != null)).toString() }));
vi.mock("./hooks.js", () => ({ useResource: vi.fn(), useImageRetry: src => ({ url: src }) }));
vi.mock("./pages/Daily.jsx", () => ({ PersonEditor: () => null }));
vi.mock("./pages/Faces.jsx", () => ({ FaceExplorer: ({source, initialView}) => <div data-source={JSON.stringify(source)} data-view={initialView} /> }));
vi.mock("./EventVideo.jsx", () => ({ EventVideo: () => null }));
vi.mock("./ui.jsx", async original => ({ ...await original(), Modal: ({children}) => <div>{children}</div> }));
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
let renderer;
afterEach(async () => { if (renderer) await act(() => renderer.unmount()); renderer = null; vi.clearAllMocks(); });
async function open() {
 useResource.mockReturnValue({data:{people:[{slug:"alice",name:"Alice",last_seen:1700000000,last_camera:"Door"}], recent:[{event_id:"e1",sample_id:"s1",person:"Alice",status:"recognized",camera:"Door",start_ts:1700000000}],cameras:["Door"],recent_total:1},reload:vi.fn()});
 await act(async () => {renderer=create(<DashboardPage navigate={vi.fn()} />);});
}
it("keeps filters collapsed and sends combined filters to the server", async () => {
 await open();
 expect(renderer.root.findAllByProps({id:"home-event-filters"})).toHaveLength(0);
 await act(() => renderer.root.findByProps({"aria-label":"סינון אירועים"}).props.onClick());
 const selects=renderer.root.findAllByType("select");
 await act(() => selects[2].props.onChange({target:{value:"black"}}));
 await act(() => renderer.root.findAllByType("select")[3].props.onChange({target:{value:"Door"}}));
 const paths=useResource.mock.calls.map(call=>call[0]);
 expect(paths.some(path=>path.includes("watchlist=black")&&path.includes("camera=Door"))).toBe(true);
 await act(() => renderer.root.findByProps({"aria-label":"סינון אירועים"}).props.onClick());
 expect(renderer.root.findAllByProps({id:"home-event-filters"})).toHaveLength(0);
});
it("opens similarity timeline from an event without searching by recognized name", async () => {
 await open();
 await act(() => renderer.root.findAllByType("button").find(b=>b.props.className?.includes("home-event-card")).props.onClick());
 await act(() => renderer.root.findAllByType("button").find(b=>b.children.includes("מצא דומה בציר זמן")).props.onClick());
 expect(renderer.root.findByProps({"data-view":"timeline"}).props["data-source"]).toBe(JSON.stringify({sample_id:"s1"}));
});
