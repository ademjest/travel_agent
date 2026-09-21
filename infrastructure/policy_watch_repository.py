from datetime import timedelta
import hashlib
import json

from infrastructure.task_repository import TaskConflict


class PolicyWatchRepository:
    def __init__(self, store):
        self.store = store
        with store._connect() as connection:
            connection.execute('''CREATE TABLE IF NOT EXISTS policy_watches (
                watch_id TEXT PRIMARY KEY, platform TEXT NOT NULL, group_id TEXT NOT NULL, owner_id TEXT NOT NULL,
                entity TEXT NOT NULL, source_url TEXT NOT NULL, interval_seconds INTEGER NOT NULL, ends_at TEXT NOT NULL,
                next_check_at TEXT NOT NULL, status TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
                poll_no INTEGER NOT NULL DEFAULT 0, last_signature TEXT NOT NULL, policy_json TEXT NOT NULL,
                last_checked_at TEXT NOT NULL, last_notice_event TEXT, source_event_id TEXT NOT NULL UNIQUE
            )''')
            connection.execute('''CREATE TRIGGER IF NOT EXISTS policy_watch_input_failure
                AFTER UPDATE OF status ON inbox_jobs WHEN NEW.status IN ('cancelled','failed','blocked')
                AND json_extract(NEW.payload_json, '$.post_type')='policy_watch'
                BEGIN
                    UPDATE policy_watches SET status=NEW.status WHERE status='queued'
                        AND watch_id=json_extract(NEW.payload_json, '$.watch_id')
                        AND version=json_extract(NEW.payload_json, '$.version')
                        AND poll_no=json_extract(NEW.payload_json, '$.poll_no');
                END''')

    @staticmethod
    def new_id(event_key):
        return 'W-' + hashlib.sha256(event_key.encode()).hexdigest()[:12]

    def list_for_owner(self, event):
        with self.store._connect() as connection:
            return tuple(dict(row) for row in connection.execute('''SELECT w.*, o.status AS notice_status
                FROM policy_watches w LEFT JOIN outbox_messages o ON o.event_id=w.last_notice_event
                WHERE w.platform=? AND w.group_id=? AND w.owner_id=? ORDER BY w.rowid DESC LIMIT 30''',
                (event.platform, event.scope_id, event.sender_id)).fetchall())

    def create(self, event, claim, update, values, reply, now):
        with self.store._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            if not connection.execute("SELECT 1 FROM processed_events WHERE event_id=? AND claim_token=? AND status='processing'", (event.event_key, claim.claim_token)).fetchone():
                raise TaskConflict('事件处理权已变化。')
            self.store.tasks.apply(connection, event, update, now)
            connection.execute('''INSERT OR IGNORE INTO policy_watches
                (watch_id, platform, group_id, owner_id, entity, source_url, interval_seconds, ends_at,
                 next_check_at, status, last_signature, policy_json, last_checked_at, source_event_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)''',
                (values['watch_id'], event.platform, event.scope_id, event.sender_id, values['entity'], values['source_url'],
                 values['interval_seconds'], values['ends_at'], (now+timedelta(seconds=values['interval_seconds'])).isoformat(),
                 values['signature'], json.dumps(values['policy'], ensure_ascii=False), now.isoformat(), event.event_key))
            connection.execute('UPDATE processed_events SET prepared_reply=?, prepared_memory_content=? WHERE event_id=?', (reply, event.content, event.event_key))
        return reply

    def stop(self, event, watch_id):
        with self.store._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            cursor = connection.execute("UPDATE policy_watches SET status='cancelled', version=version+1 WHERE watch_id=? AND platform=? AND group_id=? AND owner_id=? AND status IN ('active','queued')",
                (watch_id, event.platform, event.scope_id, event.sender_id))
            if not cursor.rowcount:
                return False
            prefix = f'policy-watch:{watch_id}:%'
            connection.execute("UPDATE inbox_jobs SET status='cancelled', claim_token=NULL, lease_until=NULL WHERE event_key LIKE ? AND status IN ('pending','running','retry')", (prefix,))
            connection.execute("UPDATE outbox_messages SET status='cancelled', claim_token=NULL, lease_expires_at=NULL WHERE event_id LIKE ? AND status IN ('pending','failed')", (prefix,))
            connection.execute("UPDATE processed_events SET status='completed', prepared_reply=NULL, prepared_memory_content=NULL, claim_token=NULL WHERE event_id LIKE ? AND NOT EXISTS(SELECT 1 FROM outbox_messages o WHERE o.event_id=processed_events.event_id AND o.status='sending')", (prefix,))
        return True

    def enqueue_due(self, platform, now, group_allowed):
        count = 0
        with self.store._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            rows = connection.execute("SELECT * FROM policy_watches WHERE platform=? AND status='active' AND next_check_at<=? ORDER BY next_check_at LIMIT 100", (platform, now.isoformat())).fetchall()
            for row in rows:
                terminal = 'blocked' if not group_allowed(row['group_id']) else 'completed' if row['ends_at'] <= now.isoformat() else ''
                if terminal:
                    connection.execute('UPDATE policy_watches SET status=? WHERE watch_id=?', (terminal, row['watch_id']))
                    continue
                poll = row['poll_no'] + 1
                key = f"policy-watch:{row['watch_id']}:v{row['version']}:p{poll}"
                payload = {'post_type': 'policy_watch', 'watch_id': row['watch_id'], 'version': row['version'],
                           'poll_no': poll, 'group_id': row['group_id'], 'user_id': row['owner_id']}
                connection.execute('''INSERT INTO inbox_jobs (event_key, platform, scope_id, owner_id, payload_json,
                    capture_state, next_attempt_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'ready', ?, ?, ?)''',
                    (key, platform, row['group_id'], row['owner_id'], json.dumps(payload), now.isoformat(), now.isoformat(), now.isoformat()))
                connection.execute("UPDATE policy_watches SET status='queued', poll_no=? WHERE watch_id=?", (poll, row['watch_id']))
                count += 1
        return count

    def for_job(self, job):
        with self.store._connect() as connection:
            row = connection.execute('''SELECT * FROM policy_watches WHERE watch_id=? AND version=? AND poll_no>=?
                AND platform=? AND group_id=? AND owner_id=? AND status IN ('active','queued','completed') ''',
                (job['payload']['watch_id'], job['payload']['version'], job['payload']['poll_no'], job['platform'], job['scope_id'], job['owner_id'])).fetchone()
        return dict(row) if row else None

    def finish_poll(self, job, claim, watch, signature, policy, reply, now):
        with self.store._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            if not connection.execute("SELECT 1 FROM processed_events WHERE event_id=? AND claim_token=? AND status='processing'", (job['event_key'], claim.claim_token)).fetchone():
                raise TaskConflict('事件处理权已变化。')
            cursor = connection.execute('''UPDATE policy_watches SET status=?, last_signature=?, policy_json=?,
                last_checked_at=?, next_check_at=?, last_notice_event=CASE WHEN ? THEN ? ELSE last_notice_event END
                WHERE watch_id=? AND version=? AND poll_no=? AND status='queued' ''',
                ('completed' if watch['ends_at'] <= now.isoformat() else 'active', signature,
                 json.dumps(policy, ensure_ascii=False), now.isoformat(), (now+timedelta(seconds=watch['interval_seconds'])).isoformat(),
                 bool(reply), job['event_key'], watch['watch_id'], watch['version'], watch['poll_no']))
            if cursor.rowcount != 1:
                raise TaskConflict('监测已停止或版本已变化。')
            connection.execute('''UPDATE processed_events SET status=?, prepared_reply=?, prepared_memory_content=?
                WHERE event_id=? AND claim_token=? AND status='processing' ''',
                ('processing' if reply else 'completed', reply or None, '[规则监测]', job['event_key'], claim.claim_token))
