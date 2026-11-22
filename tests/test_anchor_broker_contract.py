import unittest
import time
from types import SimpleNamespace
from unittest.mock import Mock
import numpy as np
import cv2
from app.frigate_api import FrigateAPI
from app.mqtt_diagnostics import MQTTDiagnostics


class AnchorContract(unittest.TestCase):
    def read(self, before=200, after=None, headers=None):
        def resp(code, value):
            return SimpleNamespace(status_code=code, json=lambda:value, close=Mock())
        active={'camera':'front','label':'person','start_time':time.time()-5}
        crop=resp(200,None)
        crop.headers=headers if headers is not None else {'Cache-Control':'no-store','X-Frame-Time':str(time.time())}
        crop.content=cv2.imencode('.jpg',np.zeros((25,25,3),np.uint8))[1].tobytes()
        api=SimpleNamespace(request=Mock(side_effect=[resp(before,active),crop,resp(200,after or active)]))
        return FrigateAPI.association_frame_result(api,'event','front')
    def test_ready_and_db_not_ready(self):
        self.assertEqual(self.read()[1],'ready')
        self.assertEqual(self.read(before=404),(None,'metadata_not_ready'))
        self.assertEqual(self.read(before=403),(None,'metadata_http_403'))
    def test_image_and_context_rejections(self):
        for headers,why in [({'Cache-Control':'private'},'crop_not_active'),({'Cache-Control':'no-store'},'crop_timestamp_invalid')]:
            self.assertEqual(self.read(headers=headers),(None,why))
        self.assertEqual(self.read(after={'camera':'other','label':'person'}),(None,'subject_mismatch'))
        self.assertEqual(self.read(after={'camera':'front','label':'person','end_time':123}),(None,'event_ended'))


class BrokerContract(unittest.TestCase):
    def test_subscription_lifecycle_and_selected_camera(self):
        d=MQTTDiagnostics()
        d.connection('connecting_subscription')
        d.subscribed((0,42));d.acknowledge(43,[0])
        self.assertEqual(d.status('front')['event_subscription'],'awaiting_ack')
        d.acknowledge(42,[128])
        self.assertEqual(d.status('front')['event_subscription'],'denied')
        d.connection('disconnected')
        d.connection('connecting_subscription')
        d.subscribed((0,55));d.acknowledge(55,[0])
        d.message(False);d.message(True);d.parsed(None)
        d.message(True);d.parsed({'id':'evt1','label':'person','camera':'front','private':'never_export'})
        s=d.status('front')
        self.assertEqual((s['event_subscription'],s['connections'],s['disconnects']),('acknowledged',2,1))
        self.assertEqual((s['messages'],s['event_topic_messages'],s['invalid_event_messages']),(3,2,1))
        self.assertEqual(s['selected_camera']['last_person_id'],'evt1')
        self.assertEqual(d.status('other')['selected_camera'],{})
        self.assertNotIn('never_export',str(s))
        d.subscribed((4,0))
        self.assertEqual(d.status('front')['event_subscription'],'request_failed')
