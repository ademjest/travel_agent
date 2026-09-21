"""Live official policy and model-led discovery; temporary storage, no QQ messages."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv
from adapters.onebot_app import OneBotAdapter, OneBotReplyRenderer
from app.runtime_factory import build_runtime
from core.settings import Settings, OneBotSettings
from infrastructure.memory_store import MemoryStore
from services.booking_policy import BookingPolicyResolver


class RecordingTransport:
    def __init__(self):
        self.messages = []

    async def send(self, message):
        self.messages.append(message)
        return f'local-receipt-{len(self.messages)}'


async def main():
    load_dotenv()
    groups = frozenset({'smoke-group', 'smoke-discovery'})
    settings = Settings('', '', groups, os.getenv('AMAP_API_KEY', ''),
        os.getenv('LLM_API_KEY', ''), os.getenv('LLM_BASE_URL', ''), os.getenv('LLM_MODEL_ID', ''))
    with tempfile.TemporaryDirectory() as folder:
        store = MemoryStore(Path(folder) / 'smoke.db')
        transport = RecordingTransport()
        runtime = build_runtime(settings, platform='onebot', store=store, transport=transport,
            reply_renderer=OneBotReplyRenderer(), group_allowed=lambda group: group in groups)
        adapter = OneBotAdapter(OneBotSettings('http://localhost', 'unused', 'unused', groups), runtime.application, store)
        async def send(text, key, at=True, group='smoke-group'):
            segments = ([{'type': 'at', 'data': {'qq': 'smoke-bot'}}] if at else [])
            segments.append({'type': 'text', 'data': {'text': text}})
            return await adapter.handle({'post_type': 'message', 'message_type': 'group', 'group_id': group,
                'user_id': 'smoke-user', 'self_id': 'smoke-bot', 'message_id': key, 'message': segments})

        results = []
        visit = (datetime.now(timezone.utc) + timedelta(days=30)).date().isoformat()
        await send(f'我想{visit}去湖北省博物馆，到了可以预约的日期提醒我及时预约', 'booking')
        assert '没有创建提醒' in transport.messages[-1].payload['message']
        # Explicit test-user confirmation is scoped to an isolated task and temporary DB only.
        await send('按这个规则设置提醒', 'confirm', at=False)
        rows = store.reminders.list_for_owner('onebot', 'smoke-group', 'smoke-user')
        assert len(rows) == 1, transport.messages[-1].payload['message']
        source = json.loads(rows[0]['source_json'])
        assert source['policy']['official'] and source['applicability_confirmed_by_user']
        results.append({'check': 'live_policy_review_confirmation', 'status': 'passed',
                        'source': source['policy']['source_url'], 'days_before': source['policy']['days_before']})

        # Isolate missing-city evaluation from the museum task's destination context.
        await send('推荐一些博物馆', 'places-ask', group='smoke-discovery')
        tasks = store.tasks.recent('onebot', 'onebot:smoke-discovery', 'smoke-user')
        assert any(task.task_type == 'places' and task.status == 'collecting' for task in tasks), transport.messages[-1].payload['message']
        await send('武汉', 'places-city', at=False, group='smoke-discovery')
        tasks = store.tasks.recent('onebot', 'onebot:smoke-discovery', 'smoke-user')
        assert any(task.task_type == 'places' and task.status == 'completed' for task in tasks), transport.messages[-1].payload['message']
        results.append({'check': 'live_discovery_clarification', 'status': 'passed'})

        await send('武汉站到湖北省博物馆坐地铁怎么走？', 'transit')
        tasks = store.tasks.recent('onebot', 'onebot:smoke-group', 'smoke-user')
        assert any(task.task_type == 'transit' and task.status == 'completed' for task in tasks), transport.messages[-1].payload['message']
        results.append({'check': 'live_model_transit_route', 'status': 'passed'})
        print(json.dumps({'model': settings.llm_model_id, 'results': results}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    asyncio.run(main())
