"""Opt-in timing experiment. Never creates events, stores images or submits access.

Uses existing models and policy primitives; all sample state is local to this run.
Decoded arrival/PTS is NOT authenticated capture time or admission evidence.
"""
from collections import Counter
from contextlib import nullcontext
import hashlib
import math
import os
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit

import cv2

from .live_frames import decoder_position_ms
from .quality import measure_face_quality
from .liveness import qualified_live_consensus
from .decision import DecisionAccumulator, DecisionPolicy


def blockers(processor, camera):
    result = []
    # The diagnostic only uses local sample state and never calls the event or
    # Access1 pipeline. Operational recognition may run alongside it. Models
    # retain background priority; opening this page does not change camera state.
    if getattr(processor, 'engine', None) is None:
        result.append('face_engine_unavailable')
    return result


def historical_timings(processor):
    try:
        values=processor.performance()
        return {k:v for k,v in values.items() if isinstance(v,dict) and set(v)=={'samples','p50_ms','p95_ms'}}
    except Exception:return {}


def runtime_limits():
    result={'logical_cpus':os.cpu_count()}
    if hasattr(os,'sched_getaffinity'):result['available_cpu_affinity_count']=len(os.sched_getaffinity(0))
    for name,path in [('cpu_quota','/sys/fs/cgroup/cpu.max'),('memory_limit','/sys/fs/cgroup/memory.max')]:
        try:result[name]=Path(path).read_text().strip()[:64]
        except OSError:pass
    try:
        flags=next((line.split(':',1)[1].split() for line in Path('/proc/cpuinfo').read_text().splitlines()
                    if line.startswith('flags') and ':' in line),[])
        result['cpu_vector_features']=[x for x in ('sse4_2','avx','avx2','fma') if x in flags]
    except OSError:pass
    return result


class DecodeReader:
    """One temporary decoder, one latest-frame slot; never queue old images."""
    def __init__(self, url, seconds=45):
        self.url, self.seconds = url, seconds
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.latest = None
        self.stats = {'decoded_frames': 0, 'pts_missing': 0, 'pts_repeated': 0,
                      'pts_backward': 0, 'decoder_relative_backlog': 0,
                      'relative_backlog_max_ms': 0., 'read_ms_total': 0.0, 'reader_thread_cpu_ms': 0.0}
        self.thread = threading.Thread(target=self.run, daemon=True, name='faceid-decode-diagnostic')

    def run(self):
        capture = None
        begin = time.monotonic()
        previous = None
        minimum_offset = None
        try:
            capture = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG, [
                cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 3000,
                cv2.CAP_PROP_READ_TIMEOUT_MSEC, 1000,
                cv2.CAP_PROP_N_THREADS, 1])
            with self.lock:
                self.stats['open_ms'] = round((time.monotonic()-begin)*1000, 2)
            if not capture.isOpened():
                with self.lock:self.stats['error'] = 'decoder_open_failed'
                return
            while not self.stop_event.is_set() and time.monotonic()-begin < self.seconds:
                started, cpu = time.monotonic(), time.thread_time()
                ok, frame = capture.read()
                now = time.monotonic()
                if not ok or frame is None:
                    with self.lock:self.stats['error'] = 'decoder_read_failed_or_ended'
                    break
                if frame.nbytes > 32_000_000:
                    with self.lock:self.stats['error'] = 'decoded_frame_too_large'
                    break
                pts = float(decoder_position_ms(capture))
                reason = None
                if not math.isfinite(pts):reason = 'pts_missing'
                elif previous is not None and pts <= previous:
                    reason = 'pts_repeated' if pts == previous else 'pts_backward'
                previous = pts if math.isfinite(pts) else previous
                relative_backlog = 0.
                if reason is None:
                    offset = now-pts/1000
                    minimum_offset = offset if minimum_offset is None else min(minimum_offset,offset)
                    relative_backlog = max(0.,offset-minimum_offset)*1000
                    if relative_backlog > 750:reason='decoder_relative_backlog'
                with self.lock:
                    self.stats['decoded_frames'] += 1
                    self.stats['read_ms_total'] += (now-started)*1000
                    self.stats['reader_thread_cpu_ms'] += (time.thread_time()-cpu)*1000
                    if reason:self.stats[reason] += 1
                    self.stats['relative_backlog_max_ms']=round(max(self.stats['relative_backlog_max_ms'],relative_backlog),2)
                    self.stats.setdefault('first_decoded_frame_ms', round((now-begin)*1000, 2))
                    self.stats['resolution'] = [int(frame.shape[1]), int(frame.shape[0])]
                    self.latest = (self.stats['decoded_frames'], now, pts, reason, frame)
        except Exception:
            with self.lock:self.stats['error'] = 'decoder_failed'
        finally:
            if capture is not None:
                try:capture.release()
                except Exception:pass
            with self.lock:self.stats['finished'] = True

    def take(self):
        with self.lock:return self.latest, dict(self.stats)

    def stop(self):
        self.stop_event.set()
        self.thread.join(4)
        with self.lock:
            self.latest = None
            self.stats['decoder_stopped'] = not self.thread.is_alive()


