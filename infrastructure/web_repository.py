from datetime import datetime, timedelta, timezone
import hashlib
import json
import secrets
import uuid

from core.chat_transport import ChatEvent
from core.web_lifecycle import ConversationDeleted, WebLifecycle


OWNER = 'local-owner'


def timestamp():
    return datetime.now(timezone.utc).isoformat()


class WebConflict(ValueError):
    pass


class WebRepository:
    def __init__(self, store):
        self.store = store
        self.root = store.database_path.parent.resolve()
        with store._connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS web_conversations (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS web_sessions (
                    token_hash TEXT PRIMARY KEY, csrf TEXT NOT NULL, expires_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS web_uploads (
                    id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES web_conversations(id),
                    filename TEXT NOT NULL, relative_path TEXT NOT NULL, content_type TEXT NOT NULL,
                    size INTEGER NOT NULL, sha256 TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS web_requests (
                    event_key TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, request_hash TEXT NOT NULL,
                    content TEXT NOT NULL, uploads_json TEXT NOT NULL, created_at TEXT NOT NULL,
                    FOREIGN KEY(event_key) REFERENCES inbox_jobs(event_key) ON DELETE CASCADE);
                CREATE TABLE IF NOT EXISTS web_deliveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, delivery_key TEXT NOT NULL UNIQUE,
                    conversation_id TEXT NOT NULL REFERENCES web_conversations(id), reply_to_id TEXT NOT NULL,
                    kind TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL, read_at TEXT);
                CREATE INDEX IF NOT EXISTS web_deliveries_conversation ON web_deliveries(conversation_id,id);
            ''')
        self.lifecycle = WebLifecycle(store)
        store.web_lifecycle = self.lifecycle

    def session(self, token):
        if not token or len(token) > 128:
            return None
        with self.store._connect() as db:
            row = db.execute('SELECT csrf FROM web_sessions WHERE token_hash=? AND expires_at>?',
                             (hashlib.sha256(token.encode()).hexdigest(), timestamp())).fetchone()
        return row['csrf'] if row else None

    def new_session(self):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.store._connect() as db:
            db.execute('DELETE FROM web_sessions WHERE expires_at<=?', (timestamp(),))
            db.execute('INSERT INTO web_sessions VALUES (?, ?, ?)',
                       (hashlib.sha256(token.encode()).hexdigest(), csrf,
                        (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()))
        return token, csrf

    def conversation(self, identity):
        with self.store._connect() as db:
            return self.lifecycle.check(identity, db)

    def allows(self, identity):
        try:
            self.conversation(identity)
            return True
        except (LookupError, ConversationDeleted):
            return False

    def conversations(self):
        with self.store._connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM web_conversations c WHERE NOT EXISTS (SELECT 1 FROM web_conversation_deletions d WHERE d.conversation_id=c.id) ORDER BY updated_at DESC LIMIT 200')]

    def create_conversation(self, title='新的旅行'):
        identity, now = uuid.uuid4().hex, timestamp()
        with self.store._connect() as db:
            db.execute('INSERT INTO web_conversations VALUES (?, ?, ?, ?)', (identity, title, now, now))
        return self.conversation(identity)

    def rename(self, identity, title):
        self.conversation(identity)
        with self.store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self.lifecycle.check(identity, db)
            db.execute('UPDATE web_conversations SET title=? WHERE id=?', (title, identity))
        return self.conversation(identity)

    def upload(self, identity, conversation_id=None):
        with self.store._connect() as db:
            row = db.execute('SELECT * FROM web_uploads WHERE id=?', (identity,)).fetchone()
        if not row or (conversation_id is not None and row['conversation_id'] != conversation_id):
            raise LookupError('附件不属于当前会话或已失效。')
        value = dict(row)
        self.conversation(value['conversation_id'])
        path = (self.root / value['relative_path']).resolve()
        if not path.is_relative_to(self.root / 'inbox-assets') or not path.is_file():
            raise LookupError('附件文件不可用，请重新上传。')
        return value

    def save_upload(self, identity, conversation_id, filename, relative_path, content_type, data):
        self.conversation(conversation_id)
        with self.store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self.lifecycle.check(conversation_id, db)
            db.execute('INSERT INTO web_uploads VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                       (identity, conversation_id, filename, relative_path, content_type, len(data),
                        hashlib.sha256(data).hexdigest(), timestamp()))
        return self.upload(identity)

    def accept(self, conversation_id, content, upload_ids, request_id, confirmation=None):
        self.conversation(conversation_id)
        body = {'content': content, 'upload_ids': upload_ids, 'confirmation': confirmation}
        digest = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        event_key = f'web:group:{conversation_id}:{request_id}'
        now = timestamp()
        with self.store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self.lifecycle.check(conversation_id, db)
            previous = db.execute('SELECT request_hash FROM web_requests WHERE event_key=?', (event_key,)).fetchone()
            if previous:
                if previous['request_hash'] != digest:
                    raise WebConflict('相同请求编号对应不同内容，请重新发送。')
                row = db.execute('SELECT id, status FROM inbox_jobs WHERE event_key=?', (event_key,)).fetchone()
                return {'job_id': row['id'], 'status': row['status']}
            uploads = [self.upload(item, conversation_id) for item in upload_ids]
            payload = {'version': 1, 'conversation_id': conversation_id, 'request_id': request_id,
                       'content': content, 'upload_ids': upload_ids, 'time': now, 'confirmation': confirmation}
            from services.scheduled_query_service import scheduled_query_control
            from services.policy_watch_service import watch_control
            priority = int(not uploads and (scheduled_query_control(content) or watch_control(content)
                           or content in {'查看任务进度', '查看任务', '取消正在处理的请求', '取消当前任务'}
                           or content.startswith('取消请求 ')))
            job = self.store.inbox.submit(event_key, 'web', conversation_id, OWNER, payload,
                                         priority=priority, connection=db)
            db.execute('INSERT INTO web_requests VALUES (?, ?, ?, ?, ?, ?)',
                       (event_key, conversation_id, digest, content, json.dumps(upload_ids), now))
            for index, upload in enumerate(uploads):
                db.execute('INSERT INTO inbox_assets VALUES (?, ?, ?, ?, ?, ?)',
                           (event_key, index, str((self.root / upload['relative_path']).resolve()),
                            upload['content_type'], upload['sha256'], upload['size']))
            db.execute('UPDATE web_conversations SET updated_at=?, title=CASE WHEN title=? THEN ? ELSE title END WHERE id=?',
                       (now, '新的旅行', (content or uploads[0]['filename'])[:30], conversation_id))
        return {'job_id': job['id'], 'status': 'accepted'}

    def deliver(self, message):
        self.conversation(message.target_id)
        if not message.delivery_key:
            raise ValueError('Web 投递缺少幂等标识。')
        with self.store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self.lifecycle.check(message.target_id, db)
            db.execute('INSERT OR IGNORE INTO web_deliveries (delivery_key,conversation_id,reply_to_id,kind,content,created_at) VALUES (?,?,?,?,?,?)',
                       (message.delivery_key, message.target_id, message.reply_to_id,
                        message.payload.get('kind', 'reply'), message.payload['text'], timestamp()))
            row = db.execute('SELECT id FROM web_deliveries WHERE delivery_key=?', (message.delivery_key,)).fetchone()
        return f'web-{row["id"]}'

    def messages(self, identity, before=None, limit=60):
        self.conversation(identity)
        with self.store._connect() as db:
            rows = db.execute('''SELECT * FROM (
                SELECT 'u:'||r.event_key AS id, 'user' AS role, 'message' AS kind, r.content, r.created_at,
                    r.uploads_json, j.id AS job_id, j.status AS job_status, '' AS reply_to_id
                FROM web_requests r JOIN inbox_jobs j ON j.event_key=r.event_key WHERE r.conversation_id=?
                UNION ALL
                SELECT 'd:'||id, 'assistant', kind, content, created_at, '[]', NULL, NULL, reply_to_id
                FROM web_deliveries WHERE conversation_id=?
                ) WHERE (? IS NULL OR created_at < ?) ORDER BY created_at DESC,id DESC LIMIT ?''',
                (identity, identity, before, before, limit + 1)).fetchall()
        items = []
        for row in reversed(rows[:limit]):
            item = dict(row)
            item['uploads'] = []
            for upload_id in json.loads(item.pop('uploads_json')):
                try:
                    u = self.upload(upload_id, identity)
                    item['uploads'].append({k: u[k] for k in ('id', 'filename', 'content_type', 'size')})
                except LookupError:
                    item['uploads'].append({'id': upload_id, 'filename': '附件已过期', 'content_type': '', 'size': 0})
            items.append(item)
        return {'items': items, 'has_more': len(rows) > limit,
                'before': items[0]['created_at'] if items else None}

    def job(self, identity):
        with self.store._connect() as db:
            row = db.execute('SELECT id,event_key,scope_id,status,attempts,created_at,updated_at FROM inbox_jobs WHERE id=? AND platform=? AND owner_id=?',
                             (identity, 'web', OWNER)).fetchone()
        if not row:
            raise LookupError('任务不存在。')
        self.conversation(row['scope_id'])
        return dict(row)

    def context(self, identity):
        self.conversation(identity)
        event = ChatEvent('web', 'group', '', identity, OWNER, '')
        tasks = self.store.tasks.recent('web', event.storage_scope_id, OWNER)
        confirmations = []
        for task in tasks:
            if task.status == 'collecting' and 'confirmation' in task.missing_slots:
                command = {'itinerary': '确认行程修改', 'booking_reminder': '按这个规则设置提醒'}.get(task.task_type)
                if command:
                    with self.store._connect() as db:
                        row = db.execute('SELECT prepared_reply FROM processed_events WHERE event_id=?', (task.last_event_id,)).fetchone()
                    confirmations.append({'id': task.task_id, 'version': task.version, 'command': command,
                                          'preview': row['prepared_reply'] if row else task.initial_request,
                                          'expires_at': task.expires_at})
        with self.store._connect() as db:
            jobs = [dict(r) for r in db.execute("SELECT id,status,created_at,updated_at FROM inbox_jobs WHERE platform='web' AND scope_id=? AND owner_id=? AND status IN ('pending','running','retry') ORDER BY id", (identity, OWNER))]
        reservation_plans = self.store.list_reservation_plans_for_creator('web', identity, OWNER)
        reservation_reminders = self.store.list_reservation_reminders('web', identity, OWNER)
        reservations = [{'code': plan.plan_code, 'status': plan.status, 'version': plan.plan_version,
                         'items': [{'name': item.attraction_name, 'visit_date': item.visit_date,
                                    'code': item.public_code,
                                    'reminders': [{'time': r.scheduled_at_utc, 'status': r.status}
                                                  for r in reservation_reminders if r.reservation_item_id == item.item_id]}
                                   for item in plan.items]} for plan in reservation_plans]
        return {'trips': self.store.trips.list_for_owner(event),
                'reminders': self.store.reminders.list_for_owner('web', identity, OWNER),
                'documents': self.store.trips.document_options(event), 'confirmations': confirmations,
                'reservations': reservations,
                'scheduled_queries': self.store.scheduled_queries.list_for_owner(event),
                'policy_watches': self.store.policy_watches.list_for_owner(event), 'jobs': jobs}

    def notifications(self):
        with self.store._connect() as db:
            rows = db.execute("SELECT d.*,c.title FROM web_deliveries d JOIN web_conversations c ON c.id=d.conversation_id WHERE d.kind='notification' AND NOT EXISTS (SELECT 1 FROM web_conversation_deletions x WHERE x.conversation_id=c.id) ORDER BY d.id DESC LIMIT 100").fetchall()
        return [dict(row) for row in rows]

    def read_notification(self, identity):
        with self.store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            notification = db.execute('SELECT conversation_id FROM web_deliveries WHERE id=?', (identity,)).fetchone()
            if notification:
                self.lifecycle.check(notification['conversation_id'], db)
            result = db.execute("UPDATE web_deliveries SET read_at=? WHERE id=? AND kind='notification'", (timestamp(), identity))
        if not result.rowcount:
            raise LookupError('通知不存在。')

    def clean_uploads(self):
        cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        with self.lifecycle.file_lock:
            with self.store._connect() as db:
                rows = db.execute('''SELECT * FROM web_uploads u WHERE created_at<? AND NOT EXISTS (
                    SELECT 1 FROM web_requests r, json_each(r.uploads_json) a WHERE a.value=u.id)
                    AND NOT EXISTS (SELECT 1 FROM web_conversation_deletions d WHERE d.conversation_id=u.conversation_id)''', (cutoff,)).fetchall()
                paths = [row['relative_path'] for row in rows]
                db.executemany('DELETE FROM web_uploads WHERE id=?', [(row['id'],) for row in rows])
                pending = db.execute('''SELECT path FROM web_file_intents f WHERE created_at<? AND NOT EXISTS
                    (SELECT 1 FROM web_conversation_deletions d WHERE d.conversation_id=f.conversation_id)''', (cutoff,)).fetchall()
                paths.extend(row['path'] for row in pending)
                db.execute('''DELETE FROM web_file_intents WHERE created_at<? AND NOT EXISTS
                    (SELECT 1 FROM web_conversation_deletions d WHERE d.conversation_id=web_file_intents.conversation_id)''', (cutoff,))
            for path in paths:
                try:
                    self.lifecycle.remove_unreferenced(path)
                except (OSError, ValueError):
                    pass
