"""Reminder workflows: one prepared operation, with distinct state semantics per kind."""
from infrastructure.skill_manager import skill_text, skill_request
import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone, date
from core.tasks import TaskUpdate, utc_now
from core.commands import scheduled_query_control, watch_control, location_answer
from core.time_input import BEIJING, is_time_answer, parse_reminder_time, number
from core.model_budget import model_call_context
from core.context_contract import complete_json, response_json
from tools.reminders.policy import BookingPolicyResolver, PolicyUnavailable
from core.tool_result import AgentToolContext, ReadFailed
from core.tool_execution import retry_read


LEGACY_RESOURCE = re.compile(r'\b(?:R-\d{8}-\d{3}|A-\d{6})\b')


def reminder_request(text):
    if LEGACY_RESOURCE.search(text) or text.strip() in {'查看预约提醒', '确认创建预约提醒'}:
        return False
    return bool(re.search(r'提醒我|叫我|通知我|(?:给我|帮我).{0,8}(?:提醒|备忘)|不用提醒|不必提醒|(?:查看|看看|列出|有哪些|我的|取消|修改|设置).{0,12}提醒|提醒.{0,8}(?:改成|改为|取消|不用)|(?:刚才|上一条).{0,8}(?:改成|改为|取消)', text))


def pending_reminder(tasks):
    values = [task for task in tasks if task.task_type == 'reminder' and task.status == 'collecting']
    return values[0] if len(values) == 1 else None


def reminder_continuation(text, tasks):
    task = pending_reminder(tasks)
    if not task:
        return None
    if is_time_answer(text) or text.strip() in {'取消这项任务', '先不设置了', '不设了'}:
        return task
    if 'reference' in task.missing_slots:
        if re.fullmatch(r'(?:第)?[1-9]\d*(?:个|条)?[。 ]*', text):
            return task
        if text.strip() in task.slots.get('candidate_titles', []):
            return task
    if 'title' in task.missing_slots and 0 < len(text.strip()) <= 200:
        if not re.search(r'天气|路况|怎么|查询|提醒我|查看任务', text):
            return task
    return None


DATE_PATTERN = r'(?:(?:20\d{2}年)?\d{1,2}月\d{1,2}[日号]?|20\d{2}-\d{1,2}-\d{1,2}|大后天|后天|明天|今天)'


CONFIRM_POLICY = {'按这个规则设置提醒', '确认按此规则提醒', '按这个规则设置'}


def booking_request(text):
    prefix = re.split(r'提醒我|叫我|通知我', text, maxsplit=1)[0]
    return bool(re.search(r'开售|开约|放票|可以预约|能预约', prefix)
                and re.search(r'提醒我|叫我|通知我', text)
                and not re.search(r'不要|不用|不必|只是问|先别', text))


def policy_query(text):
    return bool(re.search(r'预约规则|怎么预约|如何预约|什么时候.{0,8}放票|何时.{0,8}开约|提前几天', text)
                and not re.search(r'提醒我|叫我|通知我|R-\d{8}-\d{3}|A-\d{6}', text))


def booking_continuation(text, tasks):
    waiting = [task for task in tasks if task.task_type in {'booking_reminder', 'booking_policy_query'} and task.status == 'collecting']
    if len(waiting) != 1:
        return None
    task = waiting[0]
    if text in CONFIRM_POLICY or text == '重新查询预约规则' or re.fullmatch(r'https://\S+', text):
        return task
    if 'visit_date' in task.missing_slots and re.fullmatch(DATE_PATTERN + r'[。 ]*', text):
        return task
    if 'entity' in task.missing_slots and re.fullmatch(r'[\u4e00-\u9fff]{2,30}(?:博物馆|景区|美术馆|纪念馆)', text):
        return task
    return None


QUERY_TOOLS = {'weather': ('get_current_weather', ('location',)), 'forecast': ('get_weather_forecast', ('location',)),
               'traffic': ('get_route_traffic', ('origin', 'destination'))}


def scheduled_query_request(text):
    return bool(re.search(r'天气|路况', text) and re.search(r'告诉我|发给我|定时查询|届时查询|到时查', text)
                and re.search(r'定时查询|到时|届时|明天|后天|今晚|点|[:：]|分钟后|小时后|月\d', text)
                and not re.search(r'提醒我|不要|不用|不必|只是问', text))




def scheduled_query_continuation(text, tasks):
    pending = [task for task in tasks if task.task_type == 'scheduled_query' and task.status == 'collecting']
    if len(pending) == 1 and (is_time_answer(text) or location_answer(text)):
        return pending[0]
    return None


def watch_request(text):
    return bool(re.match(r'^(?:帮我|请|我想让你)?(?:持续)?(?:监测|监控|关注)', text.strip())
                and re.search(r'预约规则|放票规则|开约规则', text))




def watch_continuation(text, tasks):
    pending = [task for task in tasks if task.task_type == 'policy_watch' and task.status == 'collecting']
    if len(pending) != 1:
        return None
    task = pending[0]
    if ('ends_at' in task.missing_slots and is_time_answer(text)) or text.startswith('https://'):
        return task
    return None


POLICY_FIELDS = ('days_before', 'release_time', 'closed_weekdays', 'holiday_exceptions', 'caveats', 'official', 'identity_basis')


