#!/usr/bin/with-contenv bashio
set -e
# Native RTSP diagnostics may expose URLs; Python categorical diagnostics remain available.
export OPENCV_FFMPEG_LOGLEVEL=-8
export OPENCV_LOG_LEVEL=SILENT

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
    # 6.9.0: start anyway so setup can be finished from the FaceID UI (setup mode).
    bashio::log.warning "No MQTT broker configured and none provided by Home Assistant."
    bashio::log.warning "FaceID starts in setup mode: install the Mosquitto add-on or enter the broker in FaceID setup."
fi

CAMERAS=$(cfg_json '.cameras // []')
DISCOVERY=$(cfg_json '.discovery_cameras // []')
WEBHOOKS=$(jq -c '.webhook_urls // []' "${OPT}")

# 6.9.3: most options are optional (short options page). Each fallback below is the
# 6.9.2 default, so an option that was never saved keeps exactly the same value.
cat > /opt/faceid/config.yaml << EOF
frigate:
  url: $(cfg_json '.frigate_url // ""')
  username: $(cfg_json '.frigate_username // ""')
  password: $(cfg_json '.frigate_password // ""')
  verify_tls: $(cfg_json 'if .frigate_verify_tls == null then true else .frigate_verify_tls end')
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
  mqtt_prefix: $(cfg_json '.mqtt_prefix // "faceid"')
  backend: $(cfg_json '.backend // "auto"')
  timing_diagnostics: $(cfg_json '.timing_diagnostics // false')
  cpu_threads: $(cfg_json '.cpu_threads // 0')
  cpu_allow_spinning: $(cfg_json 'if .cpu_allow_spinning == null then true else .cpu_allow_spinning end')
  stream_warm_seconds: $(cfg_json '.stream_warm_seconds // 0')
  stream_decode_threads: $(cfg_json '.stream_decode_threads // 2')
  stream_gpu_decode: $(cfg_json 'if .stream_gpu_decode == null then true else .stream_gpu_decode end')
  stream_decode_width: $(cfg_json '.stream_decode_width // 0')
  frigate_box_association: $(cfg_json 'if .frigate_box_association == null then true else .frigate_box_association end')
  match_threshold: $(cfg_json '.match_threshold // 0.5')
  unknown_threshold: $(cfg_json '.unknown_threshold // 0.35')
  match_margin: $(cfg_json '.match_margin // 0.08')
  min_confirmations: $(cfg_json '.min_confirmations // 2')
  min_face_quality: $(cfg_json '.min_face_quality // 0.35')
  clip_analysis: $(cfg_json 'if .clip_analysis == null then true else .clip_analysis end')
  clip_max_frames: $(cfg_json '.clip_max_frames // 24')
  clip_max_samples: $(cfg_json '.clip_max_samples // 8')
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
  cluster_eps: $(cfg_json '.cluster_eps // 0.55')
  suggest_threshold: $(cfg_json '.suggest_threshold // 0.40')
  max_faces_per_person: $(cfg_json '.max_faces_per_person // 40')
  trimmed_keep: $(cfg_json '.trimmed_keep // 10')
  dedupe_threshold: $(cfg_json '.dedupe_threshold // 0.65')
  ignore_margin: $(cfg_json '.ignore_margin // 0.12')
  hires_enroll: $(cfg_json 'if .hires_enroll == null then true else .hires_enroll end')
  frigate_topic_prefix: $(cfg_json '.frigate_topic_prefix // "frigate"')
  poll_interval: $(cfg_json '.poll_interval // 0')
  backup_enabled: $(cfg_json 'if .backup_enabled == null then false else .backup_enabled end')
  backup_hour: $(cfg_json '.backup_hour // 3')
  backup_dir: $(cfg_json '.backup_dir // ""')
  backup_keep: $(cfg_json '.backup_keep // 7')
  audit_retention_days: $(cfg_json '.audit_retention_days // 90')
  known_evidence_days: $(cfg_json '.known_evidence_days // 30')
  unknown_evidence_days: $(cfg_json '.unknown_evidence_days // 14')
  known_evidence_max: $(cfg_json '.known_evidence_max // 300')
  unknown_evidence_max: $(cfg_json '.unknown_evidence_max // 300')
  liveness_enabled: $(cfg_json '.liveness_enabled // true')
  liveness_threshold: $(cfg_json '.liveness_threshold // 0.5')
  liveness_required_frames: $(cfg_json '.liveness_required_frames // 3')
  presence_window: $(cfg_json '.presence_window // 120')
  recognition_session_seconds: $(cfg_json '.recognition_session_seconds // 300')
  visit_gap_minutes: $(cfg_json '.visit_gap_minutes // 15')
  calibration_target_far: $(cfg_json '.calibration_target_far // 0.01')
  scenario_window: $(cfg_json '.scenario_window // 90')
  camera_graph: $(cfg_json '(.camera_graph_json // "{}") | if type == "string" then fromjson else . end')
  reid_enabled: $(cfg_json 'if .reid_enabled == null then true else .reid_enabled end')
  reid_ttl: $(cfg_json '.reid_ttl // 180')
  reid_threshold: $(cfg_json '.reid_threshold // 0.90')
  automation_cooldown: $(cfg_json '.automation_cooldown // 60')
  webhook_urls: ${WEBHOOKS}
  ai_enabled: $(cfg_json 'if .ai_enabled == null then false else .ai_enabled end')
  ai_url: $(cfg_json '.ai_url // "http://localhost:11434"')
  ai_vision_model: $(cfg_json '.ai_vision_model // "gemma3:4b"')
  ai_embedding_model: $(cfg_json '.ai_embedding_model // "embeddinggemma"')
  set_sub_label: $(cfg_json 'if .set_sub_label == null then false else .set_sub_label end')
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
