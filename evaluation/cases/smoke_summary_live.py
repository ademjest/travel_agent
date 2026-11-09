"""Explicit real-model smoke test with persisted physical-call and token limits."""
from evaluation.support import ROOT, Budget, BoundedClient, SmokeLimit
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

sys.path.insert(0,str(ROOT))
from core.model_budget import MODEL_CALL,MODEL_EVENT,model_call_context
from core.chat_transport import ChatEvent
from core.model_pricing import pricing_config,estimate_cost
from core.settings import Settings
from core.summary_validation import render_summary
from app.conversation_context import AgentContext, ContextBuilder
from agents.travel_agent import TravelAgent
from infrastructure.memory_store import MemoryStore
from infrastructure.model_gateway import ModelGateway,estimate_input
from app.conversation_context import ConversationSummaryService
from evaluation.cases.evaluate_summary_quality import load_cases,seed_stage,capture_conditions

RUNS=('Q01','Q02','Q03','Q04','Q06','Q01-repeat-2','Q01-repeat-3')










def main(args):
    from dotenv import load_dotenv
    from infrastructure.model_client import create_model_client as OpenAI
    from core.model_policy import current_policy
    from core.summary_validation import PROMPT_VERSION, load_summary_prompt
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
        sdk=stack.enter_context(OpenAI(api_key=key,base_url=base,before_request=budget.before_request,timeout=90))
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
                store.conversations.save_turn('web:other','other','sentinel','不属于本次讨论。','占位')
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
