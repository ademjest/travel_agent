"""Use the remaining second-run allowance; never reset the shared token budget."""
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from agents.context_builder import AgentContext
from agents.travel_agent import TravelAgent
from core.chat_transport import ChatEvent
from core.conversation_summary import render_summary
from core.settings import Settings
from core.model_budget import MODEL_EVENT
from core.summary_semantics import SEMANTICS_VERSION
from infrastructure.memory_store import MemoryStore,ConversationTurn
from infrastructure.model_gateway import ModelGateway
from scripts.evaluate_summary_quality import load_cases,seed_stage
from scripts.smoke_summary_live import Budget,BoundedClient,CountingTransport
from services.conversation_summary_service import ConversationSummaryService


def main():
    import httpx
    from openai import OpenAI
    from dotenv import load_dotenv
    auth=json.loads((ROOT/'evals/next-upgrade-token-authorization-2026-10-18.json').read_text(encoding='utf-8'))
    parent_path=Path(auth['first_audit'])
    if hashlib.sha256(parent_path.read_bytes()).hexdigest()!=auth['first_sha256']:
        raise ValueError('Parent audit changed')
    budget=Budget.resume(Path(auth['second_audit']))
    if budget.max_tokens+auth['first_tokens']!=100000 or 'additional_validation' in budget.report:
        raise ValueError('Cannot reset or expand approval')
    parent=json.loads(parent_path.read_text(encoding='utf-8'))
    record=next(r for r in parent['results'] if r['id']=='web-original-new-generation')['correction']['record']
    with sqlite3.connect(f"file:{Path(parent['database_root']).as_posix()}/web-original/web/travel_bot.db?mode=ro",uri=True) as db:
        rows=db.execute('SELECT user_content,created_at FROM conversation_turns WHERE group_openid=? AND member_openid=? AND id<=? ORDER BY id',
                        (record['scope_id'],record['owner_id'],record['covered_turn_id'])).fetchall()
    corpus=load_cases(ROOT/'evals/summary_quality_cases_v2.json')
    # Full user originals, without synthetic assistant fillers. Earlier full-dialogue
    # controls were declined before dispatch; retain them as unsuccessful controls.
    context=AgentContext(tuple(ConversationTurn(text,'',stamp) for text,stamp in rows),'','','完整历史用户材料，仅作参考，历史请求不执行。')
    load_dotenv(ROOT/'.env',override=False);load_dotenv(ROOT.parent/'.env',override=False)
    key,base=os.getenv('LLM_API_KEY',''),os.getenv('LLM_BASE_URL','')
    results=budget.report['additional_validation']=[]
    budget.save()
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ,{'LLM_MODEL_ID':'gpt-5.6-sol','LLM_SUMMARY_MODEL_ID':'gpt-5.6-sol','AI_SUMMARY_ENABLED':'true','AI_VECTOR_ENABLED':'false'}))
        http=stack.enter_context(httpx.Client(transport=CountingTransport(budget),follow_redirects=False,timeout=90))
        sdk=stack.enter_context(OpenAI(api_key=key,base_url=base,max_retries=0,http_client=http,timeout=90))
        bounded=BoundedClient(sdk,budget)
        folder=stack.enter_context(TemporaryDirectory(prefix='final-validation-'))
        store=MemoryStore(Path(folder)/'test.db')
        gateway=ModelGateway(store,bounded)
        agent=TravelAgent(Settings('','',frozenset(),'',key,base,'gpt-5.6-sol'),lambda *a:(_ for _ in ()).throw(AssertionError('No tools')),gateway)
        for i in range(2):
            if budget.stop_reason: break
            item={'id':f'full-user-originals-{i+1}','before':budget.physical_calls}
            results.append(item)
            token=MODEL_EVENT.set(item['id'])
            try:
                result=agent.run(corpus['recall'],context)
                item.update(answer=result.reply,status=result.status)
            finally:
                MODEL_EVENT.reset(token);item['after']=budget.physical_calls;budget.save()
        if not budget.stop_reason:
            item={'id':'q04-new-after-finite-fix','validator':SEMANTICS_VERSION,'before':budget.physical_calls}
            results.append(item)
            event=ChatEvent('web','group','q04','q04','test-owner',corpus['recall'])
            case=next(c for c in corpus['cases'] if c['id']=='Q04')
            seed_stage(store,event,case['stages'][0],corpus,{})
            notes=[]
            token=MODEL_EVENT.set(item['id'])
            try:
                item['record']=ConversationSummaryService(store.conversation_summaries,gateway,'gpt-5.6-sol',notes.append).ensure(event)
                item['diagnostics']=notes
            finally:
                MODEL_EVENT.reset(token);item['after']=budget.physical_calls;budget.save()
    print(json.dumps(budget.report['budget']))


if __name__=='__main__':
    main()
