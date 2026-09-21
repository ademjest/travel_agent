"""Local Web deletion barriers and short, process-wide file publication sections."""
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import threading

from core.execution_scope import ExecutionRevoked, ensure_execution_active


class ConversationDeleted(ExecutionRevoked):
    status_code = 410

    def __init__(self, identity):
        self.identity = identity
        super().__init__('该会话已删除或正在清理，不能继续使用。')


_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


class WebLifecycle:
    def __init__(self, store):
        self.store = store
        self.root = store.database_path.parent.resolve()
        with _LOCKS_GUARD:
            self.file_lock = _LOCKS.setdefault(str(store.database_path.resolve()), threading.RLock())
        with store._connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS web_conversation_deletions (
                    conversation_id TEXT PRIMARY KEY, state TEXT NOT NULL,
                    files_json TEXT NOT NULL DEFAULT '[]', events_json TEXT NOT NULL DEFAULT '[]',
                    requested_at TEXT NOT NULL, updated_at TEXT NOT NULL, error TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS web_file_intents (
                    conversation_id TEXT NOT NULL, path TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY(conversation_id, path));
            ''')

    def check(self, identity, db):
        if db.execute('SELECT 1 FROM web_conversation_deletions WHERE conversation_id=?', (identity,)).fetchone():
            raise ConversationDeleted(identity)
        row = db.execute('SELECT * FROM web_conversations WHERE id=?', (identity,)).fetchone()
        if row is None:
            raise LookupError('会话不存在。')
        return dict(row)

    def safe_path(self, raw):
        path = Path(raw)
        if not path.is_absolute():
            path = self.root / path
        # resolve() follows Windows junctions as well as symlinks. Never follow them outside the data root.
        path = path.resolve()
        if not path.is_relative_to(self.root) or path == self.root:
            raise ValueError('附件路径超出 Web 数据目录，已拒绝清理。')
        if path.relative_to(self.root).parts[0] not in {'inbox-assets', 'images', 'media'}:
            raise ValueError('不是可清理的附件路径。')
        return path

    @contextmanager
    def publish(self, identity, paths):
        with self.file_lock:
            ensure_execution_active()
            safe = [self.safe_path(path) for path in paths]
            with self.store._connect() as db:
                db.execute('BEGIN IMMEDIATE')
                self.check(identity, db)
                db.executemany('INSERT OR IGNORE INTO web_file_intents VALUES (?,?,?)',
                    [(identity, str(path), datetime.now(timezone.utc).isoformat()) for path in safe])
            # Network/model work must already have finished before entering this context.
            yield
            with self.store._connect() as db:
                db.executemany('DELETE FROM web_file_intents WHERE conversation_id=? AND path=?',
                               [(identity, str(path)) for path in safe])

    def remove_unreferenced(self, raw):
        with self.file_lock:
            path = self.safe_path(raw)
            with self.store._connect() as db:
                references = [row[0] for row in db.execute('''
                    SELECT relative_path FROM web_uploads UNION ALL SELECT file_path FROM inbox_assets
                    UNION ALL SELECT file_path FROM reservation_images UNION ALL SELECT file_path FROM media_observations
                    UNION ALL SELECT path FROM web_file_intents''')]
            for reference in references:
                other = Path(reference)
                if not other.is_absolute():
                    other = self.root / other
                if other.resolve() == path:
                    return False
            path.unlink(missing_ok=True)
            return True


@contextmanager
def publish_files(store, platform, identity, paths):
    lifecycle = getattr(store, 'web_lifecycle', None)
    if platform == 'web' and lifecycle is not None:
        with lifecycle.publish(identity, paths):
            yield
    else:
        yield
