#!/usr/bin/with-contenv bashio
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
jq -e 'type == "object"' "${OPT}" >/dev/null
cfg() { jq -r "$1 // empty" "${OPT}"; }
cfg_json() { jq -c "$1" "${OPT}"; }

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

CAMERAS=$(cfg_json '.cameras // []')
DISCOVERY=$(cfg_json '.discovery_cameras // []')
WEBHOOKS=$(jq -c '.webhook_urls // []' "${OPT}")

cat > /opt/faceid/config.yaml << EOF
frigate:
  url: $(cfg_json '.frigate_url')
  username: $(cfg_json '.frigate_username')
  password: $(cfg_json '.frigate_password')
  verify_tls: $(cfg_json '.frigate_verify_tls')
mqtt:
  host: $(jq -cn --arg value "${MQTT_HOST}" '$value')
  port: ${MQTT_PORT:-1883}
  user: $(jq -cn --arg value "${MQTT_USER}" '$value')
  password: $(jq -cn --arg value "${MQTT_PASSWORD}" '$value')
faceid:
  access1_connector:
    enabled: $(cfg_json '.access1_enabled // false')
    bind_address: $(cfg_json '.access1_bind_address // ""')
    directory_token: $(cfg_json '.access1_directory_token // ""')
    tls_cert: $(cfg_json '.access1_tls_cert // "/ssl/faceid/directory.pem"')
    tls_key: $(cfg_json '.access1_tls_key // "/ssl/faceid/directory-key.pem"')
    access1_url: $(cfg_json '.access1_url // ""')
    access1_token: $(cfg_json '.access1_token // ""')
    access1_ca: $(cfg_json '.access1_ca // "/ssl/access1-mobile/backend-ca.pem"')
  port: 8600
  mqtt_prefix: $(cfg_json '.mqtt_prefix')
  backend: $(cfg_json '.backend')
  match_threshold: $(cfg_json '.match_threshold')
  unknown_threshold: $(cfg_json '.unknown_threshold')
  match_margin: $(cfg_json '.match_margin')
  min_confirmations: $(cfg_json '.min_confirmations')
  min_face_quality: $(cfg_json '.min_face_quality')
  clip_analysis: $(cfg_json '.clip_analysis')
  clip_max_frames: $(cfg_json '.clip_max_frames')
  clip_max_samples: $(cfg_json '.clip_max_samples')
  clip_retry_attempts: $(cfg_json '.clip_retry_attempts // 3')
  clip_retry_seconds: $(cfg_json '.clip_retry_seconds // 10')
  video_decode: $(cfg_json '.video_decode // "auto"')
  body_enabled: $(cfg_json '.body_enabled // false')
  body_threshold: $(cfg_json '.body_threshold // 0.72')
  body_confirmations: $(cfg_json '.body_confirmations // 3')
  body_consensus_window: $(cfg_json '.body_consensus_window // 300')
  vision_advisor_enabled: $(cfg_json '.vision_advisor_enabled // false')
  media_max_clip_mb: $(cfg_json '.media_max_clip_mb // 150')
  media_cache_mb: $(cfg_json '.media_cache_mb // 1000')
  media_retention_hours: $(cfg_json '.media_retention_hours // 24')
  cluster_eps: $(cfg_json '.cluster_eps')
  suggest_threshold: $(cfg_json '.suggest_threshold')
  max_faces_per_person: $(cfg_json '.max_faces_per_person')
  trimmed_keep: $(cfg_json '.trimmed_keep')
  dedupe_threshold: $(cfg_json '.dedupe_threshold')
  ignore_margin: $(cfg_json '.ignore_margin')
  hires_enroll: $(cfg_json '.hires_enroll')
  frigate_topic_prefix: $(cfg_json '.frigate_topic_prefix')
  poll_interval: $(cfg_json '.poll_interval')
  backup_enabled: $(cfg_json '.backup_enabled')
  backup_hour: $(cfg_json '.backup_hour')
  backup_dir: $(cfg_json '.backup_dir // ""')
  backup_keep: $(cfg_json '.backup_keep')
  audit_retention_days: $(cfg_json '.audit_retention_days')
  known_evidence_days: $(cfg_json '.known_evidence_days')
  unknown_evidence_days: $(cfg_json '.unknown_evidence_days')
  known_evidence_max: $(cfg_json '.known_evidence_max // 300')
  unknown_evidence_max: $(cfg_json '.unknown_evidence_max // 300')
  liveness_enabled: $(cfg_json '.liveness_enabled // true')
  liveness_threshold: $(cfg_json '.liveness_threshold // 0.5')
  liveness_required_frames: $(cfg_json '.liveness_required_frames // 3')
  presence_window: $(cfg_json '.presence_window')
  recognition_session_seconds: $(cfg_json '.recognition_session_seconds // 300')
  visit_gap_minutes: $(cfg_json '.visit_gap_minutes // 15')
  calibration_target_far: $(cfg_json '.calibration_target_far')
  scenario_window: $(cfg_json '.scenario_window')
  camera_graph: $(cfg_json '(.camera_graph_json // "{}") | if type == "string" then fromjson else . end')
  reid_enabled: $(cfg_json '.reid_enabled')
  reid_ttl: $(cfg_json '.reid_ttl')
  reid_threshold: $(cfg_json '.reid_threshold')
  automation_cooldown: $(cfg_json '.automation_cooldown')
  webhook_urls: ${WEBHOOKS}
  ai_enabled: $(cfg_json '.ai_enabled')
  ai_url: $(cfg_json '.ai_url')
  ai_vision_model: $(cfg_json '.ai_vision_model')
  ai_embedding_model: $(cfg_json '.ai_embedding_model')
  set_sub_label: $(cfg_json '.set_sub_label')
  access_control_enabled: $(cfg_json '.access_control_enabled // false')
  guest_match_threshold: $(cfg_json '.guest_match_threshold // 0.62')
  guest_match_margin: $(cfg_json '.guest_match_margin // 0.12')
  min_face_px: 48
  det_size: 640
  max_attempts: 6
  retry_seconds: 2.5
  cameras: ${CAMERAS}
  discovery_cameras: ${DISCOVERY}
  review_queue_max_total: $(cfg_json '.review_queue_max_total // 200')
  review_queue_max_per_cluster: $(cfg_json '.review_queue_max_per_cluster // 12')
  review_queue_retention_days: $(cfg_json '.review_queue_retention_days // 14')
  review_queue_dedupe_days: $(cfg_json '.review_queue_dedupe_days // 7')
EOF
chmod 600 /opt/faceid/config.yaml

# Galerie + Modell-Cache im persistenten /data-Volume (überlebt Updates)
mkdir -p /data/faceid /data/model-cache
ln -sfn /data/faceid /opt/faceid/data
export FACEID_ADDON=1
export HOME=/data/model-cache

bashio::log.info "Starting FaceID..."
cd /opt/faceid
exec venv/bin/python -m app.main
