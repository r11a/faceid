# FaceID 6.6.11-access1.test1 — operational test build

Automatic recognition and the Access1 connector are restored according to saved
settings. Opening or running diagnostics does not pause cameras. Normal recognition
can activate configured relays; diagnostic results themselves never authorize access.

Restream tests now reject invalid/stale frames before inference and avoid an extra
post-inference delay. The original production access-evidence path is unchanged.

[English/Hebrew installation and testing guide](docs/ACQUISITION-DIAGNOSTICS.txt).

## Earlier release documentation (historical)

# FaceID 6.6.10-access1.diag5 — diagnostic only / אבחון בלבד

Automatic recognition and Access1 submissions do not start in this build. Saved
settings and mappings are preserved. Restore a production package after testing.
זיהוי אוטומטי ושליחה ל־Access1 אינם פעילים. ההגדרות והמיפויים נשמרים.

1. Settings → Acquisition diagnostic → Enterance → Private preview. Wait 12 seconds and inspect the startup sequence. If still grey, download the report before selecting a crop.
2. Select a crop on the image around the expected face position.
3. After cooldown, choose Crop comparison + benchmarks; remain visible for 45 seconds.
4. Allow up to 30 seconds for isolated CPU benchmarks, then download the JSON.

[Complete English/Hebrew installation and test guide](docs/ACQUISITION-DIAGNOSTICS.txt).

## Historical production notes
The following notes describe earlier production releases, not diagnostic startup.

# Local embedding release 6.6.5-access1.1

Focused CPU recognition warmup, embedding wait/compute diagnostics, and safe stale-job filtering. Existing CPU settings and all recognition/access safeguards are preserved. See [deployment and timing guide](docs/EMBEDDING-SCHEDULER-TIMING.md).

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

# FaceID — self-hosted face recognition for Frigate + Home Assistant

## FaceID 6.3: faster operation and explicit camera enrollment

- **New person from cameras:** open Needs Review, select clear faces of one person,
  choose New User, enter a name and choose **Assign & Learn**. The result reports
  accepted references, duplicates and rejected images. Assign Only confirms history
  without training. A reference enables initial matching; additional viewpoints and
  an actual recognition check are still recommended before operational use.
- **Already assigned events:** open the person and select reference faces from their
  appearance history. Multiple faces require an explicit choice. Expired images
  cannot be recovered unless Frigate still has the snapshot.
- **Fast recognition:** enable Fast Recognition in the selected camera's settings.
  This reduces waiting between Frigate snapshot updates; it does not lower decision
  thresholds or guarantee subsecond recognition. Identical snapshots are ignored.
  Live jobs and inference have priority over clip work. Health shows measured
  inference and queue timings; these are not camera-to-door measurements.
- **Video:** clicking a detection opens its clip immediately. Cached clips use local
  byte-range serving; other clips stream from Frigate. Source availability, codec and
  network conditions still determine startup time. Only the next clip metadata is
  prefetched during a sequence.
- **Alerts and lists:** camera settings include Critical Camera; person editing
  includes Notify On Recognition and normal/white/black classification. Confirmed
  blacklisted identities receive a red outline. These lists do not grant or deny
  physical access. In-app popups and the bell show persisted recognition alerts.
- **Home Assistant:** MQTT discovery creates a Last Alert sensor with person,
  camera, reasons and event ID as attributes. Phone push is optional; configure
  `blueprints/automation/faceid/recognition_alert.yaml` with your notification service.
- InsightFace is now pinned to 2.1, with the same buffalo_l recognition model,
  preprocessing size and decision policy. A real-model compatibility test is opt-in
  (`FACEID_ENGINE_MODEL_ROOT`); site performance/accuracy require on-site measurement.

## FaceID 6.1: photo search and camera timelines

Update to **6.1.1 or newer** if the Home Assistant app stopped starting after 6.1.0. It
corrects camera-graph configuration encoding without resetting data or links.

