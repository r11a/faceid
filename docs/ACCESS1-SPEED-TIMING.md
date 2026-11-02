# Speed and timing diagnostics

DEPLOYMENT AND TIMING — FaceID 6.6.4-access1.1

Deploy
1. Back up the current HA add-on and data. Preserve /data, the enrolled gallery,
   camera settings, mappings, pairing, security configuration and TLS material.
2. Copy the CONTENTS of source/faceid-addon into the existing local FaceID add-on
   source folder. Dockerfile, config.yaml, build.yaml, requirements.txt, run.sh,
   app/ and static/ must be siblings. Include the supplied hidden .dockerignore.
   Do not uninstall the add-on or replace its runtime data. If the old build
   context issue returns, inspect any existing Dockerfile.dockerignore too.
3. Refresh local add-on information, update/rebuild, and verify the add-on/UI
   version is 6.6.4-access1.1. This package changes FaceID only; no Access1,
   mobile, HA custom-integration or controller-firmware update is required.
4. Verify camera, gallery and Access1 assignments remain as configured. Keep
   required liveness and existing access safeguards. No live opening is needed
   merely to verify installation.

Enable temporary timing diagnostics
In Home Assistant: Settings > Apps/Add-ons > FaceID > Configuration (YAML), add
or set the following TOP-LEVEL add-on option, keeping every other option:

timing_diagnostics: true

Save and restart FaceID. This switch only adds timing logs; it does not enable
any camera, access or relay action. A standalone source configuration uses
faceid.timing_diagnostics: true in its faceid configuration section instead.
The new default is false. Do not change CPU settings during the first comparison.

Measure
Use existing natural activity or a separately isolated, dispatch-disabled test
bench. Do not initiate a live door opening solely for diagnostics. Save complete
FaceID log lines for one event ID, from first timing line through its final
Access1 submission line. Compare several fast/slow events under the same camera,
lighting and host load. If an authorized live attempt is later made, HA/controller
execution and door-contact logs are needed to measure the physical opening.
FaceID logs alone cannot certify physical relay/door timings.

Read the stages (milliseconds, local monotonic durations)
- snapshot_queue_wait: waiting to start an image job.
- fresh_frame_wait: no new source timestamp / duplicate image; a reason note,
  not a measured duration and not an inference failure. Timestamp/image checks
  still block reuse even with diagnostics disabled.
- live_image_fetch: Frigate request and decode time.
- face_detection: detection and its scheduler wait on the secure-live path;
  ordinary archival paths may also compute embeddings inside this stage.
- liveness_with_queue minus liveness_inference: model scheduler wait/overhead.
- liveness_inference: crop, preprocessing and PAD inference, not just ONNX time.
- face_embedding: selected face embedding including scheduler wait.
- gallery_match: enrolled-gallery comparison (not the separate guest matcher).
- mqtt_to_recognition: local receipt of the event to confirmed recognition;
  it excludes the camera/Frigate time before FaceID receives MQTT.
- submission_queue: recognition enqueued to the Access1 worker taking it.
- access1_post: POST round trip, including receiver processing.
- access1_get: one receipt-query round trip.
- access1_result_total: POST start to final receipt classification, including
  result-queue waits. It does not delay the next submission anymore.

Stages nest/overlap: do not add them all together. Match event IDs, not names.
pending = still tracking; relay_activated = receiver execution report;
unconfirmed = insufficient confirmation; none implies a door-contact opened.
Unchanged-timestamp waits of several seconds identify source-image availability
as a separate issue; lowering liveness thresholds would not fix that safely.

After collecting logs, set timing_diagnostics: false and restart FaceID.
Detailed timing lines stop; bounded aggregate metrics remain. No cleanup of
enrolled data, event history or policies is required. Do not share credentials
or full configuration dumps with diagnostic logs.

Rollback
Restore source/faceid-addon from the previous 6.6.3-access1.1 package and rebuild.
If the old add-on schema rejects timing_diagnostics, remove only that option.
No new database schema, gallery or Access1 pairing migration is introduced.
Do not edit a pending audit row to force replay; uncertain requests stay uncertain.

FaceID 6.6.4-access1.1 — local focused speed and timing release
Base: local 6.6.3-access1.1, derived from upstream 6.6.2. 2026-10-30.

Implemented
1. The existing ordered identity worker still validates and POSTs each event
   once. Pending/uncertain receipts move to a separate bounded GET-only worker.
   A slow receipt no longer holds the submission queue. Direct synchronous
   present() callers keep their previous behaviour unless defer=True is explicit.
2. Receipt tracking retains the original evidence expiry and eight-second
   reconciliation window, with an additional monotonic deadline against clock
   rollback. No GET starts after the deadline; an in-flight HTTP call retains
   its existing transport timeout. Each job is pinned to its original transport;
   disconnect/re-pair stops reconciliation rather than redirecting it.
3. A full result queue, expired evidence or failed tracking stays unconfirmed.
   There is never a retry POST or a local access grant. A late pending display
   cannot overwrite an already terminal receipt for the same request.
4. Liveness loads its ONNX runtime before MQTT processing starts, moving model
   loading out of the first real person's observation. Failure remains blocking
   in required mode. No synthetic frame counts as liveness proof.
5. CPU-only liveness now honors the existing explicit cpu_threads and
   cpu_allow_spinning settings. Defaults and accelerated providers are unchanged.
   No new forced thread count; fewer threads are not automatically faster.
6. Optional timing_diagnostics (default false) logs event-correlated stage times.
   Existing bounded performance metrics also include the new measured stages.
   Waiting for an unchanged capture timestamp or duplicate image is explicitly
   reported, rate-limited to one note per second per event while diagnostics is on.

Preserved
Three-frame liveness configuration and all other configured frame counts;
same-subject binding, duplicate evidence checks, model weights, thresholds,
face selection, freshness/expiry, source provenance, TLS, identity mapping,
deterministic request IDs, duplicate POST prevention, single ordered POST worker,
Access1 permissions/APB/interlocks/timetables, firmware, relay behaviour, UI flows.
No motion analysis. No live application, relay or door was operated in testing.

Limits
This does not change Frigate's rate of fresh event snapshots, remove no-face
history cards, or change false-person detections. Fast-mode scheduling stays
250 ms; frame/attempt limits remain unchanged. The Access1 server itself is not
edited: POST timing includes its processing and network round trip and is NOT
a direct measurement of its authorization algorithm. Final result tracking is
separate from physical door-contact confirmation. A faster UI result is not
proof of a faster physical opening.

Offline evidence
With a fake receiver imposing an 800 ms receipt delay, the second POST started
at 1382.91 ms in the baseline and 141.52 ms in this build (local host timings,
including test database/filesystem overhead). This proves queue isolation, not
end-to-end HA performance. Local synthetic-input PAD warm median was 6.98 ms
with defaults and 11.60 ms with 2 threads/no spinning. Accordingly no default
CPU tuning is imposed. Model-load time moved to startup, not eliminated.
See benchmark.log for exact measurements and regression.log for final checks.
