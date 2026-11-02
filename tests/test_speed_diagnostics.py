"""Offline queue isolation, diagnostics and PAD runtime regressions."""
import logging
import queue
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.liveness import LivenessDetector
from app.timing import stage, frame_wait
import test_access1 as fixtures


class SpeedDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.Access1Tests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.ready()
        self.integration = self.fixture.integration

    def event(self, name='one'):
        event = self.fixture.event()
        event['event_id'] = name
        return event

    def test_pending_receipt_does_not_delay_next_submission(self):
        def reply(method, request, payload=None):
            return {'status': 'pending' if method == 'POST' else 'relay_activated'}
        self.integration.transport.call.side_effect = reply
        first = self.integration.present(self.event(), defer=True)
        second = self.integration.present(self.event('two'), defer=True)
        self.assertEqual(first['state'], 'pending')
        self.assertEqual(second['state'], 'pending')
        self.assertEqual([c.args[0] for c in self.integration.transport.call.call_args_list], ['POST', 'POST'])
        self.integration._result_once()
        self.integration._result_once()
        self.assertTrue(all(row['state'] == 'relay_activated' for row in self.integration.recent()))
        self.assertEqual(self.integration._result_queue.unfinished_tasks, 0)

    def test_slow_get_does_not_block_new_post(self):
        entered, release = threading.Event(), threading.Event()
        def reply(method, request, payload=None):
            if method == 'GET':
                entered.set()
                if not release.wait(3):
                    raise TimeoutError('fake receiver')
                return {'status': 'relay_activated'}
            return {'status': 'pending'}
        self.integration.transport.call.side_effect = reply
        self.integration.present(self.event(), defer=True)
        worker = threading.Thread(target=self.integration._result_once)
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            result = self.integration.present(self.event('two'), defer=True)
            self.assertEqual(result['state'], 'pending')
            self.assertFalse(release.is_set())
            self.assertEqual(sum(c.args[0] == 'POST' for c in self.integration.transport.call.call_args_list), 2)
        finally:
            release.set()
            worker.join(4)
        self.assertFalse(worker.is_alive())

    def test_uncertain_post_is_never_replayed_and_duplicate_is_blocked(self):
        self.integration.transport.call.side_effect = [TimeoutError('secret'), {'status':'completed'}]
        event = self.event()
        self.assertEqual(self.integration.present(event, defer=True)['state'], 'pending')
        self.assertEqual(self.integration.present(event, defer=True)['state'], 'duplicate')
        self.integration._result_once()
        self.assertEqual([c.args[0] for c in self.integration.transport.call.call_args_list], ['POST','GET'])

    def test_expired_pending_job_does_not_poll_or_repost(self):
        self.integration.transport.call.return_value={'status':'pending'}
        self.integration.present(self.event(), defer=True)
        job=self.integration._result_queue.get_nowait()
        self.integration._result_queue.task_done()
        job['monotonic_deadline']=time.monotonic()-1
        self.integration._result_queue.put_nowait(job)
        self.integration._result_once()
        self.assertEqual(self.integration.transport.call.call_count,1)
        self.assertEqual(self.integration.recent()[0]['state'],'unconfirmed')

    def test_connector_change_never_redirects_pending_get(self):
        previous=self.integration.transport
        previous.call.return_value={'status':'pending'}
        self.integration.present(self.event(),defer=True)
        self.integration.transport=Mock()
        self.integration._result_once()
        self.integration.transport.call.assert_not_called()
        self.assertEqual(previous.call.call_count,1)
        self.assertEqual(self.integration.last_submission['reason'],'access1_connector_changed')

    def test_full_receipt_queue_is_bounded_and_never_grants(self):
        self.integration._result_queue=queue.Queue(maxsize=1)
        self.integration._result_queue.put_nowait('occupied')
        self.integration.transport.call.return_value={'status':'pending'}
        result=self.integration.present(self.event(),defer=True)
        self.assertEqual(result['state'],'unconfirmed')
        self.assertEqual(result['reason'],'access1_result_queue_full')
        self.assertEqual(self.integration.transport.call.call_count,1)

    def test_requeued_job_keeps_original_deadlines(self):
        self.integration.transport.call.return_value={'status':'pending'}
        self.integration.present(self.event(),defer=True)
        job=self.integration._result_queue.queue[0]
        deadlines=job['deadline'],job['monotonic_deadline']
        self.integration._result_once()
        self.assertIs(self.integration._result_queue.queue[0],job)
        self.assertEqual((job['deadline'],job['monotonic_deadline']),deadlines)

    def test_all_access_gates_still_apply_to_deferred_path(self):
        for changes in ({'liveness_confirmed':False},{'event_ended':True},
                        {'evidence_ts':time.time()-11},{'person_id':'changed'},
                        {'camera_id':'changed'},{'evidence_source':'recording'}):
            with self.subTest(changes=changes):
                event=self.event();event.update(changes)
                self.assertEqual(self.integration.present(event,defer=True)['state'],'blocked')
        self.integration.transport.call.assert_not_called()
        self.assertTrue(self.integration._result_queue.empty())

    def test_terminal_denial_is_not_queued(self):
        self.integration.transport.call.return_value={'status':'blocked','reason':'interlock_active'}
        self.assertEqual(self.integration.present(self.event(),defer=True)['reason'],'interlock_active')
        self.assertTrue(self.integration._result_queue.empty())

    def test_late_pending_display_cannot_replace_terminal_result(self):
        self.integration.transport.call.side_effect=[{'status':'pending'},{'status':'relay_activated'}]
        event=self.event()
        pending=self.integration.present(event,defer=True)
        self.integration._result_once()
        self.integration._record_submission(event,pending)
        self.assertEqual(self.integration.last_submission['state'],'relay_activated')

    def test_older_receipt_cannot_replace_newer_attempt_display(self):
        self.integration.transport.call.side_effect=[{'status':'pending'},
                                                    {'status':'blocked','reason':'interlock_active'},
                                                    {'status':'relay_activated'}]
        # Two submissions can share a monotonic clock tick on Windows.
        with patch('app.access1.time.monotonic', return_value=time.monotonic()):
            self.integration.present(self.event('older'),defer=True)
            self.integration.present(self.event('newer'),defer=True)
            self.integration._result_once()
        self.assertEqual(self.integration.last_submission['face_event_id'],'newer')
        self.assertEqual(self.integration.last_submission['state'],'blocked')
        states={row['face_event_id']:row['state'] for row in self.integration.recent()}
        self.assertEqual(states,{'older':'relay_activated','newer':'blocked'})

    def test_result_get_failure_is_unconfirmed_without_replay(self):
        self.integration.transport.call.side_effect=[{'status':'pending'},TimeoutError('PRIVATE')]
        self.integration.present(self.event(),defer=True)
        self.integration._result_once()
        self.assertEqual(self.integration.last_submission['state'],'unconfirmed')
        self.assertEqual(self.integration.last_submission['reason'],'access1_transport_unavailable')
        self.assertNotIn('PRIVATE',str(self.integration.last_submission))
        self.assertEqual([c.args[0] for c in self.integration.transport.call.call_args_list],['POST','GET'])

    def test_receipt_worker_starts_once(self):
        with patch('app.access1.threading.Thread') as factory:
            self.integration.start_worker()
            self.integration.start_worker()
        self.assertEqual(factory.call_count,3)
        self.assertEqual({c.kwargs['name'] for c in factory.call_args_list},
                         {'access1-secure-status','access1-results','access1-identities'})

    def test_unexpected_receipt_failure_does_not_kill_worker_or_grant(self):
        self.integration.transport.call.return_value={'status':'pending'}
        self.integration.present(self.event(),defer=True)
        with patch.object(self.integration,'_poll_result',side_effect=RuntimeError('PRIVATE')):
            self.integration._result_once()
        self.assertEqual(self.integration.last_submission['state'],'unconfirmed')
        self.assertEqual(self.integration.last_submission['reason'],'access1_result_tracking_failed')
        self.assertEqual(self.integration._result_queue.unfinished_tasks,0)
        self.assertEqual(self.integration.transport.call.call_count,1)

    def test_timing_switch_and_failed_stage_recording(self):
        processor=SimpleNamespace(record_timing=Mock(),timing_diagnostics=False)
        with self.assertNoLogs('faceid.timing',level='INFO'):
            with stage(processor,'event-1','liveness_inference'):pass
        processor.timing_diagnostics=True
        with self.assertLogs('faceid.timing',level='INFO') as captured:
            with self.assertRaises(ValueError):
                with stage(processor,'event-1','liveness_inference'):
                    raise ValueError('PRIVATE DO NOT LOG')
        self.assertEqual(processor.record_timing.call_count,2)
        self.assertIn('event=event-1',captured.output[0])
        self.assertNotIn('PRIVATE',str(captured.output))

    def test_frame_wait_notes_are_optional_and_rate_limited(self):
        processor=SimpleNamespace(timing_diagnostics=False)
        state={}
        with self.assertNoLogs('faceid.timing',level='INFO'):
            frame_wait(processor,'one',state,'duplicate_image')
        self.assertEqual(state,{})
        processor.timing_diagnostics=True
        with patch('app.timing.time.monotonic',side_effect=[10,10.1,11.1]):
            with self.assertLogs('faceid.timing',level='INFO') as captured:
                for _ in range(3):frame_wait(processor,'one',state,'duplicate_image')
        self.assertEqual(len(captured.output),2)
        self.assertEqual(set(state),{'timing_wait_logged'})

    def test_liveness_runtime_honors_existing_cpu_options(self):
        import onnxruntime as ort
        detector=LivenessDetector(cpu_threads=2,cpu_allow_spinning=False)
        with patch.object(Path,'is_file',return_value=True), \
             patch.object(ort,'get_available_providers',return_value=['CPUExecutionProvider']), \
             patch.object(ort,'InferenceSession') as factory:
            self.assertTrue(detector.prepare())
            options=factory.call_args.kwargs['sess_options']
            self.assertEqual(options.intra_op_num_threads,2)
            self.assertEqual(options.get_session_config_entry('session.intra_op.allow_spinning'),'0')
            detector.prepare()
            self.assertEqual(factory.call_count,1)

    def test_default_and_accelerated_runtime_not_retuned(self):
        import onnxruntime as ort
        for providers,kwargs in [(['CPUExecutionProvider'],{}),(['CUDAExecutionProvider','CPUExecutionProvider'],{'cpu_threads':2})]:
            with self.subTest(providers=providers):
                with patch.object(Path,'is_file',return_value=True), \
                     patch.object(ort,'get_available_providers',return_value=providers), \
                     patch.object(ort,'InferenceSession') as factory:
                    self.assertTrue(LivenessDetector(**kwargs).prepare())
                    self.assertNotIn('sess_options',factory.call_args.kwargs)

    def test_preload_failure_does_not_create_live_evidence(self):
        detector=LivenessDetector()
        with patch.object(Path,'is_file',return_value=False):
            self.assertFalse(detector.prepare())
        self.assertIsNone(detector._session)
        self.assertFalse(detector.consensus([])['confirmed'])
        disabled=LivenessDetector(enabled=False)
        with patch.object(disabled,'_runtime') as runtime:
            self.assertFalse(disabled.prepare())
            runtime.assert_not_called()


if __name__=='__main__':unittest.main()
