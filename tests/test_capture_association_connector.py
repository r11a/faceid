"""Passive capture must not change subject choice, authorization or requests."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from app.subject_association import single_anchor, usable_faces, mutual_assignments
from test_snapshot_subject_association import face, quality
import test_stream_observation as stream_fixtures
from app.recognition_capture import RecognitionCapture


class BrokenDiagnostics(dict):
    def update(self, *args, **kwargs):
        raise RuntimeError('diagnostic sink failed')


class AssociationCaptureTests(unittest.TestCase):
    def test_optional_assignment_details_preserve_matches_and_ambiguity(self):
        scenarios = [
            ([[1, 0, 0]], [face()], [0]),
            ([[1, 0, 0]], [face(), face()], [None]),
            ([[1, 0, 0], [1, 0, 0]], [face()], [None, None]),
            ([[1, 0, 0], [0, 1, 0]], [face(), face((0, 1, 0))], [0, 1]),
            ([[float('nan'), 0, 0]], [face()], [None]),
        ]
        for anchors, faces, expected in scenarios:
            for output in (None, {}, BrokenDiagnostics()):
                with self.subTest(expected=expected, output=type(output).__name__):
                    self.assertEqual(mutual_assignments(anchors, faces, diagnostics=output), expected)
                    if type(output) is dict:
                        self.assertEqual(output['anchor_count'], len(anchors))
                        self.assertEqual(output['candidate_count'], len(faces))
                        # Numeric summaries only; no vectors or embedding bytes.
                        json.dumps(output, allow_nan=False)
                        self.assertTrue(all(set(row) == {'association_score', 'alternative_score',
                            'association_margin', 'reverse_margin', 'reason'} for row in output['assignments']))

    def test_assignment_rejection_reason_reports_the_actual_numeric_gate(self):
        diagnostics = {}
        mutual_assignments([[1, 0, 0]], [face(), face()], diagnostics=diagnostics)
        self.assertEqual(diagnostics['assignments'][0]['reason'], 'anchor_alternative_ambiguous')
        self.assertEqual(diagnostics['assignments'][0]['association_margin'], 0)
        mutual_assignments([[1, 0, 0], [1, 0, 0]], [face()], diagnostics=diagnostics)
        self.assertEqual(diagnostics['assignments'][0]['reason'], 'anchor_competition_ambiguous')
        self.assertEqual(diagnostics['assignments'][0]['reverse_margin'], 0)

    def test_anchor_diagnostics_count_without_extra_inference(self):
        image = np.zeros((100, 100, 3), np.uint8)
        for output in (None, {}, BrokenDiagnostics()):
            engine = SimpleNamespace(detect_faces=Mock(return_value=[face()]), embed_face=Mock())
            with patch('app.subject_association.measure_face_quality', side_effect=quality):
                anchor, reason = single_anchor(engine, image, {}, min_face_px=24, min_quality=.35,
                                               diagnostics=output)
            np.testing.assert_array_equal(anchor, [1, 0, 0])
            self.assertEqual(reason, 'association_anchor_ready')
            engine.detect_faces.assert_called_once()
            engine.embed_face.assert_called_once()
            if type(output) is dict:
                self.assertEqual(output['candidate_count'], 1)
                self.assertEqual(output['usable_count'], 1)

    def test_quality_and_roi_summary_do_not_change_selection(self):
        image = np.zeros((100, 100, 3), np.uint8)
        diagnostics = {}
        faces = [face(box=(0, 0, 10, 10)), face(box=(70, 70, 90, 90))]
        profile = {'mode': 'intercom', 'roi': [.5, .5, 1, 1]}
        with patch('app.subject_association.measure_face_quality', side_effect=quality):
            selected = usable_faces(image, faces, profile, min_face_px=24, min_quality=.35,
                                    diagnostics=diagnostics)
        self.assertIs(selected[0][0], faces[1])
        self.assertEqual(diagnostics, {'candidate_count': 2, 'usable_count': 1,
                                     'roi_rejected_count': 1, 'quality_rejected_count': 0})


class CaptureFlowParityTests(unittest.TestCase):
    def fixture(self, mode):
        fixture = stream_fixtures.StreamTests()
        fixture.setUp()
        recorder = RecognitionCapture(fixture.p)
        fixture.p.recognition_capture = recorder
        if mode != 'off':
            with patch.object(recorder, '_run', return_value=None):
                recorder.start('admin', 'front', 'frigate_restream')
        if mode == 'failure':
            recorder.emit = Mock(side_effect=RuntimeError('private diagnostic failure'))
            recorder.preview = Mock(side_effect=RuntimeError('private preview failure'))
        return fixture, recorder

    def test_stream_and_connector_outcomes_are_same_off_active_and_recorder_failure(self):
        for mode in ('off', 'active', 'failure'):
            with self.subTest(mode=mode):
                fixture, recorder = self.fixture(mode)
                try:
                    session = fixture.confirmed()
                    self.assertEqual(session['st']['stream_samples'], 3)
                    self.assertTrue(session['st']['done'])
                    event = fixture.integration._queue.get_nowait()
                    result = fixture.integration.present(event)
                    self.assertEqual(result['state'], 'relay_activated')
                    fixture.integration.transport.call.assert_called_once()
                    method, _, payload = fixture.integration.transport.call.call_args.args
                    self.assertEqual(method, 'POST')
                    self.assertEqual(payload['liveness']['confirmed_frames'], 3)
                    self.assertNotIn('diagnostics', payload)
                    if mode == 'active':
                        report = recorder.report('admin')
                        records = report['events'][0]['records']
                        stages = {row['stage'] for row in records}
                        self.assertTrue({'stream_route', 'association_anchor_fetch', 'association_anchor_model',
                                         'association_candidates', 'association_match', 'access1_post_started',
                                         'access1_post_response', 'access1_result'} <= stages)
                        results = [row for row in records if row['stage'] == 'access1_result']
                        self.assertEqual(results[-1]['state'], 'relay_activated')
                        self.assertEqual(results[-1]['evidence_source'], 'bounded_local_stream')
                        output = json.dumps(report, allow_nan=False)
                        self.assertNotIn('Alice', output)
                        self.assertNotIn('normed_embedding', output)
                        self.assertNotIn('rtsp://', output)
                finally:
                    fixture.doCleanups()

    def test_ambiguous_subjects_still_cannot_complete_or_submit_with_capture(self):
        for mode in ('off', 'active', 'failure'):
            with self.subTest(mode=mode):
                fixture, recorder = self.fixture(mode)
                try:
                    session = fixture.trigger()
                    for n in (1, 2, 3):
                        fixture.frame(n, [fixture.face(n), fixture.face(n, [120, 0, 200, 80])])
                    self.assertEqual(session['st']['stream_samples'], 0)
                    self.assertTrue(fixture.integration._queue.empty())
                    fixture.integration.transport.call.assert_not_called()
                    if mode == 'active':
                        rows = recorder.report('admin')['events'][0]['records']
                        self.assertTrue(any(row.get('reason') == 'anchor_alternative_ambiguous' for row in rows))
                finally:
                    fixture.doCleanups()

    def test_retained_handled_offline_and_false_positive_have_skip_reasons(self):
        fixture, recorder = self.fixture('active')
        try:
            base = {'id': 'retained', 'camera': 'front', 'label': 'person'}
            fixture.m.event(base, 'new', retained=True)
            fixture.m.event({**base, 'id': 'false', 'false_positive': True}, 'new')
            fixture.p._handled.append('handled')
            fixture.m.event({**base, 'id': 'handled'}, 'new')
            fixture.m.online = False
            fixture.m.event({**base, 'id': 'offline'}, 'new')
            reasons = {row.get('reason') for event in recorder.report('admin')['events'] for row in event['records']}
            self.assertTrue({'retained_event', 'false_positive', 'already_handled', 'transport_disconnected'} <= reasons)
            self.assertFalse(fixture.m.sessions)
            fixture.frigate.live_frames.acquire_observation.assert_not_called()
            fixture.integration.transport.call.assert_not_called()
        finally:
            fixture.doCleanups()

    def test_connector_local_block_is_captured_without_http(self):
        fixture, recorder = self.fixture('active')
        try:
            fixture.integration.transport = None
            fixture.integration.enqueue({'event_id': 'disabled', 'camera': 'front',
                                         'evidence_source': 'bounded_local_stream'})
            self.assertTrue(fixture.integration._queue.empty())
            rows = recorder.report('admin')['events'][0]['records']
            self.assertEqual(rows[-1]['reason'], 'connector_disabled')
            self.assertEqual(rows[-1]['state'], 'blocked')
        finally:
            fixture.doCleanups()
