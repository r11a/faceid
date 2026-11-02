"""Event-associated local-network observations; no capture-time claim.

Only the existing EventProcessor confirmation and Access1 submission paths may
authorize access. This manager owns acquisition, association and expiry proofs.
"""
from collections import deque
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

from .decision import DecisionAccumulator
from .face_tracks import FaceTracker
from .quality import measure_face_quality

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

    def configured(self, camera):
        profiles = self.processor.camera_profiles
        return bool(profiles and profiles.get(camera).get('observation_source','snapshots') != 'snapshots')

    def note(self, session, stage, reason=None):
        with self.lock:
            item = {'stage':stage, 'elapsed_ms':round((time.monotonic()-session['started'])*1000,1)}
            if reason:item['reason']=reason
            session['timeline'].append(item)
            session['reason'] = reason or stage
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
            return {'active':[self.public(s) for s in self.sessions.values()], 'recent':list(self.completed)}

    def finish(self, session, reason):
        with self.lock:
            if self.sessions.get(session['eid']) is not session:return
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

    def cancel_all(self, reason='transport_disconnected'):
        with self.lock: sessions=list(self.sessions.values())
        for session in sessions:self.finish(session,reason)

    def set_online(self, online):
        self.online=bool(online)
        if not self.online:self.cancel_all('transport_disconnected')

    def event(self, after, kind, retained=False):
        if not self.online:return
        camera,eid=after.get('camera'),after.get('id')
        if not isinstance(eid,str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,200}',eid):return
        with self.lock:session=self.sessions.get(eid)
        if session:
            if camera!=session['camera']:return
            if kind=='end' or after.get('end_time'):self.finish(session,'event_ended')
            return
        if retained or kind not in ('new','update') or after.get('end_time') or after.get('false_positive'):
            return
        if after.get('label')!='person' or eid in self.seen or eid in self.processor._handled:return
        if not self.processor.camera_enabled(camera):return
        profile,direct,revision=self.processor.camera_profiles.observation_config(camera)
        if profile.get('observation_source','snapshots')=='snapshots':return
        with self.lock:
            if eid in self.sessions or eid in self.seen:return
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
            self.sessions[eid]=session
            self.processor.events[eid]=st
        if not profile.get('trust_local_video_network'):
            self.finish(session,'local_network_trust_required');return
        try:
            if profile['observation_source']=='direct_rtsp':
                from .acquisition_diagnostics import validate_direct
                url=validate_direct(direct)
            else:url=self.processor.frigate._live_url(camera)
            reader=self.processor.frigate.live_frames.acquire_observation(camera,url) if url else None
        except Exception:reader=None
        if reader is None:self.finish(session,'reader_unavailable_capacity_or_source_conflict');return
        session['reader']=reader
        st['recognition_guard']=lambda:self.processing_valid(session)

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
        with self.lock:sessions=list(self.sessions.values())
        for session in sessions:
            reason=self.valid(session)
            if reason:self.finish(session,reason)
            elif session['st'].get('stream_deadline',float('inf'))<=time.monotonic() and session['st'].get('done'):
                self.finish(session,'evidence_expired')
        with self.lock:
            for camera in {s['camera'] for s in self.sessions.values()}:
                if camera in self.pending or time.monotonic()<self.next_start.get(camera,0):continue
                self.pending.add(camera);self.next_start[camera]=time.monotonic()+.25
                self.executor.submit(self._work,camera)

    def reset(self, session, reason):
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
        if session['anchor_attempts']>=2:self.finish(session,'association_anchor_unavailable');return False
        session['anchor_attempts']+=1
        image=self.processor.frigate.association_frame(session['eid'],session['camera'])
        if self.valid(session):return False
        if image is None:return False
        self.retain_preview(session,image,'anchor')
        found=self.processor.engine.detect_faces(image)
        found=[face for face in found if self._quality(image,face,session['profile']).usable]
        if len(found)!=1:
            self.note(session,'anchor_rejected','anchor_face_ambiguous_or_low_quality');return False
        self.processor.engine.embed_face(image,found[0])
        observation=FaceTracker._observation(found[0])
        if observation is None:return False
        session['anchor']=observation[1]
        self.note(session,'association_anchor_ready')
        return True

    def _work(self, camera):
        try:self._step(camera)
        except Exception:
            with self.lock:sessions=[s for s in self.sessions.values() if s['camera']==camera]
            for s in sessions:self.finish(s,'observation_processing_failed')
        finally:
            with self.lock:self.pending.discard(camera)

    def _step(self, camera):
        with self.lock:sessions=[s for s in self.sessions.values() if s['camera']==camera]
        if not sessions or all(s['st'].get('done') for s in sessions):return
        reader=sessions[0]['reader']
        if reader is None:return
        for s in sessions:
            if self.valid(s):continue
            if s['generation']!=reader.generation:
                self.reset(s,'stream_generation_changed');s['generation']=reader.generation
                s['sequence']=None;self.trackers.pop(camera,None)
            self._anchor(s)
        sessions=[s for s in sessions if self.valid(s) is None and s['anchor'] is not None]
        if not sessions:return
        if hasattr(self.processor.engine,'detect_frame_record'):
            record,found=self.processor.engine.detect_frame_record(reader)
        else:
            record=reader.frame_record();found=None
        if record is None:
            for s in sessions:self.reset(s,'stream_not_ready')
            return
        if not math.isfinite(record['pts_ms']) or record['pts_ms']<=0:return
        if not 0<=time.monotonic()-record['arrived']<=1:return
        if record['generation'] != reader.generation:return
        for s in sessions:
            if s['generation']!=record['generation']:
                self.reset(s,'stream_generation_changed');s['generation']=record['generation']
                self.trackers.pop(camera,None)
        sessions=[s for s in sessions if s['sequence']!=record['sequence']]
        if not sessions:return
        for s in sessions:
            s['sequence']=record['sequence']
            if not any(x['stage']=='stream_ready' for x in s['timeline']):self.note(s,'stream_ready')
        image=record['image']
        for s in sessions:self.retain_preview(s,image,'live')
        digest=hashlib.sha256(image.tobytes()).hexdigest()
        fresh=[]
        for s in sessions:
            if digest in s['hashes']:self.reset(s,'duplicate_frame');continue
            s['hashes'].add(digest);fresh.append(s)
        sessions=fresh
        if not sessions:return
        if found is None:found=self.processor.engine.detect_faces(image)
        usable=[(f,self._quality(image,f,sessions[0]['profile'])) for f in found]
        usable=[(f,q) for f,q in usable if q.usable]
        if not usable or len(usable)>4:
            for s in sessions:self.reset(s,'no_usable_face' if not usable else 'overcrowded_frame')
            return
        for face,_ in usable:self.processor.engine.embed_face(image,face)
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
        scores=np.array([[float(np.dot(s['anchor'],FaceTracker._observation(f)[1]))
                          for _,f in tracks] for s in sessions])
        if not tracks:return
        best=[FaceTracker._unique_best(row) for row in scores]
        reverse=[FaceTracker._unique_best(col) for col in scores.T]
        for row,s in enumerate(sessions):
            col=best[row]
            if col is None or reverse[col]!=row:
                self.reset(s,'association_ambiguous');continue
            track,face=tracks[col]
            if track in ambiguous_tracks:
                self.reset(s,'overlap_or_crossing');continue
            if s['st'].get('done'):continue
            if s['track'] is not None and s['track']!=track:self.reset(s,'face_track_changed')
            if s['track'] is None:self.note(s,'association_established')
            s['track']=track
            self.retain_preview(s,image,'associated',face.bbox)
            st=s['st']
            if st.get('stream_deadline',float('inf'))<=time.monotonic():self.reset(s,'evidence_expired');s['track']=track
            if self.valid(s) or reader.generation!=record['generation']:continue
            # Each associated candidate must also satisfy the existing configured ROI.
            if s['profile'].get('mode')=='intercom':
                l,t,r,b=s['profile'].get('roi',[0,0,1,1]);h,w=image.shape[:2]
                x,y=(face.bbox[:2]+face.bbox[2:])/2
                if not (l<=x/w<=r and t<=y/h<=b):self.reset(s,'outside_roi');continue
            with st['processing_lock']:
                if self.valid(s):continue
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

    def retain_preview(self, session, image, kind, bbox=None):
        # Last private input only, at most four events and three minutes. Never export.
        h,w=image.shape[:2];scale=min(1,960/max(h,w))
        picture=cv2.resize(image,(max(1,int(w*scale)),max(1,int(h*scale))))
        if bbox is not None:
            x1,y1,x2,y2=(np.asarray(bbox)*scale).astype(int)
            cv2.rectangle(picture,(x1,y1),(x2,y2),(0,220,0),2)
        ok,jpeg=cv2.imencode('.jpg',picture,[cv2.IMWRITE_JPEG_QUALITY,80])
        if not ok:return
        with self.lock:
            now=time.monotonic()
            self.previews={key:value for key,value in self.previews.items() if now-value['updated']<180}
            if session['eid'] not in self.previews and len(self.previews)>=4:
                self.previews.pop(next(iter(self.previews)))
            saved=self.previews.setdefault(session['eid'],{})
            saved.update(updated=now)
            saved[kind]=jpeg.tobytes()

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
