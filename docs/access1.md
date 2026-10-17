# Access1 integration boundary

FaceID 6.1.0 contains mapping preparation and an internal provider contract, not a
working connection to a particular Access1 product. Product/vendor identity,
software version, authentication method and API/SDK documentation must be supplied
before the vendor adapter can be implemented or enabled. There are intentionally
no guessed HTTP endpoints, remote-unlock calls or fabricated Access1 events.

The intended flow is:

1. Choose an existing Access1 user for each FaceID person (stable FaceID slug).
2. Map selected FaceID cameras to existing Access1 readers.
3. A confirmed face recognition with confirmed liveness from a fresh authenticated
   live recording frame presents a biometric credential to the mapped reader.
4. Access1 checks its user's permissions, schedules and access rules, performs the
   allowed access operation, and records its own biometric event. Denial remains
   a denial; a transport timeout is unconfirmed, never reported as a grant.

The provider object injected into `Access1Integration(..., provider=...)` exposes:

- `users() -> list[{id, name}]`
- `readers() -> list[{id, name}]`
- `present_biometric(user_id, reader_id, credential_type, request_id,
  face_event_id, occurred_at, score) -> {decision, event_id}`

`decision` must be `granted` or `denied`, and `event_id` must be the actual Access1
biometric transaction identifier. The adapter must honor `request_id` for
idempotency and use a bounded network timeout. It must exercise Access1's credential
authorization API rather than an administrative door-release endpoint. A grant is
an authorization result; it does not prove a door physically opened.

Only `secure_live_hires` evidence less than ten seconds old from a still-running
event is eligible. Cached event snapshots have no verified capture timestamp, and
historical clip frames are ineligible. The request is claimed in SQLite before
calling the provider. An unconfirmed transaction is not automatically replayed.
Distinct new recognition events can present a credential even when normal
notifications are suppressed as continuing presence.

Mapping settings are stored in `access1.json`, included in gallery backups and
restored with other settings. Attempts are kept in the existing audit database.
With no provider installed, the UI shows `awaiting_api`, no directory entry is
claimed as verified, and attempts to enable the connection are refused.
