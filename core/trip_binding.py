"""Server-owned operation bindings; snapshots stay in existing message receipts."""
from copy import deepcopy
import hashlib
import json
import re
from core.tasks import utc_now

VERSION = 'trip-binding-v1'
TARGETS = {'current_trip', 'message_reference', 'bound_operation'}


def snapshot_digest(trip):
    value = {k: trip.get(k) for k in ('trip_id', 'version', 'spec', 'plan', 'sources')}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def reference_required(text):
    # A missing transport reference must not become permission to edit the sole trip.
    return bool(re.search(r'(?:我|本轮|这次)?引用(?:的|过的|了的)?(?:这|那)?(?:份|个)?(?:方案|行程)|被引(?:用)?(?:的)?(?:方案|行程)', text))


def operation_binding(event, trip, *, operation='update', previous=None):
    binding = deepcopy(previous or {})
    binding.update(version=VERSION, operation=operation, status='active')
    if event.message_reference and event.message_reference.get('trip'):
        binding['origin'] = 'message_reference'
    elif re.search(r'TR-[a-f0-9]{12}', event.content):
        binding['origin'] = 'selection'
    else:
        binding.setdefault('origin', 'current_trip')
    source = (event.message_reference or {}).get('trip')
    if source and not (operation=='create_reference' and binding.get('source')
            and binding.get('target',{}).get('trip_id')==source['trip_id']):
        ref = event.message_reference
        binding['source'] = {'message_id': ref.get('message_id'), 'event_id': ref.get('event_id'),
            'trip_id': source['trip_id'], 'version': source.get('version', 1),
            'draft': ref.get('draft'), 'sha256': snapshot_digest(source)}
    if trip:
        key = trip.get('_draft_key', ('', 0))
        binding['target'] = {'trip_id': trip['trip_id'],
            'version': 0 if trip.get('_new_trip') else trip.get('version', 0),
            'draft_id': key[0], 'draft_version': key[1]}
    return binding


def capture(store, event, credential=None):
    """Freeze identities before queueing, without copying itinerary business content."""
    trips = {t['trip_id']: {'trip_id': t['trip_id'], 'version': t['version'],
        'draft_id': '', 'draft_version': 0} for t in store.trips.list_for_owner(event)}
    drafts = store.trips.current_drafts(event)
    for task in drafts:
        identity = task.slots['trip_id']
        trips.setdefault(identity, {'trip_id': identity, 'version': 0})
        trips[identity].update(draft_id=task.task_id, draft_version=task.version)
    tasks = store.tasks.recent(event.platform, event.storage_scope_id, event.sender_id)
    operations = {t.task_id: t for t in (*tasks, *drafts) if t.status == 'collecting'
        and t.slots.get('trip_binding', {}).get('status') == 'active'}
    selected = None
    if credential:
        task = store.tasks.owned(event, credential['task_id'])
        if (not task or task.version != credential['version'] or task.status != 'collecting'
                or task.is_expired(utc_now()) or task.task_id not in operations):
            raise ValueError('编辑目标已变化、过期或不属于本会话，请选择当前方案继续。')
        selected = task
    elif credential is None:
        with store._connect() as db:
            row=db.execute("SELECT result_text FROM event_tool_results WHERE tool_name='trip_operation' AND event_id LIKE ? ORDER BY rowid DESC LIMIT 1",
                (f'{event.platform}:{event.channel}:{event.scope_id}:%',)).fetchone()
        focus=json.loads(row[0]) if row else None
        task=operations.get(focus['task_id']) if focus and focus['status']=='collecting' else None
        if task and task.version==focus['version'] and not task.is_expired(utc_now()):selected=task
        elif not focus and len(operations)==1:selected=next(iter(operations.values()))
    return {'version': VERSION, 'targets': list(trips.values()),
        'focus_disabled': credential is False,
        'continuation': {'task_id': selected.task_id, 'version': selected.version} if selected else None}


def continuation(store, event, pending=None):
    frozen = event.trip_context
    credential = frozen.get('continuation') if frozen else None
    if credential:
        task = store.tasks.owned(event, credential['task_id'])
        if (task and task.version == credential['version'] and task.status == 'collecting'
                and not task.is_expired(utc_now())):
            return task
        return None
    if frozen is None and pending and pending.slots.get('trip_binding', {}).get('status') == 'active':
        return pending
    return None


def source_snapshot(store, event, binding):
    source = binding.get('source', {})
    if not source.get('message_id') or event.platform != 'web':
        return None
    # Revalidate the original owned message, then read its immutable receipt.
    with store._connect() as db:
        if source['message_id'].startswith('d:'):
            owned=db.execute('''SELECT 1 FROM web_deliveries d JOIN outbound_message_links l
                ON l.message_id='web-'||d.id AND l.platform=? AND l.scope_id=? AND l.owner_id=?
                WHERE d.id=? AND d.conversation_id=? AND l.event_id=?''',
                (event.platform,event.storage_scope_id,event.sender_id,source['message_id'][2:],event.scope_id,source['event_id'])).fetchone()
        else:
            owned=db.execute('SELECT 1 FROM web_requests WHERE event_key=? AND conversation_id=?',
                (source['message_id'].removeprefix('u:'),event.scope_id)).fetchone()
        if not owned:return None
        row=db.execute("SELECT result_text FROM event_tool_results WHERE event_id=? AND tool_name='trip_presentation'",
            (source['event_id'],)).fetchone()
        if row:trip=json.loads(row[0]).get('trip')
        else:
            row=db.execute('''SELECT v.snapshot_json FROM trip_versions v JOIN trips t ON t.trip_id=v.trip_id
                WHERE v.event_id=? AND t.platform=? AND t.scope_id=? AND t.owner_id=?''',
                (source['event_id'],event.platform,event.storage_scope_id,event.sender_id)).fetchone()
            trip=json.loads(row[0]) if row else None
    return trip if trip and snapshot_digest(trip) == source.get('sha256') else None


def legacy_copy_task(store, event):
    """Migrate only an incomplete creation with the exact same captured source."""
    source=(event.message_reference or {}).get('trip')
    if not source:return None
    with store._connect() as db:
        rows=db.execute('''SELECT t.*,j.payload_json FROM travel_tasks t JOIN inbox_jobs j ON j.event_key=t.source_event_id
            WHERE t.platform=? AND t.scope_id=? AND t.owner_id=? AND t.task_type='itinerary'
            AND t.status='collecting' AND json_extract(t.slots_json,'$.candidate_trip') IS NULL
            AND json_extract(t.slots_json,'$.trip_binding') IS NULL''',
            (event.platform,event.storage_scope_id,event.sender_id)).fetchall()
    matches=[]
    for row in rows:
        original=json.loads(row['payload_json']).get('message_reference',{}).get('trip')
        if original and snapshot_digest(original)==snapshot_digest(source):matches.append(store.tasks._record(row))
    return matches[0] if len(matches)==1 else None
