import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import cv2
import numpy as np
from fastapi.testclient import TestClient

from app.gallery import Gallery
from app.audit import AuditStore
from app.webui import build_app


class CameraEnrollmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.gallery = Gallery(self.root)
        self.slug = self.gallery.create_person('Alice')
        self.audit = AuditStore(self.root / 'audit.db')
        self.audit.set_ground_truth = Mock(wraps=self.audit.set_ground_truth)
        self.processor = SimpleNamespace(audit=self.audit, set_sub_label=False)
        self.engine = SimpleNamespace(faces=lambda image: [])
        self.client = TestClient(build_app({'faceid': {}, 'frigate': {'url': ''}},
            self.engine, self.gallery, self.processor, self.root, self.root))
        self.embedding = np.eye(1, 512, dtype=np.float32)[0]
        self.crop = np.random.default_rng(7).integers(45, 210, (100, 100, 3), dtype=np.uint8)

    def sample(self, uid='sample', crop=None, embedding=None):
        self.audit.start_event(uid, 'entrance', 1234)
        self.audit.finalize(uid, 'unknown', end_ts=1235)
        cv2.imwrite(str(self.gallery.unknown_dir / f'{uid}.jpg'), self.crop if crop is None else crop)
        (self.gallery.unknown_dir / f'{uid}.json').write_text(json.dumps({
            'embedding': (self.embedding if embedding is None else embedding).tolist(),
            'camera': 'entrance', 'event_id': uid, 'event_ts': 1234,
        }), encoding='utf-8')
        return uid

    def assign(self, ids, person=None):
        return self.client.post('/api/unknowns/assign', json={'ids': ids, 'person': person or self.slug})

    def test_sharp_crop_is_added_as_reference_with_provenance(self):
        response = self.assign([self.sample()])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['gallery_photos_added'], 1)
        self.assertEqual(response.json()['results'][0]['status'], 'learned')
        person = self.gallery.persons()[self.slug]
        self.assertEqual(person['count'], 1)
        self.assertEqual(person['sources'][person['files'][0]]['camera'], 'entrance')
        self.assertFalse((self.gallery.unknown_dir / 'sample.jpg').exists())
        self.audit.set_ground_truth.assert_called_once()

    def test_duplicate_does_not_add_reference(self):
        self.gallery.add_face(self.slug, self.crop, self.embedding)
        result = self.assign([self.sample()]).json()
        self.assertEqual(result['gallery_photos_added'], 0)
        self.assertEqual(result['duplicates'], 1)
        self.assertEqual(self.gallery.persons()[self.slug]['count'], 1)

    def test_identical_pixels_are_duplicate_even_with_different_embedding(self):
        self.gallery.add_face(self.slug, self.crop, np.roll(self.embedding, 1))
        result = self.assign([self.sample()]).json()
        self.assertEqual(result['gallery_photos_added'], 0)
        self.assertEqual(result['duplicates'], 1)

    def test_mixed_batch_learns_good_crop_and_keeps_rejected_crop_reviewable(self):
        self.sample('good')
        self.sample('bad', embedding=np.ones(10))
        result = self.assign(['good', 'bad'], 'New Person').json()
        self.assertEqual(result['gallery_photos_added'], 1)
        self.assertEqual(result['skipped'], 1)
        response = self.client.get('/api/unknowns')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0][0]['id'], 'bad')
        self.assertEqual(self.client.post('/api/unknowns/auto_assign').json()['total'], 0)

    def test_one_good_crop_creates_ready_new_person(self):
        result = self.assign([self.sample()], 'New Person').json()
        self.assertEqual(result['gallery_photos_added'], 1)
        self.assertTrue(result['ready'])
        self.assertEqual(self.gallery.persons()[result['slug']]['count'], 1)

    def test_bad_crop_is_retained_and_does_not_create_person(self):
        for uid, crop in [('blurred', np.full((100, 100, 3), 128, dtype=np.uint8)),
                          ('tiny', self.crop[:30, :30]), ('dark', self.crop // 20)]:
            result = self.assign([self.sample(uid, crop)], 'New Person').json()
            self.assertEqual(result['gallery_photos_added'], 0)
            self.assertEqual(result['skipped'], 1)
            self.assertTrue((self.gallery.unknown_dir / f'{uid}.jpg').exists())
        self.assertEqual(len(self.gallery.persons()), 1)
        self.audit.set_ground_truth.assert_not_called()

    def test_invalid_embedding_is_retained(self):
        for uid, embedding in [('wrongsize', self.embedding[:10]),
                               ('nan', np.full(512, np.nan)), ('zero', np.zeros(512))]:
            result = self.assign([self.sample(uid, embedding=embedding)]).json()
            self.assertEqual(result['gallery_photos_added'], 0)
            self.assertEqual(result['results'][0]['reason'], 'invalid_embedding')
            self.assertTrue((self.gallery.unknown_dir / f'{uid}.jpg').exists())

    def test_stale_and_traversal_ids_do_not_create_person(self):
        self.sample('outside')
        for uid in ['stale', '../outside', '..\\outside', 'C:outside', 'outside/../outside']:
            response = self.assign([uid], 'New Person')
            self.assertEqual(response.status_code, 409)
        self.assertEqual(len(self.gallery.persons()), 1)
        self.assertTrue((self.gallery.unknown_dir / 'outside.jpg').exists())

    def test_resolve_confirms_without_learning(self):
        response = self.client.post('/api/unknowns/resolve', json={
            'ids': [self.sample()], 'person': self.slug})
        self.assertEqual(response.json()['gallery_photos_added'], 0)
        self.assertEqual(response.json()['labeled'], 1)
        self.assertEqual(self.gallery.persons()[self.slug]['count'], 0)
        self.audit.set_ground_truth.assert_called_once_with(
            'sample', 'Alice', 'operator', action='review_queue_resolve')

    def test_auto_assign_confirms_without_learning(self):
        self.gallery.add_face(self.slug, self.crop, self.embedding)
        self.sample()
        response = self.client.post('/api/unknowns/auto_assign')
        self.assertEqual(response.json()['total'], 1)
        self.assertEqual(response.json()['gallery_photos_added'], 0)
        self.assertEqual(self.gallery.persons()[self.slug]['count'], 1)
        self.audit.set_ground_truth.assert_called_once()

    def history(self, eid='assigned', person='Alice', embedding=None):
        self.audit.start_event(eid, 'entrance', time.time())
        self.audit.finalize(eid, 'unknown')
        self.audit.set_ground_truth(eid, person)
        if embedding is not None:
            self.processor.face_search.record(eid, embedding, self.crop)
        return eid

    def references(self, eid='assigned', person=None):
        return self.client.get(f'/api/faces/event/{eid}/references', params={'person': person or self.slug})

    def test_assigned_history_can_explicitly_activate_empty_gallery(self):
        self.history(embedding=self.embedding)
        candidates = self.references().json()['candidates']
        self.assertEqual(self.gallery.persons()[self.slug]['count'], 0)
        self.engine.faces = lambda image: self.fail('must not redetect stored faces')
        result = self.client.post('/api/faces/enroll', json={'token': candidates[0]['token'], 'person': self.slug})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['added'], 1)
        self.assertTrue(result.json()['ready'])
        self.assertTrue(np.allclose(self.gallery.embeddings(self.slug)[0], self.embedding))

    def test_history_multiple_faces_require_explicit_candidate_choice(self):
        self.history(embedding=self.embedding)
        self.processor.face_search.record('assigned', np.roll(self.embedding, 1), self.crop[:, ::-1].copy())
        candidates = self.references().json()['candidates']
        self.assertEqual(len(candidates), 2)
        self.assertEqual(self.gallery.persons()[self.slug]['count'], 0)
        result = self.client.post('/api/faces/enroll', json={'token': candidates[1]['token'], 'person': self.slug})
        self.assertEqual(result.json()['added'], 1)
        self.assertTrue(np.allclose(self.gallery.embeddings(self.slug)[0], np.roll(self.embedding, 1)))

    def test_history_identity_is_rechecked_and_wrong_person_blocked(self):
        self.history(embedding=self.embedding)
        bob = self.gallery.create_person('Bob')
        self.assertEqual(self.references(person=bob).status_code, 409)
        candidate = self.references().json()['candidates'][0]
        self.audit.set_ground_truth('assigned', 'Bob')
        result = self.client.post('/api/faces/enroll', json={'token': candidate['token'], 'person': self.slug})
        self.assertEqual(result.status_code, 409)
        self.assertEqual(self.gallery.persons()[self.slug]['count'], 0)

    def test_expired_history_has_clear_upload_fallback(self):
        self.history()
        result = self.references()
        self.assertEqual(result.status_code, 410)
        self.assertIn('העלה', result.json()['detail'])

    def test_retained_historical_evidence_offers_each_detected_face(self):
        self.history()
        self.audit.save_evidence('assigned', self.crop)
        self.engine.faces = lambda image: [SimpleNamespace(bbox=np.array([0, 0, 100, 100]),
            normed_embedding=self.embedding, det_score=.99)]
        candidates = self.references().json()['candidates']
        self.assertEqual(len(candidates), 1)
        self.assertEqual(self.gallery.persons()[self.slug]['count'], 0)
        result = self.client.post('/api/faces/enroll', json={'token': candidates[0]['token'], 'person': self.slug})
        self.assertEqual(result.json()['added'], 1)

    def test_history_duplicate_does_not_inflate_reference_count(self):
        self.history(embedding=self.embedding)
        self.gallery.add_face(self.slug, self.crop, self.embedding)
        candidate = self.references().json()['candidates'][0]
        result = self.client.post('/api/faces/enroll', json={'token': candidate['token'], 'person': self.slug}).json()
        self.assertEqual(result['added'], 0)
        self.assertEqual(result['duplicates'], 1)
        self.assertEqual(self.gallery.persons()[self.slug]['count'], 1)

    def test_history_sample_membership_is_rechecked(self):
        self.history(embedding=self.embedding)
        candidate = self.references().json()['candidates'][0]
        with self.audit._connection() as con:
            con.execute('UPDATE face_samples SET event_id=? WHERE sample_id=?', ('different', candidate['sample_id']))
        response = self.client.post('/api/faces/enroll', json={'token': candidate['token'], 'person': self.slug})
        self.assertEqual(response.status_code, 410)
        self.assertEqual(self.gallery.persons()[self.slug]['count'], 0)

    def test_poor_quality_history_is_previewed_but_not_learned(self):
        self.history()
        self.processor.face_search.record('assigned', self.embedding, np.full((100, 100, 3), 128, dtype=np.uint8))
        candidate = self.references().json()['candidates'][0]
        self.assertFalse(candidate['usable'])
        self.assertEqual(candidate['reason'], 'blurred')
        response = self.client.post('/api/faces/enroll', json={'token': candidate['token'], 'person': self.slug})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.gallery.persons()[self.slug]['count'], 0)
