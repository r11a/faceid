"""6.9.3: secret-free site export/import and the short add-on options page."""
import json
import re
from pathlib import Path

import pytest
import yaml

from app import setup_wizard as setup
from app.camera_profiles import CameraProfiles

ROOT = Path(__file__).resolve().parents[1]
ADDON = ROOT / "faceid-addon"
RIG_PROFILES = {
    "Enterance": {"observation_source": "frigate_restream", "trust_local_video_network": True,
                  "direct_rtsp_url": "rtsp://admin:SECRETPW@192.168.1.64:554/main", "enabled": True,
                  "min_face_px": 24, "role": "intercom", "mode": "intercom", "night_min_face_px": 24,
                  "burst_frames": 8, "high_resolution": True, "fast_recognition": True, "recognition_mode": "event",
                  "recognition_interval_seconds": 0.5, "recognition_rearm_seconds": 2.0, "critical": False,
                  "require_second_factor": True, "liveness_mode": "required", "roi": [0.0, 0.0, 1.0, 1.0],
                  "gpu_decode": True, "decode_width": 1920, "decode_step": 3},
    "Balcony": {"observation_source": "snapshots", "enabled": False, "min_face_px": 48, "role": "observation",
                "mode": "standard"},
}
RIG_CFG = {"faceid": {"match_threshold": 0.5, "unknown_threshold": 0.35, "match_margin": 0.08,
                      "min_confirmations": 2, "stream_warm_seconds": 60, "backup_enabled": False,
                      "backup_dir": "/share/private-path", "access1_token": "TOKEN-SECRET"},
           "frigate": {"url": "https://f:8971", "password": "FRIGATEPW"}}


def site_dir(tmp_path, name, profiles=None, settings=None):
    data = tmp_path / name
    data.mkdir()
    if profiles is not None:
        (data / setup.PROFILES_FILE).write_text(json.dumps(profiles), encoding="utf-8")
    if settings is not None:
        (data / setup.SETTINGS_FILE).write_text(json.dumps(settings), encoding="utf-8")
    (data / setup.CONNECTION_FILE).write_text(json.dumps({"mqtt": {"password": "MQTTPW"}}), encoding="utf-8")
    return data


def export(data, cfg=RIG_CFG):
    return setup.export_site(cfg, data, CameraProfiles(data / setup.PROFILES_FILE), "6.9.3")


def test_export_contains_no_secrets_or_connection(tmp_path):
    data = site_dir(tmp_path, "rig", RIG_PROFILES)
    text = json.dumps(export(data))
    for secret in ("SECRETPW", "rtsp://", "TOKEN-SECRET", "FRIGATEPW", "MQTTPW", "private-path", "https://f"):
        assert secret not in text
    site = json.loads(text)
    assert site["format"] == "faceid-site" and site["version"] == 1
    assert site["settings"]["stream_warm_seconds"] == 60
    assert site["cameras"]["Enterance"]["decode_width"] == 1920
    assert "decode_width" not in site["cameras"]["Balcony"]


def test_reimport_on_the_same_site_changes_nothing(tmp_path):
    data = site_dir(tmp_path, "rig", RIG_PROFILES, {"stream_warm_seconds": 60})
    before = {p.name: p.read_bytes() for p in data.iterdir()}
    result = setup.import_site(RIG_CFG, data, CameraProfiles(data / setup.PROFILES_FILE), export(data),
                               {"Enterance", "Balcony"}, apply=True)
    assert result["changes"] is False and result["applied"] == []
    assert {p.name: p.read_bytes() for p in data.iterdir()} == before


