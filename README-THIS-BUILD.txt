DEVELOPER HANDOFF — 6.6.12-access1.stream3
Status: implementation available; operational camera acceptance remains pending.
Do not present this as field-proven faster or a finished security certification.

OBSERVED FIELD FAILURE
Snapshots completed recognition + Access1 in about 11.2 s. Restream packet test
showed 25 fps; operational reader churned generations, zero evidence samples.
The report did not expose decoder clock values/reasons, so exact field cause
cannot be conclusively attributed from that report alone.

FOCUSED CORRECTIONS
Shared decoder_position_ms uses native FFmpeg CAP_PROP_PTS in FPS time base when
available; older builds retain POS_MSEC. Finite arbitrary/negative/zero timestamp
origins are supported. Internal records normalize the origin, never arrival time.
Repeated/backward/jumping timestamps, startup drain, local age and 750ms relative
buffering growth still block evidence. Generation changes still clear history.
Reader reports now expose categorical rejection counters and generation. Rejection
logs are rate limited to once per five seconds; event lifecycle stages are logged.
Observation-only readers drain without JPEG encoding. Existing regular JPEG consumers
retain encoding. Production and manual decode diagnostics share timestamp extraction.
No video-source fallback, liveness reduction, model or CPU setting change.

MULTI-PERSON BEHAVIOR
Anchor must contain exactly one usable face. Live candidates match that anchor and
conservative tracks with mutual unique matching, .65 threshold/.08 margin. Detector
order cannot select another person. Competing events cannot share one track;
subject departure, crossings/overlap, ambiguous matching and identity changes reset
evidence. More than four usable faces rejects the frame. No accumulated liveness
transfers to a bystander. Synthetic regression tests are not field accuracy proof.

PROCESSOR AND CORAL
Measured embedding 1.35-1.49 seconds per face is CPU inference, not model queue wait.
Detection was roughly .5 seconds. Multiple usable faces multiply embedding work;
no fixed low latency is promised. Measure CPU quota, affinity, thread settings and
actual provider on the stronger rig using retained benchmarks. Existing models run
ONNX Runtime CPU; Coral USB Edge TPU needs compatible compiled TensorFlow Lite
models and is not a drop-in accelerator. Model conversion/replacement is separate
work requiring recognition/PAD validation; leave Coral/Frigate configuration intact.
Sources: https://docs.opencv.org/4.13.0/d4/d15/group__videoio__flags__base.html
https://gweb-coral-full.uc.r.appspot.com/docs/edgetpu/inference/

NEXT ACCEPTANCE ON CAMERA RIG
Install complete source/faceid-addon; preserve /data/gallery/pairing/mappings.
Select restream + trust, recognition by event. Record startup/provider/CPU settings.
First verify native PTS progresses, reader generation stabilizes and stream_ready
appears. Then anchor, association, three samples, recognition and Access1 final
result. If not, export operational report including reader rejection counters and
logs; do not lower thresholds. Compare same scenes on direct RTSP and snapshots.
Test two different enrolled people in both detection orders, bystander appearance,
subject departure, overlap/crossings, duplicate/competing events, reconnect, and
repeated approaches. Use 20 approaches per baseline/source and median/p95, success,
wrong-person and duplicate counts. Zero wrong-person/duplicate activation required.
No real camera/HA/Access1/relay endpoint was contacted during offline development.
