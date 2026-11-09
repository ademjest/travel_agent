import os
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from core.model_pricing import estimate_cost, parse_profiles, pricing_config
from core.model_config import parse_config
from core.model_budget import model_call_context, MODEL_EVENT
from infrastructure.memory_store import MemoryStore
from infrastructure.model_gateway import ModelGateway, usage_report


class PricingTests(unittest.TestCase):
    def test_current_rates_supplement_exact_historical_route_without_writes(self):
        from test_model_config import configuration
        config=configuration()
        config['profiles'][0]['pricing']={'currency':'USD'}
        config['roles']={'regular':'first-model','complex':'first-model'}
        with tempfile.TemporaryDirectory() as folder:
            store=MemoryStore(Path(folder)/'prices.db')
            usages=iter([(3479,338),(3497,472)])
            def create(**kw):
                prompt,completion=next(usages)
                return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=prompt,completion_tokens=completion,
                    prompt_tokens_details=SimpleNamespace(cached_tokens=0)))
            client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
            gateway=ModelGateway(store,configuration=parse_config(config),client_factory=lambda **kw:client)
            token=MODEL_EVENT.set('web:group:pricing:new')
            try:
                for purpose in ('trip_plan','trip_plan_compare'):
                    with model_call_context(purpose):gateway.create(messages=[])
            finally:MODEL_EVENT.reset(token)
            with store._connect() as db:
                before=[tuple(r) for r in db.execute('SELECT * FROM model_calls')]
                ledger=[tuple(r) for r in db.execute('SELECT * FROM model_usage_ledger')]
            config['profiles'][0]['pricing']={'input':2,'cached':None,'output':10,'currency':'USD'}
            with patch.dict(os.environ,{'LLM_CONFIG_JSON':json.dumps(config)}):
                report=usage_report(store)
                from infrastructure.model_gateway import conversation_usage_report
                conversation=conversation_usage_report(store,'pricing','owner')
            self.assertEqual(report['totals']['cost_supplemented'],2)
            self.assertEqual(conversation['totals'],{**report['totals'],'total_tokens':7786,'running_calls':0})
            amounts=[c['cost'] for c in conversation['rounds'][0]['calls']]
            self.assertEqual(amounts,[.010338,.011714])
            self.assertTrue(all(c['pricing_version'] for c in conversation['rounds'][0]['calls']))
            with store._connect() as db:
                self.assertEqual(before,[tuple(r) for r in db.execute('SELECT * FROM model_calls')])
                self.assertEqual(ledger,[tuple(r) for r in db.execute('SELECT * FROM model_usage_ledger')])
            for change in ('base_url','model_id','profile_id'):
                from copy import deepcopy
                changed=deepcopy(config)
                if change=='base_url':changed['connections'][0]['base_url']='https://moved.example/v1'
                elif change=='model_id':changed['profiles'][0]['model_id']='different-model'
                else:
                    changed['profiles'][0]['id']='new-profile'
                    changed['roles']={'regular':'new-profile','complex':'new-profile'}
                with patch.dict(os.environ,{'LLM_CONFIG_JSON':json.dumps(changed)}):
                    unmatched=usage_report(store)['totals']
                self.assertEqual(unmatched['cost_unknown'],2,change)
                self.assertEqual(unmatched['unknown_identity'],2,change)

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
