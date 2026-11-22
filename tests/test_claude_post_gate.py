"""6.8.6: bookkeeping waits for the Access1 POST to start; quicker first result check."""
import threading
import time
import unittest
from unittest.mock import Mock, patch

import app.access1 as access1_module
import app.mqtt_listener as listener
import tests.test_access1 as base
import tests.test_live_consensus_bench as consensus


class ConnectorGateTests(unittest.TestCase):
    # Reuse the Access1 fixture without re-running its tests.
    setUp = base.Access1Tests.setUp
    event = base.Access1Tests.event
    ready = base.Access1Tests.ready

    def start_worker(self):
        threading.Thread(target=self.integration._worker, daemon=True).start()

    def test_gate_is_set_by_the_time_the_post_is_sent(self):
        self.ready()
        seen = []
        self.integration.transport.call.side_effect = lambda *a: seen.append(gate.is_set()) or {'status': 'accepted'}
        gate = self.integration.enqueue(self.event())
        self.assertIsInstance(gate, threading.Event)
        self.assertFalse(gate.is_set())
        self.start_worker()
        self.assertTrue(gate.wait(2))
        self.integration._queue.join()
        self.assertEqual(seen, [True])

    def test_gate_is_released_when_the_request_is_blocked_before_post(self):
        self.ready()
        self.processor.camera_enabled.return_value = False
        gate = self.integration.enqueue(self.event())
        self.start_worker()
        self.assertTrue(gate.wait(2))
        self.integration._queue.join()
        self.integration.transport.call.assert_not_called()

    def test_disabled_connector_returns_no_gate(self):
        self.assertIsNone(self.integration.enqueue(self.event()))

    def test_pre_post_work_is_timed_in_parts(self):
        self.ready()
        stages = []
        self.integration._timing = lambda event, stage, started: stages.append(stage)
        self.integration.present(self.event())
        for stage in ('access1_pre_insert', 'access1_attempt_insert', 'access1_pre_post', 'access1_post'):
            self.assertIn(stage, stages)
        self.assertLess(stages.index('access1_pre_post'), stages.index('access1_post'))

    def test_first_result_check_is_quick_then_backs_off(self):
        self.ready()
        self.integration.transport.call.side_effect = [{'status': 'pending'}] * 4 + [{'status': 'accepted'}]
        with patch('app.access1.time.sleep') as sleep:
            result = self.integration.present(self.event())
        self.assertEqual(result['state'], 'accepted')
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [.1, .2, .3, .3])
        self.assertEqual(access1_module.RESULT_POLL_SECONDS[0], .1)


class ProcessorGateTests(unittest.TestCase):
    def run_recognition(self, gate):
        fixture = consensus.LiveConsensusTests()
        p, st = fixture.setup_processor()
        order = []
        p.access1.enqueue = Mock(side_effect=lambda event: order.append('access1') or gate)
        p._audit_flush = Mock(side_effect=lambda *a, **k: order.append(('flush', gate.is_set())))
        for n in (1, 2):
            fixture.frame(p, st, n)
        started = time.monotonic()
        fixture.frame(p, st, 3)
        self.assertTrue(st['done'])
        return order, time.monotonic() - started

    def test_bookkeeping_waits_for_the_post_to_start(self):
        gate = threading.Event()
        threading.Timer(0.1, gate.set).start()
        order, _ = self.run_recognition(gate)
        self.assertEqual(order[0], 'access1')
        self.assertIn(('flush', True), order)

    def test_wait_is_bounded_when_the_post_never_starts(self):
        gate = threading.Event()
        with patch.object(listener, 'ACCESS1_POST_GATE_SECONDS', 0.05):
            order, elapsed = self.run_recognition(gate)
        self.assertIn(('flush', False), order)
        self.assertLess(elapsed, 1.0)

    def test_gate_default_is_half_a_second(self):
        self.assertEqual(listener.ACCESS1_POST_GATE_SECONDS, 0.5)


if __name__ == '__main__':
    unittest.main()
