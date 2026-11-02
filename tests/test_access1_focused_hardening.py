import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
import numpy as np
import test_access1 as baseline
from test_live_consensus_bench import LiveConsensusTests
from app.liveness import LivenessDetector
from app.access1_transport import Transport

class NumericTests(unittest.TestCase):
    def test_invalid_outputs_fail_closed(self):
        for values in ([float('nan'),0],[float('inf'),float('inf')],[1],[1,2,3]):
            detector=LivenessDetector()
            detector._session=Mock()
            detector._session.get_inputs.return_value=[SimpleNamespace(name='input')]
            detector._session.run.return_value=[np.asarray([values])]
            result=detector.analyze(np.zeros((120,120,3),dtype=np.uint8),SimpleNamespace(bbox=[20,20,100,100]))
            self.assertEqual(result['state'],'unavailable')
            self.assertIsNone(result['live'])

class SubjectTests(unittest.TestCase):
    def test_subject_change_during_spoof_recovery_cannot_reuse_other_person(self):
        fixture=LiveConsensusTests();p,st=fixture.setup_processor()
        for n,slug,live in ((1,'Alice',True),(2,'Alice',False),(3,'Bob',True),
                            (4,'Bob',True),(5,'Alice',True)):
            p.gallery.match_candidates.return_value=[(slug,slug,.8),('other','Other',.1)]
            p.liveness.analyze.return_value={'state':'live' if live else 'spoof','live':live,'score':.9 if live else .1}
            fixture.frame(p,st,n)
            p.access1.enqueue.assert_not_called()
        for n in (6,7):fixture.frame(p,st,n)
        p.access1.enqueue.assert_called_once()

    def test_subject_change_requires_three_frames_of_new_subject(self):
        fixture=LiveConsensusTests();p,st=fixture.setup_processor()
        for n,slug in enumerate(('Alice','Bob','Bob','Bob'),1):
            p.gallery.match_candidates.return_value=[(slug,slug,.8),('other','Other',.1)]
            fixture.frame(p,st,n)
            if n<4:p.access1.enqueue.assert_not_called()
        self.assertEqual(st['final_decision'].person,'Bob')
        self.assertEqual(p.access1.enqueue.call_args.args[0]['liveness_frames'],3)

    def test_duplicate_embedding_cannot_pad_proof(self):
        fixture=LiveConsensusTests();p,st=fixture.setup_processor()
        for n in (1,2,2):fixture.frame(p,st,n)
        p.access1.enqueue.assert_not_called()
        fixture.frame(p,st,3)
        p.access1.enqueue.assert_called_once()

class ReceiptTests(unittest.TestCase):
    setUp=baseline.Access1Tests.setUp
    ready=baseline.Access1Tests.ready
    event=baseline.Access1Tests.event

    def test_native_status_contract_and_unknown_rejection(self):
        transport=Transport.__new__(Transport)
        event_id='12345678-1234-1234-1234-123456789abc'
        for status in ('relay_activated','blocked','expired_unconfirmed','accepted','denied'):
            transport._request=Mock(return_value={'event_id':event_id,'source':'faceid','status':status})
            self.assertEqual(transport.call('GET',event_id)['status'],status)
        transport._request.return_value['status']='invented_success'
        with self.assertRaises(ValueError):transport.call('GET',event_id)
        transport._request.return_value.update(status='relay_activated',source='other')
        with self.assertRaises(ValueError):transport.call('GET',event_id)

    def test_native_receipt_retains_reason_and_separate_confirmations(self):
        self.ready()
        self.integration.transport.call.return_value={'status':'relay_activated','reason':'manual_access',
            'execution_confirmed':True,'door_open_confirmed':False}
        result=self.integration.present(self.event())
        self.assertEqual(result['state'],'relay_activated')
        self.assertTrue(result['execution_confirmed'])
        self.assertFalse(result['door_open_confirmed'])
        self.assertEqual(result['reason'],'manual_access')
        self.integration.transport.call.assert_called_once()

    def test_denial_reason_and_untrusted_reason_filter(self):
        self.ready()
        self.integration.transport.call.return_value={'status':'denied','reason':'faceid_person_unmapped'}
        self.assertEqual(self.integration.present(self.event())['reason'],'faceid_person_unmapped')

    def test_lost_post_only_reconciles_get_and_never_reposts(self):
        self.ready()
        self.integration.transport.call.side_effect=[OSError('private credential'),
            {'status':'relay_activated','execution_confirmed':True,'door_open_confirmed':False}]
        with patch('app.access1.time.sleep'):
            result=self.integration.present(self.event())
        self.assertEqual(result['state'],'relay_activated')
        self.assertEqual([c.args[0] for c in self.integration.transport.call.call_args_list],['POST','GET'])
        self.assertEqual(self.integration.present(self.event())['state'],'duplicate')

    def test_unavailable_result_has_safe_reason(self):
        self.ready();self.integration.transport.call.side_effect=OSError('private credential')
        with patch('app.access1.time.sleep'):
            result=self.integration.present(self.event())
        self.assertEqual(result['state'],'unconfirmed')
        self.assertEqual(result['reason'],'access1_transport_unavailable')
        self.assertNotIn('private',str(result))

if __name__=='__main__':unittest.main()
