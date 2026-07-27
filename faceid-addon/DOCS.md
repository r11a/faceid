# FaceID App

Face recognition for [Frigate](https://frigate.video): confirmed people are published
to MQTT (sensors appear automatically), optionally written back as `sub_label`, and
unknown faces land in a review UI (side panel) where you assign them with one click.

Full documentation: https://github.com/SkyTechNerds/faceid

## Setup

1. Set `frigate_url` to your Frigate instance (e.g. `http://192.168.1.10:5000`).
2. MQTT: leave `mqtt_host` empty to automatically use the Mosquitto broker app.
   Fill the `mqtt_*` options only for an external broker.
3. Optional: restrict processing to specific cameras (`cameras`), and list the cameras
   that should get a `sensor.faceid_<camera>` in Home Assistant (`discovery_cameras`).
4. Start the app. The first start downloads the recognition model (~300 MB) —
   check the app log until you see `MQTT verbunden`.
5. Open the **FaceID** panel in the sidebar. Recommended first step: run the backfill
   (see main README) or just wait — every detected unknown face shows up for review.

## Options

| Option | Description |
|---|---|
| `frigate_url` | Base URL of your Frigate instance |
| `mqtt_*` | Leave empty to use the internal Mosquitto app automatically |
| `backend` | `auto`, `cpu`, `cuda` or `openvino`; explicit modes fail fast when unavailable |
| `match_threshold` | ≥ this cosine similarity = recognized (raise if strangers get misassigned) |
| `unknown_threshold` | < this = definitely unknown |
| `match_margin` | required lead over the runner-up before a match can count |
| `min_confirmations` | distinct agreeing frames required before publishing |
| `min_face_quality` | reject weak size, blur, lighting and pose evidence |
| `clip_analysis` / `clip_max_*` | sample diverse faces from the finished recording |
| `cluster_eps` | how aggressively unknown faces are grouped in the review UI |
| `presence_window` | camera sensor lists everyone seen within this many seconds |
| `calibration_target_far` | false-accept target for calibration recommendations |
| `scenario_window` | maximum gap between identity-linked cross-camera events |
| `camera_graph_json` | JSON camera adjacency map, e.g. `{"front":["hall"]}` |
| `reid_*` | short-lived clothing hint; it never becomes a face identity verdict |
| `automation_cooldown` | duplicate suppression for versioned automation events |
| `webhook_urls` | optional HTTP endpoints receiving the same final v1 event |
| `ai_*` | optional local Ollama-compatible context and semantic search |
| `set_sub_label` | opt in to writing confirmed names back to Frigate events |
| `ignore_margin` | ignore match must beat the best enrolled person by this much |
| `audit_retention_days` | days to keep the SQLite decision history (0 = forever) |
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
