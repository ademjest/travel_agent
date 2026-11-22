from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import secrets
import uuid

from core.chat_transport import ChatEvent
from core.web_lifecycle import ConversationDeleted, WebLifecycle


OWNER = 'local-owner'


def timestamp():
    return datetime.now(timezone.utc).isoformat()


class WebConflict(ValueError):
    pass


class WebReferenceError(ValueError):
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

    def upload_bytes(self):
        with self.store._connect() as db:
            return db.execute('SELECT coalesce(sum(size),0) FROM web_uploads').fetchone()[0]

    def save_upload(self, identity, conversation_id, filename, relative_path, content_type, data):
        self.conversation(conversation_id)
        with self.store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self.lifecycle.check(conversation_id, db)
            db.execute('INSERT INTO web_uploads VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                       (identity, conversation_id, filename, relative_path, content_type, len(data),
                        hashlib.sha256(data).hexdigest(), timestamp()))
        return self.upload(identity)

    def accept(self, conversation_id, content, upload_ids, request_id, confirmation=None, trip_edit=None, *, priority=0, quoted_message_id=None,rail_query=None,draft_action=None,hotel_action=None):
        self.conversation(conversation_id)
        body = {'content': content, 'upload_ids': upload_ids, 'confirmation': confirmation}
        if trip_edit is not None:
            body['trip_edit'] = trip_edit
        if quoted_message_id is not None:
            body['quoted_message_id']=quoted_message_id
        if rail_query is not None:
            body['rail_query']=rail_query
        if draft_action is not None:body['draft_action']=draft_action
        if hotel_action is not None:body['hotel_action']=hotel_action
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
            action=None
            if draft_action is not None:
                event=ChatEvent('web','group',request_id,conversation_id,OWNER,content)
                action=self.store.trips.prepare_draft_action(db,event,draft_action)
            reply_to_id='';reference=None
            if quoted_message_id is not None:
                reference=self.message_reference(conversation_id,quoted_message_id)
                reply_to_id=reference.get('native_id','')
            uploads = [self.upload(item, conversation_id) for item in upload_ids]
            payload = {'version': 1, 'conversation_id': conversation_id, 'request_id': request_id,
                       'content': content, 'upload_ids': upload_ids, 'time': now, 'confirmation': confirmation,
                       'trip_edit':trip_edit}
            if reply_to_id:
                payload['reply_to_id']=reply_to_id
            if reference:
                payload['quoted_message_id']=quoted_message_id
                payload['message_reference']=reference
            if rail_query is not None:
                payload['rail_query']=rail_query
            if action is not None:payload['draft_action']=action
            if hotel_action is not None:payload['hotel_action']=hotel_action
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
        self.store.diagnostics.record('request',{'event_key':event_key,'status':'received'})
        return {'job_id': job['id'], 'status': 'accepted'}

    def message_reference(self,identity,message_id):
        """Freeze an owned message and existing immutable evidence, never a newer task's slots."""
        self.conversation(identity)
        if not isinstance(message_id,str) or len(message_id)>256:
            raise WebReferenceError('引用消息无效，请重新选择。')
        with self.store._connect() as db:
            if message_id.startswith('d:') and message_id[2:].isascii() and message_id[2:].isdigit():
                row=db.execute('''SELECT d.id,d.content,d.created_at,d.kind,l.event_id FROM web_deliveries d
                    LEFT JOIN outbound_message_links l ON l.message_id='web-'||d.id AND l.platform='web'
                    AND l.scope_id=? AND l.owner_id=? WHERE d.id=? AND d.conversation_id=?''',
                    ('web:'+identity,OWNER,message_id[2:],identity)).fetchone()
                role='assistant';native='web-'+str(row['id']) if row else ''
            elif message_id.startswith('u:'):
                row=db.execute('''SELECT r.content,r.created_at,r.uploads_json,'message' AS kind,r.event_key AS event_id
                    FROM web_requests r JOIN inbox_jobs j ON j.event_key=r.event_key
                    WHERE r.event_key=? AND r.conversation_id=? AND j.owner_id=? AND j.platform='web' ''',
                    (message_id[2:],identity,OWNER)).fetchone()
                role='user';native=''
            else:row=None
            if not row:raise WebReferenceError('引用消息不存在、已删除或不属于当前会话，请重新选择。')
            result={'message_id':message_id,'role':role,'kind':row['kind'],'content':row['content'],
                    'created_at':row['created_at'],'event_id':row['event_id'] or '',
                    'content_sha256':hashlib.sha256(row['content'].encode()).hexdigest(),'native_id':native}
            if role=='user':
                result['attachments']=[]
                for upload_id in json.loads(row['uploads_json']):
                    attachment=db.execute('SELECT id,filename,content_type,sha256 FROM web_uploads WHERE id=? AND conversation_id=?',
                                          (upload_id,identity)).fetchone()
                    result['attachments'].append({**dict(attachment),'available':True} if attachment else {'id':upload_id,'available':False})
            event=row['event_id']
            if event:
                presentation=db.execute("SELECT result_text,created_at FROM event_tool_results WHERE event_id=? AND tool_name='trip_presentation'",(event,)).fetchone()
                version=db.execute('''SELECT v.snapshot_json FROM trip_versions v JOIN trips t ON t.trip_id=v.trip_id
                    WHERE v.event_id=? AND t.platform='web' AND t.scope_id=? AND t.owner_id=?''',(event,'web:'+identity,OWNER)).fetchone()
                task=db.execute('''SELECT t.*,e.task_version FROM task_event_results e JOIN travel_tasks t ON t.task_id=e.task_id
                    WHERE e.event_id=? AND t.platform='web' AND t.scope_id=? AND t.owner_id=?''',(event,'web:'+identity,OWNER)).fetchone()
                if presentation:
                    value=json.loads(presentation['result_text']);result['trip']=value['trip'];result['is_preview']=value.get('is_preview',False)
                    if value.get('draft'):result['draft']=value['draft']
                    if role=='user' or value.get('content_sha256')!=result['content_sha256']:
                        result['association_note']='此行程是该请求后来生成的关联结果，不是被引用消息的原文。'
                        result['related_result_created_at']=presentation['created_at']
                elif version:result['trip']=json.loads(version['snapshot_json']);result['is_preview']=False
                elif task and task['version']==task['task_version'] and task['last_event_id']==event:
                    candidate=json.loads(task['slots_json']).get('candidate_trip')
                    if candidate:result.update(trip=candidate,is_preview=True)
                if task and task['task_type']=='itinerary' and result.get('trip'):
                    result['draft']={'task_id':task['task_id'],'version':task['task_version']}
                if task and task['task_type']=='itinerary' and not result.get('trip'):
                    result['trip_snapshot_missing']=True
                receipts=db.execute('''SELECT operation_key,tool_name,result_text FROM event_tool_results WHERE event_id=?
                    AND tool_name NOT LIKE 'rail:%' AND tool_name NOT IN ('__skill_assets__','__tool_catalog__','amap_http')
                    AND operation_key NOT LIKE '%:submitted' ORDER BY created_at LIMIT 20''',(event,)).fetchall()
                result['receipts']=[{'operation_key':r['operation_key'],'tool_name':r['tool_name'],
                    'sha256':hashlib.sha256(r['result_text'].encode()).hexdigest()} for r in receipts]
                result['tool_results']=[]
                for receipt in receipts:
                    try:value=json.loads(receipt['result_text'])
                    except ValueError:continue
                    if isinstance(value,dict) and value.get('action') and value.get('status'):
                        result['tool_results'].append({k:value[k] for k in ('action','status','data','payload','error_code') if k in value})
        if native:
            rail=self.store.tasks.rail_references('web','web:'+identity,OWNER,[native]).get(native)
            if rail:result['rail']=rail
        result['availability']='partial' if result.get('trip_snapshot_missing') or any(not a['available'] for a in result.get('attachments',[])) else 'snapshot' if result.get('trip') or result.get('rail') else 'text_only'
        return result

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
        references=self.store.tasks.rail_references('web','web:'+identity,OWNER,
            ['web-'+str(row['id'])[2:] for row in rows[:limit] if row['role']=='assistant' and str(row['id']).startswith('d:')])
        rail_results={}
        for result in self.rail_receipts(identity,message_ids=[
                'web-'+str(row['id'])[2:] for row in rows[:limit] if row['role']=='assistant']):
            rail_results.setdefault(result['message_id'],[]).append({'id':result['id'],
                'origin':result['request']['origin'],'destination':result['request']['destination']})
        trip_messages=self.trip_messages(identity,['web-'+str(row['id'])[2:] for row in rows[:limit] if row['role']=='assistant'])
        for row in reversed(rows[:limit]):
            item = dict(row)
            item['uploads'] = []
            item['reference']={'kind':'message','label':('你' if item['role']=='user' else '助手')+' · '+item['created_at']+' · '+item['content'][:100]}
            reference=references.get('web-'+str(item['id'])[2:]) if item['role']=='assistant' else None
            if reference:
                preferences={'high_speed_only':'仅高铁','high_speed':'高铁优先','d_train':'仅动车','conventional':'普通列车','all':'不限车种'}
                item['reference']={'kind':'rail','label':'；'.join(' → '.join(q.get(k,'待补充') for k in ('origin','destination'))+
                    ' · '+q.get('travel_date','日期待补充')+' · '+q.get('departure_window',
                    '时段待补充' if 'departure_window' in reference['missing_fields'][index] else '时段不限')+
                    ' · '+preferences.get(q.get('train_preference'),'高铁优先（默认）') for index,q in enumerate(reference['queries']))}
            if reference and 'web-'+str(item['id'])[2:] in rail_results:
                item['rail_results']=rail_results['web-'+str(item['id'])[2:]]
            trip_message=trip_messages.get('web-'+str(item['id'])[2:]) if item['role']=='assistant' and item['kind']!='progress' else None
            if trip_message:
                presentation=trip_message.get('presentation')
                if presentation and presentation.get('content_sha256')==hashlib.sha256(item['content'].encode()).hexdigest():
                    item['trip_presentation']=self.compact_presentation(presentation)
                if trip_message.get('comparison'):item['plan_comparison']=True
            for upload_id in json.loads(item.pop('uploads_json')):
                try:
                    u = self.upload(upload_id, identity)
                    item['uploads'].append({k: u[k] for k in ('id', 'filename', 'content_type', 'size')})
                except LookupError:
                    item['uploads'].append({'id': upload_id, 'filename': '附件已过期', 'content_type': '', 'size': 0})
            items.append(item)
        return {'items': items, 'has_more': len(rows) > limit,
                'before': items[0]['created_at'] if items else None}

    def trip_messages(self,identity,message_ids):
        if not message_ids:return {}
        with self.store._connect() as db:
            rows=db.execute('''SELECT l.message_id,e.tool_name,e.result_text,e.event_id FROM outbound_message_links l
                JOIN event_tool_results e ON e.event_id=l.event_id
                WHERE l.platform='web' AND l.scope_id=? AND l.owner_id=?
                AND l.message_id IN ('''+','.join('?' for _ in message_ids)+''')
                AND (e.tool_name='trip_presentation' OR e.tool_name='trip_alternative' AND e.operation_key LIKE '%:result')''',
                ('web:'+identity,OWNER,*message_ids)).fetchall()
        result={}
        for row in rows:
            value=result.setdefault(row['message_id'],{})
            if row['tool_name']=='trip_presentation':
                value['presentation']=json.loads(row['result_text'])
                with self.store._connect() as db:
                    link=db.execute('SELECT task_id,task_version FROM task_event_results WHERE event_id=?',(row['event_id'],)).fetchone()
                    if link:
                        event=ChatEvent('web','group','',identity,OWNER,'')
                        from infrastructure.trip_repository import DraftActionConflict
                        try:
                            value['presentation']['lifecycle']=self.store.trips.draft_state(db,event,link['task_id'],captured_version=link['task_version'])['view']
                        except DraftActionConflict:
                            # The immutable message remains readable when its legacy mutable task is incomplete.
                            value['presentation']['lifecycle']={'id':link['task_id'],'version':link['task_version'],
                                'state':'unavailable','can_revalidate':False,'can_discard':False}
                if value['presentation']['trip']['spec'].get('planning_decision'):value['comparison']=True
            else:value['comparison']=True
        return result

    def event_message(self,identity,event_id):
        with self.store._connect() as db:
            row=db.execute('''SELECT d.id FROM outbound_message_links l JOIN web_deliveries d ON l.message_id='web-'||d.id
                WHERE l.platform='web' AND l.scope_id=? AND l.owner_id=? AND l.event_id=? AND d.conversation_id=?
                AND d.kind='reply' ORDER BY d.id DESC LIMIT 1''',('web:'+identity,OWNER,event_id,identity)).fetchone()
        return 'd:'+str(row['id']) if row else None

    @staticmethod
    def compact_presentation(value):
        trip=value['trip']
        return {key:item for key,item in value.items() if key!='trip'} | {'trip':{
            'trip_id':trip['trip_id'],'version':trip.get('version',1),'spec':{},'sources':{},
            'plan':{'days':[],'intercity':trip['plan'].get('intercity')}}}

    def trip_comparison_messages(self,identity):
        with self.store._connect() as db:
            rows=db.execute('''SELECT l.message_id,json_extract(e.result_text,'$.trip.trip_id') AS trip_id
                FROM outbound_message_links l JOIN event_tool_results e ON e.event_id=l.event_id
                JOIN web_deliveries d ON l.message_id='web-'||d.id AND d.conversation_id=?
                WHERE l.platform='web' AND l.scope_id=? AND l.owner_id=? AND e.tool_name='trip_presentation'
                AND json_type(e.result_text,'$.trip.spec.planning_decision')='object'
                ORDER BY d.id DESC''',(identity,'web:'+identity,OWNER)).fetchall()
        result={}
        for row in rows:result.setdefault(row['trip_id'],'d:'+row['message_id'].removeprefix('web-'))
        return result

    def plan_comparison(self,identity,message_id):
        """Follow owned message links; no runtime, provider or mutable task reconstruction."""
        self.conversation(identity)
        if not re.fullmatch(r'd:\d{1,20}',message_id):raise LookupError('该消息不存在。')
        with self.store._connect() as db:
            link=db.execute('''SELECT l.event_id,d.content FROM outbound_message_links l JOIN web_deliveries d
                ON l.message_id='web-'||d.id AND d.conversation_id=?
                WHERE l.platform='web' AND l.scope_id=? AND l.owner_id=? AND l.message_id=?''',
                (identity,'web:'+identity,OWNER,'web-'+message_id[2:])).fetchone()
            if not link:raise LookupError('该消息不存在。')
            event=link['event_id']
            presentation=db.execute("SELECT result_text FROM event_tool_results WHERE event_id=? AND tool_name='trip_presentation'",(event,)).fetchone()
            rows=db.execute("SELECT operation_key,result_text,created_at FROM event_tool_results WHERE event_id=? AND tool_name='trip_alternative' AND operation_key LIKE '%:result' ORDER BY created_at",(event,)).fetchall()
            saved=db.execute('''SELECT v.snapshot_json FROM trip_versions v JOIN trips t ON t.trip_id=v.trip_id
                WHERE v.event_id=? AND t.platform='web' AND t.scope_id=? AND t.owner_id=?''',(event,'web:'+identity,OWNER)).fetchone()
        trip=json.loads(presentation['result_text'])['trip'] if presentation else json.loads(saved['snapshot_json']) if saved else None
        branches={}
        for row in rows:
            name=row['operation_key'].rsplit(':',2)[-2]
            branches[name]={'created_at':row['created_at'],**json.loads(row['result_text'])}
        decision=(trip or {}).get('spec',{}).get('planning_decision',{})
        if not branches and not decision:raise LookupError('此条消息没有保存两版草案。')
        previous_id=None
        reference=decision.get('reference_event_id') or decision.get('source_event_id')
        if reference and reference!=event:
            with self.store._connect() as db:
                prior=db.execute('''SELECT l.message_id FROM outbound_message_links l JOIN web_deliveries d ON l.message_id='web-'||d.id
                    WHERE l.platform='web' AND l.scope_id=? AND l.owner_id=? AND l.event_id=? AND d.conversation_id=? AND d.kind!='progress'
                    ORDER BY d.id DESC LIMIT 1''',('web:'+identity,OWNER,reference,identity)).fetchone()
                if prior:previous_id='d:'+prior['message_id'].removeprefix('web-')
        self.conversation(identity)
        return {'message_id':message_id,'branches':branches,'final_trip':trip,'decision':decision,
                'final_text':link['content'],'final_presentation':self.compact_presentation(json.loads(presentation['result_text'])) if presentation else None,
                'previous_message_id':previous_id,
                'final_note':'' if trip else '此条历史预览未保存独立快照；未用最新行程回填。'}

    def rail_receipts(self,identity,*,result_id=None,message_ids=()):
        """Owned query receipts are the result store; pagination never executes tools."""
        self.conversation(identity)
        if not result_id and not message_ids:
            return []
        where='e.operation_key=?' if result_id else 'l.message_id IN ('+','.join('?' for _ in message_ids)+')'
        with self.store._connect() as db:
            rows=db.execute('''SELECT e.*,l.message_id FROM outbound_message_links l
                JOIN event_tool_results e ON e.event_id=l.event_id
                WHERE l.platform='web' AND l.scope_id=? AND l.owner_id=?
                AND e.tool_name='query_train_tickets' AND '''+where+'''
                ORDER BY CASE WHEN e.operation_key GLOB 'read-plan:*:[0-9]' THEN 0 ELSE 1 END,e.created_at''',
                ('web:'+identity,OWNER,*([result_id] if result_id else message_ids))).fetchall()
        results=[]; seen=set()
        for row in rows:
            data=json.loads(row['result_text']); payload=data.get('payload') or {}; request=payload.get('request')
            if data.get('status')!='completed' or not request or payload.get('status') not in {'ok','empty','incomplete','not_on_sale'}:
                continue
            signature=(row['message_id'],json.dumps(request,sort_keys=True))
            if signature in seen:
                continue
            seen.add(signature)
            results.append({'id':row['operation_key'],'message_id':row['message_id'],'event_id':row['event_id'],
                            'request':request,'payload':payload})
        return results

    def rail_result(self,identity,result_id,*,page=1,period='current'):
        from datetime import date
        from mcp.types import CallToolResult
        from infrastructure.rail_client import RailClient,response_json
        from infrastructure.mcp_client import MCPUnavailable
        from core.time_input import RAIL_DEPARTURE_PERIODS
        records=self.rail_receipts(identity,result_id=result_id)
        if not records:
            raise LookupError('该车次结果不可用，请重新查询。')
        record=records[0]; payload=record['payload']; request=record['request']; window=request['departure_window']
        bands={'morning':('上午',RAIL_DEPARTURE_PERIODS['上午']),
               'afternoon':('下午',RAIL_DEPARTURE_PERIODS['下午']),'evening':('晚上',RAIL_DEPARTURE_PERIODS['晚上'])}
        filters=[{'id':'current','label':'全天' if window==[0,24] else '已查时段'}]
        filters.extend({'id':key,'label':label} for key,(label,bound) in bands.items()
                       if window[0]<=bound[0] and bound[1]<=window[1] and list(bound)!=window)
        if type(page) is not int or page<1 or period not in {item['id'] for item in filters}:
            raise ValueError('页码或筛选范围无效；尚未查询的时段需要重新查询。')
        full=payload.get('all_trains'); legacy=False; candidate_limit=payload.get('candidate_limit')
        if full is None:
            full=payload.get('trains',[]); legacy=bool(full)
            # Old replies retain their raw successful RPC receipt and its exact retrieval time.
            with self.store._connect() as db:
                saved=db.execute("SELECT result_text,arguments_json FROM event_tool_results WHERE event_id=? AND tool_name='rail:get-tickets'",
                                 (record['event_id'],)).fetchall()
            matches=[(json.loads(row['result_text']),json.loads(row['arguments_json'])) for row in saved
                     if json.loads(row['result_text']).get('retrieved_at')==payload.get('retrieved_at')]
            if len(matches)==1:
                try:
                    raw,args=matches[0]
                    candidate_limit=args.get('limitedNum')
                    full=RailClient.normalize_tickets(response_json(CallToolResult.model_validate(raw['result'])),
                        date.fromisoformat(request['travel_date']),(args['fromStation'],args['toStation']),
                        (request['origin_scope'],request['destination_scope']),request['train_preference'],window,request.get('limit',6))
                    legacy=False
                except (ValueError,KeyError,TypeError,MCPUnavailable):
                    pass
        selected=window if period=='current' else bands[period][1]
        from core.rail_contract import seat_matches, select_seat
        full=[train for train in full if seat_matches(train,request)]
        groups={}
        for train in full:
            hour=int(train['departure_at'][11:13])+int(train['departure_at'][14:16])/60
            if not selected[0]<=hour<selected[1]:
                continue
            key=(train['train_code'],train['departure_at'][:10])
            group=groups.setdefault(key,{'train_code':key[0],'departure_date':key[1],'options':[]})
            if request.get('seat_class'):train={**train,'quoted_seat':select_seat(train,request)}
            if train not in group['options']:
                group['options'].append(train)
        for group in groups.values():
            group['options'].sort(key=lambda train:(train['departure_at'],train['arrival_at'],train['origin_station'],train['destination_station']))
        groups=sorted(groups.values(),key=lambda group:(request['train_preference']=='high_speed' and not group['train_code'].startswith('G'),
                                                      group['options'][0]['departure_at'],group['train_code']))
        page_size=6; page_count=max(1,(len(groups)+page_size-1)//page_size)
        if page>page_count:
            raise ValueError('页码超出当前筛选结果。')
        incomplete=bool(payload.get('candidate_truncated') or legacy or payload.get('status')=='incomplete')
        actions=[]
        for key,(label,bound) in bands.items():
            covered=window[0]<=bound[0] and bound[1]<=window[1]
            if list(bound)!=window and (not covered or incomplete):
                actions.append({'period':key,'label':('补查' if covered else '查询')+label})
        if window!=[0,24]:
            actions.append({'period':'all_day','label':'扩展查询全天'})
        times=sorted(train['departure_at'][11:16] for train in full)
        return {'id':result_id,'request':request,'status':payload['status'],'queried_at':payload['retrieved_at'],
            'total_count':len({(t['train_code'],t['departure_at'][:10]) for t in full}),
            'matched_count':len(groups),'time_span':[times[0],times[-1]] if times else [],
            'page':page,'page_count':page_count,'page_size':page_size,'period':period,
            'items':groups[(page-1)*page_size:page*page_size],'filters':filters,'query_actions':actions,
            'incomplete':incomplete,'legacy_partial':legacy,'candidate_limit':candidate_limit}

    def rail_query_arguments(self,identity,result_id,period):
        from core.intent_ir import READ_OPERATION_FIELDS
        from core.time_input import RAIL_DEPARTURE_PERIODS
        records=self.rail_receipts(identity,result_id=result_id)
        if not records:
            raise LookupError('该车次结果不可用，请重新提供查询条件。')
        source=records[0]['request']
        bands={'all_day':(0,24),'morning':RAIL_DEPARTURE_PERIODS['上午'],
               'afternoon':RAIL_DEPARTURE_PERIODS['下午'],'evening':RAIL_DEPARTURE_PERIODS['晚上'],
               'current':source['departure_window']}
        if period not in bands:
            raise ValueError('不支持的查询时段。')
        window=bands[period]
        values={key:value for key,value in source.items() if key in READ_OPERATION_FIELDS['rail'] and key!='departure_window'}
        values['departure_window']='至'.join(f'{int(hour*60)//60:02d}:{int(hour*60)%60:02d}' for hour in window)
        labels={'high_speed_only':'仅高铁','high_speed':'高铁优先','d_train':'仅动车','conventional':'普通列车','all':'不限车种'}
        values['train_preference_text']=labels[values['train_preference']]
        content=f"查询{values['travel_date']} {values['origin']}到{values['destination']}，{values['departure_window']}出发，{values['train_preference_text']}"
        return values,content

    def job(self, identity):
        with self.store._connect() as db:
            row = db.execute('SELECT id,event_key,scope_id,status,attempts,created_at,updated_at FROM inbox_jobs WHERE id=? AND platform=? AND owner_id=?',
                             (identity, 'web', OWNER)).fetchone()
        if not row:
            raise LookupError('任务不存在。')
        self.conversation(row['scope_id'])
        return dict(row)

    def active_jobs(self, identity):
        with self.store._connect() as db:
            rows=[dict(r) for r in db.execute("SELECT id,event_key,status,payload_json,created_at,updated_at FROM inbox_jobs WHERE platform='web' AND scope_id=? AND owner_id=? AND status IN ('pending','running','retry') ORDER BY id", (identity, OWNER))]
            from infrastructure.execution_report import explain
            for row in rows:
                action=json.loads(row.pop('payload_json')).get('draft_action')
                if action:row['draft_action']={k:action[k] for k in ('id','version','action')}
                recent=db.execute('SELECT id,stage,created_at,details_json FROM execution_diagnostics WHERE event_key=? ORDER BY id DESC LIMIT 1',(row.pop('event_key'),)).fetchone()
                if recent:
                    value=explain({**dict(recent),'details':json.loads(recent['details_json'])})
                    row['progress']={key:value[key] for key in ('stage','created_at','description')}
            return rows

    def notifications(self):
        with self.store._connect() as db:
            now = datetime.now(timezone.utc)
            reminder_cutoff = (now - timedelta(hours=24)).isoformat()
            other_cutoff = (now - timedelta(days=7)).isoformat()
            db.execute('''DELETE FROM web_deliveries WHERE kind='notification' AND (
                (created_at < ? AND (
                    EXISTS (SELECT 1 FROM outbox_messages o
                        WHERE web_deliveries.delivery_key='web:outbox:' || o.id
                        AND (o.event_id LIKE 'reminder:%' OR o.event_id LIKE 'reservation-reminder:%'))
                    OR content LIKE '%提醒你：%' OR content LIKE '%景点预约提醒：%'))
                OR created_at < ?)''', (reminder_cutoff, other_cutoff))
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
                # Reserve the writer before reading; an empty DELETE batch otherwise
                # leaves a read snapshot that another worker can invalidate in WAL mode.
                db.execute('BEGIN IMMEDIATE')
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
