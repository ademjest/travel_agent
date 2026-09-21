from datetime import datetime, timezone
import json


class MediaRepository:
    def __init__(self, store):
        self.store = store
        with store._connect() as connection:
            connection.execute('''CREATE TABLE IF NOT EXISTS media_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT NOT NULL UNIQUE,
                platform TEXT NOT NULL, scope_id TEXT NOT NULL, owner_id TEXT NOT NULL,
                sha256 TEXT NOT NULL, file_path TEXT NOT NULL, content_type TEXT NOT NULL,
                model_id TEXT NOT NULL, result_json TEXT NOT NULL, created_at TEXT NOT NULL
            )''')

    def get_for_event(self, event):
        with self.store._connect() as connection:
            row = connection.execute('''SELECT id, result_json FROM media_observations
                WHERE event_key=? AND platform=? AND scope_id=? AND owner_id=?''',
                (event.event_key, event.platform, event.storage_scope_id, event.sender_id)).fetchone()
        return {**json.loads(row['result_json']), 'media_id': row['id']} if row else None

    def save(self, event, digest, file_path, content_type, model, result):
        with self.store._connect() as connection:
            connection.execute('''INSERT OR IGNORE INTO media_observations
                (event_key, platform, scope_id, owner_id, sha256, file_path, content_type, model_id, result_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (event.event_key, event.platform, event.storage_scope_id, event.sender_id, digest, file_path,
                 content_type, model, json.dumps(result, ensure_ascii=False), datetime.now(timezone.utc).isoformat()))
        return self.get_for_event(event)

    def context(self, event, max_chars=1200):
        with self.store._connect() as connection:
            rows = connection.execute('''SELECT id, result_json, created_at FROM media_observations
                WHERE platform=? AND scope_id=? AND owner_id=? ORDER BY id DESC LIMIT 3''',
                (event.platform, event.storage_scope_id, event.sender_id)).fetchall()
        parts = []
        for row in rows:
            result = json.loads(row['result_json'])
            parts.append(f"[图片#{row['id']}，识别时间 {row['created_at']}，{'含不确定字段' if result['uncertain'] else '仍需按原图核对'}]\n{result['facts']}")
        return '\n'.join(parts)[:max_chars]

    def purge(self, cutoff):
        with self.store._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            paths = [row['file_path'] for row in connection.execute('SELECT file_path FROM media_observations WHERE created_at < ?', (cutoff.isoformat(),))]
            count = connection.execute('DELETE FROM media_observations WHERE created_at < ?', (cutoff.isoformat(),)).rowcount
            retained = {row['file_path'] for row in connection.execute('SELECT file_path FROM media_observations')}
        return count, [path for path in paths if path not in retained]
