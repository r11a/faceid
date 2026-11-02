import struct
import unittest
from app.acquisition_metrics import Timeline, signed_delta, receiver_feedback


class TimelineTests(unittest.TestCase):
    def test_backward_step_is_not_thirteen_hour_forward_jump(self):
        t=Timeline(90000)
        t.packet(1,300000,9,0)
        t.packet(2,303600,9,.04)
        t.packet(3,129630,9,.08)
        result=t.report()
        self.assertEqual(result['timestamp_backward_changes'],1)
        self.assertEqual(result['timestamp_backward_max_ms'],1933)
        self.assertEqual(result['rtp_frame_step_ms_max'],40)
        self.assertIsNone(result['rtp_timeline_span_seconds'])
        self.assertFalse(result['timestamp_progression_regular'])

    def test_natural_timestamp_and_sequence_wraps(self):
        t=Timeline(90000)
        t.packet(65535,2**32-1800,9,0)
        t.packet(0,1800,9,.04)
        r=t.report()
        self.assertEqual(r['timestamp_forward_wraps'],1)
        self.assertEqual(r['sequence_discontinuities'],0)
        self.assertEqual(r['timestamp_backward_changes'],0)
        self.assertEqual(r['rtp_timeline_span_seconds'],.04)
        self.assertEqual(r['buffering']['relative_delay_change_end_ms'],0)

    def test_source_change_never_joins_unrelated_clocks(self):
        t=Timeline(90000)
        t.packet(10,100,1,0);t.packet(22,3000000,2,.04)
        r=t.report()
        self.assertEqual(r['source_changes'],1)
        self.assertEqual(r['timestamp_forward_jumps'],0)
        self.assertEqual(r['sequence_discontinuities'],0)
        self.assertIsNone(r['rtp_timeline_span_seconds'])

    def test_out_of_order_and_duplicate_packets_do_not_move_frame_clock(self):
        t=Timeline(90000)
        t.packet(10,9000,1,0);t.packet(10,9000,1,.01)
        t.packet(9,5400,1,.02);t.packet(11,12600,1,.04)
        r=t.report()
        self.assertEqual(r['duplicate_packets'],1)
        self.assertEqual(r['out_of_order_packets'],1)
        self.assertEqual(r['distinct_rtp_timestamps'],2)
        self.assertEqual(r['timestamp_backward_changes'],0)
        self.assertIsNone(r['rtp_timeline_span_seconds'])

    def test_relative_catchup_and_growth_are_signed_and_not_absolute_age(self):
        t=Timeline(90000)
        t.packet(1,0,1,0);t.packet(2,3600,1,.01);t.packet(3,7200,1,.02)
        b=t.report()['buffering']
        self.assertEqual(b['relative_catchup_max_ms'],60)
        self.assertEqual(b['relative_delay_change_end_ms'],-60)
        self.assertFalse(b['absolute_capture_age_verified'])
        t.packet(4,10800,1,.3)
        self.assertEqual(t.report()['buffering']['relative_delay_growth_max_ms'],180)

    def test_large_forward_jump_is_excluded_from_cadence(self):
        t=Timeline(90000)
        t.packet(1,0,1,0);t.packet(2,900000,1,.04)
        r=t.report()
        self.assertEqual(r['timestamp_forward_jumps'],1)
        self.assertEqual(r['timestamp_forward_jump_max_ms'],10000)
        self.assertNotIn('rtp_frame_step_ms_max',r)
        self.assertIsNone(r['rtp_timeline_span_seconds'])

    def test_bounded_samples(self):
        t=Timeline(90000)
        for n in range(4100):t.packet(n,n*3600,1,n*.04)
        self.assertEqual(len(t.frames),4096)
        self.assertTrue(t.report()['sample_limit_reached'])
        self.assertIsNone(t.report()['rtp_timeline_span_seconds'])

    def test_empty_samples_and_zero_arrival_span(self):
        self.assertFalse(Timeline(90000).report()['timestamp_progression_regular'])
        t=Timeline(90000);t.packet(1,0,1,0);t.packet(2,3600,1,0)
        self.assertIsNone(t.report()['observed_arrival_fps'])

    def test_compound_feedback_has_sdes_without_personal_identifier(self):
        packet=receiver_feedback(123)
        self.assertEqual(packet[1],201)
        self.assertEqual(packet[9],202)
        self.assertEqual((struct.unpack('!H',packet[10:12])[0]+1)*4,len(packet)-8)
        self.assertIn(b'faceid-acquisition-test',packet)

    def test_signed_difference(self):
        self.assertEqual(signed_delta(0,2**32-1),1)
        self.assertEqual(signed_delta(10,11),-1)
        self.assertEqual(signed_delta(0,65535,16),1)


if __name__=='__main__':unittest.main()
