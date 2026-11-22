"""U4/U5 isolated Web acceptance. No production messages, config edits or QQ sends."""
import argparse
import asyncio
from contextlib import ExitStack
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from evaluation.support import ROOT,EVALUATION_BUDGET,SmokeLimit


async def live(args):
    import openai
    import httpx
    import urllib3
    from core.logging_config import configure_logging
    from core.web_settings import WebSettings
    from adapters.web_app import create_web_app
    from app import runtime_factory
    from infrastructure.amap_client import AmapClient
    budget=EVALUATION_BUDGET.get()
    replay=json.loads(args.replay.read_text(encoding='utf-8')) if args.replay else None
    if replay:budget=SimpleNamespace(max_calls=6,max_tokens=100000,physical_calls=0)
    if budget is None or budget.max_calls!=6 or budget.max_tokens>100000:
        raise ValueError('Use the bounded evaluation runner with an explicit token budget.')
    args.output.parent.mkdir(parents=True,exist_ok=True)
    configure_logging(args.output.parent/'logs','intercity-replay.log' if replay else 'intercity-live.log')
    settings=WebSettings() if replay else WebSettings.from_env()
    models=[];maps=[];remote=[];http_count=0;lock=threading.Lock()
    native_openai=openai.OpenAI;native_get=AmapClient._get
    native_http=urllib3.connectionpool.HTTPSConnectionPool.urlopen
    model_cursor=iter(replay['models']) if replay else None
    map_cursor=iter(replay['amap_responses']) if replay else None
    remote_cursor=iter(replay['mcp_responses']) if replay else None
    quote_times=[]
    if replay:
        traffic=replay['preview_context']['trips'][0]['plan']['intercity']
        quote_times=iter([traffic[d]['queried_at'] for d in ('outbound','inbound')])
    class QuoteClock(datetime):
        value=None
        @classmethod
        def now(cls,tz=None):
            value=datetime.fromisoformat(cls.value) if cls.value else datetime.now().astimezone()
            return value.astimezone(tz) if tz else value
    def sdk(*a,**kw):
        client=None if replay else native_openai(*a,**kw)
        def create(**kwargs):
            if replay:
                from openai.types.chat import ChatCompletion
                response=ChatCompletion.model_validate(next(model_cursor)['response']);budget.physical_calls+=1
            else:response=client.chat.completions.create(**kwargs)
            models.append({'request':kwargs,'response':response.model_dump(mode='json')})
            return response
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)),close=client.close if client else lambda:None,
            network_metadata=getattr(client,'network_metadata',{'network_policy':'injected_or_legacy'}) if client else {'network_policy':'offline_replay'})
    def counted(pool,method,url,*a,**kw):
        nonlocal http_count
        with lock:
            if http_count>=30:raise SmokeLimit('Map HTTP ceiling reached')
            http_count+=1
        return native_http(pool,method,url,*a,**kw)
    def captured(client,path,params):
        if replay:
            saved=next(map_cursor)
            assert saved['path']==path and saved['params']==params,'Map replay request changed'
            value=saved['result']
        else:value=native_get(client,path,params)
        maps.append({'path':path,'params':{k:v for k,v in params.items() if k.lower() not in {'key','token','api_key'}},'result':value})
        return value
    report={'mode':'offline_replay' if replay else 'live_provider','models':models,'amap_responses':maps,'mcp_responses':remote,
            'limits':{'model_calls':6,'model_tokens':100000,'model_output_tokens':8192,'rail_tools_call':24,
                      'amap_http':30,'trial_connections':1,'wendao_calls':0},'wendao_disabled_for_isolation':True}
    started=time.monotonic();app=None
    try:
        with tempfile.TemporaryDirectory() as directory,ExitStack() as stack:
            stack.enter_context(patch.dict('os.environ',{'WENDAO_ENABLED':'false'}))
            stack.enter_context(patch.object(runtime_factory,'OpenAI',side_effect=sdk))
            stack.enter_context(patch.object(AmapClient,'_get',captured))
            stack.enter_context(patch.object(urllib3.connectionpool.HTTPSConnectionPool,'urlopen',counted))
            if replay:stack.enter_context(patch('tools.travel_query.datetime',QuoteClock))
            isolated=replace(settings,data_dir=Path(directory),port=8084,open_browser=False)
            from core.settings import Settings
            transport_settings=Settings('','',frozenset(),'fixture-map','fixture-model','https://model.example/v1',
                replay['models'][0]['request']['model'],rail_mcp_config=json.dumps({'enabled':True,'endpoint':'https://replay.example/mcp'})) if replay else settings.travel_settings()
            app=create_web_app(isolated,transport_settings)
            runtime=app.state.runtime
            mcp=runtime.rail_client.mcp
            mcp.max_calls=24;mcp.max_connections=1
            if replay:
                from evaluation.cases.rail_mcp import FakeSession
                from mcp.types import CallToolResult
                class Session(FakeSession):
                    async def call_tool(self,name,arguments):
                        saved=next(remote_cursor)
                        assert saved['name']==name and saved['arguments']==arguments,'Rail replay request changed'
                        if name=='get-tickets':QuoteClock.value=next(quote_times)
                        return CallToolResult.model_validate(saved['result'])
                    async def send_ping(self):return SimpleNamespace()
                @asynccontextmanager
                async def session_factory(config):yield Session()
                mcp.session_factory=session_factory
            original=mcp.call
            async def recorded(name,arguments):
                result=await original(name,arguments)
                remote.append({'name':name,'arguments':arguments,'result':result.model_dump(mode='json')})
                return result
            mcp.call=recorded
            async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8084') as web:
                boot=await web.get('/api/bootstrap');boot.raise_for_status()
                headers={'x-csrf-token':boot.json()['csrf_token']}
                response=await web.post('/api/conversations',json={},headers=headers);response.raise_for_status()
                identity=response.json()['id']
                async def message(text,request_id):
                    result=await web.post(f'/api/conversations/{identity}/messages',json={'content':text,'client_request_id':request_id},headers=headers)
                    result.raise_for_status();job=result.json()['job_id']
                    for _ in range(180):
                        state=(await web.get('/api/tasks/'+str(job))).json()
                        if state['status'] in {'completed','failed','cancelled','blocked'}:return state
                        await asyncio.sleep(1)
                    raise TimeoutError('Isolated request deadline')
                text='我从武汉出发，想在2026-11-03开始去南京旅行两天，含来回，市内公交，不夜游。必去夫子庙和玄武湖，每天一处主要景点，不需要酒店餐饮，不用问道。请给我完整行程预览。'
                preview=await message(text,'u45-preview')
                context=(await web.get(f'/api/conversations/{identity}/context')).json()
                report.update(preview_job=preview,preview_context=context)
                report['preview_messages']=(await web.get(f'/api/conversations/{identity}/messages')).json()
                assert preview['status']=='completed' and context['trips']
                trip=context['trips'][0];traffic=trip['plan']['intercity']
                assert trip.get('is_preview') and traffic.get('outbound',{}).get('selected') and traffic.get('inbound',{}).get('selected')
                assert not trip['spec']['hotel_requested'] and not trip['spec']['restaurant_requested']
                assert not trip['plan']['hotel_ids'] and not trip['plan']['restaurant_ids']
                assert not any(row.get('rail_results') for row in report['preview_messages']['items']),'Planning dependencies hid the itinerary reply'
                before={'models':budget.physical_calls,'rail':mcp.counts['tools_call'],'http':http_count}
                confirm=await message('确认行程修改','u45-confirm')
                after={'models':budget.physical_calls,'rail':mcp.counts['tools_call'],'http':http_count}
                assert before==after,'Confirmation made new provider calls'
                saved=(await web.get(f'/api/conversations/{identity}/context')).json()
                assert saved['trips'] and not saved['trips'][0].get('is_preview')
                assert saved['trips'][0]['plan']['intercity']==traffic
                report.update(confirm_job=confirm,confirmed_context=saved,confirmation_provider_counts={'before':before,'after':after},
                              mcp_operations=mcp.status()['operations'],passed=True)
                if replay:
                    actual=saved['trips'][0]
                    expected=replay['confirmed_context']['trips'][0]
                    assert actual['plan']['days']==expected['plan']['days']
                    for direction in ('outbound','inbound'):
                        assert actual['plan']['intercity'][direction]['selected']==expected['plan']['intercity'][direction]['selected']
                    report['original_data_times_preserved']=all(actual['plan']['intercity'][d]['queried_at']==expected['plan']['intercity'][d]['queried_at'] for d in ('outbound','inbound'))
                messages=(await web.get(f'/api/conversations/{identity}/messages')).json()
                report['messages']=messages
                with runtime.store._connect() as db:
                    report['diagnostics']=[dict(r) for r in db.execute('SELECT stage,details_json,created_at FROM execution_diagnostics ORDER BY id')]
    except Exception as error:
        report.update(passed=False,error_type=type(error).__name__)
    finally:
        report.update(amap_http=http_count,elapsed_seconds=round(time.monotonic()-started,3))
        if replay:report.update(real_external_calls=0,scripted_model_calls=len(models))
        # No keys/headers enter model kwargs or capture files; strip known configuration credentials defensively.
        serialized=json.dumps(report,ensure_ascii=False,indent=2,default=str)
        from infrastructure.mcp_client import RailMCPConfig
        cfg=RailMCPConfig() if replay else RailMCPConfig.parse(settings.travel_settings().rail_mcp_config)
        for secret in (cfg.endpoint,cfg.bearer_token,settings.travel_settings().llm_api_key,settings.travel_settings().amap_api_key):
            if secret:serialized=serialized.replace(secret,'[redacted]')
        args.output.write_text(serialized+'\n',encoding='utf-8')
    return report.get('passed',False)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--authorize-live',action='store_true')
    parser.add_argument('--replay',type=Path)
    args=parser.parse_args()
    if args.output.exists() or args.authorize_live and args.replay:parser.error('Use a fresh evidence path and separate replay/live modes.')
    if args.authorize_live or args.replay:passed=asyncio.run(live(args))
    else:
        sys.path.insert(0,str(ROOT/'tests'))
        from test_intercity_planning import IntercityPlanningTests
        from test_workflow_logging import WorkflowLoggingTests
        suite=unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(c) for c in (IntercityPlanningTests,WorkflowLoggingTests))
        result=unittest.TextTestRunner().run(suite);passed=result.wasSuccessful()
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps({'mode':'offline','real_calls':0,'summary':{'total':result.testsRun,
            'passed':result.testsRun-len(result.failures)-len(result.errors)}}),encoding='utf-8')
    raise SystemExit(0 if passed else 1)


if __name__=='__main__':main()
