from datetime import datetime, timezone
import json
import re

from core.tasks import TaskUpdate, location_answer, utc_now
from services.reminder_time import BEIJING, is_time_answer, parse_reminder_time


QUERY_TOOLS = {'weather': ('get_current_weather', ('location',)), 'forecast': ('get_weather_forecast', ('location',)),
               'traffic': ('get_route_traffic', ('origin', 'destination'))}


def scheduled_query_request(text):
    return bool(re.search(r'天气|路况', text) and re.search(r'告诉我|发给我|定时查询|届时查询|到时查', text)
                and re.search(r'定时查询|到时|届时|明天|后天|今晚|点|[:：]|分钟后|小时后|月\d', text)
                and not re.search(r'提醒我|不要|不用|不必|只是问', text))


def scheduled_query_control(text):
    return bool(re.fullmatch(r'查看定时查询(?:\s+[1-9]\d{0,2})?|取消定时查询\s+Q-[a-f0-9]{12}', text.strip()))


def scheduled_query_continuation(text, tasks):
    pending = [task for task in tasks if task.task_type == 'scheduled_query' and task.status == 'collecting']
    if len(pending) == 1 and (is_time_answer(text) or location_answer(text)):
        return pending[0]
    return None


class ScheduledQueryService:
    def __init__(self, store, travel_service, client=None, model='', clock=None):
        self.store, self.travel_service, self.client, self.model = store, travel_service, client, model
        self.clock = clock or utc_now

    def _extract(self, text, previous):
        if self.client is None:
            raise ValueError('定时查询的自然语言解析需要模型服务，请配置后重试。')
        response = self.client.chat.completions.create(model=self.model, response_format={'type': 'json_object'}, messages=[
            {'role': 'system', 'content': ('提取一次性定时查询需求，只返回 JSON：'
                '{"kind":"weather|forecast|traffic","arguments":{"location":"地点原文"},"time_text":"执行时间原文"}。'
                'weather为到时查询当前天气，forecast为到时查询天气预报，traffic为到时查询实时路况；'
                'traffic的arguments只包含origin和destination。字段缺失填空字符串。所有非空参数必须来自本轮原文或上次对应参数，'
                '时间也只能是本轮连续原文或上次值，不计算日期、不猜地点，不增加其他动作。')},
            {'role': 'user', 'content': json.dumps({'message': text, 'previous': previous}, ensure_ascii=False)},
        ])
        value = json.loads(response.choices[0].message.content or '{}')
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

    def handle(self, event, claim):
        text = event.content.strip()
        if scheduled_query_control(text):
            if text.startswith('查看定时查询'):
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
        tasks = self.store.tasks.recent(event.platform, event.storage_scope_id, event.sender_id, reply_to_id=event.reply_to_id)
        task = scheduled_query_continuation(text, tasks)
        if not scheduled_query_request(text) and task is None:
            return None
        if event.platform not in {'onebot', 'web'} or event.channel != 'group':
            return '当前定时查询支持在 OneBot 群内设置和接收结果。'
        if re.search(r'每天|每周|每隔', text):
            return '请先指定一次查询的日期和时刻；本次没有建立重复查询。'
        previous = task.slots if task else {}
        if task and is_time_answer(text):
            value = {'kind': previous['kind'], 'arguments': dict(previous['arguments']), 'time_text': text}
        else:
            value = self._extract(text, previous)
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

    async def execute_job(self, job, renderer):
        import asyncio
        from types import SimpleNamespace
        from core.chat_transport import storage_scope_id
        row = await asyncio.to_thread(self.store.scheduled_queries.for_job, job)
        if row is None:
            return {'status': 'cancelled'}
        if (self.clock()-datetime.fromisoformat(row['due_at'])).total_seconds() > 24*3600:
            await asyncio.to_thread(self.store.scheduled_queries.mark_missed, row)
            return {'status': 'expired'}
        claim = await asyncio.to_thread(self.store.begin_event, job['event_key'])
        if claim is None:
            return {'status': 'handled'}
        reply = claim.prepared_reply
        if reply is None:
            name, allowed = QUERY_TOOLS[row['kind']]
            arguments = json.loads(row['arguments_json'])
            if set(arguments) - set(allowed) or any(not isinstance(arguments.get(key), str) or not arguments[key] for key in allowed):
                raise ValueError('无效的定时查询参数')
            result = await asyncio.to_thread(self.travel_service.execute_tool, name, arguments)
            if result.startswith('工具错误：'):
                raise RuntimeError('scheduled query data source failed')
            planned = datetime.fromisoformat(row['due_at']).astimezone(BEIJING)
            reply = f'【定时查询结果】\n原定触发：{planned:%Y-%m-%d %H:%M:%S}（北京时间）；实际查询：{self.clock().astimezone(BEIJING):%Y-%m-%d %H:%M:%S}。\n' + result
            event = SimpleNamespace(event_key=job['event_key'], platform=row['platform'],
                storage_scope_id=storage_scope_id(row['platform'], row['group_id']), sender_id=row['owner_id'], content='[定时查询]')
            initial = {'weather': '当前天气', 'forecast': '天气预报', 'traffic': '路况'}[row['kind']]
            update = TaskUpdate(row['kind'], 'completed', ' → '.join(arguments[key] for key in allowed) + initial,
                {**arguments, 'intents': [row['kind']], 'source': 'scheduled_query', 'query_id': row['query_id'],
                 'reference_time': self.clock().isoformat()})
            await asyncio.to_thread(self.store.tasks.prepare_result, event, claim, update, reply)
        payload = renderer.render_reminder(row['owner_id'], reply)
        await asyncio.to_thread(self.store.prepare_event_outbox, job['event_key'], claim.claim_token, row['platform'],
            'group', row['group_id'], row['owner_id'], '', payload, '[定时查询]', assistant_text=reply)
        return {'status': 'handled'}
