import tempfile
import unittest
from unittest.mock import patch, Mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

from adapters.onebot_app import OneBotAdapter, OneBotReplyRenderer
from agents.travel_agent import TravelAgent
from app.runtime_factory import build_runtime
from core.settings import OneBotSettings, Settings
from infrastructure.memory_store import MemoryStore
from infrastructure.task_repository import TaskConflict
from core.chat_transport import ChatEvent
from core.tasks import TaskUpdate, utc_now
from test_travel_agent import FakeClient, assistant_message, completion, tool_call


class RecordingTransport:
    def __init__(self):
        self.messages = []

    async def send(self, message):
        self.messages.append(message)
        return f'bot-{len(self.messages)}'


class TaskConversationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = MemoryStore(Path(self.temp.name) / 'test.db')
        self.transport = RecordingTransport()
        self.settings = Settings('', '', frozenset({'100'}), '', '', '', '')
        self.runtime = build_runtime(self.settings, platform='onebot', store=self.store,
            transport=self.transport, reply_renderer=OneBotReplyRenderer(),
            group_allowed=lambda group: group == '100')
        self.calls = []
        self.agent_settings = Settings('', '', frozenset({'100'}), '', 'fake', 'https://example.test', 'fake')
        self.client = FakeClient([
            completion(assistant_message(tool_calls=[tool_call('weather', 'get_current_weather', '{"location":"武汉"}')])),
            completion(assistant_message(content='武汉天气：晴，数据来自测试工具。')),
        ])
        self.runtime.application.travel_agent = TravelAgent(self.agent_settings,
            lambda name, args: self.calls.append((name, args)) or '武汉晴', client=self.client)
        self.adapter = OneBotAdapter(
            OneBotSettings('http://localhost:3000', 'out', 'in', frozenset({'100'})),
            self.runtime.application, self.store)

    async def message(self, text, message_id, *, user='200', mention=False, reply_to='', nickname=''):
        segments = ([{'type': 'at', 'data': {'qq': '300'}}] if mention else [])
        if reply_to:
            segments.append({'type': 'reply', 'data': {'id': reply_to, 'user_id': '300'}})
        segments.append({'type': 'text', 'data': {'text': text}})
        return await self.adapter.handle({
            'post_type': 'message', 'message_type': 'group', 'group_id': '100',
            'user_id': user, 'self_id': '300', 'message_id': message_id,
            'message': segments, 'sender': {'nickname': nickname, 'user_id': user},
        })

    async def test_weather_reply_is_saved_and_unmentioned_city_resumes(self):
        await self.message('现在天气怎么样', 'first', mention=True)
        turns = self.store.get_recent_turns('onebot:100', '200')
        self.assertIn('城市', turns[-1].assistant_content)
        self.assertEqual(await self.message('武汉', 'second'), {'status': 'handled'})
        self.assertEqual(self.calls, [('get_current_weather', {'location': '武汉'})])
        self.assertIn('武汉天气', self.transport.messages[-1].payload['message'])

    async def test_other_member_cannot_complete_weather_task(self):
        await self.message('现在天气怎么样', 'first', mention=True)
        self.assertEqual(await self.message('武汉', 'other', user='201'), {'status': 'observed'})
        self.assertEqual(self.calls, [])
        await self.message('武汉', 'second', mention=True)
        self.assertEqual(len(self.calls), 1)

    async def test_casual_meal_chatter_without_mention_does_not_start_places_task(self):
        self.assertEqual(await self.message('午饭吃什么呀', 'chatter'), {'status': 'observed'})
        self.assertEqual(self.calls, [])
        self.assertEqual(self.transport.messages, [])

    async def test_same_nickname_does_not_share_task_and_renaming_keeps_owner(self):
        await self.message('现在天气怎么样', 'first', mention=True, nickname='旅行者')
        self.assertEqual(await self.message('武汉', 'other', user='201', nickname='旅行者'), {'status': 'observed'})
        self.assertEqual(self.calls, [])
        self.assertEqual(await self.message('武汉', 'owner', nickname='新昵称'), {'status': 'handled'})
        self.assertEqual(self.calls, [('get_current_weather', {'location': '武汉'})])

    async def test_weather_task_survives_restart_and_missing_dialogue(self):
        await self.message('现在天气怎么样', 'first', mention=True)
        with self.store._connect() as connection:
            connection.execute('DELETE FROM conversation_turns')
        self.runtime.application.store = MemoryStore(self.store.database_path)
        # Context builder uses its own store instance against the same persisted DB.
        self.assertEqual(await self.message('明天一起吃饭', 'chatter'), {'status': 'observed'})
        self.assertEqual(await self.message('武汉', 'second'), {'status': 'handled'})
        self.assertEqual(self.calls[0][1], {'location': '武汉'})

    async def test_task_is_available_before_outbound_response_finishes(self):
        transport_send = self.transport.send
        received_second = []
        async def interleave(message):
            await transport_send(message)
            if len(self.transport.messages) == 1:
                received_second.append(await self.message('武汉', 'second'))
        self.transport.send = interleave
        await self.message('现在天气怎么样', 'first', mention=True)
        self.assertEqual(received_second, [{'status': 'handled'}])
        self.assertEqual(len(self.calls), 1)

    async def test_task_compare_and_swap_rejects_stale_update(self):
        await self.message('现在天气怎么样', 'first', mention=True)
        task = self.store.tasks.recent('onebot', 'onebot:100', '200')[0]
        update = TaskUpdate('weather', 'completed', '武汉天气', {'location': '武汉'},
                            task_id=task.task_id, expected_version=task.version)
        event = ChatEvent('onebot', 'group', 'update', '100', '200', '武汉')
        claim = self.store.begin_event(event.event_key)
        self.store.tasks.prepare_result(event, claim, update, '武汉晴')
        stale_event = ChatEvent('onebot', 'group', 'stale', '100', '200', '武汉')
        stale_claim = self.store.begin_event(stale_event.event_key)
        with self.assertRaises(TaskConflict):
            self.store.tasks.prepare_result(stale_event, stale_claim, update, '旧回复')

    async def test_expired_task_does_not_claim_unmentioned_city(self):
        await self.message('现在天气怎么样', 'first', mention=True)
        with self.store._connect() as connection:
            connection.execute('DELETE FROM conversation_turns')
            connection.execute('UPDATE travel_tasks SET expires_at=?', ((utc_now()-timedelta(hours=1)).isoformat(),))
        self.assertEqual(await self.message('武汉', 'second'), {'status': 'observed'})

    async def test_cancelled_task_does_not_revive_from_old_dialogue(self):
        await self.message('现在天气怎么样', 'first', mention=True)
        await self.message('取消当前任务', 'cancel')
        with self.store._connect() as connection:
            connection.execute("DELETE FROM conversation_turns WHERE user_content='取消当前任务'")
        self.assertEqual(await self.message('武汉', 'after-cancel'), {'status': 'observed'})

    async def test_repeated_question_updates_same_pending_weather_task(self):
        await self.message('现在天气怎么样', 'first', mention=True)
        await self.message('现在天气怎么样', 'again', mention=True)
        tasks = self.store.tasks.recent('onebot', 'onebot:100', '200')
        self.assertEqual(len(tasks), 1)
        await self.message('武汉', 'second')
        self.assertEqual(len(self.calls), 1)

    def reminder_clock(self):
        now = datetime(2030, 9, 9, 2, tzinfo=timezone.utc)
        self.runtime.application.personal_reminder_service.clock = lambda: now
        return now

    async def test_text_reminder_collects_time_without_picture_and_delivers_once(self):
        now = self.reminder_clock()
        await self.message('明天提醒我抢高铁票', 'r1', mention=True)
        self.assertIn('几点', self.transport.messages[-1].payload['message'])
        self.assertEqual(self.store.reminders.list_for_owner('onebot', '100', '200'), ())
        await self.message('上午十点', 'r2')
        self.assertIn('已设置', self.transport.messages[-1].payload['message'])
        rows = self.store.reminders.list_for_owner('onebot', '100', '200')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['title'], '抢高铁票')
        self.assertEqual(rows[0]['scheduled_at_utc'], '2030-09-10T02:00:00+00:00')
        await self.message('上午十点', 'r2')
        self.assertEqual(len(self.store.reminders.list_for_owner('onebot', '100', '200')), 1)
        due = now + timedelta(days=1)
        self.assertEqual(await self.runtime.reminder_scheduler.scan_once(due), 1)
        self.assertEqual(await self.runtime.reminder_scheduler.scan_once(due), 0)
        before = len(self.transport.messages)
        await self.runtime.outbox_worker.dispatch_due_once(due)
        await self.runtime.outbox_worker.dispatch_due_once(due)
        self.assertEqual(len(self.transport.messages), before + 1)
        self.assertEqual(self.transport.messages[-1].payload['message'][0]['data']['qq'], '200')
        self.assertEqual(self.store.reminders.list_for_owner('onebot', '100', '200')[0]['delivery_status'], 'sent')

    async def test_reminder_date_is_not_visit_date(self):
        self.reminder_clock()
        await self.message('9月20号提醒我预约湖北省博物馆', 'r1', mention=True)
        await self.message('早上九点', 'r2')
        row = self.store.reminders.list_for_owner('onebot', '100', '200')[0]
        self.assertEqual(row['scheduled_at_utc'], '2030-09-20T01:00:00+00:00')
        self.assertEqual(row['title'], '预约湖北省博物馆')
        with self.store._connect() as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM reservation_plans').fetchone()[0], 0)

    async def test_explicit_time_reminder_does_not_require_policy_mentioned_in_title(self):
        self.reminder_clock()
        await self.message('明天上午十点提醒我查看高铁放票时间', 'r1')
        row = self.store.reminders.list_for_owner('onebot', '100', '200')[0]
        self.assertEqual(row['title'], '查看高铁放票时间')

    async def test_reminder_update_and_cancel_use_owned_reference(self):
        self.reminder_clock()
        await self.message('明天上午十点提醒我抢高铁票', 'r1')
        await self.message('把刚才那条改成九点半', 'r2')
        row = self.store.reminders.list_for_owner('onebot', '100', '200')[0]
        self.assertEqual(row['scheduled_at_utc'], '2030-09-10T01:30:00+00:00')
        await self.message('取消提醒 ' + row['reminder_id'], 'other', user='201')
        self.assertEqual(self.store.reminders.list_for_owner('onebot', '100', '200')[0]['status'], 'active')
        await self.message('抢高铁票那条不用提醒了', 'r3')
        self.assertEqual(self.store.reminders.list_for_owner('onebot', '100', '200')[0]['status'], 'cancelled')
        self.assertEqual(await self.runtime.reminder_scheduler.scan_once(datetime(2030, 9, 10, 3, tzinfo=timezone.utc)), 0)

    async def test_multiple_reminders_require_object_selection(self):
        self.reminder_clock()
        await self.message('明天上午十点提醒我抢高铁票', 'r1')
        await self.message('明天上午九点提醒我带身份证', 'r2')
        await self.message('取消那条提醒', 'r3')
        self.assertIn('哪一条', self.transport.messages[-1].payload['message'])
        await self.message('2', 'r4')
        rows = self.store.reminders.list_for_owner('onebot', '100', '200')
        self.assertEqual(next(row for row in rows if row['title'] == '抢高铁票')['status'], 'cancelled')

    async def test_new_weather_request_does_not_lose_pending_reminder(self):
        self.reminder_clock()
        await self.message('明天提醒我抢高铁票', 'r1')
        await self.message('武汉现在天气怎么样', 'w1', mention=True)
        await self.message('上午十点', 'r2')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.store.reminders.list_for_owner('onebot', '100', '200')), 1)

    async def test_queued_reminder_can_be_cancelled_before_send(self):
        now = self.reminder_clock()
        await self.message('明天上午十点提醒我抢高铁票', 'r1')
        due = now + timedelta(days=1)
        await self.runtime.reminder_scheduler.scan_once(due)
        await self.message('抢高铁票那条不用提醒了', 'r2')
        before = len(self.transport.messages)
        await self.runtime.outbox_worker.dispatch_due_once(due)
        self.assertEqual(len(self.transport.messages), before)

    async def test_unknown_opening_policy_does_not_create_guessed_reminder(self):
        self.reminder_clock()
        with patch.object(self.runtime.application.booking_reminder_service.resolver, 'fetch_text', side_effect=ValueError('offline')):
            await self.message('我想10月1日去看湖北省博物馆，到了可以预约的日期提醒我及时预约', 'r1', mention=True)
        self.assertIn('没有创建', self.transport.messages[-1].payload['message'])
        self.assertEqual(self.store.reminders.list_for_owner('onebot', '100', '200'), ())

    async def test_reply_selects_correct_pending_reminder(self):
        self.reminder_clock()
        await self.message('明天提醒我抢高铁票', 'r1')
        await self.message('后天提醒我带身份证', 'r2')
        await self.message('上午十点', 'ambiguous')
        self.assertIn('多条', self.transport.messages[-1].payload['message'])
        await self.message('上午十点', 'r3', reply_to='bot-1')
        rows = self.store.reminders.list_for_owner('onebot', '100', '200')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['title'], '抢高铁票')
        pending = self.store.tasks.recent('onebot', 'onebot:100', '200')
        self.assertTrue(any(task.slots.get('title') == '带身份证' and task.status == 'collecting' for task in pending))

    async def test_task_list_and_cancel_do_not_delete_created_reminders(self):
        self.reminder_clock()
        await self.message('明天上午十点提醒我抢高铁票', 'r1')
        await self.message('后天提醒我带身份证', 'r2')
        await self.message('查看任务', 'list')
        self.assertIn('等待补充信息', self.transport.messages[-1].payload['message'])
        await self.message('取消当前任务', 'cancel')
        self.assertEqual(len(self.store.reminders.list_for_owner('onebot', '100', '200')), 1)

    async def test_missing_reminder_title_is_collected_as_task_slot(self):
        self.reminder_clock()
        await self.message('明天上午十点提醒我', 'r1')
        self.assertIn('什么事', self.transport.messages[-1].payload['message'])
        await self.message('买10月1日的高铁票', 'r2')
        row = self.store.reminders.list_for_owner('onebot', '100', '200')[0]
        self.assertEqual(row['scheduled_at_utc'], '2030-09-10T02:00:00+00:00')
        self.assertEqual(row['title'], '买10月1日的高铁票')

    async def test_place_query_uses_structured_clarification_and_resumes(self):
        self.client.completions.responses = [
            completion(assistant_message(tool_calls=[tool_call('ask', 'request_missing_input',
                '{"question":"想查询哪个城市的博物馆？","missing_fields":["city"]}')])),
            completion(assistant_message(tool_calls=[tool_call('places', 'search_travel_places',
                '{"city":"武汉","keywords":"博物馆"}')])),
            completion(assistant_message(content='已查到武汉的博物馆地点。')),
        ]
        await self.message('推荐一些博物馆', 'p1', mention=True)
        self.assertIn('城市', self.transport.messages[-1].payload['message'])
        self.assertEqual(await self.message('武汉', 'p2'), {'status': 'handled'})
        self.assertEqual(self.calls, [('search_travel_places', {'city': '武汉', 'keywords': '博物馆'})])
        self.assertTrue(any(task.task_type == 'places' and task.status == 'completed'
                            for task in self.store.tasks.recent('onebot', 'onebot:100', '200')))

    async def test_transit_query_resumes_endpoints_without_switching_to_driving(self):
        self.client.completions.responses = [
            completion(assistant_message(tool_calls=[tool_call('transit', 'get_transit_route',
                '{"origin":"武汉站","destination":"湖北省博物馆","city":"武汉"}')])),
            completion(assistant_message(content='可按查询到的公共交通方案出行。')),
        ]
        await self.message('帮我看看地铁怎么走', 'p1', mention=True)
        await self.message('武汉站到湖北省博物馆', 'p2')
        self.assertEqual(self.calls[0][0], 'get_transit_route')

    async def test_weather_time_correction_keeps_one_pending_task(self):
        self.client.completions.responses = [
            completion(assistant_message(tool_calls=[tool_call('forecast', 'get_weather_forecast', '{"location":"武汉"}')])),
            completion(assistant_message(content='这是武汉的天气预报。')),
        ]
        await self.message('现在天气怎么样', 'w1', mention=True)
        await self.message('明天天气怎么样', 'w2', mention=True)
        tasks = self.store.tasks.recent('onebot', 'onebot:100', '200')
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].task_type, 'forecast')
        await self.message('武汉', 'w3')
        self.assertEqual(self.calls[0][0], 'get_weather_forecast')

    async def test_trip_flow_crosses_onebot_outbox_and_is_replay_safe(self):
        from test_trips import SPEC, PLACES, model_response, plan_value
        service = self.runtime.application.trip_service
        service.clock = lambda: datetime(2030, 9, 9, tzinfo=timezone.utc)
        service.planner.client = FakeClient([model_response(SPEC), model_response(plan_value()),
            model_response(plan_value((('h', 'a'), ('f', 'g'), ('d', 'e'))))])
        service.planner.amap = Mock()
        service.planner.amap.search_places.return_value = list(PLACES.values())
        request = '帮我规划10月1日开始的武汉三天行程，带老人，必去湖北省博物馆'
        await self.message(request, 'trip-create')
        event = ChatEvent('onebot', 'group', 'read', '100', '200', '')
        trips = self.store.trips.list_for_owner(event)
        self.assertEqual(len(trips), 1)
        sent = len(self.transport.messages)
        await self.message(request, 'trip-create')
        self.assertEqual(len(self.transport.messages), sent)
        await self.message('第二天改为室内', 'trip-edit')
        trip = self.store.trips.get(event, trips[0]['trip_id'])
        self.assertEqual(trip['version'], 2)
        self.assertEqual(trip['spec']['indoor_days'], [2])
        await self.message('查看行程 ' + trip['trip_id'], 'other-view', user='201')
        self.assertIn('没有找到你', self.transport.messages[-1].payload['message'])

    def test_onebot_segment_text_extraction_preserves_text_only(self):
        payload = {'message': [
            {'type': 'at', 'data': {'qq': '200'}},
            {'type': 'text', 'data': {'text': '请告诉我'}},
            {'type': 'image', 'data': {'file': 'private-file'}},
            {'type': 'text', 'data': {'text': '哪个城市？'}},
        ]}
        self.assertEqual(self.store._payload_reply_text(payload), '请告诉我哪个城市？')
