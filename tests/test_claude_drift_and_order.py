"""6.8.5: stream clock drift is not backlog; Access1 is queued before bookkeeping."""
import unittest
from unittest.mock import Mock

from app.live_frames import LiveFrameReader
import tests.test_live_consensus_bench as consensus

STEP = 0.12          # one delivered frame every 120 ms (25 fps, every 3rd frame)
DRIFT = 0.0125       # stream clock ~12.5 ms/s slower than ours (captures 27/28)


def feed(reader, seconds, *, start=0.0, wait=STEP, drift=DRIFT, extra=None):
    """Deliver frames for `seconds`; returns (accepted count, total, last lag)."""
    accepted = total = 0
    t = start
    while t < start + seconds:
        t += STEP
        reader.note_read(wait)
        lag = extra(t) if extra else 0.0
        position = (t * (1 - drift) - lag) * 1000
        accepted += reader.accept_position(position, 100 + t, 100.0)
        total += 1
    return accepted, total, reader.lag_ms


class DriftTests(unittest.TestCase):
    def test_slow_decoder_cannot_disguise_backlog_as_clock_drift(self):
        reader = LiveFrameReader('rtsp://fixture/front')
        for n in range(1, 501):
            reader.note_read(.126)
            accepted = reader.accept_position(n * 120, 100 + n * .126, 100)
        self.assertFalse(accepted)
        self.assertGreater(reader.rejections['buffering_growth'], 0)
        self.assertGreater(reader.lag_ms, 2900)

    def test_same_drift_with_instant_reads_is_still_rejected(self):
        # Reads returning at once mean frames were queued: keep the strict baseline.
        reader = LiveFrameReader('rtsp://fixture/front')
        feed(reader, 90, wait=0.001)
        self.assertGreater(reader.rejections['buffering_growth'], 0)

    def test_backlog_after_long_drift_is_caught(self):
        reader = LiveFrameReader('rtsp://fixture/front')
        feed(reader, 60, drift=0)
        self.assertEqual(reader.rejections.get('buffering_growth', 0), 0)
        # Decoder falls behind: frames queue (instant reads), lag grows 100 ms/s.
        feed(reader, 12, start=60, wait=0.001, extra=lambda t: (t - 60) * 0.1)
        self.assertGreater(reader.rejections['buffering_growth'], 0)
        self.assertGreater(reader.lag_ms, 750)

    def test_fast_backlog_with_blocking_reads_is_caught_within_the_window(self):
        reader = LiveFrameReader('rtsp://fixture/front')
        feed(reader, 30)
        feed(reader, 9, start=30, extra=lambda t: (t - 30) * 0.15)
        self.assertGreater(reader.rejections['buffering_growth'], 0)

    def test_report_preserves_lag_without_drift_compensation(self):
        reader = LiveFrameReader('rtsp://fixture/front')
        feed(reader, 30)
        self.assertEqual(reader.report()['drift_ms'], 0)
        self.assertGreater(reader.report()['lag_ms'], 200)


class Access1OrderTests(unittest.TestCase):
    def test_access1_is_queued_before_evidence_notification_and_audit_flush(self):
        fixture = consensus.LiveConsensusTests()
        p, st = fixture.setup_processor()
        order = []
        p.access1.enqueue = Mock(side_effect=lambda event: order.append('access1'))
        p.audit = Mock()
        p.audit.save_evidence.side_effect = lambda *a, **k: order.append('evidence')
        p._publish_notification = Mock(side_effect=lambda *a, **k: order.append('notification'))
        p._audit_flush = Mock(side_effect=lambda *a, **k: order.append('flush'))
        for n in (1, 2):
            fixture.frame(p, st, n)
        p.access1.enqueue.assert_not_called()
        order.clear()  # earlier frames may store a provisional evidence crop
        fixture.frame(p, st, 3)
        self.assertEqual(order[0], 'access1')
        self.assertEqual(order.count('access1'), 1)
        self.assertLess(order.index('access1'), order.index('flush'))
        self.assertLess(order.index('access1'), order.index('evidence'))
        self.assertLess(order.index('access1'), order.index('notification'))
        payload = p.access1.enqueue.call_args.args[0]
        self.assertEqual((payload['event_id'], payload['slug'], payload['liveness_frames']), ('e', 'd', 3))
        self.assertTrue(st['done'])


if __name__ == '__main__':
    unittest.main()
