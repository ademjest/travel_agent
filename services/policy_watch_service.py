import asyncio
from datetime import datetime, timezone
import hashlib
import json
import re

from core.tasks import TaskUpdate, utc_now
from services.booking_policy import PolicyUnavailable
from services.booking_reminder_service import BookingReminderService, DATE_PATTERN
from services.reminder_time import BEIJING, number, parse_reminder_time, is_time_answer


def watch_request(text):
    return bool(re.match(r'^(?:帮我|请|我想让你)?(?:持续)?(?:监测|监控|关注)', text.strip())
                and re.search(r'预约规则|放票规则|开约规则', text))


def watch_control(text):
    return text.strip() == '查看规则监测' or bool(re.fullmatch(r'停止规则监测\s+W-[a-f0-9]{12}', text.strip()))


def watch_continuation(text, tasks):
    pending = [task for task in tasks if task.task_type == 'policy_watch' and task.status == 'collecting']
    if len(pending) != 1:
        return None
    task = pending[0]
    if ('ends_at' in task.missing_slots and is_time_answer(text)) or text.startswith('https://'):
        return task
    return None


POLICY_FIELDS = ('days_before', 'release_time', 'closed_weekdays', 'holiday_exceptions', 'caveats')


def policy_signature(policy):
    meaningful = {key: policy[key] for key in POLICY_FIELDS}
    return 'ok:' + hashlib.sha256(json.dumps(meaningful, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class PolicyWatchService:
    def __init__(self, store, resolver, client=None, model='', clock=None):
        self.store, self.resolver = store, resolver
        self.extractor = BookingReminderService(store, resolver=resolver, client=client, model=model)
        self.clock = clock or utc_now

    def handle(self, event, claim):
        text = event.content.strip()
        if watch_control(text):
            if text == '查看规则监测':
                rows = self.store.policy_watches.list_for_owner(event)
                labels = {'active': '监测中', 'queued': '正在检查', 'cancelled': '已停止', 'completed': '已到截止时间', 'blocked': '被阻止', 'failed': '检查失败，请重新建立监测'}
                return '\n'.join(f"{row['watch_id']}：{row['entity']}，{labels[row['status']]}，截止 {datetime.fromisoformat(row['ends_at']).astimezone(BEIJING):%Y-%m-%d %H:%M}" for row in rows) or '当前没有规则监测。'
            stopped = self.store.policy_watches.stop(event, text.split()[-1])
            return '已停止后续监测；已经在途的通知可能已经发送。' if stopped else '未找到你仍在运行的这条监测。'
        tasks = self.store.tasks.recent(event.platform, event.storage_scope_id, event.sender_id, reply_to_id=event.reply_to_id)
        task = watch_continuation(text, tasks)
        if not watch_request(text) and task is None:
            return None
        if event.platform not in {'onebot', 'web'} or event.channel != 'group':
            return '规则监测目前支持 OneBot 群。'
        values = dict(task.slots) if task else {}
        if task is None:
            extracted = self.extractor._extract(text, require_date=False)
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
            return self._question(event, claim, task, values, ('ends_at',), '截止时刻指上午还是下午/晚上？')
        parsed = parse_reminder_time(values.pop('end_text', ''), event.occurred_at or self.clock(),
            day=values.get('end_day', ''), clock=values.get('end_clock', ''),
            period_required=values.get('end_needs_period', False))
        values.update(end_day=parsed.day, end_clock=parsed.clock, end_needs_period=parsed.needs_period)
        if parsed.error or not parsed.day:
            return self._question(event, claim, task, values, ('ends_at',), parsed.error or '监测到哪一天结束？请告诉我明确日期。')
        if not parsed.clock:
            parsed = parse_reminder_time('', event.occurred_at or self.clock(), day=parsed.day, clock='23:59:59')
            values['end_clock'] = parsed.clock
        try:
            policy = self.resolver.resolve(values['entity'], source_url=values.get('source_url', ''))
        except PolicyUnavailable as exc:
            return self._question(event, claim, task, values, ('source_url',), str(exc) + '\n还没有建立监测。')
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

    def _question(self, event, claim, task, values, missing, text):
        update = TaskUpdate('policy_watch', 'collecting', task.initial_request if task else event.content,
            values, missing, task.task_id if task else '', task.version if task else None)
        return self.store.reminders.commit(event, claim, update, text, now=self.clock())

    async def execute_job(self, job, renderer):
        watch = await asyncio.to_thread(self.store.policy_watches.for_job, job)
        if not watch:
            return {'status': 'cancelled'}
        claim = await asyncio.to_thread(self.store.begin_event, job['event_key'], now=self.clock())
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
            await asyncio.to_thread(self.store.prepare_event_outbox, job['event_key'], claim.claim_token, watch['platform'],
                'group', watch['group_id'], watch['owner_id'], '', payload, '[规则监测]', now=self.clock(), assistant_text=reply)
            return {'status': 'handled'}
        return {'status': 'observed'}
