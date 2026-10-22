import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from starlette.requests import Request
from app import access1_admin as admin


USER = "a" * 32


def request(peer="172.30.32.2", user=USER, method="GET", headers=None):
    values = {"x-remote-user-id": user, "x-ingress-path": "/api/hassio_ingress/test"}
    values.update(headers or {})
    return Request({"type": "http", "method": method, "path": "/api/access1/pairing/admin",
                    "client": (peer, 1234),
                    "headers": [(k.encode(), v.encode()) for k, v in values.items()]})


class GuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_and_spoofed_forwarded_requests_never_call_ha(self):
        for peer in ("127.0.0.1", "172.30.33.4", "10.0.0.21"):
            with patch.object(admin, "administrator", new_callable=AsyncMock) as check:
                with self.assertRaises(HTTPException) as error:
                    await admin.require_pairing_admin(request(peer, headers={"x-forwarded-for": "172.30.32.2"}))
                self.assertEqual(error.exception.status_code, 403)
                check.assert_not_called()

    async def test_invalid_identity_and_missing_ingress_rejected(self):
        for value in (request(user=""), request(user="admin"),
                      request(headers={"x-ingress-path": ""})):
            with patch.object(admin, "administrator", new_callable=AsyncMock) as check:
                with self.assertRaises(HTTPException):
                    await admin.require_pairing_admin(value)
                check.assert_not_called()

    async def test_verified_admin_allowed_without_cache(self):
        with patch.object(admin, "administrator", new_callable=AsyncMock, return_value=True) as check:
            for _ in range(2):
                self.assertEqual(await admin.require_pairing_admin(request()), USER)
            self.assertEqual(check.await_count, 2)

    async def test_non_admin_denied(self):
        with patch.object(admin, "administrator", new_callable=AsyncMock, return_value=False):
            with self.assertRaises(HTTPException) as error:
                await admin.require_pairing_admin(request())
            self.assertEqual(error.exception.status_code, 403)

    async def test_outage_timeout_and_bad_response_fail_closed_without_details(self):
        for failure in (OSError("SECRET"), asyncio.TimeoutError(), ValueError("SECRET"),
                        admin.VerificationUnavailable("SECRET")):
            with patch.object(admin, "administrator", new_callable=AsyncMock, side_effect=failure):
                with self.assertRaises(HTTPException) as error:
                    await admin.require_pairing_admin(request())
                self.assertEqual(error.exception.status_code, 503)
                self.assertNotIn("SECRET", error.exception.detail)

    async def test_fixed_diagnostic_codes_do_not_expose_remote_responses(self):
        with patch.object(admin,"administrator",new_callable=AsyncMock,
                          side_effect=admin.VerificationUnavailable("user_list_unavailable")):
            with self.assertRaises(HTTPException) as error:
                await admin.require_pairing_admin(request())
            self.assertEqual(error.exception.status_code,503)
            self.assertIn("[user_list_unavailable]",error.exception.detail)

    async def test_mutations_require_explicit_json_header(self):
        for headers in ({}, {"content-type": "application/json"},
                        {"x-access1-pairing": "1", "content-type": "text/plain"}):
            with patch.object(admin, "administrator", new_callable=AsyncMock) as check:
                with self.assertRaises(HTTPException):
                    await admin.require_pairing_admin(request(method="POST", headers=headers))
                check.assert_not_called()
        with patch.object(admin, "administrator", new_callable=AsyncMock, return_value=True):
            self.assertEqual(await admin.require_pairing_admin(request(
                method="POST", headers={"x-access1-pairing": "1", "content-type": "application/json"})), USER)


class HaProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def lookup(self, users=None, messages=None):
        socket = AsyncMock()
        socket.recv.side_effect = [json.dumps(m) for m in (messages or [
            {"type": "auth_required"}, {"type": "auth_ok"},
            {"id": 1, "type": "result", "success": True, "result": users}])]
        context = AsyncMock()
        context.__aenter__.return_value = socket
        with patch.dict(admin.os.environ, {"SUPERVISOR_TOKEN": "fixture-secret"}), \
                patch.object(admin, "connect", return_value=context) as connect:
            result = await admin.administrator(USER)
        self.assertEqual(connect.call_args.args, ("ws://supervisor/core/websocket",))
        self.assertEqual(json.loads(socket.send.call_args_list[-1].args[0]),
                         {"id": 1, "type": "config/auth/list"})
        return result

    async def test_only_active_human_admin_or_owner(self):
        base = {"id": USER, "is_active": True, "system_generated": False, "group_ids": []}
        self.assertTrue(await self.lookup([{**base, "group_ids": ["system-admin"]}]))
        self.assertTrue(await self.lookup([{**base, "is_owner": True}]))
        for changes in ({}, {"group_ids": ["system-users"]}, {"is_owner": True, "is_active": False},
                        {"is_owner": True, "system_generated": True}, {"is_owner": True, "id": "b"*32}):
            self.assertFalse(await self.lookup([{**base, **changes}]))

    async def test_missing_token_rejected_before_connection(self):
        with patch.dict(admin.os.environ, {}, clear=True), patch.object(admin, "connect") as connect:
            with self.assertRaises(ValueError):
                await admin.administrator(USER)
            connect.assert_not_called()

    async def test_rejected_auth_or_ha_failure(self):
        for messages in (
            [{"type": "auth_invalid"}],
            [{"type": "auth_required"}, {"type": "auth_invalid"}],
            [{"type": "auth_required"}, {"type": "auth_ok"},
             {"id": 1, "type": "result", "success": False, "error": "denied"}],
        ):
            with self.assertRaises(ValueError):
                await self.lookup(messages=messages)


class RouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_pairing_check_is_guarded_and_never_starts_connector(self):
        from fastapi import FastAPI
        from types import SimpleNamespace
        from app.access1 import register_access1_routes
        app = FastAPI()
        # No start, present, or configuration methods available: authorization
        # verification must not touch any of them.
        register_access1_routes(app, SimpleNamespace(access1=object()), None, None)
        async def get():
            messages = []
            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}
            async def send(message):
                messages.append(message)
            scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                     "method": "GET", "scheme": "http", "path": "/api/access1/pairing/admin",
                     "raw_path": b"/api/access1/pairing/admin", "root_path": "",
                     "query_string": b"", "headers": [], "server": ("test", 80),
                     "client": ("127.0.0.1", 1000)}
            await app(scope, receive, send)
            start = next(m for m in messages if m["type"] == "http.response.start")
            body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
            return start, json.loads(body)
        start, _ = await get()
        self.assertEqual(start["status"], 403)
        app.dependency_overrides[admin.require_pairing_admin] = lambda: USER
        start, body = await get()
        self.assertEqual(start["status"], 200)
        self.assertEqual(body, {"administrator": True})
        self.assertEqual(dict(start["headers"])[b"cache-control"], b"no-store")


if __name__ == "__main__":
    unittest.main()
