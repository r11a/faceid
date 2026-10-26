# Access1 connection (FaceID 6.4.11)

FaceID recognizes people and submits bounded identity evidence. Access1 maps
people to existing users and cameras to readers, owns authorization and execution,
and records the outcome. Lists in FaceID never grant access.

## Connect the installations

1. Use compatible FaceID-enabled Access1 on the same private HA add-on network.
   Back up the full add-on data and relevant /ssl material before upgrading.
2. Open FaceID through HA ingress as an active owner or system administrator.
3. In **Access1**, copy FaceID's installation UUID to Access1, create a one-time
   setup code there, and paste the code into FaceID.
4. Review and approve the installation in Access1. The wizard verifies both
   directions over HTTPS and persists scoped credentials only after approval.
5. In Access1, map cameras in Doors and FaceID identities in existing user cards.
   Keep requests disabled during setup and isolated acceptance testing.
6. Verify authorized/denied users, duplicate requests, stale evidence, connection
   loss and restart before enabling access on approved test equipment.

The ordinary directory contains UUIDs and names only: no face photos, embeddings
or exported access permissions. FaceID's directory uses private HTTPS port 8601;
Access1's supported setup endpoint is a local-* add-on hostname on HTTPS 8445.
No directory host port is published. Standalone FaceID recognition still works,
but the administrator pairing wizard requires real HA ingress and Supervisor.

## Recognition and safety

Only fresh active-event snapshot evidence can be submitted. Enable both **fast
recognition** and **high resolution** on an intended access camera; existing
camera settings are never changed by an upgrade. The active frame must have a
no-store response and a valid original MQTT snapshot capture timestamp. Neither
download time nor a requested recording timestamp proves capture freshness.
Ordinary high-resolution recordings remain available for recognition/enrollment,
but recordings (including legacy secure_live_hires evidence) cannot request access.

Frigate must use authenticated HTTPS and a successful authenticated session.
**Secure is ON by default**, also when its saved decision is absent or invalid.
ON requires active Frigate certificate verification. An active HA administrator
may explicitly acknowledge the impersonation risk and set Secure OFF in the
connector settings. This relaxes only the connector's Frigate-certificate
admission rule; it does not change the configured Frigate TLS setting itself,
HTTPS/account authentication, FaceID–Access1 TLS, liveness, freshness, mapping or
Access1 permissions. A certificate error still needs correct trust configuration
or a separate explicit Frigate setting change; this release changes neither.

Compatible Access1 receivers accept authenticated Secure status reports every
10 seconds and after a change using /faceid/v1/secure-status (receiver contract
originated in Access1 1.0.118). An unsupported/offline receiver leaves reporting
unavailable; FaceID does not invent a different endpoint or weaken access gates.
The Access1 page makes read-only connection checks every five seconds while
mounted. Status checks never complete pairing, enable requests or replay events.

Configure access cameras with liveness **required**, and verify the installed
liveness model. The connector requires confirmed liveness, finite evidence age
below 10 seconds and unchanged person/camera identities. Snapshot fallback,
clip processing, body-only matches and manually reviewed history cannot request
access. The initial maximum age is 10 seconds; Access1 independently enforces its
own accepted age and clock policy.

A durable record precedes the sole event POST. A lost response leads to bounded
result GET queries, never another POST or startup replay. Authorization,
dispatch, execution and physical door-open confirmation remain distinct.
Bluetooth Required, APB, schedules and other Access1 rules remain authoritative.

## Identity, backup and recovery

The installation and person UUIDs survive rename/restart. Temporarily disabling
a camera hides it from the directory without rotating its UUID. Actual removal
from the source catalogue removes its saved mapping; recreating it receives a
new identity. Legacy FaceID-local user/reader mappings are not automatically
imported: review the prior backup and configure them in Access1. Existing
request history is retained as non-executable historical records.

Protect /data/access1.json, /data/access1-connector.json, /data/access1-security.json, /data/access1-wizard-tls/
and each person's access1-identity.json (paths relative to FaceID's data folder).
Use a full HA add-on backup: the FaceID UI export does not include wizard secrets
and TLS keys. Never clone a paired site's identity/credentials to another site.
Local disconnect is not server-side revocation; disable/revoke the credential in
Access1 when required. Restore the matching data backup with an older image.

Wizard directory certificates last 365 days. Automatic renewal is not implemented;
plan renewal/re-pairing on both sides without disabling TLS verification.

## Source integration

Imported from handoff-faceid-access1-20261022 (connector source 6.2.9), merged onto
FaceID 6.3.0 while preserving its recognition engine, enrollment, alerts, video,
queues, realtime updates and dependencies. Added live camera-trust checks and
stable UUID retention across temporary camera disable/re-enable. No production
pairing or door operation is performed by the source release process.


## 6.4.11 handoff and verification

Integrated the consolidated FaceID-6.4.0-to-6.4.11-Developer-Handoff.zip after
checking all 47 scoped baseline and clean-target hashes. The original 6.4.0 tree
matched; only newline normalization was required before applying the clean patch.
Temporary identity traces, frame timing dumps and profiling wrappers are excluded.
A further safety correction rejects all recording-derived access evidence instead
of labeling a requested recording time as a capture timestamp.

Optional CPU tuning uses cpu_threads (0..16, default 0) and cpu_allow_spinning
(default true). It applies only to CPU-only FaceEngine model sessions, leaving
CUDA/OpenVINO, model weights and recognition thresholds unchanged. Coral in
Frigate does not accelerate FaceID's ONNX model inference. No universal latency
or real-camera accuracy improvement is implied by software tests.
