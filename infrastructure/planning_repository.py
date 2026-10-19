"""Persistent conversation-level planning proposals, separate from saved trips."""
from datetime import datetime, timezone
import hashlib
import json
import re

from services.reminder_time import NUMBER, number

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

    @staticmethod
    def _destination(user, content):
        patterns = (
            rf'(?:想去|去)([\u4e00-\u9fff]{{2,10}}?)(?:玩|旅游|旅行|看看)',
            rf'(?:搜索|查(?:一下)?|了解|规划|安排)([\u4e00-\u9fff]{{2,10}}?)(?=(?:{NUMBER})[天日](?:游)?|攻略|旅行|旅游|行程)',
        )
        for pattern in patterns:
            match = re.search(pattern, user)
            if match:
                return match[1]
        match = re.search(r'(?:去|到)([\u4e00-\u9fff]{2,10})(?=[，。！；,\s]|$)', user)
        if match:
            return match[1]
        # Only an itinerary title can supply assistant-owned destination evidence.
        for line in PlanningRepository._plan_body(content).splitlines()[:6]:
            title = re.sub(r'^[\s#*]+|[*\s]+$', '', line)
            match = re.fullmatch(rf'([\u4e00-\u9fff]{{2,10}}?)(?:(?:{NUMBER})[天日](?:游)?|参考|研究|旅行|旅游)+(?:参考|研究|旅行|旅游|游玩)?(?:规划|方案|行程|攻略).*', title)
            if match:
                return match[1]
        return ''

    @staticmethod
    def _plan_body(content):
        body = re.sub(r'```.*?```|~~~.*?~~~', '', content, flags=re.S)
        body = re.sub(r'^\s*>.*$', '', body, flags=re.M)
        body = re.sub(r'“[^”]*”|「[^」]*」|"[^"\n]*"', '', body)
        # A demonstrative itinerary is not the user's travel proposal.
        body = re.split(r'(?:例如|举例|示例)(?:如下)?[：:]', body, maxsplit=1)[0]
        return body

    @staticmethod
    def _day_numbers(content):
        body = PlanningRepository._plan_body(content)
        return [number(m[1] or m[2]) for m in re.finditer(
            rf'(?:第\s*({NUMBER})\s*天|\bDay\s*(\d+))(?=[\s*：:、，,（(\-]|[^\x00-\x7f])', body, re.I)]

    @staticmethod
    def _structured(content):
        days = PlanningRepository._day_numbers(content)
        if not days or days[0] != 1:
            return False
        # Each variant must have a complete sequential day structure.
        previous = 0
        for day in days:
            if day != 1 and day != previous + 1:
                return False
            previous = day
        variants = list(re.finditer(rf'(?:方案\s*[A-Fa-f甲乙丙一二三\d]+|(?:完整|总)行程)[：:\s*]*({NUMBER})\s*[天日]',content))
        for i, match in enumerate(variants):
            section = content[match.end():variants[i+1].start() if i+1<len(variants) else len(content)]
            variant_days = PlanningRepository._day_numbers(section)
            if not variant_days or max(variant_days) != number(match[1]):
                return False
        return bool(PlanningRepository._plan_body(content).strip())

    @staticmethod
    def _durations(user, content):
        values = []
        body = PlanningRepository._plan_body(content)
        variants = re.findall(rf'(?:方案\s*[A-Fa-f甲乙丙一二三\d]+|(?:完整|总)行程)[：:\s*]*({NUMBER})\s*[天日]', body)
        days = PlanningRepository._day_numbers(body)
        totals = variants or ([str(max(days))] if days and PlanningRepository._structured(body) else [])
        if not totals:
            totals = re.findall(rf'(?<![第每同月\d一二两三四五六七八九十])({NUMBER})\s*(?:天|日游)', user)
        for text in totals:
            value = number(text)
            if 1 <= value <= 14 and value not in values:
                values.append(value)
        return values[:6]

    def capture(self, event, assistant_content, *, kind='exploratory', destination='', duration_options=None):
        if kind not in {'exploratory','research_backed'} or not isinstance(assistant_content,str):
            raise ValueError('规划方案类型或正文无效。')
        destination = destination or self._destination(event.content,assistant_content)
        options = list(duration_options or self._durations(event.content,assistant_content))
        looks_like_plan = self._structured(assistant_content)
        structural_days = self._durations('',assistant_content)
        if duration_options and structural_days and not set(duration_options) & set(structural_days):
            return None
        requested_days = self._durations(event.content, '')
        if requested_days and options and not set(requested_days) & set(options):
            return None
        if not destination or not looks_like_plan:
            return None
        artifact_id = self._id(event.event_key)
        now = datetime.now(timezone.utc).isoformat()
        title = destination + ('研究方案' if kind=='research_backed' else '参考方案')
        if options:
            title += '（' + '／'.join(str(v)+'天' for v in options) + '）'
        summary = re.sub(r'\s+',' ',assistant_content).strip()[:360]
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
                 title,destination,json.dumps(options),summary,assistant_content,'active',1,now,now,CAPTURE_VERSION,'structured_plan'))
        return self.get(event,artifact_id)

    @staticmethod
    def _record(row):
        if not row:
            return None
        value = dict(row)
        value['duration_options'] = json.loads(value.pop('duration_options_json'))
        return value

    def list_for_owner(self,event,*,include_deleted=False):
        with self.store._connect() as db:
            rows=db.execute('''SELECT * FROM planning_artifacts WHERE platform=? AND scope_id=? AND owner_id=?
                AND (? OR (status='active' AND capture_version>=?)) ORDER BY ordinal''',
                (event.platform,event.storage_scope_id,event.sender_id,include_deleted,CAPTURE_VERSION)).fetchall()
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

    def ensure_history(self,event):
        """Lazily index old assistant plans without relying on the recent-turn token budget."""
        self.revalidate(event)
        with self.store._connect() as db:
            watermark=db.execute('SELECT last_turn_id FROM planning_history_watermarks WHERE platform=? AND scope_id=? AND owner_id=?',
                                 (event.platform,event.storage_scope_id,event.sender_id)).fetchone()
            rows=db.execute('''SELECT c.id,c.user_msg_id,c.user_content,c.assistant_content,
                CASE WHEN r.event_key IS NULL THEN 'exploratory' ELSE 'research_backed' END kind
                FROM conversation_turns c LEFT JOIN research_runs r ON r.event_key=c.user_msg_id
                WHERE c.group_openid=? AND c.member_openid=? AND c.id>? ORDER BY c.id''',
                (event.storage_scope_id,event.sender_id,watermark['last_turn_id'] if watermark else 0)).fetchall()
        for row in rows:
            historical=type(event)(event.platform,event.channel,row['user_msg_id'].split(':')[-1],
                event.scope_id,event.sender_id,row['user_content'])
            self.capture(historical,row['assistant_content'],kind=row['kind'],
                duration_options=self._durations(row['user_content'],'') if row['kind']=='research_backed' else None)
        if rows:
            with self.store._connect() as db:
                if event.platform=='web' and getattr(self.store,'web_lifecycle',None):
                    self.store.web_lifecycle.check(event.scope_id,db)
                db.execute('''INSERT INTO planning_history_watermarks VALUES (?,?,?,?)
                    ON CONFLICT(platform,scope_id,owner_id) DO UPDATE SET last_turn_id=max(last_turn_id,excluded.last_turn_id)''',
                    (event.platform,event.storage_scope_id,event.sender_id,rows[-1]['id']))

    def revalidate(self, event, limit=50):
        """Bounded one-time metadata repair. Never resurrect deleted/adopted proposals."""
        with self.store._connect() as db:
            rows = db.execute('''SELECT p.*,c.user_content FROM planning_artifacts p
                LEFT JOIN conversation_turns c ON c.user_msg_id=p.source_event_id
                    AND c.group_openid=p.scope_id AND c.member_openid=p.owner_id
                WHERE p.platform=? AND p.scope_id=? AND p.owner_id=? AND p.status='active'
                    AND p.capture_version<? ORDER BY p.ordinal LIMIT ?''',
                (event.platform,event.storage_scope_id,event.sender_id,CAPTURE_VERSION,limit)).fetchall()
            if not rows:
                return
            db.execute('BEGIN IMMEDIATE')
            if event.platform == 'web' and getattr(self.store, 'web_lifecycle', None):
                self.store.web_lifecycle.check(event.scope_id, db)
            for row in rows:
                user = row['user_content'] or ''
                destination = self._destination(user, row['content'])
                options = self._durations(user, row['content'])
                valid = bool(destination and self._structured(row['content']))
                requested_days = self._durations(user, '')
                if requested_days and options and not set(requested_days) & set(options):
                    valid = False
                title = destination + ('研究方案' if row['kind']=='research_backed' else '参考方案')
                if options:
                    title += '（' + '／'.join(str(v)+'天' for v in options) + '）'
                changed = not valid or (destination, options, title) != (row['destination'],json.loads(row['duration_options_json']),row['title'])
                db.execute('''UPDATE planning_artifacts SET status=?,destination=?,duration_options_json=?,title=?,
                    capture_version=?,validation_reason=?,version=version+?,updated_at=?
                    WHERE artifact_id=? AND status='active' AND version=? AND capture_version<?''',
                    ('active' if valid else 'invalid',destination if valid else row['destination'],
                     json.dumps(options) if valid else row['duration_options_json'],title if valid else row['title'],
                     CAPTURE_VERSION,'metadata_repaired' if valid else 'insufficient_plan_evidence',int(changed),
                     datetime.now(timezone.utc).isoformat(),row['artifact_id'],row['version'],CAPTURE_VERSION))
