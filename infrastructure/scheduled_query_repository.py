from datetime import datetime, timezone
import hashlib
import json

from infrastructure.task_repository import TaskConflict


class ScheduledQueryRepository:
    def __init__(self, store):
        self.store = store
        with store._connect() as connection:
            connection.executescript('''
                CREATE TABLE IF NOT EXISTS scheduled_queries (
                    query_id TEXT PRIMARY KEY, platform TEXT NOT NULL, group_id TEXT NOT NULL, owner_id TEXT NOT NULL,
                    kind TEXT NOT NULL, arguments_json TEXT NOT NULL, due_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active', version INTEGER NOT NULL DEFAULT 1,
                    source_event_id TEXT NOT NULL UNIQUE, output_event_id TEXT UNIQUE,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_scheduled_queries_due ON scheduled_queries(platform, status, due_at);
                CREATE INDEX IF NOT EXISTS idx_scheduled_queries_owner ON scheduled_queries(platform, group_id, owner_id, updated_at);
                CREATE TRIGGER IF NOT EXISTS scheduled_query_delivery_state AFTER UPDATE OF status ON outbox_messages
                WHEN NEW.event_id LIKE 'scheduled-query:%'
                BEGIN
                    UPDATE scheduled_queries SET status=CASE NEW.status WHEN 'sent' THEN 'sent'
                        WHEN 'dead_letter' THEN 'failed' WHEN 'cancelled' THEN 'cancelled' ELSE status END
                    WHERE output_event_id=NEW.event_id;
                END;
                CREATE TRIGGER IF NOT EXISTS scheduled_query_input_cancel AFTER UPDATE OF status ON inbox_jobs
                WHEN NEW.status IN ('cancelled','failed','blocked')
                BEGIN
                    UPDATE scheduled_queries SET status=NEW.status WHERE output_event_id=NEW.event_key AND status='queued';
                END;
            ''')

    @staticmethod
    def new_id(event_key):
        return 'Q-' + hashlib.sha256(event_key.encode()).hexdigest()[:12]

    def list_for_owner(self, event, *, limit=100, offset=0):
        with self.store._connect() as connection:
            return tuple(dict(row) for row in connection.execute('''SELECT * FROM scheduled_queries
                WHERE platform=? AND group_id=? AND owner_id=? ORDER BY updated_at DESC, rowid DESC LIMIT ? OFFSET ?''',
                (event.platform, event.scope_id, event.sender_id, limit, offset)).fetchall())

    def create(self, event, claim, update, row, reply, now):
        with self.store._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            active = connection.execute("SELECT 1 FROM processed_events WHERE event_id=? AND claim_token=? AND status='processing'", (event.event_key, claim.claim_token)).fetchone()
            if not active:
                raise TaskConflict('事件处理权已变化，请重试。')
            if row['due_at'] <= now.isoformat():
                raise ValueError('查询时间已经过去，请重新指定。')
            self.store.tasks.apply(connection, event, update, now)
            connection.execute('''INSERT OR IGNORE INTO scheduled_queries
                (query_id, platform, group_id, owner_id, kind, arguments_json, due_at, source_event_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (row['query_id'], event.platform, event.scope_id, event.sender_id, row['kind'],
                 json.dumps(row['arguments'], ensure_ascii=False), row['due_at'], event.event_key, now.isoformat(), now.isoformat()))
            connection.execute('UPDATE processed_events SET prepared_reply=?, prepared_memory_content=? WHERE event_id=?',
                               (reply, event.content, event.event_key))
        return reply

    def cancel(self, event, query_id):
        now = datetime.now(timezone.utc).isoformat()
        with self.store._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('''SELECT * FROM scheduled_queries WHERE query_id=? AND platform=? AND group_id=?
                AND owner_id=? AND status IN ('active','queued')''', (query_id, event.platform, event.scope_id, event.sender_id)).fetchone()
            if not row:
                return False
            if row['output_event_id']:
                output = connection.execute('SELECT prepared_reply FROM processed_events WHERE event_id=?', (row['output_event_id'],)).fetchone()
                if (output and output['prepared_reply'] is not None) or connection.execute('SELECT 1 FROM outbox_messages WHERE event_id=?', (row['output_event_id'],)).fetchone():
                    return False
                connection.execute("UPDATE inbox_jobs SET status='cancelled', claim_token=NULL, lease_until=NULL WHERE event_key=? AND status IN ('pending','running','retry')", (row['output_event_id'],))
                connection.execute('''INSERT INTO processed_events (event_id, status, created_at, updated_at)
                    VALUES (?, 'completed', ?, ?) ON CONFLICT(event_id) DO UPDATE SET status='completed', claim_token=NULL''',
                    (row['output_event_id'], now, now))
            connection.execute("UPDATE scheduled_queries SET status='cancelled', version=version+1, updated_at=? WHERE query_id=?", (now, query_id))
        return True

    def enqueue_due(self, platform, now, group_allowed):
        with self.store._connect() as connection:
            rows = connection.execute("SELECT * FROM scheduled_queries WHERE platform=? AND status='active' AND due_at<=? ORDER BY due_at LIMIT 100", (platform, now.isoformat())).fetchall()
        count = 0
        for source in rows:
            row = dict(source)
            with self.store._connect() as connection:
                connection.execute('BEGIN IMMEDIATE')
                current = connection.execute("SELECT 1 FROM scheduled_queries WHERE query_id=? AND status='active' AND version=?", (row['query_id'], row['version'])).fetchone()
                if not current:
                    continue
                if not group_allowed(row['group_id']):
                    connection.execute("UPDATE scheduled_queries SET status='blocked', updated_at=? WHERE query_id=?", (now.isoformat(), row['query_id']))
                    continue
                if (now-datetime.fromisoformat(row['due_at'])).total_seconds() > 24*3600:
                    connection.execute("UPDATE scheduled_queries SET status='missed', updated_at=? WHERE query_id=?", (now.isoformat(), row['query_id']))
                    continue
                event_key = f"scheduled-query:{row['query_id']}:v{row['version']}"
                payload = {'post_type': 'scheduled_query', 'query_id': row['query_id'], 'version': row['version'],
                           'group_id': row['group_id'], 'user_id': row['owner_id']}
                connection.execute('''INSERT OR IGNORE INTO inbox_jobs
                    (event_key, platform, scope_id, owner_id, payload_json, capture_state, next_attempt_at, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, 'ready', ?, ?, ?)''', (event_key, platform, row['group_id'], row['owner_id'],
                    json.dumps(payload), now.isoformat(), now.isoformat(), now.isoformat()))
                connection.execute("UPDATE scheduled_queries SET status='queued', output_event_id=?, updated_at=? WHERE query_id=?", (event_key, now.isoformat(), row['query_id']))
                count += 1
        return count

    def for_job(self, job):
        with self.store._connect() as connection:
            row = connection.execute('''SELECT * FROM scheduled_queries WHERE query_id=? AND version=? AND output_event_id=?
                AND platform=? AND group_id=? AND owner_id=? AND status='queued' ''',
                (job['payload']['query_id'], job['payload']['version'], job['event_key'], job['platform'], job['scope_id'], job['owner_id'])).fetchone()
        return dict(row) if row else None

    def mark_missed(self, row):
        with self.store._connect() as connection:
            connection.execute("UPDATE scheduled_queries SET status='missed' WHERE query_id=? AND version=? AND status='queued'",
                               (row['query_id'], row['version']))
