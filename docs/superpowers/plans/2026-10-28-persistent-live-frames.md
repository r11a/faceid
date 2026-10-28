# Persistent live frame implementation plan

**Goal:** Remove repeated multi-second main-stream snapshot startup from native recognition while retaining quality and identity safeguards.

**Evidence:** Live 6.6.1 main capture takes 3.5â€“4.2 seconds per image. A direct read-only RTSP probe of the existing Office restream opened in 2.47 seconds once, then delivered 152 full-resolution frames with a mean read time of 36.5 ms. DoorBell is continuous/detect-stream; Face and Front_door are presence/detect-stream. These sources still require camera-specific quality verification.

**Architecture:** A bounded pool of persistent OpenCV/FFmpeg readers drains configured Frigate restreams independently from inference and retains only the latest JPEG. Prewarm enabled native cameras using full images; person-presence mode still gates inference. No frame queue, no reused stale frames, timed opens/reads, reconnect backoff and stop on disabled/reconfigured cameras. Existing authenticated snapshots remain a visible compatibility fallback when RTSP is unavailable. Secure event/access paths remain unchanged. Use the existing high_resolution setting and camera diagnostics.

- [x] Add regression tests for freshness, replacement/coalescing, bounds, shutdown and fallback.
- [x] Implement reader pool, Frigate source integration and bounded prewarming.
- [x] Measure four real source streams without copying private images, run full tests and independent review.
- [ ] Publish/update release, enable appropriate full-image sources for Office/Face/Front_door/DoorBell, verify live source and recognition latency.

**Limits:** Initial connection can still take a few seconds. Actual recognition also depends on face size, angle, model compute and configured confirmation count. Do not reduce identity thresholds or promise a response time before measuring it.

User clarified that only the newest popup should appear: each recognition replaces the previous card immediately and restarts its five-second timer. Tests cover replacement, bursts, independent cleanup and preserved critical-mode behavior. Full validation: 401 backend tests (3 skipped), 93 frontend tests, build and mirror checks passed. Independent review accepted the media-clock backlog guard.
