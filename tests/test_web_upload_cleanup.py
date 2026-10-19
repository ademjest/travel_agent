from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from infrastructure.memory_store import MemoryStore
from infrastructure.web_repository import WebRepository
from services.conversation_deletion import ConversationDeletionService


class WebUploadCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = MemoryStore(self.root / 'test.db')
        self.repo = WebRepository(self.store)
        self.identity = self.repo.create_conversation()['id']
        self.old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()

    def upload(self, name, *, old=True, conversation=None):
        relative = f'inbox-assets/{name}.md'
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'test attachment')
        self.repo.save_upload(name, conversation or self.identity, name+'.md', relative, 'text/markdown', path.read_bytes())
        if old:
            with self.store._connect() as db:
                db.execute('UPDATE web_uploads SET created_at=? WHERE id=?', (self.old, name))
        return path

    def test_writer_cannot_invalidate_cleanup_snapshot_when_no_uploads_expire(self):
        orphan = self.root / 'inbox-assets' / 'interrupted.part'
        orphan.parent.mkdir(parents=True)
        orphan.write_bytes(b'incomplete')
        with self.store._connect() as db:
            db.execute('INSERT INTO web_file_intents VALUES (?,?,?)', (self.identity, str(orphan), self.old))
        writer = sqlite3.connect(self.store.database_path, timeout=0)
        self.addCleanup(writer.close)
        conflicts = []
        connect = self.store._connect

        class InterleavedConnection:
            def __init__(inner, db):
                inner.db = db

            def __getattr__(inner, name):
                return getattr(inner.db, name)

            def execute(inner, sql, parameters=()):
                cursor = inner.db.execute(sql, parameters)
                if sql.startswith('SELECT path FROM web_file_intents'):
                    rows = cursor.fetchall()
                    # A second connection writes between the cleanup read and delete.
                    try:
                        writer.execute('UPDATE web_conversations SET title=? WHERE id=?', ('concurrent update', self.identity))
                        writer.commit()
                    except sqlite3.OperationalError as error:
                        conflicts.append(error.sqlite_errorcode)
                        writer.rollback()
                    return SimpleNamespace(fetchall=lambda: rows)
                return cursor

        @contextmanager
        def interleaved():
            with connect() as db:
                yield InterleavedConnection(db)

        with patch.object(self.store, '_connect', interleaved):
            self.repo.clean_uploads()
        self.assertEqual(conflicts, [sqlite3.SQLITE_BUSY])
        self.assertFalse(orphan.exists())
        with self.store._connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM web_file_intents').fetchone()[0], 0)
        # The cleanup transaction released the writer lock before returning.
        writer.execute('UPDATE web_conversations SET title=? WHERE id=?', ('saved after cleanup', self.identity))
        writer.commit()
        self.assertEqual(self.repo.conversation(self.identity)['title'], 'saved after cleanup')

    def test_cleanup_preserves_recent_used_and_deleting_conversation_attachments(self):
        expired = self.upload('expired')
        recent = self.upload('recent', old=False)
        used = self.upload('used')
        self.repo.accept(self.identity, 'read this document', ['used'], 'request')
        deleting = self.repo.create_conversation()['id']
        protected = self.upload('protected', conversation=deleting)
        ConversationDeletionService(self.repo).request(deleting)
        # A stale publication intent must not remove a still referenced upload.
        with self.store._connect() as db:
            db.execute('INSERT INTO web_file_intents VALUES (?,?,?)', (self.identity, str(used), self.old))
        self.repo.clean_uploads()
        self.assertFalse(expired.exists())
        for path in (recent, used, protected):
            self.assertTrue(path.exists(), path)
        with self.store._connect() as db:
            self.assertEqual({row[0] for row in db.execute('SELECT id FROM web_uploads')}, {'recent', 'used', 'protected'})
