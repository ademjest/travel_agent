from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
import asyncio
from threading import Event
from unittest.mock import Mock

from adapters.onebot_app import OneBotAdapter, OneBotReplyRenderer
from app.runtime_factory import build_runtime
from core.chat_transport import ChatEvent
from core.settings import Settings, OneBotSettings
from infrastructure.memory_store import MemoryStore
from services.inbox_worker import InboxWorker
from services.scheduled_query_service import ScheduledQueryService, scheduled_query_request
from agents.travel_agent import TravelAgent
from test_travel_agent import FakeClient, assistant_message, completion, tool_call


def parsed(value):
    return completion(assistant_message(content=json.dumps(value, ensure_ascii=False)))


class ScheduledQueryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = MemoryStore(Path(self.temp.name) / 'scheduled.db')
        self.now = datetime(2030, 9, 9, 2, tzinfo=timezone.utc)
        class Transport:
            def __init__(self): self.messages = []
            async def send(self, message): self.messages.append(message)
        self.transport = Transport()
        self.runtime = build_runtime(Settings('', '', frozenset({'100'}), '', '', '', ''), platform='onebot',
            store=self.store, transport=self.transport, reply_renderer=OneBotReplyRenderer(), group_allowed=lambda group: group == '100')
        self.runtime.travel_service.execute_tool = Mock(return_value='【当前天气】武汉\n发布时间：测试执行时刻')
        self.client = FakeClient([parsed({'kind': 'weather', 'arguments': {'location': '武汉'}, 'time_text': '明天上午八点'})])
        self.service = ScheduledQueryService(self.store, self.runtime.travel_service, self.client, 'test', clock=lambda: self.now)
        self.runtime.application.scheduled_query_service = self.service
        self.worker = InboxWorker(self.store, OneBotAdapter(OneBotSettings('http://localhost', 'out', 'in', frozenset({'100'})), self.runtime.application, self.store))

    def event(self, text='', key='create', owner='owner'):
        return ChatEvent('onebot', 'group', key, '100', owner, text)

    def create(self):
        event = self.event('明天上午八点告诉我武汉天气')
        return self.service.handle(event, self.store.begin_event(event.event_key))

    def rows(self):
        return self.store.scheduled_queries.list_for_owner(self.event())

    async def test_data_source_is_called_only_at_due_time_and_once(self):
        reply = self.create()
        self.assertIn('实际查询', reply)
        self.runtime.travel_service.execute_tool.assert_not_called()
        row = self.rows()[0]
        due = datetime.fromisoformat(row['due_at'])
        self.assertEqual(await self.runtime.reminder_scheduler.scan_once(due-timedelta(seconds=1)), 0)
        self.assertEqual(await self.runtime.reminder_scheduler.scan_once(due), 1)
        self.assertEqual(await self.runtime.reminder_scheduler.scan_once(due), 0)
        self.now = due
        await self.worker.run_once(now=due)
        self.runtime.travel_service.execute_tool.assert_called_once_with('get_current_weather', {'location': '武汉'})
        self.assertEqual(self.rows()[0]['status'], 'sent')
        self.assertIn('定时查询结果', str(self.transport.messages[-1].payload))
        self.assertEqual(self.store.get_recent_turns('onebot:100', 'owner'), ())
        self.assertFalse(await self.worker.run_once(now=due))

    async def test_query_executes_via_readonly_registry_not_general_message_routing(self):
        self.client.completions.responses = [parsed({'kind': 'weather', 'arguments': {'location': '武汉帮我规划三天行程'}, 'time_text': '明天上午八点'})]
        event = self.event('明天上午八点告诉我武汉帮我规划三天行程天气')
        self.service.handle(event, self.store.begin_event(event.event_key))
        self.runtime.application.trip_service.handle = Mock(side_effect=AssertionError('must not route arbitrary messages'))
        due = datetime.fromisoformat(self.rows()[0]['due_at'])
        await self.runtime.reminder_scheduler.scan_once(due)
        await self.worker.run_once(now=due)
        self.runtime.application.trip_service.handle.assert_not_called()

    async def test_cancel_is_owner_scoped_and_cancels_queued_job(self):
        self.create()
        row = self.rows()[0]
        due = datetime.fromisoformat(row['due_at'])
        await self.runtime.reminder_scheduler.scan_once(due)
        self.assertFalse(self.store.scheduled_queries.cancel(self.event(owner='other'), row['query_id']))
        self.assertTrue(self.store.scheduled_queries.cancel(self.event(), row['query_id']))
        self.assertFalse(await self.worker.run_once(now=due))
        self.runtime.travel_service.execute_tool.assert_not_called()
        self.assertEqual(self.rows()[0]['status'], 'cancelled')

    async def test_source_failure_can_retry_without_caching_failure(self):
        self.create()
        due = datetime.fromisoformat(self.rows()[0]['due_at'])
        self.runtime.travel_service.execute_tool.side_effect = ['工具错误：暂时不可用', '武汉晴']
        await self.runtime.reminder_scheduler.scan_once(due)
        await self.worker.run_once(now=due)
        await self.worker.run_once(now=due+timedelta(seconds=10))
        self.assertEqual(self.runtime.travel_service.execute_tool.call_count, 2)
        self.assertEqual(self.rows()[0]['status'], 'sent')

    def test_remind_me_to_query_is_not_automatic_query(self):
        self.assertFalse(scheduled_query_request('明天上午八点提醒我查武汉天气'))
        self.assertTrue(scheduled_query_request('明天上午八点告诉我武汉天气'))

    def test_model_cannot_request_write_tool_or_inject_identity(self):
        for value in (
            {'kind': 'cancel_reservation_plan', 'arguments': {}, 'time_text': '明天上午八点'},
            {'kind': 'weather', 'arguments': {'location': '武汉', 'owner_id': 'other'}, 'time_text': '明天上午八点'},
        ):
            self.client.completions.responses = [parsed(value)]
            with self.assertRaises(ValueError):
                self.service._extract('明天上午八点告诉我武汉天气', {})
        self.assertEqual(self.rows(), ())

    def test_missing_city_continues_without_rebasing_time(self):
        self.client.completions.responses = [
            parsed({'kind': 'weather', 'arguments': {'location': ''}, 'time_text': '明天上午八点'}),
            parsed({'kind': 'weather', 'arguments': {'location': '武汉'}, 'time_text': '明天上午八点'}),
        ]
        event = self.event('明天上午八点告诉我天气', 'first')
        reply = self.service.handle(event, self.store.begin_event(event.event_key))
        self.assertIn('地点', reply)
        self.now += timedelta(minutes=1)
        event = self.event('武汉', 'city')
        self.service.handle(event, self.store.begin_event(event.event_key))
        self.assertEqual(self.rows()[0]['due_at'], '2030-09-10T00:00:00+00:00')

    async def test_control_requests_are_persisted_and_complete_without_waiting_for_due_job(self):
        self.create()
        row = self.rows()[0]
        payload = {'post_type': 'message', 'message_type': 'group', 'group_id': '100', 'user_id': 'owner',
                   'self_id': 'bot', 'message_id': 'cancel-control', 'message': [
                       {'type': 'text', 'data': {'text': '取消定时查询 ' + row['query_id']}}]}
        result = await self.worker.submit(payload)
        self.assertEqual(result['status'], 'accepted')
        self.assertEqual(self.rows()[0]['status'], 'cancelled')
        self.assertEqual(self.store.inbox.get('onebot:group:100:cancel-control')['status'], 'completed')

    async def test_due_readonly_query_is_not_blocked_by_same_owner_slow_conversation(self):
        self.create()
        started, release = Event(), Event()
        def slow(text):
            started.set()
            release.wait(5)
            return 'normal completed'
        self.runtime.travel_service.handle = slow
        await self.worker.submit({'post_type': 'message', 'message_type': 'group', 'group_id': '100', 'user_id': 'owner',
            'self_id': 'bot', 'message_id': 'slow', 'message': [
                {'type': 'at', 'data': {'qq': 'bot'}}, {'type': 'text', 'data': {'text': 'slow'}}]})
        running = asyncio.create_task(self.worker.run_once(lane='normal'))
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 2))
            due = datetime.fromisoformat(self.rows()[0]['due_at'])
            await self.runtime.reminder_scheduler.scan_once(due)
            await self.worker.run_once(now=due, lane='scheduled')
            self.assertEqual(self.rows()[0]['status'], 'sent')
        finally:
            release.set()
            await running

    async def test_scheduled_weather_result_can_be_followed_up_by_its_owner(self):
        self.create()
        due = datetime.fromisoformat(self.rows()[0]['due_at'])
        await self.runtime.reminder_scheduler.scan_once(due)
        await self.worker.run_once(now=due)
        calls = []
        client = FakeClient([
            completion(assistant_message(tool_calls=[tool_call('forecast', 'get_weather_forecast', '{"location":"武汉"}')])),
            completion(assistant_message(content='武汉的天气预报。')),
        ])
        self.runtime.application.travel_agent = TravelAgent(Settings('', '', frozenset(), '', 'fake', 'https://example.test', 'fake'),
            lambda name, args: calls.append((name, args)) or 'forecast', client=client)
        await self.worker.submit({'post_type': 'message', 'message_type': 'group', 'group_id': '100', 'user_id': 'owner',
            'self_id': 'bot', 'message_id': 'followup', 'message': [{'type': 'text', 'data': {'text': '那明天呢'}}]})
        await self.worker.run_once()
        self.assertEqual(calls, [('get_weather_forecast', {'location': '武汉'})])

    def test_period_clarification_does_not_require_model_to_reconstruct_time_text(self):
        self.client.completions.responses = [parsed({'kind': 'weather', 'arguments': {'location': '武汉'}, 'time_text': '明天八点'})]
        event = self.event('明天八点告诉我武汉天气', 'first')
        reply = self.service.handle(event, self.store.begin_event(event.event_key))
        self.assertIn('上午还是', reply)
        event = self.event('下午', 'period')
        self.service.handle(event, self.store.begin_event(event.event_key))
        self.assertEqual(self.rows()[0]['due_at'], '2030-09-10T12:00:00+00:00')
        self.assertEqual(len(self.client.completions.requests), 1)

    async def test_prepared_output_recovery_preserves_owner_mention(self):
        self.create()
        due = datetime.fromisoformat(self.rows()[0]['due_at'])
        await self.runtime.reminder_scheduler.scan_once(due)
        job = self.store.inbox.claim(due)
        claim = self.store.begin_event(job['event_key'])
        self.store.prepare_event_reply(claim.event_id, claim.claim_token, '【定时查询结果】cached', '[定时查询]')
        self.assertTrue(await self.worker._recover_prepared(job))
        output = self.store.list_outbox_for_event(job['event_key'])[0]
        self.assertEqual(output.payload['message'][0], {'type': 'at', 'data': {'qq': 'owner'}})
        self.runtime.travel_service.execute_tool.assert_not_called()

    async def test_old_due_query_is_not_executed_after_queue_outage(self):
        self.create()
        due = datetime.fromisoformat(self.rows()[0]['due_at'])
        await self.runtime.reminder_scheduler.scan_once(due)
        self.now = due + timedelta(hours=25)
        await self.worker.run_once(now=self.now)
        self.runtime.travel_service.execute_tool.assert_not_called()
        self.assertEqual(self.rows()[0]['status'], 'missed')

    async def test_already_prepared_scheduled_output_expires_on_delivery(self):
        self.create()
        due = datetime.fromisoformat(self.rows()[0]['due_at'])
        await self.runtime.reminder_scheduler.scan_once(due)
        job = self.store.inbox.claim(due)
        claim = self.store.begin_event(job['event_key'])
        self.store.prepare_event_outbox(job['event_key'], claim.claim_token, 'onebot', 'group', '100', 'owner', '',
            {'message': 'old weather'}, '[定时查询]', now=due)
        await self.runtime.outbox_worker.dispatch_due_once(due + timedelta(hours=25))
        self.assertEqual(self.rows()[0]['status'], 'missed')
        self.assertEqual(self.transport.messages, [])
