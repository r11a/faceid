import tempfile
import time
import unittest
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


if __name__ == '__main__':
    unittest.main()
