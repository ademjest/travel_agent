from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import json
import os
from unittest.mock import Mock, patch

import httpx
from openai import OpenAI
from scripts.smoke_summary_live import Budget,BoundedClient,CountingTransport,SmokeLimit
from scripts.smoke_summary_live import main
from scripts.evaluate_summary_quality import OFFLINE_ENV


class SmokeBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output=Path(self.temp.name)/'audit.json'

    def request(self):
        return {'model':'fake','messages':[{'role':'user','content':'hello'}],'max_completion_tokens':16}

    def response(self,total=20):
        return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=10,completion_tokens=10,total_tokens=total),
                               choices=[SimpleNamespace(finish_reason='stop',message=SimpleNamespace(content='ok'))])

    def test_physical_call_limit_blocks_before_next_dispatch(self):
        budget=Budget(self.output,max_calls=1)
        budget.reserve(self.request())
        budget.dispatch()
        budget.settle(self.response())
        with self.assertRaises(SmokeLimit):
            budget.reserve(self.request())
        self.assertEqual(budget.physical_calls,1)
        self.assertEqual(budget.tokens,20)

    def test_token_reservation_blocks_before_sending(self):
        budget=Budget(self.output,max_tokens=100)
        with self.assertRaises(SmokeLimit):
            budget.reserve(self.request())
        self.assertEqual(budget.physical_calls,0)

    def test_unknown_usage_reserves_upper_bound_and_stops(self):
        budget=Budget(self.output)
        budget.reserve(self.request())
        reserved=budget.pending['reservation']
        budget.dispatch()
        budget.settle(SimpleNamespace(usage=None,choices=[]))
        self.assertEqual(budget.tokens,reserved)
        self.assertEqual(budget.stop_reason,'unknown_usage_stop')
        with self.assertRaises(SmokeLimit):
            budget.reserve(self.request())

    def test_failed_physical_request_counts_and_stops_without_usage(self):
        budget=Budget(self.output)
        budget.reserve(self.request())
        budget.dispatch()
        budget.settle(error=TimeoutError())
        self.assertEqual(budget.physical_calls,1)
        self.assertEqual(budget.records[0]['error_type'],'TimeoutError')
        self.assertEqual(budget.stop_reason,'unknown_usage_stop')

    def test_local_failure_does_not_count_as_physical_call(self):
        budget=Budget(self.output)
        budget.reserve(self.request())
        budget.settle(error=ValueError())
        self.assertEqual(budget.physical_calls,0)
        self.assertEqual(budget.tokens,0)

    def test_bounded_client_forwards_runtime_cleanup_without_model_calls(self):
        sdk=Mock()
        budget=Budget(self.output)
        BoundedClient(sdk,budget).close()
        sdk.close.assert_called_once_with()
        self.assertEqual(budget.physical_calls,0)

    def test_overwriting_old_audit_or_expanding_limits_is_forbidden(self):
        with self.assertRaises(ValueError):
            Budget(self.output,max_calls=13)
        with self.assertRaises(ValueError):
            Budget(self.output,max_tokens=100001)
        budget=Budget(self.output)
        budget.save()
        with self.assertRaises(FileExistsError):
            Budget(self.output)

    def test_real_sdk_http_layer_has_no_retries_and_no_redirects(self):
        budget=Budget(self.output)
        observed=[]
        def respond(request):
            observed.append(request)
            return httpx.Response(503,json={'error':{'message':'injected offline error','type':'server_error'}},request=request)
        http=httpx.Client(transport=CountingTransport(budget,httpx.MockTransport(respond)),follow_redirects=False)
        self.addCleanup(http.close)
        sdk=OpenAI(api_key='offline-not-a-real-key',base_url='https://example.invalid/v1',max_retries=0,http_client=http)
        self.addCleanup(sdk.close)
        client=BoundedClient(sdk,budget)
        with self.assertRaises(Exception):
            client.create(**self.request())
        self.assertEqual(len(observed),1)
        self.assertEqual(budget.physical_calls,1)
        self.assertEqual(sdk.max_retries,0)
        self.assertFalse(http.follow_redirects)
        self.assertNotIn('offline-not-a-real-key',self.output.read_text())

    def test_sdk_usage_cost_and_physical_count_are_recorded(self):
        budget=Budget(self.output)
        def respond(request):
            return httpx.Response(200,json={'id':'offline','object':'chat.completion','created':0,'model':'fake',
                'choices':[{'index':0,'message':{'role':'assistant','content':'ok'},'finish_reason':'stop'}],
                'usage':{'prompt_tokens':10,'completion_tokens':10,'total_tokens':20}},request=request)
        http=httpx.Client(transport=CountingTransport(budget,httpx.MockTransport(respond)),follow_redirects=False)
        self.addCleanup(http.close)
        sdk=OpenAI(api_key='offline',base_url='https://example.invalid/v1',max_retries=0,http_client=http)
        self.addCleanup(sdk.close)
        BoundedClient(sdk,budget).create(**self.request())
        self.assertEqual(budget.tokens,20)
        self.assertEqual(budget.physical_calls,1)
        self.assertTrue(budget.records[0]['usage_complete'])

    def test_multiple_dispatches_for_one_sdk_request_are_blocked(self):
        budget=Budget(self.output)
        budget.reserve(self.request())
        budget.dispatch()
        with self.assertRaises(SmokeLimit):
            budget.dispatch()
        self.assertEqual(budget.physical_calls,1)

    def test_live_main_full_pipeline_uses_only_mock_http(self):
        def respond(request):
            payload=json.loads(request.content)
            if payload.get('response_format'):
                incoming=json.loads(payload['messages'][-1]['content'])
                source=next(entry for entry in incoming['new_turns'] if '慢慢看桥' in entry['user'])
                content=json.dumps({'items':[{'kind':'reference','text':'纪念册名字是《慢慢看桥》',
                    'sources':[{'message_id':source['message_id'],'quote':'照片纪念册的名字是《慢慢看桥》'}]}]},ensure_ascii=False)
            else:
                content='仅为离线HTTP模拟响应。'
            return httpx.Response(200,json={'id':'offline','object':'chat.completion','created':0,'model':'fake',
                'choices':[{'index':0,'message':{'role':'assistant','content':content},'finish_reason':'stop'}],
                'usage':{'prompt_tokens':10,'completion_tokens':10,'total_tokens':20}},request=request)
        env={**OFFLINE_ENV,'LLM_API_KEY':'offline-not-a-real-key','LLM_BASE_URL':'https://example.invalid/v1','LLM_MODEL_ID':'fake'}
        with patch.dict(os.environ,env),patch('dotenv.load_dotenv',return_value=False),\
             patch('scripts.smoke_summary_live.RUNS',('Q01',)),\
             patch('httpx.HTTPTransport',side_effect=lambda **kwargs:httpx.MockTransport(respond)):
            main(SimpleNamespace(output=self.output,max_calls=12,max_tokens=100000))
        result=json.loads(self.output.read_text(encoding='utf-8'))
        self.assertEqual(result['budget']['physical_calls'],5)
        self.assertEqual(result['budget']['accounted_tokens'],100)
        self.assertTrue(result['results'][0]['stages'][0]['published_current'])
        self.assertIn('context_conditions',result['results'][0])
        self.assertNotIn('offline-not-a-real-key',self.output.read_text())

    def test_resume_keeps_prior_calls_and_tokens(self):
        budget=Budget(self.output,max_calls=2)
        budget.reserve(self.request())
        budget.dispatch()
        budget.settle(self.response())
        continued=Budget.resume(self.output)
        self.assertEqual((continued.physical_calls,continued.tokens),(1,20))
        continued.reserve(self.request())
        continued.dispatch()
        continued.settle(self.response())
        self.assertEqual((continued.physical_calls,continued.tokens),(2,40))
        with self.assertRaises(SmokeLimit):
            continued.reserve(self.request())

    def test_resume_refuses_unsettled_unknown_or_stopped_usage(self):
        budget=Budget(self.output)
        budget.reserve(self.request())
        budget.dispatch()
        with self.assertRaises(SmokeLimit):
            Budget.resume(self.output)
        budget.settle(error=TimeoutError())
        with self.assertRaises(SmokeLimit):
            Budget.resume(self.output)


if __name__=='__main__':
    unittest.main()
