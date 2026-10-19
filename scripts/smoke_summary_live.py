"""Explicit real-model smoke test with persisted physical-call and token limits."""
import argparse
from contextlib import ExitStack
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlparse

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import httpx
from core.model_budget import MODEL_CALL,MODEL_EVENT,model_call_context
from core.chat_transport import ChatEvent
from core.model_pricing import pricing_config,estimate_cost
from core.settings import Settings
from core.conversation_summary import render_summary
from agents.context_builder import AgentContext,ContextBuilder
from agents.travel_agent import TravelAgent
from infrastructure.memory_store import MemoryStore
from infrastructure.model_gateway import ModelGateway,estimate_input
from services.conversation_summary_service import ConversationSummaryService
from scripts.evaluate_summary_quality import load_cases,seed_stage,capture_conditions

RUNS=('Q01','Q02','Q03','Q04','Q06','Q01-repeat-2','Q01-repeat-3')


class SmokeLimit(RuntimeError):
    pass


class Budget:
    def __init__(self,output,max_calls=12,max_tokens=100000):
        if not 1<=max_calls<=12 or not 1<=max_tokens<=100000:
            raise ValueError('Limits cannot exceed this authorized 12-call/100000-token run')
        self.output=Path(output)
        if self.output.exists():
            raise FileExistsError('Existing audit file cannot be overwritten or reset')
        self.max_calls,self.max_tokens=max_calls,max_tokens
        self.physical_calls=0
        self.tokens=0
        self.pending=None
        self.records=[]
        self.stop_reason=''
        self.report={'mode':'real-model-smoke','results':[],'started_at':datetime.now(timezone.utc).isoformat()}

    @classmethod
    def resume(cls,output):
        report=json.loads(Path(output).read_text(encoding='utf-8'))
        state=report['budget']
        records=report['physical_requests']
        if (report.get('mode')!='real-model-smoke' or state['pending_reservation'] or state['stop_reason']
                or state['physical_calls']!=len(records)
                or [r['physical_attempt'] for r in records]!=list(range(1,len(records)+1))
                or any(not r.get('usage_complete') for r in records)
                or sum(r['accounted_tokens'] for r in records)!=state['accounted_tokens']
                or not 1<=state['max_calls']<=12 or not 1<=state['max_tokens']<=100000):
            raise SmokeLimit('Audit is incomplete, unknown, inconsistent or already stopped; cannot resume')
        value=object.__new__(cls)
        value.output=Path(output)
        value.max_calls,value.max_tokens=state['max_calls'],state['max_tokens']
        value.physical_calls,value.tokens=state['physical_calls'],state['accounted_tokens']
        value.pending=None
        value.records=records
        value.stop_reason=''
        value.report=report
        return value

    def save(self):
        self.report['budget']={'max_calls':self.max_calls,'max_tokens':self.max_tokens,
            'physical_calls':self.physical_calls,'accounted_tokens':self.tokens,
            'pending_reservation':self.pending['reservation'] if self.pending else 0,
            'stop_reason':self.stop_reason}
        self.report['physical_requests']=self.records
        self.output.parent.mkdir(parents=True,exist_ok=True)
        temporary=self.output.with_suffix(self.output.suffix+'.tmp')
        temporary.write_text(json.dumps(self.report,ensure_ascii=False,indent=2),encoding='utf-8')
        temporary.replace(self.output)

    def reserve(self,kwargs):
        output=int(kwargs.get('max_completion_tokens',kwargs.get('max_tokens',8192)))
        reservation=estimate_input(kwargs.get('messages',[]))+estimate_input(kwargs.get('tools',[]))+output+512
        if self.pending is not None:
            raise SmokeLimit('Concurrent requests are not allowed')
        if self.stop_reason or self.physical_calls>=self.max_calls or self.tokens+reservation>self.max_tokens:
            self.stop_reason=self.stop_reason or ('call_limit' if self.physical_calls>=self.max_calls else 'insufficient_token_reservation')
            self.save()
            raise SmokeLimit(self.stop_reason)
        context=MODEL_CALL.get()
        self.pending={'purpose':context.purpose if context else 'unspecified','model':kwargs.get('model'),
            'event_key':MODEL_EVENT.get(),'reservation':reservation,'request':kwargs,
            'status':'reserved','started_at':datetime.now(timezone.utc).isoformat()}
        self.save()

    def dispatch(self):
        if self.pending is None or self.pending.get('physical_attempt'):
            raise SmokeLimit('Unexpected or repeated physical HTTP request blocked')
        if self.physical_calls>=self.max_calls:
            self.stop_reason='call_limit'
            raise SmokeLimit(self.stop_reason)
        self.physical_calls+=1
        self.pending['physical_attempt']=self.physical_calls
        self.pending['status']='dispatched'
        self.records.append(self.pending)
        self.save()

    def settle(self,response=None,error=None,pricing=None):
        item=self.pending
        if item is None:
            return
        usage=getattr(response,'usage',None)
        prompt=getattr(usage,'prompt_tokens',None)
        completion=getattr(usage,'completion_tokens',None)
        total=getattr(usage,'total_tokens',None)
        valid=type(prompt) is int and prompt>=0 and type(completion) is int and completion>=0
        if valid and total is None:
            total=prompt+completion
        valid=valid and type(total) is int and total>=prompt+completion
        if item.get('physical_attempt'):
            charged=total if valid else item['reservation']
            self.tokens+=charged
            details=getattr(usage,'prompt_tokens_details',None)
            cached=getattr(details,'cached_tokens',None)
            item.update({'status':'completed' if error is None else 'failed','usage_complete':valid,
                         'prompt_tokens':prompt,'completion_tokens':completion,'total_tokens':total if valid else None,
                         'accounted_tokens':charged,'cached_tokens':cached,
                         'estimated_cost':estimate_cost(prompt,cached,completion,pricing or {}) if valid else None,
                         'cost_currency':(pricing or {}).get('currency',''),'pricing':pricing or {},
                         'error_type':type(error).__name__ if error else '',
                         'finished_at':datetime.now(timezone.utc).isoformat()})
            if response is not None and response.choices:
                item['finish_reason']=response.choices[0].finish_reason
                item['content']=response.choices[0].message.content
            if not valid:
                self.stop_reason='unknown_usage_stop'
            elif self.tokens>=self.max_tokens:
                self.stop_reason='token_limit'
            elif self.physical_calls>=self.max_calls:
                self.stop_reason='call_limit'
        self.pending=None
        self.save()


