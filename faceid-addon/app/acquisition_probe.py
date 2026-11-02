"""Bounded RTSP video timing probe. URL on stdin; never log URL, SDP or credentials.
No app imports, HTTP access, image storage, inference or Access1 submission.
"""
import hashlib
import json
import re
import socket
import statistics
import struct
import sys
import time
import secrets
from email.utils import parsedate_to_datetime
try:
    from .acquisition_metrics import Timeline, signed_delta, receiver_feedback
except ImportError:
    from acquisition_metrics import Timeline, signed_delta, receiver_feedback
from urllib.parse import unquote, urlsplit, urlunsplit, urljoin


def run(secret_url, seconds=12):
    u = urlsplit(secret_url)
    if u.scheme != 'rtsp' or not u.hostname:
        return {'error': 'unexpected_target'}
    user, password = unquote(u.username or ''), unquote(u.password or '')
    uri = urlunsplit(('rtsp', f'[{u.hostname}]:{u.port or 554}' if ':' in u.hostname else f'{u.hostname}:{u.port or 554}', u.path, '', ''))
    sock = socket.create_connection((u.hostname, u.port or 554), timeout=4)
    sock.settimeout(2)
    buf = b''
    auth = None
    session = None
    cseq = 0
    nc = 0
    report = {'diagnostic_schema': 2, 'mode': 'packet_only', 'duration_limit_seconds': seconds,
              'frames_saved': 0, 'access1_submission': 'not_implemented'}

    def read_message(deadline):
        nonlocal buf
        while time.monotonic() < deadline:
            if buf.startswith(b'$') and len(buf) >= 4:
                size = struct.unpack('!H', buf[2:4])[0]
                if len(buf) >= size + 4:
                    channel, data = buf[1], buf[4:size+4]
                    buf = buf[size+4:]
                    return ('packet', channel, data)
            elif b'\r\n\r\n' in buf:
                head, rest = buf.split(b'\r\n\r\n', 1)
                lines = head.decode('latin1').split('\r\n')
                headers = {k.lower(): v.strip() for line in lines[1:] if ':' in line
                           for k, v in [line.split(':', 1)]}
                size = int(headers.get('content-length', '0'))
                if len(rest) >= size:
                    buf = rest[size:]
                    return ('response', int(lines[0].split()[1]), headers, rest[:size])
            try:
                sock.settimeout(min(2,max(.001,deadline-time.monotonic())))
                data = sock.recv(65536)
            except socket.timeout:
                continue
            if not data:
                raise EOFError()
            buf += data
            if len(buf) > 2_000_000:
                raise ValueError('buffer_limit')
        raise TimeoutError()

    def request(method, target, extra=None):
        nonlocal auth, cseq, session, nc
        for attempt in range(2):
            cseq += 1
            headers = {'CSeq': str(cseq), 'User-Agent': 'Access1-ReadOnly-Timing-Probe'}
            if session:
                headers['Session'] = session
            if extra:
                headers.update(extra)
            if auth:
                if auth[0] == 'basic':
                    import base64
                    headers['Authorization'] = 'Basic ' + base64.b64encode(f'{user}:{password}'.encode()).decode()
                else:
                    a = auth[1]
                    if a.get('algorithm', 'MD5').upper() != 'MD5':
                        raise ValueError('unsupported_auth_algorithm')
                    md5 = lambda x: hashlib.md5(x.encode()).hexdigest()
                    ha1, ha2 = md5(f'{user}:{a["realm"]}:{password}'), md5(f'{method}:{target}')
                    fields = f'username="{user}", realm="{a["realm"]}", nonce="{a["nonce"]}", uri="{target}"'
                    if 'qop' in a:
                        if 'auth' not in a['qop'].split(','):
                            raise ValueError('unsupported_auth_qop')
                        nc += 1
                        count, cnonce = f'{nc:08x}', 'readOnlyTimingProbe'
                        response = md5(f'{ha1}:{a["nonce"]}:{count}:{cnonce}:auth:{ha2}')
                        fields += f', qop=auth, nc={count}, cnonce="{cnonce}"'
                    else:
                        response = md5(f'{ha1}:{a["nonce"]}:{ha2}')
                    fields += f', response="{response}"'
                    if 'opaque' in a:
                        fields += f', opaque="{a["opaque"]}"'
                    headers['Authorization'] = 'Digest ' + fields
            data = f'{method} {target} RTSP/1.0\r\n' + ''.join(f'{k}: {v}\r\n' for k,v in headers.items()) + '\r\n'
            sock.sendall(data.encode())
            deadline = time.monotonic() + 4
            while True:
                answer = read_message(deadline)
                if answer[0] == 'response':
                    break
            _, code, rh, body = answer
            if code == 401 and attempt == 0:
                challenge = rh.get('www-authenticate', '')
                if challenge.lower().startswith('digest '):
                    pairs = re.findall(r'(\w+)=(?:"([^"]*)"|([^,\s]+))', challenge[7:])
                    auth = ('digest', {k: q or plain for k,q,plain in pairs})
                elif challenge.lower().startswith('basic '):
                    auth = ('basic', {})
                else:
                    return code, rh, body
                continue
            return code, rh, body

    try:
        started = time.monotonic()
        code, headers, sdp_bytes = request('DESCRIBE', uri, {'Accept': 'application/sdp'})
        report['describe_status'] = code
        if code != 200:
            report['error'] = 'authentication_required_or_rejected' if code == 401 else 'describe_rejected'
        if code != 200:
            return report
        report['rtsp_server_date_present'] = 'date' in headers
        report['rtsp_server_date_is_capture_time'] = False
        if 'date' in headers:
            try:
                date = parsedate_to_datetime(headers['date'])
                if date.tzinfo is not None:
                    report['rtsp_server_date_offset_ms'] = round((time.time()-date.timestamp())*1000,1)
            except (ValueError, TypeError, OverflowError):
                report['rtsp_server_date_invalid'] = True
        sdp = sdp_bytes.decode('utf8', errors='replace')
        video = None
        for section in sdp.split('\nm='):
            if section.startswith('video ') or section.startswith('m=video '):
                video = section
                break
        if video is None:
            report['error'] = 'no_video_track'
            return report
        control = re.search(r'^a=control:(.+)', video, re.M)
        rtpmap = re.search(r'^a=rtpmap:(\d+) ([^/\s]+)/(\d+)', video, re.M)
        if not control or not rtpmap:
            report['error'] = 'missing_video_timing_description'
            return report
        rate = int(rtpmap[3])
        if not 1 <= rate <= 1_000_000:
            report['error'] = 'invalid_rtp_clock_rate'
            return report
        report['sdp_clock_reference_present'] = 'a=ts-refclk:' in sdp
        report['sdp_media_clock_present'] = 'a=mediaclk:' in sdp
        report.update(codec=rtpmap[2], rtp_clock_hz=rate)
        base = headers.get('content-base', uri + '/')
        track = urljoin(base, control[1].strip())
        # A remote SDP cannot redirect this diagnostic to another host.
        if (urlsplit(track).hostname != u.hostname or (urlsplit(track).port or 554) != (u.port or 554) or urlsplit(track).scheme != 'rtsp'):
            report['error'] = 'unexpected_track_host'
            return report
        code, headers, _ = request('SETUP', track, {'Transport':'RTP/AVP/TCP;unicast;interleaved=0-1'})
        report['setup_status'] = code
        if code != 200:
            report['error'] = 'setup_rejected'
        if code != 200:
            return report
        session = headers.get('session','').split(';')[0]
        code, _, _ = request('PLAY', uri)
        report['play_status'] = code
        if code != 200:
            report['error'] = 'play_rejected'
        if code != 200:
            return report
        report['connection_setup_ms'] = round((time.monotonic()-started)*1000,1)
        transport = headers.get('transport','')
        channels = re.search(r'interleaved=(\d+)-(\d+)', transport)
        video_channel = int(channels[1]) if channels else 0
        rtcp_channel = int(channels[2]) if channels else 1
        begin = time.monotonic()
        deadline = begin + seconds
        timeline = Timeline(rate)
        received, sender_reports, ages = 0, [], []
        sender_report_count = 0
        last_video_elapsed = None
        mapping = None
        invalid_reports = foreign_reports = clock_changes = 0
        clock_rate_errors = []
        previous_clock = None
        wall_origin = time.time()
        receiver_ssrc = secrets.randbits(32)
        receiver_reports = 0
        next_report = begin
        last_sr = None
        while time.monotonic() < deadline:
            try:
                msg = read_message(deadline)
            except TimeoutError:
                break
            except EOFError:
                report['error'] = 'stream_closed_early'
                break
            if msg[0] != 'packet':
                continue
            _, channel, packet = msg
            now = time.time()
            elapsed = time.monotonic()-begin
            if abs((now-wall_origin)-elapsed) > .25:
                clock_changes += 1
                wall_origin = now-elapsed
                mapping = None
            if channel == video_channel and len(packet) >= 12 and packet[0] >> 6 == 2 and packet[1] & 127 == int(rtpmap[1]):
                seq, ts, ssrc = struct.unpack('!HII', packet[2:12])
                received += 1
                last_video_elapsed = elapsed
                if time.monotonic() >= next_report:
                    rr = receiver_feedback(receiver_ssrc)
                    sock.sendall(b'$' + bytes([rtcp_channel]) + struct.pack('!H',len(rr)) + rr)
                    receiver_reports += 1
                    next_report = time.monotonic() + 4
                segment = timeline.segment
                new_frame = timeline.packet(seq, ts, ssrc, elapsed)
                if timeline.segment != segment:
                    mapping = None
                    previous_clock = None
                if mapping and mapping[0] != ssrc:
                    foreign_reports += 1
                    mapping = None
                    previous_clock = None
                if new_frame and mapping and mapping[0] == ssrc:
                    delta = signed_delta(ts,mapping[2])
                    ages.append((now-(mapping[1]+delta/rate))*1000)
            elif channel == rtcp_channel:
                pos = 0
                while pos+4 <= len(packet):
                    size = (struct.unpack('!H',packet[pos+2:pos+4])[0]+1)*4
                    if size < 4 or pos+size > len(packet) or packet[pos] >> 6 != 2:
                        invalid_reports += 1
                        break
                    if packet[pos+1] == 200:
                        sender_report_count += 1
                        if size < 28:
                            invalid_reports += 1
                            break
                        ssrc, sec, frac, ts = struct.unpack('!IIII',packet[pos+4:pos+20])
                        ntp = sec-2208988800+frac/2**32
                        if not 1577836800 <= ntp <= 4102444800:
                            invalid_reports += 1
                        elif timeline.ssrc is not None and ssrc != timeline.ssrc:
                            foreign_reports += 1
                        else:
                            mapping = (ssrc,ntp,ts)
                            if previous_clock and previous_clock[0] == ssrc:
                                advance = ntp-previous_clock[1]
                                rtp_advance = signed_delta(ts,previous_clock[2])/rate
                                if advance > 0 and rtp_advance >= 0:
                                    if len(clock_rate_errors) < 4096:
                                        clock_rate_errors.append((rtp_advance/advance-1)*1_000_000)
                                else:
                                    invalid_reports += 1
                                    mapping = None
                            previous_clock = (ssrc,ntp,ts)
                            last_sr = elapsed
                            if len(sender_reports) < 10:
                                sender_reports.append({'elapsed_seconds':round(elapsed,3),
                                    'sender_clock_vs_receive_ms':round((now-ntp)*1000,1)})
                    pos += size
        report.update(timeline.report())
        report.update(observed_seconds=round(time.monotonic()-begin,2), video_packets=received,
                      rtcp_sender_reports=sender_report_count, rtcp_receiver_reports_sent=receiver_reports,
                      rtcp_feedback='compound_rr_sdes', invalid_clock_reports=invalid_reports,
                      other_source_clock_reports=foreign_reports, receiver_clock_discontinuities=clock_changes)
        report['video_tail_silence_seconds'] = (round(time.monotonic()-begin-last_video_elapsed,3)
                                               if last_video_elapsed is not None else None)
        if sender_reports:
            report['sender_clock_samples'] = sender_reports[:10]
            report['last_sender_report_elapsed_seconds'] = round(last_sr,3)
        report['clock_evidence'] = ('sender_mapping_observed_not_validated' if ages else
                                   'sender_mapping_without_matching_video' if sender_reports else
                                   'no_sender_mapping_observed')
        if invalid_reports or clock_changes:
            report['clock_evidence'] = 'inconsistent_or_invalid_clock_evidence'
        if clock_rate_errors:
            report['sender_mapping_rate_error_ppm_max_abs'] = round(max(abs(v) for v in clock_rate_errors),2)
        if ages:
            report['sender_mapped_frame_age_ms'] = {'min':round(min(ages),1),'median':round(statistics.median(ages),1),'max':round(max(ages),1)}
        if not received:
            report['error'] = 'no_video_within_window'
        report['freshness_proven'] = False
        report['note'] = 'Transport timestamps alone do not prove sensor capture age or authenticated media integrity.'
        return report
    finally:
        if session:
            try:
                request('TEARDOWN',uri)
            except Exception:
                pass
        sock.close()


if __name__ == '__main__':
    try:
        import os
        import resource
        os.nice(10)
        resource.setrlimit(resource.RLIMIT_CPU, (8, 8))
        resource.setrlimit(resource.RLIMIT_AS, (256*1024*1024, 256*1024*1024))
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
        value = json.loads(sys.stdin.read(4096))
        seconds = value.get('seconds',12)
        if type(seconds) is not int or seconds not in (12,30):
            raise ValueError('invalid_duration')
        result = run(value['url'], seconds=seconds)
    except Exception as exc:
        categories = {socket.timeout: 'connection_timeout', TimeoutError: 'probe_timeout',
                      ConnectionRefusedError: 'connection_refused', socket.gaierror: 'dns_failed',
                      EOFError: 'stream_closed'}
        result = {'error': categories.get(type(exc), 'probe_failed'), 'freshness_proven': False}
    print(json.dumps(result))
