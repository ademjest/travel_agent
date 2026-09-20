from datetime import datetime, timezone
import re

from core.tasks import TaskUpdate, utc_now
from services.reminder_time import BEIJING, is_time_answer, parse_reminder_time


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


class PersonalReminderService:
    def __init__(self, store, clock=None, intent_parser=None):
        self.store = store
        self.clock = clock or utc_now
        self.intent_parser = intent_parser

    @staticmethod
    def _display(row):
        instant = datetime.fromisoformat(row['scheduled_at_utc']).astimezone(BEIJING)
        status = '已取消' if row['status'] == 'cancelled' else {
            'pending': '待提醒', 'queued': '等待投递', 'sent': '已发送', 'failed': '投递失败',
            'blocked': '群已不在允许范围', 'missed': '已错过补发窗口', 'cancelled': '已取消',
        }.get(row.get('delivery_status', 'pending'), '待提醒')
        return f"{row['title']} — {instant:%Y-%m-%d %H:%M}（北京时间，{status}，{row['reminder_id']}）"

    def handle(self, event, claim):
        text = event.content.strip()
        tasks = self.store.tasks.recent(event.platform, event.storage_scope_id, event.sender_id,
                                       reply_to_id=event.reply_to_id)
        continuation = reminder_continuation(text, tasks)
        if continuation is None and is_time_answer(text) and len([
                task for task in tasks if task.task_type == 'reminder' and task.status == 'collecting']) > 1:
            return '你有多条正在设置的提醒，请回复其中一条设置问题，再补充时间。'
        if not reminder_request(text) and continuation is None:
            return None
        if event.channel != 'group':
            return '当前支持在群内创建提醒，到期在原群 @ 创建者。'
        if re.search(r'不要(?:创建|设置)|别(?:创建|设置)|只是问|先别|(?:不要|不用|不必|别).{0,12}(?:提醒我|通知我|叫我)', text):
            return '没有创建提醒。需要时告诉我具体事项和时间。'
        if len(re.findall(r'提醒我|叫我|通知我', text)) > 1:
            return '这条消息包含多个提醒，请先分别发送每个事项和时间，避免把不同时间合并。'
        trigger_phrase = re.split(r'提醒我|叫我|通知我', text, maxsplit=1)[0]
        if re.search(r'每天|每周|每月|工作日|每隔', trigger_phrase):
            return '这类重复提醒还在升级中。现在可以指定一次提醒的日期和时刻。'
        if re.search(r'开售|开约|放票|可以预约|能预约', trigger_phrase):
            return ('这是按预约或开售规则推导时间的任务。我需要先核实适用的官方规则，'
                    '目前没有创建开约提醒。你可以提供官方说明，或先告诉我一个明确时间提醒你预约。')
        if re.search(r'到时.{0,6}(?:查询|查一下)|届时.{0,6}(?:查询|查一下)', trigger_phrase):
            return '届时自动执行查询属于定时查询任务，正在升级中。现在可以设置一条提醒你查询的通知。'
        if re.search(r'(?:查看|看看|列出|有哪些|我的).{0,12}提醒', text) and not re.search(r'修改|取消|改成|改为', text):
            rows = self.store.reminders.list_for_owner(event.platform, event.scope_id, event.sender_id)
            return '你还没有通用提醒。可以说“明天上午十点提醒我抢高铁票”。' if not rows else '\n'.join(self._display(row) for row in rows)
        task = continuation
        if task and text in {'取消这项任务', '先不设置了', '不设了'}:
            update = TaskUpdate('reminder', 'cancelled', task.initial_request, task.slots,
                                task_id=task.task_id, expected_version=task.version)
            return self.store.reminders.commit(event, claim, update, '已结束这项提醒设置任务，没有新建提醒。')
        slots = dict(task.slots) if task else {}
        title_answer = bool(task and 'title' in task.missing_slots)
        if title_answer:
            slots['title'] = text.strip(' ，,。！!')
        action = slots.get('action', 'create')
        if re.search(r'取消|不用提醒|不必提醒|删掉', text):
            action = 'cancel'
        elif re.search(r'改成|改为|修改|推迟|提前', text) and not task:
            action = 'update'
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
                return self._save_question(event, claim, None, slots, ('reference',),
                    '要操作哪一条？回复序号或事项：\n' + '\n'.join(f'{index}. {self._display(row)}' for index, row in enumerate(rows, 1)))
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
                    return self._save_question(event, claim, task, slots, ('title',), '要提醒你做什么事？')
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
                return self._save_question(event, claim, task, slots, ('title',), '要提醒你做什么事？')
            if not slots['title'] or len(slots['title']) > 200:
                return '请把提醒事项写在 1 到 200 个字符内。'
        if action == 'cancel':
            reply = f"已取消提醒：{slots['title']}。"
            return self._commit(event, claim, task, slots, reply, action)
        # For create, only the phrase preceding 提醒我 is a trigger time. Dates in the title are not schedules.
        time_text = text if task or action != 'create' else re.split(r'提醒我|叫我|通知我', text, maxsplit=1)[0]
        if semantic_time is not None:
            time_text = semantic_time
        if title_answer:
            time_text = ''
        return self._complete_time(event, claim, task, slots, time_text)

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
            return self.handle(event, claim) or '请先明确要操作哪条已有提醒，再补充时间。'
        if slots.get('action') not in {'create', 'update'}:
            return self.handle(event, claim)
        if title:
            if len(title) > 200 or title not in event.content:
                return '提醒事项无法对应你的原话，请明确提醒事项。'
            slots['title'] = title
        if not slots.get('title'):
            parsed = parse_reminder_time(time_text, event.occurred_at or self.clock(),
                                         day=slots.get('day', ''), clock=slots.get('clock', ''))
            slots.update(day=parsed.day, clock=parsed.clock)
            missing = ('title', 'period') if parsed.needs_period else ('title',)
            return self._save_question(event, claim, task, slots, missing, '要提醒你做什么事？')
        if disposition == 'clarify':
            parsed = parse_reminder_time(time_text, event.occurred_at or self.clock(),
                day=slots.get('day', ''), clock=slots.get('clock', ''),
                period_required=bool(task and 'period' in task.missing_slots))
            if not time_text or parsed.instant() is not None:
                return self._save_question(event, claim, task, slots,
                    task.missing_slots if task else ('clock',), '时间还不够明确，请告诉我具体日期、几点，以及上午还是晚上。')
        return self._complete_time(event, claim, task, slots, time_text)

    def _complete_time(self, event, claim, task, slots, time_text):
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
            return self._save_question(event, claim, task, slots, missing or ('clock',), question)
        slots['scheduled_at_utc'] = parsed.instant().astimezone(timezone.utc).isoformat()
        if action == 'create':
            slots['reminder_id'] = self.store.reminders.new_id(event.event_key)
        reply = f"已{'设置' if action == 'create' else '修改'}：{parsed.day} {parsed.clock}（北京时间）提醒你{slots['title']}，到时在本群 @ 你。"
        return self._commit(event, claim, task, slots, reply, action)

    def _save_question(self, event, claim, task, slots, missing, question):
        update = TaskUpdate('reminder', 'collecting', task.initial_request if task else event.content,
            slots, missing, task.task_id if task else '', task.version if task else None)
        return self.store.reminders.commit(event, claim, update, question, now=self.clock())

    def _commit(self, event, claim, task, slots, reply, action):
        update = TaskUpdate('reminder', 'completed', task.initial_request if task else event.content,
            slots, task_id=task.task_id if task else '', expected_version=task.version if task else None)
        return self.store.reminders.commit(event, claim, update, reply, action=action, reminder=slots, now=self.clock())
