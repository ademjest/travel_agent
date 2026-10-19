"""Versioned, leased derived memory with publication-time source validation."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import copy
import uuid
from core.conversation_summary import render_summary
from core.summary_contract import PROMPT_VERSION

FORMAT_VERSION = 3
GENERATION_LEASE_SECONDS = 330  # gateway queue <=180s + network <=90s + publication margin


def source_hash(turns):
    return hashlib.sha256(json.dumps(list(turns), ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class ConversationSummaryRepository:
    def __init__(self, store):
        self.store = store
        with store._connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS conversation_summaries (
                platform TEXT NOT NULL, scope_id TEXT NOT NULL, owner_id TEXT NOT NULL,
                version INTEGER NOT NULL, covered_turn_id INTEGER NOT NULL,
                source_hash TEXT NOT NULL, summary TEXT NOT NULL, prompt_version TEXT NOT NULL,
                model_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(platform,scope_id,owner_id))''')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(conversation_summaries)')}
            fields = {'format_version': 'INTEGER NOT NULL DEFAULT 1',
                      'generation': 'INTEGER NOT NULL DEFAULT 1',
                      'items_json': "TEXT NOT NULL DEFAULT '[]'",
                      'dependency_hash': "TEXT NOT NULL DEFAULT ''",
                      'progress_json': "TEXT NOT NULL DEFAULT '{}'",
                      'lease_token': 'TEXT', 'lease_until': 'TEXT', 'retry_after': 'TEXT',
                      'prompt_hash': "TEXT NOT NULL DEFAULT ''"}
            for key, definition in fields.items():
                if key not in columns:
                    db.execute(f'ALTER TABLE conversation_summaries ADD COLUMN {key} {definition}')
            db.execute("UPDATE conversation_summaries SET status='stale',summary='' WHERE format_version<3")

    @staticmethod
    def identity(event):
        return event.platform, event.storage_scope_id, event.sender_id

    def _check(self, event, db):
        lifecycle = getattr(self.store, 'web_lifecycle', None)
        if event.platform == 'web' and lifecycle:
            lifecycle.check(event.scope_id, db)

    def dependencies(self, event, db):
        queries = [
            ('SELECT version,enabled FROM preference_profiles WHERE platform=? AND owner=? AND scope=?',
             (event.platform,event.sender_id,'' if event.platform=='web' else event.scope_id)),
            ('SELECT trip_id,version,status FROM trips WHERE platform=? AND scope_id=? AND owner_id=? ORDER BY trip_id', self.identity(event)),
            ('SELECT artifact_id,version,status FROM planning_artifacts WHERE platform=? AND scope_id=? AND owner_id=? ORDER BY artifact_id', self.identity(event)),
            ('SELECT id,sha256,summary FROM documents WHERE group_openid=? ORDER BY id', (event.storage_scope_id,)),
            ('SELECT destination,source_event,updated_at FROM research_contexts WHERE platform=? AND scope=? AND owner=?',
             (event.platform,event.scope_id,event.sender_id)),
        ]
        values = [tuple(tuple(row) for row in db.execute(sql, params)) for sql, params in queries]
        return source_hash(values)

    def metadata(self, event):
        with self.store._connect() as db:
            self._check(event, db)
            row = db.execute('SELECT * FROM conversation_summaries WHERE platform=? AND scope_id=? AND owner_id=?',
                             self.identity(event)).fetchone()
        return dict(row) if row else None

    def latest(self, event):
        with self.store._connect() as db:
            self._check(event, db)
            row = db.execute('SELECT * FROM conversation_summaries WHERE platform=? AND scope_id=? AND owner_id=?',
                             self.identity(event)).fetchone()
            if not row or row['status'] != 'active' or row['format_version'] != FORMAT_VERSION:
                return None
            if row['dependency_hash'] != self.dependencies(event, db):
                return None
            if not self._validated_items(event, db, json.loads(row['items_json'])):
                return None
        return dict(row)

    def _validated_items(self, event, db, items):
        """Validate all accumulated references, including unchanged prior batch items."""
        for item in items:
            for ref in item['sources']:
                original = db.execute('SELECT user_content,user_msg_id FROM conversation_turns WHERE id=? AND group_openid=? AND member_openid=?',
                                      (ref['message_id'],event.storage_scope_id,event.sender_id)).fetchone()
                if not original or ref['quote'] not in original['user_content']:
                    return False
                if self._excluded(event,db,original['user_msg_id']):
                    return False
                digest = source_hash([original['user_content']])
                if ref.get('source_hash') and ref['source_hash'] != digest:
                    return False
                ref['source_hash'] = digest
        return True

    @staticmethod
    def _excluded(event,db,event_key):
        return db.execute('SELECT 1 FROM summary_excluded_events WHERE platform=? AND owner=? AND scope=? AND event_key=?',
                          (event.platform,event.sender_id,'' if event.platform=='web' else event.scope_id,event_key)).fetchone() is not None

    def pending_turns(self, event, after_id=0, limit=100):
        if type(limit) is not int or not 1 <= limit <= 101:
            raise ValueError('摘要来源读取上限无效。')
        with self.store._connect() as db:
            self._check(event, db)
            rows = db.execute('''SELECT id,user_content,assistant_content,created_at,user_msg_id FROM conversation_turns
                WHERE group_openid=? AND member_openid=? AND id>? ORDER BY id LIMIT ?''',
                (event.storage_scope_id,event.sender_id,after_id,limit)).fetchall()
            values = [dict(row) for row in rows]
            for row in values:
                row['summary_excluded'] = self._excluded(event,db,row.pop('user_msg_id'))
        return tuple(values)

    def claim(self, event, previous, now=None):
        now = now or datetime.now(timezone.utc)
        timestamp = now.isoformat()
        token = uuid.uuid4().hex
        with self.store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self._check(event, db)
            db.execute('''INSERT OR IGNORE INTO conversation_summaries
                (platform,scope_id,owner_id,version,covered_turn_id,source_hash,summary,prompt_version,model_id,status,created_at,updated_at)
                VALUES (?,?,?,0,0,'','','','','stale',?,?)''', (*self.identity(event),timestamp,timestamp))
            row = db.execute('SELECT * FROM conversation_summaries WHERE platform=? AND scope_id=? AND owner_id=?',
                             self.identity(event)).fetchone()
            if ((row['lease_until'] and row['lease_until'] > timestamp)
                    or (row['retry_after'] and row['retry_after'] > timestamp)):
                return None
            expected = previous['version'] if previous else row['version']
            if expected != row['version']:
                return None
            db.execute('''UPDATE conversation_summaries SET lease_token=?,lease_until=?
                WHERE platform=? AND scope_id=? AND owner_id=?''',
                (token,(now+timedelta(seconds=GENERATION_LEASE_SECONDS)).isoformat(),*self.identity(event)))
            return {'token':token, 'generation':row['generation'], 'version':row['version'],
                    'dependencies':self.dependencies(event,db)}

    def release(self, event, claim, failed=False):
        with self.store._connect() as db:
            db.execute('''UPDATE conversation_summaries SET lease_token=NULL,lease_until=NULL,retry_after=?
                WHERE platform=? AND scope_id=? AND owner_id=? AND lease_token=?''',
                ((datetime.now(timezone.utc)+timedelta(seconds=60)).isoformat() if failed else None,
                 *self.identity(event),claim['token']))

    def publish(self, event, claim, turns, items, covered, progress, model_id, *,
                prompt_version=PROMPT_VERSION, prompt_hash=''):
        now = datetime.now(timezone.utc).isoformat()
        with self.store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self._check(event, db)
            row = db.execute('SELECT * FROM conversation_summaries WHERE platform=? AND scope_id=? AND owner_id=?',
                             self.identity(event)).fetchone()
            if (not row or row['lease_token'] != claim['token'] or row['lease_until'] <= now
                    or row['generation'] != claim['generation'] or row['version'] != claim['version']
                    or self.dependencies(event,db) != claim['dependencies']):
                return None
            current = []
            for turn in turns:
                source = db.execute('''SELECT id,user_content,assistant_content,created_at FROM conversation_turns
                    WHERE id=? AND group_openid=? AND member_openid=?''',
                    (turn['id'],event.storage_scope_id,event.sender_id)).fetchone()
                if not source:
                    return None
                current.append({**dict(source),'summary_excluded':turn.get('summary_excluded',False)})
            if source_hash(current) != source_hash(turns):
                return None
            if row['status'] == 'active' and not self._validated_items(event, db, json.loads(row['items_json'])):
                return None
            items = copy.deepcopy(items)
            if not self._validated_items(event, db, items):
                return None
            summary = render_summary(items)
            db.execute('''UPDATE conversation_summaries SET version=version+1,status='active',format_version=3,
                covered_turn_id=?,source_hash=?,summary=?,items_json=?,dependency_hash=?,progress_json=?,
                prompt_version=?,prompt_hash=?,model_id=?,updated_at=?,lease_token=NULL,lease_until=NULL,retry_after=NULL
                WHERE platform=? AND scope_id=? AND owner_id=? AND lease_token=?''',
                (covered,source_hash(turns),summary,json.dumps(items,ensure_ascii=False),claim['dependencies'],
                 json.dumps(progress),prompt_version,prompt_hash,model_id,now,*self.identity(event),claim['token']))
        return self.latest(event)
