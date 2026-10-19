import os
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from core.model_pricing import estimate_cost, parse_profiles, pricing_config
from infrastructure.memory_store import MemoryStore
from infrastructure.model_gateway import ModelGateway, usage_report


class PricingTests(unittest.TestCase):
    def test_non_finite_rates_and_duplicate_models_are_rejected(self):
        for raw in ('[{"model":"m","currency":"USD","input":"NaN"}]',
                    '[{"model":"m","currency":"USD"},{"model":"m","currency":"USD"}]'):
            with self.assertRaises(ValueError):
                parse_profiles(raw)

    def test_override_without_matching_price_is_unknown(self):
        with patch.dict(os.environ, {'LLM_INPUT_PRICE_PER_M':'1','LLM_OUTPUT_PRICE_PER_M':'2'}):
            self.assertIsNone(pricing_config('small','default')['input'])

    def test_model_and_provider_select_exact_price(self):
        profiles = [{'provider':'p','model':'small','input':.1,'cached':0,'output':.2,'currency':'EUR'}]
        with patch.dict(os.environ, {'LLM_PROVIDER_ALIAS':'p','LLM_MODEL_PRICES':json.dumps(profiles)}):
            self.assertEqual(pricing_config('small','default')['currency'],'EUR')
            self.assertEqual(estimate_cost(100,100,0,pricing_config('small','default')),0)

    def test_price_change_during_request_does_not_change_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            store = MemoryStore(Path(folder)/'prices.db')
            def create(**kwargs):
                os.environ['LLM_INPUT_PRICE_PER_M'] = '100'
                return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=100,completion_tokens=0))
            gateway = ModelGateway(store,SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
            with patch.dict(os.environ, {'LLM_INPUT_PRICE_PER_M':'1','LLM_OUTPUT_PRICE_PER_M':'2'}):
                gateway.create(model='m',messages=[])
            with store._connect() as db:
                row=db.execute('SELECT estimated_cost,input_price_per_m FROM model_calls').fetchone()
            self.assertEqual(row['input_price_per_m'],1)
            self.assertEqual(row['estimated_cost'],.0001)

    def test_invalid_cache_usage_cannot_produce_exact_cost(self):
        self.assertIsNone(estimate_cost(100,101,0,{'input':1,'cached':0,'output':2}))

    def test_historical_zero_cache_correction_is_read_only(self):
        with tempfile.TemporaryDirectory() as folder:
            store = MemoryStore(Path(folder)/'prices.db')
            usage = SimpleNamespace(prompt_tokens=100,completion_tokens=0,prompt_tokens_details=SimpleNamespace(cached_tokens=100))
            gateway = ModelGateway(store,SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw:SimpleNamespace(usage=usage)))))
            with patch.dict(os.environ,{'LLM_INPUT_PRICE_PER_M':'1','LLM_OUTPUT_PRICE_PER_M':'2','LLM_CACHED_INPUT_PRICE_PER_M':'0'}):
                gateway.create(model='m',messages=[])
                with store._connect() as db:
                    db.execute('UPDATE model_calls SET estimated_cost=.0001,cost_estimation_version=1')
                report = usage_report(store)
            item = report['historical_corrections']['items'][0]
            self.assertEqual((item['original'],item['corrected'],item['delta']),(.0001,0,-.0001))
            self.assertFalse(report['historical_corrections']['applied'])
            with store._connect() as db:
                self.assertEqual(db.execute('SELECT estimated_cost FROM model_calls').fetchone()[0],.0001)
