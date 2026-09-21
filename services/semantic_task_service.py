"""Resolve validated Intent IR into existing deterministic domain services."""
from dataclasses import replace
from datetime import datetime
import json
import logging
import re

from agents.intent_compiler import IntentCompiler
from core.tasks import TaskUpdate
from core.intent_ir import IntentIR, IntentOperation, to_jsonable
from core.commands import parse_command
from core.trip_updates import apply_constraints
from services.preference_service import PreferenceService, preference_request
from services.reminder_time import is_time_answer, parse_reminder_time

logger = logging.getLogger(__name__)


def semantic_candidate(text: str) -> bool:
    """Only explicit commands bypass the compiler; natural language has no keyword gate."""
    stripped = text.strip()
    if stripped in {'我的偏好','查看我的偏好','查看偏好','保存偏好建议','忘记所有偏好','暂停应用偏好','启用偏好','关闭偏好建议','开启偏好建议'}:
        return False
    if stripped in {'帮助', '菜单', '状态', '查看行程', '查看我的行程', '我的行程', '确认行程修改', '确认行程提醒', '查看我的提醒', '查看任务进度', '查看任务', '查看定时查询', '查看规则监测'}:
        return False
    if re.fullmatch(r'(?:取消提醒|修改提醒)\s+M-[a-f0-9]{12}', stripped):
        return False
    return bool(stripped) and parse_command(stripped).name == 'unknown'


