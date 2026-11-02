# FaceID 6.6.12-access1.stream3 — integrated local-stream observation

Frigate-triggered observation now supports the existing Frigate restream or an explicitly
configured private RTSP source. Existing cameras remain on snapshots by default.
Camera settings require explicit local-network trust before stream evidence can authorize.
At least three distinct, same-person live confirmations are required. Capture time is not
claimed to be authenticated; Access1 policies and connector authentication remain unchanged.

Automatic recognition and Access1 remain active while diagnostics run. Manual diagnostic
results never authorize access. Operational sessions may activate the disconnected test relay.
The retained window now includes the actual operational timeline and private input previews.

- [English installation, commissioning and rollback](INSTALL-AND-COMMISSION-EN.txt)
- [התקנה, בדיקות וחזרה למצב הקודם בעברית](INSTALL-AND-COMMISSION-HE.txt)
- [Developer handoff](DEVELOPER-HANDOFF.txt)

This release is ready for rig validation, not proven faster. Complete the comparative
20-approach gate before calling commissioning finished. Historical notes below do not
supersede these source/trust/diagnostic rules.

## Historical production notes
The following notes describe earlier production releases, retained as reference.

# Local embedding release 6.6.5-access1.1

FaceID 6.6.5-access1.1 — focused embedding/scheduler maintenance release
Local fork based on 6.6.4-access1.1 / upstream 6.6.2. 2026-10-30.

Changes
- One CPU recognition-model warmup inference runs before camera workers start.
  It uses a synthetic zero image through the installed model's get_feat()
  preprocessing. It is not an event or a face, and never updates identity,
  gallery, liveness, confirmations, notifications or Access1. Startup takes the
  warmup time instead of the first visitor paying that cold-inference cost.
  Accelerated providers are left unchanged. A failed warmup logs a categorical
  warning; real inference still has to succeed normally.
- embed_face now rechecks the existing embedding after acquiring its lock.
  Two concurrent callers sharing the same face object do not calculate it twice.
  No embeddings are shared across different face objects/events/frames.
- Event-correlated embedding_scheduler_wait and embedding_compute timings split
  the existing face_embedding duration. Compute still calls the same installed
  model.get(), so it includes alignment, preprocessing and model execution.
  No replacement alignment or recognition implementation was introduced.
- Bounded per-engine timing counters report p 50/p 95 wait and compute time,
  failure count and startup warmup status through the existing health report.
- Secure-live sampling no longer repeatedly enqueues work whose snapshot
  timestamp is missing, stale, future, before the event, or already consumed.
  These jobs previously reached the same execution gate and returned without
  useful image work. Execution-time gates remain intact to handle races.
  Fresh valid timestamps resume sampling; notification-only sampling is unchanged.

Preserved
Model weights, FP 32 precision, library versions, face alignment, gallery math,
similarity thresholds, liveness model and required frame count, same-identity
and duplicate-evidence guards, source provenance, expiry, TLS/security settings,
Access1 mappings/permissions/APB/interlocks/timetables, POST deduplication and
the prior separate receipt worker. No motion analysis or live door operations.
No automatic CPU setting change: saved cpu_threads=2 remains 2 after upgrading.

Measured evidence and limits
Your successful event: 8.04 s MQTT-to-recognition; about 4.87 s inside embedding
across three frames; about 51 ms liveness; 565 ms Access1 result round trip.
Your startup log confirms CPUExecutionProvider, cpu_threads=2, spinning=true.
Local synthetic-input FP 32 benchmark: two threads median 208.65 ms versus four
100.36 ms. Eight local CPUs were visible in that initial benchmark.
Separate old/new path comparison under a four-CPU Docker quota: at two threads,
first embedding 336.67 -> 157.91 ms; at four threads 105.31 -> 108.35 ms (no cold
benefit demonstrated there). Warm medians at four threads were about 98 ms.
Outputs were bit-identical for the tested synthetic inputs. These small local
benchmarks do not measure biometric accuracy or guarantee your HA latency.
The benchmark used the public Immich buffalo_l recognition artifact, hash
4c 06341c 33c 2ca 1f 86781dab 0e 829f 88ad 5b 64be 9fba 56e 56bc 9ebdefc 619e 43;
your installed file's hash was not collected. No downloaded model is packaged
or substituted for your existing model. See benchmark logs and deployment guide.

