"""Forty distinct production-assembly state scenarios complement the 120 legacy cases."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.runtime_factory import build_runtime
from core.chat_transport import ChatEvent
from core.settings import Settings
from core.tool_result import ToolResult
from infrastructure.memory_store import MemoryStore
from infrastructure.model_gateway import usage_summary

DOMAINS={
    'weather':{'location':'武汉'},'forecast':{'location':'武汉'},
    'route':{'origin':'武汉站','destination':'湖北省博物馆'},
    'traffic':{'origin':'武汉站','destination':'湖北省博物馆'},
    'walking':{'origin':'武汉站','destination':'江汉路'},
    'transit':{'origin':'武汉站','destination':'江汉路','city':'武汉'},
    'places':{'city':'武汉','keywords':'美术馆'},
}
WORDS={'weather':'天气','forecast':'天气预报','route':'驾车路线','traffic':'路况','walking':'步行','transit':'地铁','places':'场所'}
TOOL={'weather':'get_current_weather','forecast':'get_weather_forecast','route':'get_driving_route',
      'traffic':'get_route_traffic','walking':'get_walking_route','transit':'get_transit_route','places':'search_travel_places'}


class FixtureModel:
    def __init__(self):
        self.ir={}
        self.chat=SimpleNamespace(completions=SimpleNamespace(create=self.create))
    def create(self,**kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(self.ir,ensure_ascii=False),tool_calls=[]))])


class Transport:
    def __init__(self):
        self.messages=[]
    async def send(self,message):
        self.messages.append(message)
        return str(len(self.messages))


def fixtures():
    cases=[]
    for domain in DOMAINS:
        cases.append((domain+'-complete',domain,'complete',''))
        for field in DOMAINS[domain]:
            cases.append((domain+'-missing-'+field,domain,'missing',field))
        cases.append((domain+'-unsupported',domain,'unsupported',''))
        cases.append((domain+'-replay',domain,'replay',''))
    for domain in ('weather','route','places'):
        cases.append((domain+'-forged-owner',domain,'forged',''))
    cases.extend([('weather-cancel','weather','cancel',''),('weather-restart','weather','restart',''),
                  ('weather-topic-switch','weather','switch','')])
    return cases


async def evaluate(case):
    name,domain,mode,field=case
    with tempfile.TemporaryDirectory() as folder:
        path=Path(folder)/'eval.db'
        store=MemoryStore(path)
        model=FixtureModel()
        transport=Transport()
        calls=[]
        def build(store):
            runtime=build_runtime(Settings('','',frozenset(),'','fixture','https://example.test','fixture'),
                platform='web',store=store,client=model,transport=transport,
                reply_renderer=SimpleNamespace(render=lambda c,u,r:{'text':r,'kind':'reply'}),
                group_allowed=lambda _:True,semantic_mode='execute')
            runtime.travel_service.execute_result=lambda tool,args: calls.append((tool,args)) or ToolResult('completed',tool,data='固定快照')
            return runtime
        runtime=build(store)
        arguments=dict(DOMAINS[domain])
        if mode=='missing':
            arguments.pop(field)
        if mode=='unsupported':
            arguments['extra']='不支持字段'
        if mode=='forged':
            arguments['owner_id']='other'
        if mode in {'cancel','restart','switch'}:
            arguments={}
        text='请了解'+WORDS[domain]+' '+ ' '.join(str(v) for v in arguments.values())
        def ir(values,action='read'):
            return {'schema_version':1,'action':action,
                    'operations':[{'domain':domain,'operation':'search' if domain=='places' else 'query',**values}],
                    'source_spans':[WORDS[domain]],'missing_fields':[key for key in DOMAINS[domain] if key not in values]}
        model.ir=ir(arguments,'clarify' if mode in {'missing','cancel','restart','switch'} else 'read')
        event=ChatEvent('web','group','first','conversation','owner',text,occurred_at=datetime.now(timezone.utc))
        await runtime.application.handle(event)
        errors=[]
        if mode in {'complete','replay'} and calls != [(TOOL[domain],DOMAINS[domain])]:
            errors.append('wrong query parameters')
        if mode=='replay':
            await runtime.application.handle(event)
            if len(calls)!=1:
                errors.append('replayed query executed twice')
        if mode in {'missing','cancel','restart','switch'}:
            tasks=store.tasks.recent('web','web:conversation','owner')
            if not tasks or tasks[0].status!='collecting':
                errors.append('missing persistent clarification')
            if mode=='cancel':
                with store._connect() as db:
                    db.execute("UPDATE travel_tasks SET status='cancelled'")
                model.ir={'schema_version':1,'action':'none','operations':[]}
                await runtime.application.handle(ChatEvent('web','group','next','conversation','owner','武汉'))
            if mode=='restart':
                store=MemoryStore(path)
                runtime=build(store)
                model.ir=ir({'location':'武汉'})
                model.ir['source_spans']=['武汉']
                await runtime.application.handle(ChatEvent('web','group','next','conversation','owner','武汉'))
                if not calls or calls[-1] != (TOOL['weather'],{'location':'武汉'}):
                    errors.append('restart lost continuation')
            if mode=='switch':
                await runtime.application.handle(ChatEvent('web','group','next','conversation','owner','你好'))
        if mode not in {'complete','replay','restart'} and calls:
            errors.append('query executed without valid parameters')
        if store.tasks.recent('web','web:conversation','other'):
            errors.append('cross-owner task leak')
        return {'id':name,'passed':not errors,'errors':errors,'usage':usage_summary(store)}


async def main(output):
    env={'AI_SUMMARY_ENABLED':'false','AI_VECTOR_ENABLED':'false','AI_DAILY_COST_LIMIT':'0','AI_MONTHLY_COST_LIMIT':'0',
         'AI_DAILY_TOKEN_LIMIT':'0','AI_MONTHLY_TOKEN_LIMIT':'0','LLM_INTENT_MODEL_ID':'','LLM_MODEL_PRICES':''}
    with patch.dict(os.environ,env):
        results=[]
        for case in fixtures():
            result=await evaluate(case)
            results.append(result)
            print(result['id'], 'PASS' if result['passed'] else result['errors'],flush=True)
    report={'runtime_contract':'build_runtime-v3-complete','mode':'scripted-production-state',
            'total':len(results),'passed':sum(r['passed'] for r in results),'results':results}
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    if report['passed']!=report['total']:
        raise SystemExit(1)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,default=ROOT/'evals/v3-state-flows.json')
    args=parser.parse_args()
    asyncio.run(main(args.output))
