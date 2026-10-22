"""Public TLS material and local certificate preparation for pairing."""
from pathlib import Path
import re
import ssl
import os
import http.client
import json
import subprocess
import tempfile
from fastapi import HTTPException


def public_certificate(path):
    path=Path(path)
    if not path.is_file() or path.stat().st_size>16384:
        return ""
    value=path.read_text(encoding="ascii").strip()
    blocks=re.findall(r"-----BEGIN CERTIFICATE-----\s+[A-Za-z0-9+/=\s]+-----END CERTIFICATE-----",value)
    if not blocks or "".join(value.split())!="".join("".join(blocks).split()):
        return ""
    for block in blocks:ssl.PEM_cert_to_DER_cert(block)
    return "\n".join(blocks)+"\n"


def prepare_certificate(integration):
    """Explicit admin action: local TLS files only, no credential/config writes."""
    host = addon_hostname()
    if not host:
        raise HTTPException(503, "Internal addon hostname unavailable")
    root = integration.path.parent
    target = root / "access1-wizard-tls"
    with integration._lock:
        try:
            if not target.exists():
                with tempfile.TemporaryDirectory(prefix="tls-prepare-", dir=root) as temporary:
                    folder = Path(temporary)
                    os.chmod(folder, 0o700)
                    subprocess.run(
                        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                         "-days", "365", "-subj", "/CN=" + host,
                         "-addext", "subjectAltName=DNS:" + host,
                         "-keyout", str(folder/"directory-key.pem"),
                         "-out", str(folder/"directory.pem")],
                        check=True, timeout=15, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    for filename in ("directory.pem","directory-key.pem"):
                        os.chmod(folder/filename, 0o600)
                    # Never replace an existing setup or rotate trust implicitly.
                    folder.rename(target)
            certificate=target/"directory.pem";key=target/"directory-key.pem"
            context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(str(certificate),str(key))
            public=public_certificate(certificate)
            if not public:raise ValueError("Public certificate required")
            for check in (["-checkhost",host],["-checkend","0"]):
                subprocess.run(["openssl","x509","-in",str(certificate),"-noout",*check],
                               check=True,timeout=3,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            return {"bind_address":host,"tls_cert":str(certificate),"tls_key":str(key),
                    "certificate":public}
        except Exception:
            raise HTTPException(503, "Certificate preparation failed; existing files were not overwritten") from None


def addon_hostname():
    """Read only our own addon identity; never return Supervisor options."""
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not token:
        return ""
    connection = http.client.HTTPConnection("supervisor", timeout=3)
    try:
        connection.request("GET", "/addons/self/info", headers={"Authorization": "Bearer " + token})
        response = connection.getresponse()
        if response.status != 200:
            return ""
        raw = response.read(131073)
        if len(raw) > 131072:
            return ""
        host = json.loads(raw).get("data", {}).get("hostname", "")
        return host if isinstance(host,str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}",host) else ""
    except Exception:
        return ""
    finally:
        connection.close()
