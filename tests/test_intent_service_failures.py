from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
import asyncio,json,os,tempfile,unittest

import httpx
from openai import APITimeoutError,APIConnectionError,APIStatusError
from fastapi.testclient import TestClient

from adapters.web_app import create_web_app
from agents.intent_compiler import IntentCompiler,IntentCompilationError
from app.request_router import RequestRouter
from core.chat_transport import ChatEvent
from core.execution_scope import ExecutionRevoked
from core.intent_ir import IntentIR,IntentOperation
from core.settings import Settings
from core.web_settings import WebSettings
from evaluation.support import Budget,BoundedClient,SmokeLimit
from infrastructure.memory_store import MemoryStore
from infrastructure.model_client import create_model_client,model_failure_metadata
from tools.travel_query import TravelQueryTool

QUESTION='我想从武汉去南京玩两天，计划30号下午出发，11月1号下午回来，帮我规划一下旅程。'


def timed_out(kind=httpx.ReadTimeout):
    request=httpx.Request('POST','https://llm.invalid/private-key-path')
    error=APITimeoutError(request=request)
    error.__cause__=kind('credential-sensitive-error',request=request)
    return error


class ModelFailureTests(unittest.TestCase):
    def test_timeout_phase_survives_sdk_wrapping_without_private_data(self):
        for kind,phase in ((httpx.ReadTimeout,'read'),(httpx.ConnectTimeout,'connect'),
                           (httpx.WriteTimeout,'write'),(httpx.PoolTimeout,'pool')):
            data=model_failure_metadata(timed_out(kind))
            self.assertEqual((data['error_code'],data['timeout_phase']),('timeout',phase))
            self.assertEqual(data['cause_type'],kind.__name__)
            self.assertNotIn('credential',json.dumps(data));self.assertNotIn('private-key',json.dumps(data))

    def test_statuses_and_safe_request_ids_are_classified(self):
        for status,code in ((401,'authentication'),(403,'authentication'),(429,'rate_limit'),(503,'upstream_error'),(400,'invalid_request')):
            response=httpx.Response(status,request=httpx.Request('POST','https://llm.invalid/private'),headers={'x-request-id':'safe-request-id'})
            error=APIStatusError('private-secret',response=response,body={'api_key':'private-secret'})
            data=model_failure_metadata(error)
            self.assertEqual((data['error_code'],data['http_status']),(code,status))
            self.assertEqual(data['upstream_request_id'],'safe-request-id')
            self.assertNotIn('private',json.dumps(data))

    def test_unknown_validation_is_not_a_transport_error_and_budgets_win_wrappers(self):
        self.assertEqual(model_failure_metadata(ValueError('private content')),{})
        error=APIConnectionError(request=httpx.Request('POST','https://llm.invalid'))
        error.__cause__=SmokeLimit('limit')
        self.assertEqual(model_failure_metadata(error)['error_code'],'budget_exceeded')

    def test_timeout_never_falls_back_to_another_model(self):
        client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=Mock(side_effect=timed_out()))))
        with patch.dict(os.environ,{'LLM_INTENT_MODEL_ID':'special-intent'}):
            with self.assertRaises(IntentCompilationError) as caught:IntentCompiler(client,'default').try_compile(QUESTION)
        self.assertEqual(caught.exception.code,'timeout')
        self.assertEqual(client.chat.completions.create.call_count,1)

    def test_invalid_json_is_distinct_from_service_failure_and_cancellation_survives(self):
        client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=Mock(return_value=
            SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='invalid JSON'))])))))
        with patch.dict(os.environ,{'LLM_INTENT_MODEL_ID':''}):
            with self.assertRaises(IntentCompilationError) as caught:IntentCompiler(client,'same').try_compile(QUESTION)
        self.assertEqual(caught.exception.code,'invalid_response')
        client.chat.completions.create.side_effect=ExecutionRevoked('cancelled')
        with self.assertRaises(ExecutionRevoked):IntentCompiler(client,'same').try_compile(QUESTION)

    def test_internal_errors_are_not_repaired_or_sent_to_a_fallback_model(self):
        client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=Mock(side_effect=RuntimeError('private')))))
        with patch.dict(os.environ,{'LLM_INTENT_MODEL_ID':'other-model'}):
            with self.assertRaises(IntentCompilationError) as caught:IntentCompiler(client,'default').try_compile(QUESTION)
        self.assertEqual(caught.exception.code,'internal_error')
        self.assertEqual(client.chat.completions.create.call_count,1)


