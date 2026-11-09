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
            db.execute('''CREATE TABLE IF NOT EXISTS wendao_requests (
                event_key TEXT, purpose TEXT, platform TEXT, scope TEXT, owner TEXT,
                result_json TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY(event_key,purpose))''')

    def wendao_call(self, event, *, reserve=None, purpose='research', query_hash='', fingerprint=''):
        """Reserve before transport; the event key survives cancellation and restarts."""
        with self.store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if event.platform == 'web' and getattr(self.store, 'web_lifecycle', None):
                self.store.web_lifecycle.check(event.scope_id, db)
            if purpose not in {'research','hotel_quotes'}:raise ValueError('Unsupported Wendao purpose')
            table='wendao_calls' if purpose=='research' else 'wendao_requests'
            extra='' if purpose=='research' else ' AND purpose=?'
            parameters=(event.event_key,event.platform,event.scope_id,event.sender_id)+(() if purpose=='research' else (purpose,))
            row = db.execute('SELECT result_json FROM '+table+' WHERE event_key=? AND platform=? AND scope=? AND owner=?'+extra,parameters).fetchone()
            if row:
                result=json.loads(row['result_json'])
                if purpose!='research' and (result.get('query_hash')!=query_hash or result.get('fingerprint')!=fingerprint):
                    raise ValueError('问道同事件同用途的查询条件冲突，未复用或追加提交。')
                return result, False
            if reserve is not None:
                count=db.execute('SELECT (SELECT count(*) FROM wendao_calls WHERE event_key=?)+(SELECT count(*) FROM wendao_requests WHERE event_key=?)',
                    (event.event_key,event.event_key)).fetchone()[0]
                if count>=2:raise ValueError('本事件问道总提交额度已用完。')
                payload=json.dumps({**reserve,'fingerprint':fingerprint},ensure_ascii=False)
                if purpose=='research':db.execute('INSERT INTO wendao_calls VALUES (?,?,?,?,?)',(*parameters,payload))
                else:db.execute('INSERT INTO wendao_requests VALUES (?,?,?,?,?,?,?)',
                    (event.event_key,purpose,event.platform,event.scope_id,event.sender_id,payload,datetime.now(timezone.utc).isoformat()))
            return reserve, reserve is not None

    def finish_wendao(self, event, result, *, purpose='research'):
        with self.store._connect() as db:
            if event.platform=='web' and getattr(self.store,'web_lifecycle',None):self.store.web_lifecycle.check(event.scope_id,db)
            table='wendao_calls' if purpose=='research' else 'wendao_requests'
            db.execute('UPDATE '+table+' SET result_json=? WHERE event_key=? AND platform=? AND scope=? AND owner=?'+('' if purpose=='research' else ' AND purpose=?'),
                (json.dumps(result, ensure_ascii=False),event.event_key,event.platform,event.scope_id,event.sender_id)+(() if purpose=='research' else (purpose,)))

    def hotel_extraction(self,event,*,reserve=False,result=None):
        """Consume at most one model attempt per saved hotel response, including unknown outcomes."""
        with self.store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if event.platform=='web' and getattr(self.store,'web_lifecycle',None):self.store.web_lifecycle.check(event.scope_id,db)
            row=db.execute("SELECT result_json FROM wendao_requests WHERE event_key=? AND purpose='hotel_quotes' AND platform=? AND scope=? AND owner=?",
                (event.event_key,event.platform,event.scope_id,event.sender_id)).fetchone()
            if not row:raise ValueError('酒店提取需要已保存的同用途来源。')
            data=json.loads(row['result_json']);old=data.get('extraction')
            if reserve and old:return old,False
            if reserve or result is not None:
                data['extraction']=result if result is not None else {'status':'submitted'}
                db.execute("UPDATE wendao_requests SET result_json=? WHERE event_key=? AND purpose='hotel_quotes'",
                    (json.dumps(data,ensure_ascii=False),event.event_key))
            return data.get('extraction'),reserve

    def hotel_quote_records(self,event,*,limit=20):
        with self.store._connect() as db:
            if event.platform=='web' and getattr(self.store,'web_lifecycle',None):self.store.web_lifecycle.check(event.scope_id,db)
            rows=db.execute("SELECT result_json FROM wendao_requests WHERE purpose='hotel_quotes' AND platform=? AND scope=? AND owner=? ORDER BY created_at DESC LIMIT ?",
                (event.platform,event.scope_id,event.sender_id,limit if limit is not None else -1)).fetchall()
        return [json.loads(row['result_json']) for row in rows]

    def hotel_quote_for_event(self,event,source_event):
        with self.store._connect() as db:
            if event.platform=='web' and getattr(self.store,'web_lifecycle',None):self.store.web_lifecycle.check(event.scope_id,db)
            row=db.execute("SELECT result_json FROM wendao_requests WHERE purpose='hotel_quotes' AND event_key=? AND platform=? AND scope=? AND owner=?",
                (source_event,event.platform,event.scope_id,event.sender_id)).fetchone()
        return json.loads(row['result_json']).get('extraction',{}).get('snapshot') if row else None

    def hotel_quotes_for_event(self,event,source_event):
        quote=self.hotel_quote_for_event(event,source_event)
        if quote:return [quote]
        # A cache receipt can refer to an older owned source, without creating a new request.
        with self.store._connect() as db:
            rows=db.execute("SELECT result_text FROM event_tool_results WHERE event_id=? AND tool_name IN ('query_hotel_prices','hotel_local_read')",(source_event,)).fetchall()
        sources=[]
        for row in rows:
            data=json.loads(row['result_text']);payload=data.get('payload') or {}
            key=payload.get('snapshot',{}).get('source_event_id')
            if key and key not in sources:sources.append(key)
            for ref in payload.get('history_refs',[]):
                if ref not in sources:sources.append(ref)
        return [quote for key in sources if (quote:=self.hotel_quote_for_event(event,key))]

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

    def navigation_cache(self, event):
        """Owner-scoped public identity snapshots; JSON storage, no separate cache or network."""
        with self.store._connect() as db:
            rows = db.execute('''SELECT data_json FROM research_runs WHERE platform=? AND scope=? AND owner=?
                AND json_type(data_json,'$.official_links')='object' ORDER BY updated_at DESC LIMIT 20''',
                (event.platform, event.scope_id, event.sender_id)).fetchall()
        cached = {}
        for row in rows:
            data = json.loads(row['data_json'])
            sources = {s['id']:s for s in data.get('sources', [])}
            for link in data.get('official_links', {}).values():
                ids = link.get('evidence_source_ids', [])
                if ids and all(sid in sources for sid in ids):
                    cached.setdefault(link['scope_key'], {'link':link, 'sources':[sources[sid] for sid in ids]})
        return cached

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
