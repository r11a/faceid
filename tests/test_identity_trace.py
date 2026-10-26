import unittest
import test_live_consensus_bench as consensus

class CleanIdentityTests(unittest.TestCase):
    def test_pending_identity_remains_gated_without_trace(self):
        h=consensus.LiveConsensusTests();p,st=h.setup_processor()
        for n in (1,2):
            h.frame(p,st,n);p.access1.enqueue.assert_not_called()
        h.frame(p,st,3);p.access1.enqueue.assert_called_once()
        self.assertNotIn('live_attempt_summary',st)
        self.assertNotIn('frame_timing',st)
        payload=p.access1.enqueue.call_args.args[0]
        self.assertNotIn('chain_frames',payload)
        self.assertNotIn('first_live_monotonic',payload)

    def test_spoof_still_skips_identity_and_submission(self):
        h=consensus.LiveConsensusTests();p,st=h.setup_processor()
        p.liveness.analyze.return_value={'state':'spoof','live':False,'face_px':80}
        h.frame(p,st,1);p.gallery.match_candidates.assert_not_called();p.access1.enqueue.assert_not_called()

    def test_duplicate_does_not_add_identity_evidence(self):
        h=consensus.LiveConsensusTests();p,st=h.setup_processor()
        h.frame(p,st,1);h.frame(p,st,1)
        self.assertEqual(len(st['decision'].observations),1);p.access1.enqueue.assert_not_called()

    def test_weak_and_ambiguous_matches_remain_denied(self):
        for scores in ((.3,.01),(.7,.69)):
            h=consensus.LiveConsensusTests();p,st=h.setup_processor()
            p.gallery.match_candidates.return_value=[('d','David',scores[0]),('o','Other',scores[1])]
            for n in (1,2,3):h.frame(p,st,n)
            p.access1.enqueue.assert_not_called()