class SharedClientTests(unittest.TestCase):
    def test_proxy_mounts_and_budget_hooks_share_production_sdk_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            budget=Budget(Path(directory)/'audit.json',max_calls=2,max_tokens=10000)
            mounts=[];requests=[]
            def respond(request):
                requests.append(request)
                return httpx.Response(200,json={'id':'fake','object':'chat.completion','created':0,'model':'fake',
                    'choices':[{'index':0,'message':{'role':'assistant','content':'ok'},'finish_reason':'stop'}],
                    'usage':{'prompt_tokens':10,'completion_tokens':2,'total_tokens':12}},request=request)
            def transport(**kwargs):
                mounts.append(kwargs.get('proxy') is not None)
                return httpx.MockTransport(respond)
            with patch.dict(os.environ,{'HTTPS_PROXY':'http://proxy.invalid:8443','HTTP_PROXY':'http://proxy.invalid:8443'},clear=True),\
                 patch('httpx._client.HTTPTransport',side_effect=transport):
                sdk=create_model_client(api_key='offline-key',base_url='https://llm.invalid/v1',before_request=budget.before_request)
                client=BoundedClient(sdk,budget)
                try:client.chat.completions.create(model='fake',messages=[{'role':'user','content':'hello'}])
                finally:client.close()
            self.assertTrue(any(mounts),'Environment proxy mounts must not disappear under evaluation')
            self.assertEqual((budget.physical_calls,budget.tokens,len(requests)),(1,12,1))
            self.assertEqual(client.network_metadata['network_policy'],'sdk_environment')
            self.assertTrue(client.network_metadata['trust_env'])
            self.assertEqual(sdk.max_retries,0)
            self.assertEqual(sdk.timeout,90)

    def test_sdk_errors_do_not_hide_retries_and_unknown_usage_stops_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            budget=Budget(Path(directory)/'audit.json',max_calls=2,max_tokens=10000);calls=[]
            def respond(request):calls.append(request);raise httpx.ConnectTimeout('private-network-message',request=request)
            with patch('httpx._client.HTTPTransport',side_effect=lambda **kwargs:httpx.MockTransport(respond)):
                sdk=create_model_client(api_key='offline',base_url='https://llm.invalid/v1',before_request=budget.before_request)
                client=BoundedClient(sdk,budget)
                try:
                    with self.assertRaises(APITimeoutError):client.chat.completions.create(model='fake',messages=[],max_tokens=100)
                finally:client.close()
            self.assertEqual((len(calls),budget.physical_calls),(1,1))
            self.assertEqual(budget.stop_reason,'unknown_usage_stop')

    def test_formal_debug_entry_uses_full_intent_and_production_factory_without_network(self):
        from evaluation.runner import main
        observed=[]
        def respond(request):
            payload=json.loads(request.content);observed.append(payload)
            text=json.loads(payload['messages'][-1]['content'])['message']
            ir={'schema_version':1,'action':'create','operations':[{'domain':'trip','operation':'create','request_text':text}],
                'missing_fields':[],'requires_confirmation':False,'source_spans':[text],'confidence':0.99}
            return httpx.Response(200,json={'id':'fake','object':'chat.completion','created':0,'model':'fake',
                'choices':[{'index':0,'message':{'role':'assistant','content':json.dumps(ir,ensure_ascii=False)},'finish_reason':'stop'}],
                'usage':{'prompt_tokens':10,'completion_tokens':10,'total_tokens':20}},request=request)
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{
                'LLM_API_KEY':'offline-key','LLM_BASE_URL':'https://llm.invalid/v1','LLM_MODEL_ID':'fake','LLM_INTENT_MODEL_ID':'',
                'AI_DAILY_TOKEN_LIMIT':'0','AI_MONTHLY_TOKEN_LIMIT':'0','AI_DAILY_COST_LIMIT':'0','AI_MONTHLY_COST_LIMIT':'0'}),\
             patch('dotenv.load_dotenv',return_value=False),patch('core.web_settings.load_dotenv',return_value=False),\
             patch('core.web_settings.EnvSettingsStore',side_effect=OSError),\
             patch('httpx._client.HTTPTransport',side_effect=lambda **kwargs:httpx.MockTransport(respond)):
            report=Path(directory)/'report.json';result=Path(directory)/'result.json'
            code=main(['--mode','real_model_fixed_tools','--token-budget','60000','--report',str(report),
                       'smoke_semantic_compiler','--output',str(result)])
            self.assertEqual(code,0)
            saved=json.loads(result.read_text(encoding='utf-8'))
            self.assertEqual(saved['business_actions'],0)
            self.assertEqual(saved['physical_model_calls'],1)
            details=[r['details'] for r in saved['results'][0]['diagnostics'] if r['stage']=='model']
            self.assertTrue(any(r.get('purpose')=='intent_parse' and r.get('network_policy')=='sdk_environment' for r in details))
            self.assertEqual(json.loads(report.read_text(encoding='utf-8'))['actual_usage']['physical_calls'],1)
        self.assertEqual(len(observed),1)
        self.assertGreater(len(observed[0]['messages'][0]['content']),5000,'Debug must not replace the full intent prompt with a small health probe')


