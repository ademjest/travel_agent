"""U3 acceptance through the shared router/registry; temporary state, no messages or purchases."""
import argparse
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest

from evaluation.support import ROOT, EVALUATION_BUDGET


async def live(args):
    from openai import OpenAI
    from agents.intent_compiler import IntentCompiler
    from app.request_router import RequestRouter
    from core.chat_transport import ChatEvent
    from core.intent_ir import IntentIR, IntentOperation
    from core.tool_result import ToolResult
    from core.web_settings import WebSettings
    from infrastructure.memory_store import MemoryStore
    from infrastructure.model_gateway import ModelGateway
    from infrastructure.mcp_client import RailMCPConfig, RemoteMCPClient
    from infrastructure.rail_client import RailClient, validate_rail_tools
    from tools.travel_query import TravelQueryTool
    from tools.registry import AgentToolRouter
    budget=EVALUATION_BUDGET.get()
    if budget is None or budget.max_calls!=4 or budget.max_tokens>40000:
        raise ValueError('Live rail/model acceptance requires the shared bounded evaluation runner.')
    settings=WebSettings.from_env().travel_settings()
    config=RailMCPConfig.parse(settings.rail_mcp_config)
    mcp=RemoteMCPClient(config,validate_tools=validate_rail_tools,max_calls=args.max_calls,max_connections=1)
    results=[];raw=[];started=time.monotonic()
    report={'mode':'live_provider','results':results,'provider_responses':raw,'provider_cost':None,
            'limits':{'tools_call':args.max_calls,'connections':1,'seconds':args.seconds,'model_calls':4,
                      'model_tokens':budget.max_tokens,'model_output_tokens':4000}}
    original_call=mcp.call
    async def recorded(name,arguments):
        response=await original_call(name,arguments)
        raw.append({'tool':name,'arguments':arguments,'response':response.model_dump(mode='json')})
        return response
    mcp.call=recorded
    try:
        with tempfile.TemporaryDirectory() as folder:
            store=MemoryStore(Path(folder)/'acceptance.db')
            model_config=settings.model_configuration
            _,regular=model_config.select('unspecified',force_default=True)
            client=ModelGateway(store,configuration=model_config,client_factory=OpenAI,default_model=regular.model_id)
            tool=TravelQueryTool(settings,store=store);tool.rail=RailClient(mcp)
            reservation=SimpleNamespace(execute_result=lambda *a:ToolResult('failed','unsupported',error_code='denied'))
            registry=AgentToolRouter(tool,reservation,store.tool_artifacts,delivery_repository=store.delivery)
            read_results=[]
            def execute(name,arguments,context):
                result=registry.execute_result(name,arguments,context)
                if name=='query_train_tickets':read_results.append(result)
                return result
            tool.tool_executor=execute
            router=RequestRouter(store,IntentCompiler(client,regular.model_id,store.diagnostics),read_only_service=tool)
            async with asyncio.timeout(args.seconds):
                if not args.intent_only:
                    mcp.start();await mcp.wait_ready()
                # First validate the corrected U2 adapter through U3's exact same entry.
                samples=[('北京南','上海虹桥','station','high_speed_only',''),
                         ('南京南','上海虹桥','station','all','00:00至01:00'),
                         ('北京','上海','city','all','18:00至24:00')]
                for index,(origin,destination,scope,preference,window) in enumerate([] if args.intent_only else samples):
                    text=f'{args.date} {origin}到{destination} {window}'
                    event=ChatEvent('web','group','fixed-'+str(index),'acceptance','tester',text,occurred_at=datetime.now(timezone.utc))
                    claim=store.delivery.begin_event(event.event_key)
                    from core.tool_result import AgentToolContext
                    context=AgentToolContext('web','acceptance','tester',event.event_key,event=event,allowed_actions=frozenset({'query_train_tickets'}))
                    arguments={'origin':origin,'destination':destination,'travel_date':args.date,
                        'origin_scope':scope,'destination_scope':scope,'train_preference':preference,'departure_window':window}
                    result=await asyncio.to_thread(registry.execute_result,'query_train_tickets',arguments,context)
                    payload=result.payload or {}
                    trains=payload.get('trains',[])
                    expected='empty' if index==1 else 'ok'
                    valid=(result.status=='completed' and payload.get('status')==expected
                        and bool(trains)==(expected=='ok')
                        and (preference!='high_speed_only' or all(row['train_code'].startswith('G') for row in trains))
                        and (scope!='station' or all(
                            row['origin_station']==origin and row['destination_station']==destination for row in trains)))
                    results.append({'kind':'fixed_request_real_tools','request':arguments,'result':json.loads(result.to_json()),'passed':valid})
                # Two real NL decisions fit the conservative token ceiling; remaining turns use saved/fake tests.
                natural=['武汉站到南京南，上午出发的高铁有哪些？'] if args.intent_only else [f'{args.date}武汉到南京怎么坐车？','武汉站到南京南，上午出发的高铁有哪些？']
                for index,text in enumerate(natural):
                    event=ChatEvent('web','group','natural-'+str(index),'nl-'+str(index),'tester',text,occurred_at=datetime.now(timezone.utc))
                    claim=store.delivery.begin_event(event.event_key)
                    before=mcp.counts['tools_call']
                    first_result=len(read_results)
                    outcome=await asyncio.to_thread(router.route,event,claim)
                    reply=outcome[0] if isinstance(outcome,tuple) else outcome
                    receipts=read_results[first_result:]
                    passed=(isinstance(reply,str) and bool(receipts) and all(
                                r.status=='completed' and r.payload and r.payload.get('status')=='ok'
                                and r.payload.get('trains') and r.payload['request']['travel_date']==args.date
                                and all(t['train_code'].startswith(('G','D')) and t['train_code'] in reply
                                        for t in r.payload['trains']) for r in receipts)
                            if index==0 and not args.intent_only
                            else '日期' in str(reply) and not receipts and mcp.counts['tools_call']==before)
                    results.append({'kind':'natural_language_real_model_and_tools','message':text,'reply':reply,
                                    'tool_results':[json.loads(r.to_json()) for r in receipts],
                                    'tools_call_delta':mcp.counts['tools_call']-before,'passed':bool(passed)})
    except Exception as error:
        report['failure_type']=type(error).__name__
    finally:
        report.update(connection=mcp.status(),elapsed_seconds=round(time.monotonic()-started,3),tool_schemas=mcp._schemas)
        await mcp.close()
        report['summary']={'total':len(results),'passed':sum(row['passed'] for row in results)}
        report['gate_passed']=len(results)==(1 if args.intent_only else 5) and all(row['passed'] for row in results)
        text=json.dumps(report,ensure_ascii=False,indent=2)
        for secret in (config.endpoint,config.bearer_token):
            if secret:text=text.replace(secret,'[redacted]')
        args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(text+'\n',encoding='utf-8')
    return report['gate_passed']


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--authorize-live',action='store_true')
    parser.add_argument('--intent-only',action='store_true')
    parser.add_argument('--carry-model-receipt',type=Path)
    parser.add_argument('--date',default='2026-10-30')
    parser.add_argument('--max-calls',type=int,default=24)
    parser.add_argument('--seconds',type=int,default=180)
    args=parser.parse_args()
    if args.output.exists() or not 1<=args.max_calls<=24 or not 1<=args.seconds<=180:
        parser.error('Use fresh evidence and at most 24 tools/call / 180 seconds.')
    if args.authorize_live:
        passed=asyncio.run(live(args))
    else:
        sys.path.insert(0,str(ROOT/'tests'))
        from test_rail_query import RailQueryContracts, RailWorkflowTests
        suite=unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(case) for case in (RailQueryContracts,RailWorkflowTests))
        result=unittest.TextTestRunner().run(suite)
        passed=result.wasSuccessful()
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps({'mode':'offline','real_requests':0,'summary':{'total':result.testsRun,
            'passed':result.testsRun-len(result.failures)-len(result.errors)}},indent=2),encoding='utf-8')
    raise SystemExit(0 if passed else 1)


if __name__=='__main__':
    main()
