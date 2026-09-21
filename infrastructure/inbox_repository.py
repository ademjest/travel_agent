from datetime import datetime, timedelta, timezone
from contextlib import nullcontext
import json
import uuid


JOB_LEASE_SECONDS = 120
JOB_DEADLINE_SECONDS = 300
MAX_JOB_ATTEMPTS = 3


class InboxRepository:
    def __init__(self, store):
        self.store = store
        with store._connect() as connection:
            connection.executescript('''
                CREATE TABLE IF NOT EXISTS inbox_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT NOT NULL UNIQUE,
                    platform TEXT NOT NULL, scope_id TEXT NOT NULL, owner_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    capture_state TEXT NOT NULL DEFAULT 'ready', capture_token TEXT,
                    capture_lease_until TEXT, capture_attempts INTEGER NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0, claim_token TEXT, lease_until TEXT, deadline_at TEXT,
                    next_attempt_at TEXT NOT NULL, result_json TEXT, last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_inbox_due ON inbox_jobs(status, next_attempt_at, id);
                CREATE INDEX IF NOT EXISTS idx_inbox_owner ON inbox_jobs(platform, scope_id, owner_id, id);
                CREATE TABLE IF NOT EXISTS inbox_assets (
                    event_key TEXT NOT NULL, attachment_index INTEGER NOT NULL,
                    file_path TEXT NOT NULL, content_type TEXT NOT NULL, sha256 TEXT NOT NULL,
                    byte_size INTEGER NOT NULL, PRIMARY KEY(event_key, attachment_index),
                    FOREIGN KEY(event_key) REFERENCES inbox_jobs(event_key) ON DELETE CASCADE
                );
            ''')
            columns = {row['name'] for row in connection.execute('PRAGMA table_info(inbox_jobs)')}
            if 'priority' not in columns:
                connection.execute('ALTER TABLE inbox_jobs ADD COLUMN priority INTEGER NOT NULL DEFAULT 0')

    def submit(self, event_key, platform, scope_id, owner_id, payload, has_assets=False, now=None, *, priority=0, connection=None):
        now = (now or datetime.now(timezone.utc)).isoformat()
        own_connection = connection is None
        with (self.store._connect() if own_connection else nullcontext(connection)) as connection:
            if own_connection:
                connection.execute('BEGIN IMMEDIATE')
            existing = connection.execute('SELECT id, status FROM inbox_jobs WHERE event_key=?', (event_key,)).fetchone()
            if existing:
                return dict(existing)
            count = connection.execute("SELECT count(*) FROM inbox_jobs WHERE status IN ('pending','running','retry')").fetchone()[0]
            if count >= 1000 and not priority:
                raise ValueError('接收队列暂满，请稍后重试。')
            cursor = connection.execute('''INSERT INTO inbox_jobs
                (event_key, platform, scope_id, owner_id, payload_json, capture_state, next_attempt_at, created_at, updated_at, priority)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (event_key, platform, scope_id, owner_id, json.dumps(payload, ensure_ascii=False),
                 'pending' if has_assets else 'ready', now, now, now, priority))
            return {'id': int(cursor.lastrowid), 'status': 'pending'}

    def claim(self, now=None, *, deadline_seconds=JOB_DEADLINE_SECONDS, job_id=None, lane=None, platform=None):
        now = now or datetime.now(timezone.utc)
        timestamp = now.isoformat()
        token = uuid.uuid4().hex
        lane_filter = {None: '', 'normal': "AND coalesce(json_extract(j.payload_json, '$.post_type'),'') NOT IN ('scheduled_query','policy_watch')",
                       'scheduled': "AND json_extract(j.payload_json, '$.post_type') IN ('scheduled_query','policy_watch')"}[lane]
        with self.store._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute(f'''SELECT j.* FROM inbox_jobs j
                WHERE ((j.status IN ('pending','retry') AND j.next_attempt_at <= ?)
                       OR (j.status='running' AND j.lease_until <= ?))
                  AND j.capture_state IN ('ready','deferred')
                  {lane_filter}
                  AND (? IS NULL OR j.platform=?)
                  AND (? IS NULL OR j.id=?)
                  AND (j.priority=1 OR json_extract(j.payload_json, '$.post_type') IN ('scheduled_query','policy_watch') OR NOT EXISTS (SELECT 1 FROM inbox_jobs earlier
                    WHERE earlier.platform=j.platform AND earlier.scope_id=j.scope_id AND earlier.owner_id=j.owner_id
                    AND coalesce(json_extract(earlier.payload_json, '$.post_type'),'') NOT IN ('scheduled_query','policy_watch')
                    AND earlier.id < j.id AND earlier.status IN ('pending','running','retry')))
                ORDER BY j.priority DESC, j.id LIMIT 1''', (timestamp, timestamp, platform, platform, job_id, job_id)).fetchone()
            if not row:
                return None
            if row['status'] == 'running':
                # New job ownership fences the old thread before its event lease is replaced.
                connection.execute("UPDATE processed_events SET lease_expires_at=? WHERE event_id=? AND status='processing'",
                                   (timestamp, row['event_key']))
            connection.execute('''UPDATE inbox_jobs SET status='running', claim_token=?, attempts=attempts+1,
                lease_until=?, deadline_at=?, updated_at=? WHERE id=?''',
                (token, (now+timedelta(seconds=JOB_LEASE_SECONDS)).isoformat(),
                 (now+timedelta(seconds=deadline_seconds)).isoformat(), timestamp, row['id']))
            claimed = dict(connection.execute('SELECT * FROM inbox_jobs WHERE id=?', (row['id'],)).fetchone())
        claimed['payload'] = json.loads(claimed.pop('payload_json'))
        return claimed

    def renew(self, job_id, token, now=None):
        now = now or datetime.now(timezone.utc)
        with self.store._connect() as connection:
            cursor = connection.execute('''UPDATE inbox_jobs SET lease_until=min(?, deadline_at), updated_at=?
                WHERE id=? AND status='running' AND claim_token=? AND deadline_at>? AND lease_until>?''',
                ((now+timedelta(seconds=JOB_LEASE_SECONDS)).isoformat(), now.isoformat(), job_id, token,
                 now.isoformat(), now.isoformat()))
        return cursor.rowcount == 1

    def finish(self, job, result, now=None):
        timestamp = (now or datetime.now(timezone.utc)).isoformat()
        with self.store._connect() as connection:
            cursor = connection.execute('''UPDATE inbox_jobs SET status='completed', result_json=?, updated_at=?,
                claim_token=NULL, lease_until=NULL WHERE id=? AND status='running' AND claim_token=?''',
                (json.dumps(result, ensure_ascii=False), timestamp, job['id'], job['claim_token']))
        return cursor.rowcount == 1

    def fail(self, job, error, *, busy=False, now=None):
        now = now or datetime.now(timezone.utc)
        terminal = not busy and job['attempts'] >= MAX_JOB_ATTEMPTS
        status = 'failed' if terminal else 'retry'
        with self.store._connect() as connection:
            cursor = connection.execute('''UPDATE inbox_jobs SET status=?, last_error=?, next_attempt_at=?,
                updated_at=?, claim_token=NULL, lease_until=NULL, attempts=attempts-?
                WHERE id=? AND status='running' AND claim_token=?''',
                (status, str(error)[:500], (now+timedelta(seconds=5 if busy else min(60, 5*job['attempts']))).isoformat(),
                 now.isoformat(), int(busy), job['id'], job['claim_token']))
            if cursor.rowcount and not busy:
                connection.execute("UPDATE processed_events SET lease_expires_at=? WHERE event_id=? AND status='processing'",
                                   (now.isoformat(), job['event_key']))
        return status if cursor.rowcount else 'revoked'

    def get(self, event_key):
        with self.store._connect() as connection:
            row = connection.execute('SELECT * FROM inbox_jobs WHERE event_key=?', (event_key,)).fetchone()
        return dict(row) if row else None

    def active_for_owner(self, platform, scope_id, owner_id):
        with self.store._connect() as connection:
            return tuple(dict(row) for row in connection.execute('''SELECT id, event_key, status, capture_state,
                payload_json, created_at, attempts FROM inbox_jobs WHERE platform=? AND scope_id=? AND owner_id=?
                AND status IN ('pending','running','retry') ORDER BY id''', (platform, scope_id, owner_id)).fetchall())

    def cancel(self, event_key, platform, scope_id, owner_id):
        now = datetime.now(timezone.utc).isoformat()
        with self.store._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('''SELECT * FROM inbox_jobs WHERE event_key=? AND platform=? AND scope_id=?
                AND owner_id=? AND status IN ('pending','running','retry')''', (event_key, platform, scope_id, owner_id)).fetchone()
            if not row:
                return False
            result = connection.execute('SELECT prepared_reply FROM processed_events WHERE event_id=?', (event_key,)).fetchone()
            if (result and result['prepared_reply'] is not None) or connection.execute('SELECT 1 FROM outbox_messages WHERE event_id=?', (event_key,)).fetchone():
                return False
            connection.execute("UPDATE inbox_jobs SET status='cancelled', claim_token=NULL, lease_until=NULL, capture_token=NULL, updated_at=? WHERE id=?", (now, row['id']))
            connection.execute('''INSERT INTO processed_events (event_id, status, created_at, updated_at, last_error)
                VALUES (?, 'completed', ?, ?, 'cancelled by owner')
                ON CONFLICT(event_id) DO UPDATE SET status='completed', claim_token=NULL, lease_expires_at=NULL,
                    updated_at=excluded.updated_at, last_error=excluded.last_error''', (event_key, now, now))
        return True

    def claim_capture(self, now=None, *, platform=None):
        now = now or datetime.now(timezone.utc)
        with self.store._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('''SELECT * FROM inbox_jobs WHERE status IN ('pending','retry')
                AND (capture_state='pending' OR (capture_state='capturing' AND capture_lease_until<=?))
                AND next_attempt_at <= ? AND (? IS NULL OR platform=?) ORDER BY id LIMIT 1''',
                (now.isoformat(), now.isoformat(), platform, platform)).fetchone()
            if not row:
                return None
            token = uuid.uuid4().hex
            connection.execute('''UPDATE inbox_jobs SET capture_state='capturing', capture_token=?, capture_attempts=capture_attempts+1,
                capture_lease_until=?, updated_at=? WHERE id=?''',
                (token, (now+timedelta(seconds=180)).isoformat(), now.isoformat(), row['id']))
            result = dict(row)
            result.update(capture_token=token, capture_attempts=row['capture_attempts']+1)
            result['payload'] = json.loads(result.pop('payload_json'))
            return result

    def finish_capture(self, job, assets, *, deferred=False):
        now = datetime.now(timezone.utc).isoformat()
        with self.store._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute("SELECT 1 FROM inbox_jobs WHERE id=? AND capture_state='capturing' AND capture_token=? AND status IN ('pending','retry') AND capture_lease_until>?",
                                     (job['id'], job['capture_token'], now)).fetchone()
            if not row:
                return False
            for asset in assets:
                connection.execute('INSERT OR REPLACE INTO inbox_assets VALUES (?, ?, ?, ?, ?, ?)',
                    (job['event_key'], asset['index'], asset['path'], asset['content_type'], asset['sha256'], asset['size']))
            connection.execute('''UPDATE inbox_jobs SET capture_state=?, capture_token=NULL, capture_lease_until=NULL, updated_at=? WHERE id=?''',
                               ('deferred' if deferred else 'ready', now, job['id']))
        return True

    def record_asset(self, job, asset):
        with self.store._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute("SELECT 1 FROM inbox_jobs WHERE id=? AND capture_state='capturing' AND capture_token=? AND status IN ('pending','retry') AND capture_lease_until>?",
                                     (job['id'], job['capture_token'], datetime.now(timezone.utc).isoformat())).fetchone()
            if not row:
                return False
            connection.execute('INSERT OR REPLACE INTO inbox_assets VALUES (?, ?, ?, ?, ?, ?)',
                (job['event_key'], asset['index'], asset['path'], asset['content_type'], asset['sha256'], asset['size']))
        return True

    def renew_capture(self, job):
        now = datetime.now(timezone.utc)
        with self.store._connect() as connection:
            cursor = connection.execute("UPDATE inbox_jobs SET capture_lease_until=? WHERE id=? AND capture_state='capturing' AND capture_token=? AND status IN ('pending','retry') AND capture_lease_until>?",
                ((now+timedelta(seconds=180)).isoformat(), job['id'], job['capture_token'], now.isoformat()))
        return cursor.rowcount == 1

    def fail_capture(self, job, error):
        now = datetime.now(timezone.utc)
        terminal = job['capture_attempts'] >= 3
        with self.store._connect() as connection:
            cursor = connection.execute('''UPDATE inbox_jobs SET capture_state=?, capture_token=NULL, last_error=?,
                next_attempt_at=?, updated_at=? WHERE id=? AND capture_token=? AND status IN ('pending','retry')''',
                ('failed' if terminal else 'pending', str(error)[:500], (now+timedelta(seconds=5)).isoformat(),
                 now.isoformat(), job['id'], job['capture_token']))
            if terminal and cursor.rowcount:
                connection.execute("UPDATE inbox_jobs SET status='failed' WHERE id=? AND capture_state='failed'", (job['id'],))
        return terminal and cursor.rowcount == 1

    def assets(self, event_key):
        with self.store._connect() as connection:
            return tuple(dict(row) for row in connection.execute('SELECT * FROM inbox_assets WHERE event_key=? ORDER BY attachment_index', (event_key,)).fetchall())

    def health(self):
        with self.store._connect() as connection:
            return {row['status']: row['count'] for row in connection.execute('SELECT status, count(*) AS count FROM inbox_jobs GROUP BY status').fetchall()}

    def release(self, job):
        now = datetime.now(timezone.utc).isoformat()
        with self.store._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            cursor = connection.execute("UPDATE inbox_jobs SET status='retry', claim_token=NULL, lease_until=NULL, next_attempt_at=?, updated_at=?, attempts=max(0, attempts-1) WHERE id=? AND status='running' AND claim_token=?",
                (now, now, job['id'], job['claim_token']))
            if cursor.rowcount:
                connection.execute("UPDATE processed_events SET lease_expires_at=? WHERE event_id=? AND status='processing'", (now, job['event_key']))

    def recovery_claim(self, job):
        now = datetime.now(timezone.utc)
        with self.store._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            event = connection.execute('SELECT * FROM processed_events WHERE event_id=?', (job['event_key'],)).fetchone()
            if not event or event['prepared_reply'] is None:
                return None
            token = uuid.uuid4().hex
            cursor = connection.execute("UPDATE inbox_jobs SET claim_token=?, lease_until=?, deadline_at=? WHERE id=? AND status='running' AND claim_token=?",
                (token, (now+timedelta(seconds=30)).isoformat(), (now+timedelta(seconds=30)).isoformat(), job['id'], job['claim_token']))
            if not cursor.rowcount:
                return None
            return token, dict(event)

    def purge(self, cutoff):
        with self.store._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            paths = [row['file_path'] for row in connection.execute('''SELECT a.file_path FROM inbox_assets a
                JOIN inbox_jobs j ON j.event_key=a.event_key WHERE j.status IN ('completed','failed','cancelled','blocked') AND j.updated_at < ?''',
                (cutoff.isoformat(),)).fetchall()]
            deleted = connection.execute("DELETE FROM inbox_jobs WHERE status IN ('completed','failed','cancelled','blocked') AND updated_at < ?",
                                         (cutoff.isoformat(),)).rowcount
            retained = {row['file_path'] for row in connection.execute('SELECT file_path FROM inbox_assets').fetchall()}
        return deleted, paths, retained

    def release_capture(self, job):
        with self.store._connect() as connection:
            connection.execute("UPDATE inbox_jobs SET capture_state='pending', capture_token=NULL, capture_lease_until=NULL, capture_attempts=max(0, capture_attempts-1) WHERE id=? AND capture_state='capturing' AND capture_token=? AND status IN ('pending','retry')",
                               (job['id'], job['capture_token']))

    def capture_is_current(self, job):
        with self.store._connect() as connection:
            return connection.execute("SELECT 1 FROM inbox_jobs WHERE id=? AND capture_state='capturing' AND capture_token=? AND status IN ('pending','retry') AND capture_lease_until>?",
                (job['id'], job['capture_token'], datetime.now(timezone.utc).isoformat())).fetchone() is not None

    def block(self, job, reason):
        with self.store._connect() as connection:
            connection.execute('''UPDATE inbox_jobs SET status='blocked', last_error=?, claim_token=NULL,
                lease_until=NULL, capture_token=NULL, updated_at=? WHERE id=?
                AND ((status='running' AND claim_token=?) OR (capture_state='capturing' AND capture_token=?))''',
                (reason, datetime.now(timezone.utc).isoformat(), job['id'], job.get('claim_token'), job.get('capture_token')))

    def bind_control_target(self, event_key, target):
        with self.store._connect() as connection:
            connection.execute('''UPDATE inbox_jobs SET payload_json=json_set(payload_json, '$._control_target', ?)
                WHERE event_key=? AND status='running' AND json_type(payload_json, '$._control_target') IS NULL''',
                (target, event_key))