class IntentFailureWebTests(unittest.TestCase):
    def test_legacy_handle_reports_failure_and_shadow_has_no_reply(self):
        with tempfile.TemporaryDirectory() as directory:
            store=MemoryStore(Path(directory)/'test.db')
            event=ChatEvent('web','group','failure','scope','owner',QUESTION)
            compiler=Mock(try_compile=Mock(side_effect=IntentCompilationError('timeout')))
            router=RequestRouter(store,compiler)
            reply=router.handle(event,store.delivery.begin_event(event.event_key))
            self.assertIn('模型服务响应超时',reply)
            router.mode='shadow'
            self.assertIsNone(router.handle(event,None))

    def test_actual_web_timeout_preserves_input_has_no_business_effect_and_explains_failure(self):
        requests=[]
        def respond(request):requests.append(request);raise httpx.ReadTimeout('private-network-message',request=request)
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'AI_SUMMARY_ENABLED':'false','AI_VECTOR_ENABLED':'false'}),\
             patch('httpx._client.HTTPTransport',side_effect=lambda **kwargs:httpx.MockTransport(respond)):
            app=create_web_app(WebSettings(data_dir=Path(directory),open_browser=False),
                Settings('','',frozenset(),'','offline-key','https://llm.invalid/v1/private-key-path','fake'),start_workers=False)
            with TestClient(app,base_url='http://127.0.0.1:8080') as client:
                headers={'x-csrf-token':client.get('/api/bootstrap').json()['csrf_token']}
                cid=app.state.repository.create_conversation('offline')['id']
                submitted=client.post(f'/api/conversations/{cid}/messages',headers=headers,
                    json={'content':QUESTION,'client_request_id':'failure','upload_ids':[]}).json()
                asyncio.run(app.state.worker.run_once())
                messages=client.get(f'/api/conversations/{cid}/messages').json()['items']
                reply=next(row['content'] for row in messages if row['role']=='assistant')
                self.assertIn('模型服务响应超时',reply);self.assertIn('完整请求已保留',reply)
                self.assertNotIn('补充或重新说明',reply);self.assertNotIn('计划玩几天',reply)
                report=client.get(f"/api/conversations/{cid}/jobs/{submitted['job_id']}/diagnostics").json()
                self.assertIn('业务未执行',report['state'])
                self.assertTrue(any(row['details'].get('timeout_phase')=='read' for row in report['timeline']))
                self.assertTrue(any(row['details'].get('timeout_seconds')==90 for row in report['timeline']))
                self.assertFalse(any(row['details'].get('status')=='selected' for row in report['timeline']))
                text=json.dumps(report,ensure_ascii=False)
                for secret in ('offline-key','private-key-path','private-network-message',QUESTION):self.assertNotIn(secret,text)
                store=app.state.repository.store
                with store._connect() as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM model_calls').fetchone()[0],1)
                    self.assertEqual(db.execute('SELECT count(*) FROM travel_tasks').fetchone()[0],0)
                    self.assertEqual(db.execute('SELECT count(*) FROM trips').fetchone()[0],0)
                    self.assertEqual(db.execute('SELECT content FROM web_requests').fetchone()[0],QUESTION)
                    self.assertEqual(db.execute('SELECT usage_complete FROM model_calls').fetchone()[0],0)
            self.assertEqual(len(requests),1)

    def test_valid_clarification_still_asks_a_genuinely_missing_location(self):
        with tempfile.TemporaryDirectory() as directory:
            store=MemoryStore(Path(directory)/'test.db')
            event=ChatEvent('web','group','missing','scope','owner','明天天气怎么样？')
            ir=IntentIR(1,'clarify',(IntentOperation('forecast','query',{}),),missing_fields=('location',),source_spans=(event.content,))
            router=RequestRouter(store,Mock(try_compile=Mock(return_value=ir)),
                read_only_service=TravelQueryTool(Settings('','',frozenset(),'','','',''),store=store))
            reply,_=router.route(event,store.delivery.begin_event(event.event_key))
            self.assertIn('地点',reply);self.assertNotIn('模型服务',reply)
