import React from "react";
import { act, create } from "react-test-renderer";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Access1Page } from "./Access1.jsx";

const { api, resource } = vi.hoisted(() => ({ api: vi.fn(), resource: {} }));
vi.mock("../api.js", () => ({ api }));
vi.mock("../hooks.js", () => ({ useResource: () => resource }));

globalThis.IS_REACT_ACT_ENVIRONMENT = true;
let renderer;
const text = node => typeof node === "string" ? node : (node.children || []).map(text).join("");
const button = name => renderer.root.findAllByType("button").find(node => text(node) === name);
const click = async name => { await act(async () => { await button(name).props.onClick(); }); };
const enterCode = async code => {
  await act(() => renderer.root.findByType("textarea").props.onChange({ target: { value: code } }));
};
const mount = async () => {
  await act(() => { renderer = create(<Access1Page />); });
  await click("English");
};

beforeEach(() => {
  vi.useFakeTimers();
  api.mockReset();
  Object.assign(resource, {
    loading: false, error: null, reload: vi.fn(),
    data: { enabled: false, state: "disabled", instance_id: "test-installation", attempts: [] },
  });
});
afterEach(async () => {
  if (renderer) await act(() => renderer.unmount());
  renderer = null;
  vi.useRealTimers();
});

describe("Access1 pairing wizard", () => {
  it.each([
    ["idle", "Paste the setup code from Access1."],
    ["review", "Return to Access1 and press Approve connection."],
  ])("does not claim a configured connection is connected when verification returns %s", async (state, expected) => {
    resource.data.enabled = true;
    api.mockImplementation(async path => path === "access1/pairing/admin" ? { administrator: true } : { state });
    await mount();
    await click("Check connection");
    expect(api.mock.calls.map(([path]) => path)).toEqual(["access1/pairing/admin", "access1/pairing/finish"]);
    const rendered = JSON.stringify(renderer.toJSON());
    expect(rendered).not.toContain("Next in Access1: cameras in Doors; faces in user cards.");
    expect(rendered).toContain(expected);
    expect(resource.reload).not.toHaveBeenCalled();
  });

  it("keeps setup gated while the administrator check is pending and after denial", async () => {
    let rejectAdmin;
    api.mockImplementation(() => new Promise((_, reject) => { rejectAdmin = reject; }));
    await mount();
    await act(() => { button("Connect to Access1").props.onClick(); });
    expect(api.mock.calls.map(([path]) => path)).toEqual(["access1/pairing/admin"]);
    expect(renderer.root.findAllByType("textarea")).toHaveLength(0);
    expect(button("Connect to Access1").props.disabled).toBe(true);
    await act(async () => { rejectAdmin(Object.assign(new Error("PRIVATE_ADMIN_DETAIL"), { status: 403 })); });
    expect(renderer.root.findAllByType("textarea")).toHaveLength(0);
    expect(text(renderer.root.findByProps({ role: "alert" }))).toContain("Open from the HA sidebar as administrator.");
    expect(JSON.stringify(renderer.toJSON())).not.toContain("PRIVATE_ADMIN_DETAIL");
    expect(api).toHaveBeenCalledTimes(1);
  });

  it("submits only the trimmed code with the pairing header and clears it before review or expiry", async () => {
    api.mockImplementation(async path => path === "access1" ? { state: "pairing_expired" } : { state: "review" });
    await mount();
    await click("Connect to Access1");
    expect(button("Submit").props.disabled).toBe(true);
    await enterCode("  sensitive-single-use-code  ");
    await click("Submit");
    const submission = api.mock.calls.find(([path]) => path === "access1/pairing/submit");
    expect(submission[1]).toEqual({
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Access1-Pairing": "1" },
      body: JSON.stringify({ code: "sensitive-single-use-code" }),
    });
    expect(renderer.root.findAllByType("textarea")).toHaveLength(0);
    expect(JSON.stringify(renderer.toJSON())).not.toContain("sensitive-single-use-code");
    expect(text(renderer.root.findByProps({ role: "status" }))).toContain("Return to Access1 and press Approve connection.");
    await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
    expect(renderer.root.findByType("textarea").props.value).toBe("");
    expect(button("Submit").props.disabled).toBe(true);
    expect(text(renderer.root.findByProps({ role: "alert" }))).toContain("Code expired.");
  });

  it("waits for Access1 approval before showing connected and stops polling afterwards", async () => {
    let approved = false;
    api.mockImplementation(async path => path === "access1" ? { state: approved ? "ready" : "pairing_pending" } : { state: "review" });
    await mount();
    await click("Connect to Access1");
    await enterCode("fixture-code");
    await click("Submit");
    await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
    expect(text(renderer.root.findByProps({ role: "status" }))).toContain("Return to Access1");
    expect(resource.reload).not.toHaveBeenCalled();
    approved = true;
    await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
    expect(text(renderer.root.findByProps({ role: "status" }))).toContain("Next in Access1: cameras in Doors; faces in user cards.");
    expect(resource.reload).toHaveBeenCalledTimes(1);
    const count = api.mock.calls.length;
    await act(async () => { await vi.advanceTimersByTimeAsync(9000); });
    expect(api).toHaveBeenCalledTimes(count);
  });

  it.each([
    ["PRIVATE_UPSTREAM_TOKEN arbitrary response body", "Connection not completed."],
    ["Access1 pairing: stage=tls reason=certificate_verification_failed PRIVATE_UPSTREAM_TOKEN", "Connection not completed."],
    ["Access1 pairing: stage=tls reason=certificate_verification_failed", "Access1 certificate verification failed. [tls / certificate_verification_failed]"],
  ])("sanitizes a setup failure: %s", async (message, expected) => {
    api.mockImplementation(async path => {
      if (path === "access1/pairing/submit") throw new Error(message);
      return { administrator: true };
    });
    await mount();
    await click("Connect to Access1");
    await enterCode("client-fixture-code");
    await click("Submit");
    expect(text(renderer.root.findByProps({ role: "alert" }))).toContain(expected);
    expect(JSON.stringify(renderer.toJSON())).not.toContain("PRIVATE_UPSTREAM_TOKEN");
  });
});
