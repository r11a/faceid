"""Diagnostic-only RTP arithmetic. No recognition/access dependencies."""
import statistics
import struct


def signed_delta(current, previous, bits=32):
    half = 1 << (bits-1)
    return ((current-previous+half) % (1 << bits))-half


def receiver_feedback(receiver_ssrc):
    # Compound RR + SDES/CNAME; deliberately contains no host/user identifier.
    rr = struct.pack('!BBHI', 0x80, 201, 1, receiver_ssrc)
    cname = b'faceid-acquisition-test'
    chunk = struct.pack('!I',receiver_ssrc) + bytes([1,len(cname)]) + cname + b'\0'
    chunk += b'\0' * ((-len(chunk)) % 4)
    return rr + struct.pack('!BBH',0x81,202,len(chunk)//4) + chunk


class Timeline:
    def __init__(self, rate):
        self.rate = rate
        self.ssrc = self.last_seq = self.last_ts = None
        self.frames = []
        self.steps = []
        self.segment = 0
        self.media = 0.0
        self.counts = dict(sequence_discontinuities=0, duplicate_packets=0,
                           out_of_order_packets=0, source_changes=0,
                           timestamp_backward_changes=0, timestamp_forward_jumps=0,
                           timestamp_forward_wraps=0)
        self.largest_backward_ms = self.largest_forward_ms = 0.0
        self.sample_limit_reached = False

    def packet(self, seq, ts, ssrc, elapsed):
        if self.ssrc is not None and ssrc != self.ssrc:
            self.counts['source_changes'] += 1
            self.segment += 1
            self.last_seq = self.last_ts = None
            self.media = 0.0
        self.ssrc = ssrc
        if self.last_seq is not None:
            delta = signed_delta(seq,self.last_seq,16)
            if delta != 1:
                self.counts['sequence_discontinuities'] += 1
            if delta == 0:
                self.counts['duplicate_packets'] += 1
                return False
            if delta < 0:
                self.counts['out_of_order_packets'] += 1
                return False
        self.last_seq = seq
        if ts == self.last_ts:
            return False
        if len(self.frames) >= 4096:
            self.sample_limit_reached = True
            return False
        if self.last_ts is not None:
            delta = signed_delta(ts,self.last_ts) * 1000 / self.rate
            if delta < 0:
                self.counts['timestamp_backward_changes'] += 1
                self.largest_backward_ms = max(self.largest_backward_ms,-delta)
                self.segment += 1
                self.media = 0.0
            elif delta > 1000:
                self.counts['timestamp_forward_jumps'] += 1
                self.largest_forward_ms = max(self.largest_forward_ms,delta)
                self.segment += 1
                self.media = 0.0
            else:
                if ts < self.last_ts:
                    self.counts['timestamp_forward_wraps'] += 1
                self.steps.append(delta)
                self.media += delta / 1000
        self.last_ts = ts
        self.frames.append((self.segment,self.media,elapsed))
        return True

    def report(self):
        output = {**self.counts,'distinct_rtp_timestamps':len(self.frames),
                  'timestamp_backward_max_ms':round(self.largest_backward_ms,3),
                  'timestamp_forward_jump_max_ms':round(self.largest_forward_ms,3),
                  'sample_limit_reached':self.sample_limit_reached}
        clean = not any(self.counts[k] for k in ('source_changes','timestamp_backward_changes',
                'timestamp_forward_jumps','out_of_order_packets')) and not self.sample_limit_reached
        output['timestamp_progression_regular'] = bool(self.steps) and clean
        if self.steps:
            output.update(rtp_frame_step_ms_median=round(statistics.median(self.steps),3),
                          rtp_frame_step_ms_max=round(max(self.steps),3))
        if self.frames:
            output['first_video_packet_after_play_ms'] = round(self.frames[0][2]*1000,1)
        buffering = {'absolute_capture_age_verified':False,
                     'interpretation':'Relative change only; fixed upstream delay and replay are not excluded.'}
        if len(self.frames)>1:
            wall = self.frames[-1][2]-self.frames[0][2]
            output['maximum_timestamp_arrival_gap_ms'] = round(max(
                b[2]-a[2] for a,b in zip(self.frames,self.frames[1:]))*1000,2)
            output['observed_arrival_fps']=round((len(self.frames)-1)/wall,2) if wall>0 else None
            output['startup_frame_arrivals_first_2s']=sum(f[2]<2 for f in self.frames)
        if len(self.frames)>1 and clean and not self.counts['sequence_discontinuities']:
            first = self.frames[0]
            changes = [((f[2]-first[2])-(f[1]-first[1]))*1000 for f in self.frames]
            output['rtp_timeline_span_seconds']=round(self.frames[-1][1]-first[1],3)
            buffering.update(status='relative_timeline_available',
                relative_delay_change_end_ms=round(changes[-1],2),
                relative_catchup_max_ms=round(max(0,-min(changes)),2),
                relative_delay_growth_max_ms=round(max(0,max(changes)),2))
            startup = [v for f,v in zip(self.frames,changes) if f[2]<2]
            steady = [v for f,v in zip(self.frames,changes) if f[2]>=2]
            buffering['startup_relative_catchup_max_ms']=round(max(0,-min(startup)),2) if startup else None
            buffering['steady_relative_delay_range_ms']=round(max(steady)-min(steady),2) if steady else None
        else:
            output['rtp_timeline_span_seconds']=None
            buffering['status']='inconclusive_discontinuity_or_insufficient_samples'
        output['buffering']=buffering
        return output
