# Full-resolution camera sampling implementation plan

**Goal:** Repair continuous recognition on low-resolution Frigate detect streams without lowering face-quality or identity thresholds.

**Evidence:** Live 6.6.0 health shows sampling and detected faces, but zero usable tracks. Office analysis is 320x240 with 7–12px faces; its main restream snapshot is 1920x1080. Frigate config confirms the detect stream dimensions. Main-stream snapshot capture currently takes about three seconds.

**Architecture:** Extend the existing per-camera high_resolution setting for native recognition modes. Resolve a configured go2rtc restream from authenticated Frigate config and capture it through Frigate's authenticated ffprobe snapshot endpoint. Keep event/access evidence on its existing path. A failed main source must be visible, not silently substituted with an old event image. Reuse the same source for camera preview/analysis. Preserve two sampler jobs and report actual source, dimensions and rejected-face reasons in the existing camera screen. Do not change Frigate configuration or security thresholds.

**Tech stack:** Existing Python requests/OpenCV, Frigate HTTP API, React.

- [x] Regression tests for source selection, timeout/failure handling, quality rejection diagnostics and camera UI.
- [x] Implement bounded main-stream capture and wire native sampler, preview and analysis.
- [ ] Run affected and full tests, build/mirror add-on, review and release 6.6.1.
- [ ] Update installed FaceID, enable full resolution for the tested Office camera, and verify live source/quality/recognition results.

Verification: 388 backend tests passed (3 skipped), 93 frontend tests passed; production UI built and browser-checked. Independent reviewer found two issues (capture contention and empty-image decoding), both fixed and regression-tested; re-review has no blockers.
