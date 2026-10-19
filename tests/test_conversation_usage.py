from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from core.model_budget import MODEL_EVENT, model_call_context
from infrastructure.memory_store import MemoryStore
from infrastructure.model_gateway import ModelGateway, conversation_usage_report, usage_summary
from infrastructure.web_repository import OWNER, WebRepository


class ConversationUsageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = MemoryStore(Path(temporary.name)/'usage.db')
        self.repo = WebRepository(self.store)
        self.left = self.repo.create_conversation()['id']
        self.right = self.repo.create_conversation()['id']
        self.environment = patch.dict('os.environ', {
            'LLM_MODEL_ID':'test-model','LLM_MODEL_PRICES':'[]','LLM_PROVIDER_ALIAS':'',
            'LLM_INPUT_PRICE_PER_M':'2','LLM_CACHED_INPUT_PRICE_PER_M':'.2',
            'LLM_OUTPUT_PRICE_PER_M':'10','LLM_PRICE_CURRENCY':'USD',
            'AI_DAILY_TOKEN_LIMIT':'0','AI_MONTHLY_TOKEN_LIMIT':'0',
            'AI_DAILY_COST_LIMIT':'0','AI_MONTHLY_COST_LIMIT':'0'})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.result = SimpleNamespace(choices=[], usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20,
            prompt_tokens_details=SimpleNamespace(cached_tokens=40),
            completion_tokens_details=SimpleNamespace(reasoning_tokens=5)))
        client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs:self.result)))
        self.gateway=ModelGateway(self.store,client)

    def call(self, event_key, purpose='intent_parse'):
        token=MODEL_EVENT.set(event_key)
        try:
            with model_call_context(purpose):
                self.gateway.create(model='test-model',messages=[])
        finally:
            MODEL_EVENT.reset(token)

    def report(self, identity=None):
        return conversation_usage_report(self.store,identity or self.left,OWNER)

    def test_scopes_exclude_other_conversations_qq_and_service_tests(self):
        self.call(f'web:group:{self.left}:same-request')
        self.call(f'web:group:{self.left}:same-request','conversation_summary')
        for event in (f'web:group:{self.right}:same-request', f'onebot:group:{self.left}:same-request',
                      'direct-model:probe', f'web:group:{self.left}extra:request'):
            self.call(event)
        report=self.report()
        self.assertEqual(report['conversation_id'],self.left)
        self.assertEqual(report['totals']['calls'],2)
        self.assertEqual(report['totals']['total_tokens'],240)
        self.assertEqual(report['totals']['cached_input_tokens'],80)
        self.assertEqual(report['totals']['reasoning_tokens'],10)
        self.assertAlmostEqual(report['totals']['estimated_cost'],.000656)
        self.assertEqual({row['purpose'] for row in report['purposes']},{'intent_parse','conversation_summary'})
        self.assertEqual(self.report(self.right)['totals']['calls'],1)
        self.assertEqual(usage_summary(self.store)['calls'],6)

    def test_repeated_reads_do_not_accumulate_and_new_calls_refresh_stats(self):
        self.assertEqual(self.report()['totals']['total_tokens'],0)
        self.call(f'web:group:{self.left}:first')
        first=self.report()
        self.assertEqual(first,self.report())
        self.call(f'web:group:{self.left}:second')
        self.assertEqual(self.report()['totals']['total_tokens'],240)
        self.assertEqual(self.repo.context(self.left)['model_usage']['totals']['calls'],2)

    def test_background_job_attribution_and_missing_usage_remain_visible(self):
        job=self.store.inbox.submit('scheduled-query:linked','web',self.left,OWNER,{})
        self.call('scheduled-query:linked')
        with self.store._connect() as db:
            db.execute('UPDATE model_calls SET job_id=? WHERE event_key=?',(job['id'],'scheduled-query:linked'))
        self.result.usage=None
        self.call(f'web:group:{self.left}:missing-usage')
        totals=self.report()['totals']
        self.assertEqual(totals['calls'],2)
        self.assertEqual(totals['total_tokens'],120)
        self.assertEqual(totals['unknown_usage'],1)
        self.assertEqual(totals['cost_unknown'],1)

    def test_inflight_calls_are_counted_without_invented_token_usage(self):
        self.gateway._reserve(f'web:group:{self.left}:running','test-model',100,20)
        totals=self.report()['totals']
        self.assertEqual(totals['calls'],1)
        self.assertEqual(totals['running_calls'],1)
        self.assertEqual(totals['total_tokens'],0)
        self.assertEqual(totals['unknown_usage'],1)

    def test_failed_attempts_remain_in_conversation_call_count(self):
        def fail(**kwargs):
            raise RuntimeError('simulated timeout')
        self.gateway.client.chat.completions.create=fail
        with self.assertRaises(RuntimeError):
            self.call(f'web:group:{self.left}:failed-attempt')
        totals=self.report()['totals']
        self.assertEqual(totals['calls'],1)
        self.assertEqual(totals['total_tokens'],0)
        self.assertEqual(totals['running_calls'],0)
        self.assertEqual(totals['cost_unknown'],1)

    def test_empty_conversation_without_model_configuration_reports_zero(self):
        with tempfile.TemporaryDirectory() as folder:
            store=MemoryStore(Path(folder)/'empty.db')
            repo=WebRepository(store)
            identity=repo.create_conversation()['id']
            self.assertEqual(repo.context(identity)['model_usage']['totals']['calls'],0)
