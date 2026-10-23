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
            db.execute('''CREATE TABLE IF NOT EXISTS wendao_calls (
                event_key TEXT PRIMARY KEY, platform TEXT, scope TEXT, owner TEXT,
                result_json TEXT NOT NULL)''')

    def wendao_call(self, event, *, reserve=None):
        """Reserve before transport; the event key survives cancellation and restarts."""
        with self.store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if event.platform == 'web' and getattr(self.store, 'web_lifecycle', None):
                self.store.web_lifecycle.check(event.scope_id, db)
            row = db.execute('SELECT result_json FROM wendao_calls WHERE event_key=? AND platform=? AND scope=? AND owner=?',
                (event.event_key, event.platform, event.scope_id, event.sender_id)).fetchone()
            if row:
                return json.loads(row['result_json']), False
            if reserve is not None:
                db.execute('INSERT INTO wendao_calls VALUES (?,?,?,?,?)',
                    (event.event_key, event.platform, event.scope_id, event.sender_id, json.dumps(reserve, ensure_ascii=False)))
            return reserve, reserve is not None

    def finish_wendao(self, event, result):
        with self.store._connect() as db:
            db.execute('UPDATE wendao_calls SET result_json=? WHERE event_key=? AND platform=? AND scope=? AND owner=?',
                (json.dumps(result, ensure_ascii=False), event.event_key, event.platform, event.scope_id, event.sender_id))

    def reserve_answer_write(self, event, metadata):
        """Atomically consume one writing attempt in the existing research record."""
        with self.store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if event.platform == 'web' and getattr(self.store, 'web_lifecycle', None):
                self.store.web_lifecycle.check(event.scope_id, db)
            row = db.execute('SELECT data_json FROM research_runs WHERE event_key=? AND platform=? AND scope=? AND owner=?',
                (event.event_key,event.platform,event.scope_id,event.sender_id)).fetchone()
            if row is None:
                raise ValueError('Writing requires a saved research checkpoint.')
            data = json.loads(row['data_json'])
            if data.get('answer_write'):
                return data['answer_write'], False
            record = {'status':'reserved', **metadata}
            data['answer_write'] = record
            db.execute('UPDATE research_runs SET data_json=? WHERE event_key=?',
                (json.dumps(data,ensure_ascii=False),event.event_key))
            return record, True

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
            payload = {k:v for k,v in data.items() if k!='stage'}
            previous = db.execute('SELECT data_json FROM research_runs WHERE event_key=?',(event.event_key,)).fetchone()
            old = json.loads(previous['data_json']) if previous else {}
            writing = old.get('answer_write')
            if writing and ('answer_write' not in payload or writing.get('status') in {'completed','fallback','unknown'}):
                payload['answer_write'] = writing
                if writing.get('result'):
                    for key in ('report','brief_report','answer_schema'):
                        if key in old:
                            payload[key] = old[key]
            db.execute('''INSERT INTO research_runs VALUES (?,?,?,?,?,?,?) ON CONFLICT(event_key) DO UPDATE SET
                stage=excluded.stage,data_json=excluded.data_json,updated_at=excluded.updated_at''',
                (event.event_key, event.platform, event.scope_id, event.sender_id, stage,
                 json.dumps(payload, ensure_ascii=False), datetime.now(timezone.utc).isoformat()))

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
                r.event_key,r.data_json,r.updated_at FROM research_runs r LEFT JOIN inbox_jobs j ON j.event_key=r.event_key
                WHERE r.platform=? AND r.scope=? AND r.owner=? ORDER BY r.updated_at DESC LIMIT 5''',
                (event.platform, event.scope_id, event.sender_id)).fetchall()
        return [{'stage': row['stage'], 'updated_at': row['updated_at'], 'research_id':row['event_key'], **json.loads(row['data_json'])} for row in rows]

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