DEPLOYMENT — FaceID 6.6.5-access1.1

1. Back up the current FaceID add-on and runtime data/configuration.
2. Copy the CONTENTS of source/faceid-addon into the current local FaceID add-on
   source folder. Dockerfile, config.yaml, build.yaml, requirements.txt, run.sh,
   app/ and static/ must be siblings. Include hidden .dockerignore. Do not replace
   /data, gallery, pairing, mappings, certificates or saved settings.
3. Refresh/rebuild/update the local add-on. Verify version 6.6.5-access1.1 in HA
   and FaceID. No Access1/firmware/mobile/custom-integration update is required.
4. Keep timing_diagnostics: true during the comparison. Keep all liveness,
   recognition, camera and access policy settings unchanged.
5. First inspect the new timings with your existing CPU setting (currently 2).
   The startup log should include embedding startup warmup state=ready with a
   duration, then the usual MQTT startup. No warmup identity/event is created.

Read the new timing lines for ONE event ID
embedding_scheduler_wait: time waiting to acquire the existing model scheduler.
embedding_compute: alignment, preprocessing and model.get() while holding it.
face_embedding: outer total, including those two stages and small bookkeeping.
Do not add nested timings together. Failed computations also record timings.
An already populated face does not run inference again; an ordinary new frame
still needs its own embedding and all the existing identity/liveness checks.
The existing performance/engine health report contains bounded aggregate stats.

If scheduler wait dominates, inspect competing/background model work before
changing thread counts. If compute dominates, test the existing CPU option below.
capture_timestamp_missing_or_stale / unchanged_capture_timestamp means no new
eligible image is available. Repeated useless jobs are now skipped rather than
queued/audited every sampler tick; no freshness condition is relaxed.

Controlled CPU comparison for your installation
Your log confirms cpu_threads: 2 and cpu_allow_spinning: true. If HA's VM has at
least four vCPUs available, compare four threads using the EXISTING add-on option:

cpu_threads: 4
cpu_allow_spinning: true
timing_diagnostics: true

Merge these values into the existing HA add-on configuration; do not replace the
whole configuration with this snippet. Save/restart and verify startup actually
says threads=4 applied=True. This option applies to CPU detection/recognition
and the existing liveness runtime; it is not an embedding-only setting. No new
configuration path is added. Keep backend and every other setting unchanged.
If fewer than four vCPUs are available, keep two for now. Do not change VM CPU
allocation as part of this pass. If startup still says 2, stop and inspect the
effective configuration; do not delete settings.json or reset the gallery.

Compare several natural events or a dispatch-disabled isolated bench under the
same lighting/camera/load. Use embedding_compute, scheduler wait, detection,
liveness and mqtt_to_recognition. Compare typical AND slow attempts; monitor
HA/Frigate responsiveness too. No live door operation was performed by this work.
Do not trigger a live door solely to benchmark the package. Any authorized real
access test can operate the configured relay, just as it did before.

Keep four only if it improves your actual timings without harming other services.
Otherwise restore cpu_threads: 2. Leave spinning=true for this first comparison
so only one variable changes. No guaranteed twofold HA speedup is claimed.
Once sufficient evidence is collected, set timing_diagnostics: false and restart.
Detailed logs stop; bounded aggregate metrics remain. No runtime data cleanup.

Rollback
Restore the previous 6.6.4-access1.1 source/faceid-addon and rebuild. Restore the
previous CPU setting if changed. There is no database, gallery, pairing or policy
migration in this release. No new mandatory configuration field was added.

