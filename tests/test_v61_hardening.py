import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from fastapi.testclient import TestClient
from types import SimpleNamespace

from app.gallery import Gallery
from app.backup_util import write_backup_file
from app.webui import build_app
from app.mqtt_listener import EventProcessor


class HardeningTests(unittest.TestCase):
    def test_callback_error_does_not_escape_network_thread(self):
        def broken(*args):
            raise RuntimeError('bad event')
        with self.assertLogs('faceid.mqtt', level='ERROR'):
            EventProcessor._guard_callback(broken)(None)

    def test_hebrew_names_and_ascii_collisions_remain_distinct(self):
        with tempfile.TemporaryDirectory() as tmp:
            g = Gallery(Path(tmp))
            slugs = [g.create_person(n) for n in ['רונן', 'דנה', 'A B', 'A-B']]
            self.assertEqual(len(set(slugs)), 4)
            self.assertEqual(g.create_person('רונן'), slugs[0])
            self.assertEqual(Gallery(Path(tmp)).persons()[slugs[1]]['name'], 'דנה')

    def test_failed_backup_does_not_leave_a_final_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch('app.backup_util.os.replace', side_effect=OSError('disk full')):
                with self.assertRaises(OSError):
                    write_backup_file(root, root / 'backups')
            self.assertEqual(list((root / 'backups').glob('*.tar.gz')), [])

    def test_empty_assignment_does_not_create_a_person(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            g = Gallery(root)
            client = TestClient(build_app({'faceid': {}, 'frigate': {'url': ''}}, None, g,
                SimpleNamespace(audit=None, set_sub_label=False), root, root))
            for ids in [[], ['no-longer-present']]:
                response = client.post('/api/unknowns/assign', json={'ids': ids, 'person': 'New Person'})
                self.assertEqual(response.status_code, 409)
                self.assertEqual(g.persons(), {})

    def test_addon_forwards_review_options_and_escapes_strings(self):
        root = Path(__file__).parents[1]
        run = (root / 'faceid-addon/run.sh').read_text(encoding='utf-8')
        for key in ['review_queue_max_total', 'review_queue_max_per_cluster',
                    'review_queue_retention_days', 'review_queue_dedupe_days']:
            self.assertIn(key + ':', run)
        self.assertIn('cfg_json', run)


if __name__ == '__main__':
    unittest.main()
