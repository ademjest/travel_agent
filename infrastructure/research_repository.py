from datetime import datetime, timezone
import json


class ResearchRepository:
    def __init__(self, store):
        self.store = store
        with store._connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS research_runs (
                event_key TEXT PRIMARY KEY, platform TEXT, scope TEXT, owner TEXT,
                stage TEXT NOT NULL, data_json TEXT NOT NULL, updated_at TEXT NOT NULL)''')

    def get(self, event):
        with self.store._connect() as db:
            row = db.execute('SELECT * FROM research_runs WHERE event_key=? AND platform=? AND scope=? AND owner=?',
                (event.event_key, event.platform, event.scope_id, event.sender_id)).fetchone()
        return {'stage': row['stage'], **json.loads(row['data_json'])} if row else None

    def save(self, event, stage, data):
        with self.store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if event.platform == 'web' and getattr(self.store, 'web_lifecycle', None):
                self.store.web_lifecycle.check(event.scope_id, db)
            db.execute('''INSERT INTO research_runs VALUES (?,?,?,?,?,?,?) ON CONFLICT(event_key) DO UPDATE SET
                stage=excluded.stage,data_json=excluded.data_json,updated_at=excluded.updated_at''',
                (event.event_key, event.platform, event.scope_id, event.sender_id, stage,
                 json.dumps({k: v for k, v in data.items() if k != 'stage'}, ensure_ascii=False), datetime.now(timezone.utc).isoformat()))

    def latest(self, event):
        with self.store._connect() as db:
            rows = db.execute('''SELECT CASE WHEN j.status IN ('cancelled','failed','blocked') THEN j.status ELSE r.stage END AS stage,
                r.data_json,r.updated_at FROM research_runs r LEFT JOIN inbox_jobs j ON j.event_key=r.event_key
                WHERE r.platform=? AND r.scope=? AND r.owner=? ORDER BY r.updated_at DESC LIMIT 5''',
                (event.platform, event.scope_id, event.sender_id)).fetchall()
        return [{'stage': row['stage'], 'updated_at': row['updated_at'], **json.loads(row['data_json'])} for row in rows]