def run_decode(processor, camera, url, *, seconds=45, reader_factory=DecodeReader,
               region=None, preview=None, benchmarks=False, benchmark_runner=None, sample_interval=.5):
    report = {'mode': 'decode_chain', 'access1_submission': 'not_implemented',
              'frames_saved': 0, 'freshness_proven': False, 'admission_eligible': False,
              'sample_window_seconds': seconds, 'samples': [], 'trials': []}
    reasons = blockers(processor, camera)
    if reasons:return {**report, 'blockers': reasons}
    parsed = urlsplit(url)
    if parsed.scheme != 'rtsp' or parsed.username or parsed.password:
        return {**report, 'error': 'anonymous_configured_restream_required'}
    from .crop_diagnostics import compare, preview_images, validate_region
    from .embedding_benchmark import prepare, run as run_benchmark
    if region is not None:region=validate_region(region)
    benchmark_runner=benchmark_runner or run_benchmark
    benchmark_payload=None
    preview_sent=False
    preview_last=0.
    face_samples=0
    next_sample=0.
    report['mode']='crop_compare' if region is not None else 'decode_chain'
    report['budgets']={'search_samples':120,'single_face_samples':24,'minimum_sample_interval_ms':round(sample_interval*1000,2)}
    if region is not None:
        report['comparison_region']=region
        report['comparison_note']='Full and crop detection use the same frame, alternating order. Extra comparison time is not production latency.'
    engine, gallery = processor.engine, processor.gallery
    live = getattr(processor, 'liveness', None)
    profile = processor.camera_profiles.get(camera) if processor.camera_profiles else {}
    hour = time.localtime().tm_hour
    min_px = profile.get('night_min_face_px' if hour >= 19 or hour < 6 else 'min_face_px',processor.min_face_px)
    policy = DecisionPolicy(**{k:getattr(processor,k) for k in (
        'match_threshold','unknown_threshold','match_margin','ignore_threshold','ignore_margin','min_confirmations')
        if hasattr(processor,k)})
    # Processor names predate DecisionPolicy; retain the exact configured values.
    policy.match_threshold = processor.match_thr
    policy.unknown_threshold = processor.unknown_thr
    policy.ignore_threshold = processor.ignore_thr
    required = max(3, getattr(live,'required_frames',3), policy.min_confirmations)
    report['policy'] = {**vars(policy), 'min_face_px':min_px,
                        'min_face_quality':processor.min_face_quality,
                        'liveness_required_samples_for_test':required,
                        'liveness_threshold':getattr(live,'threshold',None),
                        'production_liveness_mode':profile.get('liveness_mode')}
    report['engine_before'] = engine.health()
    report['diagnostic_only_runtime']=getattr(processor,'diagnostic_only',False) is True
    report['historical_production_timings_not_this_trial']=historical_timings(processor)
    report['runtime_limits'] = runtime_limits()
    report['limitations'] = ['Arrival time and decoder PTS do not prove capture freshness.',
        'No Access1, relay, MQTT trigger latency or authorization is exercised.',
        'Shared models prioritize normal work; diagnostic native model calls must finish before cleanup.',
        'Single-face test; guest credentials and physical relay feedback are outside this experiment.']
    reader = reader_factory(url,seconds=seconds)
    begin = time.monotonic()
    trial_begin = begin
    last_seq = 0
    hashes = set()
    embedding_hashes = set()
    history = []
    subject = None
    labels = {}
    counts = Counter()
    totals = Counter()
    accumulator = DecisionAccumulator(policy)
    model_subject=None
    model_samples=0
    reader.thread.start()
    try:
        while time.monotonic()-begin < seconds and len(report['samples']) < 120 and face_samples < 24 and len(report['trials']) < 3:
            safety = blockers(processor,camera)
            if safety:
                report['blockers'] = safety
                break
            if time.monotonic()<next_sample:
                time.sleep(max(0.,min(.05,next_sample-time.monotonic())));continue
            item, stats = reader.take()
            if item is None or item[0] == last_seq:
                if stats.get('finished'):break
                time.sleep(.05)
                continue
            seq, received, pts, pts_reason, frame = item
            last_seq = seq
            next_sample=time.monotonic()+sample_interval
            sample = {'sequence':seq,'arrival_since_start_ms':round((received-begin)*1000,2),
                      'decoder_pts_ms':pts if math.isfinite(pts) else None,'timings_ms':{}}
            report['samples'].append(sample)
            def measured(stage, fn):
                if blockers(processor,camera):raise RuntimeError('safety_changed')
                if time.monotonic()-begin >= seconds:raise TimeoutError('sample_window_finished')
                report['last_stage']=stage
                start = time.monotonic()
                value = fn()
                elapsed = (time.monotonic()-start)*1000
                sample['timings_ms'][stage] = round(elapsed,2)
                totals[stage] += elapsed
                return value
            def reject(reason):
                nonlocal history, subject, accumulator, model_samples, model_subject
                history=[];subject=None;accumulator=DecisionAccumulator(policy)
                model_samples=0;model_subject=None
                counts[reason] += 1
                sample['result'] = reason
            # Reject transport-invalid or locally stale frames before expensive inference.
            # PTS progression is still NOT capture-age proof or admission evidence.
            age = time.monotonic()-received
            sample['decoded_arrival_age_ms'] = round(age*1000,2)
            if pts_reason:
                reject(pts_reason);continue
            if not math.isfinite(pts):
                reject('pts_missing');continue
            if age < 0 or age > 1:
                reject('decoded_frame_stale');continue
            digest = measured('frame_hash',lambda:hashlib.sha256(frame.tobytes()).hexdigest())
            if digest in hashes:
                reject('duplicate_pixels');continue
            hashes.add(digest)
            background = engine.background_work() if hasattr(engine,'background_work') else nullcontext()
            with background:
                if region is None:
                    found = measured('detection_with_scheduler',lambda:engine.detect_faces(frame))
                else:
                    found,comparison=measured('detection_comparison',lambda:compare(engine,frame,region,len(report['samples'])%2==0))
                    sample['comparison']=comparison
                    if preview and (not preview_sent or time.monotonic()-preview_last>=2):
                        measured('preview_encoding',lambda:preview(preview_images(frame,region,getattr(engine,'det_size',640))))
                        preview_sent=True
                        preview_last=time.monotonic()
                # Reject ambiguity before ROI filtering, rather than selecting a bystander.
                if len(found) != 1:
                    history=[];subject=None;accumulator=DecisionAccumulator(policy)
                    reject('no_face' if not found else 'multiple_faces');continue
                face_samples+=1
                face = found[0]
                if profile.get('mode') == 'intercom':
                    l,t,r,b = profile.get('roi',[0,0,1,1]);h,w=frame.shape[:2]
                    if not (l <= float(face.bbox[0]+face.bbox[2])/2/w <= r and
                            t <= float(face.bbox[1]+face.bbox[3])/2/h <= b):
                        reject('outside_roi');continue
                quality = measured('quality',lambda:measure_face_quality(frame,face,
                    min_face_px=min_px,min_quality=processor.min_face_quality))
                sample['quality'] = quality.to_dict()
                if not quality.usable:reject('low_quality');continue
                if live is None:
                    pad={'state':'unavailable','live':None}
                else:
                    start=time.monotonic()
                    with engine.model_work():
                        wait=(time.monotonic()-start)*1000
                        sample['timings_ms']['liveness_scheduler_wait']=round(wait,2)
                        totals['liveness_scheduler_wait']+=wait
                        pad=measured('liveness_compute',lambda:live.analyze(frame,face))
                sample['liveness']={k:pad.get(k) for k in ('state','live','score','threshold','face_px')}
                timing={}
                measured('embedding_total',lambda:engine.embed_face(frame,face,timings=timing))
                for key,value in timing.items():sample['timings_ms'][key]=round(value*1000,2)
                observation_key=hashlib.sha256(face.normed_embedding.astype('float32').tobytes()).hexdigest()
                if observation_key in embedding_hashes:
                    reject('duplicate_face_embedding');continue
                embedding_hashes.add(observation_key)
                candidates=measured('gallery_match',lambda:gallery.match_candidates(face.normed_embedding,limit=3))
                ignored=measured('ignore_match',lambda:gallery.match_ignored_detail(face.normed_embedding))
                slug,name,score=candidates[0] if candidates else (None,None,0.)
                runner_score=float(candidates[1][2]) if len(candidates)>1 else 0.
                label=labels.setdefault(slug,'subject_'+str(len(labels)+1)) if slug else None
                sample.update(candidate=label,score=float(score),margin=float(score)-runner_score)
                if subject != slug:
                    history=[];accumulator=DecisionAccumulator(policy);subject=slug
                decision=accumulator.add(key=observation_key,slug=slug,person=label,score=float(score),
                    runner_up='other' if len(candidates)>1 else None,runner_up_score=runner_score,
                    ignore_score=float(ignored[0]),ignore_group=ignored[2])
                sample['identity_state']=decision.status
                if pad.get('live') is True and decision.status in ('recognized','recognized_candidate'):
                    if benchmarks and benchmark_payload is None:
                        try:benchmark_payload=prepare(engine,frame,face)
                        except Exception:report['benchmark_preparation']='unavailable'
                    model_samples=model_samples+1 if model_subject==slug else 1
                    model_subject=slug
                    if model_samples==3:
                        report.setdefault('first_three_model_valid_samples_ms',round((time.monotonic()-begin)*1000,2))
                        report['model_samples_are_not_freshness_proof']=True
                else:
                    model_samples=0;model_subject=None
                # Timing can be measured on rejected samples, but they never count.
                if pts_reason:reject(pts_reason);history=[];continue
                if time.monotonic()-received >= 10:reject('processing_exceeded_evidence_window');history=[];continue
                if pad.get('live') is not True:reject('liveness_'+str(pad.get('state','unavailable')));history=[];continue
                if decision.status not in ('recognized','recognized_candidate'):
                    reject('identity_'+decision.status);history=[];continue
                history.append(pad)
                sample['result']='diagnostic_usable'
                sample['consecutive_usable']=len(history)
                counts['diagnostic_usable']+=1
                if len(history)>=required and qualified_live_consensus(live,history,required)['confirmed'] and decision.status=='recognized':
                    report['trials'].append({'elapsed_ms':round((time.monotonic()-trial_begin)*1000,2),
                        'samples':len(history),'subject':label,'admission_eligible':False})
                    history=[];subject=None;accumulator=DecisionAccumulator(policy)
                    trial_begin=time.monotonic()
            # next_sample enforces cadence; inference time already counts toward it.
    except TimeoutError:
        report['stop_reason']='sample_window_finished'
    except Exception:
        report['error']='diagnostic_processing_failed'
        report['blockers']=blockers(processor,camera)
    finally:
        reader.stop()
        _,report['decoder']=reader.take()
        report['elapsed_ms']=round((time.monotonic()-begin)*1000,2)
        report['latest_buffer_frames_not_processed']=max(0,report['decoder'].get('decoded_frames',0)-len(report['samples']))
        report['reasons']=dict(counts)
        report['stage_totals_ms']={k:round(v,2) for k,v in totals.items()}
        report['engine_after']=engine.health()
        report['outcome']='timing_trials_completed' if len(report['trials'])==3 else 'incomplete_observation'
        report['single_face_samples']=face_samples
        report.setdefault('stop_reason','trials_completed' if len(report['trials'])==3 else
                          'face_sample_limit' if face_samples>=24 else
                          'search_sample_limit' if len(report['samples'])>=120 else
                          'sample_window_finished' if time.monotonic()-begin>=seconds else
                          'decoder_finished_or_safety_abort')
        report['next_edit_evidence']=rank_findings(report)
    if benchmarks:
        if report['decoder'].get('decoder_stopped') and not blockers(processor,camera):
            report['embedding_benchmark']=benchmark_runner(benchmark_payload,allowed=lambda:not blockers(processor,camera))
        else:report['embedding_benchmark']={'state':'not_run_decoder_or_safety'}
    return report


