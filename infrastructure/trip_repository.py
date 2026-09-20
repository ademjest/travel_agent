import hashlib
import json

from core.tasks import utc_now
from infrastructure.task_repository import TaskConflict


class TripRepository:
    def __init__(self, store):
        self.store = store
        with store._connect() as connection:
            connection.executescript('''
                CREATE TABLE IF NOT EXISTS trips (
                    trip_id TEXT PRIMARY KEY, platform TEXT NOT NULL, scope_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL, title TEXT NOT NULL, status TEXT NOT NULL,
                    version INTEGER NOT NULL, spec_json TEXT NOT NULL, plan_json TEXT NOT NULL,
                    sources_json TEXT NOT NULL, source_event_id TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_trips_owner ON trips(platform, scope_id, owner_id, updated_at);
                CREATE TABLE IF NOT EXISTS trip_versions (
                    trip_id TEXT NOT NULL, version INTEGER NOT NULL, event_id TEXT NOT NULL UNIQUE,
                    snapshot_json TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY(trip_id, version), FOREIGN KEY(trip_id) REFERENCES trips(trip_id)
                );
            ''')

    @staticmethod
    def new_id(event_key):
        return 'TR-' + hashlib.sha256(event_key.encode()).hexdigest()[:12]

    @staticmethod
    def _record(row):
        if not row:
            return None
        value = dict(row)
        for key in ('spec', 'plan', 'sources'):
            value[key] = json.loads(value.pop(key + '_json'))
        return value

    def list_for_owner(self, event):
        with self.store._connect() as connection:
            return tuple(self._record(row) for row in connection.execute('''
                SELECT * FROM trips WHERE platform=? AND scope_id=? AND owner_id=? AND status='active'
                ORDER BY updated_at DESC, rowid DESC LIMIT 30
            ''', (event.platform, event.storage_scope_id, event.sender_id)).fetchall())

    def get(self, event, trip_id):
        with self.store._connect() as connection:
            return self._record(connection.execute('''SELECT * FROM trips
                WHERE trip_id=? AND platform=? AND scope_id=? AND owner_id=?''',
                (trip_id, event.platform, event.storage_scope_id, event.sender_id)).fetchone())

    def document_options(self, event):
        with self.store._connect() as connection:
            return tuple(dict(row) for row in connection.execute(
                'SELECT id, filename, created_at FROM documents WHERE group_openid=? ORDER BY id DESC LIMIT 10',
                (event.storage_scope_id,)).fetchall())

    def referenced(self, event):
        """Resolve only an actual reply link, including completed/expired planning tasks."""
        with self.store._connect() as connection:
            row = connection.execute('''SELECT p.* FROM outbound_message_links l
                JOIN task_event_results e ON e.event_id=l.event_id
                JOIN travel_tasks t ON t.task_id=e.task_id
                JOIN trips p ON p.trip_id=json_extract(t.slots_json,'$.trip_id')
                WHERE l.platform=? AND l.scope_id=? AND l.message_id=?
                AND p.platform=? AND p.scope_id=? AND p.owner_id=? AND p.status='active'
                LIMIT 1''', (event.platform,event.storage_scope_id,event.reply_to_id,
                            event.platform,event.storage_scope_id,event.sender_id)).fetchone()
        return self._record(row)

    def document_context(self, event, document_id):
        with self.store._connect() as connection:
            connection.execute('BEGIN')
            row = connection.execute('SELECT id, filename FROM documents WHERE id=? AND group_openid=?',
                                     (document_id, event.storage_scope_id)).fetchone()
            if not row:
                raise TaskConflict('所选文档已不可用或不属于当前群，请重新选择。')
            chunks = connection.execute('SELECT content FROM document_chunks WHERE document_id=? ORDER BY chunk_index LIMIT 6',
                                        (document_id,)).fetchall()
        return (f"[文档#{row['id']} {row['filename']}]\n" + '\n'.join(chunk['content'] for chunk in chunks))[:3200]

    def linked_reminders(self, event, trip_id, *, include_cancelled=False):
        with self.store._connect() as connection:
            return tuple(dict(row) for row in connection.execute('''
                SELECT r.*, o.status AS delivery_status FROM personal_reminders r
                JOIN reminder_occurrences o ON o.reminder_id=r.reminder_id AND o.version=r.version
                WHERE r.platform=? AND r.group_id=? AND r.owner_id=?
                AND json_extract(r.source_json, '$.trip_id')=? AND (? OR r.status='active')
                ORDER BY r.updated_at DESC, r.rowid DESC
            ''', (event.platform, event.scope_id, event.sender_id, trip_id, include_cancelled)).fetchall())

    def commit(self, event, claim, task_update, reply, *, trip=None, expected_version=None,
               expected_trip=None, reminder_changes=(), supersede_request='', preference_update=None, now=None):
        now = now or utc_now()
        timestamp = now.isoformat()
        with self.store._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            current = connection.execute('SELECT * FROM processed_events WHERE event_id=?', (event.event_key,)).fetchone()
            if not current or current['claim_token'] != claim.claim_token or current['status'] != 'processing':
                raise TaskConflict('事件处理权已变化，请重新查看行程后重试。')
            if connection.execute('SELECT 1 FROM task_event_results WHERE event_id=?', (event.event_key,)).fetchone():
                return current['prepared_reply'] or reply
            if expected_trip:
                existing = connection.execute('''SELECT version FROM trips WHERE trip_id=? AND platform=?
                    AND scope_id=? AND owner_id=? AND status='active' ''',
                    (expected_trip[0], event.platform, event.storage_scope_id, event.sender_id)).fetchone()
                if not existing or existing['version'] != expected_trip[1]:
                    raise TaskConflict('关联行程已经变化，请重新核对。')
            task_id = self.store.tasks.apply(connection, event, task_update, now)
            semantic = task_update.slots.get('semantic_task')
            if semantic:
                cursor = connection.execute('''UPDATE travel_tasks SET status='completed',version=version+1
                    WHERE task_id=? AND version=? AND status='collecting' AND task_type='semantic'
                    AND platform=? AND scope_id=? AND owner_id=?''',
                    (semantic['task_id'],semantic['version'],event.platform,event.storage_scope_id,event.sender_id))
                if cursor.rowcount != 1:
                    raise TaskConflict('行程选择已变化，请重新提出调整。')
            if supersede_request:
                connection.execute('''UPDATE travel_tasks SET status='superseded',version=version+1
                    WHERE platform=? AND scope_id=? AND owner_id=? AND task_type IN ('transit','walking')
                    AND status='collecting' AND initial_request=?''',
                    (event.platform,event.storage_scope_id,event.sender_id,supersede_request))
            if (task_update.task_type == 'itinerary' and task_update.status == 'collecting'
                    and task_update.slots.get('trip_id')):
                connection.execute('''UPDATE travel_tasks SET status='superseded', version=version+1
                    WHERE task_type='itinerary' AND status='collecting' AND task_id != ?
                    AND platform=? AND scope_id=? AND owner_id=? AND json_extract(slots_json, '$.trip_id')=?''',
                    (task_id, event.platform, event.storage_scope_id, event.sender_id,
                     task_update.slots['trip_id']))
            if trip is not None:
                snapshot = {key: trip[key] for key in ('trip_id', 'title', 'spec', 'plan', 'sources', 'status')}
                if expected_version is None:
                    connection.execute('''INSERT INTO trips VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)''',
                        (trip['trip_id'], event.platform, event.storage_scope_id, event.sender_id, trip['title'],
                         trip['status'], json.dumps(trip['spec'], ensure_ascii=False), json.dumps(trip['plan'], ensure_ascii=False),
                         json.dumps(trip['sources'], ensure_ascii=False), event.event_key, timestamp, timestamp))
                    version = 1
                else:
                    cursor = connection.execute('''UPDATE trips SET title=?, status=?, spec_json=?, plan_json=?,
                        sources_json=?, version=version+1, updated_at=?
                        WHERE trip_id=? AND platform=? AND scope_id=? AND owner_id=? AND version=? AND status='active'
                    ''', (trip['title'], trip['status'], json.dumps(trip['spec'], ensure_ascii=False),
                          json.dumps(trip['plan'], ensure_ascii=False), json.dumps(trip['sources'], ensure_ascii=False),
                          timestamp, trip['trip_id'], event.platform, event.storage_scope_id, event.sender_id, expected_version))
                    if cursor.rowcount != 1:
                        raise TaskConflict('行程版本已变化，请查看最新行程后重新修改。')
                    version = expected_version + 1
                snapshot['version'] = version
                connection.execute('INSERT INTO trip_versions VALUES (?, ?, ?, ?, ?)',
                    (trip['trip_id'], version, event.event_key, json.dumps(snapshot, ensure_ascii=False), timestamp))
            for action, reminder in reminder_changes:
                self.store.reminders.apply_change(connection, event, task_id, action, reminder, now)
            if preference_update:
                self.store.preferences.apply_update(connection, event, preference_update['version'], preference_update['values'])
            connection.execute('UPDATE processed_events SET prepared_reply=?, prepared_memory_content=?, updated_at=? WHERE event_id=?',
                               (reply, event.content, timestamp, event.event_key))
        return reply
