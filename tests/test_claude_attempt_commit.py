"""6.8.7: the Access1 attempt row commits without a disk flush; lock and commit are timed."""
from contextlib import contextmanager
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.audit import AuditStore
import tests.test_access1 as base


class Recording(sqlite3.Connection):
    statements = []

    def execute(self, sql, *args):
        if sql.startswith('PRAGMA synchronous'):
            Recording.statements.append((sql, None))
        else:
            Recording.statements.append((sql.split()[0] + ' ' + sql.split()[1], self.execute_raw('PRAGMA synchronous')))
        return super().execute(sql, *args)

    def execute_raw(self, sql):
        return super().execute(sql).fetchone()[0]


class RecordingAudit(base.Audit):
    @contextmanager
    def _connection(self):
        con = sqlite3.connect(self.path, factory=Recording)
        try:
            with con:
                yield con
        finally:
            con.close()


class AttemptCommitTests(unittest.TestCase):
    setUp = base.Access1Tests.setUp
    event = base.Access1Tests.event
    ready = base.Access1Tests.ready

    def use_recording_audit(self):
        self.integration.audit = RecordingAudit(self.audit.path)
        Recording.statements = []

    def test_attempt_row_commits_with_normal_sync_inside_an_immediate_transaction(self):
        self.ready()
        self.use_recording_audit()
        self.assertEqual(self.integration.present(self.event())['state'], 'accepted')
        sql = [s for s, _ in Recording.statements]
        insert = sql.index('INSERT INTO')
        self.assertLess(sql.index('PRAGMA synchronous=NORMAL'), sql.index('BEGIN IMMEDIATE'))
        self.assertLess(sql.index('BEGIN IMMEDIATE'), insert)
        self.assertEqual(Recording.statements[insert][1], 1)  # NORMAL while inserting
        with sqlite3.connect(self.audit.path) as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM access1_attempts').fetchone()[0], 1)

    def test_duplicate_is_still_refused_and_never_posted_twice(self):
        self.ready()
        self.integration.present(self.event())
        self.assertEqual(self.integration.present(self.event())['state'], 'duplicate')
        self.assertEqual([c.args[0] for c in self.integration.transport.call.call_args_list], ['POST'])

    def test_lock_wait_and_commit_are_timed(self):
        self.ready()
        stages = []
        self.integration._timing = lambda event, stage, started: stages.append(stage)
        self.integration.present(self.event())
        order = ['access1_pre_insert', 'access1_attempt_lock', 'access1_attempt_commit',
                 'access1_attempt_insert', 'access1_pre_post']
        self.assertEqual([s for s in stages if s in order], order)

    def test_other_audit_connections_keep_full_sync(self):
        with tempfile.TemporaryDirectory() as temp:
            store = AuditStore(Path(temp) / 'audit.db')
            with store._connection() as con:
                self.assertEqual(con.execute('PRAGMA synchronous').fetchone()[0], 2)


if __name__ == '__main__':
    unittest.main()
