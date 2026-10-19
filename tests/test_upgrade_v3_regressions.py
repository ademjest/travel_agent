import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from core.chat_transport import ChatEvent
from core.intent_ir import IntentIR, IntentOperation
from infrastructure.memory_store import MemoryStore
from infrastructure.model_gateway import ModelGateway
from services.conversation_summary_service import ConversationSummaryService
from services.semantic_task_service import SemanticTaskService


class V3Regressions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = MemoryStore(Path(self.temp.name) / 'audit.db')
        self.event = ChatEvent('web', 'group', 'audit', 'conv', 'owner', '天气如何')

    def test_missing_query_location_persists_task(self):
        ir = IntentIR(1, 'clarify', (IntentOperation('weather', 'query', {}),),
                      ('location',), False, ('天气',), .9)
        compiler = SimpleNamespace(try_compile=lambda *args, **kwargs: ir)
        service = SemanticTaskService(self.store, compiler,
            travel_service=SimpleNamespace(execute_tool=lambda *args: 'ok'))
        reply = service.handle(self.event, self.store.begin_event(self.event.event_key))
        self.assertIn('地点', reply)
        with self.store._connect() as db:
            row = db.execute('SELECT status,missing_slots_json FROM travel_tasks').fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row['status'], 'collecting')

    def test_stale_summary_can_be_rebuilt(self):
        for index in range(8):
            self.store.save_turn(self.event.storage_scope_id, 'owner', f'e{index}', 'x'*700, 'y'*700)
        def generate(**kwargs):
            # Keep this invalidation regression on the current wire contract;
            # legacy persisted records are tested separately from model output.
            value = {'items': [{'kind': 'reference', 'text': 'x', 'sources': [{'message_id': 1, 'quote': 'x'}]}]}
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(value)))])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=generate)))
        service = ConversationSummaryService(self.store.conversation_summaries, client, 'fake')
        with patch.dict(os.environ, {'AI_SUMMARY_ENABLED': 'true'}):
            first = service.ensure(self.event)
            self.assertIsNotNone(first)
            self.store.preferences.update(self.event, 0, {'pace': '轻松'})
            rebuilt = service.ensure(self.event)
        self.assertIsNotNone(rebuilt)
        self.assertGreater(rebuilt['version'], first['version'])

    def test_zero_cached_price_is_zero_cost(self):
        usage = SimpleNamespace(prompt_tokens=100, completion_tokens=0,
            prompt_tokens_details=SimpleNamespace(cached_tokens=100))
        result = SimpleNamespace(usage=usage)
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs: result)))
        gateway = ModelGateway(self.store, client)
        with patch.dict(os.environ, {'LLM_INPUT_PRICE_PER_M': '1',
                                    'LLM_CACHED_INPUT_PRICE_PER_M': '0', 'LLM_OUTPUT_PRICE_PER_M': '2'}):
            gateway.create(model='fake', messages=[])
        with self.store._connect() as db:
            cost = db.execute('SELECT estimated_cost FROM model_calls').fetchone()[0]
        self.assertEqual(cost, 0)
