import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from app.audit import AuditStore
from app.gallery import Gallery
from app.camera_profiles import CameraProfiles
from app.alerting import AlertStore


class AlertTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.gallery = Gallery(self.root)
        self.slug = self.gallery.create_person('Alice')
        self.profiles = CameraProfiles(self.root / 'cameras.json')
        self.audit = AuditStore(self.root / 'audit.db')
        self.processor = SimpleNamespace(client=Mock(), prefix='faceid', camera_profiles=self.profiles, audit=self.audit)
        self.alerts = AlertStore(self.audit, self.gallery, self.processor)

    def test_combined_reasons_deduplicate_persist_and_publish_nonretained(self):
        self.gallery.set_alert_policy(self.slug, watchlist='black', notify_on_recognition=True)
        self.profiles.update('door', min_face_px=48, role='entry', critical=True)
        result = self.alerts.record(event_id='one', camera='door', status='recognized', person='Alice', score=.8)
        self.assertEqual(result['reasons'], ['critical_camera', 'person_notification', 'blacklist'])
        self.assertIsNone(self.alerts.record(event_id='one', camera='door', status='recognized', person='Alice'))
        events = [call for call in self.processor.client.publish.call_args_list if call.args[0] == 'faceid/v1/alerts']
        self.assertEqual(len(events), 1)
        self.assertFalse(events[0].kwargs['retain'])
        restored = AlertStore(self.audit, Gallery(self.root), self.processor)
        self.assertEqual(restored.list(after=0)['alerts'][0]['watchlist'], 'black')
        self.assertEqual(Gallery(self.root).persons()[self.slug]['watchlist'], 'black')
        self.alerts.publish_latest(self.processor.client)
        import json
        self.assertEqual(json.loads(self.processor.client.publish.call_args.args[1])['person'], 'Alice')

    def test_unknown_never_inherits_probable_blacklist_and_no_face_never_alerts(self):
        self.gallery.set_alert_policy(self.slug, watchlist='black', notify_on_recognition=True)
        self.assertIsNone(self.alerts.record(event_id='guess', camera='door', status='ambiguous', person='Alice'))
        self.profiles.update('door', min_face_px=48, role='entry', critical=True)
        result = self.alerts.record(event_id='unknown', camera='door', status='unknown', person='Alice')
        self.assertEqual(result['reasons'], ['critical_camera'])
        self.assertIsNone(result['person'])
        self.assertIsNone(self.alerts.record(event_id='none', camera='door', status='no_face'))

    def test_initial_cursor_does_not_replay_old_alerts(self):
        self.gallery.set_alert_policy(self.slug, notify_on_recognition=True)
        self.alerts.record(event_id='old', camera='door', status='recognized', person='Alice')
        baseline = self.alerts.list()
        self.assertEqual(baseline['alerts'], [])
        self.alerts.record(event_id='new', camera='door', status='recognized', person='Alice')
        self.assertEqual([a['event_id'] for a in self.alerts.list(after=baseline['cursor'])['alerts']], ['new'])

    def test_every_recognition_is_opt_in_without_changing_policy_mqtt(self):
        old = self.alerts.record(event_id='old-normal', camera='door', status='recognized', person='Alice', ts=1)
        self.assertEqual(self.alerts.list(after=0)['alerts'], [])
        self.assertEqual(self.processor.client.publish.call_count, 0)
        baseline = self.alerts.list(include_recognitions=True)
        self.assertEqual(baseline['alerts'], [])
        current = self.alerts.record(event_id='normal', camera='door', status='recognized', person='Alice')
        result = self.alerts.list(after=baseline['cursor'], include_recognitions=True)['alerts']
        self.assertEqual([a['event_id'] for a in result], ['normal'])
        self.assertEqual(result[0]['image_url'], 'api/activity/normal/image')
        self.assertGreater(old['notified_ts'], old['ts'])
        self.assertGreater(current['id'], old['id'])
        self.assertIsNone(self.alerts.record(event_id='normal', camera='door', status='recognized', person='Alice'))
        self.assertIsNone(self.alerts.record(event_id='guess', camera='door', status='ambiguous', person='Alice'))

    def test_normal_recognition_never_changes_or_resurrects_policy_sensor(self):
        import json
        self.gallery.set_alert_policy(self.slug, notify_on_recognition=True)
        policy = self.alerts.record(event_id='policy', camera='door', status='recognized', person='Alice')
        self.gallery.set_alert_policy(self.slug, notify_on_recognition=False)
        self.alerts.record(event_id='normal', camera='door', status='recognized', person='Alice')
        self.alerts.publish_latest(self.processor.client)
        self.assertEqual(json.loads(self.processor.client.publish.call_args.args[1])['id'], policy['id'])
        with self.audit._lock, self.audit._connection() as con:
            con.execute("DELETE FROM recognition_alerts WHERE event_id='policy'")
        restored = AlertStore(self.audit, self.gallery, self.processor)
        restored.publish_latest(self.processor.client)
        self.assertIsNone(json.loads(self.processor.client.publish.call_args.args[1])['person'])

    def test_all_recognition_pagination_and_selected_evidence_url(self):
        self.alerts.record(event_id='one', camera='door', status='recognized', person='Alice')
        self.alerts.record(event_id='two', camera='door', status='recognized', person='Alice')
        first = self.alerts.list(after=0, limit=1, include_recognitions=True)
        self.assertTrue(first['has_more'])
        self.assertEqual(first['alerts'][0]['image_url'], 'api/activity/one/image')
        self.assertEqual(self.alerts.list(after=first['cursor'], include_recognitions=True)['alerts'][0]['event_id'], 'two')

    def test_policy_route_validation_and_readonly_role(self):
        import json
        from fastapi.testclient import TestClient
        from app.access_control import AccessControl
        from app.webui import build_app
        path = self.root / 'access.json'
        path.write_text(json.dumps({'assignments': {'viewer': 'viewer'}}), encoding='utf-8')
        self.processor.access_control = AccessControl(path, enabled=True)
        client = TestClient(build_app({'faceid': {}, 'frigate': {'url': ''}}, SimpleNamespace(), self.gallery, self.processor, self.root, self.root))
        endpoint = f'/api/persons/{self.slug}/policy'
        self.assertEqual(client.patch(endpoint, json={'watchlist': 'invalid'}).status_code, 422)
        self.assertEqual(client.patch(endpoint, json={'watchlist': 'black'}, headers={'x-remote-user-id': 'viewer'}).status_code, 403)
        self.assertEqual(client.patch(endpoint, json={'watchlist': 'white', 'notify_on_recognition': True}).status_code, 200)
        self.assertEqual(Gallery(self.root).alert_policy('Alice'), {'watchlist': 'white', 'notify_on_recognition': True})
        self.assertEqual(client.get('/api/users').json()['users'][0]['watchlist'], 'white')
        profile = client.post('/api/cameras/door/profile', json={'min_face_px': 48, 'role': 'entry', 'critical': True, 'fast_recognition': True})
        self.assertEqual(profile.status_code, 200)
        self.assertTrue(profile.json()['profile']['critical'])
        profile = client.post('/api/cameras/door/profile', json={'min_face_px': 48, 'role': 'entry'})
        self.assertTrue(profile.json()['profile']['critical'])
        self.assertTrue(profile.json()['profile']['fast_recognition'])
        self.assertEqual(client.get('/api/alerts').json()['alerts'], [])

    def test_concurrent_same_event_only_alerts_once(self):
        from concurrent.futures import ThreadPoolExecutor
        self.gallery.set_alert_policy(self.slug, notify_on_recognition=True)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.alerts.record(event_id='same', camera='door', status='recognized', person='Alice'), range(8)))
        self.assertEqual(sum(result is not None for result in results), 1)
        events = [call for call in self.processor.client.publish.call_args_list if call.args[0] == 'faceid/v1/alerts']
        self.assertEqual(len(events), 1)

    def test_alerts_follow_person_purge_and_audit_retention(self):
        self.gallery.set_alert_policy(self.slug, notify_on_recognition=True)
        self.audit.start_event('purge', 'door', 1)
        self.audit.finalize('purge', 'recognized', person='Alice')
        self.alerts.record(event_id='purge', camera='door', status='recognized', person='Alice')
        self.audit.delete_person_history('Alice')
        self.assertEqual(self.alerts.list(after=0)['alerts'], [])
        self.alerts.record(event_id='old', camera='door', status='recognized', person='Alice', ts=1)
        self.audit.prune(90)
        self.assertEqual(self.alerts.list(after=0)['alerts'], [])

    def test_sensor_discovery_and_reconnect_restore_state_without_replaying_event(self):
        import json
        self.processor.prefix = 'entry-faceid'
        self.gallery.set_alert_policy(self.slug, notify_on_recognition=True)
        first = self.alerts.record(event_id='first', camera='door', status='recognized', person='Alice')
        second = self.alerts.record(event_id='second', camera='door', status='recognized', person='Alice')
        self.assertGreater(second['id'], first['id'])
        self.processor.client.reset_mock()
        self.alerts.publish_discovery(self.processor.client)
        calls = self.processor.client.publish.call_args_list
        config = json.loads(calls[0].args[1])
        self.assertEqual(calls[0].args[0], 'homeassistant/sensor/entry-faceid_last_alert/config')
        self.assertEqual(config['unique_id'], 'entry-faceid_last_alert')
        self.assertEqual(config['availability_topic'], 'entry-faceid/status')
        self.assertEqual(config['state_topic'], 'entry-faceid/v1/alerts/state')
        self.assertEqual(config['value_template'], '{{ value_json.id }}')
        self.assertTrue(all(call.kwargs['retain'] for call in calls))
        self.assertFalse(any(call.args[0] == 'entry-faceid/v1/alerts' for call in calls))
        latest = json.loads(calls[-1].args[1])
        self.assertEqual(latest['id'], second['id'])
        self.assertEqual(latest['event_id'], 'second')
        self.assertEqual(latest['reasons'], ['person_notification'])
        self.assertFalse(any('image' in key for key in latest))

    def test_sensor_clears_private_attributes_after_purge_without_reusing_sequence(self):
        import json
        self.gallery.set_alert_policy(self.slug, notify_on_recognition=True)
        first = self.alerts.record(event_id='first', camera='door', status='recognized', person='Alice')
        with self.audit._lock, self.audit._connection() as con:
            con.execute('DELETE FROM recognition_alerts')
        self.processor.client.reset_mock()
        self.alerts.publish_discovery(self.processor.client)
        cleared = json.loads(self.processor.client.publish.call_args.args[1])
        self.assertEqual(cleared['id'], first['id'])
        self.assertIsNone(cleared['person'])
        second = self.alerts.record(event_id='second', camera='door', status='recognized', person='Alice')
        self.assertGreater(second['id'], first['id'])

    def test_mqtt_reconnect_wires_sensor_discovery_without_replaying_alert(self):
        from app.mqtt_listener import EventProcessor
        self.gallery.set_alert_policy(self.slug, notify_on_recognition=True)
        latest = self.alerts.record(event_id='saved', camera='door', status='recognized', person='Alice')
        self.processor.frigate_topic = 'frigate'
        self.processor.continuous = SimpleNamespace(reconnect=Mock())
        self.processor.alerts = self.alerts
        self.processor._publish_discovery = Mock()
        self.processor.client.reset_mock()
        EventProcessor._on_connect(self.processor, self.processor.client, None, None, 0, None)
        import json
        calls = self.processor.client.publish.call_args_list
        state = next(call for call in calls if call.args[0] == 'faceid/v1/alerts/state')
        self.assertEqual(json.loads(state.args[1])['id'], latest['id'])
        self.assertTrue(state.kwargs['retain'])
        self.assertFalse(any(call.args[0] == 'faceid/v1/alerts' for call in calls))

    def test_person_purge_route_clears_retained_mqtt_metadata_immediately(self):
        import json
        from fastapi.testclient import TestClient
        from app.webui import build_app
        self.gallery.set_alert_policy(self.slug, notify_on_recognition=True)
        self.audit.start_event('purge-route', 'door', 1)
        self.audit.finalize('purge-route', 'recognized', person='Alice')
        self.alerts.record(event_id='purge-route', camera='door', status='recognized', person='Alice')
        self.processor.alerts = self.alerts
        client = TestClient(build_app({'faceid': {}, 'frigate': {'url': ''}}, SimpleNamespace(), self.gallery, self.processor, self.root, self.root))
        self.processor.client.reset_mock()
        response = client.delete(f'/api/persons/{self.slug}?purge_history=true')
        self.assertEqual(response.status_code, 200)
        state = next(call for call in self.processor.client.publish.call_args_list if call.args[0] == 'faceid/v1/alerts/state')
        self.assertIsNone(json.loads(state.args[1])['person'])
        self.assertIsNone(json.loads(state.args[1])['event_id'])
        self.assertTrue(state.kwargs['retain'])

    def test_retention_clears_retained_metadata_without_reconnect(self):
        import json
        self.gallery.set_alert_policy(self.slug, notify_on_recognition=True)
        self.alerts.record(event_id='expired', camera='door', status='recognized', person='Alice', ts=1)
        self.processor.client.reset_mock()
        self.audit.prune(90)
        state = next(call for call in self.processor.client.publish.call_args_list if call.args[0] == 'faceid/v1/alerts/state')
        self.assertIsNone(json.loads(state.args[1])['person'])
        self.assertTrue(state.kwargs['retain'])
