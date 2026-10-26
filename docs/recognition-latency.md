# Immediate confirmed recognition

The fast path publishes only after the existing identity threshold, runner-up
margin, distinct-frame agreement, quality and configured liveness gates pass.
No recognition model, threshold, resolution or required confirmation is weakened.

## Event flow (6.5.1)

1. Active Frigate MQTT event arrives and queues image processing.
2. Opted-in `fast_recognition` cameras sample at a bounded 250ms cadence, coalescing
   duplicate jobs. This also works without an Access1 connection. Full-resolution
   camera profiles request an uncropped event image, without waiting for recordings.
   Enable the existing fast-recognition switch for the door camera; camera settings
   are not modified automatically by an upgrade.
3. After identity confirmation, publish non-retained `PREFIX/v1/recognitions`
   immediately and enqueue existing webhooks. Access1 admission still passes through
   the existing freshness, secure evidence, liveness and authorization path.
4. Publish legacy `PREFIX/event`, persist configured alerts, then index bounded face
   samples and save evidence. Body analysis and Frigate sublabels belong to final
   enrichment. Alert UI subscribers skip the ordinary 150ms visual debounce.
5. Final `PREFIX/v1/events` retains enriched scenario/body context. It does not
   trigger a second webhook or immediate recognition for an already announced event.

HA recognition event discovery and camera device triggers use the immediate topic;
entity IDs stay unchanged. MQTT consumers subscribed explicitly to `v1/events`
should use `v1/recognitions` for immediate identity automation. The alert blueprint
continues using `v1/alerts` for configured camera/person/blacklist notifications.
Unconfirmed/blocked outcomes remain distinct from recognized identity. Definite broker/queue rejection may retry the same payload for at most two
seconds while the encounter remains active; already accepted destinations are
not repeated. Uncertain publication, end, expiry or a later reconnect never
creates a new automation replay. Events are not retained.
Continuous-presence suppression and existing alert policies stay unchanged.

## Payload

Schema version 1 includes `event_id`, `camera`, `decision`, confirmed `person` or
null, `score`, `margin`, `confirmations`, `liveness`, `occurrence`, `start_ts`,
`end_ts` (null while active), `decided_at` (UTC epoch), `phase`, and `live` (active encounter, not a proof of authenticated capture).
This is an identity notification, never an access grant. Final enriched context
remains on the final-event topic. Configured webhooks now receive the confirmed
phase for recognized identities; scenario/body context can still be null then.
Consumers that require completed context should consume the final-event topic.

## Measurement boundary

Bounded processor metrics expose `mqtt_to_recognition` only when an actual MQTT
receipt timestamp exists, and `confirmed_to_notification` for local publish enqueue.
They do not measure camera exposure, broker delivery, HA execution, phone push or
network latency. No zero-latency or production speed percentage is inferred from
synthetic tests. Use camera-site p50/p95 measurements for those claims.

Tests block media/index work and verify publication occurs first, only after
agreement, once per event, with no wrong-camera triggers or weakened liveness.

## Interface read performance

Person statistics use three bulk reads in one WAL snapshot instead of one plus
 two per person. Visit summaries share a timeline read and camera-role lookups.
Reference quality reports are cached per gallery with reference/embedding/metadata
file signatures, so unchanged polls do not decode the entire gallery again.
Cached event images check event existence without loading full observation history.
Gallery JPEGs have a private five-minute browser cache; source templates stay intact.
Visible home images load first with asynchronous decoding. A missing just-created
image retries twice (400ms, then 1200ms), and changing selection cancels the retry.
Camera preview URLs change only on camera/explicit refresh or a new capture result.

A controlled local SQLite check with 10,000 synthetic events and 20 people gave
identical statistics: 41 queries / 44.68ms median before, 3 queries / 20.92ms after
(five samples each). This is not a measured speed claim for the remote installation.
