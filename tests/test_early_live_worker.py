import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import test_stream_observation as scenarios
from app.subject_association import single_anchor


class EarlyWorkerTests(unittest.TestCase):
    def test_model_pending_and_event_end_during_reference_processing(self):
        f = scenarios.StreamTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        entered = threading.Event()
        release = threading.Event()

        def slow_anchor(*args, **kwargs):
            entered.set()
            if not release.wait(10):
                raise AssertionError("camera worker waited for reference processing")
            return single_anchor(*args, **kwargs)

        with ThreadPoolExecutor(max_workers=1) as pool:
            f.m.anchor_executor = pool
            with patch('app.stream_observation.single_anchor', side_effect=slow_anchor):
                try:
                    session = f.trigger()
                    f.frame(1)
                    self.assertTrue(entered.wait(5))
                    f.frame(2)
                    self.assertEqual(session['early_candidate'][0]['sequence'], 2)
                    f.p.liveness.analyze.assert_not_called()
                    future = session['anchor_future']
                    f.trigger(kind='end')
                    self.assertNotIn('early_candidate', session)
                finally:
                    release.set()
                future.result(timeout=5)
        self.assertIsNone(session['anchor'])
        self.assertFalse(f.m.sessions)
        self.assertTrue(f.integration._queue.empty())
        f.integration.transport.call.assert_not_called()
