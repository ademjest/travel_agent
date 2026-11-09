import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
import threading

import httpx

from core.model_config import ModelConfigError,parse_config,legacy_config
from core.model_budget import MODEL_EVENT,ModelCallContext,model_call_context,ModelBudgetExceeded
from infrastructure.memory_store import MemoryStore
from infrastructure.model_gateway import ModelGateway,usage_report
from infrastructure.model_client import create_model_client


def configuration():
    return {'schema_version':1,'connections':[
        {'id':name,'name':name,'base_url':f'https://{name}.example/v1','api_key':name+'-secret'} for name in ('first','second','third')],
        'profiles':[{'id':name+'-model','name':name,'connection_id':name,'model_id':'same-model',
            'capabilities':['text','json','tools','vision'],'token_parameter':'max_tokens' if name=='second' else 'max_completion_tokens',
            'temperature':'omit' if name=='second' else 'allow',
            'pricing':{'input':i+1,'cached':0,'output':i+2,'currency':'CNY' if name=='third' else 'USD'}} for i,name in enumerate(('first','second','third'))],
        'roles':{'regular':'first-model','basic':'second-model','complex':'third-model','vision':'third-model'}}


class ModelConfigTests(unittest.TestCase):
    def test_config_is_secret_free_in_repr_projection_and_version(self):
        config=parse_config(configuration())
        for secret in ('first-secret','second-secret','third-secret'):
            self.assertNotIn(secret,repr(config));self.assertNotIn(secret,json.dumps(config.as_dict()))
        changed=configuration();changed['connections'][0]['api_key']='rotated'
        self.assertEqual(config.version,parse_config(changed).version)

    def test_legacy_keeps_every_purpose_override_without_writing_or_merging(self):
        config=legacy_config({'LLM_MODEL_ID':'main','LLM_API_KEY':'key','LLM_BASE_URL':'https://old.example/v1',
            'LLM_INTENT_MODEL_ID':'intent','LLM_SUMMARY_MODEL_ID':'summary','LLM_RESEARCH_MODEL_ID':'research','LLM_VISION_MODEL_ID':'image'})
        for purpose,model in [('intent_parse','intent'),('conversation_summary','summary'),('research_plan','research'),('travel_answer_write','research'),('image_extract','image'),('trip_plan','main')]:
            connection,profile=config.select(purpose)
            self.assertEqual(profile.model_id,model);self.assertEqual(connection.id,'legacy')
        self.assertEqual(config.select('intent_parse',force_default=True)[1].model_id,'main')

    def test_invalid_config_and_dangling_references_never_fall_back(self):
        for modify in (lambda c:c['connections'].pop(0),lambda c:c['profiles'].pop(0),
                       lambda c:c['roles'].update(regular='missing'),lambda c:c.update(schema_version=2),
                       lambda c:c['connections'][0].update(protocol='other')):
            value=configuration();modify(value)
            with self.assertRaises(ModelConfigError):parse_config(value)
        with self.assertRaises(ModelConfigError):parse_config('{broken')
        self.assertFalse(parse_config('').configured)

    def test_invalid_nested_values_disable_models_without_breaking_other_settings(self):
        from core.settings import Settings
        for patch_value in ({'kind':{}},{'token_parameter':[]},{'temperature':False}):
            value=configuration();value['profiles'][0].update(patch_value)
            settings=Settings('','',frozenset(),'map','old-key','https://old.example/v1','old',model_config_json=json.dumps(value))
            self.assertFalse(settings.llm_configured)
        value=configuration();value['connections'][0]['base_url']='https://['
        with self.assertRaises(ModelConfigError):parse_config(value)

    def test_vision_requires_its_own_capability_and_basic_complex_inherit_regular(self):
        value=configuration();value['roles']={'regular':'first-model'}
        config=parse_config(value)
        self.assertEqual(config.select('intent_parse')[1].id,'first-model')
        with self.assertRaises(ModelConfigError):config.select('image_extract')
        value['roles']['vision']='first-model';value['profiles'][0]['capabilities']=['text']
        with self.assertRaises(ModelConfigError):parse_config(value)


class MultiProviderGatewayTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.store=MemoryStore(Path(temp.name)/'models.db');self.requests=[]
        def respond(request):
            self.requests.append(request)
            return httpx.Response(200,json={'id':'fake','object':'chat.completion','created':0,'model':'same-model',
                'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':'OK'}}],
                'usage':{'prompt_tokens':20,'completion_tokens':10,'total_tokens':30}})
        self.transport=httpx.MockTransport(respond)
        patcher=patch('infrastructure.model_client.DefaultHttpxClient',side_effect=lambda **kw:httpx.Client(transport=self.transport))
        patcher.start();self.addCleanup(patcher.stop)
        self.gateway=ModelGateway(self.store,configuration=parse_config(configuration()),client_factory=create_model_client)
        self.addCleanup(self.gateway.close)

    def test_three_endpoints_keys_prices_and_capabilities_do_not_mix(self):
        for purpose in ('tool_reasoning','intent_parse','trip_plan'):
            with model_call_context(purpose):
                self.gateway.create(model='ignored',messages=[{'role':'user','content':'OK'}],response_format={'type':'json_object'},temperature=0.2)
        self.assertEqual([r.url.host for r in self.requests],['first.example','second.example','third.example'])
        self.assertEqual([r.headers['authorization'] for r in self.requests],['Bearer '+name+'-secret' for name in ('first','second','third')])
        self.assertIn('max_tokens',json.loads(self.requests[1].content));self.assertNotIn('max_completion_tokens',json.loads(self.requests[1].content))
        self.assertNotIn('temperature',json.loads(self.requests[1].content))
        self.assertEqual(json.loads(self.requests[0].content)['temperature'],0.2)
        rows=usage_report(self.store)['purposes']
        self.assertEqual({r['connection_id'] for r in rows},{'first','second','third'})
        with self.store._connect() as db:
            periods=db.execute("SELECT * FROM model_usage_periods WHERE period_type='day'").fetchall()
            self.assertEqual(len(periods),3)
            self.assertEqual(sum(r['reserved_tokens'] for r in periods),90)
            text='\n'.join(r['pricing_json'] for r in db.execute('SELECT * FROM model_usage_ledger'))
            self.assertNotIn('-secret',text);self.assertNotIn('https://',text)

    def test_same_purpose_and_model_still_groups_each_provider_separately(self):
        for name in ('first','second','third'):
            with model_call_context('tool_reasoning',profile_id=name+'-model'):
                self.gateway.create(messages=[])
        self.assertEqual(len(usage_report(self.store)['purposes']),3)

    def test_route_changes_block_retry_but_key_rotation_does_not(self):
        token=MODEL_EVENT.set('same-request');self.addCleanup(MODEL_EVENT.reset,token)
        self.gateway.create(messages=[])
        changed=configuration();changed['roles']['regular']='second-model'
        self.gateway.configuration=parse_config(changed)
        with self.assertRaises(ModelConfigError):self.gateway.create(messages=[])
        self.assertEqual(len(self.requests),1)
        changed=configuration();changed['connections'][0]['api_key']='rotated'
        gateway=ModelGateway(self.store,configuration=parse_config(changed),client_factory=create_model_client)
        self.addCleanup(gateway.close);gateway.create(messages=[])
        self.assertEqual(len(self.requests),2)
        self.assertEqual(self.requests[-1].headers['authorization'],'Bearer rotated')

    def test_undeclared_tools_and_vision_are_blocked_before_network(self):
        value=configuration();value['profiles'][0]['capabilities']=['text'];self.gateway.configuration=parse_config(value)
        for kwargs in ({'tools':[{'type':'function','function':{'name':'f'}}]},
                       {'response_format':{'type':'json_object'}},
                       {'messages':[{'role':'user','content':[{'type':'image_url','image_url':{'url':'data:image/png;base64,AA'}}]}]}):
            with self.assertRaises(ModelConfigError):self.gateway.create(**kwargs)
        self.assertEqual(self.requests,[])

    def test_currency_limits_are_separate_and_output_tier_does_not_change_provider(self):
        value=configuration();value['cost_limits']={'USD':{'daily':0.00001},'CNY':{'daily':1}}
        self.gateway.configuration=parse_config(value)
        with self.assertRaises(ModelBudgetExceeded):self.gateway.create(messages=[])
        with patch.dict(os.environ,{'AI_BUDGET_TIER':'economy'}),model_call_context('trip_plan'):
            self.gateway.create(messages=[])
        self.assertEqual(len(self.requests),1);self.assertEqual(self.requests[0].url.host,'third.example')

    def test_other_currency_spend_does_not_consume_this_currency_limit(self):
        value=configuration();value['cost_limits']={'USD':{'daily':0.0001},'CNY':{'daily':1}}
        self.gateway.configuration=parse_config(value)
        with model_call_context('trip_plan'):self.gateway.create(messages=[])
        self.gateway.create(messages=[{'role':'user','content':'hello'}],max_completion_tokens=10)
        self.assertEqual(len(self.requests),2)

    def test_migration_splits_only_saved_currency_evidence_and_is_idempotent(self):
        for identity in ('first-model','second-model','third-model'):
            with model_call_context('tool_reasoning',profile_id=identity):self.gateway.create(messages=[])
        with self.store._connect() as db:
            original=db.execute("SELECT sum(calls),sum(reserved_tokens),sum(estimated_cost) FROM model_usage_periods WHERE period_type='day'").fetchone()
            db.execute('ALTER TABLE model_usage_periods RENAME TO periods_for_test')
            db.execute('''CREATE TABLE model_usage_periods AS SELECT period_type,period_start,purpose,model_id,
                sum(calls) calls,sum(reserved_tokens) reserved_tokens,sum(prompt_tokens) prompt_tokens,
                sum(cached_input_tokens) cached_input_tokens,sum(completion_tokens) completion_tokens,
                sum(reasoning_tokens) reasoning_tokens,sum(estimated_cost) estimated_cost,sum(cost_unknown) cost_unknown,
                '' cost_currency FROM periods_for_test GROUP BY period_type,period_start,purpose,model_id''')
            db.execute('DROP TABLE periods_for_test')
            db.execute("UPDATE model_usage_ledger SET connection_id='legacy_unknown',profile_id='legacy_unknown'")
            from infrastructure.model_accounting import initialize_accounting
            initialize_accounting(db)
            rows=[dict(r) for r in db.execute("SELECT * FROM model_usage_periods WHERE period_type='day'")]
            self.assertEqual(len(rows),2)
            self.assertEqual({r['cost_currency'] for r in rows},{'USD','CNY'})
            self.assertTrue(all(r['connection_id']=='legacy_unknown' for r in rows))
            after=db.execute("SELECT sum(calls),sum(reserved_tokens),sum(estimated_cost) FROM model_usage_periods WHERE period_type='day'").fetchone()
            self.assertEqual(tuple(original),tuple(after))
            initialize_accounting(db)
            self.assertEqual(rows,[dict(r) for r in db.execute("SELECT * FROM model_usage_periods WHERE period_type='day'")])

    def test_new_routing_does_not_fall_back_to_legacy_environment_model(self):
        from agents.intent_compiler import IntentCompiler,IntentCompilationError
        def malformed(request):
            self.requests.append(request)
            return httpx.Response(200,json={'id':'bad','object':'chat.completion','created':0,'model':'same-model',
                'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':'not JSON'}}],
                'usage':{'prompt_tokens':20,'completion_tokens':10,'total_tokens':30}})
        self.transport.handler=malformed
        with patch.dict(os.environ,{'LLM_INTENT_MODEL_ID':'different-legacy-model'}):
            with self.assertRaises(IntentCompilationError):IntentCompiler(self.gateway,'default').try_compile('hello')
        self.assertTrue(self.requests)
        self.assertTrue(all(r.url.host=='second.example' for r in self.requests))

    def test_unknown_submission_is_not_replayed_on_restart(self):
        token=MODEL_EVENT.set('uncertain-request');self.addCleanup(MODEL_EVENT.reset,token)
        _,pricing=self.gateway._route(ModelCallContext(),{'messages':[]})
        call=self.gateway._reserve('uncertain-request','same-model',100,20,pricing=pricing)
        self.gateway._finish(call,time.monotonic(),None,TimeoutError())
        with self.assertRaises(ModelConfigError):self.gateway.create(messages=[])
        self.assertEqual(self.requests,[])

    def test_received_response_without_usage_can_continue_the_bounded_workflow(self):
        token=MODEL_EVENT.set('response-without-usage');self.addCleanup(MODEL_EVENT.reset,token)
        def received(request):
            self.requests.append(request)
            return httpx.Response(200,json={'id':'known','object':'chat.completion','created':0,'model':'same-model',
                'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':'OK'}}]})
        self.transport.handler=received
        for _ in range(2):self.gateway.create(messages=[{'role':'user','content':'hello'}])
        self.assertEqual(len(self.requests),2)
        report=usage_report(self.store)
        self.assertEqual(report['totals']['unknown_usage'],2)
        self.assertGreater(report['budget']['day_unknown_occupied'],0)

    def test_legacy_unknown_submission_is_correlated_without_guessing_its_identity(self):
        token=MODEL_EVENT.set('legacy-unknown');self.addCleanup(MODEL_EVENT.reset,token)
        pricing={'input':None,'cached':None,'output':None,'currency':'USD','provider':'','version':''}
        call=self.gateway._reserve('legacy-unknown','same-model',100,20,pricing=pricing)
        self.gateway._finish(call,time.monotonic(),None,TimeoutError())
        with self.store._connect() as db:
            db.execute("UPDATE model_usage_ledger SET event_key='',stage_id='' WHERE call_id=?",(call,))
        gateway=ModelGateway(self.store,configuration=parse_config(configuration()),client_factory=create_model_client)
        self.addCleanup(gateway.close)
        with self.assertRaises(ModelConfigError):gateway.create(messages=[])
        self.assertEqual(self.requests,[])

    def test_unknown_exact_sdk_request_is_not_submitted_twice(self):
        from openai import APITimeoutError
        def timeout(request):
            self.requests.append(request)
            raise httpx.ReadTimeout('fixture timeout',request=request)
        self.transport.handler=timeout
        token=MODEL_EVENT.set('sdk-unknown');self.addCleanup(MODEL_EVENT.reset,token)
        with self.assertRaises(APITimeoutError):self.gateway.create(messages=[{'role':'user','content':'hello'}])
        with self.assertRaises(ModelConfigError):self.gateway.create(messages=[{'role':'user','content':'hello'}])
        self.assertEqual(len(self.requests),1)
        with self.store._connect() as db:
            row=db.execute('SELECT result_received,request_fingerprint FROM model_usage_ledger').fetchone()
        self.assertEqual(row['result_received'],0);self.assertEqual(len(row['request_fingerprint']),64)

    def test_distinct_inputs_in_one_event_can_use_the_two_model_slots(self):
        barrier=threading.Barrier(2);original=self.transport.handler
        def respond(request):
            barrier.wait(timeout=8)
            return original(request)
        self.transport.handler=respond
        def run(text):
            token=MODEL_EVENT.set('parallel-distinct-inputs')
            try:self.gateway.create(messages=[{'role':'user','content':text}])
            finally:MODEL_EVENT.reset(token)
        with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(run,('first input','second input')))
        self.assertEqual(len(self.requests),2)