class CountingTransport(httpx.BaseTransport):
    def __init__(self,budget,inner=None):
        self.budget=budget
        self.inner=inner or httpx.HTTPTransport(retries=0)

    def handle_request(self,request):
        if request.method!='POST' or not request.url.path.endswith('/chat/completions'):
            raise SmokeLimit('Only the configured chat-completion endpoint is allowed')
        self.budget.dispatch()
        return self.inner.handle_request(request)

    def close(self):
        self.inner.close()


class BoundedClient:
    def __init__(self,sdk,budget):
        self.sdk,self.budget=sdk,budget
        self.calls=[]
        self.chat=SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def close(self):
        self.sdk.close()

    def create(self,**kwargs):
        prices=pricing_config(kwargs.get('model'),os.getenv('LLM_MODEL_ID',''))
        self.budget.reserve(kwargs)
        self.calls.append(json.loads(json.dumps(kwargs)))
        started=time.monotonic()
        response=None
        error=None
        try:
            response=self.sdk.chat.completions.create(**kwargs)
            return response
        except BaseException as failure:
            error=failure
            raise
        finally:
            if self.budget.pending:
                self.budget.pending['elapsed_seconds']=round(time.monotonic()-started,3)
            self.budget.settle(response,error,prices)


def main(args):
    from dotenv import load_dotenv
    from openai import OpenAI
    from core.model_policy import current_policy
    from core.summary_contract import PROMPT_VERSION, load_summary_prompt
    answer_case=getattr(args,'answer_case','')
    budget=Budget.resume(args.output) if answer_case else Budget(args.output,args.max_calls,args.max_tokens)
    if (budget.max_calls,budget.max_tokens)!=(args.max_calls,args.max_tokens):
        raise SmokeLimit('Resume cannot change the authorized budget')
    load_dotenv(ROOT/'.env',override=False)
    load_dotenv(ROOT.parent/'.env',override=False)
    key=os.getenv('LLM_API_KEY','').strip()
    base=os.getenv('LLM_BASE_URL','').strip()
    model=os.getenv('LLM_MODEL_ID','').strip()
    if not key or not base or not model:
        raise ValueError('Configured model key, base URL and model ID are required')
    corpus_path=ROOT/'evals/summary_quality_cases_v2.json'
    corpus=load_cases(corpus_path)
    if answer_case and (budget.report['model']!=model or budget.report['summary_model']!=(os.getenv('LLM_SUMMARY_MODEL_ID','').strip() or model)
                        or budget.report['provider_host']!=urlparse(base).hostname
                        or budget.report['corpus_hash']!=hashlib.sha256(corpus_path.read_bytes()).hexdigest()):
        raise SmokeLimit('Resume must preserve model, endpoint and corpus')
    summary_model=os.getenv('LLM_SUMMARY_MODEL_ID','').strip() or model
    tier=current_policy().tier
    prompt_hash=load_summary_prompt()[1]
    if answer_case and (budget.report['budget_tier']!=tier
                        or budget.report.get('summary_prompt_hash',prompt_hash)!=prompt_hash):
        raise SmokeLimit('Resume must preserve budget tier and summary prompt')
    budget.report.update({'model':model,'summary_model':summary_model,
        'provider_host':urlparse(base).hostname,'budget_tier':tier,'sdk_retries':0,'transport_retries':0,
        'summary_prompt_version':PROMPT_VERSION,'summary_prompt_hash':prompt_hash,
        'corpus_hash':hashlib.sha256(corpus_path.read_bytes()).hexdigest(),
        'pricing_profiles':{name:pricing_config(name,model) for name in {model,summary_model}},
        'note':'Current production prompts and budgets; synthetic user materials only; no production DB or external tools. Unknown usage halts further calls.'})
    budget.save()
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ,{'AI_SUMMARY_ENABLED':'true','AI_BUDGET_TIER':tier,'AI_VECTOR_ENABLED':'false'}))
        http=stack.enter_context(httpx.Client(transport=CountingTransport(budget),follow_redirects=False,timeout=90))
        sdk=stack.enter_context(OpenAI(api_key=key,base_url=base,max_retries=0,http_client=http,timeout=90))
        client=BoundedClient(sdk,budget)
        if answer_case:
            answer_from_published(args,budget,client,corpus,model,base)
            budget.report['finished_at']=datetime.now(timezone.utc).isoformat()
            budget.save()
            print(json.dumps(budget.report['budget'],ensure_ascii=False),flush=True)
            return
        with tempfile.TemporaryDirectory(prefix='summary-live-') as directory:
            for case_id in RUNS:
                if budget.stop_reason:
                    break
                base_case=case_id.split('-repeat-')[0]
                case=next(case for case in corpus['cases'] if case['id']==base_case)
                store=MemoryStore(Path(directory)/(case_id+'.db'))
                store.save_turn('web:other','other','sentinel','不属于本次讨论。','占位')
                event=ChatEvent('web','group',case_id,case_id,'test-owner',corpus['recall'])
                gateway=ModelGateway(store,client)
                diagnostics=[]
                service=ConversationSummaryService(store.conversation_summaries,gateway,model,diagnostics.append)
                mapping={}
                result={'id':case_id,'stages':[],'diagnostics':diagnostics,'source_mapping':mapping}
                budget.report['results'].append(result)
                token=MODEL_EVENT.set('summary-live:'+case_id)
                record=None
                try:
                    for stage in case['stages']:
                        seed_stage(store,event,stage,corpus,mapping)  # returned gold is NOT sent to the SDK
                        target=max(mapping.values())
                        record=service.ensure(event)
                        reached=bool(record and record['covered_turn_id']>=target)
                        result['stages'].append({'id':stage['id'],'published_current':reached,
                            'record':record,'target_turn_id':target,'user_chars':sum(len(t['user']) for t in stage['turns'])})
                        budget.save()
                        if not reached or budget.stop_reason:
                            if diagnostics and diagnostics[-1].get('error_type')=='ModelBudgetExceeded':
                                budget.stop_reason='application_budget_blocked'
                            break
                    if case_id=='Q01' and record and not budget.stop_reason:
                        settings=Settings('','',frozenset(),'','configured',base,model)
                        result['context_conditions']=capture_conditions(store,event,service,client,gateway,corpus,record,
                                                                       settings=settings,real=True)
                        if not budget.stop_reason:
                            request=result['context_conditions']['requests']['summary_only']['request']
                            messages=json.loads(json.dumps(request['messages']))
                            messages[-1]['content']=corpus['application']
                            with model_call_context('tool_reasoning',prompt_version='travel-agent-v2',
                                                    context_policy_version='travel-context-v2'):
                                reply=gateway.chat.completions.create(model=model,messages=messages)
                            result['application_summary_only']={'content':reply.choices[0].message.content,
                                                                'finish_reason':reply.choices[0].finish_reason}
                except Exception as error:
                    result['error_type']=type(error).__name__
                finally:
                    MODEL_EVENT.reset(token)
                    budget.save()
                print(json.dumps({'case':case_id,'physical_calls':budget.physical_calls,'tokens':budget.tokens,
                                  'published':[s['published_current'] for s in result['stages']],
                                  'stop_reason':budget.stop_reason},ensure_ascii=False),flush=True)
    budget.report['finished_at']=datetime.now(timezone.utc).isoformat()
    budget.report['skipped_cases']=[case for case in RUNS if case not in {r['id'] for r in budget.report['results']}]
    budget.save()
    print(json.dumps(budget.report['budget'],ensure_ascii=False),flush=True)


