from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import cv2
import numpy as np

from app.timestamp_diagnostics import TimestampTrace, run_timestamp_test, native_reference


class TimestampDiagnosticTests(unittest.TestCase):
    def test_same_frame_readings_distinguish_quantization_from_delay(self):
        image = np.zeros((64, 80, 3), dtype=np.uint8)
        trace = TimestampTrace(Mock())
        values = {cv2.CAP_PROP_FPS:25., cv2.CAP_PROP_PTS:25., cv2.CAP_PROP_POS_MSEC:1000.}
        capture = SimpleNamespace(get=values.__getitem__)
        trace(capture, image, 1000., 13., 0., .01)
        values[cv2.CAP_PROP_POS_MSEC] = 1010.
        reason, generation = trace(capture, image, 1010., 13.01, 0., .01)
        self.assertIsNone(reason)
        self.assertEqual(generation, 0)
        self.assertEqual(trace.rows[-1][9], 'timestamp_nonprogressing')
        values[cv2.CAP_PROP_POS_MSEC] = 1040.
        values[cv2.CAP_PROP_PTS] = 26.
        reason, _ = trace(capture, image, 1040., 14.1, 0., 1.09)
        self.assertEqual(reason, 'buffering_growth')
        self.assertEqual(trace.rows[-1][9], 'buffering_growth')

    @patch('app.timestamp_diagnostics.native_reference')
    @patch('app.timestamp_diagnostics.run_decode')
    def test_single_result_overlaps_packet_probe_without_access_submission(self, decode, native):
        native.return_value = {'frames':[]}
        probe = Mock(return_value={'packets':10})
        phase = Mock()
        def exercise(*args, reader_factory, **kwargs):
            trace = reader_factory('unused', seconds=1).observer
            trace.phase_changed('timestamp_recognition_load')
            return {'decoder':{'decoder_stopped':True}}
        decode.side_effect = exercise
        connector = Mock()
        report = run_timestamp_test(SimpleNamespace(diagnostic_only=False,access1=connector),
                                    'door','unused',Mock(),probe,phase)
        self.assertEqual(connector.mock_calls, [])
        self.assertEqual(report['manual_access1_submission'], 'not_implemented')
        self.assertEqual(report['rtp_overlap']['result']['packets'], 10)
        self.assertEqual(report['native_ffmpeg']['frames'], [])
        self.assertEqual([call.args[0] for call in phase.call_args_list],
                         ['timestamp_connecting','timestamp_recognition_load','timestamp_packet_reference','timestamp_native_reference'])

    @patch('app.timestamp_diagnostics.subprocess.run')
    def test_reference_export_has_only_numeric_fields(self, run):
        run.return_value = SimpleNamespace(returncode=0,stdout=b'{"frames":[{"pts_time":"0.04","secret":"private"}]}')
        self.assertEqual(native_reference('fixture')['frames'], [{'pts_time':.04}])
