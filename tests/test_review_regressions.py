import asyncio
import tempfile
import unittest
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

from agents.travel_agent import TravelAgent
from agents.travel_decision import decide_travel_action
from app.group_trigger_policy import GroupTriggerPolicy
from core.chat_transport import ChatEvent
from infrastructure.memory_store import MemoryStore, ConversationTurn
from services.reservation_service import ReservationService, normalize_extraction_item
from tools.agent_tools import AgentToolContext
from tools.reservation_tools import ReservationToolExecutor, AgentToolRouter
from test_travel_agent import FakeClient, assistant_message, completion, tool_call
from core.settings import Settings


class ReviewStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = MemoryStore(Path(self.temp.name) / 'review.db')
        self.service = ReservationService(self.store)
        self.image, _ = self.store.create_reservation_image(
            storage_scope_id='onebot:g', platform='onebot', group_id='g',
            uploader_id='alice', sha256='a' * 64, file_path='image.jpg',
            content_type='image/jpeg', byte_size=10, model_id='fake')

    def draft(self):
        plan = self.service.create_draft(self.image, ())
        return self.service.add_manual_item(
            platform='onebot', group_id='g', creator_id='alice',
            plan_code=plan.plan_code, attraction_name='青海湖',
            visit_date=date(2030, 8, 20), advance_value=1,
            advance_unit='day', requires_reservation=True)

    def test_shared_image_preserves_current_creator_and_replay(self):
        shared, is_new = self.store.create_reservation_image(
            storage_scope_id='onebot:g', platform='onebot', group_id='g',
            uploader_id='bob', sha256='a' * 64, file_path='image.jpg',
            content_type='image/jpeg', byte_size=10, model_id='fake')
        self.assertFalse(is_new)
        self.assertEqual(shared.image_id, self.image.image_id)
        alice = self.service.create_draft(self.image, (), creator_id='alice', source_event_id='a')
        bob = self.service.create_draft(shared, (), creator_id='bob', source_event_id='b')
        replay = self.service.create_draft(self.image, (), creator_id='bob', source_event_id='b')
        self.assertEqual((alice.creator_id, bob.creator_id), ('alice', 'bob'))
        self.assertEqual(bob.plan_id, replay.plan_id)
        self.assertEqual(alice.image_id, bob.image_id)
        self.assertEqual(self.image.uploader_id, 'alice')
        other, is_new = self.store.create_reservation_image(
            storage_scope_id='onebot:other', platform='onebot', group_id='other',
            uploader_id='bob', sha256='a' * 64, file_path='image.jpg',
            content_type='image/jpeg', byte_size=10, model_id='fake')
        self.assertTrue(is_new)
        other_plan = self.service.create_draft(other, (), creator_id='bob', source_event_id='other')
        self.assertIsNone(self.store.get_reservation_plan('onebot', 'g', other_plan.plan_code))

    def test_confirmation_rejects_interleaved_date_change(self):
        plan = self.draft()
        confirm = self.store.confirm_reservation_plan
        def interleave(*args, **kwargs):
            self.service.complete_item_date('onebot', 'g', 'alice', plan.plan_code, 1, date(2030, 8, 25))
            return confirm(*args, **kwargs)
        with patch.object(self.store, 'confirm_reservation_plan', side_effect=interleave):
            with self.assertRaisesRegex(ValueError, '版本|变化'):
                self.service.confirm_plan('onebot', 'g', 'alice', plan.plan_code)
        self.assertEqual(self.store.get_reservation_plan('onebot', 'g', plan.plan_code).status, 'draft')
        with self.store._connect() as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM reservation_reminders').fetchone()[0], 0)

    def test_reservation_read_write_read_is_fresh(self):
        plan = self.draft()
        executor = ReservationToolExecutor(self.service, None)
        context = AgentToolContext('onebot', 'g', 'alice', 'event')
        self.store.begin_event('event')
        first = executor.execute('list_reservation_plans', {}, context)
        executor.execute('complete_reservation_item_date', {
            'plan_code': plan.plan_code, 'item_index': 1, 'visit_date': '2030-08-25'}, context)
        second = executor.execute('list_reservation_plans', {}, context)
        self.assertIn('2030-08-20', first)
        self.assertIn('2030-08-25', second)
        self.assertNotIn('2030-08-20', second)

    def test_unaddressed_chatter_does_not_trigger(self):
        policy = GroupTriggerPolicy(self.store)
        for text in ('明天一起吃饭', '这份文件我晚点发', '今天的天气真好', '我已经预约好了'):
            with self.subTest(text=text):
                event = ChatEvent('onebot', 'group', 'e', 'g', 'alice', text)
                self.assertFalse(asyncio.run(policy.should_handle(event)))

    def add_trip(self, name, text):
        return self.store.add_document(
            group_openid='onebot:g', uploader_openid='alice', filename=name,
            sha256=name, full_text=text, chunks=[text])

    def extracted_draft(self):
        items = tuple(normalize_extraction_item({
            'attraction_name': name, 'requires_reservation': True,
            'advance_value': 1, 'advance_unit': 'day', 'confidence': 1,
        }) for name in ('青海湖', '莫高窟'))
        return self.service.create_draft(self.image, items)

    def test_document_conflict_blocks_cross_version_merge(self):
        first_doc = self.add_trip('v1.md', '2030-08-20 游览青海湖。')
        plan = self.extracted_draft()
        self.assertEqual(plan.items[0].date_source['document_id'], first_doc.document_id)
        self.assertIn('2030-08-20', plan.items[0].date_source['evidence'])
        self.add_trip('v2.md', '2030-08-25 游览青海湖。\n2030-08-26 游览莫高窟。')
        result = self.service.refresh_plan('onebot', 'g', 'alice', plan.plan_code)
        self.assertTrue(result.plan.source_conflicts)
        self.assertIsNone(result.plan.items[1].visit_date)
        with self.assertRaisesRegex(ValueError, '冲突'):
            self.service.confirm_plan('onebot', 'g', 'alice', plan.plan_code)
        # The owner explicitly keeps the first date, allowing a documented override.
        self.service.complete_item_date('onebot', 'g', 'alice', plan.plan_code, 1, date(2030, 8, 20))
        result = self.service.refresh_plan('onebot', 'g', 'alice', plan.plan_code)
        self.assertEqual(result.plan.items[0].date_source['origin'], 'manual')
        self.assertEqual(result.plan.items[1].visit_date, date(2030, 8, 26))
        self.assertEqual(result.plan.items[1].date_source['filename'], 'v2.md')

    def test_confirmation_previews_new_dates_before_creating_reminders(self):
        plan = self.extracted_draft()
        self.add_trip('trip.md', '2030-08-25 游览青海湖。\n2030-08-26 游览莫高窟。')
        with self.assertRaisesRegex(ValueError, '核对'):
            self.service.confirm_plan('onebot', 'g', 'alice', plan.plan_code)
        self.assertEqual(self.store.get_reservation_plan('onebot', 'g', plan.plan_code).status, 'draft')
        confirmed = self.service.confirm_plan('onebot', 'g', 'alice', plan.plan_code)
        self.assertEqual(confirmed.status, 'confirmed')

    def test_agent_stops_at_confirmation_preview_instead_of_retrying(self):
        plan = self.extracted_draft()
        self.add_trip('trip.md', '2030-08-25 游览青海湖。\n2030-08-26 游览莫高窟。')
        router = AgentToolRouter(None, self.service, None)
        settings = Settings('', '', frozenset(), '', 'fake', 'https://example.test', 'fake')
        client = FakeClient([completion(assistant_message(tool_calls=[tool_call(
            'confirm', 'confirm_reservation_plan', '{"plan_code":"' + plan.plan_code + '"}')]))])
        agent = TravelAgent(settings, router.execute_result, client=client)
        result = agent.run('确认预约 ' + plan.plan_code, tool_context=AgentToolContext('onebot', 'g', 'alice', 'event'))
        self.assertEqual(result.status, 'needs_input')
        self.assertIn('核对', result.reply)
        self.assertEqual(len(client.completions.requests), 1)
        self.assertEqual(self.store.get_reservation_plan('onebot', 'g', plan.plan_code).status, 'draft')

    def test_legacy_database_migrates_without_losing_plan(self):
        plan = self.draft()
        with self.store._connect() as connection:
            for trigger in ('reservation_version_insert', 'reservation_version_update',
                            'reservation_version_delete', 'reservation_version_status', 'reservation_manual_date_source'):
                connection.execute('DROP TRIGGER ' + trigger)
            connection.execute('ALTER TABLE reservation_items DROP COLUMN date_source_json')
            connection.execute('ALTER TABLE reservation_plans DROP COLUMN plan_version')
        for _ in range(2):
            store = MemoryStore(self.store.database_path)
            restored = store.get_reservation_plan('onebot', 'g', plan.plan_code)
            self.assertEqual(restored.items[0].visit_date, date(2030, 8, 20))
            self.assertEqual(restored.items[0].date_source, {})

    def test_confirmation_rejects_interleaved_custom_times_and_item_addition(self):
        for mutation in ('times', 'add'):
            with self.subTest(mutation=mutation):
                plan = self.draft()
                confirm = self.store.confirm_reservation_plan
                def interleave(*args, **kwargs):
                    if mutation == 'times':
                        self.service.set_draft_reminder_times('onebot', 'g', 'alice', plan.plan_code, 1,
                            (datetime(2030, 8, 19, tzinfo=timezone.utc),))
                    else:
                        self.service.add_manual_item(
                            platform='onebot', group_id='g', creator_id='alice',
                            plan_code=plan.plan_code, attraction_name='莫高窟',
                            visit_date=date(2030, 8, 26), advance_value=1,
                            advance_unit='day', requires_reservation=True)
                    return confirm(*args, **kwargs)
                with patch.object(self.store, 'confirm_reservation_plan', side_effect=interleave):
                    with self.assertRaisesRegex(ValueError, '版本'):
                        self.service.confirm_plan('onebot', 'g', 'alice', plan.plan_code)

    def test_clarification_trigger_is_scoped_to_member(self):
        self.store.save_turn('onebot:g', 'alice', 'clarification', '现在天气怎么样？', '想查询哪个城市？')
        policy = GroupTriggerPolicy(self.store)
        self.assertTrue(asyncio.run(policy.should_handle(ChatEvent('onebot', 'group', 'e', 'g', 'alice', '西宁'))))
        self.assertFalse(asyncio.run(policy.should_handle(ChatEvent('onebot', 'group', 'e', 'g', 'bob', '西宁'))))


