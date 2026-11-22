import tempfile
import json
import time
import unittest
from unittest.mock import patch
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
from fastapi.testclient import TestClient

from app.audit import AuditStore
from app.gallery import Gallery
from app.webui import build_app


def vector(axis=0):
    v = np.zeros(512, dtype=np.float32)
    v[axis] = 1
    return v


class FaceSearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.audit = AuditStore(self.root / 'audit.db')
        self.gallery = Gallery(self.root)
        self.face = SimpleNamespace(normed_embedding=vector(), bbox=np.array([0, 0, 80, 80]), det_score=.99)
        self.engine = SimpleNamespace(faces=lambda image: [self.face])
        self.processor = SimpleNamespace(audit=self.audit, set_sub_label=False)
        self.client = TestClient(build_app({'faceid': {}, 'frigate': {'url': ''}}, self.engine,
            self.gallery, self.processor, self.root, self.root))
        self.search = self.processor.face_search

    def event(self, eid, camera, ts, emb=None, person=None):
        self.audit.start_event(eid, camera, ts)
        self.audit.finalize(eid, 'recognized' if person else 'unknown', person=person)
        if emb is not None:
            self.search.record(eid, emb, np.zeros((80,80,3), dtype=np.uint8))

    def test_unknown_similarity_is_chronological_paginated_and_not_identity(self):
        self.event('late', 'door', 300, vector())
        self.event('early', 'yard', 100, vector())
        self.event('other', 'yard', 200, vector(1))
        self.search.record('late', vector(), None)
        result = self.search.timeline([vector()], threshold=.6, limit=1)
        self.assertEqual(result['total'], 2)
        self.assertEqual(result['events'][0]['event_id'], 'early')
        self.assertEqual(self.search.timeline([vector()], offset=1)['events'][0]['event_id'], 'late')
        self.assertEqual(self.search.timeline([vector()], camera='door')['total'], 1)
        self.assertEqual(self.search.timeline([vector()], date_from=200)['total'], 1)
        self.assertEqual(self.audit.event_detail('early')['event']['status'], 'unknown')

    def test_home_recent_includes_unknowns_and_more_than_eight_events(self):
        for i in range(12):
            self.event(f'home-{i}', 'door', 100 + i, person='Alice' if i % 2 else None)
        rows = self.client.get('/api/dashboard').json()['recent']
        self.assertEqual(len(rows), 12)
        self.assertIn('unknown', {row['status'] for row in rows})

    def test_last_location_respects_reviewed_identity(self):
        self.gallery.create_person('Alice')
        self.gallery.create_person('Bob')
        self.event('old', 'yard', 100, person='Alice')
        self.event('wrong', 'door', 200, person='Alice')
        self.audit.set_ground_truth('wrong', 'Bob', 'operator')
        cards = {row['name']: row for row in self.client.get('/api/dashboard').json()['people']}
        self.assertEqual(cards['Alice']['last_camera'], 'yard')
        self.assertEqual(cards['Bob']['last_camera'], 'door')
        self.audit.set_ground_truth('wrong', '__unknown__', 'operator')
        cards = {row['name']: row for row in self.client.get('/api/dashboard').json()['people']}
        self.assertEqual(cards['Alice']['last_camera'], 'yard')
        self.assertIsNone(cards['Bob']['last_camera'])

    def test_home_selects_latest_occurrences_not_recent_reviews(self):
        self.event('old', 'door', 1, person='Alice')
        for i in range(60):
            self.event(f'new-{i}', 'yard', 100 + i, person='Alice')
        self.audit.set_ground_truth('old', 'Alice', 'operator')
        rows = self.client.get('/api/dashboard').json()['recent']
        self.assertEqual(len(rows), 60)
        self.assertNotIn('old', {row['event_id'] for row in rows})

    def test_known_timeline_includes_old_events_without_vectors_but_respects_review(self):
        slug = self.gallery.create_person('רונן')
        self.event('a', 'yard', 100, person='רונן')
        self.event('b', 'door', 200, person='רונן')
        self.audit.set_ground_truth('b', 'Someone else', 'operator')
        response = self.client.get('/api/faces/timeline', params={'person': slug})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([e['event_id'] for e in response.json()['events']], ['a'])

    def test_identity_history_does_not_promote_a_guess(self):
        slug=self.gallery.create_person('Alice')
        self.event('guess','door',100)
        self.audit.finalize('guess','unknown',person='Alice',score=.2)
        result=self.client.get('/api/faces/timeline',params={'person':slug}).json()
        self.assertEqual(result['total'],0)

    def test_known_person_can_explicitly_search_similar_unknown_events(self):
        slug=self.gallery.create_person('Alice')
        self.gallery.add_face(slug,np.zeros((80,80,3),dtype=np.uint8),vector())
        self.event('unknown','yard',time.time(),vector())
        self.assertEqual(self.client.get('/api/faces/timeline',params={'person':slug}).json()['total'],0)
        result=self.client.get('/api/faces/timeline',params={'person':slug,'similar':True}).json()
        self.assertEqual(result['mode'],'similarity')
        self.assertEqual(result['total'],1)

    def test_enrollment_uses_exact_selected_face_without_rerunning_detection(self):
        slug=self.gallery.create_person('Alice')
        other=SimpleNamespace(normed_embedding=vector(1),bbox=np.array([100,0,180,80]),det_score=.99)
        self.engine.faces=lambda image:[self.face,other]
        image=np.random.default_rng(2).integers(0,255,(200,200,3),dtype=np.uint8)
        _,raw=cv2.imencode('.jpg',image)
        found=self.client.post('/api/faces/search?face_index=1',files={'file':('group.jpg',raw.tobytes(),'image/jpeg')}).json()
        self.engine.faces=lambda image: (_ for _ in ()).throw(AssertionError('must not detect again'))
        result=self.client.post('/api/faces/enroll',json={'token':found['token'],'person':slug})
        self.assertEqual(result.status_code,200)
        self.assertTrue(np.allclose(self.gallery.embeddings(slug)[0],vector(1)))

    def test_privacy_prune_applies_to_new_face_crops(self):
        self.event('old','yard',time.time()-3*86400,vector())
        with self.audit._connection() as con:
            con.execute('UPDATE events SET updated_ts=? WHERE event_id=?',(time.time()-3*86400,'old'))
        self.audit.prune_evidence(1,1)
        self.assertEqual(list(self.search.image_dir.glob('*.jpg')),[])
        self.assertEqual(self.search.timeline([vector()])['total'],1)

    def test_zero_image_cap_keeps_no_search_crops(self):
        self.event('fresh','yard',time.time(),vector())
        self.audit.prune_evidence(30,14,0,0)
        self.assertEqual(list(self.search.image_dir.glob('*.jpg')),[])

    def test_upload_search_has_no_enrollment_side_effect_and_rejects_ambiguous_source(self):
        self.event('match', 'door', 100, vector())
        ok, image = cv2.imencode('.jpg', np.zeros((100,100,3), dtype=np.uint8))
        response = self.client.post('/api/faces/search', files={'file': ('face.jpg', image.tobytes(), 'image/jpeg')})
        self.assertEqual(response.status_code, 200)
        token = response.json()['token']
        result = self.client.get('/api/faces/timeline', params={'token': token}).json()
        self.assertEqual(result['total'], 1)
        self.assertEqual(self.gallery.persons(), {})
        self.assertEqual(self.client.get('/api/faces/timeline', params={'token': token, 'event_id':'match'}).status_code, 400)

    def test_multi_face_upload_requires_selection(self):
        self.engine.faces = lambda image: [self.face, self.face]
        _, image = cv2.imencode('.jpg', np.zeros((100,100,3), dtype=np.uint8))
        response = self.client.post('/api/faces/search', files={'file':('group.jpg',image.tobytes(),'image/jpeg')})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'needs_selection')
        self.assertNotIn('token', response.json())

    def test_existing_gallery_upload_still_works(self):
        slug=self.gallery.create_person('Alice')
        self.processor.min_face_quality=.35
        _, image=cv2.imencode('.jpg',np.random.default_rng(2).integers(0,255,(100,100,3),dtype=np.uint8))
        result=self.client.post(f'/api/persons/{slug}/photos',files={'files':('face.jpg',image.tobytes(),'image/jpeg')})
        self.assertEqual(result.status_code,200)
        self.assertEqual(result.json()['added'],1)

    def test_invalid_vector_and_expired_image_do_not_corrupt_search(self):
        self.event('a','door',100,vector())
        with self.assertRaises(ValueError):
            self.search.record('a', [float('nan')]*512, None)
        with self.assertRaises(ValueError):
            self.search.record('a', [1,0], None)
        self.assertEqual(self.search.timeline([vector()])['total'], 1)

    def test_purge_history_erases_face_vectors_and_crops(self):
        self.event('private','door',time.time(),vector(),person='Alice')
        self.assertTrue(list(self.search.image_dir.glob('*.jpg')))
        self.audit.delete_person_history('Alice')
        self.assertEqual(self.search.timeline([vector()])['total'], 0)
        self.assertEqual(list(self.search.image_dir.glob('*.jpg')), [])

    def test_event_selection_recovers_only_selected_retained_evidence(self):
        for eid in ('selected', 'unrelated'):
            self.event(eid, 'yard', time.time())
            self.audit.save_evidence(eid, np.zeros((100,100,3), dtype=np.uint8))
        response = self.client.get('/api/faces/event/selected')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()['samples']), 1)
        self.assertEqual(self.search.sample_vectors(event_id='unrelated'), [])
        result = self.client.get('/api/faces/timeline', params={'event_id':'selected'})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['total'], 1)
        self.assertEqual(self.audit.event_detail('selected')['event']['status'], 'unknown')

    def test_event_timeline_recovers_unknown_embedding_without_redetection(self):
        self.event('selected', 'yard', time.time())
        self.gallery.save_unknown(np.zeros((80,80,3), dtype=np.uint8), vector(),
                                  {'event_id':'selected', 'camera':'yard'})
        self.engine.faces = lambda image: (_ for _ in ()).throw(AssertionError('must reuse vector'))
        result = self.client.get('/api/faces/timeline', params={'event_id':'selected'})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['total'], 1)

    def test_unknown_search_includes_its_unindexed_source_event(self):
        self.event('selected', 'yard', time.time())
        uid = self.gallery.save_unknown(np.zeros((80,80,3), dtype=np.uint8), vector(),
                                        {'event_id':'selected', 'camera':'yard'})
        result = self.client.get('/api/faces/timeline', params={'unknown_id':uid})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['total'], 1)
        self.assertEqual(result.json()['mode'], 'similarity')

    def test_history_retry_recovers_fingerprinted_event_without_samples(self):
        self.event('old', 'yard', time.time())
        path = self.audit.save_evidence('old', np.zeros((100,100,3), dtype=np.uint8))
        fingerprint = f'{path.stat().st_mtime_ns}:{path.stat().st_size}'
        with self.audit._connection() as con:
            con.execute('INSERT INTO face_indexed VALUES(?,?)', ('old', fingerprint))
        self.search.index_existing()
        self.assertEqual(self.search.timeline([vector()])['total'], 1)

    def test_unknown_selection_and_background_index_recover_retained_history(self):
        self.event('old','yard',100)
        self.audit.save_evidence('old', np.zeros((100,100,3),dtype=np.uint8))
        self.search.index_existing()
        self.assertEqual(self.search.timeline([vector()])['total'],1)
        uid=self.gallery.save_unknown(np.zeros((80,80,3),dtype=np.uint8),vector(),{'event_id':'old','camera':'yard'})
        result=self.client.get('/api/faces/timeline',params={'unknown_id':uid})
        self.assertEqual(result.status_code,200)
        self.assertEqual(result.json()['total'],1)

    def test_remote_scan_recovers_other_known_history_without_identity_fallback(self):
        for eid in ('source', 'match-one', 'match-two'):
            self.event(eid, 'yard', time.time(), person='Alice')
        self.search.record('source', vector())
        requested = []
        background = []
        @contextmanager
        def background_work():
            background.append(True)
            try:
                yield
            finally:
                background.pop()
        def snapshot(eid, crop):
            self.assertTrue(background)
            requested.append(eid)
            return np.zeros((100,100,3), dtype=np.uint8)
        self.engine.background_work = background_work
        self.search.frigate = SimpleNamespace(snapshot=snapshot)
        self.assertEqual(self.search.coverage()['events_missing'], 2)
        with patch('app.face_search.time.sleep'):
            self.search.index_existing(remote=True, limit=2)
        result = self.client.get('/api/faces/timeline', params={'event_id':'source'}).json()
        self.assertEqual(set(requested), {'match-one','match-two'})
        self.assertEqual({e['event_id'] for e in result['events']}, {'source','match-one','match-two'})
        self.assertEqual(result['source_event_id'], 'source')
        self.assertEqual(result['index']['events_indexed'], 3)
        self.assertEqual(result['index']['events_missing'], 0)
        self.assertEqual(result['mode'], 'similarity')

    def test_remote_scan_is_explicit_bounded_and_advances_past_expired_images(self):
        for i in range(5):
            self.event(f'old-{i}', 'yard', 100+i, person='Alice')
        requested = []
        self.search.frigate = SimpleNamespace(snapshot=lambda eid, crop: requested.append(eid))
        with patch('app.face_search.time.sleep'):
            self.search.index_existing()
            self.assertEqual(requested, [])
            self.search.index_existing(remote=True, limit=2)
            self.assertEqual(requested, ['old-4','old-3'])
            self.search.index_existing(remote=True, limit=2)
        self.assertEqual(requested, ['old-4','old-3','old-2','old-1'])
        self.assertEqual(self.search.coverage()['events_missing'], 5)
        self.assertEqual(self.search.index_status['unavailable'], 4)
        self.assertEqual(self.client.post('/api/faces/index?remote=true&limit=2001').status_code,400)

    def test_background_scan_recovers_entire_saved_unknown_corpus(self):
        for eid in ('source', 'match-one', 'match-two'):
            self.event(eid, 'yard', time.time())
            (self.gallery.unknown_dir / f'{eid}.json').write_text(
                json.dumps({'event_id':eid, 'embedding':vector().tolist()}), encoding='utf-8')
            cv2.imwrite(str(self.gallery.unknown_dir / f'{eid}.jpg'), np.zeros((80,80,3), dtype=np.uint8))
        self.engine.faces = lambda image: (_ for _ in ()).throw(AssertionError('reuse saved vectors'))
        self.search.index_existing()
        result = self.client.get('/api/faces/timeline', params={'event_id':'source'}).json()
        self.assertEqual(result['total'],3)
        self.assertEqual(result['index']['coverage_percent'],100)

    def test_remote_batch_bounds_local_evidence_and_skips_indexed_images(self):
        for i in range(5):
            eid = f'local-{i}'
            self.event(eid, 'yard', time.time()+i)
            self.audit.save_evidence(eid, np.zeros((100,100,3), dtype=np.uint8))
        self.search.record('local-4', vector())
        self.search.frigate = SimpleNamespace(snapshot=lambda *a, **kw: self.fail('local images available'))
        with patch('app.face_search.time.sleep') as sleep:
            self.search.index_existing(remote=True, limit=2)
        self.assertEqual(sleep.call_count,2)
        self.assertEqual(self.search.coverage()['events_indexed'],3)
        self.assertEqual(self.search.index_status['total'],2)
        self.assertEqual(self.search.sample_vectors(event_id='local-0'),[])

    def test_delete_history_cleans_scan_attempts(self):
        self.event('private-scan','yard',time.time(),person='Alice')
        self.search.frigate = SimpleNamespace(snapshot=lambda *a, **kw: None)
        with patch('app.face_search.time.sleep'):
            self.search.index_existing(remote=True,limit=1)
        self.audit.delete_person_history('Alice')
        with self.audit._connection() as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM face_scan_attempts').fetchone()[0],0)



    def test_local_scan_limit_advances_without_repeating_empty_images(self):
        calls = []
        self.engine.faces = lambda image: calls.append(True) or []
        for i in range(4):
            self.event(f'empty-{i}', 'yard', 100+i)
            self.audit.save_evidence(f'empty-{i}', np.zeros((100,100,3), dtype=np.uint8))
        with patch('app.face_search.time.sleep'):
            self.search.index_existing(limit=2)
            self.assertEqual(len(calls), 2)
            self.search.index_existing(limit=2)
            self.assertEqual(len(calls), 4)
            self.search.index_existing(limit=4)
        self.assertEqual(len(calls), 4)
        self.assertEqual(self.search.index_status['cached_no_faces'], 4)
        self.assertEqual(self.search.coverage()['events_indexed'], 0)

    def test_negative_cache_survives_restart_but_image_model_and_force_invalidate(self):
        from app.face_search import FaceSearch
        calls = []
        self.engine.faces = lambda image: calls.append(True) or []
        self.event('empty', 'yard', 100)
        path = self.audit.save_evidence('empty', np.zeros((100,100,3), dtype=np.uint8))
        with patch('app.face_search.time.sleep'):
            self.search.index_existing()
            restored = FaceSearch(self.audit, self.gallery, self.engine)
            restored.index_existing()
            self.assertEqual(len(calls), 1)
            self.engine.det_size = 320
            restored.index_existing()
            self.assertEqual(len(calls), 2)
            restored.index_existing(force=True)
            self.assertEqual(len(calls), 3)
            self.audit.save_evidence('empty', np.ones((120,120,3), dtype=np.uint8))
            restored.index_existing()
            self.assertEqual(len(calls), 4)

    def test_negative_cache_invalidates_on_model_file_and_preprocessor_change(self):
        from app.face_search import FaceSearch
        model = self.root / 'detector.onnx'
        model.write_bytes(b'first')
        self.engine.app = SimpleNamespace(models={'detection':SimpleNamespace(model_file=str(model))})
        calls = []
        self.engine.faces = lambda image: calls.append(True) or []
        self.event('model-change', 'yard', 100)
        self.audit.save_evidence('model-change', np.zeros((100,100,3), dtype=np.uint8))
        with patch('app.face_search.time.sleep'):
            self.search.index_existing()
            model.write_bytes(b'new model')
            self.search.index_existing()
            with patch('app.face_search.INDEX_PREPROCESSOR', 'next-preprocessor'):
                self.search.index_existing()
        self.assertEqual(len(calls), 3)

    def test_remote_negative_cache_uses_image_fingerprint_and_errors_are_retryable(self):
        self.event('remote-empty', 'yard', 100)
        calls = []
        self.engine.faces = lambda image: calls.append(True) or []
        image = np.zeros((100,100,3), dtype=np.uint8)
        self.search.frigate = SimpleNamespace(snapshot=lambda *a, **kw: image)
        with patch('app.face_search.time.sleep'):
            self.search.index_existing(remote=True)
            self.search.index_existing(remote=True)
            self.assertEqual(len(calls), 1)
            image[:] = 1
            self.search.index_existing(remote=True)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.search.index_status['cached_no_faces'], 1)

    def test_inference_failure_and_decode_failure_do_not_create_negative_cache(self):
        self.event('retry', 'yard', 100)
        path = self.audit.save_evidence('retry', np.zeros((100,100,3), dtype=np.uint8))
        self.engine.faces = lambda image: (_ for _ in ()).throw(RuntimeError('temporary inference failure'))
        with patch('app.face_search.time.sleep'):
            self.search.index_existing()
            with self.audit._connection() as con:
                self.assertEqual(con.execute('SELECT COUNT(*) FROM face_index_empty').fetchone()[0], 0)
            self.engine.faces = lambda image: [self.face]
            self.search.index_existing()
        self.assertEqual(len(self.search.sample_vectors(event_id='retry')), 1)
        self.event('bad-image', 'yard', 101)
        bad = self.audit.evidence_path('bad-image')
        bad.write_bytes(b'not a JPEG')
        with patch('app.face_search.time.sleep'):
            self.search.index_existing()
        with self.audit._connection() as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM face_index_empty').fetchone()[0], 0)

    def test_negative_cache_removed_with_deleted_history(self):
        self.event('empty-private', 'yard', 100, person='Alice')
        self.audit.save_evidence('empty-private', np.zeros((100,100,3), dtype=np.uint8))
        self.engine.faces = lambda image: []
        with patch('app.face_search.time.sleep'):
            self.search.index_existing()
        self.audit.delete_person_history('Alice')
        with self.audit._connection() as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM face_index_empty').fetchone()[0], 0)

    def test_unknown_recovery_obeys_local_limit_without_redetection(self):
        for i in range(3):
            eid = f'unknown-{i}'
            self.event(eid, 'yard', 100+i)
            self.gallery.save_unknown(np.zeros((80,80,3), dtype=np.uint8), vector(), {'event_id':eid})
        self.engine.faces = lambda image: self.fail('stored vectors must be reused')
        with patch('app.face_search.time.sleep'):
            self.search.index_existing(limit=1)
        self.assertEqual(self.search.index_status['total'], 1)
        self.assertEqual(self.search.coverage()['events_indexed'], 1)

    def test_automatic_index_waits_for_thirty_idle_seconds_after_foreground_work(self):
        clock = [0.0]
        self.search._idle_since = 0
        activity = {'active':True, 'last_activity_monotonic':0.0}
        self.engine.live_activity = lambda: activity.copy()
        def advance(seconds):
            clock[0] += seconds
            if clock[0] >= 3:
                activity.update(active=False, last_activity_monotonic=3.0)
        with patch('app.face_search.time.monotonic', side_effect=lambda: clock[0]), patch('app.face_search.time.sleep', side_effect=advance):
            self.search._wait_for_idle(True)
        self.assertGreaterEqual(clock[0], 33.0)
        self.assertFalse(self.search.index_status['waiting_for_idle'])

    def test_batch_image_limit_resumes_same_cursor_and_live_work_preempts_next_image(self):
        calls, pauses = [], []
        clock = [100.0]
        self.search._idle_since = 0
        for i in range(30):
            self.event(f'batch-{i}', 'yard', 100+i)
            self.audit.save_evidence(f'batch-{i}', np.zeros((100,100,3), dtype=np.uint8))
        active = [False]
        def infer(image):
            calls.append(clock[0])
            if len(calls) == 3:
                active[0] = True
            return [self.face]
        self.engine.faces = infer
        self.search._live_activity = lambda: active[0]
        def advance(seconds):
            if seconds == .25:
                pauses.append(len(calls))
                active[0] = False
            clock[0] += seconds
        with patch('app.face_search.time.monotonic', side_effect=lambda: clock[0]), patch('app.face_search.time.sleep', side_effect=advance):
            self.search.index_existing(automatic=True)
        self.assertEqual(len(calls), 30)
        self.assertIn(3, pauses)
        self.assertIn(28, pauses)
        self.assertGreaterEqual(calls[28]-calls[27], 30)
        self.assertEqual(self.search.coverage()['events_indexed'], 30)

    def test_slow_single_model_call_finishes_then_batch_yields(self):
        clock = [100.0]
        calls = []
        self.search._idle_since = 0
        for i in range(2):
            self.event(f'slow-{i}', 'yard', 100+i)
            self.audit.save_evidence(f'slow-{i}', np.zeros((100,100,3), dtype=np.uint8))
        def infer(image):
            calls.append(clock[0])
            clock[0] += 6
            return [self.face]
        self.engine.faces = infer
        def advance(seconds):
            clock[0] += seconds
        with patch('app.face_search.time.monotonic', side_effect=lambda: clock[0]), patch('app.face_search.time.sleep', side_effect=advance):
            self.search.index_existing(automatic=True)
        self.assertEqual(len(calls), 2)
        self.assertGreaterEqual(calls[1]-calls[0], 36)

    def test_explicit_force_requires_verified_admin_and_preserves_normal_api(self):
        with patch.object(self.search, 'start_index', return_value={'running':True}) as start:
            response = self.client.post('/api/faces/index?force=true')
            self.assertEqual(response.status_code, 403)
            start.assert_not_called()
            response = self.client.post('/api/faces/index?limit=7')
            self.assertEqual(response.status_code, 200)
            start.assert_called_once_with(remote=False, limit=7, force=False)
        from unittest.mock import AsyncMock
        with patch('app.access1_admin.require_pairing_admin', new_callable=AsyncMock, return_value='verified'), patch.object(self.search, 'start_index', return_value={'running':True}) as start:
            response = self.client.post('/api/faces/index?force=true&limit=9')
            self.assertEqual(response.status_code, 200)
            start.assert_called_once_with(remote=False, limit=9, force=True)

    def test_idle_activity_ignores_completed_events_but_keeps_pending_acquisition(self):
        import threading
        from app.face_search import recognition_active
        processor = SimpleNamespace(_jobs_lock=threading.RLock(), _queued_jobs=set(), _deferred_jobs={}, events={},
            stream_observations=SimpleNamespace(lock=threading.RLock(), sessions={}, pending=set()),
            continuous=SimpleNamespace(lock=threading.RLock(), pending=set()))
        processor.events['done'] = {'done':True, 'processing_lock':threading.Lock()}
        self.assertFalse(recognition_active(processor))
        processor.stream_observations.sessions['stream'] = {}
        self.assertTrue(recognition_active(processor))
        processor.stream_observations.sessions.clear()
        processor._queued_jobs.add(('id', 'snapshot'))
        self.assertTrue(recognition_active(processor))



    def test_unknown_recovery_without_engine_still_reuses_stored_vectors(self):
        self.event('engine-off', 'yard', 100)
        self.gallery.save_unknown(np.zeros((80,80,3), dtype=np.uint8), vector(), {'event_id':'engine-off'})
        self.search.engine = None
        with patch('app.face_search.time.sleep'):
            self.search.index_existing()
        self.assertEqual(len(self.search.sample_vectors(event_id='engine-off')), 1)



    def test_remote_cached_images_still_obey_decode_batch_budget(self):
        clock, received = [100.0], []
        image = np.zeros((100,100,3), dtype=np.uint8)
        self.engine.faces = lambda image: []
        for i in range(27):
            eid = f'remote-cached-{i}'
            self.event(eid, 'yard', 100+i)
            self.search._index_image(eid, image)
        self.engine.faces = lambda image: self.fail('successful empty results must be reused')
        def snapshot(eid, crop):
            received.append(clock[0])
            return image
        self.search.frigate = SimpleNamespace(snapshot=snapshot)
        self.search._idle_since = 0
        def advance(seconds):
            clock[0] += seconds
        with patch('app.face_search.time.monotonic', side_effect=lambda: clock[0]), patch('app.face_search.time.sleep', side_effect=advance):
            self.search.index_existing(remote=True, automatic=True)
        self.assertEqual(len(received), 27)
        self.assertGreaterEqual(received[25]-received[24], 30)
        self.assertEqual(self.search.index_status['cached_no_faces'], 27)


if __name__ == '__main__':
    unittest.main()