Separate observation
Your supplied startup log contains an unverified-Frigate-HTTPS warning. This
performance package does not alter TLS or the saved secure-mode choice. Review
that configuration separately; timing results do not establish transport security.

---
Previous release documentation follows.

# Local speed/timing release 6.6.4-access1.1

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

---
Previous release documentation follows.

# Local maintenance release 6.6.3-access1.1

FaceID 6.6.3-access1.1 — focused local maintenance release
Base: supplied upstream FaceID 6.6.2. Date: 2026-10-30.

Changes
- Accept Access1 native relay_activated, blocked and expired_unconfirmed receipts.
- Keep execution confirmation separate from physical door-open confirmation.
- Retain safe backend reason codes and categorical transport/result errors.
- An uncertain POST is still never replayed: existing bounded GET reconciliation
  and deterministic request identity/deduplication remain unchanged.
- Reject non-finite or wrong-shaped liveness model output as unavailable.
- Bind secure-live liveness evidence to a sufficiently matched identity; reset
  accumulated proof and identity votes on a subject change. Duplicate embeddings
  cannot add live proof. Continue binding individually live samples during
  recovery from an earlier failed sample. Ordinary and continuous monitoring
  paths are unchanged. This does not add hardware depth/IR or certify spoof resistance.
- Normal individually-live warmup is INFO; failed samples remain WARNING.
- Explicit minimal add-on .dockerignore prevents inherited exclude-all rules
  from hiding the deployment source. Version labels identify this local fork.

Unchanged
Model weights, score thresholds, required live-frame count, camera acquisition,
freshness, TLS, queue bounds, Access1 authorization, timetables, APB, interlocks,
relay pulse settings, gallery and pairing schemas. No firmware/integration/mobile
update is needed. No live doors were operated during preparation.

Known/deferred audit items
Advisory mode is not a substitute for required liveness on an access camera.
Continuous monitoring still needs the separate secure Frigate access path and
its existing fast_recognition/full-image settings. UI readiness, attempt-budget
validation, intercom diagnostic frame-source/deduplication, global boolean option
encoding and the second-factor UI scope are not changed in this focused pass.
Keep the currently working required-liveness camera configuration.
Capture speed is not changed; no performance improvement is claimed.


DEPLOYMENT — FaceID 6.6.3-access1.1

1. Keep a current HA backup containing FaceID data and configuration. Preserve
   gallery, /data, access1.json, access1-connector.json, security choices and TLS
   material. Do not uninstall FaceID or delete its runtime data.
2. Copy CONTENTS of source/faceid-addon into the existing local FaceID add-on
   SOURCE folder. Dockerfile, config.yaml, build.yaml, requirements.txt, run.sh,
   app/ and static/ must be siblings. Do not introduce an extra nested folder.
3. Include hidden .dockerignore, replacing the stale source exclusion file that
   caused the earlier 2-byte-context failure. Inspect any Dockerfile.dockerignore
   already on HA: none is supplied by this release and its rules can override
   .dockerignore. Preserve a copy outside the build folder before removing a
   stale Dockerfile-specific ignore file.
4. Refresh local add-on information and update FaceID. Check Supervisor build
   logs if the update fails. Verify version 6.6.3-access1.1 in the add-on and UI.
5. Confirm existing camera, gallery and Access1 mappings/settings remain intact.
   Inspect existing results or use a dispatch-disabled bench for initial checks.
   Do not trigger a live door solely to validate this package.

Result meanings
relay_activated = controller reports relay execution; it does NOT establish that
the door physically opened. door_open_confirmed remains a separate value.
blocked/denied = receiver/controller rejected the request; inspect reason.
accepted = receiver accepted the request, not necessarily physical opening.
expired_unconfirmed/unconfirmed = execution is not established; do not resend
automatically. access1_http_NNN identifies the HTTP failure without private data.
Normal liveness 1/3 and 2/3 are progress, not failed checks.

Rollback
Restore the previous complete add-on source bundle and supported HA backup if
required. No database migrations or data deletions are introduced by this pass.
Never delete pairing or gallery data as a build troubleshooting step.


