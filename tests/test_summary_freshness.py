import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agents.context_builder import ContextBuilder, render_untrusted_context
from core.chat_transport import ChatEvent
from infrastructure.memory_store import MemoryStore
from scripts.evaluate_summary_quality import OFFLINE_ENV, ScriptedClient, load_cases, seed_stage
from services.conversation_summary_service import ConversationSummaryService


class SummaryFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.env = patch.dict('os.environ', OFFLINE_ENV)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.store = MemoryStore(Path(self.folder.name) / 'test.db')
        self.event = ChatEvent('onebot', 'group', 'recall', 'scope', 'owner', '最新费用和结束时间是什么？')
        corpus = load_cases(Path(__file__).resolve().parents[1] / 'evals/summary_quality_cases_v2.json')
        self.stages = next(case['stages'] for case in corpus['cases'] if case['id'] == 'Q02')
        self.mapping = {}
        sdk = ScriptedClient()
        sdk.items = seed_stage(self.store, self.event, self.stages[0], corpus, self.mapping)
        self.record = ConversationSummaryService(self.store.conversation_summaries, sdk, 'offline').ensure(self.event)
        self.assertIsNotNone(self.record)
        self.builder = ContextBuilder(self.store, SimpleNamespace(ensure=lambda *a, **k: self.record))

    def age(self):
        for i in range(20):
            self.store.save_turn(self.event.storage_scope_id, self.event.sender_id, f'neutral-{i}', '中性讨论', '中性回答')
            self.store.save_chat_message(f'neutral-chat-{i}', 'onebot', 'scope', 'owner',
                                         f'neutral-{i}', '', 'user', '中性讨论')

    def test_uncovered_correction_survives_recent_history_exit(self):
        self.store.save_turn(self.event.storage_scope_id, self.event.sender_id, 'correction',
            '费用改为每间每晚320元人民币，最晚19:00结束，轮椅决定不借用。', '收到')
        self.age()
        context = self.builder.build(self.event)
        rendered = render_untrusted_context(context)
        self.assertIn('320', rendered)
        self.assertIn('19:00', rendered)
        self.assertIn('决定不借用', rendered)
        self.assertLessEqual(context.total_chars, 7000)

    def test_oversized_uncovered_material_is_explicitly_incomplete(self):
        self.store.save_turn(self.event.storage_scope_id, self.event.sender_id, 'long', '尚未覆盖的新资料。' * 1000, '收到')
        self.age()
        context = self.builder.build(self.event)
        self.assertTrue(context.summary_information_incomplete)
        self.assertEqual(context.conversation_summary, '')
        from agents.travel_agent import TravelAgent
        from core.settings import Settings
        client=unittest.mock.Mock()
        agent=TravelAgent(Settings('','',frozenset(),'','fixture','https://example.invalid','fixture'),lambda *a:None,client)
        result=agent.run(self.event.content,context)
        self.assertEqual(result.status,'needs_input')
        client.chat.completions.create.assert_not_called()

    def test_successful_update_removes_the_supplement(self):
        context = self.builder.build(self.event)
        self.assertEqual(context.summary_pending_context, '')
        self.assertFalse(context.summary_information_incomplete)

    def test_excluded_material_does_not_reappear_as_pending_context(self):
        self.store.save_turn(self.event.storage_scope_id, self.event.sender_id, 'secret', 'API KEY secret-should-not-appear', '收到')
        self.age()
        self.assertNotIn('secret-should-not-appear', self.builder.build(self.event).summary_pending_context)
