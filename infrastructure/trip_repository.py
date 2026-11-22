import hashlib
import json
from dataclasses import replace

from core.tasks import utc_now
from core.context_contract import trip_answer_basis
from infrastructure.task_repository import TaskConflict


class DraftActionConflict(TaskConflict):
    def __init__(self,message,latest=None):
        super().__init__(message)
        self.latest=latest


class TripRepository:
    def __init__(self, store):
        self.store = store
        with store._connect() as connection:
            connection.executescript('''
                CREATE TABLE IF NOT EXISTS trips (
                    trip_id TEXT PRIMARY KEY, platform TEXT NOT NULL, scope_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL, title TEXT NOT NULL, status TEXT NOT NULL,
                    version INTEGER NOT NULL, spec_json TEXT NOT NULL, plan_json TEXT NOT NULL,
                    sources_json TEXT NOT NULL, source_event_id TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_trips_owner ON trips(platform, scope_id, owner_id, updated_at);
                CREATE TABLE IF NOT EXISTS trip_versions (
                    trip_id TEXT NOT NULL, version INTEGER NOT NULL, event_id TEXT NOT NULL UNIQUE,
                    snapshot_json TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY(trip_id, version), FOREIGN KEY(trip_id) REFERENCES trips(trip_id)
                );
            ''')

    @staticmethod
    def new_id(event_key):
        return 'TR-' + hashlib.sha256(event_key.encode()).hexdigest()[:12]

    @staticmethod
    def _record(row, connection=None):
        if not row:
            return None
        value = dict(row)
        for key in ('spec', 'plan', 'sources'):
            value[key] = json.loads(value.pop(key + '_json'))
        if connection is not None:
            # Exact version only: old saves kept narrative in their presentation receipt.
            saved=connection.execute('''SELECT v.snapshot_json,e.result_text FROM trip_versions v
                LEFT JOIN event_tool_results e ON e.operation_key='trip-presentation:' || v.event_id
                AND e.event_id=v.event_id AND e.tool_name='trip_presentation'
                WHERE v.trip_id=? AND v.version=?''',(value['trip_id'],value['version'])).fetchone()
            if saved:
                basis=trip_answer_basis(value)
                sources=[json.loads(saved['snapshot_json'])]
                if saved['result_text']:sources.append(json.loads(saved['result_text']).get('trip',{}))
                for source in sources:
                    if (source.get('trip_id')==value['trip_id'] and source.get('version')==value['version']
                            and source.get('answer') and source.get('answer_basis')==basis):
                        value.update(answer=source['answer'],answer_basis=basis)
                        break
        return value

    def list_for_owner(self, event):
        with self.store._connect() as connection:
            return tuple(self._record(row,connection) for row in connection.execute('''
                SELECT * FROM trips WHERE platform=? AND scope_id=? AND owner_id=? AND status='active'
                ORDER BY updated_at DESC, rowid DESC LIMIT 30
            ''', (event.platform, event.storage_scope_id, event.sender_id)).fetchall())

    def get(self, event, trip_id):
        with self.store._connect() as connection:
            return self._record(connection.execute('''SELECT * FROM trips
                WHERE trip_id=? AND platform=? AND scope_id=? AND owner_id=?''',
                (trip_id, event.platform, event.storage_scope_id, event.sender_id)).fetchone(),connection)

    def planning_dates_for_event(self, event, source_event):
        with self.store._connect() as db:
            row = db.execute('''SELECT v.snapshot_json FROM trip_versions v JOIN trips t ON t.trip_id=v.trip_id
                WHERE v.event_id=? AND t.platform=? AND t.scope_id=? AND t.owner_id=?''',
                (source_event, event.platform, event.storage_scope_id, event.sender_id)).fetchone()
        if not row:
            return None
        spec = json.loads(row['snapshot_json'])['spec']
        return {key: spec[key] for key in ('start_date', 'end_date', 'day_count', 'date_is_tentative',
                'date_reference_day', 'date_preference_text', 'defer_start_date') if key in spec}

    def document_options(self, event):
        with self.store._connect() as connection:
            return tuple(dict(row) for row in connection.execute(
                'SELECT id, filename, created_at FROM documents WHERE group_openid=? ORDER BY id DESC LIMIT 10',
                (event.storage_scope_id,)).fetchall())

    def referenced(self, event):
        """Resolve only an actual reply link, including completed/expired planning tasks."""
        with self.store._connect() as connection:
            row = connection.execute('''SELECT p.* FROM outbound_message_links l
                JOIN task_event_results e ON e.event_id=l.event_id
                JOIN travel_tasks t ON t.task_id=e.task_id
                JOIN trips p ON p.trip_id=json_extract(t.slots_json,'$.trip_id')
                WHERE l.platform=? AND l.scope_id=? AND l.message_id=?
                AND p.platform=? AND p.scope_id=? AND p.owner_id=? AND p.status='active'
                LIMIT 1''', (event.platform,event.storage_scope_id,event.reply_to_id,
                            event.platform,event.storage_scope_id,event.sender_id)).fetchone()
            return self._record(row,connection)

    def document_context(self, event, document_id):
        with self.store._connect() as connection:
            connection.execute('BEGIN')
            row = connection.execute('SELECT id, filename FROM documents WHERE id=? AND group_openid=?',
                                     (document_id, event.storage_scope_id)).fetchone()
            if not row:
                raise TaskConflict('所选文档已不可用或不属于当前群，请重新选择。')
            chunks = connection.execute('SELECT content FROM document_chunks WHERE document_id=? ORDER BY chunk_index LIMIT 6',
                                        (document_id,)).fetchall()
        return (f"[文档#{row['id']} {row['filename']}]\n" + '\n'.join(chunk['content'] for chunk in chunks))[:3200]

    def linked_reminders(self, event, trip_id, *, include_cancelled=False):
        with self.store._connect() as connection:
            return tuple(dict(row) for row in connection.execute('''
                SELECT r.*, o.status AS delivery_status FROM personal_reminders r
                JOIN reminder_occurrences o ON o.reminder_id=r.reminder_id AND o.version=r.version
                WHERE r.platform=? AND r.group_id=? AND r.owner_id=?
                AND json_extract(r.source_json, '$.trip_id')=? AND (? OR r.status='active')
                ORDER BY r.updated_at DESC, r.rowid DESC
            ''', (event.platform, event.scope_id, event.sender_id, trip_id, include_cancelled)).fetchall())

    def draft_history(self, event, trip):
        """Read scoped draft history without interpreting or reconstructing constraints."""
        with self.store._connect() as db:
            rows = db.execute('''SELECT * FROM travel_tasks WHERE platform=? AND scope_id=? AND owner_id=?
                AND task_type='itinerary' AND json_extract(slots_json,'$.trip_id')=?
                ORDER BY rowid DESC LIMIT 30''',
                (event.platform,event.storage_scope_id,event.sender_id,trip['trip_id'])).fetchall()
        return tuple(self.store.tasks._record(row) for row in rows)

    def current_drafts(self,event,trip_id=''):
        """Draft lifetime is independent of the short confirmation task lease."""
        with self.store._connect() as db:
            rows=db.execute('''SELECT * FROM travel_tasks WHERE platform=? AND scope_id=? AND owner_id=?
                AND task_type='itinerary' AND (?='' OR json_extract(slots_json,'$.trip_id')=?)
                ORDER BY rowid DESC LIMIT 100''',
                (event.platform,event.storage_scope_id,event.sender_id,trip_id,trip_id)).fetchall()
        result=[];seen=set()
        for row in rows:
            task=self.store.tasks._record(row);slots=task.slots;trip_id=slots.get('trip_id')
            if not trip_id or trip_id in seen:continue
            seen.add(trip_id)
            if (task.status=='collecting' and slots.get('candidate_trip',{}).get('status') in {'active','cancelled'}
                    and not slots.get('trip_already_saved')):result.append(task)
        return tuple(result)

    def draft_state(self,db,event,identity,*,captured_version=None,now=None):
        """Read an owned draft by identity; content lifetime does not use recent-task TTL."""
        row=db.execute('''SELECT * FROM travel_tasks WHERE task_id=? AND platform=? AND scope_id=?
            AND owner_id=? AND task_type='itinerary' ''',
            (identity,event.platform,event.storage_scope_id,event.sender_id)).fetchone()
        if not row:raise DraftActionConflict('这份草案不存在或不属于当前会话。')
        task=self.store.tasks._record(row);slots=task.slots;trip_id=slots.get('trip_id')
        if not trip_id:raise DraftActionConflict('该任务不是可操作的行程草案。')
        latest_row=db.execute('''SELECT * FROM travel_tasks WHERE platform=? AND scope_id=? AND owner_id=?
            AND task_type='itinerary' AND json_extract(slots_json,'$.trip_id')=? ORDER BY rowid DESC LIMIT 1''',
            (event.platform,event.storage_scope_id,event.sender_id,trip_id)).fetchone()
        latest=self.store.tasks._record(latest_row)
        current=self._record(db.execute('''SELECT * FROM trips WHERE trip_id=? AND platform=? AND scope_id=? AND owner_id=?''',
            (trip_id,event.platform,event.storage_scope_id,event.sender_id)).fetchone(),db)
        if task.status=='cancelled':state='discarded'
        elif task.status=='completed':state='saved'
        elif task.status=='superseded' or latest.task_id!=task.task_id or captured_version is not None and captured_version!=task.version:state='superseded'
        elif task.status!='collecting' or not slots.get('candidate_trip'):state='unavailable'
        elif current and (current['status']!='active' or current['version']!=slots.get('expected_trip_version')):state='stale'
        elif task.is_expired(now or utc_now()):state='expired'
        else:state='ready'
        active=state in {'ready','expired','stale'}
        operation=None
        job=db.execute('''SELECT event_key,status,payload_json,id FROM inbox_jobs WHERE platform=? AND scope_id=? AND owner_id=?
            AND json_extract(payload_json,'$.draft_action.id')=? AND json_extract(payload_json,'$.draft_action.version')=?
            ORDER BY id DESC LIMIT 1''',(event.platform,event.scope_id,event.sender_id,identity,task.version)).fetchone()
        if job:
            action=json.loads(job['payload_json'])['draft_action']
            receipt=db.execute('SELECT result_text FROM event_tool_results WHERE operation_key=?',('draft-action:'+action['attempt_event'],)).fetchone()
            outcome=json.loads(receipt[0]) if receipt else {}
            if outcome.get('status')=='running' and job['status'] not in {'pending','running','retry'}:
                outcome={**outcome,'status':job['status']}
            operation={'action':action['action'],'status':outcome.get('status',job['status']),'job_id':job['id']}
            if outcome.get('status') in {'failed','unknown'}:operation['message']=outcome.get('reply','')
        view={'id':identity,'version':task.version,'state':state,'base_version':slots.get('expected_trip_version',0),
            'latest':{'id':latest.task_id,'version':latest.version},'can_revalidate':active,'can_discard':active,
            'operation':operation}
        return {'task':task,'current':current,'latest':latest,'view':view}

    def prepare_draft_action(self,db,event,data):
        """Freeze one attempt for double clicks; a later explicit click may retry a terminal failure."""
        previous=db.execute('''SELECT event_key,status,payload_json FROM inbox_jobs WHERE platform=? AND scope_id=? AND owner_id=?
            AND json_extract(payload_json,'$.draft_action.id')=? AND json_extract(payload_json,'$.draft_action.version')=?
            AND json_extract(payload_json,'$.draft_action.action')=? ORDER BY id DESC LIMIT 1''',
            (event.platform,event.scope_id,event.sender_id,data['id'],data['version'],data['action'])).fetchone()
        if previous:
            root=json.loads(previous['payload_json'])['draft_action']['attempt_event']
            receipt=db.execute('SELECT result_text FROM event_tool_results WHERE operation_key=?',('draft-action:'+root,)).fetchone()
            outcome=json.loads(receipt[0]) if receipt else {}
            primary=db.execute('SELECT status FROM inbox_jobs WHERE event_key=?',(root,)).fetchone()
            if outcome.get('status')=='completed' or primary and primary[0] in {'pending','running','retry'}:
                return {**data,'attempt_event':root}
        state=self.draft_state(db,event,data['id'])
        if state['task'].version!=data['version'] or not state['view']['can_'+data['action']]:
            raise DraftActionConflict('草案已保存、放弃、被替代或修订已变化，请查看当前草案。',state['view']['latest'])
        return {**data,'attempt_event':event.event_key}

    def action_outcome(self,event,data):
        with self.store._connect() as db:
            # The accepted server-side attempt must still belong to this exact target and owner.
            job=db.execute('SELECT payload_json FROM inbox_jobs WHERE event_key=? AND platform=? AND scope_id=? AND owner_id=?',
                (data['attempt_event'],event.platform,event.scope_id,event.sender_id)).fetchone()
            if job:
                original=json.loads(job[0]).get('draft_action',{})
                if any(original.get(k)!=data.get(k) for k in ('id','version','action','attempt_event')):
                    raise DraftActionConflict('草案操作身份不一致。')
            elif data['attempt_event']!=event.event_key:raise DraftActionConflict('草案操作不属于当前请求。')
            receipt=db.execute('SELECT result_text FROM event_tool_results WHERE operation_key=?',('draft-action:'+data['attempt_event'],)).fetchone()
        return json.loads(receipt[0]) if receipt else None

    def finish_draft_action(self,event,claim,data,status,reply):
        with self.store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self._finish_draft_action(db,event,claim,data,status,reply)
        return reply

    @staticmethod
    def _finish_draft_action(db,event,claim,data,status,reply,task_id=None):
        owner=db.execute("SELECT 1 FROM processed_events WHERE event_id=? AND claim_token=? AND status='processing'",(event.event_key,claim.claim_token)).fetchone()
        if not owner:raise TaskConflict('草案操作处理权已变化。')
        value={'status':status,'reply':reply,'target_id':data['id'],'target_version':data['version'],'action':data['action']}
        if task_id:value['result_draft_id']=task_id
        db.execute('UPDATE event_tool_results SET result_text=? WHERE operation_key=? AND event_id=?',
            (json.dumps(value,ensure_ascii=False),'draft-action:'+data['attempt_event'],event.event_key))
        db.execute('UPDATE processed_events SET prepared_reply=?,prepared_memory_content=? WHERE event_id=?',
            (reply,event.content,event.event_key))

    def rail_snapshots(self, event, queried_at):
        """Read full query receipts only within this principal's conversation."""
        with self.store._connect() as db:
            rows=db.execute('''SELECT e.result_text FROM event_tool_results e JOIN inbox_jobs j ON j.event_key=e.event_id
                WHERE j.platform=? AND j.scope_id=? AND j.owner_id=? AND e.tool_name='query_train_tickets'
                AND json_extract(e.result_text,'$.payload.retrieved_at')=?''',
                (event.platform,event.scope_id,event.sender_id,queried_at)).fetchall()
        return [json.loads(row['result_text']).get('payload',{}) for row in rows]

    def commit(self, event, claim, task_update, reply, *, trip=None, expected_version=None,
               expected_trip=None, expected_draft=None, expected_artifact=None, adopt_artifact=False,
               reminder_changes=(), supersede_request='', preference_update=None, now=None,presentation=None,
               draft_action=None,draft_guard=None):
        now = now or utc_now()
        timestamp = now.isoformat()
        with self.store._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            current = connection.execute('SELECT * FROM processed_events WHERE event_id=?', (event.event_key,)).fetchone()
            if not current or current['claim_token'] != claim.claim_token or current['status'] != 'processing':
                raise TaskConflict('事件处理权已变化，请重新查看行程后重试。')
            if connection.execute('SELECT 1 FROM task_event_results WHERE event_id=?', (event.event_key,)).fetchone():
                return current['prepared_reply'] or reply
            if draft_guard:
                state=self.draft_state(connection,event,draft_guard[0],now=now)
                if state['task'].version!=draft_guard[1] or not state['view']['can_revalidate']:
                    raise DraftActionConflict('草案已被放弃或更新，本次迟到的核对结果未发布。',state['view']['latest'])
                if len(draft_guard)>2 and (state['current']['version'] if state['current'] else 0)!=draft_guard[2]:
                    raise DraftActionConflict('核对期间已保存行程再次变化，未覆盖新版本。',state['view']['latest'])
                pending_discard=connection.execute('''SELECT 1 FROM inbox_jobs WHERE platform=? AND scope_id=? AND owner_id=?
                    AND json_extract(payload_json,'$.draft_action.id')=? AND json_extract(payload_json,'$.draft_action.version')=?
                    AND json_extract(payload_json,'$.draft_action.action')='discard' AND status IN ('pending','running','retry')''',
                    (event.platform,event.scope_id,event.sender_id,*draft_guard[:2])).fetchone()
                if pending_discard and (not draft_action or draft_action['action']!='discard'):
                    raise DraftActionConflict('已收到放弃这份草案的请求，本次核对结果未发布。',state['view']['latest'])
            if expected_trip:
                existing = connection.execute('''SELECT version FROM trips WHERE trip_id=? AND platform=?
                    AND scope_id=? AND owner_id=? AND status='active' ''',
                    (expected_trip[0], event.platform, event.storage_scope_id, event.sender_id)).fetchone()
                if not existing or existing['version'] != expected_trip[1]:
                    raise TaskConflict('关联行程已经变化，请重新核对。')
            if expected_draft:
                latest = connection.execute('''SELECT task_id,version,status,slots_json FROM travel_tasks
                    WHERE platform=? AND scope_id=? AND owner_id=? AND task_type='itinerary'
                    AND json_extract(slots_json,'$.trip_id')=? ORDER BY rowid DESC LIMIT 1''',
                    (event.platform,event.storage_scope_id,event.sender_id,expected_draft[0])).fetchone()
                slots = json.loads(latest['slots_json']) if latest else {}
                actual = ((latest['task_id'],latest['version']) if latest and latest['status']=='collecting'
                    and slots.get('candidate_trip') and not slots.get('trip_already_saved') else ('',0))
                if actual != expected_draft[1:]:
                    raise TaskConflict('待确认草稿已被另一条消息更新，请重新查看后再修改。')
            if expected_artifact:
                artifact=connection.execute('''SELECT version,status,capture_version FROM planning_artifacts WHERE artifact_id=?
                    AND platform=? AND scope_id=? AND owner_id=?''',
                    (expected_artifact[0],event.platform,event.storage_scope_id,event.sender_id)).fetchone()
                from infrastructure.planning_repository import CAPTURE_VERSION
                if (not artifact or artifact['version']!=expected_artifact[1] or artifact['status']!='active'
                        or artifact['capture_version']<CAPTURE_VERSION):
                    raise TaskConflict('方案草稿已变化，请重新选择后规划。')
            semantic=task_update.slots.get('semantic_task')
            if semantic:task_update=replace(task_update,slots={k:v for k,v in task_update.slots.items() if k!='semantic_task'})
            task_id = self.store.tasks.apply(connection, event, task_update, now, reply=reply,
                **({'allow_expired_draft':True} if draft_action and draft_action['action']=='discard' else {}))
            if draft_action:
                self._finish_draft_action(connection,event,claim,draft_action,'completed',reply,task_id)
                if draft_action['action']=='discard':
                    revoked=connection.execute('''SELECT event_key,payload_json FROM inbox_jobs WHERE platform=? AND scope_id=? AND owner_id=?
                        AND json_extract(payload_json,'$.draft_action.id')=? AND json_extract(payload_json,'$.draft_action.version')=?
                        AND json_extract(payload_json,'$.draft_action.action')='revalidate' AND status IN ('pending','running','retry')''',
                        (event.platform,event.scope_id,event.sender_id,draft_action['id'],draft_action['version'])).fetchall()
                    for job in revoked:
                        connection.execute("UPDATE inbox_jobs SET status='cancelled',claim_token=NULL,lease_until=NULL,updated_at=? WHERE event_key=?",(timestamp,job['event_key']))
                        connection.execute("UPDATE processed_events SET status='completed',claim_token=NULL,lease_expires_at=NULL WHERE event_id=? AND prepared_reply IS NULL",(job['event_key'],))
                        root=json.loads(job['payload_json'])['draft_action']['attempt_event']
                        connection.execute("UPDATE event_tool_results SET result_text=? WHERE operation_key=?",(json.dumps({'status':'cancelled','reply':'这份草案已放弃，核对结果不会发布。'},ensure_ascii=False),'draft-action:'+root))
            if semantic:
                cursor = connection.execute('''UPDATE travel_tasks SET status='completed',version=version+1
                    WHERE task_id=? AND version=? AND status='collecting' AND task_type='semantic'
                    AND platform=? AND scope_id=? AND owner_id=?''',
                    (semantic['task_id'],semantic['version'],event.platform,event.storage_scope_id,event.sender_id))
                if cursor.rowcount != 1:
                    raise TaskConflict('行程选择已变化，请重新提出调整。')
            if supersede_request:
                connection.execute('''UPDATE travel_tasks SET status='superseded',version=version+1
                    WHERE platform=? AND scope_id=? AND owner_id=? AND task_type IN ('transit','walking')
                    AND status='collecting' AND initial_request=?''',
                    (event.platform,event.storage_scope_id,event.sender_id,supersede_request))
            if (task_update.task_type == 'itinerary' and task_update.status == 'collecting'
                    and task_update.slots.get('trip_id')):
                connection.execute('''UPDATE travel_tasks SET status='superseded', version=version+1
                    WHERE task_type='itinerary' AND status='collecting' AND task_id != ?
                    AND platform=? AND scope_id=? AND owner_id=? AND json_extract(slots_json, '$.trip_id')=?''',
                    (task_id, event.platform, event.storage_scope_id, event.sender_id,
                     task_update.slots['trip_id']))
            if trip is not None:
                snapshot = {key: trip[key] for key in ('trip_id', 'title', 'spec', 'plan', 'sources', 'status')}
                if trip.get('answer'):
                    if trip.get('answer_basis')==trip_answer_basis(trip):
                        snapshot.update(answer=trip['answer'],answer_basis=trip['answer_basis'])
                if expected_version is None:
                    connection.execute('''INSERT INTO trips VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)''',
                        (trip['trip_id'], event.platform, event.storage_scope_id, event.sender_id, trip['title'],
                         trip['status'], json.dumps(trip['spec'], ensure_ascii=False), json.dumps(trip['plan'], ensure_ascii=False),
                         json.dumps(trip['sources'], ensure_ascii=False), event.event_key, timestamp, timestamp))
                    version = 1
                else:
                    cursor = connection.execute('''UPDATE trips SET title=?, status=?, spec_json=?, plan_json=?,
                        sources_json=?, version=version+1, updated_at=?
                        WHERE trip_id=? AND platform=? AND scope_id=? AND owner_id=? AND version=? AND status='active'
                    ''', (trip['title'], trip['status'], json.dumps(trip['spec'], ensure_ascii=False),
                          json.dumps(trip['plan'], ensure_ascii=False), json.dumps(trip['sources'], ensure_ascii=False),
                          timestamp, trip['trip_id'], event.platform, event.storage_scope_id, event.sender_id, expected_version))
                    if cursor.rowcount != 1:
                        raise TaskConflict('行程版本已变化，请查看最新行程后重新修改。')
                    version = expected_version + 1
                snapshot['version'] = version
                connection.execute('INSERT INTO trip_versions VALUES (?, ?, ?, ?, ?)',
                    (trip['trip_id'], version, event.event_key, json.dumps(snapshot, ensure_ascii=False), timestamp))
            for action, reminder in reminder_changes:
                self.store.reminders.apply_change(connection, event, task_id, action, reminder, now)
            if preference_update:
                self.store.preferences.apply_update(connection, event, preference_update['version'], preference_update['values'])
            if adopt_artifact and expected_artifact:
                connection.execute("UPDATE planning_artifacts SET status='adopted',version=version+1,updated_at=? WHERE artifact_id=?",
                                   (timestamp,expected_artifact[0]))
            if presentation is not None:
                value={**presentation,'content_sha256':hashlib.sha256(reply.encode()).hexdigest()}
                connection.execute('''INSERT OR IGNORE INTO event_tool_results VALUES (?,?,?,?,?,?)''',
                    ('trip-presentation:'+event.event_key,event.event_key,'trip_presentation','{}',
                     json.dumps(value,ensure_ascii=False),timestamp))
            connection.execute('UPDATE processed_events SET prepared_reply=?, prepared_memory_content=?, updated_at=? WHERE event_id=?',
                               (reply, event.content, timestamp, event.event_key))
        return reply