def policy_signature(policy):
    meaningful = {key: policy.get(key) for key in POLICY_FIELDS}
    return 'ok:' + hashlib.sha256(json.dumps(meaningful, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class ReminderIntentParser:
    def __init__(self, client, model):
        self.client = client
        self.model = model

    @skill_request
    def parse(self, text):
        with model_call_context('reminder_extract', prompt_version='reminder-intent-v1', stage_id='reminder.personal'):
            response = self.client.chat.completions.create(
                model=self.model,
                response_format={'type': 'json_object'},
                messages=[
                {'role': 'system', 'content': (
                    skill_text('reminder.personal', 'system')
                )},
                {'role': 'user', 'content': complete_json({'message':text}, 12000)},
                ],
            )
        value = response_json(response)
        if not isinstance(value, dict) or value.get('action') not in {'create', 'none', 'needs_input'}:
            raise ValueError('提醒意图解析结果无效，请换一种表达。')
        for key in ('title', 'time_text', 'question'):
            if not isinstance(value.get(key, ''), str):
                raise ValueError('提醒意图解析结果字段无效。')
        if value['action'] == 'create':
            if not value.get('title') or value['title'] not in text:
                raise ValueError('提醒事项无法对应到你的原话，请明确提醒事项。')
            if value.get('time_text') and value['time_text'] not in text:
                raise ValueError('提醒时间无法对应到你的原话，请明确提醒时刻。')
        return value


@dataclass(frozen=True)
class ReminderChanges:
    changes: list
    notes: list
    errors: list
    fingerprints: dict
    review: bool
    policy_requests: list


def change_plan(event_key, trip, linked, *, now, policies=None, failures=None, force_refresh=False):
    """Pure reminder diff. Missing policies are requests, never hidden I/O or commits."""
    by_entity = {json.loads(row['source_json']).get('entity'): row for row in linked}
    targets = set(trip['spec'].get('reminder_entities', [])) | set(by_entity)
    changes, notes, errors, fingerprints = [], [], [], {}
    review = False
    requests = []
    policies, failures = policies or {}, failures or {}
    if trip['spec'].get('date_is_tentative') and targets:
        note = '行程日期暂定；实际日期明确后，再核验预约规则并处理关联提醒。'
        if any(row['status'] != 'cancelled' for row in linked):
            note += ' 已有的关联提醒暂未调整，仍按原时刻执行。'
        return ReminderChanges([], [note], [], {}, False, [])
    for entity in sorted(target for target in targets if target):
        old = by_entity.get(entity)
        if old and old['status'] == 'cancelled':
            notes.append(f'{entity} 的提醒已由你取消，本次不会重新创建。')
            continue
        visits = [day['date'] for day in trip['plan']['days'] for activity in day['activities']
                  if trip['sources'][activity['poi_id']]['name'] == entity]
        if trip['status'] == 'cancelled' or not visits:
            if old and old['delivery_status'] != 'sent':
                changes.append(('cancel', old))
                notes.append(f"取消 {entity} 的关联提醒：该地点已不在本次行程中。")
                review = True
            continue
        if len(visits) != 1 or not visits[0]:
            errors.append(f'{entity}：需要唯一、明确的参观日期。')
            continue
        visit_date = visits[0]
        old_source = json.loads(old['source_json']) if old else {}
        if old and old['delivery_status'] == 'sent':
            notes.append(f'{entity} 的历史提醒已发送，不会重新发送或撤回。')
            continue
        if old and old_source.get('visit_date') == visit_date and not force_refresh:
            continue
        if old and old_source.get('manual_time_override') and not force_refresh:
            source = {**old_source, 'visit_date': visit_date, 'trip_version': trip['version']}
            changes.append(('update', {**old, 'source': source,
                'title': f'预约{entity}（参观日期 {visit_date}）'}))
            notes.append(f'{entity}：保留你人工指定的提醒时间，仅更新关联参观日期为 {visit_date}。')
            review = True
            continue
        if entity in failures:
            errors.append(f'{entity}：{failures[entity]}')
            continue
        if entity not in policies:
            source_url = trip['spec'].get('policy_sources', {}).get(entity) or old_source.get('policy', {}).get('source_url', '')
            requests.append((entity, visit_date, source_url))
            continue
        try:
            policy = policies[entity]
            if not policy.official:
                raise PolicyUnavailable('页面官方身份尚未核实，不能按该规则自动计算关联提醒。')
            applicability = policy.date_applicability or {}
            if applicability.get('date_status') in {'expired', 'not_yet_effective', 'outside_declared_window', 'conflicting_range'}:
                raise PolicyUnavailable('该规则不适用于目标日期或有效期冲突。')
            if date.fromisoformat(visit_date).weekday() in policy.closed_weekdays and not policy.holiday_exceptions:
                raise PolicyUnavailable('该日期按常规安排闭馆，需要更换日期或核实特殊公告。')
            instant = policy.opening_at(date.fromisoformat(visit_date))
            if instant <= now:
                raise PolicyUnavailable('计算出的开约时刻已过，请现在核对预约渠道或指定其他提醒时间。')
            evidence = policy.to_dict()
        except PolicyUnavailable as exc:
            errors.append(f'{entity}：{exc}')
            continue
        fingerprints[entity] = policy.fingerprint
        if old and old_source.get('visit_date') == visit_date and force_refresh:
            keys = ('days_before', 'release_time', 'closed_weekdays', 'holiday_exceptions', 'caveats')
            previous = old_source.get('policy', {})
            if all(json.dumps(previous.get(key), sort_keys=True) == json.dumps(evidence.get(key), sort_keys=True) for key in keys):
                notes.append(f'{entity}：已重新核查，适用规则无变化。')
                continue
        source = {'kind': 'booking_policy', 'trip_id': trip['trip_id'], 'trip_version': trip['version'],
                  'entity': entity, 'visit_date': visit_date, 'policy': evidence, 'policy_fingerprint': policy.fingerprint}
        operation_key = f'{event_key}:trip:{trip["trip_id"]}:{entity}'
        reminder = {**(old or {}), 'reminder_id': old['reminder_id'] if old else '',
            'source_operation_key': operation_key, 'title': f'预约{entity}（参观日期 {visit_date}）',
            'scheduled_at_utc': instant.astimezone(timezone.utc).isoformat(), 'source': source}
        if old_source.get('manual_time_override'):
            reminder['scheduled_at_utc'] = old['scheduled_at_utc']
            reminder['source']['manual_time_override'] = old_source['manual_time_override']
            notes.append(f"{entity}：保留人工提醒时间 {old['scheduled_at_utc']}（UTC）；以下为规则计算参考。")
        changes.append(('update' if old else 'create', reminder))
        timing_label = '规则推算参考' if old_source.get('manual_time_override') else '改为' if old else '拟定'
        notes.append(f"{entity}：参观 {visit_date}，{timing_label} {instant:%Y-%m-%d %H:%M}（北京时间）；来源 {policy.source_url}")
        if policy.caveats:
            notes.extend(policy.caveats)
        review = review or bool(old) or bool(policy.caveats)
    return ReminderChanges(changes, notes, errors, fingerprints, review, requests)



@dataclass(frozen=True)
class ReminderSelection:
    kind: str
    operation: str
    task: object | None = None
    reply: str = ''


class RemindersTool:
    change_plan = staticmethod(change_plan)

    def __init__(self, store, travel_service=None, *, resolver=None, client=None, model='',
                 clock=None, intent_parser=None, tool_executor=None):
        self.store, self.travel_service = store, travel_service
        self.resolver = resolver or BookingPolicyResolver()
        self.client, self.model = client, model
        self.clock = clock or utc_now
        self.intent_parser = intent_parser or (ReminderIntentParser(client, model) if client is not None else None)
        self.tool_executor = tool_executor

    def prepare_request(self, event, *, kind=None, tasks=None):
        text = event.content.strip()
        if tasks is None:
            tasks = self.store.tasks.recent(event.platform,event.storage_scope_id,event.sender_id,reply_to_id=event.reply_to_id)
        selectors = {'watch': self._select_watch, 'booking': self._select_booking,
                     'scheduled': self._select_query, 'personal': self._select_personal}
        for candidate in (kind,) if kind else ('watch', 'booking', 'scheduled', 'personal'):
            selected = selectors[candidate](text, tasks)
            if selected is not None:
                return selected
        return None

    def execute(self, selection, event, claim):
        if selection.reply:
            return selection.reply
        handlers = {'personal': self._personal_handle, 'booking': self._booking_handle,
                    'scheduled': self._query_handle, 'watch': self._watch_handle}
        return handlers[selection.kind](event, claim, selection)

    def apply_domain_intent(self,event,claim,ir,*,mode='execute',tasks=None):
        if (len(ir.operations)!=1 or ir.operations[0].values.keys()!={'request_text'}
                or ir.operations[0].values['request_text']!=event.content or not ir.source_spans):
            return '提醒或后台任务必须对应本轮完整请求，尚未创建任务。'
        operations={'create_booking_reminder':('booking','create'),'query_booking_policy':('booking','query'),
                    'create_scheduled_query':('scheduled','create'),'create_policy_watch':('watch','create')}
        selected=operations.get(ir.operations[0].operation)
        if not selected or ir.action!=('read' if selected[1]=='query' else 'create') or ir.missing_fields:
            return '任务动作或必要信息尚不明确，本次未执行。'
        if mode!='execute' or ir.requires_confirmation:
            return '已识别任务要求；当前只解析，尚未创建或执行。'
        return self.execute(ReminderSelection(*selected),event,claim)

    def handle(self, event, claim, *, kind=None):
        selection = self.prepare_request(event, kind=kind)
        return self.execute(selection, event, claim) if selection is not None else None

    def handle_personal(self, event, claim):
        return RemindersTool.handle(self, event, claim, kind='personal')

    def cancel(self, event, claim, reminder_id):
        rows = self.store.reminders.list_for_owner(event.platform, event.scope_id, event.sender_id)
        row = next((row for row in rows if row['reminder_id']==reminder_id and row['status']=='active'), None)
        if row is None:
            return '没有找到你在本群创建的这条提醒，请先查看我的提醒。'
        slots = {**row, 'action': 'cancel'}
        return self._personal_commit(event, claim, None, slots, f"已取消提醒：{row['title']}。", 'cancel')

    async def execute_due(self, kind, job, renderer):
        handlers = {'scheduled_query': self._query_due, 'policy_watch': self._watch_due}
        return await handlers[kind](job, renderer)

    @staticmethod
    def _select_watch(text, tasks):
        if watch_control(text):
            return ReminderSelection('watch', 'list' if text=='查看规则监测' else 'cancel')
        task = watch_continuation(text, tasks)
        return ReminderSelection('watch', 'create', task) if task or watch_request(text) else None

    @staticmethod
    def _select_booking(text, tasks):
        task = booking_continuation(text, tasks)
        query = policy_query(text)
        if task is None and not booking_request(text) and not query:
            return None
        operation = 'confirm' if task and text in CONFIRM_POLICY else 'query' if query else 'create'
        return ReminderSelection('booking', operation, task)

    @staticmethod
    def _select_query(text, tasks):
        if scheduled_query_control(text):
            return ReminderSelection('scheduled', 'list' if text.startswith('查看定时查询') else 'cancel')
        task = scheduled_query_continuation(text, tasks)
        return ReminderSelection('scheduled', 'create', task) if task or scheduled_query_request(text) else None

    @staticmethod
    def _select_personal(text, tasks):
        task = reminder_continuation(text, tasks)
        if task is None and is_time_answer(text) and len([
                item for item in tasks if item.task_type=='reminder' and item.status=='collecting']) > 1:
            return ReminderSelection('personal', 'reply', reply='你有多条正在设置的提醒，请回复其中一条设置问题，再补充时间。')
        if not reminder_request(text) and task is None:
            return None
        if re.search('不要(?:创建|设置)|别(?:创建|设置)|只是问|先别|(?:不要|不用|不必|别).{0,12}(?:提醒我|通知我|叫我)', text):
            return ReminderSelection('personal', 'reply', task, '没有创建提醒。需要时告诉我具体事项和时间。')
        if len(re.findall('提醒我|叫我|通知我', text)) > 1:
            return ReminderSelection('personal', 'reply', task, '这条消息包含多个提醒，请先分别发送每个事项和时间，避免把不同时间合并。')
        trigger_phrase = re.split('提醒我|叫我|通知我', text, maxsplit=1)[0]
        if re.search('每天|每周|每月|工作日|每隔', trigger_phrase):
            return ReminderSelection('personal', 'reply', task, '这类重复提醒还在升级中。现在可以指定一次提醒的日期和时刻。')
        if re.search('开售|开约|放票|可以预约|能预约', trigger_phrase):
            return ReminderSelection('personal', 'reply', task, '这是按预约或开售规则推导时间的任务。我需要先核实适用的官方规则，目前没有创建开约提醒。你可以提供官方说明，或先告诉我一个明确时间提醒你预约。')
        if re.search('到时.{0,6}(?:查询|查一下)|届时.{0,6}(?:查询|查一下)', trigger_phrase):
            return ReminderSelection('personal', 'reply', task, '届时自动执行查询属于定时查询任务，正在升级中。现在可以设置一条提醒你查询的通知。')

        if re.search(r'(?:查看|看看|列出|有哪些|我的).{0,12}提醒', text) and not re.search(r'修改|取消|改成|改为', text):
            return ReminderSelection('personal', 'list', task)
        if task and text in {'取消这项任务', '先不设置了', '不设了'}:
            return ReminderSelection('personal', 'cancel_task', task)
        action = task.slots.get('action', 'create') if task else 'create'
        if re.search(r'取消|不用提醒|不必提醒|删掉', text):
            action = 'cancel'
        elif re.search(r'改成|改为|修改|推迟|提前', text) and not task:
            action = 'update'
        return ReminderSelection('personal', action, task)

    @staticmethod
    def _personal_display(row):
        instant = datetime.fromisoformat(row['scheduled_at_utc']).astimezone(BEIJING)
        status = '已取消' if row['status'] == 'cancelled' else {
            'pending': '待提醒', 'queued': '等待投递', 'sent': '已发送', 'failed': '投递失败',
            'blocked': '群已不在允许范围', 'missed': '已错过补发窗口', 'cancelled': '已取消',
        }.get(row.get('delivery_status', 'pending'), '待提醒')
        return f"{row['title']} — {instant:%Y-%m-%d %H:%M}（北京时间，{status}，{row['reminder_id']}）"


    def _personal_handle(self, event, claim, selection):
        text = event.content.strip()
        continuation = selection.task
        if event.channel != 'group':
            return '当前支持在群内创建提醒，到期在原群 @ 创建者。'
        if selection.operation == 'list':
            rows = self.store.reminders.list_for_owner(event.platform, event.scope_id, event.sender_id)
            return '你还没有通用提醒。可以说“明天上午十点提醒我抢高铁票”。' if not rows else '\n'.join(self._personal_display(row) for row in rows)
        task = continuation
        if selection.operation == 'cancel_task':
            update = TaskUpdate('reminder', 'cancelled', task.initial_request, task.slots,
                                task_id=task.task_id, expected_version=task.version)
            return self.store.reminders.commit(event, claim, update, '已结束这项提醒设置任务，没有新建提醒。')
        slots = dict(task.slots) if task else {}
        title_answer = bool(task and 'title' in task.missing_slots)
        if title_answer:
            slots['title'] = text.strip(' ，,。！!')
        action = selection.operation
        if not task and action in {'update', 'cancel'}:
            # Only a scoped reminder reference can authorize changing an existing resource.
            rows = [row for row in self.store.reminders.list_for_owner(event.platform, event.scope_id, event.sender_id)
                    if row['status'] == 'active']
            ids = re.findall(r'M-[a-f0-9]{12}', text)
            matches = [row for row in rows if row['reminder_id'] in ids or row['title'] in text]
            if not matches and not ids and re.search(r'刚才|上一条', text):
                matches = rows[:1]
            if not matches and not ids and len(rows) == 1:
                matches = rows
            if len(matches) != 1:
                if not rows or ids:
                    return '没有找到你在本群创建的这条提醒，请先查看我的提醒。'
                slots = {'action': action, 'candidates': [row['reminder_id'] for row in rows],
                         'candidate_titles': [row['title'] for row in rows], 'change_text': text}
                return self._personal_question(event, claim, None, slots, ('reference',),
                    '要操作哪一条？回复序号或事项：\n' + '\n'.join(f'{index}. {self._personal_display(row)}' for index, row in enumerate(rows, 1)))
            slots.update(matches[0])
        elif task and 'reference' in task.missing_slots:
            rows = self.store.reminders.list_for_owner(event.platform, event.scope_id, event.sender_id)
            selected = None
            index = re.fullmatch(r'(?:第)?([1-9]\d*)(?:个|条)?[。 ]*', text)
            if index and int(index[1]) <= len(slots['candidates']):
                selected = slots['candidates'][int(index[1])-1]
            matches = [row for row in rows if row['reminder_id'] == selected or row['title'] == text]
            if len(matches) != 1:
                return '没有唯一匹配的提醒，请回复列表中的序号。'
            slots.update(matches[0])
            text = slots.get('change_text', text)
        slots['action'] = action
        semantic_time = None
        if action == 'create' and not slots.get('title'):
            match = re.search(r'(?:提醒我|叫我|通知我)(.+)', text)
            simple_prefix = re.split(r'提醒我|叫我|通知我', text, maxsplit=1)[0]
            # Reliable common syntax is handled without a model. Unusual word order uses semantic extraction.
            if match and re.search(r'明天|后天|今天|今晚|\d|[一二三四五六七八九十]+[月点时]', simple_prefix):
                slots['title'] = match[1].strip(' ，,。！!')
            elif self.intent_parser is not None:
                if re.search(r'(?:提醒我|叫我|通知我)[。！？!\s]*$', text):
                    parsed = parse_reminder_time(simple_prefix, event.occurred_at or self.clock())
                    slots.update(day=parsed.day, clock=parsed.clock)
                    return self._personal_question(event, claim, task, slots, ('title',), '要提醒你做什么事？')
                intent = self.intent_parser.parse(text)
                if intent['action'] != 'create':
                    return intent.get('question') or '没有创建提醒。请明确要提醒的事项和时间。'
                slots['title'] = intent['title']
                semantic_time = intent.get('time_text', '')
            elif match:
                slots['title'] = match[1].strip(' ，,。！!')
            else:
                parsed = parse_reminder_time(simple_prefix, event.occurred_at or self.clock())
                slots.update(day=parsed.day, clock=parsed.clock)
                return self._personal_question(event, claim, task, slots, ('title',), '要提醒你做什么事？')
            if not slots['title'] or len(slots['title']) > 200:
                return '请把提醒事项写在 1 到 200 个字符内。'
        if action == 'cancel':
            reply = f"已取消提醒：{slots['title']}。"
            return self._personal_commit(event, claim, task, slots, reply, action)
        # For create, only the phrase preceding 提醒我 is a trigger time. Dates in the title are not schedules.
        time_text = text if task or action != 'create' else re.split(r'提醒我|叫我|通知我', text, maxsplit=1)[0]
        if semantic_time is not None:
            time_text = semantic_time
        if title_answer:
            time_text = ''
        return self._personal_complete_time(event, claim, task, slots, time_text)


    def handle_intent(self, event, claim, *, task=None, title='', time_text='', disposition='answer'):
        """Execute validated semantic fields directly, without re-parsing a synthetic user sentence."""
        if event.channel != 'group':
            return '这个入口暂不支持提醒设置。'
        if task is not None:
            current = self.store.tasks.recent(event.platform, event.storage_scope_id, event.sender_id,
                                             reply_to_id=event.reply_to_id)
            if not any(item.task_id == task.task_id and item.version == task.version
                       and item.task_type == 'reminder' and item.status == 'collecting' for item in current):
                return '提醒设置已变化或过期，请查看最新任务后再补充。'
        slots = dict(task.slots) if task else {'action': 'create'}
        if disposition == 'cancel' and task is not None:
            update = TaskUpdate('reminder', 'cancelled', task.initial_request, slots,
                                task_id=task.task_id, expected_version=task.version)
            return self.store.reminders.commit(event, claim, update,
                '已结束这项提醒设置，没有创建新的提醒。已经存在的提醒保持不变。', now=self.clock())
        if 'reference' in (task.missing_slots if task else ()):
            return self.handle_personal(event, claim) or '请先明确要操作哪条已有提醒，再补充时间。'
        if slots.get('action') not in {'create', 'update'}:
            return self.handle_personal(event, claim)
        if title:
            if len(title) > 200 or title not in event.content:
                return '提醒事项无法对应你的原话，请明确提醒事项。'
            slots['title'] = title
        if not slots.get('title'):
            parsed = parse_reminder_time(time_text, event.occurred_at or self.clock(),
                                         day=slots.get('day', ''), clock=slots.get('clock', ''))
            slots.update(day=parsed.day, clock=parsed.clock)
            missing = ('title', 'period') if parsed.needs_period else ('title',)
            return self._personal_question(event, claim, task, slots, missing, '要提醒你做什么事？')
        if disposition == 'clarify':
            parsed = parse_reminder_time(time_text, event.occurred_at or self.clock(),
                day=slots.get('day', ''), clock=slots.get('clock', ''),
                period_required=bool(task and 'period' in task.missing_slots))
            if not time_text or parsed.instant() is not None:
                return self._personal_question(event, claim, task, slots,
                    task.missing_slots if task else ('clock',), '时间还不够明确，请告诉我具体日期、几点，以及上午还是晚上。')
        return self._personal_complete_time(event, claim, task, slots, time_text)


    def _personal_complete_time(self, event, claim, task, slots, time_text):
        action = slots.get('action', 'create')
        default_day, default_clock = slots.get('day', ''), slots.get('clock', '')
        if action == 'update' and not default_day:
            previous = datetime.fromisoformat(slots['scheduled_at_utc']).astimezone(BEIJING)
            default_day, default_clock = previous.date().isoformat(), previous.strftime('%H:%M')
        parsed = parse_reminder_time(time_text, event.occurred_at or self.clock(), day=default_day,
                                     clock=default_clock, period_required=bool(task and 'period' in task.missing_slots))
        slots.update(day=parsed.day, clock=parsed.clock)
        if parsed.error or not parsed.instant():
            missing = tuple(name for name, value in (('day', parsed.day), ('clock', parsed.clock)) if not value)
            if parsed.needs_period:
                missing = (*missing, 'period')
            question = parsed.error or (f"{parsed.day} 几点提醒你{slots['title']}？" if parsed.day else f"哪天几点提醒你{slots['title']}？")
            return self._personal_question(event, claim, task, slots, missing or ('clock',), question)
        slots['scheduled_at_utc'] = parsed.instant().astimezone(timezone.utc).isoformat()
        if action == 'create':
            slots['reminder_id'] = self.store.reminders.new_id(event.event_key)
        reply = f"已{'设置' if action == 'create' else '修改'}：{parsed.day} {parsed.clock}（北京时间）提醒你{slots['title']}，到时在本群 @ 你。"
        return self._personal_commit(event, claim, task, slots, reply, action)


    def _personal_question(self, event, claim, task, slots, missing, question):
        update = TaskUpdate('reminder', 'collecting', task.initial_request if task else event.content,
            slots, missing, task.task_id if task else '', task.version if task else None)
        return self.store.reminders.commit(event, claim, update, question, now=self.clock())


    def _personal_commit(self, event, claim, task, slots, reply, action):
        update = TaskUpdate('reminder', 'completed', task.initial_request if task else event.content,
            slots, task_id=task.task_id if task else '', expected_version=task.version if task else None)
        return self.store.reminders.commit(event, claim, update, reply, action=action, reminder=slots, now=self.clock())


    @skill_request
    def _extract_booking(self, text, *, require_date=True):
        entities = [name for name in self.resolver.sources if name in text]
        dates = list(dict.fromkeys(re.findall(DATE_PATTERN, text)))
        result = {'entity': entities[0] if len(entities) == 1 else '',
                  'visit_date_text': dates[0] if len(dates) == 1 else '', 'audience': 'personal'}
        if re.search(r'团队|团体|旅行社|学校|讲解|演出|夜游|特展', text):
            result['audience'] = 'unsupported'
        if result['entity'] and (result['visit_date_text'] or not require_date):
            return result
        if self.client is not None:
            with model_call_context('reminder_extract', prompt_version='booking-reminder-v1', stage_id='reminder.booking'):
                response = self.client.chat.completions.create(model=self.model,
                    response_format={'type': 'json_object'}, messages=[
                    {'role': 'system', 'content': (
                        skill_text('reminder.booking', 'system'))},
                    {'role': 'user', 'content': complete_json({'message':text},12000)},
                    ])
            value = response_json(response)
            if not isinstance(value, dict) or value.get('audience') not in {'personal', 'unsupported'}:
                raise ValueError('未能明确预约类型，请说明普通个人入馆的场馆和参观日期。')
            for key in ('entity', 'visit_date_text'):
                if not isinstance(value.get(key), str) or (value[key] and value[key] not in text):
                    raise ValueError('参观目标无法对应你的原话，请补充完整场馆名称和参观日期。')
            result = value
        elif not result['entity']:
            match = re.search(r'([\u4e00-\u9fff]{2,30}(?:博物馆|景区|美术馆|纪念馆))', text)
            if match:
                result['entity'] = re.sub(r'^.*?(?:参观|游览|去看|去)', '', match[1])
        return result


    def _booking_handle(self, event, claim, selection):
        text = event.content.strip()
        task = selection.task
        query_only = selection.operation == 'query'
        slots = dict(task.slots) if task else {}
        previous_visit_date = slots.get('visit_date')
        if task is None:
            slots['mode'] = 'query' if query_only else 'reminder'
            extracted = self._extract_booking(text, require_date=not query_only)
            if extracted['audience'] != 'personal':
                return '团队、演出、夜游或特展的规则不同，请提供对应的官方预约说明。当前没有创建提醒。'
            slots['entity'] = extracted['entity']
            slots['visit_date_text'] = extracted['visit_date_text']
        elif 'visit_date' in task.missing_slots and re.fullmatch(DATE_PATTERN + r'[。 ]*', text):
            slots['visit_date_text'] = text.rstrip('。 ')
            slots.pop('visit_date', None)
        elif 'entity' in task.missing_slots and not text.startswith('https://'):
            slots['entity'] = text
        urls = re.findall(r'https://[^\s<>，。]+', text)
        if urls:
            if len(urls) != 1:
                return '请一次提供一个适用于该场馆的预约说明链接。'
            slots['source_url'] = urls[0]
        if not slots.get('entity'):
            return self._booking_question(event, claim, task, slots, ('entity',), '你想参观哪一家场馆？请告诉我完整名称。')
        if slots.get('mode') == 'query':
            try:
                policy = self.resolver.resolve(slots['entity'], source_url=slots.get('source_url', ''))
            except PolicyUnavailable as exc:
                return self._booking_question(event, claim, task, slots, ('source_url',), str(exc))
            slots['policy'] = self.store.policies.record(policy)
            reply = (f'{policy.entity}：页面中的个人入馆规则为最多提前 {policy.days_before} 天，'
                     f'每日 {policy.release_time} 放票。\n来源：{policy.source_url}\n'
                     + '\n'.join(policy.caveats) + '\n本次只查询规则，没有创建提醒或提交预约。')
            return self.store.reminders.commit(event, claim, self._booking_update(task, event, slots, 'completed'), reply, now=self.clock())
        if not slots.get('visit_date_text'):
            return self._booking_question(event, claim, task, slots, ('visit_date',), f"计划哪天参观{slots['entity']}？")
        parsed = parse_reminder_time(slots.get('visit_date') or slots['visit_date_text'], event.occurred_at or self.clock())
        if parsed.error or not parsed.day:
            slots['visit_date_text'] = ''
            return self._booking_question(event, claim, task, slots, ('visit_date',), parsed.error or '请告诉我明确的参观年月日。')
        slots['visit_date'] = parsed.day
        previous_fingerprint = slots.get('policy', {}).get('fingerprint')
        try:
            policy = self.resolver.resolve(slots['entity'], source_url=slots.get('source_url', ''),
                                           visit_date=date.fromisoformat(parsed.day))
        except PolicyUnavailable as exc:
            return self._booking_question(event, claim, task, slots, ('source_url',),
                str(exc) + '\n目前没有创建开约提醒。也可以另行指定日期和时刻提醒你预约。')
        slots['policy'] = self.store.policies.record(policy)
        if not policy.official:
            return self._booking_question(event, claim, task, slots, ('source_url',),
                '页面官方身份尚未核实，不能按该规则自动计算开约提醒。请提供可核验官方说明，或另行指定个人提醒的日期和时刻。')
        applicability = policy.date_applicability or {}
        if applicability.get('date_status') in {'expired', 'not_yet_effective', 'outside_declared_window', 'conflicting_range'}:
            return self._booking_question(event, claim, task, slots, ('source_url',), '该规则不适用于目标日期或有效期冲突，尚未创建提醒；请补充适用公告。')
        if date.fromisoformat(parsed.day).weekday() in policy.closed_weekdays and not policy.holiday_exceptions:
            return self._booking_question(event, claim, task, slots, ('visit_date',),
                '按页面常规开放安排，这天是闭馆日。当前没有创建提醒，请选择其他参观日期或提供适用的新公告。')
        opening = policy.opening_at(date.fromisoformat(parsed.day))
        if opening <= self.clock():
            return self._booking_question(event, claim, task, slots, ('visit_date',),
                f'按该规则，预约开放时刻 {opening:%Y-%m-%d %H:%M} 已经过了。当前没有创建过去时间的提醒；请现在查看官方预约渠道，或指定其他提醒时间。')
        preview = (f"{slots['entity']}，计划参观 {parsed.day}。\n"
                   f"页面规则：提前 {policy.days_before} 天，每日 {policy.release_time} 放票。\n"
                   f"对应提醒时刻：{opening:%Y-%m-%d %H:%M}（北京时间）。\n"
                   f"来源：{policy.source_url}\n核查时间：{policy.retrieved_at}\n")
        confirmed = bool(task and 'confirmation' in task.missing_slots and selection.operation == 'confirm')
        if ((confirmed and (previous_fingerprint != policy.fingerprint or previous_visit_date != parsed.day))
                or (policy.caveats and not confirmed)):
            prefix = '规则内容发生变化，请重新核对。\n' if confirmed else ''
            return self._booking_question(event, claim, task, slots, ('confirmation',),
                prefix + preview + '\n'.join(policy.caveats)
                + '\n目前没有创建提醒。核对适用后可回复“按这个规则设置提醒”，或提供更明确的官方说明链接。')
        slots.update(title=f"预约{slots['entity']}（参观日期 {parsed.day}）",
                     scheduled_at_utc=opening.astimezone(timezone.utc).isoformat(),
                     reminder_id=self.store.reminders.new_id(event.event_key),
                     source={'kind': 'booking_policy', 'visit_date': parsed.day,
                             'policy_fingerprint': policy.fingerprint, 'policy': slots['policy'],
                             'applicability_confirmed_by_user': confirmed})
        update = self._booking_update(task, event, slots, 'completed')
        reply = '已设置开约提醒。\n' + preview + '到时在本群 @ 你；这只是提醒，不代表已经预约成功。'
        return self.store.reminders.commit(event, claim, update, reply,
            action='create', reminder=slots, now=self.clock())


    @staticmethod
    def _booking_update(task, event, slots, status, missing=()):
        return TaskUpdate('booking_policy_query' if slots.get('mode') == 'query' else 'booking_reminder',
            status, task.initial_request if task else event.content,
            slots, missing, task.task_id if task else '', task.version if task else None)


    def _booking_question(self, event, claim, task, slots, missing, reply):
        return self.store.reminders.commit(event, claim, self._booking_update(task, event, slots, 'collecting', missing),
                                            reply, now=self.clock())


    @skill_request
    def _extract_query(self, text, previous):
        if self.client is None:
            raise ValueError('定时查询的自然语言解析需要模型服务，请配置后重试。')
        with model_call_context('intent_parse', prompt_version='scheduled-query-v2', stage_id='reminder.scheduled_query'):
            response = self.client.chat.completions.create(model=self.model, response_format={'type': 'json_object'}, messages=[
                {'role': 'system', 'content': (skill_text('reminder.scheduled_query', 'system'))},
                {'role': 'user', 'content': json.dumps({'message': text, 'previous': previous}, ensure_ascii=False)},
            ])
        value = response_json(response)
        if not isinstance(value, dict) or value.get('kind') not in QUERY_TOOLS or not isinstance(value.get('arguments'), dict):
            raise ValueError('请明确要定时查询天气、天气预报还是路况。')
        allowed = QUERY_TOOLS[value['kind']][1]
        if value['arguments'].keys() - set(allowed):
            raise ValueError('定时查询不能包含其他动作或身份参数。')
        for key, item in value['arguments'].items():
            if not isinstance(item, str) or len(item) > 150 or (item and item not in text and item != previous.get('arguments', {}).get(key)):
                raise ValueError('查询对象无法对应你的原话，请明确地点或起终点。')
        time_text = value.get('time_text', '')
        if not isinstance(time_text, str) or (time_text and time_text not in text and time_text != previous.get('time_text')):
            raise ValueError('执行时间无法对应你的原话。')
        return {'kind': value['kind'], 'arguments': value['arguments'], 'time_text': time_text}


    def _query_handle(self, event, claim, selection):
        text = event.content.strip()
        if selection.operation in {'list', 'cancel'}:
            if selection.operation == 'list':
                page = int(text.split()[1]) if len(text.split()) > 1 else 1
                rows = self.store.scheduled_queries.list_for_owner(event, limit=10, offset=(page-1)*10)
                labels = {'weather': '当前天气', 'forecast': '天气预报', 'traffic': '路况'}
                states = {'active': '已安排', 'queued': '等待执行或投递', 'sent': '已发送', 'cancelled': '已取消',
                          'failed': '失败', 'blocked': '被阻止', 'missed': '已错过补发窗口'}
                listing = '\n'.join(f"{row['query_id']}：{labels[row['kind']]} {' → '.join(json.loads(row['arguments_json'])[key] for key in QUERY_TOOLS[row['kind']][1])}，"
                    f"{datetime.fromisoformat(row['due_at']).astimezone(BEIJING):%Y-%m-%d %H:%M:%S}（北京时间），{states[row['status']]}"
                    for row in rows) or '本页没有定时查询。'
                return listing + (f'\n第 {page} 页，可发送“查看定时查询 {page+1}”继续。' if len(rows) == 10 else '')
            cancelled = self.store.scheduled_queries.cancel(event, text.split()[-1])
            return '已取消这条定时查询。' if cancelled else '未找到可取消的查询，或结果已经进入投递阶段。'
        task = selection.task
        if event.platform not in {'onebot', 'web'} or event.channel != 'group':
            return '当前定时查询支持在 OneBot 群内设置和接收结果。'
        if re.search(r'每天|每周|每隔', text):
            return '请先指定一次查询的日期和时刻；本次没有建立重复查询。'
        previous = task.slots if task else {}
        if task and is_time_answer(text):
            value = {'kind': previous['kind'], 'arguments': dict(previous['arguments']), 'time_text': text}
        else:
            value = self._extract_query(text, previous)
        same_time = task and value['time_text'] == previous.get('time_text')
        parsed = parse_reminder_time('' if same_time else value['time_text'], event.occurred_at or self.clock(),
            day=previous.get('day', '') if task else '', clock=previous.get('clock', '') if task else '',
            period_required=bool(task and 'period' in task.missing_slots))
        value.update(day=parsed.day, clock=parsed.clock)
        missing = [key for key in QUERY_TOOLS[value['kind']][1] if not value['arguments'].get(key)]
        if not parsed.day: missing.append('day')
        if not parsed.clock: missing.append('clock')
        if parsed.needs_period: missing.append('period')
        if parsed.error or missing:
            question = parsed.error or ('请补充查询地点或起终点。' if set(missing) & {'location','origin','destination'} else '请补充具体查询日期和时刻。')
            update = TaskUpdate('scheduled_query', 'collecting', task.initial_request if task else event.content,
                value, tuple(missing), task.task_id if task else '', task.version if task else None)
            return self.store.reminders.commit(event, claim, update, question, now=self.clock())
        value['due_at'] = parsed.instant().astimezone(timezone.utc).isoformat()
        value['query_id'] = self.store.scheduled_queries.new_id(event.event_key)
        update = TaskUpdate('scheduled_query', 'completed', task.initial_request if task else event.content,
            value, task_id=task.task_id if task else '', expected_version=task.version if task else None)
        subject = ' → '.join(value['arguments'][key] for key in QUERY_TOOLS[value['kind']][1])
        label = {'weather': '当前天气', 'forecast': '天气预报', 'traffic': '实时路况'}[value['kind']]
        reply = f"已安排在 {parsed.day} {parsed.clock}（北京时间）实际查询{subject}的{label}，并在本群 @ 你发送结果。编号 {value['query_id']}。"
        if event.platform == 'web':
            reply = reply.replace('在本群 @ 你发送结果', '在当前网页会话中保存结果并通知你')
        return self.store.scheduled_queries.create(event, claim, update, value, reply, self.clock())


    async def _query_due(self, job, renderer):
        import asyncio
        from types import SimpleNamespace
        from core.chat_transport import storage_scope_id
        row = await asyncio.to_thread(self.store.scheduled_queries.for_job, job)
        if row is None:
            return {'status': 'cancelled'}
        if (self.clock()-datetime.fromisoformat(row['due_at'])).total_seconds() > 24*3600:
            await asyncio.to_thread(self.store.scheduled_queries.mark_missed, row)
            return {'status': 'expired'}
        claim = await asyncio.to_thread(self.store.delivery.begin_event, job['event_key'])
        if claim is None:
            return {'status': 'handled'}
        reply = claim.prepared_reply
        if reply is None:
            name, allowed = QUERY_TOOLS[row['kind']]
            arguments = json.loads(row['arguments_json'])
            if set(arguments) - set(allowed) or any(not isinstance(arguments.get(key), str) or not arguments[key] for key in allowed):
                raise ValueError('无效的定时查询参数')
            if self.tool_executor:
                result = await asyncio.to_thread(self.tool_executor, name, arguments,
                    AgentToolContext(row['platform'], row['group_id'], row['owner_id'], job['event_key']))
            else:
                result = await asyncio.to_thread(lambda: retry_read(
                    lambda: self.travel_service.execute_result(name, arguments),
                    max_attempts=1 if self.travel_service.manages_read_retries else 3))
            if result.status != 'completed':
                raise ReadFailed(result)
            planned = datetime.fromisoformat(row['due_at']).astimezone(BEIJING)
            reply = f'【定时查询结果】\n原定触发：{planned:%Y-%m-%d %H:%M:%S}（北京时间）；实际查询：{self.clock().astimezone(BEIJING):%Y-%m-%d %H:%M:%S}。\n' + result.data
            event = SimpleNamespace(event_key=job['event_key'], platform=row['platform'],
                storage_scope_id=storage_scope_id(row['platform'], row['group_id']), sender_id=row['owner_id'], content='[定时查询]')
            initial = {'weather': '当前天气', 'forecast': '天气预报', 'traffic': '路况'}[row['kind']]
            update = TaskUpdate(row['kind'], 'completed', ' → '.join(arguments[key] for key in allowed) + initial,
                {**arguments, 'intents': [row['kind']], 'source': 'scheduled_query', 'query_id': row['query_id'],
                 'reference_time': self.clock().isoformat()})
            await asyncio.to_thread(self.store.tasks.prepare_result, event, claim, update, reply)
        payload = renderer.render_reminder(row['owner_id'], reply)
        await asyncio.to_thread(self.store.delivery.prepare_event_outbox, job['event_key'], claim.claim_token, row['platform'],
            'group', row['group_id'], row['owner_id'], '', payload, '[定时查询]', assistant_text=reply)
        return {'status': 'handled'}


    def _watch_handle(self, event, claim, selection):
        text = event.content.strip()
        if selection.operation in {'list', 'cancel'}:
            if selection.operation == 'list':
                rows = self.store.policy_watches.list_for_owner(event)
                labels = {'active': '监测中', 'queued': '正在检查', 'cancelled': '已停止', 'completed': '已到截止时间', 'blocked': '被阻止', 'failed': '检查失败，请重新建立监测'}
                return '\n'.join(f"{row['watch_id']}：{row['entity']}，{labels[row['status']]}，截止 {datetime.fromisoformat(row['ends_at']).astimezone(BEIJING):%Y-%m-%d %H:%M}" for row in rows) or '当前没有规则监测。'
            stopped = self.store.policy_watches.stop(event, text.split()[-1])
            return '已停止后续监测；已经在途的通知可能已经发送。' if stopped else '未找到你仍在运行的这条监测。'
        task = selection.task
        if event.platform not in {'onebot', 'web'} or event.channel != 'group':
            return '规则监测目前支持 OneBot 群。'
        values = dict(task.slots) if task else {}
        if task is None:
            extracted = self._extract_booking(text, require_date=False)
            values['entity'] = extracted['entity']
            ending = re.search(r'(?:直到|截止到|截止|到)\s*(' + DATE_PATTERN + r'[^，。；,;\n]*)', text)
            values['end_text'] = ending[1] if ending else ''
            interval = re.search(r'每([一二两三四五六七八九十\d]+)(分钟|小时)', text)
            seconds = number(interval[1]) * (60 if interval[2] == '分钟' else 3600) if interval else 3600
            if not 900 <= seconds <= 86400:
                raise ValueError('监测间隔请设为 15 分钟到 24 小时；未指定时每小时检查一次。')
            values['interval_seconds'] = seconds
        elif 'ends_at' in task.missing_slots and is_time_answer(text):
            values['end_text'] = text.rstrip('。 ')
        urls = re.findall(r'https://[^\s，。<>]+', text)
        if len(urls) == 1:
            values['source_url'] = urls[0]
        if not values.get('entity'):
            return '请用完整场馆名称描述要监测的预约规则。'
        if values.get('end_needs_period') and not values.get('end_text'):
            return self._watch_question(event, claim, task, values, ('ends_at',), '截止时刻指上午还是下午/晚上？')
        parsed = parse_reminder_time(values.pop('end_text', ''), event.occurred_at or self.clock(),
            day=values.get('end_day', ''), clock=values.get('end_clock', ''),
            period_required=values.get('end_needs_period', False))
        values.update(end_day=parsed.day, end_clock=parsed.clock, end_needs_period=parsed.needs_period)
        if parsed.error or not parsed.day:
            return self._watch_question(event, claim, task, values, ('ends_at',), parsed.error or '监测到哪一天结束？请告诉我明确日期。')
        if not parsed.clock:
            parsed = parse_reminder_time('', event.occurred_at or self.clock(), day=parsed.day, clock='23:59:59')
            values['end_clock'] = parsed.clock
        try:
            policy = self.resolver.resolve(values['entity'], source_url=values.get('source_url', ''))
        except PolicyUnavailable as exc:
            return self._watch_question(event, claim, task, values, ('source_url',), str(exc) + '\n还没有建立监测。')
        values.update(watch_id=self.store.policy_watches.new_id(event.event_key), source_url=policy.source_url,
                      ends_at=parsed.instant().astimezone(timezone.utc).isoformat(), policy=self.store.policies.record(policy),
                      signature=policy_signature(policy.to_dict()))
        reply = (f"已建立规则监测 {values['watch_id']}：{values['entity']}，每 {values['interval_seconds']//60} 分钟检查，"
                 f"截止 {parsed.day} {parsed.clock}（北京时间）。仅变化、首次读取失败或恢复时在本群 @ 你。\n"
                 f"基线：提前 {policy.days_before} 天，{policy.release_time} 放票。来源：{policy.source_url}\n"
                 '监测不会自动修改已有提醒。' + ('该页面的官方身份尚未独立核实。' if not policy.official else '')
                 + ('\n适用范围：' + '；'.join(policy.caveats) if policy.caveats else ''))
        update = TaskUpdate('policy_watch', 'completed', task.initial_request if task else event.content, values,
                            task_id=task.task_id if task else '', expected_version=task.version if task else None)
        if event.platform == 'web':
            reply = reply.replace('在本群 @ 你', '在当前网页会话中通知你')
        return self.store.policy_watches.create(event, claim, update, values, reply, self.clock())


    def _watch_question(self, event, claim, task, values, missing, text):
        update = TaskUpdate('policy_watch', 'collecting', task.initial_request if task else event.content,
            values, missing, task.task_id if task else '', task.version if task else None)
        return self.store.reminders.commit(event, claim, update, text, now=self.clock())


    async def _watch_due(self, job, renderer):
        watch = await asyncio.to_thread(self.store.policy_watches.for_job, job)
        if not watch:
            return {'status': 'cancelled'}
        claim = await asyncio.to_thread(self.store.delivery.begin_event, job['event_key'], now=self.clock())
        if claim is None:
            return {'status': 'handled'}
        reply = claim.prepared_reply
        if reply is None:
            old = json.loads(watch['policy_json'])
            policy_data = old
            if watch['ends_at'] <= self.clock().isoformat():
                await asyncio.to_thread(self.store.policy_watches.finish_poll, job, claim, watch, watch['last_signature'], old, '', self.clock())
                return {'status': 'observed'}
            try:
                policy = await asyncio.to_thread(self.resolver.resolve, watch['entity'], source_url=watch['source_url'])
                policy_data = await asyncio.to_thread(self.store.policies.record, policy)
                signature = policy_signature(policy_data)
                if signature != watch['last_signature']:
                    prefix = '规则来源恢复读取' if watch['last_signature'].startswith('error:') else '预约规则有变化'
                    details = []
                    for key, label in (('closed_weekdays', '常规闭馆日'),
                                       ('holiday_exceptions', '存在节假日例外'), ('caveats', '适用范围说明')):
                        if json.dumps(old[key]) != json.dumps(policy_data[key]):
                            def display(value):
                                if key == 'holiday_exceptions':
                                    return '是' if value else '否'
                                if key == 'closed_weekdays':
                                    return '、'.join('周' + '一二三四五六日'[day] for day in value) or '未列明'
                                return '；'.join(value) or '无额外说明'
                            details.append(f'{label}：{display(old[key])} → {display(policy_data[key])}')
                    reply = (f"{prefix}：{watch['entity']}\n上次：提前 {old['days_before']} 天，{old['release_time']} 放票；"
                             f"现在：提前 {policy.days_before} 天，{policy.release_time} 放票。\n"
                             + ('\n'.join(details) + '\n' if details else '') +
                             f"核查时间：{policy.retrieved_at}\n来源：{policy.source_url}\n"
                             '请核对适用范围；已有提醒没有被自动修改，可用“刷新行程预约规则”查看关联提醒的调整预览。')
            except PolicyUnavailable as exc:
                signature = 'error:unavailable'
                if signature != watch['last_signature']:
                    reply = f"规则监测暂时无法明确读取：{watch['entity']}。{exc}\n来源：{watch['source_url']}"
            await asyncio.to_thread(self.store.policy_watches.finish_poll, job, claim, watch, signature, policy_data, reply or '', self.clock())
        if reply:
            payload = renderer.render_reminder(watch['owner_id'], reply)
            await asyncio.to_thread(self.store.delivery.prepare_event_outbox, job['event_key'], claim.claim_token, watch['platform'],
                'group', watch['group_id'], watch['owner_id'], '', payload, '[规则监测]', now=self.clock(), assistant_text=reply)
            return {'status': 'handled'}
        return {'status': 'observed'}



    def apply_intent(self, event, claim, ir, *, mode='execute'):
        linked = [item for item in ir.operations if item.domain == 'reminder' and item.operation == 'cancel_linked_reminder']
        if linked:
            entity = linked[0].values.get('entity', '')
            if not entity:
                return '请说明要取消哪一个地点的关联预约提醒。'
            trips = self.store.trips.list_for_owner(event)
            reminders = []
            for trip in trips:
                reminders.extend(self.store.trips.linked_reminders(event, trip['trip_id']))
            matches = [row for row in reminders if entity == json.loads(row['source_json']).get('entity')]
            if len(matches) != 1:
                return '没有找到唯一的关联预约提醒，请补充行程或场馆名称。' if not matches else '这个地点有多条关联预约提醒，请先说明参观日期。'
            if mode == 'preview':
                return f"已找到关联预约提醒 {matches[0]['reminder_id']}；当前仅生成预览，尚未取消。"
            return self.cancel(event, claim, matches[0]['reminder_id'])
        items = [item for item in ir.operations if item.domain == 'reminder' and item.operation == 'create']
        if not items:
            return None
        if len(items) != 1 or len(ir.operations) != 1:
            return '这次涉及多个操作，请分别说明每一项提醒的事项和时间，当前没有创建提醒。'
        if ir.action not in {'create', 'clarify'}:
            return '解析到的动作与创建提醒不一致，当前没有创建提醒。'
        values = items[0].values
        title = values.get('title', '')
        trigger = values.get('trigger') if isinstance(values.get('trigger'), dict) else {}
        time_text = trigger.get('text', '')
        source = trigger.get('source_text', time_text)
        if (not isinstance(title, str) or not isinstance(time_text, str)
                or len(time_text) > 120 or not isinstance(source, str)
                or (title and title not in event.content)
                or (time_text and (not source or source not in event.content))):
            return '提醒事项或时间无法对应你的原话，尚未创建提醒。请明确要提醒的事项和时间。'
        if source and is_time_answer(source):
            parsed_source = parse_reminder_time(source, event.occurred_at or datetime.now().astimezone())
            if parsed_source.needs_period:
                time_text = source
        if mode == 'preview':
            return f'已识别提醒：{time_text}提醒你{title}。当前仅生成预览，尚未创建。'
        return self.handle_intent(event, claim, title=title, time_text=time_text,
            disposition='clarify' if ir.action == 'clarify' or ir.requires_confirmation or ir.missing_fields else 'answer')



    def continue_intent(self, event, claim, ir, tasks, *, mode='execute'):
        if not tasks:
            return '没有可接续的提醒设置，可能已经完成或过期。请重新说明事项和时间。'
        if len(ir.operations) != 1 or not ir.source_spans:
            return '这次补充涉及的操作还不明确，尚未修改提醒设置。请分别说明要补充或取消的内容。'
        values = ir.operations[0].values
        index = values.get('task_index')
        if type(index) is not int or not 1 <= index <= len(tasks):
            return '请说明要继续哪一项提醒设置：' + '；'.join(task.slots.get('title') or task.initial_request for task in tasks)
        task = tasks[index-1]
        if len(tasks) > 1:
            reference = values.get('task_reference', '')
            if not isinstance(reference, str):
                return '请明确说出要继续设置的提醒事项。'
            matches = [item for item in tasks if reference and reference in item.slots.get('title', '')]
            if reference not in event.content or len(matches) != 1 or matches[0].task_id != task.task_id:
                return '有多项提醒正在设置，请明确说出要继续的事项，或回复对应任务后再补充。'
        disposition = values.get('disposition')
        if disposition not in {'answer', 'clarify', 'cancel'}:
            return '尚未理解这次补充，没有修改提醒。请明确时间、事项或取消设置。'
        if ir.action not in {'create', 'update', 'clarify', 'cancel'} or ((disposition == 'cancel') != (ir.action == 'cancel')):
            return '解析到的动作不一致，尚未修改提醒设置。'
        if (ir.action == 'clarify' or ir.missing_fields or ir.requires_confirmation) and disposition != 'cancel':
            disposition = 'clarify'
        title = values.get('title', '')
        trigger = values.get('trigger', {})
        if not isinstance(title, str) or (title and title not in event.content) or not isinstance(trigger, dict):
            return '补充内容无法对应你的原话，没有修改提醒设置。'
        time_text, source = trigger.get('text', ''), trigger.get('source_text', '')
        if not isinstance(time_text, str) or len(time_text) > 120 or not isinstance(source, str):
            return '提醒时间解析无效，请重新说明。'
        if time_text and (not source or source not in event.content):
            return '提醒时间缺少你本轮的明确依据，请重新说明。'
        # A recognizably ambiguous clock must not be silently upgraded to AM/PM by the model.
        if source and is_time_answer(source):
            parsed_source = parse_reminder_time(source, event.occurred_at or datetime.now().astimezone(),
                day=task.slots.get('day', ''), clock=task.slots.get('clock', ''),
                period_required='period' in task.missing_slots)
            if parsed_source.needs_period:
                time_text = source
        if mode == 'preview':
            return '已理解你对提醒设置的补充；当前仅预览，尚未保存或创建提醒。'
        return self.handle_intent(event, claim, task=task, title=title,
            time_text=time_text, disposition=disposition)