def test_import_onto_a_new_site_previews_then_applies(tmp_path):
    site = export(site_dir(tmp_path, "rig", RIG_PROFILES))
    new = site_dir(tmp_path, "new")
    cfg = {"faceid": {"match_threshold": 0.5, "unknown_threshold": 0.35, "stream_warm_seconds": 0}}
    names = {"Enterance"}
    preview = setup.import_site(cfg, new, None, site, names)
    assert preview["applied"] == [] and not (new / setup.PROFILES_FILE).exists()
    assert preview["cameras"] == {"Enterance": ["new"]} and preview["skipped"] == ["Balcony"]
    assert preview["settings"]["stream_warm_seconds"] == {"from": 0, "to": 60}
    assert preview["warnings"] == [] and preview["restart_required"] is True
    result = setup.import_site(cfg, new, None, site, names, apply=True)
    assert result["applied"] == [setup.PROFILES_FILE, setup.SETTINGS_FILE]
    stored = json.loads((new / setup.PROFILES_FILE).read_text())["Enterance"]
    assert stored["direct_rtsp_url"] == "" and stored["observation_source"] == "frigate_restream"
    assert (stored["gpu_decode"], stored["decode_width"], stored["decode_step"]) == (True, 1920, 3)
    assert json.loads((new / setup.SETTINGS_FILE).read_text())["stream_warm_seconds"] == 60
    assert setup.rollback_available(new)
    assert json.loads((new / setup.CONNECTION_FILE).read_text()) == {"mqtt": {"password": "MQTTPW"}}


def test_import_keeps_the_target_address_and_other_cameras(tmp_path):
    site = export(site_dir(tmp_path, "rig", RIG_PROFILES))
    site["cameras"]["Enterance"]["observation_source"] = "direct_rtsp"
    site["cameras"]["Enterance"]["min_face_px"] = 30
    target_profiles = {"Enterance": {**RIG_PROFILES["Enterance"], "direct_rtsp_url": ""},
                       "Garage": {"observation_source": "snapshots", "role": "observation", "min_face_px": 48}}
    data = site_dir(tmp_path, "target", target_profiles)
    result = setup.import_site(RIG_CFG, data, None, site, {"Enterance", "Garage"}, apply=True)
    assert result["warnings"] == [{"camera": "Enterance", "reason": "direct_rtsp_address_needed"}]
    assert result["cameras"]["Enterance"] == ["min_face_px"]
    stored = json.loads((data / setup.PROFILES_FILE).read_text())
    assert stored["Enterance"]["observation_source"] == "frigate_restream"
    assert stored["Garage"] == target_profiles["Garage"]


def test_decode_removed_when_the_site_uses_general_settings(tmp_path):
    site = export(site_dir(tmp_path, "rig", RIG_PROFILES))
    for key in ("gpu_decode", "decode_width", "decode_step"):
        site["cameras"]["Enterance"].pop(key)
    data = site_dir(tmp_path, "target", RIG_PROFILES)
    result = setup.import_site(RIG_CFG, data, None, site, {"Enterance", "Balcony"}, apply=True)
    assert result["cameras"]["Enterance"] == ["decode_step", "decode_width", "gpu_decode"]
    assert "decode_width" not in json.loads((data / setup.PROFILES_FILE).read_text())["Enterance"]


@pytest.mark.parametrize("change, message", [
    ({"format": "other"}, "not a FaceID site file"),
    ({"settings": {"match_threshold": 1.5}}, "match_threshold must be between"),
    ({"settings": {"unknown_threshold": 0.6}}, "unknown_threshold must be lower"),
    ({"settings": {"backup_enabled": 1}}, "backup_enabled must be true or false"),
    ({"cameras": {"Enterance": {"min_face_px": 5}}}, "invalid camera Enterance"),
    ({"cameras": {"Enterance": {"decode_width": 1920}}}, "invalid stream decode"),
])
def test_invalid_site_files_are_refused_without_writing(tmp_path, change, message):
    site = {"format": "faceid-site", "version": 1, "settings": {}, "cameras": {}, **change}
    data = site_dir(tmp_path, "target", RIG_PROFILES)
    before = (data / setup.PROFILES_FILE).read_bytes()
    with pytest.raises(ValueError, match=message):
        setup.import_site(RIG_CFG, data, None, site, {"Enterance"}, apply=True)
    assert (data / setup.PROFILES_FILE).read_bytes() == before and not setup.rollback_available(data)


