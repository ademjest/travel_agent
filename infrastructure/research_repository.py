from datetime import datetime, timezone
import json


class ResearchRepository:
    def __init__(self, store):
        self.store = store
        with store._connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS research_runs (
                event_key TEXT PRIMARY KEY, platform TEXT, scope TEXT, owner TEXT,
                stage TEXT NOT NULL, data_json TEXT NOT NULL, updated_at TEXT NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS research_contexts (
                platform TEXT, scope TEXT, owner TEXT, destination TEXT NOT NULL,
                preferred_domains_json TEXT NOT NULL, excluded_domains_json TEXT NOT NULL,
                strict_sources INTEGER NOT NULL, source_event TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(platform,scope,owner))''')

    def get(self, event):
        with self.store._connect() as db:
            row = db.execute('SELECT * FROM research_runs WHERE event_key=? AND platform=? AND scope=? AND owner=?',
                (event.event_key, event.platform, event.scope_id, event.sender_id)).fetchone()
        return {'stage': row['stage'], **json.loads(row['data_json'])} if row else None

    def save(self, event, stage, data):
        with self.store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if event.platform == 'web' and getattr(self.store, 'web_lifecycle', None):
                self.store.web_lifecycle.check(event.scope_id, db)
            db.execute('''INSERT INTO research_runs VALUES (?,?,?,?,?,?,?) ON CONFLICT(event_key) DO UPDATE SET
                stage=excluded.stage,data_json=excluded.data_json,updated_at=excluded.updated_at''',
                (event.event_key, event.platform, event.scope_id, event.sender_id, stage,
                 json.dumps({k: v for k, v in data.items() if k != 'stage'}, ensure_ascii=False), datetime.now(timezone.utc).isoformat()))

    def for_artifact(self,event,source_event):
        """Read one referenced research result in the adopting owner's scope."""
        with self.store._connect() as db:
            if event.platform=='web' and getattr(self.store,'web_lifecycle',None):
                self.store.web_lifecycle.check(event.scope_id,db)
            row=db.execute('''SELECT r.stage,r.data_json,r.updated_at,j.status AS job_status FROM research_runs r
                LEFT JOIN inbox_jobs j ON j.event_key=r.event_key
                WHERE r.event_key=? AND r.platform=? AND r.scope=? AND r.owner=?''',
                (source_event,event.platform,event.scope_id,event.sender_id)).fetchone()
        if not row:
            return None
        stage=row['job_status'] if row['job_status'] in {'cancelled','failed','blocked'} else row['stage']
        return {**json.loads(row['data_json']),'stage':stage,'updated_at':row['updated_at']}

    def latest(self, event):
        with self.store._connect() as db:
            rows = db.execute('''SELECT CASE WHEN j.status IN ('cancelled','failed','blocked') THEN j.status ELSE r.stage END AS stage,
                r.data_json,r.updated_at FROM research_runs r LEFT JOIN inbox_jobs j ON j.event_key=r.event_key
                WHERE r.platform=? AND r.scope=? AND r.owner=? ORDER BY r.updated_at DESC LIMIT 5''',
                (event.platform, event.scope_id, event.sender_id)).fetchall()
        return [{'stage': row['stage'], 'updated_at': row['updated_at'], **json.loads(row['data_json'])} for row in rows]

    def context(self,event):
        with self.store._connect() as db:
            row=db.execute('SELECT * FROM research_contexts WHERE platform=? AND scope=? AND owner=?',
                (event.platform,event.scope_id,event.sender_id)).fetchone()
        if not row:
            return None
        value=dict(row)
        value['preferred_domains']=json.loads(value.pop('preferred_domains_json'))
        value['excluded_domains']=json.loads(value.pop('excluded_domains_json'))
        value['strict_sources']=bool(value['strict_sources'])
        return value

    def update_context(self,event,plan):
        destination=plan.get('destination','').strip()
        if not destination:
            return
        now=datetime.now(timezone.utc).isoformat()
        with self.store._connect() as db:
            if event.platform=='web' and getattr(self.store,'web_lifecycle',None):
                self.store.web_lifecycle.check(event.scope_id,db)
            db.execute('''INSERT INTO research_contexts VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(platform,scope,owner) DO UPDATE SET destination=excluded.destination,
                preferred_domains_json=excluded.preferred_domains_json,
                excluded_domains_json=excluded.excluded_domains_json,strict_sources=excluded.strict_sources,
                source_event=excluded.source_event,updated_at=excluded.updated_at''',
                (event.platform,event.scope_id,event.sender_id,destination,
                 json.dumps(plan.get('requested_domains',[])),json.dumps(plan.get('excluded_domains',[])),
                 int(plan.get('strict_sources',False)),event.event_key,now))
