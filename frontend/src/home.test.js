import { describe, expect, it } from "vitest";
import { chronological, identityForEvent, latestPeople, clipAfter } from "./home.js";

describe("home activity", () => {
  it("orders the newest fetched events chronologically without mutating them", () => {
    const events = [{ event_id: "b", start_ts: 200 }, { event_id: "a", start_ts: 100 }];
    expect(chronological(events).map(row => row.event_id)).toEqual(["a", "b"]);
    expect(events[0].event_id).toBe("b");
  });
  it("never turns an unknown guess or a rejected identity into a known person", () => {
    const people = [{ slug: "alice", name: "Alice" }];
    expect(identityForEvent({ status: "unknown", person: "Alice" }, people)).toBeNull();
    expect(identityForEvent({ status: "recognized", person: "Alice", ground_truth: "__unknown__" }, people)).toBeNull();
    expect(identityForEvent({ status: "recognized", person: "Bob", ground_truth: "Alice" }, people)?.slug).toBe("alice");
  });
  it("orders last locations by actual last sighting, with unseen people last", () => {
    expect(latestPeople([{ slug: "a", last_seen: 3 }, { slug: "b", last_seen: 9 }, { slug: "c" }]).map(p => p.slug)).toEqual(["b", "a", "c"]);
  });
  it("advances past a failed clip and stops after the final clip", () => {
    expect(clipAfter(0, 3)).toBe(1);
    expect(clipAfter(2, 3)).toBeNull();
  });
});
