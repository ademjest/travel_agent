from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock

from agents.intent_compiler import IntentCompiler
from app.group_trigger_policy import GroupTriggerPolicy
from core.chat_transport import ChatEvent
from infrastructure.memory_store import MemoryStore
from services.personal_reminder_service import PersonalReminderService
from services.reminder_time import is_time_answer
from services.semantic_task_service import SemanticTaskService, semantic_candidate


class Client:
    def __init__(self):
        self.value = {}
        self.requests = []
        self.chat = SimpleNamespace(completions=self)

    def create(self, **request):
        self.requests.append(json.loads(request['messages'][-1]['content']))
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(self.value, ensure_ascii=False)))])


class SemanticReminderContinuationTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store = MemoryStore(Path(self.folder.name) / 'test.db')
        self.now = datetime(2030, 9, 9, 2, tzinfo=timezone.utc)
        self.reminders = PersonalReminderService(self.store, clock=lambda: self.now)
        self.client = Client()
        self.compiler = IntentCompiler(self.client, 'test')
        self.service = SemanticTaskService(self.store, self.compiler, reminder_service=self.reminders,
                                           clock=lambda: self.now)

    def event(self, text, key, owner='owner'):
        return ChatEvent('web', 'group', key, 'conversation', owner, text, occurred_at=self.now)

    def start(self, text='明天提醒我预约陕西历史博物馆', key='start', owner='owner'):
        event = self.event(text, key, owner)
        self.reminders.handle(event, self.store.begin_event(event.event_key))

    def answer(self, text, value, key='reply'):
        self.client.value = value
        event = self.event(text, key)
        return self.service.handle(event, self.store.begin_event(event.event_key))

    def continuation(self, source, time_text='', *, disposition='answer', index=1):
        operation = {'domain':'reminder', 'operation':'continue_pending',
                     'task_index':index, 'disposition':disposition}
        if time_text:
            operation['trigger'] = {'text':time_text, 'source_text':source}
        return {'schema_version':1, 'action':'cancel' if disposition == 'cancel' else 'update',
                'operations':[operation], 'source_spans':[source]}

    def rows(self):
        return self.store.reminders.list_for_owner('web', 'conversation', 'owner')

    def test_llm_resumes_nonmatching_phrase_with_owned_slots_and_same_task(self):
        self.start()
        original = self.store.tasks.recent('web', 'web:conversation', 'owner')[0]
        text = '我希望安排在快到中午的十点整，方便出门前处理'
        self.assertFalse(is_time_answer(text))
        reply = self.answer(text, self.continuation('快到中午的十点整', '上午十点'))
        self.assertIn('2030-09-10 10:00', reply)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]['title'], '预约陕西历史博物馆')
        self.assertEqual(self.rows()[0]['task_id'], original.task_id)
        context = json.loads(self.client.requests[-1]['context'])['pending_reminders'][0]
        self.assertEqual(context['slots']['day'], '2030-09-10')
        self.assertIn('几点', context['last_question'])

    def test_ambiguous_clock_is_not_silently_assigned_am_then_semantic_period_resumes(self):
        self.start()
        reply = self.answer('10点吧', self.continuation('10点吧', '上午10点'), 'clock')
        self.assertIn('上午还是', reply)
        self.assertEqual(self.rows(), ())
        text = '我选白天的那个时段，不是夜里'
        self.assertFalse(is_time_answer(text))
        reply = self.answer(text, self.continuation(text, '上午'), 'period')
        self.assertIn('10:00', reply)
        self.assertEqual(len(self.rows()), 1)

    def test_new_reminder_without_trigger_keywords_reaches_compiler(self):
        text = '明天上午十点，买车票这件事到时候喊我一声'
        self.assertTrue(semantic_candidate(text))
        reply = self.answer(text, {'schema_version':1, 'action':'create', 'operations':[
            {'domain':'reminder', 'operation':'create', 'title':'买车票', 'trigger':{'text':'明天上午十点'}}],
            'source_spans':['买车票','明天上午十点']})
        self.assertIn('已设置', reply)
        self.assertEqual(len(self.client.requests), 1)
        self.assertEqual(self.rows()[0]['title'], '买车票')

    def test_missing_initial_time_is_persisted_as_reminder_not_generic_semantic_task(self):
        reply = self.answer('买车票这件事到时候喊我一声', {'schema_version':1, 'action':'clarify', 'operations':[
            {'domain':'reminder', 'operation':'create', 'title':'买车票'}], 'missing_fields':['time'], 'source_spans':['买车票']})
        self.assertIn('几点', reply)
        task = self.store.tasks.recent('web', 'web:conversation', 'owner')[0]
        self.assertEqual(task.task_type, 'reminder')
        self.assertEqual(task.slots['title'], '买车票')

    def test_initial_time_can_be_semantically_normalized_from_grounded_source(self):
        text = '明天，钟点选上午十点，买车票这件事到时候喊我一声'
        reply = self.answer(text, {'schema_version':1, 'action':'create', 'operations':[
            {'domain':'reminder','operation':'create','title':'买车票',
             'trigger':{'text':'明天上午十点','source_text':'明天，钟点选上午十点'}}],
            'source_spans':['买车票','明天，钟点选上午十点']})
        self.assertIn('2030-09-10 10:00', reply)
        self.assertEqual(len(self.rows()), 1)

    def test_topic_switch_does_not_consume_or_change_pending_reminder(self):
        self.start()
        before = self.store.tasks.recent('web', 'web:conversation', 'owner')[0]
        result = self.answer('先说说西安天气怎么样', {'schema_version':1, 'action':'read',
            'operations':[{'domain':'weather','operation':'query','location':'西安'}], 'source_spans':['西安']})
        self.assertIsNone(result)
        self.assertEqual(self.rows(), ())
        after = self.store.tasks.recent('web', 'web:conversation', 'owner')[0]
        self.assertEqual(after.version, before.version)

    def test_semantic_cancellation_only_ends_the_pending_setup(self):
        self.start()
        reply = self.answer('我改主意了，这个安排先作罢', self.continuation('这个安排先作罢', disposition='cancel'))
        self.assertIn('已结束', reply)
        self.assertEqual(self.rows(), ())
        self.assertFalse(self.service._pending_reminders(self.event('', 'inspect')))

    def test_ambiguous_multiple_tasks_cannot_be_selected_arbitrarily(self):
        self.start()
        self.start('明天提醒我买车票', key='second')
        reply = self.answer('就上午十点', self.continuation('上午十点','上午十点'))
        self.assertIn('多项', reply)
        self.assertEqual(self.rows(), ())

    def test_other_owner_and_expired_tasks_are_not_in_model_context(self):
        self.start()
        self.start('明天提醒我秘密事项', key='other', owner='different-owner')
        with self.store._connect() as db:
            db.execute("UPDATE travel_tasks SET expires_at='2000-01-01T00:00:00+00:00' WHERE owner_id='owner'")
        reply = self.answer('就上午十点', self.continuation('上午十点','上午十点'))
        self.assertIn('没有可接续', reply)
        self.assertNotIn('秘密事项', str(self.client.requests))
        self.assertEqual(self.rows(), ())

    def test_unavailable_model_preserves_pending_task_and_does_not_claim_no_tools(self):
        self.start()
        self.compiler.try_compile = Mock(return_value=None)
        reply = self.answer('在我出门之前吧', {})
        self.assertIn('未能可靠解析', reply)
        self.assertNotIn('没有可用', reply)
        self.assertTrue(self.service._pending_reminders(self.event('', 'inspect')))
        self.assertEqual(self.rows(), ())

    def test_ungrounded_time_or_forged_identity_never_creates(self):
        self.start()
        ir = self.continuation('上午十点', '上午十点')
        ir['source_spans'] = ['出门之前']
        reply = self.answer('出门之前', ir)
        self.assertIn('缺少', reply)
        ir['operations'][0]['owner_id'] = 'forged'
        self.answer('出门之前', ir, 'forged')
        self.assertEqual(self.rows(), ())

    def test_changing_only_date_keeps_unresolved_am_pm(self):
        self.start()
        self.answer('10点', self.continuation('10点','10点'), 'clock')
        reply = self.answer('把日子挪到后天，钟点先不变', self.continuation('后天','后天'), 'date')
        self.assertIn('上午还是', reply)
        self.assertEqual(self.rows(), ())

    def test_onebot_owner_reply_reaches_semantic_layer_without_time_regex(self):
        import asyncio
        self.start()
        policy = GroupTriggerPolicy(self.store)
        self.assertTrue(asyncio.run(policy.should_handle(self.event('按白天那个时段来安排', 'answer'))))
        self.assertFalse(asyncio.run(policy.should_handle(self.event('按白天那个时段来安排', 'other', 'different-owner'))))

    def test_inconsistent_action_cannot_turn_cancel_or_read_into_creation(self):
        self.start()
        for action in ('cancel', 'read'):
            ir = self.continuation('上午十点', '上午十点')
            ir['action'] = action
            self.assertIn('不一致', self.answer('就上午十点', ir, action))
        self.assertEqual(self.rows(), ())

    def test_clarification_does_not_commit_even_if_model_supplies_complete_time(self):
        self.start()
        ir = self.continuation('吃完饭之后', '上午十点', disposition='clarify')
        ir['action'] = 'clarify'
        reply = self.answer('吃完饭之后', ir)
        self.assertIn('还不够明确', reply)
        self.assertEqual(self.rows(), ())
        self.assertEqual(self.service._pending_reminders(self.event('', 'inspect'))[0].slots['clock'], '')

    def test_task_change_during_model_call_rejects_stale_resolution(self):
        self.start()
        original = self.client.create
        def concurrent_change(**request):
            response = original(**request)
            with self.store._connect() as db:
                db.execute("UPDATE travel_tasks SET version=version+1 WHERE owner_id='owner'")
            return response
        self.client.create = concurrent_change
        self.assertIn('已变化', self.answer('就上午十点', self.continuation('上午十点','上午十点')))
        self.assertEqual(self.rows(), ())
