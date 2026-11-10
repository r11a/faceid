"""Bounded passive commissioning recorder; never authorizes or starts recognition.

Only explicitly allowlisted scalar diagnostics leave this module. Private JPEGs
remain owner-bound in memory and are excluded from every status/report export.
"""
from collections import OrderedDict
import copy
import math
import os
from pathlib import Path
import re
import threading
import time
from uuid import uuid4

from fastapi import HTTPException

from . import VERSION

MAX_EVENTS = 32
MAX_RECORDS = 256
MAX_RESOURCES = 601
RESULT_TTL = 1800
PREVIEW_TTL = 180
MAX_PREVIEW_EVENTS = 4
MAX_PREVIEW_BYTES = 256 * 1024
SOURCES = {'snapshots', 'frigate_restream', 'direct_rtsp'}
KINDS = {'anchor', 'input', 'subject'}
ALIASES = {'live': 'input', 'associated': 'subject', 'rejected': 'subject'}
PROVIDERS = {'CPUExecutionProvider', 'CUDAExecutionProvider', 'OpenVINOExecutionProvider',
             'TensorrtExecutionProvider', 'AzureExecutionProvider', 'DmlExecutionProvider',
             'CoreMLExecutionProvider', 'ROCMExecutionProvider'}
NUMBERS = set('''sequence generation capture_timestamp arrival_age_ms duration_ms
queue_wait_ms compute_ms count required_samples qualifying_samples liveness_score
score margin threshold candidate_count usable_count anchor_count association_score
alternative_score association_margin reverse_margin queue_depth event_start_time
event_end_time pts_ms buffering_growth_ms scheduler_wait_ms detection_scheduler_wait_ms
detection_compute_ms anchor_fetch_wait_ms anchor_model_ms fetch_wait_ms model_ms
attempt attempts row det_score face_px quality quality_score identity_score identity_margin
top1_score top2_score http_status confirmations live_frames required_frames elapsed_ms
interval_ms remaining_ms deadline_remaining_ms processing_age_ms frame_age_ms
anchor_attempt candidate_index anchor_index track_index required_score required_margin
recognition_interval_seconds recognition_rearm_seconds samples min_face_px
roi_rejected_count quality_rejected_count'''.split())
BOOLEANS = set('''trust accepted retained false_positive event_ended already_handled
old_handled source_changed enabled fast_recognition high_resolution profile_fallback_used
requested_trust saved_trust persisted_trust direct_rtsp_configured source_matches_request
trust_matches_request matched_one_to_one passed required execution_confirmed
door_open_confirmed usable live confirmed duplicate event_active source_boundary
old_event_before_capture'''.split())
CATEGORIES = set('''source expected_source saved_source persisted_source requested_source
dispatch_source evidence_source reason state result phase label event_type recognition_mode
liveness_state profile_read_status configuration_read_status trust_policy source_boundary
outcome liveness_mode timing_stage context'''.split())
PROFILE_KEYS = {'observation_source', 'trust_local_video_network', 'enabled', 'recognition_mode',
                'fast_recognition', 'high_resolution', 'direct_rtsp_configured', 'liveness_mode',
                'min_face_px', 'burst_frames', 'recognition_interval_seconds',
                'recognition_rearm_seconds', 'require_second_factor'}


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and abs(value) <= 1e18


def _category(value):
    return value if isinstance(value, str) and re.fullmatch(r'[a-z][a-z0-9_]{0,95}', value) else None


def _camera(value):
    return isinstance(value, str) and bool(re.fullmatch(r'[\w .()\-]{1,100}', value))


def _event_id(value):
    return isinstance(value, str) and bool(re.fullmatch(r'[A-Za-z0-9_.\-]{1,200}', value))


def _safe_fields(fields):
    safe = {}
    for key, value in fields.items():
        if key in NUMBERS and _number(value):
            safe['stage_elapsed_ms' if key == 'elapsed_ms' else key] = value
        elif key in BOOLEANS and type(value) is bool:
            safe[key] = value
        elif key in CATEGORIES and _category(value) is not None:
            safe[key] = value
        elif key in ('source', 'evidence_source') and isinstance(value, str) and re.fullmatch(r'clip:[0-9]{1,5}', value):
            safe[key] = value
        elif key in ('profile_revision', 'requested_profile_revision', 'saved_profile_revision'):
            if isinstance(value, str) and re.fullmatch(r'[a-f0-9]{16,64}', value):
                safe[key] = value
        elif key == 'track_id' and (type(value) is int or
                isinstance(value, str) and re.fullmatch(r'[a-f0-9\-]{1,40}', value)):
            safe[key] = value
        elif key == 'error':
            safe[key] = value if isinstance(value, str) and value in {'capture_failed', 'profile_read_failed', 'metrics_unavailable',
                'preview_unavailable', 'engine_unavailable'} else 'capture_failed'
    return safe