Open **Gallery & Search** to upload a photo, choose one face if several people are
present, and see similar enrolled people plus chronological appearances across
cameras. Search is temporary and does not add anyone to the gallery. **Add to
gallery** explicitly enrolls the selected face into an existing or new person.

Opening a person shows their reference photos and confirmed history. **Find
appearances** on an unknown face searches by similarity without assigning a name.
Use camera/date filters and the similarity slider to refine the timeline; open an
event to play its Frigate clip. Similarity is not proof of identity.

New observations are indexed automatically. Retained old images are indexed in the
background at startup, or with **Scan available historical images**. Deleted images
cannot be reconstructed. Face crops share the configured evidence retention policy;
vectors stay with the audit history and are removed when that history is purged.

**New in 6.5.6:** confirmed identity notifications and HA automation events publish before event completion and media/index work. [Latency and MQTT guide](docs/recognition-latency.md).

**New in 6.5.0:** HOMEii workspace with activity, people, cameras and management; local Hebrew typography, light/dark themes and Ctrl/Cmd+K screen search. [Interface guide](docs/interface.md).

**Access1 connection is available; 6.4.11 adds live capture and status updates.** Pair through HA ingress using a
one-time code from compatible Access1, then manage all people/reader mappings and
permissions in Access1. Requests require fresh authenticated camera evidence and
confirmed liveness. Secure defaults ON; only an HA administrator may explicitly
approve its certificate-admission exception. See [setup and recovery](docs/access1.md).

## FaceID 6.0: a focused product interface for homes and offices

6.0 replaces the legacy single-file dashboard with a maintainable React interface.
The permanent task-based navigation keeps Home, review, investigation, people and
cameras immediately visible; calibration, body recognition, gallery maintenance,
Frigate library sync, privacy and logs live in a clearly labelled expert area. The
same layout becomes a touch-friendly drawer and bottom-sheet workflow on phones.

Every existing operational workflow has a home: bounded unknown review, visual
camera thresholds, intercom and liveness checks, guest access, Home Assistant entities,
calibration, safe backup/restore, curated Frigate import/export, editable site map and
sequential visit clips. The UI uses versioned immutable bundles while the HTML entry
point is always served `no-store`, preventing stale Home Assistant WebViews.

## FaceID 5.1: security and site intelligence

5.1 adds temporary guest passes with strict liveness/second-factor boundaries, a
drag-and-drop camera site map, anonymous Frigate person-traffic analytics and a visit
player that plays recognition clips in camera order. Map locations are explicitly
estimated, and face recognition alone never authorizes a door unlock.

Version 5 adds a dedicated **Users** area for normal day-to-day operation. Create a
person, upload 5–10 photos, see a plain-language quality result for each photo, rename
the person or remove their profile/history without entering the technical gallery.
Nothing from the advanced gallery, calibration or investigation tools was removed.

The new **Intercom** mode focuses an entrance camera on high-resolution, close face
captures and gives live feedback for face size, sharpness and light. It is an evidence
source, not an access-control credential: never unlock a door from a face match alone;
combine it with a trusted phone, PIN or explicit approval.

The **Liveness** area reduces presentation attacks from printed photographs and phone
screens. A small local MiniFAS ONNX model evaluates face texture and several consecutive
frames must agree. Intercom profiles require liveness by default; distant cameras begin
in advisory mode because RGB anti-spoofing is less reliable with small faces. RGB PAD is
not a substitute for depth/IR hardware or a second factor against advanced attacks.

5.0 also introduces automatic pre-migration backups, a persistent schema marker and a
disabled-by-default foundation for future Admin/Operator/Viewer permissions by tab.
The permission editor and enforcement are intentionally deferred until the identity
mapping can be validated safely on the target Home Assistant installation.

## FaceID 3.1: camera studio and visits

Version 3.1 adds a visual per-camera studio. It proxies the current Frigate frame
without exposing Frigate credentials, lets an operator preview and save a minimum
face size, and shows how that threshold would affect recent real events. The saved
threshold is applied by the recognition pipeline to new events for that camera.

