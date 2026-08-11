#!/usr/bin/env bashio
set -e

# Optionen direkt aus /data/options.json lesen — robust gegenüber
# bashio/Supervisor-API-Versionsunterschieden.
# onnxruntime/NumPy brauchen mind. x86-64-v2 — in VMs oft vom CPU-Modell maskiert
if [ "$(uname -m)" = "x86_64" ] && ! grep -qm1 avx /proc/cpuinfo; then
    bashio::log.fatal "This CPU (or VM CPU model) lacks AVX, required by the recognition runtime."
    bashio::log.fatal "Running HAOS in a VM? Set the CPU type to 'host' (Proxmox: qm set <vmid> --cpu host, then cold-restart the VM)."
    exit 1
fi

OPT=/data/options.json
cfg() { jq -r "$1 // empty" "${OPT}"; }

MQTT_HOST=$(cfg '.mqtt_host')
MQTT_PORT=$(cfg '.mqtt_port')
MQTT_USER=$(cfg '.mqtt_user')
MQTT_PASSWORD=$(cfg '.mqtt_password')

# Kein Broker konfiguriert -> Mosquitto-Add-on über die Supervisor services API beziehen
TOKEN="${SUPERVISOR_TOKEN:-${HASSIO_TOKEN:-}}"
if [ -z "${MQTT_HOST}" ] && [ -z "${TOKEN}" ]; then
    bashio::log.warning "No Supervisor token in the container environment - cannot auto-detect MQTT."
fi
if [ -z "${MQTT_HOST}" ] && [ -n "${TOKEN}" ]; then
    SVC=$(curl -s -H "Authorization: Bearer ${TOKEN}" http://supervisor/services/mqtt || true)
    if [ "$(echo "${SVC}" | jq -r '.result // empty')" = "ok" ]; then
        bashio::log.info "Using MQTT broker from the Supervisor services API"
        MQTT_HOST=$(echo "${SVC}" | jq -r '.data.host')
        MQTT_PORT=$(echo "${SVC}" | jq -r '.data.port')
        MQTT_USER=$(echo "${SVC}" | jq -r '.data.username')
        MQTT_PASSWORD=$(echo "${SVC}" | jq -r '.data.password')
    else
        bashio::log.warning "Supervisor services API answered: ${SVC:-<empty>}"
    fi
fi

if [ -z "${MQTT_HOST}" ]; then
    bashio::log.fatal "No MQTT broker configured and none provided by Home Assistant."
    bashio::log.fatal "Set mqtt_host in the add-on options or install the Mosquitto add-on."
    exit 1
fi

CAMERAS=$(cfg '.cameras | join(", ")')
DISCOVERY=$(cfg '.discovery_cameras | join(", ")')
WEBHOOKS=$(jq -c '.webhook_urls // []' "${OPT}")

cat > /opt/faceid/config.yaml << EOF
frigate:
  url: $(cfg '.frigate_url')
  username: "$(cfg '.frigate_username')"
  password: "$(cfg '.frigate_password')"
  verify_tls: $(cfg '.frigate_verify_tls')
mqtt:
  host: ${MQTT_HOST}
  port: ${MQTT_PORT:-1883}
  user: "${MQTT_USER}"
  password: "${MQTT_PASSWORD}"
faceid:
  port: 8600
  mqtt_prefix: $(cfg '.mqtt_prefix')
  backend: $(cfg '.backend')
  match_threshold: $(cfg '.match_threshold')
  unknown_threshold: $(cfg '.unknown_threshold')
  match_margin: $(cfg '.match_margin')
  min_confirmations: $(cfg '.min_confirmations')
  min_face_quality: $(cfg '.min_face_quality')
  clip_analysis: $(cfg '.clip_analysis')
  clip_max_frames: $(cfg '.clip_max_frames')
  clip_max_samples: $(cfg '.clip_max_samples')
  video_decode: $(cfg '.video_decode // "auto"')
  body_enabled: $(cfg '.body_enabled // false')
  body_threshold: $(cfg '.body_threshold // 0.72')
  body_confirmations: $(cfg '.body_confirmations // 3')
  body_consensus_window: $(cfg '.body_consensus_window // 300')
  vision_advisor_enabled: $(cfg '.vision_advisor_enabled // false')
  media_max_clip_mb: $(cfg '.media_max_clip_mb // 150')
  media_cache_mb: $(cfg '.media_cache_mb // 1000')
  media_retention_hours: $(cfg '.media_retention_hours // 24')
  cluster_eps: $(cfg '.cluster_eps')
  suggest_threshold: $(cfg '.suggest_threshold')
  max_faces_per_person: $(cfg '.max_faces_per_person')
  trimmed_keep: $(cfg '.trimmed_keep')
  dedupe_threshold: $(cfg '.dedupe_threshold')
  ignore_margin: $(cfg '.ignore_margin')
  hires_enroll: $(cfg '.hires_enroll')
  frigate_topic_prefix: $(cfg '.frigate_topic_prefix')
  poll_interval: $(cfg '.poll_interval')
  backup_enabled: $(cfg '.backup_enabled')
  backup_hour: $(cfg '.backup_hour')
  backup_keep: $(cfg '.backup_keep')
  audit_retention_days: $(cfg '.audit_retention_days')
  known_evidence_days: $(cfg '.known_evidence_days')
  unknown_evidence_days: $(cfg '.unknown_evidence_days')
  liveness_enabled: $(cfg '.liveness_enabled // true')
  liveness_threshold: $(cfg '.liveness_threshold // 0.5')
  liveness_required_frames: $(cfg '.liveness_required_frames // 3')
  presence_window: $(cfg '.presence_window')
  visit_gap_minutes: $(cfg '.visit_gap_minutes // 15')
  calibration_target_far: $(cfg '.calibration_target_far')
  scenario_window: $(cfg '.scenario_window')
  camera_graph: $(cfg '.camera_graph_json')
  reid_enabled: $(cfg '.reid_enabled')
  reid_ttl: $(cfg '.reid_ttl')
  reid_threshold: $(cfg '.reid_threshold')
  automation_cooldown: $(cfg '.automation_cooldown')
  webhook_urls: ${WEBHOOKS}
  ai_enabled: $(cfg '.ai_enabled')
  ai_url: $(cfg '.ai_url')
  ai_vision_model: $(cfg '.ai_vision_model')
  ai_embedding_model: $(cfg '.ai_embedding_model')
  set_sub_label: $(cfg '.set_sub_label')
  access_control_enabled: $(cfg '.access_control_enabled // false')
  guest_match_threshold: $(cfg '.guest_match_threshold // 0.62')
  guest_match_margin: $(cfg '.guest_match_margin // 0.12')
  min_face_px: 48
  det_size: 640
  max_attempts: 6
  retry_seconds: 2.5
  cameras: [${CAMERAS}]
  discovery_cameras: [${DISCOVERY}]
EOF

# Galerie + Modell-Cache im persistenten /data-Volume (überlebt Updates)
mkdir -p /data/faceid /data/model-cache
ln -sfn /data/faceid /opt/faceid/data
export HOME=/data/model-cache

bashio::log.info "Starting FaceID..."
cd /opt/faceid
exec venv/bin/python -m app.main