def answer_from_published(args,budget,client,corpus,model,base):
    case=next(case for case in corpus['cases'] if case['id']==args.answer_case)
    result=next(result for result in budget.report['results'] if result['id']==args.answer_case)
    stage=result['stages'][-1]
    if not stage['published_current'] or not stage['record']:
        raise SmokeLimit('Only an actually published current snapshot may be used')
    record=stage['record']
    common={'recent_dialogue':(),'group_context':'','document_context':'',
            'source_note':ContextBuilder._source_note('web'),'message_time':datetime.now(timezone.utc).isoformat()}
    contexts={'full_original':AgentContext(**{**common,'group_context':'\n'.join(t['user'] for s in case['stages'] for t in s['turns'])}),
              'summary_only':AgentContext(**common,conversation_summary=render_summary(json.loads(record['items_json']),1800)),
              'no_summary':AgentContext(**common)}
    saved={'based_on_case':args.answer_case,'summary_version':record['version'],'conditions':{}}
    budget.report['answer_comparison']=saved
    with tempfile.TemporaryDirectory(prefix='summary-live-answer-') as directory:
        gateway=ModelGateway(MemoryStore(Path(directory)/'answers.db'),client)
        settings=Settings('','',frozenset(),'','configured',base,model)
        agent=TravelAgent(settings,lambda *args,**kwargs: (_ for _ in ()).throw(AssertionError('External tool forbidden')),gateway)
        token=MODEL_EVENT.set('summary-live:answer-comparison')
        try:
            for name,context in contexts.items():
                if budget.stop_reason:
                    break
                reply=agent.run(corpus['recall'],context)
                saved['conditions'][name]={'reply':reply.reply,'status':reply.status,'request':client.calls[-1]}
                budget.save()
                print(json.dumps({'condition':name,'physical_calls':budget.physical_calls,'tokens':budget.tokens},ensure_ascii=False),flush=True)
            if not budget.stop_reason:
                reply=agent.run(corpus['application'],contexts['summary_only'])
                saved['application_summary_only']={'reply':reply.reply,'status':reply.status,'request':client.calls[-1]}
                budget.save()
        finally:
            MODEL_EVENT.reset(token)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--max-calls',type=int,default=12)
    parser.add_argument('--max-tokens',type=int,default=100000)
    parser.add_argument('--answer-case',default='',help='Continue an existing fully accounted audit using a published snapshot; never resets limits')
    main(parser.parse_args())
