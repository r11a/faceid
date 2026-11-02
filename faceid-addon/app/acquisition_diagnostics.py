"""Opt-in administrator-only acquisition tests, isolated from recognition/access."""
import asyncio
import ipaddress
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit, unquote

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from .access1_admin import require_pairing_admin
from . import VERSION


def validate_direct(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 2048:
        raise ValueError('invalid_direct_url')
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError('invalid_direct_url')
    if any(ord(c) < 32 or ord(c) == 127 for c in unquote(value)):
        raise ValueError('invalid_direct_url')
    try:
        parsed = urlsplit(value)
        address = ipaddress.ip_address(parsed.hostname or '')
        # Restrict user-selected targets to private LAN IPv4, RTSP ports only.
        networks = ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')
        if (parsed.scheme != 'rtsp' or parsed.port not in (None, 554, 8554)
                or not any(address in ipaddress.ip_network(n) for n in networks)
                or parsed.fragment or not parsed.path or parsed.path == '/'):
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError('invalid_direct_url') from None
    return value


def probe_source(url, seconds=12):
    """Own process group, wall/CPU bounds; no raw stdout/stderr escapes."""
    worker = Path(__file__).with_name('acquisition_probe.py')
    process = subprocess.Popen([sys.executable, str(worker)], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               start_new_session=True)
    try:
        output, _ = process.communicate(json.dumps({'url': url, 'seconds': seconds}).encode(), timeout=seconds+17)
        if process.returncode != 0 or len(output) > 65536:
            return {'error': 'probe_failed', 'freshness_proven': False}
        result = json.loads(output)
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        return {'error': 'probe_deadline', 'freshness_proven': False}
    except (ValueError, OSError):
        return {'error': 'probe_failed', 'freshness_proven': False}
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()


class AcquisitionDiagnostics:
    def __init__(self, probe=probe_source, decode=None, compare=None, preview=None, clear_preview=None):
        self.probe = probe
        self.decode = decode
        self.compare=compare;self.preview=preview;self.clear_preview=clear_preview
        self.lock = threading.Lock()
        self.state = {'running': False, 'results': []}
        self.owner = None
        self.next_start = 0.0

    def start(self, owner, sources, camera=None, region=None):
        with self.lock:
            if self.state['running'] or time.monotonic() < self.next_start:
                raise HTTPException(409, 'A diagnostic is running or cooling down; try again shortly')
            self.owner = owner
            if self.clear_preview:self.clear_preview()
            self.state = {'running': True, 'current': None, 'results': [],
                          'camera': camera, 'access1_submission': 'not_implemented', 'frames_saved': 0}
            self.region=region
        threading.Thread(target=self._run, args=(sources,), daemon=True,
                         name='acquisition-diagnostic').start()

    def _run(self, sources):
        try:
            for source, url in sources:
                with self.lock:
                    self.state['current'] = source
                try:
                    if source == 'decode_chain':
                        result = self.decode(self.state.get('camera'),url) if self.decode and url else {'error':'decode_unavailable'}
                    elif source in ('preview','crop_compare'):
                        callback=self.preview if source=='preview' else self.compare
                        result=callback(self.state.get('camera'),url,self.region,self.owner) if callback and url else {'error':'decode_unavailable'}
                    else:
                        result = (self.probe(url, seconds=30) if source == 'restream_clock'
                              else self.probe(url)) if url else {'error': 'restream_not_configured'}
                except Exception:
                    result = {'error': 'probe_failed'}
                with self.lock:
                    self.state['results'].append({'source': source, 'camera': self.state.get('camera'),
                                                  **result, 'freshness_proven': False})
        finally:
            sources.clear()  # Release per-run credentials; never persist or report them.
            with self.lock:
                self.state.update(running=False, current=None)
                self.next_start = time.monotonic() + 30

    def status(self, owner):
        with self.lock:
            if self.owner and owner != self.owner:
                raise HTTPException(403, 'Diagnostic belongs to another administrator')
            return json.loads(json.dumps(self.state))


def register_acquisition_routes(app, processor, static_dir):
    from .decode_diagnostics import run_decode, run_preview, blockers
    from .crop_diagnostics import PreviewCache,validate_region
    previews=PreviewCache()
    diagnostic = AcquisitionDiagnostics(decode=lambda camera,url:run_decode(processor,camera,url),
        preview=lambda camera,url,region,owner:run_preview(processor,camera,url,region,lambda images:previews.put(owner,images)),
        compare=lambda camera,url,region,owner:run_decode(processor,camera,url,region=region,
            preview=lambda images:previews.put(owner,images),benchmarks=True),clear_preview=previews.clear)
    discovery = {'until': 0.0, 'names': []}

    def cameras():
        profiles = getattr(processor, 'camera_profiles', None)
        names = set(profiles.all() if profiles else ()) | set(getattr(processor, 'cameras', ()) or ())
        if not names:
            if time.monotonic() >= discovery['until']:
                discovery.update(until=time.monotonic() + 60, names=[])
                response = None
                try:
                    response = processor.frigate.request('GET', '/api/config', timeout=2)
                    if response.status_code == 200:
                        discovery['names'] = [n for n in response.json().get('cameras', {})
                                              if isinstance(n, str) and n]
                except Exception:
                    pass
                finally:
                    if response is not None:
                        response.close()
            names.update(discovery['names'])
        return sorted(names)

    @app.get('/acquisition-diagnostics')
    async def diagnostic_page(request: Request):
        await require_pairing_admin(request)
        return HTMLResponse((static_dir / 'acquisition-diagnostics.html').read_text(encoding='utf8'),
                            headers={'Cache-Control': 'no-store'})

    @app.get('/api/acquisition-diagnostics')
    async def diagnostic_status(request: Request):
        owner = await require_pairing_admin(request)
        return {**diagnostic.status(owner), 'build': VERSION, 'cameras': await asyncio.to_thread(cameras),
                'preview':previews.status(owner),
                'operational_sessions':processor.stream_observations.report() if hasattr(processor,'stream_observations') else {},
                'diagnostic_only_runtime':getattr(processor,'diagnostic_only',False) is True}

    @app.get('/api/acquisition-diagnostics/preview/{kind}')
    async def diagnostic_preview(request:Request,kind:str):
        owner=await require_pairing_admin(request)
        return Response(previews.get(owner,kind),media_type='image/jpeg',
                        headers={'Cache-Control':'private, no-store','Pragma':'no-cache',
                                 'X-Content-Type-Options':'nosniff','Cross-Origin-Resource-Policy':'same-origin'})

    @app.get('/api/acquisition-diagnostics/operational-preview/{event_id}/{kind}')
    async def operational_preview(request:Request,event_id:str,kind:str):
        await require_pairing_admin(request)
        manager=getattr(processor,'stream_observations',None)
        content=manager.preview(event_id,kind) if manager else None
        if content is None:raise HTTPException(404,'Preview unavailable or expired')
        return Response(content,media_type='image/jpeg',headers={'Cache-Control':'private, no-store',
            'Pragma':'no-cache','X-Content-Type-Options':'nosniff','Cross-Origin-Resource-Policy':'same-origin'})

    @app.post('/api/acquisition-diagnostics')
    async def diagnostic_start(request: Request):
        owner = await require_pairing_admin(request)
        # Avoid framework validation errors echoing a secret URL into responses.
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 4096:
                raise HTTPException(400, 'Invalid diagnostic request')
        try:
            body = json.loads(raw)
            if not isinstance(body, dict) or set(body) - {'camera', 'source', 'direct_url','region'}:
                raise ValueError()
            camera, source = body.get('camera'), body.get('source')
            if camera not in await asyncio.to_thread(cameras) or source not in ('restream', 'restream_clock', 'direct', 'both', 'decode_chain','preview','crop_compare'):
                raise ValueError()
            region=validate_region(body.get('region',[.25,.1,.75,.9])) if source in ('preview','crop_compare') else None
            if 'region' in body and region is None:raise ValueError()
            direct = validate_direct(body.get('direct_url')) if source in ('direct', 'both') else None
        except (ValueError, TypeError):
            raise HTTPException(400, 'Choose a configured camera and valid private-LAN RTSP URL') from None
        sources = []
        if source in ('decode_chain','preview','crop_compare'):
            reasons = blockers(processor,camera)
            if reasons:
                raise HTTPException(409, 'Decode test blocked: '+', '.join(reasons))
        if source in ('restream', 'restream_clock', 'both', 'decode_chain','preview','crop_compare'):
            url = await asyncio.to_thread(processor.frigate._live_url, camera)
            sources.append((source if source in ('restream_clock','decode_chain','preview','crop_compare') else 'restream', url))
        if direct:
            sources.append(('direct', direct))
        diagnostic.start(owner, sources, camera=camera,**({'region':region} if region is not None else {}))
        return {'started': True, 'access1_submission': 'not_implemented'}
