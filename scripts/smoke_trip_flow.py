"""Real model and POI data; full OneBot trip flow with temporary DB and no QQ sends."""
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
from core.chat_transport import ChatEvent
from core.settings import OneBotSettings, Settings
from infrastructure.memory_store import MemoryStore
from agents.trip_planner import indoor_place


class LocalTransport:
    def __init__(self):
        self.messages = []

    async def send(self, message):
        self.messages.append(message)
        return f'local-{len(self.messages)}'


async def main():
    load_dotenv()
    settings = Settings('', '', frozenset({'trip-test'}), os.getenv('AMAP_API_KEY', ''),
        os.getenv('LLM_API_KEY', ''), os.getenv('LLM_BASE_URL', ''), os.getenv('LLM_MODEL_ID', ''))
    with tempfile.TemporaryDirectory() as folder:
        store = MemoryStore(Path(folder) / 'trip.db')
        transport = LocalTransport()
        runtime = build_runtime(settings, platform='onebot', transport=transport, reply_renderer=OneBotReplyRenderer(),
                                group_allowed=lambda group: group == 'trip-test', store=store)
        adapter = OneBotAdapter(OneBotSettings('http://localhost', 'unused', 'unused', frozenset({'trip-test'})), runtime.application, store)
        async def send(text, key):
            return await adapter.handle({'post_type': 'message', 'message_type': 'group', 'group_id': 'trip-test',
                'user_id': 'trip-user', 'self_id': 'trip-bot', 'message_id': key,
                'message': [{'type': 'at', 'data': {'qq': 'trip-bot'}}, {'type': 'text', 'data': {'text': text}}]})
        event = ChatEvent('onebot', 'group', 'read', 'trip-test', 'trip-user', '')
        start = (datetime.now(timezone.utc) + timedelta(days=30)).date().isoformat()
        await send(f'帮我规划{start}开始的武汉3天行程，带老人，每天安排2处景点，使用公共交通，必去湖北省博物馆，并提醒预约', 'create')
        trips = store.trips.list_for_owner(event)
        assert len(trips) == 1, transport.messages[-1].payload['message']
        trip = trips[0]
        assert len(trip['plan']['days']) == 3
        assert all(len(day['activities']) == 2 for day in trip['plan']['days'])
        assert trip['spec']['must_visit'] == ['湖北省博物馆']
        expected_legs = sum(len(day['activities'])-1 for day in trip['plan']['days'])
        assert expected_legs > 0, '本测试需要至少一个实际相邻地点交通段'
        assert all(len(day.get('legs', [])) == len(day['activities'])-1
                   and all(not leg.get('error') for leg in day.get('legs', [])) for day in trip['plan']['days']), json.dumps(
                       {'transport': trip['spec'].get('transport_text'), 'days': trip['plan']['days']}, ensure_ascii=False)
        await send('确认行程提醒', 'confirm-reminder')
        reminders = store.trips.linked_reminders(event, trip['trip_id'])
        assert len(reminders) == 1, transport.messages[-1].payload['message']
        first = store.trips.get(event, trip['trip_id'])
        await send('第二天改为室内', 'indoor')
        pending = store.tasks.recent('onebot', event.storage_scope_id, event.sender_id)
        if any(task.task_type == 'itinerary' and task.status == 'collecting' for task in pending):
            await send('确认行程修改', 'confirm-indoor')
        edited = store.trips.get(event, trip['trip_id'])
        assert edited['version'] > first['version'], transport.messages[-1].payload['message']
        assert all(indoor_place(edited['sources'][a['poi_id']]) for a in edited['plan']['days'][1]['activities'])
        assert edited['plan']['days'][0] == first['plan']['days'][0]
        assert edited['plan']['days'][2] == first['plan']['days'][2]
        new_start = (datetime.fromisoformat(start) + timedelta(days=1)).date().isoformat()
        await send(f'把行程改到{new_start}开始', 'date-edit')
        await send('确认行程修改', 'confirm-date')
        final = store.trips.get(event, trip['trip_id'])
        assert final['spec']['start_date'] == new_start, transport.messages[-1].payload['message']
        linked = store.trips.linked_reminders(event, trip['trip_id'])
        source = json.loads(linked[0]['source_json'])
        target_date = next(day['date'] for day in final['plan']['days'] for a in day['activities']
                           if final['sources'][a['poi_id']]['name'] == '湖北省博物馆')
        assert source['visit_date'] == target_date
        print(json.dumps({'model': settings.llm_model_id, 'status': 'passed', 'checks': [
            'dated_trip_from_live_pois', 'gentle_pace_and_required_place', 'official_policy_linked_reminder',
            'indoor_edit_preserves_other_days', 'trip_and_reminder_dates_updated_together', 'live_transit_legs_between_pois'],
            'trip_version': final['version'], 'reminder_version': linked[0]['version']}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    asyncio.run(main())
