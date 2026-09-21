"""Real model/tools through the OneBot application chain, using temporary data and no QQ sends."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv
from adapters.onebot_app import OneBotAdapter, OneBotReplyRenderer
from app.runtime_factory import build_runtime
from core.settings import Settings, OneBotSettings
from infrastructure.memory_store import MemoryStore


class RecordingTransport:
    def __init__(self):
        self.messages = []

    async def send(self, message):
        self.messages.append(message)


async def main():
    load_dotenv()
    settings = Settings('', '', frozenset({'smoke-group'}), os.getenv('AMAP_API_KEY', ''),
        os.getenv('LLM_API_KEY', ''), os.getenv('LLM_BASE_URL', ''), os.getenv('LLM_MODEL_ID', ''))
    with tempfile.TemporaryDirectory() as folder:
        store = MemoryStore(Path(folder) / 'smoke.db')
        transport = RecordingTransport()
        runtime = build_runtime(settings, platform='onebot', store=store, transport=transport,
                                reply_renderer=OneBotReplyRenderer(), group_allowed=lambda group: group == 'smoke-group')
        adapter = OneBotAdapter(OneBotSettings('http://localhost', 'unused', 'unused', frozenset({'smoke-group'})),
                                runtime.application, store)
        async def send(text, event_id, *, at=False, user='smoke-user'):
            message = ([{'type': 'at', 'data': {'qq': 'smoke-bot'}}] if at else [])
            message.append({'type': 'text', 'data': {'text': text}})
            return await adapter.handle({'post_type': 'message', 'message_type': 'group',
                'group_id': 'smoke-group', 'user_id': user, 'self_id': 'smoke-bot',
                'message_id': event_id, 'message': message})

        results = []
        await send('现在天气怎么样', 'weather-start', at=True)
        turns = store.get_recent_turns('onebot:smoke-group', 'smoke-user')
        assert '城市' in turns[-1].assistant_content
        assert await send('武汉', 'different-user', user='other') == {'status': 'observed'}
        assert await send('武汉', 'weather-location') == {'status': 'handled'}
        tasks = store.tasks.recent('onebot', 'onebot:smoke-group', 'smoke-user')
        assert any(task.status == 'completed' and task.slots.get('location') == '武汉' for task in tasks)
        results.append({'check': 'weather_onebot_memory_continuation', 'status': 'passed'})

        await send('明天提醒我抢高铁票', 'reminder-start', at=True)
        assert '几点' in transport.messages[-1].payload['message']
        await send('上午十点', 'reminder-time')
        rows = store.reminders.list_for_owner('onebot', 'smoke-group', 'smoke-user')
        assert len(rows) == 1 and rows[0]['title'] == '抢高铁票'
        await send('上午十点', 'reminder-time')
        assert len(store.reminders.list_for_owner('onebot', 'smoke-group', 'smoke-user')) == 1
        results.append({'check': 'text_reminder_collect_time_and_replay', 'status': 'passed'})

        await send('提醒我明天上午十点带身份证', 'semantic-order', at=True)
        rows = store.reminders.list_for_owner('onebot', 'smoke-group', 'smoke-user')
        assert any(row['title'] == '带身份证' for row in rows), transport.messages[-1].payload['message']
        results.append({'check': 'live_model_reminder_semantic_extraction', 'status': 'passed'})

        due = max(datetime.fromisoformat(row['scheduled_at_utc']) for row in rows)
        count = await runtime.reminder_scheduler.scan_once(due)
        assert count == 2
        assert await runtime.outbox_worker.dispatch_due_once(due) == 2
        assert await runtime.reminder_scheduler.scan_once(due) == 0
        assert all(row['delivery_status'] == 'sent' for row in store.reminders.list_for_owner('onebot', 'smoke-group', 'smoke-user'))
        results.append({'check': 'reminder_scheduler_outbox_receipts', 'status': 'passed'})
        print(json.dumps({'model': settings.llm_model_id, 'results': results}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    asyncio.run(main())