Recognized events are also grouped into visits so one person standing in view does
not look like many separate arrivals. Mark cameras as entry, exit, entry/exit,
observation or restricted in the camera studio. FaceID only calls an arrival or
departure confirmed when the configured camera role supports that conclusion.
Home Assistant receives a 30-day visit sensor per enrolled person; raw recognition
counts remain available separately as evidence totals.

Version 3 adds three deliberately separated evidence paths: authoritative ArcFace
identity, human-reviewed DINOv 2 body appearance with multi-event consensus, and an
optional local Vision advisor. A face decision or explicit operator review establishes
identity; body and Vision are supporting signals and must never unlock a door alone.

Open **Expert tools → Body recognition** in the web UI to review Frigate person crops, approve or
reject body material, train the local model and run a historical three-way test. Start
with at least three varied full-person images per resident and add confirmed stranger
examples. Both `body_enabled` and `vision_advisor_enabled` remain off by default.

The image bundles a checksum-pinned DINOv 2-small ONNX model and FFmpeg. Video decode
can be `auto`, `software`, `vaapi` or `cuda`; Health reports the backend that really
worked and every fallback instead of presenting configured acceleration as fact.

**Recommended Frigate connection:** use `https://FRIGATE_IP:8971` with a dedicated
Frigate `viewer` account. FaceID logs in server-side and renews the session; credentials
and media are never exposed to the browser. Keep TLS verification enabled with a
trusted certificate. Port `5000` remains compatible, but it is Frigate's unauthenticated
internal API and should not be exposed on a LAN or the internet.

