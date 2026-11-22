"""Fail-closed HA administrator verification for pairing only.

Never use FaceID's optional UI roles or caller-supplied forwarded IP headers as
proof of administrator identity. No credentials or HA user lists are logged.
"""
import asyncio
import json
import os
import re
import logging

from fastapi import HTTPException, Request
from websockets.legacy.client import connect

log = logging.getLogger("faceid.access1.pairing")


class VerificationUnavailable(ValueError):
    """Only a fixed diagnostic code, never a remote response or credential."""


async def administrator(user_id):
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not token:
        raise VerificationUnavailable("token_missing")
    # Fixed local endpoint: callers cannot redirect the Supervisor credential.
    async with connect(
        "ws://supervisor/core/websocket", open_timeout=3, close_timeout=1,
        max_size=262144, max_queue=4,
    ) as socket:
        if json.loads(await socket.recv()).get("type") != "auth_required":
            raise VerificationUnavailable("handshake_invalid")
        await socket.send(json.dumps({"type": "auth", "access_token": token}))
        if json.loads(await socket.recv()).get("type") != "auth_ok":
            raise VerificationUnavailable("ha_api_auth_rejected")
        await socket.send(json.dumps({"id": 1, "type": "config/auth/list"}))
        message = json.loads(await socket.recv())
        if (message.get("id") != 1 or message.get("type") != "result"
                or message.get("success") is not True
                or not isinstance(message.get("result"), list)):
            raise VerificationUnavailable("user_list_unavailable")
        for user in message["result"]:
            if user.get("id") == user_id:
                return (user.get("is_active") is True
                        and user.get("system_generated") is False
                        and (user.get("is_owner") is True
                             or "system-admin" in user.get("group_ids", [])))
        return False


async def require_pairing_admin(request: Request):
    return await require_admin(request, "x-access1-pairing", "Access1 pairing")


async def require_setup_admin(request: Request):
    """6.9.0 setup endpoints: same verified HA administrator, own confirm header."""
    return await require_admin(request, "x-faceid-setup", "Setup")


async def require_admin(request: Request, confirm_header: str, purpose: str):
    # HA documents this exact Ingress source. Do not broaden to the addon subnet.
    # Pairing is deliberately unavailable through the optional public UI port.
    if not request.client or request.client.host != "172.30.32.2":
        raise HTTPException(403, "Open FaceID through the Home Assistant sidebar")
    user_id = request.headers.get("x-remote-user-id", "")
    ingress = request.headers.get("x-ingress-path", "")
    if (not re.fullmatch(r"[a-f0-9]{32}", user_id)
            or not ingress.startswith("/api/hassio_ingress/")):
        raise HTTPException(403, "Verified Home Assistant identity required")
    if request.method not in ("GET", "HEAD"):
        # Cross-origin forms cannot supply this header. No CORS permission is
        # provided by the module. Pairing changes are JSON-only.
        if (request.headers.get(confirm_header) != "1"
                or request.headers.get("content-type", "").split(";")[0] != "application/json"):
            raise HTTPException(403, f"{purpose} confirmation required")
    try:
        allowed = await asyncio.wait_for(administrator(user_id), timeout=5)
    except Exception as error:
        reason=str(error) if isinstance(error,VerificationUnavailable) and str(error) in {
            "token_missing","handshake_invalid","ha_api_auth_rejected","user_list_unavailable"
        } else (
            "timeout" if isinstance(error,asyncio.TimeoutError) else "transport_unavailable")
        log.warning("%s administrator check failed: %s; %s disabled",purpose,reason,purpose.lower())
        raise HTTPException(503, f"HA administrator verification unavailable; {purpose.lower()} disabled ["+reason+"]") from None
    if not allowed:
        raise HTTPException(403, "Home Assistant administrator required")
    return user_id
