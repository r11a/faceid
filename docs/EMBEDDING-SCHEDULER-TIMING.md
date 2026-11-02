# Embedding and scheduler timing

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
- Bounded per-engine timing counters report p50/p95 wait and compute time,
  failure count and startup warmup status through the existing health report.
- Secure-live sampling no longer repeatedly enqueues work whose snapshot
  timestamp is missing, stale, future, before the event, or already consumed.
  These jobs previously reached the same execution gate and returned without
  useful image work. Execution-time gates remain intact to handle races.
  Fresh valid timestamps resume sampling; notification-only sampling is unchanged.

Preserved
Model weights, FP32 precision, library versions, face alignment, gallery math,
similarity thresholds, liveness model and required frame count, same-identity
and duplicate-evidence guards, source provenance, expiry, TLS/security settings,
Access1 mappings/permissions/APB/interlocks/timetables, POST deduplication and
the prior separate receipt worker. No motion analysis or live door operations.
No automatic CPU setting change: saved cpu_threads=2 remains 2 after upgrading.

Measured evidence and limits
Your successful event: 8.04 s MQTT-to-recognition; about 4.87 s inside embedding
across three frames; about 51 ms liveness; 565 ms Access1 result round trip.
Your startup log confirms CPUExecutionProvider, cpu_threads=2, spinning=true.
Local synthetic-input FP32 benchmark: two threads median 208.65 ms versus four
100.36 ms. Eight local CPUs were visible in that initial benchmark.
Separate old/new path comparison under a four-CPU Docker quota: at two threads,
first embedding 336.67 -> 157.91 ms; at four threads 105.31 -> 108.35 ms (no cold
benefit demonstrated there). Warm medians at four threads were about 98 ms.
Outputs were bit-identical for the tested synthetic inputs. These small local
benchmarks do not measure biometric accuracy or guarantee your HA latency.
The benchmark used the public Immich buffalo_l recognition artifact, hash
4c06341c33c2ca1f86781dab0e829f88ad5b64be9fba56e56bc9ebdefc619e43;
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
