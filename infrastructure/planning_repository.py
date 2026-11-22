"""Persistent conversation-level planning proposals, separate from saved trips."""
from datetime import datetime, timezone
import hashlib
import json

CAPTURE_VERSION = 2




class PlanningConflict(ValueError):
    pass


class PlanningRepository:
    def __init__(self, store):
        self.store = store
        with store._connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS planning_artifacts (
                    artifact_id TEXT PRIMARY KEY, platform TEXT NOT NULL, scope_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL, ordinal INTEGER NOT NULL, kind TEXT NOT NULL,
                    source_event_id TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
                    destination TEXT NOT NULL, duration_options_json TEXT NOT NULL,
                    summary TEXT NOT NULL, content TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
                    version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS idx_planning_artifacts_owner
                ON planning_artifacts(platform,scope_id,owner_id,ordinal);
                CREATE TABLE IF NOT EXISTS planning_history_watermarks (
                    platform TEXT NOT NULL,scope_id TEXT NOT NULL,owner_id TEXT NOT NULL,last_turn_id INTEGER NOT NULL,
                    PRIMARY KEY(platform,scope_id,owner_id));
            ''')
            db.execute('BEGIN IMMEDIATE')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(planning_artifacts)')}
            if 'capture_version' not in columns:
                db.execute('ALTER TABLE planning_artifacts ADD COLUMN capture_version INTEGER NOT NULL DEFAULT 1')
            if 'validation_reason' not in columns:
                db.execute("ALTER TABLE planning_artifacts ADD COLUMN validation_reason TEXT NOT NULL DEFAULT ''")

    @staticmethod
    def _id(event_key):
        return 'P-' + hashlib.sha256(event_key.encode()).hexdigest()[:12]

    def capture(self, event, assistant_content, *, kind, destination, duration_options,
                title, summary, capture_version):
        """Persist prepared proposal metadata; never interpret conversation text."""
        if kind not in {'exploratory', 'research_backed'} or not isinstance(assistant_content, str):
            raise ValueError('规划方案类型或正文无效。')
        if not destination or not title:
            raise ValueError('方案元数据不完整。')
        options = duration_options
        artifact_id = self._id(event.event_key)
        now = datetime.now(timezone.utc).isoformat()
        with self.store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if event.platform=='web' and getattr(self.store,'web_lifecycle',None):
                self.store.web_lifecycle.check(event.scope_id,db)
            existing = db.execute('SELECT * FROM planning_artifacts WHERE source_event_id=?',(event.event_key,)).fetchone()
            if existing:
                return self._record(existing)
            ordinal = db.execute('''SELECT coalesce(max(ordinal),0)+1 FROM planning_artifacts
                WHERE platform=? AND scope_id=? AND owner_id=?''',
                (event.platform,event.storage_scope_id,event.sender_id)).fetchone()[0]
            db.execute('''INSERT INTO planning_artifacts
                (artifact_id,platform,scope_id,owner_id,ordinal,kind,source_event_id,title,destination,
                 duration_options_json,summary,content,status,version,created_at,updated_at,capture_version,validation_reason)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (artifact_id,event.platform,event.storage_scope_id,event.sender_id,ordinal,kind,event.event_key,
                 title,destination,json.dumps(options),summary,assistant_content,'active',1,now,now,capture_version,'structured_plan'))
        return self.get(event,artifact_id)

    @staticmethod
    def _record(row):
        if not row:
            return None
        value = dict(row)
        value['duration_options'] = json.loads(value.pop('duration_options_json'))
        return value

    def list_for_owner(self,event,*,include_deleted=False,include_history=False):
        with self.store._connect() as db:
            rows=db.execute('''SELECT * FROM planning_artifacts WHERE platform=? AND scope_id=? AND owner_id=?
                AND (? OR (status IN ('active','adopted','superseded') AND (? OR status='active') AND capture_version>=?
                    AND NOT EXISTS (SELECT 1 FROM event_tool_results e WHERE e.event_id=planning_artifacts.source_event_id
                        AND e.tool_name='trip_presentation'))) ORDER BY ordinal''',
                (event.platform,event.storage_scope_id,event.sender_id,include_deleted,include_history,CAPTURE_VERSION)).fetchall()
        return tuple(self._record(row) for row in rows)

    def get(self,event,artifact_id):
        with self.store._connect() as db:
            row=db.execute('''SELECT * FROM planning_artifacts WHERE artifact_id=? AND platform=?
                AND scope_id=? AND owner_id=?''',
                (artifact_id,event.platform,event.storage_scope_id,event.sender_id)).fetchone()
        return self._record(row)

    def delete(self,event,artifact_id,version):
        with self.store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if event.platform=='web' and getattr(self.store,'web_lifecycle',None):
                self.store.web_lifecycle.check(event.scope_id,db)
            cursor=db.execute('''UPDATE planning_artifacts SET status='deleted',version=version+1,updated_at=?
                WHERE artifact_id=? AND platform=? AND scope_id=? AND owner_id=? AND version=? AND status='active' ''',
                (datetime.now(timezone.utc).isoformat(),artifact_id,event.platform,event.storage_scope_id,event.sender_id,version))
            if cursor.rowcount != 1:
                raise PlanningConflict('方案草稿已变化或不存在，请刷新后重试。')

    def history_rows(self, event):
        with self.store._connect() as db:
            watermark = db.execute('SELECT last_turn_id FROM planning_history_watermarks WHERE platform=? AND scope_id=? AND owner_id=?',
                                   (event.platform,event.storage_scope_id,event.sender_id)).fetchone()
            rows = db.execute("""SELECT c.id,c.user_msg_id,c.user_content,c.assistant_content,
                CASE WHEN r.event_key IS NULL THEN 'exploratory' ELSE 'research_backed' END kind
                FROM conversation_turns c LEFT JOIN research_runs r ON r.event_key=c.user_msg_id
                WHERE c.group_openid=? AND c.member_openid=? AND c.id>? ORDER BY c.id""",
                (event.storage_scope_id,event.sender_id,watermark['last_turn_id'] if watermark else 0)).fetchall()
        return [dict(row) for row in rows]

    def advance_history(self, event, turn_id):
        with self.store._connect() as db:
            if event.platform=='web' and getattr(self.store,'web_lifecycle',None):
                self.store.web_lifecycle.check(event.scope_id,db)
            db.execute("""INSERT INTO planning_history_watermarks VALUES (?,?,?,?)
                ON CONFLICT(platform,scope_id,owner_id) DO UPDATE SET last_turn_id=max(last_turn_id,excluded.last_turn_id)""",
                (event.platform,event.storage_scope_id,event.sender_id,turn_id))

    def stale_metadata(self, event, capture_version, limit=50):
        with self.store._connect() as db:
            rows = db.execute("""SELECT p.*,c.user_content FROM planning_artifacts p
                LEFT JOIN conversation_turns c ON c.user_msg_id=p.source_event_id
                    AND c.group_openid=p.scope_id AND c.member_openid=p.owner_id
                WHERE p.platform=? AND p.scope_id=? AND p.owner_id=? AND p.status='active'
                    AND p.capture_version<? ORDER BY p.ordinal LIMIT ?""",
                (event.platform,event.storage_scope_id,event.sender_id,capture_version,limit)).fetchall()
        return [dict(row) for row in rows]

    def repair_metadata(self, event, repairs, capture_version):
        if not repairs:
            return
        with self.store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if event.platform=='web' and getattr(self.store,'web_lifecycle',None):
                self.store.web_lifecycle.check(event.scope_id,db)
            for row in repairs:
                db.execute("""UPDATE planning_artifacts SET status=?,destination=?,duration_options_json=?,title=?,
                    capture_version=?,validation_reason=?,version=version+?,updated_at=?
                    WHERE artifact_id=? AND platform=? AND scope_id=? AND owner_id=?
                        AND status='active' AND version=? AND capture_version<?""",
                    (row['status'],row['destination'],json.dumps(row['duration_options']),row['title'],
                     capture_version,row['reason'],int(row['changed']),datetime.now(timezone.utc).isoformat(),
                     row['artifact_id'],event.platform,event.storage_scope_id,event.sender_id,row['version'],capture_version))
