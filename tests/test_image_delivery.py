"""Fast image routes preserve authorization and event existence checks."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import numpy as np
from fastapi.testclient import TestClient
from app.audit import AuditStore
from app.gallery import Gallery
from app.webui import build_app

class ImageDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.audit = AuditStore(self.root / 'audit.db')
        self.gallery = Gallery(self.root)
        self.processor = SimpleNamespace(audit=self.audit, set_sub_label=False,
            frigate=SimpleNamespace(snapshot=Mock(side_effect=AssertionError('No upstream fetch for cached evidence'))))
        self.client = TestClient(build_app({'faceid': {}, 'frigate': {'url': ''}}, SimpleNamespace(),
            self.gallery, self.processor, self.root, self.root))

    def test_cached_image_does_not_load_observation_history(self):
        self.audit.start_event('cached', 'door', 1)
        self.audit.save_evidence('cached', np.zeros((80,80,3), dtype=np.uint8))
        self.audit.event_detail = Mock(side_effect=AssertionError('Thumbnail must not expand event history'))
        response = self.client.get('/api/activity/cached/image')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content.startswith(b'\xff\xd8'))
        self.assertIn('private', response.headers['cache-control'])
        self.audit.event_detail.assert_not_called()

    def test_orphan_image_cannot_bypass_deleted_event_check(self):
        self.audit.save_evidence('orphan', np.zeros((80,80,3), dtype=np.uint8))
        self.assertEqual(self.client.get('/api/activity/orphan/image').status_code, 404)

    def test_gallery_images_are_private_cached_and_path_traversal_still_fails(self):
        slug = self.gallery.create_person('Example')
        vector = np.zeros(512, dtype=np.float32); vector[0] = 1
        self.gallery.add_face(slug, np.zeros((80,80,3), dtype=np.uint8), vector)
        filename = self.gallery.persons()[slug]['files'][0]
        response = self.client.get(f'/media/persons/{slug}/{filename}')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['cache-control'], 'private, max-age=300')
        self.assertEqual(self.client.get('/media/persons/%2e%2e/audit.db').status_code, 404)
