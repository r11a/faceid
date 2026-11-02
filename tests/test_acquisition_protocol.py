"""Synthetic loopback RTSP server only. Never touches a camera or Access1."""
import json
import socket
import struct
import threading
import time
import unittest

from app.acquisition_probe import run


class Server:
    def __init__(self, redirect=False, deny=False, invalid_clock=False, foreign_clock=False, close_early=False):
        self.socket=socket.socket();self.socket.bind(('127.0.0.1',0));self.socket.listen(1)
        self.port=self.socket.getsockname()[1];self.methods=[];self.headers=[];self.error=None
        self.redirect=redirect;self.deny=deny
        self.invalid_clock=invalid_clock;self.foreign_clock=foreign_clock;self.close_early=close_early
        self.thread=threading.Thread(target=self.serve,daemon=True);self.thread.start()

    def serve(self):
        try:
            connection,_=self.socket.accept();connection.settimeout(5)
            with connection:
                buf=b''
                while True:
                    if not buf:buf+=connection.recv(65536)
                    if not buf:break
                    if buf.startswith(b'$'):
                        while len(buf)<4:buf+=connection.recv(65536)
                        length=struct.unpack('!H',buf[2:4])[0]+4
                        while len(buf)<length:buf+=connection.recv(65536)
                        buf=buf[length:];continue
                    while b'\r\n\r\n' not in buf:buf+=connection.recv(65536)
                    raw,buf=buf.split(b'\r\n\r\n',1)
                    head=raw.decode();method=head.split()[0];self.methods.append(method);self.headers.append(head)
                    body=b'';extra='';code='200 OK'
                    if method=='DESCRIBE':
                        if 'Authorization:' not in head or self.deny:
                            code='401 Unauthorized';extra='WWW-Authenticate: Digest realm="test", nonce="123", qop="auth"\r\n'
                        else:
                            track='rtsp://10.0.0.222:554/other' if self.redirect else 'trackID=0'
                            body=('v=0\r\nm=video 0 RTP/AVP 96\r\na=rtpmap:96 H265/90000\r\na=control:'+track+'\r\n').encode()
                            extra=f'Content-Base: rtsp://127.0.0.1:{self.port}/live/\r\n'
                    if method=='SETUP':extra='Session: synthetic\r\nTransport: RTP/AVP/TCP;unicast;interleaved=0-1\r\n'
                    connection.sendall((f'RTSP/1.0 {code}\r\n'+extra+f'Content-Length: {len(body)}\r\n\r\n').encode()+body)
                    if method=='PLAY':
                        now=time.time()+2208988800;sec=int(now);frac=int((now-sec)*2**32)
                        if self.invalid_clock:sec=frac=0
                        sr=struct.pack('!BBHIIIIII',0x80,200,6,123 if self.foreign_clock else 99,sec,frac,90000,2,10)
                        rtp1=struct.pack('!BBHII',0x80,96,1,90000,99)+b'frame'
                        rtp2=struct.pack('!BBHII',0x80,96,2,93600,99)+b'frame'
                        for channel,data in [(1,sr),(0,rtp1),(0,rtp2)]:
                            connection.sendall(b'$'+bytes([channel])+struct.pack('!H',len(data))+data)
                        if self.close_early:
                            connection.shutdown(socket.SHUT_WR)
                    if method=='TEARDOWN':break
        except (ConnectionError,socket.timeout):pass
        except Exception as error:self.error=error
        finally:self.socket.close()

    def finish(self):
        self.thread.join(6)
        if self.error:raise self.error


class ProtocolTests(unittest.TestCase):
    def test_invalid_sender_clock_is_not_an_age_measurement(self):
        server=Server(invalid_clock=True)
        result=run(f'rtsp://test:SECRET@127.0.0.1:{server.port}/live',seconds=.1)
        server.finish()
        self.assertEqual(result['invalid_clock_reports'],1)
        self.assertEqual(result['clock_evidence'],'inconsistent_or_invalid_clock_evidence')
        self.assertNotIn('sender_mapped_frame_age_ms',result)
        self.assertFalse(result['freshness_proven'])

    def test_unrelated_sender_source_cannot_supply_video_age(self):
        server=Server(foreign_clock=True)
        result=run(f'rtsp://test:SECRET@127.0.0.1:{server.port}/live',seconds=.1)
        server.finish()
        self.assertEqual(result['other_source_clock_reports'],1)
        self.assertEqual(result['clock_evidence'],'sender_mapping_without_matching_video')
        self.assertNotIn('sender_mapped_frame_age_ms',result)

    def test_early_close_keeps_partial_diagnostics_and_reports_failure(self):
        server=Server(close_early=True)
        result=run(f'rtsp://test:SECRET@127.0.0.1:{server.port}/live',seconds=.5)
        server.finish()
        self.assertEqual(result['error'],'stream_closed_early')
        self.assertEqual(result['distinct_rtp_timestamps'],2)
        self.assertFalse(result['freshness_proven'])
    def test_digest_packets_sender_report_feedback_and_teardown(self):
        server=Server()
        result=run(f'rtsp://test:SECRET@127.0.0.1:{server.port}/live',seconds=.1)
        server.finish()
        self.assertEqual(result['rtcp_sender_reports'],1)
        self.assertEqual(result['rtcp_receiver_reports_sent'],1)
        self.assertEqual(result['distinct_rtp_timestamps'],2)
        self.assertEqual(result['rtp_frame_step_ms_median'],40)
        self.assertEqual(result['sequence_discontinuities'],0)
        self.assertFalse(result['freshness_proven'])
        self.assertIn('TEARDOWN',server.methods)
        self.assertNotIn('SECRET',json.dumps(result))
        self.assertNotIn('SECRET',''.join(server.headers))

    def test_cross_host_track_is_rejected(self):
        server=Server(redirect=True)
        result=run(f'rtsp://test:SECRET@127.0.0.1:{server.port}/live',seconds=.1)
        server.finish()
        self.assertEqual(result['error'],'unexpected_track_host')
        self.assertNotIn('SETUP',server.methods)

    def test_failed_auth_does_not_setup_or_retry_forever(self):
        server=Server(deny=True)
        result=run(f'rtsp://test:SECRET@127.0.0.1:{server.port}/live',seconds=.1)
        server.finish()
        self.assertEqual(result['error'],'authentication_required_or_rejected')
        self.assertEqual(server.methods,['DESCRIBE','DESCRIBE'])


if __name__=='__main__':unittest.main()
