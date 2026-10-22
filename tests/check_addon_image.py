"""Offline smoke check of the actual add-on image; run only inside Docker."""
import json
from pathlib import Path
import re
import ssl
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import patch

root = Path('/opt/faceid')
sys.path.insert(0, str(root))
import app.main
import app.webui
import app.mqtt_listener
from app.access1_setup import prepare_certificate
from websockets.legacy.client import connect

assert Path(app.main.__file__).resolve().is_relative_to(root)
index = (root / 'static/index.html').read_text()
assets = re.findall(r'(?:src|href)=["\'](?:\./)?(assets/[^"\']+)["\']', index)
assert assets and all((root / 'static' / asset).is_file() for asset in assets)
subprocess.run(['bash', '-n', '/run.sh'], check=True)
assert Path('/run.sh').read_text().startswith('#!/usr/bin/with-contenv bashio\n')
assert ssl.VERIFY_X509_PARTIAL_CHAIN
with tempfile.TemporaryDirectory(prefix='faceid-image-') as temporary:
    integration = SimpleNamespace(path=Path(temporary) / 'access1.json', _lock=threading.RLock())
    with patch('app.access1_setup.addon_hostname', return_value='local-faceid-ci'):
        prepared = prepare_certificate(integration)
        key = Path(prepared['tls_key'])
        original = key.read_bytes()
        assert key.stat().st_mode & 0o777 == 0o600
        assert prepare_certificate(integration)['certificate'] == prepared['certificate']
        assert key.read_bytes() == original
        assert 'PRIVATE KEY' not in prepared['certificate']
print('Packaged application imports, static assets, startup shell and certificate creation/reuse: PASS')
