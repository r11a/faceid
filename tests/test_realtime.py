import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.audit import AuditStore


class RealtimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_commits_wake_stream_but_reads_and_rollbacks_do_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = AuditStore(Path(tmp) / 'audit.db')
            stream = audit.revisions.stream(heartbeat=0.02)
            initial = await anext(stream)
            self.assertIn('event: revision', initial)
            before = audit.revisions.revision
            audit.recent()
            self.assertEqual(audit.revisions.revision, before)
            with self.assertRaises(RuntimeError):
                with audit._connection() as con:
                    con.execute("INSERT INTO events(event_id,camera,created_ts,updated_ts) VALUES('rollback','door',1,1)")
                    raise RuntimeError('rollback')
            self.assertEqual(audit.revisions.revision, before)
            self.assertEqual(await anext(stream), ': heartbeat\n\n')
            audit.start_event('one', 'door', 1)
            audit.start_event('two', 'door', 2)
            update = await anext(stream)
            self.assertIn(f'data: {before + 2}\n', update)
            self.assertEqual(await anext(stream), ': heartbeat\n\n')
            await stream.aclose()

    async def test_worker_thread_wakes_waiting_stream_and_cancel_unsubscribes(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = AuditStore(Path(tmp) / 'audit.db')
            stream = audit.revisions.stream()
            await anext(stream)
            waiting = asyncio.create_task(anext(stream))
            await asyncio.to_thread(audit.start_event, 'thread', 'door', 1)
            self.assertIn('event: revision', await asyncio.wait_for(waiting, 1))
            waiting = asyncio.create_task(anext(stream))
            await asyncio.sleep(0)
            waiting.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiting
            self.assertEqual(audit.revisions.subscriber_count, 0)


class RealtimeRouteTests(unittest.IsolatedAsyncioTestCase):
    def test_updates_route_uses_existing_auth_middleware(self):
        from fastapi.testclient import TestClient
        from app.gallery import Gallery
        from app.webui import build_app

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = build_app(
                {'faceid': {'auth': {'user': 'reviewer', 'password': 'secret'}}, 'frigate': {'url': ''}},
                SimpleNamespace(), Gallery(root), SimpleNamespace(audit=None), root, root,
            )
            with TestClient(app) as client:
                self.assertEqual(client.get('/api/updates').status_code, 401)
                self.assertEqual(client.get('/api/updates', auth=('reviewer', 'secret')).status_code, 503)
    async def test_commit_before_subscribe_is_in_initial_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = AuditStore(Path(tmp) / 'audit.db')
            audit.start_event('one', 'door', 1)
            stream = audit.revisions.stream()
            self.assertIn(f'data: {audit.revisions.revision}\n', await anext(stream))
            await stream.aclose()
