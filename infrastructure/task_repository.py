import json
import uuid

from core.tasks import TASK_TTL, TaskRecord, TaskUpdate, utc_now


class TaskConflict(ValueError):
    pass


class TaskRepository:
    """Task writes share the event-result transaction; no model work holds a DB lock."""

    def __init__(self, store):
        self.store = store
        with store._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS travel_tasks (
                    task_id TEXT PRIMARY KEY,
                    platform TEXT NOT NULL, scope_id TEXT NOT NULL, owner_id TEXT NOT NULL,
                    task_type TEXT NOT NULL, status TEXT NOT NULL,
                    initial_request TEXT NOT NULL, slots_json TEXT NOT NULL,
                    missing_slots_json TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    source_event_id TEXT NOT NULL UNIQUE, last_event_id TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, expires_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_owner
                ON travel_tasks(platform, scope_id, owner_id, updated_at);
                CREATE TABLE IF NOT EXISTS task_event_results (
                    event_id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
                    task_version INTEGER NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES travel_tasks(task_id)
                );
                CREATE TABLE IF NOT EXISTS outbound_message_links (
                    platform TEXT NOT NULL, scope_id TEXT NOT NULL, message_id TEXT NOT NULL,
                    event_id TEXT NOT NULL, owner_id TEXT NOT NULL,
                    PRIMARY KEY(platform, scope_id, message_id),
                    FOREIGN KEY(event_id) REFERENCES processed_events(event_id) ON DELETE CASCADE
                );
            """)

    def recent(self, platform, scope_id, owner_id, now=None, reply_to_id=''):
        timestamp = (now or utc_now()).isoformat()
        with self.store._connect() as connection:
            if reply_to_id:
                reference = connection.execute("""
                    SELECT t.* FROM outbound_message_links l
                    JOIN task_event_results e ON e.event_id=l.event_id
                    JOIN travel_tasks t ON t.task_id=e.task_id
                    WHERE l.platform=? AND l.scope_id=? AND l.message_id=?
                    AND t.owner_id=? AND t.expires_at > ?
                """, (platform, scope_id, reply_to_id, owner_id, timestamp)).fetchone()
                if reference:
                    return (self._record(reference),)
            rows = connection.execute("""
                SELECT * FROM travel_tasks WHERE platform=? AND scope_id=? AND owner_id=?
                AND expires_at > ? AND status IN ('collecting', 'completed', 'waiting_external')
                ORDER BY updated_at DESC, rowid DESC LIMIT 10
            """, (platform, scope_id, owner_id, timestamp)).fetchall()
        return tuple(self._record(row) for row in rows)

    def has_history(self, platform, scope_id, owner_id):
        with self.store._connect() as connection:
            return connection.execute('SELECT 1 FROM travel_tasks WHERE platform=? AND scope_id=? AND owner_id=? LIMIT 1',
                                      (platform, scope_id, owner_id)).fetchone() is not None

    def command(self, event, claim):
        text = event.content.strip()
        if text not in {'查看任务', '查看任务进度', '我的任务', '取消当前任务', '退出当前任务'}:
            return None
        tasks = self.recent(event.platform, event.storage_scope_id, event.sender_id, reply_to_id=event.reply_to_id)
        waiting = [task for task in tasks if task.status in {'collecting', 'waiting_external'}]
        if text in {'查看任务', '查看任务进度', '我的任务'}:
            if not tasks:
                return '当前没有近期任务。已设置的提醒请说“查看我的提醒”。'
            labels = {'weather': '天气查询', 'forecast': '天气预报', 'reminder': '提醒设置', 'booking_reminder': '开约提醒',
                      'booking_policy_query': '预约规则查询', 'places': '地点查询', 'transit': '公共交通查询', 'walking': '步行路线',
                      'route': '驾车路线', 'traffic': '路况查询', 'itinerary': '行程规划', 'scheduled_query': '定时查询', 'policy_watch': '规则监测'}
            states = {'collecting': '等待补充信息', 'waiting_external': '等待外部信息', 'completed': '已完成'}
            return '\n'.join(f"{labels.get(task.task_type, task.task_type)}：{states.get(task.status, task.status)}；{task.initial_request[:120]}" for task in tasks)
        if len(waiting) != 1:
            return '请回复要结束的那条任务消息后发送“取消当前任务”。已创建的提醒请使用“取消提醒”。'
        task = waiting[0]
        update = TaskUpdate(task.task_type, 'cancelled', task.initial_request, task.slots,
                            task_id=task.task_id, expected_version=task.version)
        reply = '已结束当前待补充任务。已经创建的提醒仍保留，需要时请单独取消提醒。'
        self.prepare_result(event, claim, update, reply)
        return reply

    @staticmethod
    def _record(row):
        return TaskRecord(row['task_id'], row['task_type'], row['status'], row['initial_request'],
            json.loads(row['slots_json']), tuple(json.loads(row['missing_slots_json'])),
            row['version'], row['expires_at'], row['last_event_id'])

    def apply(self, connection, event, update: TaskUpdate, now):
        existing = connection.execute('SELECT task_id FROM task_event_results WHERE event_id=?',
                                      (event.event_key,)).fetchone()
        if existing:
            return existing['task_id']
        timestamp = now.isoformat()
        expiry = (now + TASK_TTL).isoformat()
        task_id = update.task_id or 'T-' + uuid.uuid4().hex[:16]
        if update.task_id:
            cursor = connection.execute("""
                UPDATE travel_tasks SET task_type=?, status=?, slots_json=?, missing_slots_json=?,
                    initial_request=?, version=version+1, last_event_id=?, updated_at=?, expires_at=?
                WHERE task_id=? AND platform=? AND scope_id=? AND owner_id=? AND version=?
                    AND expires_at > ? AND status != 'cancelled'
            """, (update.task_type, update.status, json.dumps(update.slots, ensure_ascii=False),
                  json.dumps(update.missing_slots), update.initial_request, event.event_key,
                  timestamp, expiry, task_id, event.platform, event.storage_scope_id,
                  event.sender_id, update.expected_version, timestamp))
            if cursor.rowcount != 1:
                raise TaskConflict('任务已被另一条消息更新或已过期，请查看任务后重试。')
            version = update.expected_version + 1
        else:
            connection.execute("""
                INSERT INTO travel_tasks (task_id, platform, scope_id, owner_id, task_type,
                    status, initial_request, slots_json, missing_slots_json, source_event_id,
                    last_event_id, created_at, updated_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (task_id, event.platform, event.storage_scope_id, event.sender_id, update.task_type,
                  update.status, update.initial_request, json.dumps(update.slots, ensure_ascii=False),
                  json.dumps(update.missing_slots), event.event_key, event.event_key,
                  timestamp, timestamp, expiry))
            version = 1
        connection.execute('INSERT INTO task_event_results VALUES (?, ?, ?)',
                           (event.event_key, task_id, version))
        return task_id

    def prepare_result(self, event, claim, update, reply):
        now = utc_now()
        with self.store._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT status, claim_token FROM processed_events WHERE event_id=?',
                                     (event.event_key,)).fetchone()
            if not row or row['status'] != 'processing' or row['claim_token'] != claim.claim_token:
                raise TaskConflict('事件处理权已变化，请稍后重试。')
            self.apply(connection, event, update, now)
            connection.execute("""
                UPDATE processed_events SET prepared_reply=?, prepared_memory_content=?, updated_at=?
                WHERE event_id=? AND claim_token=?
            """, (reply, event.content, now.isoformat(), event.event_key, claim.claim_token))
