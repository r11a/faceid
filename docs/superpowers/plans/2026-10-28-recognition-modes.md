# Selectable recognition modes implementation plan

**Goal:** Per-camera event, person-presence, or always-on recognition with independent face sightings and immediate notifications on each new appearance.

**Architecture:** Preserve the existing event pipeline as the default. A bounded camera sampler feeds independently tracked faces through the existing quality, identity, liveness, audit and alert pipeline. Native sighting IDs keep camera-wide faces separate from Frigate object identities. Secure Access1 still requires its original verified event evidence; camera sampling never authorizes admission.

**Tech stack:** Existing Python/OpenCV/InsightFace, MQTT, FastAPI, React.

## Accepted behavior

- User confirmed `event` / `presence` / `continuous` selection per camera.
- Existing settings migrate to `event` without changing behavior.
- Sample default 0.5 seconds, configurable 0.25–5; face absence rearm default 2 seconds, configurable 1–30. Occlusion longer than this can create a new sighting. No notification for every frame of an uninterrupted appearance.
- Presence sampling follows Frigate person events/counts and reconciles active events after startup/reconnection; continuous sampling needs no person event but still reads Frigate's camera frames.
- Multiple simultaneous people receive independent consensus and sighting IDs. A new appearance bypasses the old five-minute presence notification suppression.
- At most two camera jobs in flight, one per camera, no frame backlog. Current frame fetched at execution. Model scheduling remains shared and bounded.
- Stale/duplicate frames cannot provide repeated identity confirmations. Disabled/mode-changed cameras discard in-flight results. Unknown tracks continue trying; bounded consensus history and tracking state.
- Best face image can improve after notification. Native sighting playback uses the local camera/time recording window, never a fabricated Frigate object association.

## Tasks and verification

- [x] Inspect existing sampler, decisions, alerting, camera profiles and media paths.
- [x] Add profile persistence/API/UI with migration/validation and focused tests (settings agent).
- [x] Add pure one-to-one face tracker with deterministic crossing, ambiguity, absence, invalid-data and memory-bound tests (tracking agent).
- [x] Add continuous camera scheduler and lifecycle; test presence start/end, retained counts, always-on without MQTT, fair bounded scheduling, slow/failed camera, mode changes, duplicate frames, repeated appearances, multiple faces, no access authorization, unchanged legacy path.
- [x] Add native sighting media fallback and tests; wire audit metadata resolver (media agent).
- [x] Run complete backend/frontend suites, build UI, sync addon, inspect settings UI, independent review.
- [ ] Bump version to 6.6.0, publish repository update after checks and report installation steps and live-hardware verification limits.

## Review focus

1. Identity consensus must never transfer between neighboring/crossing people.
2. Configuration changes during I/O must not emit late recognition from a disabled camera.
3. Missing end messages/reconnect must not leave presence scanning stuck or permanently stopped.
4. Continuous observation must not grow memory/disk per frame indefinitely.
5. Synthetic media IDs must not access unrelated Frigate events or bypass existing admission protections.

## Progress

Final local verification: 375 backend tests passed (3 skipped), 92 frontend tests passed, UI build passed, source/static addon mirrors byte-identical, import/version checks passed. Browser QA saved presence and continuous modes and verified persistence after reload.

Independent review fixes verified with failing-then-passing regressions: observed absence instead of sampler delay; notification cancellation when disabled during evidence I/O and retry; access-only event suppression; per-camera MQTT reconciliation; immediate policy changes with old votes discarded; known presence refresh; recognition-time recording windows and cache invalidation. Native evidence changes now refresh event/list image URLs. No outstanding critical/important review findings. Real-camera throughput and detection accuracy remain unverified on the installed hardware.