FaceID is a small, self-hosted service that adds **reliable, trainable face recognition**
on top of [Frigate](https://frigate.video). It uses the same model family as Immich and
CompreFace (**InsightFace `buffalo_l`**: SCRFD detection + ArcFace embeddings) and was
built because Frigate's built-in face recognition UX didn't cut it:

The 1.0 pipeline evaluates diverse, quality-ranked faces from the finished Frigate
clip, keeps a durable decision audit, measures false accepts/rejects from operator
labels, and emits conservative cross-camera scenarios. Optional local AI adds searchable
security context, but neither AI nor clothing appearance is allowed to declare identity.

- **No train-tab treadmill.** Matching is nearest-neighbor over face embeddings — every
  image you assign is a visible reference point, with no training cycles and no queue
  that refills with already-known faces. To be clear: this is not immune to bad data —
  an imbalanced or mislabeled gallery still degrades matching (a person with many
  reference images wins borderline matches more often). The difference is that the
  failure mode is an image you can see and delete, not an opaque model update.
- **Strangers are first-class.** Unknown faces are collected, **auto-clustered** (DBSCAN,
  the same trick photo apps use) and reviewed in a web UI: one click assigns a whole
  cluster to a person — or **ignores** it. People you enroll (the mailman you *want*
  notifications for) get recognized; people you ignore go permanently silent.
- **An ignore list that actually sticks.** Ignored faces stay as *negative anchors*:
  never notified, never matched to your family, never resurfacing in review — and
  FaceID learns their new looks over time (only on unambiguous matches with a clear
  margin over every enrolled person, visibly marked "auto", deletable anytime).
  Anchors are grouped per person; groups can be merged, curated, or released into a
  real person with one click if you change your mind.
- **Train from anywhere, without bloat.** Assign faces from your cameras, upload photos
  from your photo library, or enroll whole folders via CLI. A per-person photo cap keeps
  galleries lean: when it's exceeded, the reference **most similar to the rest** is set
  aside (so unusual angles are kept, not lost) — visibly, on the person card, where you
  can restore it. The cap is adjustable in the Settings tab.
- **Home Assistant native.** MQTT discovery creates sensors per camera and a device for
  every enrolled person: last location, last-seen time, recent presence, appearances
  today, average confidence and total appearances. Rich attributes include 7/30-day
  counts, most frequent camera and a camera breakdown.
- **Optional Frigate write-back.** Confirmed names can be written as `sub_label`, so you can
  filter clips by person in Frigate's Explore view — including retroactively: the history
  scan tags past events, and assigning a face in the review UI tags its original event too.
- **Yours to keep.** A Settings tab holds the matching thresholds (live-editable) plus
  one-click **backup & restore** of your gallery, and an optional built-in **daily
  auto-backup** — your hand-curated face data is the one irreplaceable thing, so it's
  easy to safeguard.

## Screenshots

*All faces below are AI-generated (StyleGAN) — no real persons.*

**Unknown review** — new faces arrive auto-clustered; assign a whole cluster with one
click, or scan your camera history to bootstrap the gallery:

![Unknown review with auto-clustered faces](docs/screenshot-unknowns.png)

**Persons** — your gallery; upload photos or send faces back to review:

![Person gallery](docs/screenshot-persons.png)

## How it works

```
Frigate --MQTT frigate/events--> FaceID
   ^                                |  several distinct person snapshots
   |                                v
   +--optional sub_label------ InsightFace -> top-2 gallery matches
                                    |
        score + runner-up margin + repeated agreement -> recognized
        below unknown threshold                       -> unknown
        otherwise                                     -> ambiguous review

MQTT -> Home Assistant:  sensor.faceid_<camera>  +  faceid/event (JSON)
```

## Activity, calibration and automation

The **Activity** tab shows each final decision, evidence, scenario and optional context.
Each row includes the captured face and an enlarged preview, so labels are based on
visual evidence. Label complete events as a known person or **Unknown person**. The **Calibration** tab
then measures TAR, FAR and FRR by camera and person and recommends a threshold/margin
for the configured false-accept target. It intentionally keeps all frames from one event
together; random frame splitting would produce misleadingly optimistic accuracy.

Immediate automation consumers should use `faceid/v 1/recognitions`; enriched completed events remain on `faceid/v 1/events`. The legacy `faceid/event` topic
remains for compatibility. The v 1 payload contains a schema version, final decision,
evidence, scenario and any appearance hint. Home Assistant device triggers are
discovered automatically, and standalone installs can configure asynchronous webhooks.

Optional AI context uses an Ollama-compatible `/api/generate` and `/api/embed` service
for factual scene descriptions, tags and searches such as “red backpack”. It is never
used for recognition, Frigate `sub_label`, or ground truth.

### Home Assistant entities per person

After FaceID reconnects to MQTT, each enrolled person appears as a separate device under
**Settings → Devices & services → MQTT**. For a person whose slug is `alice`, discovery
creates:

| Entity | Example | Useful for |
|---|---|---|
| `sensor.faceid_alice_last_location` | `Front Door` | Where the person was last seen |
| `sensor.faceid_alice_last_seen` | timestamp | “Seen in the last 10 minutes” automations |
| `binary_sensor.faceid_alice_presence` | on/off | Short presence window after recognition |
| `sensor.faceid_alice_today` | `4` | Daily dashboard counters |
| `sensor.faceid_alice_average_confidence` | `73.2 %` | Monitoring recognition quality |
| `sensor.faceid_alice_appearances` | `128` | Total finalized recognition events |

The last-location sensor attributes also contain `appearances_7_days`,
`appearances_30_days`, `most_seen_camera`, `camera_breakdown`, `last_score` and active
cameras. Entity IDs may receive a numeric suffix if an older entity with the same ID
already exists; use the entity shown in Home Assistant rather than assuming the exact ID.

## Local-only, and what gets downloaded

FaceID performs **all recognition locally on your hardware** — no cloud APIs, no
accounts, no telemetry. The only thing ever fetched from the internet is the open-source
recognition model itself, once, on first start:

- **What:** InsightFace `buffalo_l` model pack (SCRFD face detection + ArcFace
  recognition, the same open models Immich and CompreFace use)
- **From where:** the official [InsightFace GitHub release](https://github.com/deepinsight/insightface/releases/tag/v 0.7)
- **Size:** ~300 MB, cached on disk afterwards (survives restarts and app updates)

After that download, core recognition works completely offline. Your camera images and
face data never leave your machine. If AI context is enabled, frames are sent only to
the explicitly configured Ollama-compatible URL; use a local address to keep all data
on your network.

## Requirements

- Frigate 0.16+ (snapshot + sub_label APIs), reachable over HTTP
- An MQTT broker (the one Frigate already uses is fine)
- A CPU with AVX (any Intel/AMD from the last decade; ~1.5 GB RAM; no GPU needed).
  **Running HAOS/your host in a VM?** The default virtual CPU model (e.g. Proxmox `kvm 64`)
  hides AVX — set the VM CPU type to `host` and cold-restart the VM, or the recognition
  runtime will refuse to start.

## Install as a Home Assistant app (recommended for HAOS)

*(Apps were formerly known as apps.)*

1. Add this repository to your app store — one click:

   [![Add repository to my Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fr 11a%2Ffaceid)

   (or manually: **Settings → Apps → App Store → ⋮ → Repositories** → add
   `https://github.com/r 11a/faceid`)
2. Install the **FaceID** app, set your Frigate URL in the options (MQTT is picked up
   automatically from the Mosquitto app) and start it.
3. Open the **FaceID** panel in the sidebar. First start downloads the model (~300 MB).

The app is built locally on your machine (amd 64/aarch 64). See
[faceid-addon/DOCS.md](faceid-addon/DOCS.md) for all options.

## Install standalone (LXC, VM, bare metal)

Tested on Debian 12/13 and Ubuntu 22.04+; any Linux with Python 3.10+ works.

**1. System packages**

```bash
apt install python 3-venv python 3-dev build-essential libglib 2.0-0 libgl 1 libxcb 1 libgomp 1
```

**2. Get the code and install the Python environment**

```bash
git clone https://github.com/r 11a/faceid /opt/faceid
cd /opt/faceid
python 3 -m venv venv
venv/bin/pip install -r requirements.txt
```

**3. Configure**

```bash
cp docs/example-config.yaml config.yaml
nano config.yaml   # set: Frigate URL, MQTT host + credentials, your camera names
```

**4. Run as a service**

```bash
cp faceid.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now faceid
```

**5. Verify**

The first start downloads the model pack (~300 MB, one time — see above). Follow the
log with `journalctl -u faceid -f` until you see `MQTT verbunden (Success)`, then open
`http://<host>:8600` and check that the header shows your person/queue counters.

## Getting started

1. **Scan your camera history** (optional but recommended): in the Unknown tab, click
   **"Scan camera history"** — faces from past Frigate events land in the review queue,
   pre-clustered per person, and already-known people are tagged in Frigate retroactively.
   (CLI alternative: `venv/bin/python -m app.backfill --days 14`)
2. **Assign clusters** in the UI (**Needs review**): pick a name per cluster — select individual
   tiles first if a cluster contains a stray face. The ⛶ button shows the full snapshot
   for context.
3. Once a few people exist, use **"apply suggestions"** to bulk-assign everything the
   gallery already recognizes with ≥ 50 % similarity. Repeat as the gallery grows.
4. Optionally upload 5–10 clear photos per person (Persons tab) as clean anchors, or
   enroll a folder: `venv/bin/python -m app.enroll "Alice" /path/to/photos`.

## Ignoring people

Not everyone deserves a notification. FaceID distinguishes three actions on an unknown
face, and the difference matters:

| Action | Meaning |
|---|---|
| **Assign** | This is someone I track — recognize, notify, tag in Frigate |
| **Ignore** | I know who this is and never want to hear about them — silent forever |
| **Discard** | Garbage crop (blurry, not a face) — delete, no memory kept |

Ignored faces become negative anchors, grouped by person in the **Ignored** tab:

- Reappearances are silently dropped (a log line is all you get), and genuinely new
  looks are **auto-learned** into the right group — guarded so a household member can
  never silently become an anchor (requires high similarity *and* a clear margin over
  every enrolled person; auto anchors are marked and deletable). Opt out with
  `ignore_learning: false`.
- **Curate groups**: merge two groups when they're the same person, move selected
  anchors between groups, or send a face back to the review queue.
- **Change your mind**: release a whole group into an existing or brand-new person —
  the anchors become that person's reference gallery and tracking starts immediately.
- You can also ignore an entire enrolled person via **"ignore person"** on their card.

**Training tips:** camera snapshots beat photo-library images (same lens, angle and light
as at recognition time). Diversity beats volume. Create dedicated persons for regular
strangers (mailman, neighbors) instead of discarding them — that keeps them from being
force-matched to your family.

## Sharper reference photos

Frigate runs detection on a **downscaled** stream (often 1280 × 960 or even 640 × 360) but
records in **full camera resolution** (e.g. 2560 × 1920). Snapshots come from the detect
stream, so faces arrive smaller and softer than they need to be.

With **Settings → Sharper reference photos** enabled (default), a face heading for the
review queue is re-fetched from the *recording* instead: measured across a dozen real
events, faces came out roughly **twice as large** (e.g. 84px → 178px). Better references
mean better recognition — and, as a bonus, similarity scores spread out, making genuine
duplicates easy to tell from "same person, other angle".

Details: FaceID downloads the event clip once and scans frames across it, because Frigate
picks its snapshot from a moment that can't be queried afterwards. Every candidate face
is compared against the snapshot face and the best identity match wins — so with several
people in frame, the wrong face can't be enrolled. When no frame yields a usable face
(roughly one event in three), the original snapshot is kept.

Live recognition still uses the fast snapshot path; only enrollment takes the slower,
sharper route (a few seconds and one clip download per event). Needs recordings enabled
for the camera.

### Recovering missed events

Sometimes the detect snapshot holds no usable face at all and the event is skipped
entirely. The history scan can go back over those via the clip:

```bash
python -m app.backfill --days 28 --rescue
```

About one in five such events yields a face this way. Because there is no snapshot face
to check identity against, the only guard is detection quality — and it has to be strict:
in a measured run, finds below ~0.8 were overwhelmingly back-of-head shots, motion blur
and false positives (a church spire scored 0.57), while finds above it were real faces.
The default is `--rescue-min-det 0.85`; lowering it multiplies the queue faster than it
adds usable references.

Expect a clip download (8–32 MB) and several seconds per affected event, so this is a
manual run, not something the live pipeline does.

**Keep `--days` within your recording retention.** Rescue needs the clip, so events older
than `record.retain` yield nothing and only cost time — a 28-day run against a 10-day
retention spends most of its hour on events it cannot help.

## Events MQTT never announces

FaceID listens on `frigate/events`. Events **created through Frigate's API** are not
tracked objects, so they never appear there — FaceID simply never learns about them.

This matters if you use a camera's own person detection as a reliability bridge: an
automation sees the camera report a person and creates a Frigate event for it. Those
events carry a snapshot and usually a clip, but stay invisible to FaceID.

Set `poll_interval` (seconds, e.g. `30`) to also poll Frigate's event API:

```yaml
faceid:
  poll_interval: 30
```

On one installation that added ~22 events a day at the front door, of which 12 in 20
held a usable face — roughly doubling what FaceID could learn from. Polled events run
through the same pipeline; they carry no bounding box, so their snapshot is the full
frame rather than a person crop. Off by default, since it costs one API request per
interval.

Setting up such a bridge, checking beforehand whether it pays off, and the pitfalls of
box-less snapshots: **[docs/camera-bridge.md](docs/camera-bridge.md)**.


## Calibrating the threshold

The defaults are deliberately cautious, and on a real gallery that caution turned out to
be expensive. Measure yours rather than guessing — `scripts/measure-recognition.py` and
`scripts/coverage.py` produce the numbers.

On a 128-photo household gallery the separation was far wider than the defaults assume:

| | correct person | different person |
|---|---|---|
| best match score | median 0.50 | median 0.18, **max 0.31** |

With a default threshold of 0.50 sitting exactly on the median of correct matches, half
of all genuine recognitions were discarded to keep a distance nothing ever came close to.
Two changes followed, both measured:

* **`match_top_k` 3 → 1.** Averaging the best k photos punishes people whose references
  cover many angles — their own less similar photos drag the score down, so the
  best-covered people scored worst.
* **`match_threshold` 0.50 → 0.45**, still 0.14 above the highest score any stranger
  ever reached.

Together these lifted recognition on a held-out set of real events from 90% to 100%, and
moved the weakest favourite's worst match from 0.01 above the cut-off to 0.09 above it —
without a single misassignment.

Do not copy these numbers. Run the scripts on your own gallery: how far strangers get is
the number that decides how low you can safely go.

## Measuring instead of guessing

Two scripts answer the questions that otherwise invite guesswork:

```bash
python scripts/coverage.py                 # what is each person missing?
python scripts/measure-recognition.py --baseline /tmp/old --days 3
```

`coverage.py` reports, per person: photo count, diversity, viewing angles from the
landmarks, which cameras she was enrolled from, greyscale/IR shots, and a leave-one-out
self test — then names the concrete gap.

`measure-recognition.py` compares the current gallery against an older one (unpack a
backup from `data/backups`) and adds a practical probe against recent Frigate events,
including how much headroom each recognition has above the threshold. Events whose face
is already in the gallery are excluded — they score ~1.0 and measure nothing.


## How training stays healthy

Recognition is only as good as the reference photos, so FaceID keeps galleries diverse
rather than large:

- **A successful recognition never adds a photo.** Recognising you at the door tags the
  Frigate event and updates the sensor — but the gallery stays untouched. This is
  deliberate: a gallery that grows from its own matches reinforces whatever it already
  believes, and a single wrong match would quietly breed more of the same. References
  only come from what *you* assign in the review queue, or upload yourself.

- **New photos are only kept if they add something** — a near-duplicate of one you
  already have is skipped.
- **A per-person cap** (default 40, adjustable in Settings) bounds how many references a
  person keeps. When exceeded, FaceID sets aside the photo that is **most similar to all
  the others** — i.e. the most redundant one — so a rare side/angle shot is preserved
  while a 30th near-identical front shot is the first to go.
- **Remove duplicates on demand**: even under the cap, near-identical photos add nothing.
  **Settings → Remove duplicates** finds both truly identical *images* (perceptual hash) and
  near-identical *faces* (embedding similarity) and sets them aside (live preview),
  keeping the gallery diverse — as a button, so you stay in control. (Camera crops score
  lower than phone photos, so useful sensitivity is ~0.60–0.70.)
- **Nothing vanishes silently**: trimmed photos appear on the person card with a short
  reason and a one-click **restore** (or delete), and the set-aside pile is itself capped
  (`trimmed_keep`) so it never grows without bound.

If someone is recognized poorly from a certain angle, just add a photo from *that* angle —
being unusual, it's automatically kept.

**Full details:** [docs/trimming.md](docs/trimming.md) explains the why, the exact
selection rule (with numbers), and how to restore or curate set-aside photos.

## The 6.0 product interface

The old tab wall was replaced with a responsive task-first interface. The daily workflow
is intentionally short: **Dashboard → Needs review → Events → People**. Cameras, entry,
liveness and guests live under **Security & entry**; Home Assistant and system health live
under **Management**. Calibration, body recognition, Frigate sync, privacy, routes and logs
are preserved under the collapsed **Expert tools** group.

The document shell is never cached, while its versioned JavaScript and CSS are immutable.
This lets Home Assistant load a new release without private browsing and without repeatedly
downloading unchanged assets.

Frontend source lives in `frontend/`; `npm run build` writes the deployable bundle to
`static/`. The production image does not require Node.js because the built files are committed.

## Seeing what it is doing

The **LOG tab** shows the last 500 log lines straight in the UI — including the quiet
cases that decide whether a setup works: whether Frigate answers at startup, which
cameras were announced to Home Assistant, and for every event whether a face was found
at all. If nothing is ever recognised, that tab usually says why within a few lines.
There is a warnings-only filter and a copy button for pasting into an issue.

## Backup & restore

Your gallery (enrolled persons + ignore anchors) is the one thing you can't regenerate —
so FaceID makes it easy to keep. Everything is on the **Settings** tab:

- **Download backup** — a `.tar.gz` of `persons/` + `ignored/` (not the unknown queue).
- **Restore** — *replace* everything, or *merge* in only what's missing (handy for moving
  people between instances).
- **Automatic daily backup** — enable it, pick an hour and how many to keep. It runs
  inside FaceID (no external cron needed); the folder defaults to `data/backups`, which
  survives app updates. Point it at a mounted share to get backups off the box.

**Automate it yourself** if you prefer: the download is a plain endpoint, so any host
cron or Home Assistant automation can pull it:

```bash
# nightly host cron — keep 14 days
curl -fsS http://<faceid-host>:8600/api/backup -o /backups/faceid-$(date +\%F).tar.gz
find /backups -name 'faceid-*.tar.gz' -mtime +14 -delete
```

Settings changed here (thresholds + backup) are stored in `data/settings.json` and
**override `config.yaml` / app options**, persisting across restarts and updates.

## Home Assistant

Sensors appear automatically via MQTT discovery (`sensor.faceid_<camera>` for every
camera in `discovery_cameras`). The state lists everyone recognized within
`presence_window` seconds (`Alice, Bob`), then falls back to `nobody`. Attributes carry
the person list and the last recognition (score, event id).

For automations, trigger on the `faceid/event` topic — one JSON message per
(Frigate event, person): see [docs/ha-automation-example.yaml](docs/ha-automation-example.yaml)
for a phone-notification automation with the Frigate snapshot attached.

## Updates

- **Home Assistant app:** you get notified automatically when a new version is available
  (Settings → Apps → FaceID → Update). The changelog is shown right in the update dialog.
- **Standalone:** `cd /opt/faceid && git pull && systemctl restart faceid`. Watch the
  GitHub releases to get notified.

See [CHANGELOG.md](CHANGELOG.md) for the full history.

## Security & privacy notes

- The web UI supports optional **HTTP Basic Auth** (`faceid.auth` in config.yaml) —
  strongly recommended for standalone installs; the HA app is protected by ingress
  and your Home Assistant login instead. Either way: it manages biometric data — keep
  it on a trusted LAN and don't expose port 8600 to the internet (Basic Auth without
  TLS is not internet-grade protection).
- All face data stays in `data/` on your host (JPEG crops + embeddings). Delete a person
  and their data is gone.
- Depending on where you live, informing household members/visitors about face
  recognition on your cameras may be legally required. Be a good human.

## Configuration reference

See [docs/example-config.yaml](docs/example-config.yaml) — every option is commented. The two knobs
that matter most:

| Option | Meaning |
|---|---|
| `match_threshold` (0.50) | raise if strangers get matched to known persons, lower if known persons end up in the review queue |
| `unknown_threshold` (0.35) | below this the face is a clear unknown; the band above it is ambiguous |
| `match_margin` (0.08) | required lead over the second-best enrolled person |
| `min_confirmations` (2) | distinct agreeing frames required before publishing or ignoring |
| `cluster_eps` (0.55) | raise to merge unknown clusters more aggressively, lower if different people land in one cluster |
| `match_top_k` (3) | a person's score is the mean of their top-k reference similarities — dampens photo-count bias (1 = raw max) |
| `max_faces_per_person` (40) | soft cap; adding more drops the most redundant reference (0 = unlimited) |
| `ignore_threshold` (= match_threshold) | similarity at which a face counts as ignored |
| `ignore_margin` (0.12) | required lead of an ignore anchor over the best enrolled person |
| `ignore_learning` (true) | learn new looks of ignored people as additional anchors (guarded) |
| `hires_enroll` (true) | fetch new review-queue faces from the recording (sharper references) |
| `poll_interval` (0) | seconds; >0 also polls Frigate's event API for events MQTT never announces |
| `set_sub_label` (false) | opt in to writing confirmed names back to Frigate |
| `audit_retention_days` (90) | retention for the SQLite decision/evidence history |

## License

MIT
