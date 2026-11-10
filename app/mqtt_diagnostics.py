"""Bounded, passive broker diagnostics. No payloads or credentials are retained."""
import re
import threading
import time
from collections import OrderedDict


class MQTTDiagnostics:
    def __init__(self):
        self.lock = threading.Lock()
        self.connections = self.disconnects = self.messages = self.event_messages = 0
        self.invalid_messages = 0
        self.last_message = self.last_event_message = None
        self.cameras = OrderedDict()
        self.subscription = 'not_requested'
        self.mid = None

    def connection(self, state):
        with self.lock:
            self.mid = None
            self.subscription = state
            if state == 'connecting_subscription':
                self.connections += 1
            elif state == 'disconnected':
                self.disconnects += 1

    def subscribed(self, result):
        with self.lock:
            if isinstance(result, tuple) and len(result) == 2 and result[0] == 0:
                self.mid = result[1]
                self.subscription = 'awaiting_ack'
            else:
                self.mid = None
                self.subscription = 'request_failed'

    def acknowledge(self, mid, reasons):
        with self.lock:
            if self.mid is not None and self.mid == mid:
                self.mid = None
                granted = bool(reasons) and all(getattr(code, 'value', code) in (0,1,2) for code in reasons)
                self.subscription = 'acknowledged' if granted else 'denied'

    def message(self, is_event):
        with self.lock:
            self.messages += 1
            self.last_message = time.time()
            if is_event:
                self.event_messages += 1
                self.last_event_message = self.last_message

    def parsed(self, after):
        camera = after.get('camera') if isinstance(after, dict) else None
        eid = after.get('id') if isinstance(after, dict) else None
        with self.lock:
            if (not isinstance(camera,str) or not re.fullmatch(r'[\w .()\-]{1,100}',camera)
                    or not isinstance(eid,str) or not re.fullmatch(r'[A-Za-z0-9_.\-]{1,200}',eid)):
                self.invalid_messages += 1
                return
            row = self.cameras.pop(camera, None) or {'count':0,'person_count':0}
            row.update(count=row['count']+1, last_at=time.time(), last_event_id=eid)
            if after.get('label') == 'person':
                row['person_count'] += 1
                row['last_person_id'] = eid
            self.cameras[camera] = row
            if len(self.cameras)>32:
                self.cameras.popitem(last=False)

    def status(self, camera):
        with self.lock:
            return {'connections':self.connections, 'disconnects':self.disconnects,
                    'event_subscription':self.subscription, 'messages':self.messages,
                    'last_message_at':self.last_message,'event_topic_messages':self.event_messages,
                    'last_event_topic_at':self.last_event_message,'invalid_event_messages':self.invalid_messages,
                    'selected_camera':dict(self.cameras.get(camera, {}))}


def record_mqtt(processor, method, *args):
    """Diagnostics cannot interrupt broker subscriptions or recognition."""
    try:
        monitor = getattr(processor, 'mqtt_diagnostics', None)
        if monitor is not None:
            getattr(monitor, method)(*args)
    except Exception:
        return