def _metric_dict(value):
    allowed = {'calls', 'failures', 'samples', 'p50_ms', 'p95_ms', 'elapsed_ms',
               'requests', 'unavailable', 'rejected', 'decoded_frames',
               'frame_requests', 'unavailable_frames', 'rejected_frames'}
    return {key: item for key, item in value.items() if key in allowed and _number(item)} if isinstance(value, dict) else {}


def _engine_snapshot(engine):
    raw = engine.health() if engine is not None and hasattr(engine, 'health') else {}
    result = {}
    for key in ('backend', 'model_pack'):
        if _category(raw.get(key)) is not None:
            result[key] = raw[key]
    for key in ('det_size', 'startup_seconds'):
        if _number(raw.get(key)):
            result[key] = raw[key]
    for key in ('providers', 'available_providers'):
        result[key] = [item for item in raw.get(key, [])[:16] if item in PROVIDERS]
    tuning = raw.get('cpu_tuning') or {}
    result['cpu_tuning'] = {key: tuning[key] for key in ('threads', 'allow_spinning', 'applied')
                            if type(tuning.get(key)) in (int, bool)}
    result['runtime'] = {key: value for key, value in (raw.get('runtime') or {}).items()
                         if key in ('insightface', 'onnxruntime') and isinstance(value, str)
                         and re.fullmatch(r'[0-9][0-9A-Za-z.+\-]{0,48}', value)}
    for key in ('inference', 'embedding', 'stream_detection'):
        value = raw.get(key) or {}
        result[key] = _metric_dict(value)
        for part in ('compute', 'scheduler_wait', 'warmup'):
            if isinstance(value.get(part), dict):
                result[key][part] = _metric_dict(value[part])
    return result


def _runtime_limits():
    result = {'logical_cpus': os.cpu_count(), 'cpu_quota_cpus': None}
    try:
        result['affinity_cpus'] = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        pass
    try:
        quota, period = Path('/sys/fs/cgroup/cpu.max').read_text().split()
        result['cpu_period_us'] = int(period)
        if quota != 'max' and int(quota) > 0 and int(period) > 0:
            result['cpu_quota_cpus'] = int(quota) / int(period)
    except (OSError, ValueError):
        try:
            quota = int(Path('/sys/fs/cgroup/cpu/cpu.cfs_quota_us').read_text())
            period = int(Path('/sys/fs/cgroup/cpu/cpu.cfs_period_us').read_text())
            if quota > 0 and period > 0:
                result['cpu_quota_cpus'] = quota / period
        except (OSError, ValueError):
            pass
    try:
        value = Path('/sys/fs/cgroup/memory.max').read_text().strip()
        result['memory_limit_bytes'] = int(value) if value != 'max' else None
    except (OSError, ValueError):
        pass
    return result


def _rss_bytes():
    try:
        return int(Path('/proc/self/statm').read_text().split()[1]) * os.sysconf('SC_PAGE_SIZE')
    except (OSError, ValueError, IndexError, AttributeError):
        return None


def _cpu_throttle():
    """Cgroup counters include other processes in that group, not just FaceID."""
    for path, version in (('/sys/fs/cgroup/cpu.stat', 2), ('/sys/fs/cgroup/cpu/cpu.stat', 1)):
        try:
            pairs = dict(line.split() for line in Path(path).read_text().splitlines())
            return {'cgroup_version': version, 'periods': int(pairs['nr_periods']),
                    'throttled_periods': int(pairs['nr_throttled']),
                    'throttled_usec': int(pairs['throttled_usec']) if version == 2 else int(pairs['throttled_time']) / 1000}
        except (OSError, KeyError, ValueError):
            pass
    return {'cgroup_version': None, 'periods': None, 'throttled_periods': None, 'throttled_usec': None}


