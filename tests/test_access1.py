import tempfile
import unittest
import time
from pathlib import Path

from app.audit import AuditStore
from app.access1 import Access1Integration


class Provider:
    def __init__(self):
        self.calls = []

    def users(self):
        return [{'id':'u1','name':'Existing user'}]

    def readers(self):
        return [{'id':'r1','name':'Entrance'}]

    def present_biometric(self, **credential):
        self.calls.append(credential)
        return {'decision':'granted','event_id':'access1-event-123'}


class Access1Tests(unittest.TestCase):
    def test_disabled_without_a_real_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            service=Access1Integration(Path(tmp),AuditStore(Path(tmp)/'audit.db'))
            self.assertEqual(service.status()['state'],'awaiting_api')
            with self.assertRaises(ValueError):
                service.configure({'enabled':True})

    def test_permission_request_uses_mapped_user_and_reader_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider=Provider()
            service=Access1Integration(Path(tmp),AuditStore(Path(tmp)/'audit.db'),provider=provider)
            service.configure({'enabled':True,'people':{'alice':'u1'},'cameras':{'door':'r1'}})
            event={'event_id':'face-1','slug':'alice','camera':'door','liveness_confirmed':True,'score':.8,'occurred_at':time.time(), 'evidence_ts':time.time(), 'evidence_source':'secure_live_hires','event_ended':False}
            result=service.present(event)
            self.assertEqual(result['decision'],'granted')
            self.assertEqual(provider.calls[0]['user_id'],'u1')
            self.assertEqual(provider.calls[0]['reader_id'],'r1')
            self.assertEqual(provider.calls[0]['credential_type'],'face')
            self.assertEqual(service.present(event)['state'],'duplicate')
            self.assertEqual(len(provider.calls),1)

    def test_unmapped_or_non_live_faces_never_reach_access1(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider=Provider()
            service=Access1Integration(Path(tmp),AuditStore(Path(tmp)/'audit.db'),provider=provider)
            service.configure({'enabled':True,'people':{'alice':'u1'},'cameras':{'door':'r1'}})
            for slug,camera,live in [('other','door',True),('alice','yard',True),('alice','door',False)]:
                result=service.present({'event_id':slug+camera+str(live),'slug':slug,'camera':camera,'liveness_confirmed':live})
                self.assertEqual(result['state'],'blocked')
            self.assertEqual(provider.calls,[])

    def test_old_clip_and_ended_evidence_cannot_be_presented(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider=Provider()
            service=Access1Integration(Path(tmp),AuditStore(Path(tmp)/'audit.db'),provider=provider)
            service.configure({'enabled':True,'people':{'alice':'u1'},'cameras':{'door':'r1'}})
            for source,ts,ended in [('clip:12',time.time(),False),('secure_live_hires',time.time()-60,False),('secure_live_hires',time.time(),True)]:
                result=service.present({'event_id':str(ts)+source,'slug':'alice','camera':'door','liveness_confirmed':True,'evidence_source':source,'evidence_ts':ts,'event_ended':ended})
                self.assertEqual(result['state'],'blocked')
            self.assertEqual(provider.calls,[])


if __name__ == '__main__': unittest.main()
