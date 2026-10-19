import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agents.context_builder import ContextBuilder, render_untrusted_context
from core.chat_transport import ChatEvent
from infrastructure.memory_store import MemoryStore
from services.conversation_summary_service import ConversationSummaryService


class FakeClient:
    def __init__(self):
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls.append(kwargs)
        value = {'items': [{'kind': 'decision', 'text': '用户明确否定夜游',
                           'sources': [{'message_id': 1, 'quote': '不安排夜游'}]}]}
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=json.dumps(value, ensure_ascii=False)))])


class ConversationSummaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = MemoryStore(Path(self.temp.name) / 'summary.db')
        self.client = FakeClient()
        self.service = ConversationSummaryService(self.store.conversation_summaries, self.client, 'model')
        self.event = ChatEvent('web', 'group', 'current', 'conversation', 'owner', '继续规划')
        for index in range(8):
            self.store.save_turn('web:conversation', 'owner', f'event-{index}',
                                 '不安排夜游；用户讨论' + '甲' * 600, '助手回复' + '乙' * 600)

    def test_summary_is_disabled_by_default(self):
        self.assertIsNone(self.service.ensure(self.event))
        self.assertEqual(self.client.calls, [])

    def test_enabled_summary_is_versioned_and_injected_as_derived_context(self):
        with patch.dict(os.environ, {'AI_SUMMARY_ENABLED': 'true'}):
            record = self.service.ensure(self.event)
            context = ContextBuilder(self.store, self.service).build(self.event)

        self.assertEqual(record['version'], 1)
        self.assertEqual(len(self.client.calls), 1)
        rendered = render_untrusted_context(context)
        self.assertIn('派生参考', rendered)
        self.assertIn('否定夜游', rendered)

    def test_explicit_preference_change_invalidates_derived_summary(self):
        with patch.dict(os.environ, {'AI_SUMMARY_ENABLED': 'true'}):
            self.assertIsNotNone(self.service.ensure(self.event))
            self.store.preferences.update(self.event, 0, {'pace': '轻松'})

        self.assertIsNone(self.store.conversation_summaries.latest(self.event))


if __name__ == '__main__':
    unittest.main()