class RecognitionCapture:
    def __init__(self, processor):
        self.processor = processor
        self.runtime_id = str(uuid4())
        self.lock = threading.RLock()
        self.owner = None
        self.run = None
        self.previews = {}
        self.preview_times = {}
        self.anchor_saved = set()
        self.preview_pending = set()
        self.preview_recent = []
        self.thread = None
        self.wake = threading.Event()

    def _snapshot(self, camera):
        result = {}
        try:
            profiles = getattr(self.processor, 'camera_profiles', None)
            profile = profiles.get(camera) if profiles else {}
            safe = {}
            for key in PROFILE_KEYS:
                value = profile.get(key)
                if type(value) is bool or _number(value) or _category(value) is not None:
                    safe[key] = value
            safe['camera'] = camera
            result['profile'] = safe
            from .camera_profiles import profile_capture_fields
            result['profile_revision'] = profile_capture_fields(profile)['profile_revision']
            read = profiles.read_status() if profiles and hasattr(profiles, 'read_status') else {}
            result['profile_read_status'] = _category(read.get('status')) or 'unavailable'
            result['profile_fallback_used'] = read.get('fallback_used') is True
        except Exception:
            result.update(profile={}, profile_read_status='read_failed', profile_fallback_used=True)
        try:
            result['engine'] = _engine_snapshot(getattr(self.processor, 'engine', None))
        except Exception:
            result['engine'] = {'error': 'metrics_unavailable'}
        try:
            result['mqtt'] = {'connected': self.processor.mqtt_health().get('connected') is True}
            if hasattr(self.processor, 'mqtt_diagnostics'):
                result['mqtt'].update(self.processor.mqtt_diagnostics.status(camera))
        except Exception:
            result['mqtt'] = {'connected': None}
        result['resources'] = _runtime_limits()
        result['capacity'] = self._queues()
        return result

    def _queues(self):
        result = {}
        for key, name in (('snapshot_queue', 'queue'), ('clip_queue', 'clip_queue')):
            try:
                result[key] = int(getattr(self.processor, name).qsize())
            except Exception:
                result[key] = None
        for name in ('stream_observations', 'continuous'):
            service = getattr(self.processor, name, None)
            if service is not None:
                try:
                    with service.lock:
                        result[name] = {field: len(getattr(service, field, ()))
                                        for field in ('sessions', 'pending', 'anchor_pending')}
                except Exception:
                    pass
        pool = getattr(getattr(self.processor, 'frigate', None), 'live_frames', None)
        if pool is not None:
            try:
                with pool.lock:
                    result['readers'] = len(pool.readers)
                    result['retiring_readers'] = len(pool.retiring)
                    result['observation_readers'] = len(pool.observation_sources)
            except Exception:
                pass
        return result

    def _prune_locked(self, now):
        self.previews = {key: value for key, value in self.previews.items() if now < value[0]}
        if self.run and not self.run['recording'] and now >= self.run['ended_monotonic'] + RESULT_TTL:
            self.run = None
            self.owner = None
            self.previews.clear()
            self.preview_times.clear()
            self.anchor_saved.clear()
            self.preview_pending.clear()

    def _check_owner_locked(self, owner):
        if self.owner is not None and owner != self.owner:
            raise HTTPException(403, 'Recognition capture belongs to another administrator')

    def start(self, owner, camera, expected_source, duration_seconds=300):
        if not owner or not _camera(camera) or not isinstance(expected_source, str) or expected_source not in SOURCES or type(duration_seconds) is not int or not 1 <= duration_seconds <= 600:
            raise HTTPException(400, 'Invalid recognition capture request')
        with self.lock:
            if self.run and self.run['recording']:
                raise HTTPException(409, 'A recognition capture is already running')
        before = self._snapshot(camera)
        now = time.monotonic()
        with self.lock:
            self._prune_locked(now)
            if self.run and self.run['recording']:
                raise HTTPException(409, 'A recognition capture is already running')
            self.owner = owner
            self.previews.clear()
            self.preview_times.clear()
            self.anchor_saved.clear()
            self.preview_pending.clear()
            self.preview_recent = []
            self.run = {'capture_id': str(uuid4()), 'runtime_id': self.runtime_id, 'build': VERSION,
                        'camera': camera, 'expected_source': expected_source, 'state': 'recording',
                        'recording': True, 'started_at': time.time(), 'started_monotonic': now,
                        'deadline_monotonic': now + duration_seconds, 'duration_seconds': duration_seconds,
                        'before': before, 'after': None, 'events': OrderedDict(), 'records': [],
                        'resources': [], 'mismatches': [], 'truncation': {'events': 0, 'records': 0,
                        'fields': 0, 'resources': 0, 'previews': 0, 'mismatches': 0,
                        'coalesced_records': 0}, 'last_cpu': (now, time.process_time()),
                        'last_throttle': _cpu_throttle()}
            self.run['preflight'] = self._preflight(self.run, before)
            if not self.run['preflight']['source_matches']:
                self._mismatch_locked('selected_saved_source', before.get('profile', {}).get('observation_source'))
            if not self.thread or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._run, daemon=True, name='recognition-capture')
                self.thread.start()
            self.wake.set()
        return self.status(owner)

    @staticmethod
    def _preflight(run, snapshot):
        profile = snapshot.get('profile', {})
        return {'expected_source': run['expected_source'], 'saved_source': profile.get('observation_source'),
                'source_matches': profile.get('observation_source') == run['expected_source'],
                'enabled': profile.get('enabled') is True,
                'trust_local_video_network': profile.get('trust_local_video_network') is True,
                'profile_read_status': snapshot.get('profile_read_status'),
                'profile_fallback_used': snapshot.get('profile_fallback_used') is True,
                'changes_settings': False, 'starts_recognition': False}

    def _mismatch_locked(self, reason, source=None, event_id=None):
        if len(self.run['mismatches']) >= 128:
            self.run['truncation']['mismatches'] += 1
            return
        item = {'reason': reason, 'expected_source': self.run['expected_source']}
        if _category(source) is not None:
            item['actual_source'] = source
        if event_id:
            item['event_id'] = event_id
        self.run['mismatches'].append(item)

    def is_active(self, camera=None):
        with self.lock:
            return bool(self.run and self.run['recording'] and time.monotonic() < self.run['deadline_monotonic']
                        and (camera is None or camera == self.run['camera']))

    def _finish(self, reason, capture_id=None):
        with self.lock:
            if not self.run or not self.run['recording'] or capture_id and self.run['capture_id'] != capture_id:
                return
            self.run.update(recording=False, state='stopped' if reason == 'operator_stop' else 'complete',
                            finish_reason=reason, ended_at=time.time(), ended_monotonic=time.monotonic())
            capture_id, camera = self.run['capture_id'], self.run['camera']
        after = self._snapshot(camera)
        with self.lock:
            if self.run and self.run['capture_id'] == capture_id:
                self.run['after'] = after
                if after.get('profile', {}).get('observation_source') != self.run['expected_source']:
                    self._mismatch_locked('selected_saved_source_at_end', after.get('profile', {}).get('observation_source'))

    def stop(self, owner):
        with self.lock:
            self._prune_locked(time.monotonic())
            self._check_owner_locked(owner)
            capture_id = self.run['capture_id'] if self.run else None
        if capture_id:
            self._finish('operator_stop', capture_id)
        self.wake.set()
        return self.status(owner)

    def _tick(self):
        now = time.monotonic()
        with self.lock:
            self._prune_locked(now)
            if not self.run:
                return
            capture_id = self.run['capture_id']
            expired = self.run['recording'] and now >= self.run['deadline_monotonic']
            recording = self.run['recording']
        if expired:
            self._finish('deadline', capture_id)
            return
        if not recording:
            return
        cpu, rss, queues, throttle = time.process_time(), _rss_bytes(), self._queues(), _cpu_throttle()
        with self.lock:
            if not self.run or self.run['capture_id'] != capture_id or not self.run['recording']:
                return
            previous, previous_cpu = self.run['last_cpu']
            if now - previous < 1:
                return
            percent = max(0.0, 100 * (cpu - previous_cpu) / (now - previous))
            sample = {'elapsed_ms': round((now-self.run['started_monotonic'])*1000, 1),
                      'process_cpu_percent_one_core': round(percent, 2), 'process_cpu_seconds': cpu,
                      'rss_bytes': rss, 'queues': queues,
                      'cgroup_cpu_throttle': {**throttle, 'scope': 'container_cgroup_not_per_event'}}
            previous_throttle = self.run['last_throttle']
            for key in ('periods', 'throttled_periods', 'throttled_usec'):
                if _number(throttle.get(key)) and _number(previous_throttle.get(key)):
                    sample['cgroup_cpu_throttle'][key+'_delta'] = max(0, throttle[key]-previous_throttle[key])
            quota = self.run['before']['resources'].get('cpu_quota_cpus')
            if quota:
                sample['process_cpu_percent_quota'] = round(percent / quota, 2)
            if len(self.run['resources']) < MAX_RESOURCES:
                self.run['resources'].append(sample)
            else:
                self.run['truncation']['resources'] += 1
            self.run['last_cpu'] = (now, cpu)
            self.run['last_throttle'] = throttle

    def _run(self):
        while True:
            self.wake.wait(1.0)
            self.wake.clear()
            try:
                self._tick()
            except Exception:
                pass  # Recording must not stop or fail production services.
            with self.lock:
                if self.run is None:
                    self.thread = None
                    return

    def emit(self, event_id, camera, stage, **fields):
        if not self.is_active(camera):
            return
        with self.lock:
            if not self.is_active(camera):
                return
            if not isinstance(stage, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', stage) or event_id not in (None, '') and not _event_id(event_id):
                self.run['truncation']['records'] += 1
                return
            safe = _safe_fields(fields)
            self.run['truncation']['fields'] += len(fields) - len(safe)
            record = {'stage': stage, 'elapsed_ms': round((time.monotonic()-self.run['started_monotonic'])*1000, 1), **safe}
            if event_id:
                events = self.run['events']
                if event_id not in events:
                    if len(events) >= MAX_EVENTS:
                        self.run['truncation']['events'] += 1
                        return
                    events[event_id] = {'event_id': event_id, 'camera': camera, 'started_before_capture': None, 'records': []}
                event = events[event_id]
                if _number(fields.get('event_start_time')):
                    event['started_before_capture'] = fields['event_start_time'] < self.run['started_at']
                records = event['records']
            else:
                records = self.run['records']
            if (records and record['elapsed_ms']-records[-1]['elapsed_ms'] < 1000
                    and {key:value for key,value in record.items() if key != 'elapsed_ms'}
                    == {key:value for key,value in records[-1].items() if key != 'elapsed_ms'}):
                self.run['truncation']['coalesced_records'] += 1
                return
            if len(records) >= MAX_RECORDS:
                records.pop(1)  # Keep first trigger/context plus the latest tail.
                self.run['truncation']['records'] += 1
            records.append(record)
            actual = safe.get('dispatch_source')
            if actual is None and stage in {'mqtt_dispatch', 'dispatch', 'stream_trigger', 'snapshot_dispatch', 'stream_route', 'stream_trigger_received'}:
                actual = safe.get('saved_source') or safe.get('source')
            if actual in SOURCES and actual != self.run['expected_source']:
                self._mismatch_locked('selected_dispatch_source', actual, event_id)

    def preview(self, event_id, camera, image, kind, bbox=None):
        kind = ALIASES.get(kind, kind)
        if kind not in KINDS or not self.is_active(camera) or not _event_id(event_id):
            return
        import cv2
        import numpy as np
        if not isinstance(image, np.ndarray) or image.dtype != np.uint8 or image.ndim != 3 or image.shape[2] != 3 or image.nbytes > 32_000_000 or not image.size:
            return
        if bbox is not None:
            if len(bbox) != 4 or not all(math.isfinite(float(value)) for value in bbox):
                return
            left, top, right, bottom = [int(value) for value in bbox]
            image = image[max(0,top):min(image.shape[0],bottom), max(0,left):min(image.shape[1],right)]
            if not image.size:
                return
        now = time.monotonic()
        with self.lock:
            self._prune_locked(now)
            if not self.is_active(camera) or event_id not in self.run['events']:
                return
            key = (event_id, kind)
            seen = {item[0] for item in self.preview_times}
            if event_id not in seen and len(seen) >= MAX_PREVIEW_EVENTS:
                self.run['truncation']['previews'] += 1
                return
            if key in self.preview_pending or kind == 'anchor' and key in self.anchor_saved:
                return
            self.preview_recent = [timestamp for timestamp in self.preview_recent if now-timestamp < 1]
            if now-self.preview_times.get(key, -float('inf')) < (1 if kind == 'anchor' else 2):
                return
            if kind != 'anchor':
                if len(self.preview_recent) >= 2:
                    return
                self.preview_recent.append(now)
            capture_id = self.run['capture_id']
            self.preview_times[key] = now
            self.preview_pending.add(key)
        success = False
        try:
            scale = min(1., 640/max(image.shape[:2]))
            small = cv2.resize(image, (max(1, int(image.shape[1]*scale)), max(1,int(image.shape[0]*scale)))) if scale < 1 else image
            ok, encoded = cv2.imencode('.jpg', small, [cv2.IMWRITE_JPEG_QUALITY, 75])
            if not ok or encoded.nbytes > MAX_PREVIEW_BYTES:
                return
            with self.lock:
                if self.run and self.run['capture_id'] == capture_id and self.is_active(camera):
                    self.previews[key] = (now+PREVIEW_TTL, encoded.tobytes())
                    if kind == 'anchor':
                        self.anchor_saved.add(key)
                    success = True
        finally:
            with self.lock:
                if self.run and self.run['capture_id'] == capture_id:
                    self.preview_pending.discard(key)
                    if not success:
                        self.run['truncation']['previews'] += 1
                        self.emit(event_id, camera, 'capture_preview', reason='preview_encode_failed')

    def get_preview(self, owner, event_id, kind):
        with self.lock:
            self._prune_locked(time.monotonic())
            self._check_owner_locked(owner)
            item = self.previews.get((event_id, kind)) if kind in KINDS else None
            if item is None:
                raise HTTPException(404, 'Recognition capture preview unavailable or expired')
            return item[1]

    def _public(self, owner, full):
        # Deadline and TTL are enforced even if the timer thread is delayed.
        now = time.monotonic()
        with self.lock:
            self._prune_locked(now)
            self._check_owner_locked(owner)
            expired = bool(self.run and self.run['recording'] and now >= self.run['deadline_monotonic'])
            capture_id = self.run['capture_id'] if self.run else None
        if expired:
            self._finish('deadline', capture_id)
        with self.lock:
            self._check_owner_locked(owner)
            if self.run is None:
                return {'state': 'idle', 'recording': False, 'build': VERSION, 'runtime_id': self.runtime_id}
            value = {key: item for key, item in self.run.items() if key not in
                     {'events', 'resources', 'records', 'last_cpu', 'last_throttle', 'started_monotonic', 'deadline_monotonic', 'ended_monotonic'}}
            if hasattr(self.processor, 'mqtt_diagnostics'):
                value['mqtt_current'] = self.processor.mqtt_diagnostics.status(self.run['camera']) if self.run['recording'] else (self.run.get('after') or {}).get('mqtt')
            value['remaining_seconds'] = max(0, round(self.run['deadline_monotonic']-now, 1)) if self.run['recording'] else 0
            value['event_count'] = len(self.run['events'])
            value['limits'] = {'events': MAX_EVENTS, 'records_per_event': MAX_RECORDS, 'run_records': MAX_RECORDS,
                               'max_duration_seconds': 600, 'resource_samples': MAX_RESOURCES,
                               'result_ttl_seconds': RESULT_TTL, 'preview_events': MAX_PREVIEW_EVENTS,
                               'preview_ttl_seconds': PREVIEW_TTL, 'preview_bytes_each': MAX_PREVIEW_BYTES}
            value['resource_measurement'] = 'whole_process_including_normal_work_and_capture_overhead_not_per_event'
            value['capture_changes_settings'] = False
            value['capture_starts_recognition'] = False
            value['normal_recognition_and_access1_continue'] = True
            value['previews'] = [{'event_id': eid, 'kinds': sorted(kind for (event_id,kind) in self.previews if event_id == eid),
                                  'expires_in': round(max(expiry for (event_id,_), (expiry,_) in self.previews.items() if event_id == eid)-now,1)}
                                 for eid in sorted({event_id for event_id,_ in self.previews})]
            if full:
                value.update(events=list(self.run['events'].values()), records=self.run['records'], resources=self.run['resources'])
            else:
                events = list(self.run['events'].values())[-8:]
                value['events'] = [{**event, 'records': event['records'] if len(event['records']) <= 9
                                   else [event['records'][0], *event['records'][-8:]]} for event in events]
                value['records'] = self.run['records'][-16:]
                value['resources'] = self.run['resources'][-5:]
                value['view_limits'] = {'events':8, 'records_per_event':9, 'run_records':16, 'resource_samples':5,
                                       'full_report_endpoint':'api/recognition-capture'}
            return copy.deepcopy(value)

    def status(self, owner):
        return self._public(owner, False)

    def report(self, owner):
        return self._public(owner, True)


def active(processor, camera=None):
    try:
        capture = getattr(processor, 'recognition_capture', None)
        return bool(capture and capture.is_active(camera))
    except Exception:
        return False


def emit(processor, event_id, camera, stage, **fields):
    try:
        capture = getattr(processor, 'recognition_capture', None)
        if capture is not None:
            capture.emit(event_id, camera, stage, **fields)
    except Exception:
        pass


def preview(processor, event_id, camera, image, kind, bbox=None):
    try:
        capture = getattr(processor, 'recognition_capture', None)
        if capture is not None:
            capture.preview(event_id, camera, image, kind, bbox)
    except Exception:
        pass
