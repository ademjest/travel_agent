"""2026-10-18 approved gpt-5.6-sol acceptance, bounded to 100000 tokens total."""
import argparse
from contextlib import ExitStack
import json
import hashlib
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from agents.context_builder import ContextBuilder
from agents.travel_agent import TravelAgent
from core.chat_transport import ChatEvent
from core.model_budget import MODEL_EVENT
from core.settings import Settings
from core.summary_contract import load_summary_prompt
from infrastructure.memory_store import MemoryStore
from infrastructure.model_gateway import ModelGateway
from scripts.evaluate_summary_quality import load_cases,seed_stage,ScriptedClient
from scripts.smoke_summary_live import Budget,BoundedClient,CountingTransport
from scripts.smoke_summary_web import web_flow
from services.conversation_summary_service import ConversationSummaryService
from services.research_service import ResearchService


def main(args):
    from dotenv import load_dotenv
    import httpx
    from openai import OpenAI
    load_dotenv(ROOT/'.env',override=False)
    load_dotenv(ROOT.parent/'.env',override=False)
    continuation=getattr(args,'continuation',False)
    token_limit=100000
    if continuation:
        parent=ROOT/'evals/next-upgrade-live-2026-10-18.json'
        previous=json.loads(parent.read_text(encoding='utf-8'))
        state=previous['budget']
        if (state['pending_reservation'] or any(not c.get('usage_complete') for c in previous['physical_requests'])
                or sum(c['accounted_tokens'] for c in previous['physical_requests'])!=state['accounted_tokens']
                or args.output.resolve()!=(ROOT/'evals/next-upgrade-live-round2-2026-10-18.json').resolve()):
            raise ValueError('Continuation must preserve a settled parent and its fixed child audit')
        token_limit-=state['accounted_tokens']
        authorization={'authorized_total_tokens':100000,'first_audit':str(parent),
                       'first_sha256':hashlib.sha256(parent.read_bytes()).hexdigest(),
                       'first_tokens':state['accounted_tokens'],'second_audit':str(args.output),
                       'second_max_tokens':token_limit,'only_two_serial_runs':True}
        with (ROOT/'evals/next-upgrade-token-authorization-2026-10-18.json').open('x',encoding='utf-8') as f:
            json.dump(authorization,f,ensure_ascii=False,indent=2)
    budget=Budget(args.output,12,token_limit)
    key,base=os.getenv('LLM_API_KEY',''),os.getenv('LLM_BASE_URL','')
    if not key or not base:
        raise ValueError('Configured connection required')
    corpus=load_cases(ROOT/'evals/summary_quality_cases_v2.json')
    root=ROOT/('.upgrade-testing/20261018-live-round2' if continuation else '.upgrade-testing/20261018-live')
    if root.exists():
        raise FileExistsError('Preserved test databases must not be reset')
    root.mkdir(parents=True)
    model='gpt-5.6-sol'
    budget.report.update(model=model,authorization='User 2026-10-18: 100000 tokens total',
                         summary_prompt_hash=load_summary_prompt()[1],database_root=str(root),
                         production_database_accessed=False,results=[])
    budget.save()
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ,{'LLM_MODEL_ID':model,'LLM_INTENT_MODEL_ID':model,
            'LLM_SUMMARY_MODEL_ID':model,'LLM_RESEARCH_MODEL_ID':model,'AI_SUMMARY_ENABLED':'true',
            'AI_VECTOR_ENABLED':'false','AI_DAILY_TOKEN_LIMIT':'0','AI_MONTHLY_TOKEN_LIMIT':'0',
            'AI_DAILY_COST_LIMIT':'0','AI_MONTHLY_COST_LIMIT':'0','SEARCH_API_KEY':''}))
        stack.enter_context(patch('requests.sessions.Session.request',side_effect=AssertionError('External tools forbidden')))
        stack.enter_context(patch('infrastructure.public_http.request_public',side_effect=AssertionError('External tools forbidden')))
        http=stack.enter_context(httpx.Client(transport=CountingTransport(budget),follow_redirects=False,timeout=90))
        sdk=stack.enter_context(OpenAI(api_key=key,base_url=base,max_retries=0,http_client=http,timeout=90))
        bounded=BoundedClient(sdk,budget)
        shared=SimpleNamespace(chat=bounded.chat,calls=bounded.calls,close=lambda:None)
        settings=Settings('','',frozenset(),'',key,base,model)
        def run(name,operation):
            if budget.stop_reason:
                return
            result={'id':name,'first_physical_request':budget.physical_calls+1}
            budget.report['results'].append(result)
            token=MODEL_EVENT.set('next-upgrade:'+name)
            try:
                operation(result)
            except Exception as error:
                result['failure_type']=type(error).__name__
            finally:
                result['last_physical_request']=budget.physical_calls
                MODEL_EVENT.reset(token)
                budget.save()
        def q04(result):
            case=next(c for c in corpus['cases'] if c['id']=='Q04')
            store=MemoryStore(root/'q04.db')
            event=ChatEvent('web','group','q04','q04','test-owner',corpus['recall'])
            seed_stage(store,event,case['stages'][0],corpus,{})
            diagnostics=[]
            result['record']=ConversationSummaryService(store.conversation_summaries,ModelGateway(store,bounded),model,diagnostics.append).ensure(event)
            result['diagnostics']=diagnostics
        run('q04-new-generation',q04)
        run('web-original-new-generation',lambda r:web_flow(shared,settings,corpus,root/'web-original',r,budget.save))
        def stale(result):
            stages=next(c['stages'] for c in corpus['cases'] if c['id']=='Q02')
            store=MemoryStore(root/'stale.db')
            event=ChatEvent('web','group','recall','stale','test-owner',corpus['recall'])
            fake=ScriptedClient();mapping={}
            fake.items=seed_stage(store,event,stages[0],corpus,mapping)
            old=ConversationSummaryService(store.conversation_summaries,fake,'scripted-initial').ensure(event)
            seed_stage(store,event,stages[1],corpus,mapping)
            for i in range(20):
                store.save_turn(event.storage_scope_id,event.sender_id,f'aging-{i}','ping','pong')
                store.save_chat_message(f'aging-chat-{i}','web',event.scope_id,event.sender_id,f'aging-{i}','','user','ping')
            context=ContextBuilder(store,SimpleNamespace(ensure=lambda *a,**k:old)).build(event)
            result.update(initial_summary='scripted fixture',forced_failure='old record returned without a new summary call',
                pending_context=context.summary_pending_context,incomplete=context.summary_information_incomplete,
                answer=TravelAgent(settings,lambda *a:(_ for _ in ()).throw(AssertionError('No tools')),ModelGateway(store,bounded)).run(event.content,context).reply)
        run('failed-increment-real-answer',stale)
        def research(result):
            store=MemoryStore(root/'research.db')
            sources=[{'id':1,'url':'https://example.test/museum','title':'虚构场馆测试资料','status':'read',
                'published_at':'','retrieved_at':'2026-10-18','text':'虚构场馆周一闭馆，周二至周日开放。门票为每人每次60元人民币。无需提前预约，但入馆仍需登记。'}]
            items=[]
            result['report']=ResearchService(store,None,ModelGateway(store,bounded),model)._summarize(
                '根据资料核对开放、门票和预约，不扩展其他事实。',sources,evidence_sink=items)
            result['validated_items']=items
            result['mode']='real-model-fixed-synthetic-source-not-live-web'
        run('research-evidence-real-model',research)
        def document_qa(result):
            from infrastructure.local_embedding import configure_local_index
            source_cases=json.loads((ROOT/'evals/nps_retrieval_cases_v1.json').read_text(encoding='utf-8'))['cases']
            case=next(c for c in source_cases if c['id']==('nps-yose-4' if continuation else 'nps-zion-4'))
            store=MemoryStore(root/'real-docs.db')
            event=ChatEvent('web','group','qa','real-docs','test-owner',case['query'])
            for doc in case['documents']:
                store.add_document(event.storage_scope_id,event.sender_id,doc['filename'],doc['filename'],doc['content'],doc['chunks'])
            with patch.dict(os.environ,{'AI_VECTOR_ENABLED':'true','AI_EMBEDDING_MODEL_DIR':str(ROOT/'.models/bge-small-zh-v1.5-onnx')}):
                configure_local_index(store)
                if store.semantic_index is None:
                    raise RuntimeError('Local dependencies not available')
                store.semantic_index.update(event.storage_scope_id)
                context=ContextBuilder(store).build(event,purpose='document_qa')
                result['actual_document_context']=context.document_context
                result['answer']=TravelAgent(settings,lambda *a:(_ for _ in ()).throw(AssertionError('No tools')),
                                               ModelGateway(store,bounded)).run(event.content,context).reply
                result['mode']='real-local-embedding-and-real-answer-on-frozen-public-source-excerpts'
                result['question']=event.content
        run('public-document-no-answer',document_qa)
    print(json.dumps(budget.report['budget'],ensure_ascii=False))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--continuation',action='store_true',help='One serial continuation limited to the unused part of the same 100000-token approval')
    main(p.parse_args())
