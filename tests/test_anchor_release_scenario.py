import json,time,unittest
from types import SimpleNamespace
from unittest.mock import Mock
from concurrent.futures import ThreadPoolExecutor
import cv2,numpy as np
import test_stream_observation as scenarios
from app.frigate_api import FrigateAPI
from app.mqtt_listener import EventProcessor


class ReleaseScenario(unittest.TestCase):
    def test_two_second_db_readiness_with_real_anchor_executor(self):
        fixture=scenarios.StreamTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        pool=ThreadPoolExecutor(max_workers=2)
        self.addCleanup(pool.shutdown)
        fixture.m.anchor_executor=pool
        started=time.monotonic()
        fetches=[]
        jpeg=cv2.imencode('.jpg',np.zeros((100,100,3),np.uint8))[1].tobytes()
        def request(method,path,**kwargs):
            elapsed=time.monotonic()-started
            if path.endswith('snapshot.jpg'):
                return SimpleNamespace(close=Mock(),status_code=200,content=jpeg,
                    headers={'Cache-Control':'no-store','X-Frame-Time':str(time.time())})
            return SimpleNamespace(close=Mock(),status_code=200 if elapsed>=2 else 404,
                json=lambda:{'camera':'front','label':'person','start_time':time.time()-10})
        fixture.frigate.request=request
        def fetch(eid,camera):
            fetches.append(time.monotonic()-started)
            return FrigateAPI.association_frame_result(fixture.frigate,eid,camera)
        fixture.m.anchor_executor=pool
        with unittest.mock.patch('app.stream_observation.fetch_anchor',side_effect=lambda obj,eid,cam:fetch(eid,cam)):
            session=fixture.trigger()
            deadline=session['deadline']
            for index in range(1,36):
                fixture.frame(index)
                if session['st'].get('done'):break
                time.sleep(.12)
        stages = [item['stage'] for item in session['timeline']]
        self.assertLess(stages.index('early_face_detected'), stages.index('association_anchor_ready'))
        # 6.7.8: quick retries while Frigate has not stored the event, never hammering it.
        self.assertGreater(len(fetches),2)
        self.assertTrue(all(.25 <= later-earlier < 1 for earlier,later in zip(fetches,fetches[1:])))
        self.assertGreaterEqual(fetches[-1],2)
        self.assertEqual(session['deadline'],deadline)
        self.assertEqual(session['st']['stream_samples'],3)
        self.assertEqual(fixture.integration._queue.qsize(),1)
        fixture.integration.transport.call.assert_not_called()

    def test_broker_ack_and_occupancy_are_distinct_in_capture(self):
        fixture=scenarios.StreamTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        p=fixture.p
        p.recognition_capture.start('admin','front','frigate_restream',20)
        p._publish_discovery=Mock();p.client=Mock();p.client.subscribe.return_value=(0,40)
        p._on_connect(p.client,None,None,0,None)
        p._on_subscribe(p.client,None,40,[0],None)
        msg=lambda topic,data: SimpleNamespace(topic=topic,payload=data,retain=False)
        p._on_message(p.client,None,msg('frigate/front/person',b'1'))
        p._on_message(p.client,None,msg('frigate/events',b'malformed'))
        p._on_message(p.client,None,msg('frigate/events',json.dumps({'type':'new','after':
            {'id':'evt','camera':'front','label':'person','start_time':time.time()}}).encode()))
        p.recognition_capture.stop('admin')
        report=p.recognition_capture.report('admin')
        info=report['after']['mqtt']
        self.assertEqual(info['event_subscription'],'acknowledged')
        self.assertEqual(info['messages'],3)
        self.assertEqual(info['invalid_event_messages'],1)
        self.assertEqual(info['event_topic_messages'],2)
        self.assertEqual(info['selected_camera']['last_person_id'],'evt')
        self.assertEqual(report['event_count'],1)
        fixture.integration.transport.call.assert_not_called()
