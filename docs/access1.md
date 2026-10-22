# Access1 connection (FaceID 6.4.0)

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

Only fresh high-resolution evidence from an ongoing event can be submitted.
Frigate must use authenticated HTTPS, active certificate verification and a
successful authenticated session. The connector checks actual client state;
a source label alone is insufficient. Ordinary recognition remains available
when Access1 evidence is blocked.

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

Protect /data/access1.json, /data/access1-connector.json, /data/access1-wizard-tls/
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