# FaceID App

## What appears in Home Assistant

FaceID uses MQTT discovery. After the app starts, every enrolled person gets a separate
device with entities for last location, last-seen time, recent presence, appearances
today, average confidence and total appearances. Open **Settings → Devices & services →
MQTT → Devices** and search for the person's name.

The last-location entity also exposes 7/30-day statistics, the most frequent camera,
confidence scores and a camera breakdown as attributes. Restart FaceID once after
updating so the new discovery messages are published immediately.

Face recognition for [Frigate](https://frigate.video): confirmed people are published
to MQTT (sensors appear automatically), optionally written back as `sub_label`, and
unknown faces land in a review UI (side panel) where you assign them with one click.

Full documentation: https://github.com/r 11a/faceid

## Setup

1. Prefer `frigate_url: https://FRIGATE_IP:8971`, with `frigate_username` and
   `frigate_password` set to a dedicated Frigate viewer. Leave
   `frigate_verify_tls` enabled for a trusted certificate. Port 5000 remains a
   compatibility option for a tightly restricted internal network only.
2. MQTT: leave `mqtt_host` empty to automatically use the Mosquitto broker app.
   Fill the `mqtt_*` options only for an external broker.
3. Optional: restrict processing to specific cameras (`cameras`), and list the cameras
   that should get a `sensor.faceid_<camera>` in Home Assistant (`discovery_cameras`).
4. Start the app. The first start downloads the recognition model (~300 MB) —
   check the app log until you see `MQTT verbunden`.
5. Open the **FaceID** panel in the sidebar. Recommended first step: run the backfill
   (see main README) or just wait — every detected unknown face shows up for review.

## First 5.3 check

Upload one clear single-person photo and one group photo in **People → Known people**.
The single photo should be accepted normally. When the group cannot be matched with a
clear lead, FaceID must show the photo with a numbered frame around every eligible face
and save nothing until you choose one. In **Cameras → Intercom**, enable high-resolution
capture only for the entrance camera; it uses the configured Frigate API and does not
require exposing go 2rtc port 1984.

Open **Guests** to create a time/camera/count-limited temporary pass, and open
**Investigation → Site map** to position cameras and connect plausible transitions.
From **Visits**, use **Play clips by route** to review the matching Frigate clips in
chronological order. A guest face match is only eligibility evidence: liveness and a
second factor remain mandatory before any door-control automation acts.

1. Open **Daily operation → Users**, create a test person and add 5–10 clear photos.
   Rejected photos show the reason instead of silently weakening the gallery.
2. Open **Daily operation → Intercom**, select the entrance camera, save Intercom mode
   and run **Test current capture** while someone stands at the normal door distance.
3. Open **Daily operation → Liveness**. Keep distant cameras on Advisory and set a
   close entrance/intercom camera to Required. Test once with a real person and once
   with a printed or phone-displayed photograph.
4. In **System → Health**, verify Frigate authentication/TLS, persistent storage and
   schema 5. A pre-migration backup is created automatically before the first upgrade.

## Options

| Option | Description |
|---|---|
| `frigate_url` | Base URL of your Frigate instance |
| `frigate_username` / `frigate_password` | Credentials for authenticated port 8971 |
| `frigate_verify_tls` | Verify the HTTPS certificate (recommended) |
| `known_evidence_days` / `unknown_evidence_days` | Review-image retention |
| `liveness_enabled` | Enable local RGB print/screen presentation-attack checks |
| `liveness_threshold` | Real-vs-spoof score threshold; higher is stricter and rejects more real faces |
| `liveness_required_frames` | Consecutive live frames needed in Required mode |
| `access_control_enabled` | Future role foundation; keep `false` in 5.0 unless assignments were configured manually |
| `mqtt_*` | Leave empty to use the internal Mosquitto app automatically |
| `backend` | `auto`, `cpu`, `cuda` or `openvino`; explicit modes fail fast when unavailable |
| `match_threshold` | ≥ this cosine similarity = recognized (raise if strangers get misassigned) |
| `unknown_threshold` | < this = definitely unknown |
| `match_margin` | required lead over the runner-up before a match can count |
| `min_confirmations` | distinct agreeing frames required before publishing |
| `min_face_quality` | reject weak size, blur, lighting and pose evidence |
| `clip_analysis` / `clip_max_*` | sample diverse faces from the finished recording |
| `clip_retry_attempts` / `clip_retry_seconds` | retry a Frigate clip that has not finished preparing; a readable clip with no face is not retried |
| `cluster_eps` | how aggressively unknown faces are grouped in the review UI |
| `presence_window` | camera sensor lists everyone seen within this many seconds |
| `recognition_session_seconds` | continuous same-person/same-camera sightings stay one visit; default 300 seconds |
| `visit_gap_minutes` | gap after which repeated detections count as a new visit |
| `calibration_target_far` | false-accept target for calibration recommendations |
| `scenario_window` | maximum gap between identity-linked cross-camera events |
| `camera_graph_json` | JSON camera adjacency map, e.g. `{"front":["hall"]}` |
| `reid_*` | short-lived clothing hint; it never becomes a face identity verdict |
| `automation_cooldown` | duplicate suppression for versioned automation events |
| `video_decode` | `auto`, software, VAAPI or CUDA; actual backend/fallback appears in Health |
| `body_enabled` | enable the trained body-consensus advisor (off until reviewed and trained) |
| `body_threshold` | minimum body classifier probability; calibration may raise it |
| `body_confirmations` | distinct events required before body consensus is shown |
| `body_consensus_window` | seconds in which body confirmations are grouped |
| `vision_advisor_enabled` | optional local Vision investigation path; never identity authority |
| `webhook_urls` | optional HTTP endpoints receiving the same final v 1 event |
| `ai_*` | optional local Ollama-compatible context and semantic search |
| `set_sub_label` | opt in to writing confirmed names back to Frigate events |
| `ignore_margin` | ignore match must beat the best enrolled person by this much |
| `audit_retention_days` | days to keep the SQLite decision history (0 = forever) |
| `known_evidence_max` / `unknown_evidence_max` | hard image caps; metadata remains after an image is pruned |
| `review_queue_max_total` / `review_queue_max_per_cluster` | hard limits for representative verification crops |
| `review_queue_retention_days` / `review_queue_dedupe_days` | age and similarity window for the verification inbox |
| `cameras` | process only these cameras (empty = all) |
| `discovery_cameras` | cameras that get a Home Assistant sensor |
| `suggest_threshold` | score at which unknown faces are grouped into a "looks like <person>" suggestion |
| `max_faces_per_person` | photo cap per person; the most redundant reference is set aside when exceeded (restorable) |
| `trimmed_keep` | how many set-aside photos to keep per person (0 = delete immediately) |
| `dedupe_threshold` | default sensitivity for the Settings "Remove duplicates" action |
| `hires_enroll` | fetch new review-queue faces from the recording instead of the detect snapshot (sharper references) |
| `frigate_topic_prefix` | must match `mqtt.topic_prefix` in Frigate's own config (default `frigate`). Wrong value = FaceID hears nothing at all |
| `poll_interval` | seconds; >0 also polls Frigate's event API for events MQTT never announces (e.g. events created by an automation from a camera's own detection). 0 = off |
| `backup_enabled` / `backup_hour` / `backup_keep` | optional built-in daily gallery backup |

Thresholds and backup can also be changed live on the app's **Settings** tab; those
edits are stored in the app's data volume and override these options. The Settings tab
additionally holds **"photos averaged per match"** (`match_top_k`) — a match score is the
mean of that many best-fitting reference photos. Higher resists a single lucky photo;
lower helps people whose references cover many different angles, since their own less
similar photos otherwise drag the mean down.

Face data (gallery, review queue) is stored in the app's data volume and survives
updates. Uninstalling the app deletes it.
