import React from "react";
import { act, create } from "react-test-renderer";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SettingsPage } from "./Advanced.jsx";

const { api, resources } = vi.hoisted(() => ({ api: vi.fn(), resources: {} }));
vi.mock("../api.js", () => ({ api, assetUrl: path => `/${path}` }));
vi.mock("../hooks.js", () => ({ useResource: path => resources[path] }));
vi.mock("../ui.jsx", async original => ({ ...(await original()), useToast: () => vi.fn() }));

globalThis.IS_REACT_ACT_ENVIRONMENT = true;
let renderer;
const text = node => typeof node === "string" ? node : (node.children || []).map(text).join("");
const settings = {
  thresholds: { match_threshold: 0.5, unknown_threshold: 0.35, match_margin: 0.08, suggest_threshold: 0.4, cluster_eps: 0.55, ignore_threshold: 0.5, ignore_margin: 0.12, dedupe_threshold: 0.65 },
  ranges: { match_threshold: [0.2, 0.9], unknown_threshold: [0.1, 0.8], match_margin: [0, 0.5], suggest_threshold: [0.1, 0.9], cluster_eps: [0.3, 0.8], ignore_threshold: [0.1, 0.9], ignore_margin: [0, 0.5], dedupe_threshold: [0.5, 0.95] },
  backup: { enabled: false, hour: 3, keep: 7, dir: "" },
  max_faces_per_person: 40, trimmed_keep: 10, match_top_k: 3, min_confirmations: 2, hires_enroll: true,
};

beforeEach(() => {
  api.mockReset();
  api.mockResolvedValue({ ok: true, trimmed: 0 });
  resources.settings = { data: settings, loading: false, error: null, reload: vi.fn() };
});
afterEach(async () => { if (renderer) await act(() => renderer.unmount()); renderer = null; });

describe("settings page", () => {
  it("has one save button for the whole page, as in 6.9.2", async () => {
    await act(() => { renderer = create(<SettingsPage />); });
    expect(text(renderer.root)).not.toContain("שמירת הסעיף");
    const saves = renderer.root.findAllByType("button").filter(node => text(node).includes("שמירת כל השינויים"));
    expect(saves).toHaveLength(1);
    const input = renderer.root.findAllByType("input").find(node => node.props.max === "100" && node.props.min === "5");
    await act(() => { input.props.onChange({ target: { value: "30" } }); });
    await act(async () => { await saves[0].props.onClick(); });
    const [path, options] = api.mock.calls[0];
    expect(path).toBe("settings");
    const body = JSON.parse(options.body);
    expect(body.max_faces_per_person).toBe(30);
    expect(body.match_threshold).toBe(0.5);
    expect(body.backup_hour).toBe(3);
  });
});
