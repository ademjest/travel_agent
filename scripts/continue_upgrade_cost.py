"""Append paired cost observations to the same approved second-run budget."""
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
from core.settings import Settings
from core.conversation_summary import render_summary
from core.model_budget import MODEL_EVENT
from infrastructure.memory_store import MemoryStore,ConversationTurn
from infrastructure.model_gateway import ModelGateway
from scripts.smoke_summary_live import Budget,BoundedClient,CountingTransport


def main():
    import httpx
    from openai import OpenAI
    from dotenv import load_dotenv
    authorization=json.loads((ROOT/'evals/next-upgrade-token-authorization-2026-10-18.json').read_text(encoding='utf-8'))
    first=Path(authorization['first_audit'])
    if hashlib.sha256(first.read_bytes()).hexdigest()!=authorization['first_sha256']:
        raise ValueError('Parent audit changed')
    budget=Budget.resume(Path(authorization['second_audit']))
    if budget.max_tokens+authorization['first_tokens']!=100000 or 'cost_experiment' in budget.report:
        raise ValueError('Cannot reset, branch, or expand approved budget')
    parent=json.loads(first.read_text(encoding='utf-8'))
    web=next(r for r in parent['results'] if r['id']=='web-original-new-generation')
    record=web['correction']['record']
    with sqlite3.connect(f"file:{Path(parent['database_root']).as_posix()}/web-original/web/travel_bot.db?mode=ro",uri=True) as db:
        rows=db.execute('SELECT user_content,assistant_content,created_at FROM conversation_turns WHERE group_openid=? AND member_openid=? AND id<=? ORDER BY id',
                        (record['scope_id'],record['owner_id'],record['covered_turn_id'])).fetchall()
    question=json.loads((ROOT/'evals/summary_quality_cases_v2.json').read_text(encoding='utf-8'))['recall']
    conditions={'full':AgentContext(tuple(ConversationTurn(*r) for r in rows),'','','隔离固定原文对照，仅作为事实材料。'),
                'summary':AgentContext((),'','','隔离固定原文对照，仅作为事实材料。',conversation_summary=render_summary(json.loads(record['items_json']),1800))}
    load_dotenv(ROOT/'.env',override=False);load_dotenv(ROOT.parent/'.env',override=False)
    key,base=os.getenv('LLM_API_KEY',''),os.getenv('LLM_BASE_URL','')
    results=budget.report['cost_experiment']=[]
    budget.save()
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ,{'LLM_MODEL_ID':'gpt-5.6-sol','AI_VECTOR_ENABLED':'false'}))
        http=stack.enter_context(httpx.Client(transport=CountingTransport(budget),follow_redirects=False,timeout=90))
        sdk=stack.enter_context(OpenAI(api_key=key,base_url=base,max_retries=0,http_client=http,timeout=90))
        bounded=BoundedClient(sdk,budget)
        folder=stack.enter_context(TemporaryDirectory(prefix='paired-cost-'))
        store=MemoryStore(Path(folder)/'test.db')
        agent=TravelAgent(Settings('','',frozenset(),'',key,base,'gpt-5.6-sol'),lambda *a:(_ for _ in ()).throw(AssertionError('No tools')),ModelGateway(store,bounded))
        for i,name in enumerate(('full','summary','summary','full')):
            if budget.stop_reason:
                break
            result={'condition':name,'sample':i+1,'mode':'counterfactual-fixed-input-not-production-window','request_start':budget.physical_calls+1}
            results.append(result)
            token=MODEL_EVENT.set(f'cost-pair:{i+1}:{name}')
            try:
                result['answer']=agent.run(question,conditions[name]).reply
            except Exception as error:
                result['failure_type']=type(error).__name__
            finally:
                MODEL_EVENT.reset(token)
                result['request_end']=budget.physical_calls
                budget.save()
    print(json.dumps(budget.report['budget']))


if __name__=='__main__':
    main()