class ReviewAgentTests(unittest.TestCase):
    def agent(self, responses, execute=lambda name, args: '工具错误：失败'):
        settings = Settings('', '', frozenset(), '', 'fake', 'https://example.test', 'fake')
        client = FakeClient(responses)
        return TravelAgent(settings, execute, client=client), client

    def test_invalid_json_does_not_unlock_success(self):
        agent, _ = self.agent([
            completion(assistant_message(tool_calls=[tool_call('a', 'confirm_reservation_plan', '{broken')])),
            *[completion(assistant_message(content='预约已确认')) for _ in range(3)],
        ])
        result = agent.run('确认预约 R-20300801-001')
        self.assertNotEqual(result.reply, '预约已确认')

    def test_query_cannot_satisfy_confirmation(self):
        agent, _ = self.agent([
            completion(assistant_message(tool_calls=[tool_call('a', 'list_reservation_plans', '{}')])),
            *[completion(assistant_message(content='预约已确认')) for _ in range(3)],
        ], lambda name, args: '预约列表')
        self.assertNotEqual(agent.run('确认预约 R-20300801-001').reply, '预约已确认')

    def test_failed_tool_does_not_unlock_success(self):
        agent, _ = self.agent([
            completion(assistant_message(tool_calls=[tool_call('a', 'confirm_reservation_plan', '{"plan_code":"R-20300801-001"}')])),
            *[completion(assistant_message(content='预约已确认')) for _ in range(3)],
        ])
        self.assertNotEqual(agent.run('确认预约 R-20300801-001').reply, '预约已确认')

    def test_weather_clarifies_once(self):
        agent, client = self.agent([completion(assistant_message(content='想查询哪个城市？'))])
        result = agent.run('现在天气怎么样？')
        self.assertIn('城市', result.reply)
        self.assertEqual(result.status, 'needs_input')
        self.assertLessEqual(len(client.completions.requests), 1)

    def test_location_answer_resumes_weather(self):
        calls = []
        agent, _ = self.agent([
            completion(assistant_message(tool_calls=[tool_call('a', 'get_current_weather', '{"location":"西宁"}')])),
            completion(assistant_message(content='西宁晴')),
        ], lambda name, args: calls.append((name, args)) or '西宁晴')
        history = (ConversationTurn('现在天气怎么样？', '想查询哪个城市？', datetime.now(timezone.utc).isoformat()),)
        self.assertEqual(agent.run('西宁', history).reply, '西宁晴')
        self.assertEqual(calls[0][0], 'get_current_weather')

    def test_wrong_resource_is_not_executed(self):
        calls = []
        agent, _ = self.agent([
            completion(assistant_message(tool_calls=[tool_call('a', 'confirm_reservation_plan', '{"plan_code":"R-20300801-002"}')])),
            *[completion(assistant_message(content='预约已确认')) for _ in range(3)],
        ], lambda name, args: calls.append(name) or '已确认')
        self.assertEqual(agent.run('确认预约 R-20300801-001').status, 'failed')
        self.assertEqual(calls, [])

    def test_query_and_negation_do_not_authorize_mutations(self):
        for text in ('查看预约提醒', '只查看预约，不要取消或确认 R-20300801-001'):
            with self.subTest(text=text):
                self.assertEqual(decide_travel_action(text).allowed_tools, ('list_reservation_plans',))

    def test_compound_reservation_and_weather_preserves_both_results(self):
        agent, _ = self.agent([
            completion(assistant_message(tool_calls=[
                tool_call('a', 'confirm_reservation_plan', '{"plan_code":"R-20300801-001"}'),
                tool_call('b', 'get_current_weather', '{"location":"西宁"}'),
            ])),
            completion(assistant_message(content='完成了')),
        ], lambda name, args: '预约已确认' if name == 'confirm_reservation_plan' else '西宁晴')
        result = agent.run('确认预约 R-20300801-001，西宁现在天气怎么样？')
        self.assertIn('预约已确认', result.reply)
        self.assertIn('西宁晴', result.reply)

    def test_different_actions_are_bound_to_their_own_resources(self):
        decision = decide_travel_action('确认预约 R-20300801-001，取消预约 R-20300801-002')
        self.assertEqual(set(decision.action_resources), {
            ('confirm_reservation_plan', 'R-20300801-001'),
            ('cancel_reservation_plan', 'R-20300801-002'),
        })

    def test_expired_clarification_does_not_resume(self):
        from agents.clarification import resume_weather_request
        history = (ConversationTurn('现在天气怎么样？', '想查询哪个城市？',
            (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()),)
        self.assertEqual(resume_weather_request('西宁', history), '西宁')

    def test_failed_read_can_retry_and_reservation_reads_are_not_cached(self):
        replies = iter(('工具错误：临时失败', '2030-08-20', '2030-08-25'))
        calls = []
        agent, _ = self.agent([
            *[completion(assistant_message(tool_calls=[tool_call(str(i), 'list_reservation_plans', '{}')])) for i in range(3)],
            completion(assistant_message(content='2030-08-25')),
        ], lambda name, args: calls.append(name) or next(replies))
        self.assertIn('2030-08-25', agent.run('查看预约提醒').reply)
        self.assertEqual(len(calls), 3)
