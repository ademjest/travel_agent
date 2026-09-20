import hashlib
import json
from datetime import timedelta, timezone

from core.tasks import utc_now
from infrastructure.task_repository import TaskConflict


class ReminderRepository:
    def __init__(self, store):
        self.store = store
        with store._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS personal_reminders (
                    reminder_id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
                    platform TEXT NOT NULL, group_id TEXT NOT NULL, owner_id TEXT NOT NULL,
                    title TEXT NOT NULL, scheduled_at_utc TEXT NOT NULL,
                    timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
                    status TEXT NOT NULL DEFAULT 'active', version INTEGER NOT NULL DEFAULT 1,
                    source_event_id TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES travel_tasks(task_id)
                );
                CREATE INDEX IF NOT EXISTS idx_personal_reminders_owner
                ON personal_reminders(platform, group_id, owner_id, updated_at);
                CREATE TABLE IF NOT EXISTS reminder_occurrences (
                    reminder_id TEXT NOT NULL, version INTEGER NOT NULL,
                    scheduled_at_utc TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    outbox_event_id TEXT UNIQUE, last_error TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(reminder_id, version),
                    FOREIGN KEY(reminder_id) REFERENCES personal_reminders(reminder_id)
                );
                CREATE TRIGGER IF NOT EXISTS personal_reminder_delivery_state
                AFTER UPDATE OF status ON outbox_messages
                WHEN NEW.event_id LIKE 'reminder:%'
                BEGIN
                    UPDATE reminder_occurrences SET status = CASE NEW.status
                        WHEN 'sent' THEN 'sent' WHEN 'dead_letter' THEN 'failed'
                        WHEN 'cancelled' THEN 'cancelled' ELSE status END,
                        last_error = COALESCE(NEW.last_error, '')
                    WHERE outbox_event_id = NEW.event_id;
                END;
            """)
            columns = {row['name'] for row in connection.execute('PRAGMA table_info(personal_reminders)')}
            if 'source_json' not in columns:
                connection.execute("ALTER TABLE personal_reminders ADD COLUMN source_json TEXT NOT NULL DEFAULT '{}'")

    @staticmethod
    def new_id(event_key):
        return 'M-' + hashlib.sha256(event_key.encode()).hexdigest()[:12]

    def list_for_owner(self, platform, group_id, owner_id):
        with self.store._connect() as connection:
            return tuple(dict(row) for row in connection.execute("""
                SELECT r.*, o.status AS delivery_status FROM personal_reminders r
                JOIN reminder_occurrences o ON o.reminder_id=r.reminder_id AND o.version=r.version
                WHERE r.platform=? AND r.group_id=? AND r.owner_id=?
                ORDER BY r.updated_at DESC, r.rowid DESC LIMIT 100
            """, (platform, group_id, owner_id)).fetchall())

    def commit(self, event, claim, task_update, reply, *, action='', reminder=None, now=None):
        now = now or utc_now()
        timestamp = now.isoformat()
        with self.store._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            current = connection.execute('SELECT * FROM processed_events WHERE event_id=?', (event.event_key,)).fetchone()
            if not current or current['claim_token'] != claim.claim_token or current['status'] != 'processing':
                raise TaskConflict('事件处理权已变化，请稍后重试。')
            if connection.execute('SELECT 1 FROM task_event_results WHERE event_id=?', (event.event_key,)).fetchone():
                return current['prepared_reply'] or reply
            task_id = self.store.tasks.apply(connection, event, task_update, now)
            if action:
                self.apply_change(connection, event, task_id, action, reminder, now)
            connection.execute('UPDATE processed_events SET prepared_reply=?, prepared_memory_content=?, updated_at=? WHERE event_id=?',
                               (reply, event.content, timestamp, event.event_key))
        return reply

    def apply_change(self, connection, event, task_id, action, reminder, now):
        timestamp = now.isoformat()
        if action == 'create':
            if reminder['scheduled_at_utc'] <= timestamp:
                raise ValueError('提醒时间已过，请重新指定未来时间。')
            connection.execute("""
                INSERT INTO personal_reminders (reminder_id, task_id, platform, group_id,
                    owner_id, title, scheduled_at_utc, source_event_id, created_at, updated_at, source_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (reminder['reminder_id'], task_id, event.platform, event.scope_id,
                  event.sender_id, reminder['title'], reminder['scheduled_at_utc'],
                  reminder.get('source_operation_key', event.event_key), timestamp, timestamp, json.dumps(reminder.get('source', {}), ensure_ascii=False)))
        elif action in {'update', 'cancel'}:
            existing = connection.execute("""
                SELECT * FROM personal_reminders WHERE reminder_id=? AND platform=?
                AND group_id=? AND owner_id=? AND status='active' AND version=?
            """, (reminder['reminder_id'], event.platform, event.scope_id,
                  event.sender_id, reminder['version'])).fetchone()
            if not existing:
                raise TaskConflict('提醒已经变化或不属于你，请重新查看提醒。')
            old_event = f"reminder:{existing['reminder_id']}:v{existing['version']}"
            sending = connection.execute("SELECT 1 FROM outbox_messages WHERE event_id=? AND status='sending'",
                                         (old_event,)).fetchone()
            if sending:
                raise TaskConflict('这条提醒正在投递，可能已经发送，请稍后再查看和修改。')
            if action == 'update' and reminder['scheduled_at_utc'] <= timestamp:
                raise ValueError('提醒时间已过，请重新指定未来时间。')
            connection.execute("UPDATE outbox_messages SET status='cancelled' WHERE event_id=? AND status IN ('pending','failed')", (old_event,))
            connection.execute("UPDATE processed_events SET status='completed', prepared_reply=NULL, prepared_memory_content=NULL WHERE event_id=?", (old_event,))
            connection.execute("UPDATE reminder_occurrences SET status='cancelled' WHERE reminder_id=? AND status IN ('pending','queued','blocked','missed','failed')", (existing['reminder_id'],))
            connection.execute("""
                UPDATE personal_reminders SET title=?, scheduled_at_utc=?, status=?, version=version+1, updated_at=?
                WHERE reminder_id=?
            """, (reminder.get('title', existing['title']), reminder['scheduled_at_utc'], 'cancelled' if action == 'cancel' else 'active', timestamp, existing['reminder_id']))
            if action == 'update' and reminder.get('source') is not None:
                connection.execute('UPDATE personal_reminders SET source_json=? WHERE reminder_id=?',
                                   (json.dumps(reminder['source'], ensure_ascii=False), existing['reminder_id']))
            elif action == 'update' and existing['source_json'] != '{}':
                source = json.loads(existing['source_json'])
                source['manual_time_override'] = {'event_id': event.event_key, 'scheduled_at_utc': reminder['scheduled_at_utc']}
                connection.execute('UPDATE personal_reminders SET source_json=? WHERE reminder_id=?',
                                   (json.dumps(source, ensure_ascii=False), existing['reminder_id']))
        if action in {'create', 'update', 'cancel'}:
            version = 1 if action == 'create' else reminder['version'] + 1
            connection.execute('INSERT INTO reminder_occurrences (reminder_id, version, scheduled_at_utc, status) VALUES (?, ?, ?, ?)',
                               (reminder['reminder_id'], version, reminder['scheduled_at_utc'], 'cancelled' if action == 'cancel' else 'pending'))

    def due(self, platform, now):
        with self.store._connect() as connection:
            return tuple(dict(row) for row in connection.execute("""
                SELECT r.* FROM personal_reminders r JOIN reminder_occurrences o
                ON o.reminder_id=r.reminder_id AND o.version=r.version
                WHERE r.platform=? AND r.status='active' AND o.status='pending' AND o.scheduled_at_utc <= ?
                ORDER BY o.scheduled_at_utc LIMIT 100
            """, (platform, now.isoformat())).fetchall())

    def enqueue(self, reminder, payload, text, now, *, allowed=True):
        event_id = f"reminder:{reminder['reminder_id']}:v{reminder['version']}"
        timestamp = now.isoformat()
        with self.store._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute("""
                SELECT r.* FROM personal_reminders r JOIN reminder_occurrences o
                ON o.reminder_id=r.reminder_id AND o.version=r.version
                WHERE r.reminder_id=? AND r.version=? AND r.status='active' AND o.status='pending'
                    AND o.scheduled_at_utc <= ?
            """, (reminder['reminder_id'], reminder['version'], timestamp)).fetchone()
            if not row:
                return False
            status = 'queued' if allowed else 'blocked'
            if row['scheduled_at_utc'] < (now - timedelta(hours=24)).isoformat():
                status = 'missed'
            connection.execute('UPDATE reminder_occurrences SET status=?, outbox_event_id=? WHERE reminder_id=? AND version=?',
                               (status, event_id if status == 'queued' else None, row['reminder_id'], row['version']))
            if status != 'queued':
                return False
            connection.execute("""
                INSERT INTO processed_events (event_id, status, created_at, updated_at, prepared_reply, prepared_memory_content)
                VALUES (?, 'processing', ?, ?, ?, '')
            """, (event_id, timestamp, timestamp, text))
            connection.execute("""
                INSERT INTO outbox_messages (event_id, platform, channel, target_id, sender_id,
                    reply_to_id, payload_json, status, attempt_count, next_attempt_at, created_at)
                VALUES (?, ?, 'group', ?, ?, '', ?, 'pending', 0, ?, ?)
            """, (event_id, row['platform'], row['group_id'], row['owner_id'],
                  json.dumps(payload, ensure_ascii=False), timestamp, timestamp))
        return True

    def delivery_is_current(self, event_id):
        if not event_id.startswith('reminder:'):
            return True
        with self.store._connect() as connection:
            return connection.execute("""
                SELECT 1 FROM reminder_occurrences o JOIN personal_reminders r
                ON r.reminder_id=o.reminder_id AND r.version=o.version
                WHERE o.outbox_event_id=? AND o.status='queued' AND r.status='active'
            """, (event_id,)).fetchone() is not None

    def stop_delivery(self, outbox_id, token):
        with self.store._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute("SELECT event_id FROM outbox_messages WHERE id=? AND status='sending' AND claim_token=?",
                                     (outbox_id, token)).fetchone()
            if row:
                connection.execute("UPDATE outbox_messages SET status='cancelled', claim_token=NULL, lease_expires_at=NULL WHERE id=?", (outbox_id,))
                connection.execute("UPDATE processed_events SET status='completed', prepared_reply=NULL, prepared_memory_content=NULL WHERE event_id=?", (row['event_id'],))