def test_interrupted_import_is_undone_at_startup(tmp_path, monkeypatch):
    site = export(site_dir(tmp_path, "rig", RIG_PROFILES))
    site["settings"]["stream_warm_seconds"] = 30
    site["cameras"]["Enterance"]["min_face_px"] = 40
    data = site_dir(tmp_path, "target", RIG_PROFILES, {"stream_warm_seconds": 60})
    before = {name: (data / name).read_bytes() for name in (setup.SETTINGS_FILE, setup.PROFILES_FILE)}
    original = setup._write_json

    def crash(path, value):
        if path.name == setup.PROFILES_FILE and path.parent == data:
            raise OSError("power loss")
        original(path, value)
    monkeypatch.setattr(setup, "_write_json", crash)
    with pytest.raises(OSError):
        setup.import_site(RIG_CFG, data, None, site, {"Enterance", "Balcony"}, apply=True)
    monkeypatch.setattr(setup, "_write_json", original)
    assert setup.recover_interrupted_apply(data)
    assert {name: (data / name).read_bytes() for name in before} == before


# ---------- add-on options page ----------

def run_sh_config(options: dict) -> dict:
    """Evaluate run.sh's per-option jq reads (the forms it uses) against an options.json."""
    text = (ADDON / "run.sh").read_text(encoding="utf-8")
    values = {}
    for key, expression in re.findall(r"^\s+([a-z_0-9]+): \$\(cfg_json '(.+)'\)$", text, re.M):
        if expression.startswith("(.camera_graph_json"):
            raw = options.get("camera_graph_json") or "{}"
            values[key] = json.loads(raw) if isinstance(raw, str) else raw
        elif match := re.fullmatch(r"if \.([a-z_0-9]+) == null then (\S+) else \.\1 end", expression):
            value = options.get(match[1])
            values[key] = json.loads(match[2]) if value is None else value
        elif match := re.fullmatch(r"\.([a-z_0-9]+)(?: // (.+))?", expression):
            value = options.get(match[1])
            values[key] = json.loads(match[2]) if match[2] and value in (None, False) else value
        else:
            raise AssertionError(f"unhandled run.sh expression: {expression}")
    return values


def addon_config():
    return yaml.safe_load((ADDON / "config.yaml").read_text(encoding="utf-8"))


def test_short_options_page_keeps_every_6_9_2_default():
    old = json.loads((ROOT / "tests/fixtures/addon-options-6.9.2.json").read_text())
    config = addon_config()
    assert set(config["schema"]) == set(old)
    assert len(config["options"]) == 10
    assert all(key in old for key in config["options"])
    previous, current = run_sh_config(old), run_sh_config(config["options"])
    differences = {key for key in previous if previous[key] != current[key]}
    assert differences == {"url"}  # the Frigate address no longer defaults to the test rig
    assert current["url"] == ""


def test_saved_options_read_exactly_as_saved():
    """An installation that saved its options (the rig) gets exactly those values."""
    old = json.loads((ROOT / "tests/fixtures/addon-options-6.9.2.json").read_text())
    rig = {**old, "backend": "openvino_gpu", "stream_warm_seconds": 60, "stream_decode_width": 1920,
           "liveness_required_frames": 2, "access_control_enabled": True, "access1_enabled": True,
           "frigate_url": "https://ccab4aaf-frigate:8971", "frigate_verify_tls": False,
           "clip_analysis": False, "hires_enroll": False, "reid_enabled": False, "poll_interval": 0}
    values = run_sh_config(rig)
    assert values["url"] == rig["frigate_url"] and values["verify_tls"] is False
    for key in ("backend", "stream_warm_seconds", "stream_decode_width", "liveness_required_frames",
                "clip_analysis", "hires_enroll", "reid_enabled", "poll_interval", "match_threshold"):
        assert values[key] == rig[key], key


def test_optional_options_are_grouped_and_named():
    config = addon_config()
    names = yaml.safe_load((ADDON / "translations/en.yaml").read_text(encoding="utf-8"))["configuration"]
    assert set(names) == set(config["schema"])
    for key, kind in config["schema"].items():
        assert key in config["options"] or (isinstance(kind, str) and kind.endswith("?")), key
    order = list(config["schema"])
    assert order.index("frigate_url") < order.index("mqtt_password") < order.index("backend")
    assert names["frigate_url"]["name"].startswith("Connection · ")
