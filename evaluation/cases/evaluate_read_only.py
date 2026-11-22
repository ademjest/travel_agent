"""Offline read contracts through explicit policies and typed requests. Never loads .env."""
from evaluation.support import ROOT
import argparse
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch

def run(args):
    source=args.source_root.resolve()
    sys.path[:]=[str(source),*[p for p in sys.path if Path(p or os.getcwd()).resolve() not in {ROOT,ROOT/'scripts'}]]
    os.environ.update(AI_SUMMARY_ENABLED='false',AI_VECTOR_ENABLED='false')
    from agents.travel_agent import TravelAgent
    from app.conversation_context import AgentContext
    from tools.registry import TOOL_BY_INTENT
    from tools.travel_query import read_coverage_domains
    from core.settings import Settings
    from core.chat_transport import ChatEvent
    from core.tool_result import ToolResult
    from core.execution_outcome import TaskContext,TravelReadRequest
    from core.intent_ir import IntentIR,IntentOperation
    from infrastructure.memory_store import MemoryStore
    from infrastructure.model_gateway import estimate_input
    from app.request_router import RequestRouter
    from tools.travel_query import TravelQueryTool
    raw=args.corpus.read_bytes();corpus=json.loads(raw)
    settings=Settings('','',frozenset(),'','scripted','https://example.test','scripted')
    results=[]
    for case in corpus['cases']:
        started=time.perf_counter();calls=[];classifications=[];model_requests=[];compiler_calls=[]
        domain=case.get('domain','weather')
        operations=case.get('operations',[{'domain':domain,'operation':'search' if domain=='places' else 'query',**corpus['arguments'][domain]}])
        proposed=case.get('proposal_queries',operations)
        ir=IntentIR(1,case.get('action','read'),tuple(IntentOperation(q['domain'],q['operation'],{k:v for k,v in q.items() if k not in {'domain','operation'}}) for q in operations),source_spans=(case['message'],))
        class Compiler:
            def try_compile(self,*values,**kwargs):
                compiler_calls.append(1)
                return ir
        def read(name,values):
            calls.append({'name':name,'arguments':values})
            fixture_domain=next(d for d,tools in TOOL_BY_INTENT.items() if name in tools)
            result=deepcopy(corpus['fixtures'][fixture_domain])
            result.update(case.get('receipt_override',{}))
            for override in case.get('receipt_overrides', []):
                if override['name'] == name and override['arguments'] == values:
                    result.update(deepcopy(override['result']))
            if values.get('location'):
                result['data']=result['data'].replace('武汉',values['location'])
            if values.get('origin'):
                result['data']=result['data'].replace('武汉站',values['origin'])
            if values.get('destination'):
                result['data']=result['data'].replace('湖北省博物馆',values['destination'])
            return ToolResult(status=result.pop('status','completed'),**result)
        class Client:
            def __init__(self):self.chat=SimpleNamespace(completions=self)
            def create(self,**kwargs):
                model_requests.append(deepcopy(kwargs))
                choices=[]
                if len(model_requests)==1:
                    for i,query in enumerate(proposed):
                        tool=TOOL_BY_INTENT[query['domain']][0]
                        values={k:v for k,v in query.items() if k not in {'domain','operation'}}
                        choices.append(SimpleNamespace(id=str(i),function=SimpleNamespace(name=tool,arguments=json.dumps(values,ensure_ascii=False))))
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                    content=case.get('proposal',''),tool_calls=choices))])
        client=Client();agent=TravelAgent(settings,lambda name,values,*rest:read(name,values),client)
        context=AgentContext((),'', '', '',message_time=corpus['reference_time'])
        def counted(*values,**kwargs):
            classifications.append(1)
            return read_coverage_domains(*values,**kwargs)
        failure=None;status='failed';reply='';answer_status='not_checked';owner=None
        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            import tools.travel_query as read_module
            stack.enter_context(patch.object(read_module,'read_coverage_domains',side_effect=counted))
            try:
                if case['kind']=='agent':
                    request=TravelReadRequest.from_ir(ir,TaskContext(None,case['message'],corpus['reference_time'],{},case['message']))
                    if request is None:
                        raise ValueError('Fixture does not supply a grounded read request')
                    result=agent.run(case['message'],context,read_request=request)
                else:
                    store=MemoryStore(Path(folder)/'test.db')
                    travel=TravelQueryTool(settings);travel.amap=True
                    travel.execute_result=read
                    service=RequestRouter(store,Compiler(),travel_service=travel)
                    event=ChatEvent('onebot','group',case['id'],'evaluation','owner',case['message'],occurred_at=datetime.fromisoformat(corpus['reference_time']))
                    claim=store.delivery.begin_event(event.event_key)
                    stack.enter_context(patch.dict(os.environ,{'AI_RULE_ROUTING':'true' if case['kind']=='rule' else 'false'}))
                    if case['kind']=='command':
                        response=travel.handle(event.content)
                    else:
                        response=service.handle(event,claim)
                    if isinstance(response,str):
                        with store._connect() as db:
                            task=db.execute('SELECT status FROM travel_tasks ORDER BY rowid DESC LIMIT 1').fetchone()
                        status=task[0] if task else ('completed' if calls else 'rejected')
                        reply=response;result=None
                    elif isinstance(response,TravelReadRequest):
                        from agents.travel_agent import run_read_agent
                        result=run_read_agent(event,response,agent,context,None)
                    else:
                        result=None;status='rejected';reply='请求没有形成可执行的只读合同，本次未执行。'
                    owner='request-router-v1'
                if result is not None:
                    status=result.status;reply=result.reply;answer_status=getattr(result,'answer_status','not_checked')
            except Exception as error:
                failure=type(error).__name__
        faults=[]
        if failure:faults.append('exception:'+failure)
        for text in case.get('required',[]):
            if text not in reply:faults.append('missing:'+text)
        for text in case.get('forbidden',[]):
            if text in reply:faults.append('forbidden:'+text)
        if 'expected_tools' in case and [c['name'] for c in calls]!=case['expected_tools']:
            faults.append('tool_sequence')
        if case.get('not_completed'):
            if status=='completed':faults.append('false_completed')
        elif status!='completed':faults.append('task_not_completed')
        if any(call['name'] not in {t for d in corpus['fixtures'] for t in TOOL_BY_INTENT[d]} for call in calls):
            faults.append('unexpected_write_tool')
        results.append({'id':case['id'],'label':case['label'],'passed':not faults,'failures':faults,
            'status':status,'answer_status':answer_status,'reply':reply,'calls':calls,'owner':owner,
            'legacy_classifier_calls':0,'coverage_hint_calls':len(classifications),'semantic_compilations':len(compiler_calls),
            'scripted_model_calls':len(model_requests)+len(compiler_calls),
            'agent_input_estimate':sum(estimate_input(r.get('messages',[]))+estimate_input(r.get('tools',[])) for r in model_requests),
            'actual_usage':None,'paid_model_calls':0,'elapsed_seconds':round(time.perf_counter()-started,6)})
    summary={'total':len(results),'passed':sum(r['passed'] for r in results),
        'positive_total':sum(r['label']=='positive' for r in results),'positive_passed':sum(r['passed'] and r['label']=='positive' for r in results),
        'negative_total':sum(r['label']=='negative' for r in results),'negative_passed':sum(r['passed'] and r['label']=='negative' for r in results),
        'scripted_model_calls':sum(r['scripted_model_calls'] for r in results),
        'legacy_classifier_calls':sum(r['legacy_classifier_calls'] for r in results),
        'coverage_hint_calls':sum(r['coverage_hint_calls'] for r in results),
        'paid_model_calls':0,'actual_usage':None,'p50_seconds':statistics.median(r['elapsed_seconds'] for r in results)}
    report={'schema_version':'readonly-eval-v2','mode':'fixed_tools_scripted_model','corpus_sha256':hashlib.sha256(raw).hexdigest(),
        'source_root':str(source),'source_manifest':str(args.source_manifest) if args.source_manifest else None,
        'source_sha256':json.loads(args.source_manifest.read_text(encoding='utf-8'))['source_sha256'] if args.source_manifest else None,
        'summary':summary,'results':results}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(summary),flush=True)
    return all(r['passed'] for r in results)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root',type=Path,default=ROOT)
    parser.add_argument('--source-manifest',type=Path)
    parser.add_argument('--corpus',type=Path,default=ROOT/'evals/read_only_cases_v1.json')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    raise SystemExit(0 if run(args) else 1)
