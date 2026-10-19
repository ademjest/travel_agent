from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib


MAX_ARTIFACT_CHARS = 500_000


class ToolArtifactRepository:
    def __init__(self, store):
        self.store = store
        with store._connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS event_read_attempts (
                    operation_key TEXT PRIMARY KEY,event_id TEXT NOT NULL,attempts INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS http_read_attempts (
                    operation_key TEXT PRIMARY KEY,event_id TEXT NOT NULL,attempts INTEGER NOT NULL,
                    created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tool_artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    platform TEXT NOT NULL,
                    scope_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_tool_artifacts_owner
                ON tool_artifacts(platform, scope_id, owner_id, created_at);
            ''')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(tool_artifacts)')}
            for name, definition in {'original_length':'INTEGER', 'stored_length':'INTEGER',
                                      'truncated':'INTEGER NOT NULL DEFAULT 0', 'expires_at':'TEXT'}.items():
                if name not in columns:
                    db.execute(f'ALTER TABLE tool_artifacts ADD COLUMN {name} {definition}')

    def save(self, context, tool_name: str, content: str) -> str:
        value = content[:MAX_ARTIFACT_CHARS]
        digest = hashlib.sha256(
            f'{context.event_id}\n{tool_name}\n{value}'.encode('utf-8')
        ).hexdigest()[:16]
        artifact_id = 'TA-' + digest
        with self.store._connect() as db:
            db.execute('''INSERT OR IGNORE INTO tool_artifacts
                (artifact_id, platform, scope_id, owner_id, event_id, tool_name, content, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                (artifact_id, context.platform, context.group_id, context.creator_id,
                 context.event_id, tool_name, value, datetime.now(timezone.utc).isoformat()))
            lifecycle = getattr(self.store,'web_lifecycle',None)
            if context.platform == 'web' and lifecycle:
                lifecycle.check(context.group_id,db)
            db.execute('UPDATE tool_artifacts SET original_length=?,stored_length=?,truncated=?,expires_at=? WHERE artifact_id=?',
                (len(content),len(value),int(len(content)>len(value)),
                 (datetime.now(timezone.utc)+timedelta(days=30)).isoformat(),artifact_id))
        return artifact_id

    def read(self, context, artifact_id: str, cursor: int = 0, max_chars: int = 4000) -> dict:
        if not artifact_id.startswith('TA-') or not 0 <= cursor <= MAX_ARTIFACT_CHARS:
            raise ValueError('工具结果编号或读取位置无效。')
        max_chars = min(max(500, max_chars), 8000)
        with self.store._connect() as db:
            lifecycle = getattr(self.store,'web_lifecycle',None)
            if context.platform == 'web' and lifecycle:
                lifecycle.check(context.group_id,db)
            row = db.execute('''SELECT * FROM tool_artifacts
                WHERE artifact_id=? AND platform=? AND scope_id=? AND owner_id=?''',
                (artifact_id, context.platform, context.group_id, context.creator_id)).fetchone()
        if row is None or (row['expires_at'] and row['expires_at'] <= datetime.now(timezone.utc).isoformat()):
            raise PermissionError('没有找到当前会话可读取的工具结果。')
        content = row['content']
        end = min(len(content), cursor + max_chars)
        return {'artifact_id': artifact_id, 'tool_name': row['tool_name'],
                'cursor': cursor, 'next_cursor': end if end < len(content) else None,
                'content': content[cursor:end], 'total_chars': len(content),
                'original_length':row['original_length'], 'truncated':bool(row['truncated'])}
