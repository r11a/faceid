"""Event-associated local-network observations; no capture-time claim.

Only the existing EventProcessor confirmation and Access1 submission paths may
authorize access. This manager owns acquisition, association and expiry proofs.
"""
from collections import deque
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor
import hashlib
import math
import logging
_LOGGER=logging.getLogger(__name__)
import re
import threading
import time
from uuid import uuid4

import numpy as np
import cv2

from .frigate_api import fetch_anchor, TRANSIENT_ANCHOR, ANCHOR_RETRY_SECONDS
from .decision import DecisionAccumulator
from .face_tracks import FaceTracker
from .quality import measure_face_quality
from .subject_association import single_anchor, usable_faces, mutual_assignments
from .recognition_capture import active as capture_active, emit as capture_emit, preview as capture_preview

SOURCE = 'bounded_local_stream'


class StreamObservations:
    def __init__(self, processor):
        self.processor = processor
        self.lock = threading.RLock()
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='faceid-observe')
        self.sessions = {}
        self.online = True
        self.completed = deque(maxlen=64)
        self.seen = deque(maxlen=1000)
        self.pending = set()
        self.next_start = {}
        self.trackers = {}
        self.previews = {}
        self.anchor_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='faceid-anchor')
        self.anchor_pending = {}
        self.preview_viewer = None
        self.preview_until = 0
        self.preview_frames = {}
        self.last_images = {}
        self.counters = {'frames_admitted':0, 'duplicate_sequences_skipped':0,
                         'duplicate_images_skipped':0, 'preview_encodes':0,
                         'anchor_capacity_waits':0}


    def configured(self, camera):
        profiles = self.processor.camera_profiles
        return bool(profiles and profiles.get(camera).get('observation_source','snapshots') != 'snapshots')

    def note(self, session, stage, reason=None):
        with self.lock:
            item = {'stage':stage, 'elapsed_ms':round((time.monotonic()-session['started'])*1000,1)}
            if reason:item['reason']=reason
            session['timeline'].append(item)
            session['reason'] = reason or stage
            capture_emit(self.processor,session['eid'],session['camera'],'stream_'+stage,
                         reason=reason or stage,source=session['profile']['observation_source'],
                         elapsed_ms=item['elapsed_ms'])
            if stage in ('trigger_received','association_anchor_ready','recognition_complete','finished'):
                _LOGGER.info('Stream observation event=%s stage=%s reason=%s elapsed_ms=%s',session['eid'],stage,reason or 'none',item['elapsed_ms'])

    def public(self, session):
        return {'event_id':session['eid'], 'camera':session['camera'], 'source':session['profile']['observation_source'],
                'trust_policy':'explicit_local_network', 'capture_freshness_proven':False,
                'state':session['state'], 'reason':session['reason'], 'timeline':list(session['timeline']),
                'samples':session['st'].get('stream_samples',0),
                'reader':session['reader'].report() if hasattr(session.get('reader'),'report') else None,
                'required_samples':session['st'].get('stream_required',3)}

    def report(self):
        with self.lock:
            return {'active':[self.public(s) for s in self.sessions.values()], 'recent':list(self.completed), 'resources':dict(self.counters),
                    'anchor_fetches_in_flight':sum(not f.done() for f in self.anchor_pending.values())}

    def finish(self, session, reason):
        with self.lock:
            if self.sessions.get(session['eid']) is not session:return
            # A single terminal input remains available even without a viewer.
            image = self.last_images.get(session['camera'])
            if image is not None and not session['st'].get('done'):
                self.retain_preview(session,image,'live',force=True)
            session.pop('early_candidate', None)
            future = session.get('anchor_future')
            if future is not None:future.cancel()
            self.note(session,'finished',reason)
            session['state']='finished'
            session['st']['ended']=True
            session['st']['end_time']=session['st'].get('end_time') or time.time()
            self.completed.append(self.public(session))
            self.sessions.pop(session['eid'],None)
            camera=session['camera']
            if not any(s['camera']==camera for s in self.sessions.values()):
                self.processor.frigate.live_frames.release_observation(camera)
                self.trackers.pop(camera,None)
                self.next_start.pop(camera,None)
                self.last_images.pop(camera,None)
                self.preview_frames.pop(camera,None)

    def cancel_all(self, reason='transport_disconnected'):
        with self.lock: sessions=list(self.sessions.values())
        for session in sessions:self.finish(session,reason)

    def set_online(self, online):
        self.online=bool(online)
        if not self.online:self.cancel_all('transport_disconnected')

    def event(self, after, kind, retained=False):
        camera,eid=after.get('camera'),after.get('id')
        if not self.online:
            capture_emit(self.processor,eid,camera,'stream_route',result='skipped',reason='transport_disconnected');return
        if not isinstance(eid,str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,200}',eid):
            capture_emit(self.processor,None,camera,'stream_route',result='skipped',reason='invalid_event_id');return
        with self.lock:session=self.sessions.get(eid)
        if session:
            if camera!=session['camera']:
                capture_emit(self.processor,eid,camera,'stream_route',result='skipped',reason='event_camera_changed');return
            if kind=='end' or after.get('end_time'):self.finish(session,'event_ended')
            else:capture_emit(self.processor,eid,camera,'stream_route',result='existing',reason='session_already_active')
            return
        if retained or kind not in ('new','update') or after.get('end_time') or after.get('false_positive'):
            reason=('retained_event' if retained else 'event_ended' if kind=='end' or after.get('end_time')
                    else 'false_positive' if after.get('false_positive') else 'unsupported_event_type')
            capture_emit(self.processor,eid,camera,'stream_route',result='skipped',reason=reason)
            return
        if after.get('label')!='person' or eid in self.seen or eid in self.processor._handled:
            capture_emit(self.processor,eid,camera,'stream_route',result='skipped',
                         reason='not_person_event' if after.get('label')!='person' else 'already_handled');return
        if not self.processor.camera_enabled(camera):
            capture_emit(self.processor,eid,camera,'stream_route',result='skipped',reason='camera_disabled');return
        profile,direct,revision=self.processor.camera_profiles.observation_config(camera)
        if profile.get('observation_source','snapshots')=='snapshots':
            capture_emit(self.processor,eid,camera,'stream_route',result='skipped',reason='snapshot_source_configured',source='snapshots');return
        with self.lock:
            if eid in self.sessions or eid in self.seen:
                capture_emit(self.processor,eid,camera,'stream_route',result='skipped',reason='already_handled');return
            self.seen.append(eid)
            self.processor._handled.append(eid)
            start=time.monotonic()
            st=self.processor._new_event_state(eid,camera,start_time=after.get('start_time'),
                stream_observation=True, clip_analyzed=True, mqtt_received_monotonic=time.perf_counter())
            st['stream_required']=max(3,self.processor.min_confirmations,
                                      getattr(getattr(self.processor,'liveness',None),'required_frames',3))
            session={'eid':eid,'camera':camera,'profile':profile,'revision':revision,'started':start,
                     'wall_started':time.time(),'deadline':start+30,'state':'observing','reason':'trigger_received',
                     'timeline':deque(maxlen=128),'st':st,'reader':None,'generation':None,'sequence':None,
                     'anchor':None,'anchor_attempts':0,'track':None,'hashes':set(),'token':uuid4().hex}
            self.note(session,'trigger_received')
            if sum(s['camera']==camera for s in self.sessions.values())>=4:
                session['state']='finished';self.note(session,'finished','session_capacity');self.completed.append(self.public(session));return
            neighbours = [s for s in self.sessions.values() if s['camera'] == camera]
            session['early_allowed'] = not neighbours
            for neighbour in neighbours:
                neighbour['early_allowed'] = False
                neighbour.pop('early_candidate', None)
            self.sessions[eid]=session
            self.processor.events[eid]=st
            capture_emit(self.processor,eid,camera,'stream_route',result='accepted',source=profile['observation_source'])
        if not profile.get('trust_local_video_network'):
            self.finish(session,'local_network_trust_required');return
        try:
            if profile['observation_source']=='direct_rtsp':
                from .acquisition_diagnostics import validate_direct
                url=validate_direct(direct)
            else:url=self.processor.frigate._live_url(camera)
            with self.lock:
                if self.valid(session):return
                reader=self.processor.frigate.live_frames.acquire_observation(camera,url) if url else None
                if self.valid(session):
                    if not any(s['camera']==camera for s in self.sessions.values()):
                        self.processor.frigate.live_frames.release_observation(camera)
                    return
                session['reader']=reader
                st['recognition_guard']=lambda:self.processing_valid(session)
        except Exception:reader=None
        if reader is None:self.finish(session,'reader_unavailable_capacity_or_source_conflict');return

    def valid(self, session):
        with self.lock:
            if self.sessions.get(session['eid']) is not session:return 'session_inactive'
        now=time.monotonic()
        if now>=session['deadline']:return 'observation_expired'
        if now>=session['st'].get('stream_deadline',float('inf')):return 'evidence_expired'
        if abs((time.time()-session['wall_started'])-(now-session['started']))>2:return 'clock_changed'
        if session['st'].get('ended') or session['st'].get('end_time'):return 'event_ended'
        if not self.processor.camera_enabled(session['camera']):return 'camera_disabled'
        profile,_,revision=self.processor.camera_profiles.observation_config(session['camera'])
        if revision!=session['revision']:return 'camera_configuration_changed'
        if not profile.get('trust_local_video_network'):return 'local_network_trust_required'
        return None

    def processing_valid(self, session):
        if self.valid(session) is not None:return False
        reader=session['reader'];proof=session['st'].get('stream_proof')
        if proof and (reader is None or reader.generation!=proof.get('generation')):return False
        return reader is not None and reader.frame_record() is not None

    def tick(self):
        with self.lock:
            sessions=list(self.sessions.values())
            now=time.monotonic()
            self.previews={key:value for key,value in self.previews.items() if now-value['updated']<180}
        for session in sessions:
            reason=self.valid(session)
            if reason:self.finish(session,reason)
            elif session['st'].get('stream_deadline',float('inf'))<=time.monotonic() and session['st'].get('done'):
                self.finish(session,'evidence_expired')
        with self.lock:
            # Completed/cancelled HTTP futures still occupy capacity until they retire.
            active_tokens={s['token'] for s in self.sessions.values()}
            self.anchor_pending={key:f for key,f in self.anchor_pending.items()
                                 if key in active_tokens or not f.done()}
            for camera in sorted({s['camera'] for s in self.sessions.values()},
                                 key=lambda c:self.next_start.get(c,0)):
                if camera in self.pending or time.monotonic()<self.next_start.get(camera,0):continue
                self.pending.add(camera);self.next_start[camera]=time.monotonic()+.25
                self.executor.submit(self._work,camera)

    def reset(self, session, reason):
        session.pop('early_candidate', None)
        st=session['st']
        st['liveness_history']=[];st['liveness']=None;st['liveness_subject']=None
        st['liveness_keys']=set();st['decision']=DecisionAccumulator(self.processor.policy)
        st['stream_samples']=0;st.pop('stream_deadline',None);st.pop('stream_proof',None)
        session['track']=None
        if session['reason']!=reason:self.note(session,'evidence_reset',reason)

    def _quality(self, image, face, profile):
        hour=time.localtime().tm_hour
        minimum=profile.get('night_min_face_px' if hour>=19 or hour<6 else 'min_face_px',self.processor.min_face_px)
        return measure_face_quality(image,face,min_face_px=minimum,min_quality=self.processor.min_face_quality)

    def _anchor(self, session):
        if session['anchor'] is not None:return True
        future=session.get('anchor_future')
        if future is None:
            if session['anchor_attempts']>=2:
                self.finish(session,'association_anchor_unavailable');return False
            if time.monotonic() < session.get('anchor_retry_at', 0):return False
            with self.lock:
                if self.valid(session):return False
                # No unbounded executor backlog; cancelled fetches count while retiring.
                if sum(not f.done() for f in self.anchor_pending.values())>=2:
                    self.counters['anchor_capacity_waits']+=1
                    capture_emit(self.processor,session['eid'],session['camera'],'association_anchor_fetch',
                                 result='waiting',reason='anchor_fetch_capacity')
                    return False
                session['anchor_attempts']+=1
                session['anchor_fetch_started']=time.perf_counter()
                capture_emit(self.processor,session['eid'],session['camera'],'association_anchor_fetch',
                             attempt=session['anchor_attempts'],result='started')
                future = self.anchor_executor.submit(self._prepare_anchor, session)
                session['anchor_future']=future
                self.anchor_pending[session['token']]=future
        if not future.done():return False
        session.pop('anchor_future',None)
        with self.lock:self.anchor_pending.pop(session['token'],None)
        if self.valid(session):return False
        try:
            anchor, reason, crop_received = future.result()
        except Exception:
            self.finish(session, 'anchor_processing_failed')
            return False
        if not crop_received:
            if reason in TRANSIENT_ANCHOR and session['anchor_attempts'] < 2:
                session['anchor_retry_at'] = time.monotonic() + ANCHOR_RETRY_SECONDS
                self.note(session,'anchor_retry_scheduled',reason)
            else:
                self.finish(session,reason)
            return False
        if self.valid(session):return False
        if anchor is None:
            self.note(session,'anchor_rejected',reason);return False
        session['anchor']=anchor
        self.note(session,'association_anchor_ready')
        return True

    def _prepare_anchor(self, session):
        eid, camera = session['eid'], session['camera']
        try:
            image, reason = fetch_anchor(self.processor.frigate, eid, camera)
        except Exception:
            image, reason = None, 'anchor_fetch_failed'
        if self.valid(session):
            return None, 'session_inactive', False
        capture_emit(self.processor, eid, camera, 'association_anchor_fetch',
                     attempt=session['anchor_attempts'], result='received' if image is not None else 'unavailable',
                     reason=reason, anchor_fetch_wait_ms=(time.perf_counter()-session['anchor_fetch_started'])*1000)
        if image is None:
            return None, reason, False
        self.retain_preview(session, image, 'anchor')
        capture_preview(self.processor, eid, camera, image, 'anchor')
        diagnostics = {} if capture_active(self.processor, camera) else None
        started = time.perf_counter()
        anchor, reason = single_anchor(self.processor.engine, image, session['profile'],
            min_face_px=self.processor.min_face_px, min_quality=self.processor.min_face_quality,
            diagnostics=diagnostics)
        capture_emit(self.processor, eid, camera, 'association_anchor_model',
                     anchor_model_ms=(time.perf_counter()-started)*1000, **(diagnostics or {}))
        return anchor, reason, True

    def _work(self, camera):
        try:self._step(camera)
        except Exception:
            with self.lock:sessions=[s for s in self.sessions.values() if s['camera']==camera]
            for s in sessions:self.finish(s,'observation_processing_failed')
        finally:
            with self.lock:self.pending.discard(camera)

    def _capture_frame(self,sessions,record,reader):
        # Optional metadata must never interfere with model scheduling or admission.
        try:
            if not capture_active(self.processor,sessions[0]['camera']):return
            if record is None:
                report=reader.report() if hasattr(reader,'report') else {}
                for s in sessions:
                    capture_emit(self.processor,s['eid'],s['camera'],'stream_frame',result='unavailable',
                                 reason=report.get('rejection_reason') or report.get('state') or 'stream_not_ready',
                                 generation=report.get('generation'))
                return
            fields={key:record.get(key) for key in ('sequence','generation','pts_ms',
                    'processing_age_ms','detection_scheduler_wait_ms','detection_compute_ms')}
            fields['arrival_age_ms']=fields.pop('processing_age_ms')
            fields['result']='rejected' if record.get('processing_rejected') else 'available'
            fields['reason']=record.get('processing_rejected')
            offset=getattr(reader,'clock_offset',None)
            position=getattr(reader,'last_position',None)
            arrived=getattr(reader,'position_arrived',None)
            if all(type(v) in (int,float) and math.isfinite(v) for v in (offset,position,arrived)):
                fields['buffering_growth_ms']=max(0,arrived-position-offset)*1000
            for s in sessions:capture_emit(self.processor,s['eid'],s['camera'],'stream_frame',**fields)
        except Exception:
            pass

    def _step(self, camera):
        # Reserve priority for this bounded frame burst, never a whole session.
        live_work=getattr(self.processor.engine,'live_work',None)
        with live_work() if live_work else nullcontext():
            self._step_model(camera)

    def _step_model(self, camera):
        with self.lock:sessions=[s for s in self.sessions.values() if s['camera']==camera]
        if not sessions or all(s['st'].get('done') for s in sessions):return
        reader=sessions[0]['reader']
        if reader is None:return
        for s in sessions:
            if self.valid(s):continue
            if s['generation']!=reader.generation:
                reconnect=s['generation'] is not None
                self.reset(s,'stream_generation_changed');s['generation']=reader.generation
                s['sequence']=None
                if reconnect:self.trackers.pop(camera,None)
            if not s['st'].get('done'):self._anchor(s)
        sessions = [s for s in sessions if self.valid(s) is None]
        if not sessions:
            return
        # A solitary event may reuse its latest detected face exactly once.
        # A frame too old to start work on now is discarded even if its initial
        # detection began on time. All evidence clocks use original arrival.
        if len(sessions) == 1 and sessions[0]['anchor'] is not None:
            s = sessions[0]
            with self.lock:
                candidate = s.pop('early_candidate', None)
            if candidate is not None:
                record, usable = candidate
                if (s['early_allowed'] and self.valid(s) is None
                        and reader.generation == record['generation'] and reader.frame_record() is not None
                        and 0 <= time.monotonic() - record['arrived'] <= 1):
                    self._associate(camera, sessions, sessions, reader, record, usable, early=True)
                    return
        sessions = [s for s in sessions if s['anchor'] is not None or s['early_allowed']]
        if not sessions:
            return
        def admit(record):
            active=[s for s in sessions if not s['st'].get('done') and self.valid(s) is None]
            fresh=[s for s in active if s['sequence']!=record['sequence']]
            if not fresh:
                self.counters['duplicate_sequences_skipped']+=1
                return False
            image=record['image']
            digest=hashlib.sha256(np.ascontiguousarray(image)).hexdigest()
            record['fingerprint']=digest
            admitted=False
            for s in fresh:
                if digest in s['hashes']:
                    s['sequence']=record['sequence']
                    self.reset(s,'duplicate_frame')
                    self.counters['duplicate_images_skipped']+=1
                else:admitted=True
            return admitted

        if hasattr(self.processor.engine,'detect_frame_record'):
            record,found=self.processor.engine.detect_frame_record(reader,admit=admit)
        else:
            record=reader.frame_record();found=None
            if record is not None:
                age=time.monotonic()-record['arrived']
                if not 0<=age<=1:return
                record={**record,'processing_started_monotonic':time.monotonic(),
                        'processing_age_ms':age*1000}
                if not admit(record):return
        if record is None:
            self._capture_frame(sessions,None,reader)
            for s in sessions:
                if not s['st'].get('done'):self.reset(s,'stream_not_ready')
            return
        self._capture_frame(sessions,record,reader)
        if record.get('processing_rejected'):return
        if not math.isfinite(record['pts_ms']) or record['pts_ms']<=0:
            for s in sessions:capture_emit(self.processor,s['eid'],camera,'stream_frame_rejected',reason='invalid_pts')
            return
        if not 0<=record.get('processing_age_ms',float('inf'))<=1000:
            for s in sessions:capture_emit(self.processor,s['eid'],camera,'stream_frame_rejected',reason='processing_start_stale')
            return
        if record['generation'] != reader.generation:
            for s in sessions:capture_emit(self.processor,s['eid'],camera,'stream_frame_rejected',reason='stream_generation_changed')
            return
        for s in sessions:
            if s['generation']!=record['generation']:
                self.reset(s,'stream_generation_changed');s['generation']=record['generation']
                self.trackers.pop(camera,None)
        sessions=[s for s in sessions if self.valid(s) is None]
        fresh=[s for s in sessions if not s['st'].get('done') and s['sequence']!=record['sequence']]
        if not fresh:return
        digest=record['fingerprint']
        for s in fresh:
            s['sequence']=record['sequence'];s['hashes'].add(digest)
            if not any(x['stage']=='stream_ready' for x in s['timeline']):self.note(s,'stream_ready')
        image=record['image']
        self.counters['frames_admitted']+=1
        with self.lock:
            if not any(self.valid(s) is None for s in fresh):return
            self.last_images[camera]=image
        for s in fresh:self.retain_preview(s,image,'live')
        if found is None:found=self.processor.engine.detect_faces(image)
        # Reject irrelevant faces before the expensive recognition embedding.
        diagnostics={} if capture_active(self.processor,camera) else None
        usable=usable_faces(image,found,sessions[0]['profile'],
            min_face_px=self.processor.min_face_px,min_quality=self.processor.min_face_quality,diagnostics=diagnostics)
        for s in fresh:
            capture_emit(self.processor,s['eid'],camera,'association_candidates',**(diagnostics or {}))
        if not usable or len(usable)>4:
            for s in fresh:capture_preview(self.processor,s['eid'],camera,image,'input')
            for s in fresh:self.reset(s,'no_usable_face' if not usable else 'overcrowded_frame')
            return
        if len(sessions) == 1 and sessions[0]['anchor'] is None:
            s = sessions[0]
            with self.lock:
                s.pop('early_candidate', None)
                if (s['early_allowed'] and self.valid(s) is None and reader.generation == record['generation']
                        and record['arrived'] >= s['started'] and len(found) == 1 and len(usable) == 1):
                    s['early_candidate'] = (record, usable)
                    if not s.get('early_face_seen'):
                        s['early_face_seen'] = True
                        self.note(s, 'early_face_detected')
            return
        self._associate(camera, sessions, fresh, reader, record, usable)

    def _associate(self, camera, sessions, fresh, reader, record, usable, *, early=False):
        image = record['image']
        for face, _ in usable:
            self.processor.engine.embed_face(image, face)
        with self.lock:
            sessions=[s for s in sessions if self.valid(s) is None and s['anchor'] is not None]
            if early and (not sessions or not sessions[0]['early_allowed']):
                return
            if reader.generation != record['generation']:
                return
            if not any(not s['st'].get('done') for s in sessions):return
            tracker=self.trackers.setdefault(camera,FaceTracker(max_tracks=16))
            previous={key:track.bbox.copy() for key,track in tracker._tracks.items()}
            tracks,_=tracker.update([f for f,_ in usable],record['arrived'])
        ambiguous_tracks=set()
        for i,(key,face) in enumerate(tracks):
            for other_key,other in tracks[i+1:]:
                a,b=np.asarray(face.bbox),np.asarray(other.bbox)
                overlap=bool(np.all(np.minimum(a[2:],b[2:])>np.maximum(a[:2],b[:2])))
                crossed=False
                if key in previous and other_key in previous:
                    old=previous[key][:2]+previous[key][2:]-previous[other_key][:2]-previous[other_key][2:]
                    new=a[:2]+a[2:]-b[:2]-b[2:]
                    crossed=float(np.dot(old,new))<0
                if overlap or crossed:ambiguous_tracks.update((key,other_key))
        qualities={id(f):q for f,q in usable}
        # Mutual unique matching prevents one face being assigned to two events.
        if not tracks:return
        diagnostics={} if capture_active(self.processor,camera) else None
        best=mutual_assignments([s['anchor'] for s in sessions],[f for _,f in tracks],diagnostics=diagnostics)
        fresh_tokens={s['token'] for s in fresh}
        for row,s in enumerate(sessions):
            if s['token'] not in fresh_tokens or (early and not s['early_allowed']):continue
            if diagnostics:
                capture_emit(self.processor,s['eid'],camera,'association_match',
                             candidate_count=diagnostics['candidate_count'],anchor_count=diagnostics['anchor_count'],
                             **diagnostics['assignments'][row])
            col=best[row]
            if col is None:
                capture_preview(self.processor,s['eid'],camera,image,'input')
                self.reset(s,'association_ambiguous');continue
            track,face=tracks[col]
            if track in ambiguous_tracks:
                self.reset(s,'overlap_or_crossing');continue
            if s['st'].get('done'):continue
            if s['track'] is not None and s['track']!=track:self.reset(s,'face_track_changed')
            if s['track'] is None:self.note(s,'association_established')
            s['track']=track
            self.retain_preview(s,image,'associated',face.bbox)
            capture_preview(self.processor,s['eid'],camera,image,'subject',bbox=face.bbox)
            st=s['st']
            if st.get('stream_deadline',float('inf'))<=time.monotonic():self.reset(s,'evidence_expired');s['track']=track
            if self.valid(s) or reader.generation!=record['generation'] or (early and not s['early_allowed']):continue
            with st['processing_lock']:
                if self.valid(s):continue
                if early:
                    if not s['early_allowed'] or reader.generation != record['generation']:
                        continue
                    self.note(s, 'early_face_associated')
                st.setdefault('stream_deadline',record['arrived']+10)
                st['stream_proof']={'session':s['token'],'generation':record['generation'],'track':track}
                st['attempts']+=1;st['last_try']=time.time()
                self.processor._process_face(s['eid'],st,image,face,quality=qualities[id(face)],source=SOURCE)
                self.note(s,'qualifying_sample' if st.get('stream_samples',0) else 'sample_rejected',st.get('stream_reason'))
                if st.get('done'):
                    s['state']='recognized' if st.get('best_person') else 'finished'
                    self.note(s,'recognition_complete')

    def evidence(self, event):
        """Validate internal proof again at connector delivery; no wire extension."""
        with self.lock:s=self.sessions.get(event.get('event_id'))
        if s is None:return None,'session_inactive'
        reason=self.valid(s)
        if reason:return None,reason
        proof=event.get('stream_proof') or {}
        st=s['st'];reader=s['reader']
        if (proof!=st.get('stream_proof') or proof.get('session')!=s['token'] or
            reader is None or reader.generation!=proof.get('generation') or s['track']!=proof.get('track')):
            return None,'stream_association_changed'
        if reader.frame_record() is None:return None,'stream_not_ready'
        if st.get('stream_samples',0)<st['stream_required'] or not (st.get('liveness') or {}).get('confirmed'):
            return None,'liveness_unconfirmed'
        remaining=min(st.get('stream_deadline',0),s['deadline'])-time.monotonic()
        if remaining<=0:return None,'evidence_expired'
        return min(remaining,10),'ok'

    def request_preview(self,event_id):
        """Called only by the authenticated diagnostic route; a short viewer lease."""
        with self.lock:
            if event_id in self.sessions or event_id in self.previews:
                self.preview_viewer=event_id
                self.preview_until=time.monotonic()+10

    def retain_preview(self, session, image, kind, bbox=None, force=False):
        now=time.monotonic()
        eid=session['eid'];camera=session['camera']
        with self.lock:
            self.previews={key:value for key,value in self.previews.items() if now-value['updated']<180}
            if kind!='anchor' and not force:
                if now>=self.preview_until or self.preview_viewer!=eid:return
                saved=self.previews.get(eid,{})
                if now-saved.get(kind+'_encoded',-10)<1:return
            if kind=='anchor' and 'anchor' in self.previews.get(eid,{}):return
            cached=self.preview_frames.get(camera)
            jpeg=cached[1] if kind=='live' and cached and now-cached[0]<1 else None
        try:
            if jpeg is None:
                h,w=image.shape[:2];scale=min(1,960/max(h,w))
                picture=cv2.resize(image,(max(1,int(w*scale)),max(1,int(h*scale))))
                if bbox is not None:
                    x1,y1,x2,y2=(np.asarray(bbox)*scale).astype(int)
                    cv2.rectangle(picture,(x1,y1),(x2,y2),(0,220,0),2)
                ok,encoded=cv2.imencode('.jpg',picture,[cv2.IMWRITE_JPEG_QUALITY,80])
                if not ok:return
                jpeg=encoded.tobytes()
                self.counters['preview_encodes']+=1
        except Exception:
            return # Optional visualization must not alter evidence or access.
        with self.lock:
            if self.sessions.get(eid) is not session:return
            if eid not in self.previews and len(self.previews)>=4:self.previews.pop(next(iter(self.previews)))
            saved=self.previews.setdefault(eid,{})
            saved.update(updated=now)
            saved[kind]=jpeg;saved[kind+'_encoded']=now
            if kind=='live':self.preview_frames[camera]=(now,jpeg)

    def preview(self,event_id,kind):
        with self.lock:
            saved=self.previews.get(event_id)
            if not saved or time.monotonic()-saved['updated']>=180:return None
            return saved.get(kind) if kind in ('anchor','live','associated') else None

    def submitting(self,event):
        with self.lock:
            session=self.sessions.get(event.get('event_id'))
            if session:self.note(session,'access1_submission')

    def receipt(self, event, result):
        with self.lock:s=self.sessions.get(event.get('event_id'))
        if s:
            self.note(s,'access1_result',str(result.get('state','unknown')))
            if result.get('state')!='pending':self.finish(s,'access1_'+str(result.get('state','unknown')))