def run_preview(processor,camera,url,region,preview,reader_factory=DecodeReader,*,seconds=12,sample_interval=.5):
    from .preview_startup import inspect_startup
    return inspect_startup(processor,camera,url,region,preview,reader_factory,blockers,
                           seconds=seconds,sample_interval=sample_interval)


def rank_findings(report):
    stages=report.get('stage_totals_ms',{})
    ranked=sorted(((k,v) for k,v in stages.items() if k!='frame_hash'),key=lambda x:x[1],reverse=True)
    findings=[{'stage':k,'measured_total_ms':v} for k,v in ranked[:3]]
    comparisons=[s for s in report.get('samples',[]) if 'comparison' in s]
    crop_summary={'samples_compared':len(comparisons),
        'full_single_face':sum(s['comparison']['full_faces']==1 for s in comparisons),
        'crop_single_face':sum(s['comparison']['crop_faces']==1 for s in comparisons),
        'crop_only_single_face':sum(s['comparison']['full_faces']==0 and s['comparison']['crop_faces']==1 for s in comparisons),
        'crop_only_usable_live_identity':sum(s['comparison']['full_faces']==0 and s.get('result')=='diagnostic_usable' for s in comparisons),
        'comparison_only_overhead_ms':round(sum(s['comparison']['comparison_only_overhead_ms'] for s in comparisons),2),
        'note':'Detection counts alone do not prove identity or liveness. Compare usable samples and stage timings.'}
    return {'largest_measured_processing_stages':findings,'crop_comparison':crop_summary,
            'rejections':report.get('reasons',{}),
            'production_change_authorized_by_test':False,
            'unresolved':['absolute_capture_age','event_to_person_association','live_Access1_and_relay_timing']}
