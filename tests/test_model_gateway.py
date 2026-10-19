from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import tempfile
from threading import Event, Lock
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from core.execution_scope import CURRENT_EXECUTION, ExecutionScope, ExecutionRevoked
from core.model_budget import MODEL_EVENT, ModelBudgetExceeded, model_call_context
from infrastructure.memory_store import MemoryStore
from infrastructure.model_gateway import ModelGateway, estimate_input, usage_report, usage_summary


class ModelGatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = MemoryStore(Path(self.temp.name) / 'models.db')
        self.requests = []
        self.result = SimpleNamespace(choices=[], usage=SimpleNamespace(prompt_tokens=20, completion_tokens=10))
        def create(**kwargs):
            self.requests.append(kwargs)
            return self.result
        self.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        self.gateway = ModelGateway(self.store, self.client)

    def test_records_actual_usage_and_limits_output(self):
        token = MODEL_EVENT.set('test-event')
        try:
            self.gateway.create(model='fake', messages=[{'role': 'user', 'content': 'hello'}], max_completion_tokens=20000)
        finally:
            MODEL_EVENT.reset(token)
        self.assertEqual(self.requests[0]['max_completion_tokens'], 8192)
        with self.store._connect() as connection:
            row = connection.execute('SELECT * FROM model_calls').fetchone()
        self.assertEqual((row['prompt_tokens'], row['completion_tokens']), (20, 10))
        self.assertEqual(row['event_key'], 'test-event')
        self.assertNotIn('hello', str(dict(row)))

    def test_image_payload_is_estimated_as_image_not_base64_text_tokens(self):
        value = [{'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + 'A'*100000}}]
        self.assertLess(estimate_input(value), 5000)

    def test_records_purpose_cached_and_reasoning_usage(self):
        self.result.usage = SimpleNamespace(
            prompt_tokens=100,
            completion_tokens=20,
            prompt_tokens_details=SimpleNamespace(cached_tokens=40),
            completion_tokens_details=SimpleNamespace(reasoning_tokens=7),
        )

        with model_call_context('intent_parse', prompt_version='intent-v2',
                                context_policy_version='context-v2'):
            self.gateway.create(model='fake', messages=[{'role': 'user', 'content': 'hello'}])

        with self.store._connect() as connection:
            row = connection.execute('SELECT * FROM model_calls').fetchone()
        self.assertEqual(row['purpose'], 'intent_parse')
        self.assertEqual(row['prompt_version'], 'intent-v2')
        self.assertEqual(row['context_policy_version'], 'context-v2')
        self.assertEqual(row['cached_input_tokens'], 40)
        self.assertEqual(row['reasoning_tokens'], 7)
        self.assertEqual(row['output_limit'], 1024)
        self.assertEqual(usage_summary(self.store)['cached_input_tokens'], 40)

    def test_purpose_model_override_and_daily_budget(self):
        with patch.dict('os.environ', {'LLM_INTENT_MODEL_ID': 'small-intent',
                                       'AI_DAILY_TOKEN_LIMIT': '1500'}):
            with model_call_context('intent_parse'):
                self.gateway.create(model='default', messages=[{'role': 'user', 'content': 'hello'}])
            self.assertEqual(self.requests[0]['model'], 'small-intent')
            with model_call_context('intent_parse'):
                self.gateway.create(model='default', messages=[{'role': 'user', 'content': 'hello'}])
            # Known actual usage releases the unused reservation.
            with patch.dict('os.environ', {'AI_DAILY_TOKEN_LIMIT':'500'}):
                with model_call_context('intent_parse'):
                    with self.assertRaises(ModelBudgetExceeded):
                        self.gateway.create(model='default', messages=[{'role':'user','content':'hello'}])

    def test_configured_prices_estimate_cost_without_hiding_unknown_usage(self):
        self.result.usage = SimpleNamespace(
            prompt_tokens=100, completion_tokens=20,
            prompt_tokens_details=SimpleNamespace(cached_tokens=40),
            completion_tokens_details=SimpleNamespace(reasoning_tokens=0))
        with patch.dict('os.environ', {'LLM_INPUT_PRICE_PER_M': '1',
                                       'LLM_CACHED_INPUT_PRICE_PER_M': '.5',
                                       'LLM_OUTPUT_PRICE_PER_M': '2',
                                       'LLM_PRICE_CURRENCY': 'CNY'}):
            self.gateway.create(model='fake', messages=[])
        with self.store._connect() as connection:
            row = connection.execute('''SELECT estimated_cost,cost_currency,input_price_per_m,
                cached_input_price_per_m,output_price_per_m,cost_origin FROM model_calls''').fetchone()
        self.assertAlmostEqual(row['estimated_cost'], .00012)
        self.assertEqual(row['cost_currency'], 'CNY')
        self.assertEqual((row['input_price_per_m'],row['cached_input_price_per_m'],row['output_price_per_m']),
                         (1,.5,2))
        self.assertEqual(row['cost_origin'],'live')

    def test_report_recalculates_unpriced_history_with_current_rates(self):
        with patch.dict('os.environ', {'LLM_INPUT_PRICE_PER_M': '', 'LLM_OUTPUT_PRICE_PER_M': ''}):
            self.gateway.create(model='fake', messages=[])
        with patch.dict('os.environ', {'LLM_INPUT_PRICE_PER_M': '2',
                                       'LLM_CACHED_INPUT_PRICE_PER_M': '.2',
                                       'LLM_OUTPUT_PRICE_PER_M': '10',
                                       'LLM_PRICE_CURRENCY': 'USD'}):
            report = usage_report(self.store)
        self.assertEqual(report['totals']['cost_unknown'],0)
        self.assertEqual(report['totals']['cost_recalculated'],1)
        self.assertEqual(report['totals']['cache_detail_missing'],1)
        self.assertAlmostEqual(report['totals']['estimated_cost'],.00014)
        self.assertEqual(report['totals']['currency'],'USD')
        self.assertIn('按当前生效费率回算',report['cost_note'])

    def test_truly_missing_usage_remains_unknown(self):
        self.result.usage = None
        self.gateway.create(model='fake', messages=[])
        with patch.dict('os.environ', {'LLM_INPUT_PRICE_PER_M': '2','LLM_OUTPUT_PRICE_PER_M':'10'}):
            report=usage_report(self.store)
        self.assertEqual(report['totals']['cost_unknown'],1)
        self.assertEqual(report['totals']['unknown_usage'],1)
        self.assertEqual(report['totals']['cost_recalculated'],0)

    def test_default_currency_is_usd_and_persisted_cost_is_not_repriced(self):
        with patch.dict('os.environ', {'LLM_INPUT_PRICE_PER_M':'1','LLM_OUTPUT_PRICE_PER_M':'2'}, clear=False):
            os.environ.pop('LLM_PRICE_CURRENCY',None)
            self.gateway.create(model='fake',messages=[])
        with self.store._connect() as connection:
            row=connection.execute('SELECT estimated_cost,cost_currency FROM model_calls').fetchone()
        self.assertEqual(row['cost_currency'],'USD')
        original=row['estimated_cost']
        with patch.dict('os.environ', {'LLM_INPUT_PRICE_PER_M':'100','LLM_OUTPUT_PRICE_PER_M':'200',
                                       'LLM_PRICE_CURRENCY':'USD'}):
            report=usage_report(self.store)
        self.assertAlmostEqual(report['totals']['estimated_cost'],original)
        self.assertEqual(report['totals']['cost_recalculated'],0)

    def test_budget_stops_before_extra_model_call(self):
        token = MODEL_EVENT.set('bounded-event')
        try:
            for _ in range(12):
                self.gateway.create(model='fake', messages=[{'role': 'user', 'content': 'hello'}])
            with self.assertRaises(ModelBudgetExceeded):
                self.gateway.create(model='fake', messages=[{'role': 'user', 'content': 'hello'}])
        finally:
            MODEL_EVENT.reset(token)
        self.assertEqual(len(self.requests), 12)
        with self.assertRaises(ModelBudgetExceeded):
            self.gateway.create(model='fake', messages=[{'role': 'user', 'content': '很长'*40000}])

    def test_physical_model_concurrency_is_at_most_two(self):
        release = Event()
        two_started = Event()
        lock = Lock()
        running, peak = 0, 0
        def create(**kwargs):
            nonlocal running, peak
            with lock:
                running += 1
                peak = max(peak, running)
                if running == 2:
                    two_started.set()
            release.wait(3)
            with lock:
                running -= 1
            return self.result
        self.client.chat.completions.create = create
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(self.gateway.create, model='fake', messages=[{'role': 'user', 'content': 'hello'}]) for _ in range(4)]
            self.assertTrue(two_started.wait(2))
            release.set()
            for future in futures:
                future.result(timeout=3)
        self.assertEqual(peak, 2)

    def test_revoked_job_cannot_start_another_model_call(self):
        self.store.inbox.submit('onebot:group:g:e', 'onebot', 'g', 'u', {})
        job = self.store.inbox.claim()
        self.store.inbox.cancel(job['event_key'], 'onebot', 'g', 'u')
        token = CURRENT_EXECUTION.set(ExecutionScope(str(self.store.database_path), job['id'], job['claim_token']))
        try:
            with self.assertRaises(ExecutionRevoked):
                self.gateway.create(model='fake', messages=[])
        finally:
            CURRENT_EXECUTION.reset(token)
        self.assertEqual(self.requests, [])
