import React from "react";
import { act, create } from "react-test-renderer";
import { afterEach, expect, it, vi } from "vitest";
import { DashboardPage } from "./pages/Home.jsx";
import { useResource } from "./hooks.js";
vi.mock("./api.js", () => ({ api: vi.fn(), assetUrl: path => path, query: values => new URLSearchParams(Object.entries(values).filter(([,v]) => v !== "" && v != null)).toString() }));
vi.mock("./hooks.js", async original => ({ ...await original(), useResource: vi.fn(), useImageRetry: src => ({ url: src }) }));
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
it("refreshes native event thumbnails when better evidence updates their revision", async () => {
 const event = {event_id:"faceid-door-1",updated_ts:100,status:"recognized",camera:"Door"};
 useResource.mockReturnValue({data:{people:[],recent:[event],cameras:["Door"]},reload:vi.fn()});
 await act(() => {renderer=create(<DashboardPage navigate={vi.fn()} />);});
 expect(renderer.root.findByType("img").props.src).toBe("api/activity/faceid-door-1/image?faceid_version=100");
 event.updated_ts=101;
 await act(() => renderer.update(<DashboardPage navigate={vi.fn()} />));
 expect(renderer.root.findByType("img").props.src).toBe("api/activity/faceid-door-1/image?faceid_version=101");
});
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

it("opens person details on the latest appearance and searches from its selected event", async () => {
 const person = {slug:"alice",name:"Alice",photo:"alice.jpg",last_seen:1700000000,last_camera:"Door"};
 const events = [{event_id:"latest",sample_id:"latest-face",camera:"Door",start_ts:1700000000},{event_id:"older",sample_id:"older-face",camera:"Gate",start_ts:1699990000}];
 useResource.mockImplementation(path => ({data:path.startsWith("faces/timeline") ? {events,total:61} : {people:[person],recent:[],cameras:["Door"]},reload:vi.fn()}));
 await act(() => {renderer=create(<DashboardPage navigate={vi.fn()} />);});
 await act(() => renderer.root.findAllByType("button").find(button=>button.props.className?.includes("home-person-card")).props.onClick());
 expect(renderer.root.findByProps({className:"home-sequence-sidebar"}).findByProps({className:"home-person-summary"}).findByType("b").children).toEqual(["Alice"]);
 expect(renderer.root.findByProps({className:"home-video-stage"}).findByType("img").props.src).toContain("latest/image");
 await act(() => renderer.root.findByProps({className:"home-route"}).findAllByType("button")[0].props.onClick());
 await act(() => renderer.root.findByProps({className:"text-button home-sequence-search"}).props.onClick());
 expect(renderer.root.findByProps({"data-view":"timeline"}).props["data-source"]).toBe(JSON.stringify({sample_id:"older-face"}));
 expect(renderer.root.findAllByProps({className:"home-sequence-sidebar"})).toHaveLength(0);
});

it("keeps history paging and resets the preview to the newest event on that page", async () => {
 const person = {slug:"alice",name:"Alice",last_seen:1700000000};
 useResource.mockImplementation(path => ({data:path.startsWith("faces/timeline") ? {events:path.includes("offset=60") ? [{event_id:"previous-page",start_ts:1699900000}] : [{event_id:"latest-page",start_ts:1700000000}],total:61} : {people:[person],recent:[]},reload:vi.fn()}));
 await act(() => {renderer=create(<DashboardPage navigate={vi.fn()} />);});
 await act(() => renderer.root.findAllByType("button").find(button=>button.props.className?.includes("home-person-card")).props.onClick());
 await act(() => renderer.root.findAllByType("button").find(button=>button.children.includes("אירועים קודמים")).props.onClick());
 expect(renderer.root.findByProps({className:"home-video-stage"}).findByType("img").props.src).toContain("previous-page/image");
 expect(renderer.root.findAllByType("button").filter(button=>button.children.includes("אירועים קודמים")).every(button=>button.props.disabled)).toBe(true);
 await act(() => renderer.root.findAllByType("button").find(button=>button.children.includes("אירועים חדשים יותר")).props.onClick());
 expect(renderer.root.findByProps({className:"home-video-stage"}).findByType("img").props.src).toContain("latest-page/image");
});
