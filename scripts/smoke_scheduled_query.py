"""Wait one real minute, then query the real data source. Temporary DB and no QQ sends."""
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv
from adapters.onebot_app import OneBotAdapter, OneBotReplyRenderer
from app.runtime_factory import build_runtime
from core.chat_transport import ChatEvent
from core.settings import OneBotSettings, Settings
from infrastructure.memory_store import MemoryStore
from services.inbox_worker import InboxWorker


class RecordingTransport:
    def __init__(self): self.messages = []
    async def send(self, message):
        self.messages.append(message)
        return f'local-{len(self.messages)}'


async def main():
    load_dotenv()
    settings = Settings('', '', frozenset({'scheduled-smoke'}), os.getenv('AMAP_API_KEY', ''),
        os.getenv('LLM_API_KEY', ''), os.getenv('LLM_BASE_URL', ''), os.getenv('LLM_MODEL_ID', ''))
    with tempfile.TemporaryDirectory() as directory:
        store = MemoryStore(Path(directory) / 'schedule.db')
        transport = RecordingTransport()
        runtime = build_runtime(settings, platform='onebot', store=store, transport=transport,
            reply_renderer=OneBotReplyRenderer(), group_allowed=lambda group: group == 'scheduled-smoke')
        worker = InboxWorker(store, OneBotAdapter(OneBotSettings('http://localhost', 'unused', 'unused', frozenset({'scheduled-smoke'})), runtime.application, store))
        calls = []
        execute = runtime.travel_service.execute_tool
        def tracked(name, arguments):
            calls.append({'name': name, 'at': datetime.now(timezone.utc).isoformat()})
            return execute(name, arguments)
        runtime.travel_service.execute_tool = tracked
        await worker.submit({'post_type': 'message', 'message_type': 'group', 'group_id': 'scheduled-smoke',
            'user_id': 'scheduled-user', 'self_id': 'scheduled-bot', 'message_id': 'create',
            'time': datetime.now(timezone.utc).timestamp(), 'message': [
                {'type': 'at', 'data': {'qq': 'scheduled-bot'}},
                {'type': 'text', 'data': {'text': '1分钟后告诉我武汉当前天气'}},
            ]})
        await worker.run_once()
        event = ChatEvent('onebot', 'group', 'read', 'scheduled-smoke', 'scheduled-user', '')
        rows = store.scheduled_queries.list_for_owner(event)
        assert len(rows) == 1, str([message.payload for message in transport.messages])
        assert rows[0]['kind'] == 'weather'
        due = datetime.fromisoformat(rows[0]['due_at'])
        assert calls == []
        print(json.dumps({'state': 'waiting_for_real_due_time', 'due_at': due.isoformat()}, ensure_ascii=False), flush=True)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            await runtime.reminder_scheduler.scan_once()
            await worker.run_once()
            rows = store.scheduled_queries.list_for_owner(event)
            if rows[0]['status'] == 'sent':
                break
            await asyncio.sleep(0.5)
        assert rows[0]['status'] == 'sent', rows[0]['status']
        assert len(calls) == 1 and datetime.fromisoformat(calls[0]['at']) >= due, calls
        assert '定时查询结果' in str(transport.messages[-1].payload)
        print(json.dumps({'status': 'passed', 'checks': ['real_delayed_query_not_early', 'readonly_tool_at_due_time',
            'outbox_delivery_state'], 'due_at': due.isoformat(), 'tool_call': calls[0]}, ensure_ascii=False, indent=2))
        runtime.travel_agent.client.close()


if __name__ == '__main__':
    asyncio.run(main())