class SemanticTaskService:
    def __init__(self, store, compiler: IntentCompiler, trip_service=None,
                 reminder_service=None, mode='execute', clock=None):
        self.store, self.compiler = store, compiler
        self.trip_service, self.reminder_service = trip_service, reminder_service
        self.mode = mode if mode in {'shadow', 'preview', 'execute'} else 'shadow'
        self.clock = clock

    def shadow(self, event):
        if not semantic_candidate(event.content):
            return None
        ir = self.compiler.try_compile(event.content)
        if ir is not None:
            logger.info('Semantic shadow IR: event=%s action=%s operations=%s', event.event_key, ir.action,
                        [(item.domain, item.operation) for item in ir.operations])
        return ir

    def handle(self, event, claim):
        pending = self._pending(event)
        reminders = self._pending_reminders(event)
        if not semantic_candidate(event.content):
            return None
        compile_text = event.content
        if pending is not None:
            compile_text = pending.slots.get('source_text', pending.initial_request) + '\n用户补充：' + event.content
        trips, trip_context = self._trip_context(event)
        context = json.loads(self._reminder_context(reminders, event)) if reminders else {}
        context.update(trip_context)
        if context:
            ir = self.compiler.try_compile(event.content if reminders else compile_text,
                                           context=json.dumps(context, ensure_ascii=False))
        else:
            ir = self.compiler.try_compile(compile_text)
        if self.mode == 'shadow' and reminders:
            return None
        if ir is None and reminders and not is_time_answer(event.content):
            return '这次未能可靠解析你的补充，提醒尚未完成设置。请再说明要补充的时间、事项，或明确结束设置。'
        if ir is not None and ir.action == 'clarify' and not ir.operations and reminders:
            return '提醒设置仍在等待补充。请说明具体时间或要继续的事项，不明确的时间不会自动创建提醒。'
        if ir is not None and any(item.domain == 'reminder' and item.operation == 'continue_pending' for item in ir.operations):
            if self.mode == 'shadow':
                return None
            return self._continue_reminder(event, claim, ir, reminders)
        if ir is not None and any(item.domain == 'trip' and item.operation == 'update_constraints' for item in ir.operations):
            if self.mode == 'shadow':
                return None
            return self._update_trip(event, claim, ir, trips, trip_context, pending, compile_text)
        if ir is None and trips and self.mode != 'shadow':
            return '这次未能可靠识别你的意图，原行程和偏好未改变。请再说明要调整行程还是查询独立路线。'
        recovered = self._recover_ir(event, pending)
        if ir is None or not self._supported(ir):
            ir = recovered
        if ir is None:
            return None
        logger.info('Semantic IR accepted: event=%s mode=%s source=%s action=%s operations=%s',
                    event.event_key, self.mode, 'recovery' if ir is recovered else 'model', ir.action,
                    [(item.domain, item.operation) for item in ir.operations])
        if self.mode == 'shadow':
            logger.info('Semantic shadow mode ignored actionable IR: event=%s action=%s operations=%s',
                        event.event_key, ir.action, [(item.domain, item.operation) for item in ir.operations])
            return None
        if ir.action == 'clarify':
            if len(ir.operations) == 1 and ir.operations[0].domain == 'reminder' and ir.operations[0].operation == 'create':
                return self._reminder(event, claim, ir)
            question = '请补充这次操作涉及的地点、日期或事项。'
            if ir.missing_fields and any('地点' in field or 'name' in field for field in ir.missing_fields):
                question = '请说明要从行程中删除的具体地点。'
            return self._save_clarification(event, claim, ir, question, task=pending,
                                            source_text=compile_text)
        if ir.action == 'none':
            return None
        if ir.action == 'confirm':
            return self._confirm(event, claim, ir)
        if any(item.domain == 'trip' for item in ir.operations):
            return self._trip(event, claim, ir)
        if any(item.domain == 'reminder' for item in ir.operations):
            return self._reminder(event, claim, ir)
        return None

    def _trip_context(self, event):
        repo = getattr(self.store, 'trips', None)
        trips = tuple(repo.list_for_owner(event)) if repo else ()
        if not trips:
            return (), {}
        candidates = []
        for index, trip in enumerate(trips, 1):
            spec = trip.get('spec', {})
            candidates.append({'index': index, 'title': trip.get('title', ''),
                'city': spec.get('destination', ''), 'start_date': spec.get('start_date', ''),
                'end_date': spec.get('end_date', ''), 'days': spec.get('day_count'), 'version': trip.get('version')})
        selected = trips[0] if len(trips) == 1 else None
        ids = re.findall(r'TR-[a-f0-9]{12}', event.content)
        if ids:
            selected = next((t for t in trips if len(set(ids)) == 1 and t['trip_id'] == ids[0]), None)
        elif event.reply_to_id and hasattr(repo, 'referenced'):
            referenced = repo.referenced(event)
            selected = next((t for t in trips if referenced and t['trip_id'] == referenced['trip_id']), selected)
        context = {'trips': candidates}
        if selected:
            context['current_trip_index'] = next(i for i,t in enumerate(trips,1) if t['trip_id'] == selected['trip_id'])
            spec = selected.get('spec', {})
            context['current_trip'] = {k: spec[k] for k in ('destination','start_date','end_date','day_count',
                'gentle','pace','transport_text','transport_preferences','must_visit','preferences','constraint_overrides') if k in spec}
            context['current_trip']['activities'] = [
                {'day': d.get('day_index'), 'places': [selected.get('sources',{}).get(a['poi_id'],{}).get('name','')
                    for a in d.get('activities',[])]} for d in selected.get('plan',{}).get('days',[])]
            context['current_trip']['research_summary'] = spec.get('source_context','')[:800]
        preferences = getattr(self.store, 'preferences', None)
        if preferences:
            context['long_term_defaults'] = preferences.defaults(event)['values']
        return trips, context

    def _update_trip(self, event, claim, ir, trips, context, pending, source_text):
        if self.trip_service is None:
            return '行程服务暂不可用，未修改行程。'
        if len(ir.operations) != 1 or ir.action not in {'update','clarify'} or not ir.source_spans:
            return '行程调整的动作或依据不明确，未修改行程。请分别说明其他操作。'
        values = ir.operations[0].values
        if values.keys() - {'target_ref','trip_index','trip_reference','changes','save_preference_text'}:
            return '行程调整包含不支持的字段，未修改行程。'
        if values.get('target_ref', 'current_trip') != 'current_trip':
            return '行程目标引用无效，未修改行程。请说明要调整哪份已保存行程。'
        try:
            apply_constraints({}, values.get('changes'), source_text)
        except ValueError as exc:
            logger.info('Trip constraint rejected: event=%s reason=invalid_patch',event.event_key)
            return str(exc)
        ids = re.findall(r'TR-[a-f0-9]{12}', event.content)
        if ids and (len(set(ids)) != 1 or not any(t['trip_id'] == ids[0] for t in trips)):
            logger.info('Trip constraint rejected: event=%s reason=invalid_target',event.event_key)
            return '没有找到当前会话中属于你的这份有效行程。'
        if not trips:
            return '当前会话还没有已保存行程，请先规划行程；本轮未创建交通查询。'
        index = context.get('current_trip_index')
        requested = values.get('trip_index')
        reference = values.get('trip_reference', '')
        if requested is not None and (index is None or requested != index):
            matches = [i for i,t in enumerate(trips,1) if reference and reference in (
                t['trip_id'], t.get('title',''), t.get('spec',{}).get('destination',''))]
            if (type(requested) is int and isinstance(reference,str) and reference in event.content
                    and matches == [requested]):
                index = requested
            else:
                index = None
        if index is None:
            logger.info('Trip constraint clarification: event=%s reason=ambiguous_target',event.event_key)
            question = '要调整哪一份行程？请回复行程编号或唯一的城市/标题：\n' + '\n'.join(
                f"{t.get('title','行程')}（{t['trip_id']}）" for t in trips)
            return self._save_clarification(event, claim, ir, question, task=pending, source_text=source_text)
        if ir.action == 'clarify' and any(f != 'trip_ref' for f in ir.missing_fields):
            return '已找到原行程，请明确希望怎样调整节奏、交通或活动；无需重复城市和日期。'
        trip = trips[index-1]
        preference_update = None
        save_text = values.get('save_preference_text')
        if save_text is not None:
            if (not isinstance(save_text,str) or not save_text or save_text not in source_text
                    or not preference_request(save_text) or not re.search(r'记住|保存|更新偏好|以后.*(?:默认|优先)',save_text)
                    or re.search(r'不要记住|不要保存|别记住|不用记住|不保存|举例|例如|引用',source_text)):
                return '长期保存缺少明确依据，尚未修改行程或偏好。'
            profile_values = PreferenceService.extract(save_text)
            if not profile_values:
                return '长期偏好暂不支持这组表达，尚未修改行程或偏好。请将本次行程调整与长期偏好分别说明。'
            preference_update = {'values':profile_values,'version':self.store.preferences.snapshot(event)['version']}
        if self.mode == 'preview':
            return '已识别对原行程的约束调整；当前仅解析预览，尚未保存行程或偏好。'
        logger.info('Trip constraint dispatch: event=%s trip=%s version=%s fields=%s',
                    event.event_key,trip['trip_id'],trip['version'],sorted(values['changes']))
        return self.trip_service.update_constraints(event, claim, trip['trip_id'], trip['version'], values['changes'],
            source_text=source_text, preference_update=preference_update, semantic_task=pending)

    @staticmethod
    def _supported(ir):
        supported = {
            ('trip', 'remove_activity'), ('trip', 'view_current_trip'),
            ('trip', 'confirm_pending_operation'), ('reminder', 'create'),
            ('reminder', 'cancel_linked_reminder'),
        }
        return any((item.domain, item.operation) in supported for item in ir.operations)

    def _pending_reminders(self, event):
        if not hasattr(self.store, 'tasks'):
            return ()
        return tuple(task for task in self.store.tasks.recent(
            event.platform, event.storage_scope_id, event.sender_id, reply_to_id=event.reply_to_id)
            if task.task_type == 'reminder' and task.status == 'collecting')

    def _reminder_context(self, tasks, event):
        pending = []
        for index, task in enumerate(tasks, 1):
            question = ''
            with self.store._connect() as connection:
                row = connection.execute('SELECT prepared_reply FROM processed_events WHERE event_id=?',
                                         (task.last_event_id,)).fetchone()
                question = (row['prepared_reply'] or '')[:400] if row else ''
            pending.append({'index': index, 'initial_request': task.initial_request[:240],
                            'slots': {key: task.slots.get(key, '') for key in ('action', 'title', 'day', 'clock')},
                            'missing_fields': list(task.missing_slots), 'last_question': question})
        return json.dumps({'pending_reminders': pending, 'timezone': 'Asia/Shanghai', 'current_time': (event.occurred_at or
                          (self.clock() if self.clock else datetime.now().astimezone())).isoformat()}, ensure_ascii=False)

    def _continue_reminder(self, event, claim, ir, tasks):
        if self.reminder_service is None or not tasks:
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
        if self.mode == 'preview':
            return '已理解你对提醒设置的补充；当前仅预览，尚未保存或创建提醒。'
        return self.reminder_service.handle_intent(event, claim, task=task, title=title,
            time_text=time_text, disposition=disposition)

    def _recover_ir(self, event, pending):
        text = event.content
        tasks_repo = getattr(self.store, 'tasks', None)
        if re.search(r'确认|同意|执行|保存|变更', text) and tasks_repo is not None:
            tasks = tasks_repo.recent(event.platform, event.storage_scope_id, event.sender_id,
                                      reply_to_id=event.reply_to_id)
            if any(task.task_type == 'itinerary' and task.status == 'collecting'
                   and 'confirmation' in task.missing_slots for task in tasks):
                return IntentIR(1, 'confirm', (IntentOperation('trip', 'confirm_pending_operation', {}),),
                                source_spans=())
        if not re.search(r'不去|删除|取消.{0,12}(?:行程|日程)|不安排', text):
            return None
        trips = self.store.trips.list_for_owner(event)
        if len(trips) != 1:
            return None
        names = list(dict.fromkeys(place['name'] for place in trips[0]['sources'].values()
                                   if place.get('name') and place['name'] in text))
        if len(names) != 1:
            return None
        date_match = re.search(r'(?:(?:20\d{2}年)?\d{1,2}月\d{1,2}[日号]?|20\d{2}-\d{1,2}-\d{1,2}|大后天|后天|明天|今天)', text)
        activity = {'name': names[0]}
        if date_match:
            activity['date_text'] = date_match[0]
        operations = [IntentOperation('trip', 'remove_activity', {'activity': activity})]
        if re.search(r'预约提醒|关联提醒', text):
            operations.append(IntentOperation('reminder', 'cancel_linked_reminder', {'entity': names[0]}))
        spans = tuple(item for item in (date_match[0] if date_match else '', names[0]) if item)
        return IntentIR(1, 'update', tuple(operations), requires_confirmation=True, source_spans=spans)

    def _pending(self, event):
        tasks_repo = getattr(self.store, 'tasks', None)
        if tasks_repo is None:
            return None
        tasks = tasks_repo.recent(event.platform, event.storage_scope_id, event.sender_id,
                                  reply_to_id=event.reply_to_id)
        pending = [task for task in tasks if task.task_type == 'semantic' and task.status == 'collecting']
        return pending[0] if len(pending) == 1 else None

    def _save_clarification(self, event, claim, ir, question, *, source_text=None, task=None):
        if not hasattr(self.store, 'tasks'):
            return question
        slots = {'semantic_ir': to_jsonable(ir), 'source_text': source_text or event.content}
        update = TaskUpdate('semantic', 'collecting', task.initial_request if task else event.content,
                            slots, tuple(ir.missing_fields or ('semantic_input',)),
                            task.task_id if task else '', task.version if task else None)
        self.store.tasks.prepare_result(event, claim, update, question)
        return question

    def _current_trip(self, event):
        trips = self.store.trips.list_for_owner(event)
        return (trips[0], trips) if len(trips) == 1 else (None, trips)

    def _trip(self, event, claim, ir):
        trip, trips = self._current_trip(event)
        if any(item.operation == 'view_current_trip' for item in ir.operations):
            if self.trip_service is None:
                return None
            return self.trip_service.handle(replace(event, content='我的行程'), claim)
        if trip is None:
            if not trips:
                return '还没有找到你在本群创建的行程，请先规划或查看行程。'
            return '你有多份行程，请先使用“查看行程”并说明要修改哪一份。'
        remove_ops = [item for item in ir.operations if item.domain == 'trip' and item.operation == 'remove_activity']
        if not remove_ops or self.trip_service is None:
            return None
        values = remove_ops[0].values
        activity = values.get('activity') if isinstance(values.get('activity'), dict) else {}
        name = activity.get('name') or values.get('entity') or values.get('activity_name') or values.get('attraction_name') or ''
        date_text = activity.get('date_text') or values.get('date_text') or values.get('date') or values.get('visit_date') or ''
        if not name:
            source_names = [place['name'] for place in trip['sources'].values()
                            if place.get('name') and place['name'] in event.content]
            if len(set(source_names)) == 1:
                name = source_names[0]
        if not name:
            return self._save_clarification(event, claim, ir, '请说明要从行程中删除的具体地点。',
                                            source_text=event.content)
        target_date = ''
        if date_text:
            now = event.occurred_at or (self.clock() if self.clock else datetime.now().astimezone())
            parsed = parse_reminder_time(date_text, now)
            if parsed.error or not parsed.day:
                return parsed.error or '请补充明确的行程日期。'
            target_date = parsed.day
        matches = []
        for day in trip['plan']['days']:
            if target_date and day.get('date') != target_date:
                continue
            for activity_value in day['activities']:
                place = trip['sources'][activity_value['poi_id']]
                if place['name'] == name or name in place['name']:
                    matches.append(day['day_index'])
        if len(matches) != 1:
            return '行程中没有唯一匹配的地点和日期，请补充完整地点名称或日期。'
        return self.trip_service.handle(replace(event, content=f'第{matches[0]}天不去{name}'), claim,
                                        force_confirmation=True)

    def _confirm(self, event, claim, ir):
        if self.mode == 'preview':
            return '已识别到待确认的行程变更；当前处于预览模式，尚未执行。'
        if self.trip_service and any(item.domain == 'trip' and item.operation == 'confirm_pending_operation'
                                     for item in ir.operations):
            return self.trip_service.handle(replace(event, content='确认行程修改'), claim)
        return None

    def _reminder(self, event, claim, ir):
        if self.reminder_service is None:
            return None
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
            if self.mode == 'preview':
                return f"已找到关联预约提醒 {matches[0]['reminder_id']}；当前仅生成预览，尚未取消。"
            return self.reminder_service.handle(replace(event, content=f"取消提醒 {matches[0]['reminder_id']}"), claim)
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
        if self.mode == 'preview':
            return f'已识别提醒：{time_text}提醒你{title}。当前仅生成预览，尚未创建。'
        return self.reminder_service.handle_intent(event, claim, title=title, time_text=time_text,
            disposition='clarify' if ir.action == 'clarify' or ir.requires_confirmation or ir.missing_fields else 'answer')
