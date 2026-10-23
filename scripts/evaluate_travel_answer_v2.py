"""User-authorized three-tier writing sample; never calls a research supplier.

Uses saved public travel material, a separate research DB and the existing Web
model ledger so current daily/monthly limits still apply. Each tier/event can
reserve only one attempt, including after a process restart.
"""
import json
import os
from pathlib import Path
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from dotenv import load_dotenv
from openai import OpenAI
from core.settings import Settings
from core.data_paths import log_directory
from core.chat_transport import ChatEvent
from core.model_budget import MODEL_EVENT
from infrastructure.env_settings_store import EnvSettingsStore
from infrastructure.memory_store import MemoryStore
from infrastructure.model_gateway import ModelGateway
from services.research_service import ResearchService
from services.travel_answer_writer import write_answer


def main():
    config=EnvSettingsStore(ROOT)
    load_dotenv(ROOT/'.env',override=False)
    config.apply_web_overrides()
    settings=Settings.from_env()
    if not settings.llm_configured:
        raise SystemExit('Configured model unavailable; no requests made.')
    output=log_directory()/'travel-answer-v2-samples'
    output.mkdir(parents=True,exist_ok=True)
    sample_store=MemoryStore(ROOT/'.upgrade-testing/travel-answer-v2-live/travel_bot.db')
    ledger=MemoryStore(ROOT/'data/web/travel_bot.db')
    physical=0
    current_tier=''
    client=OpenAI(api_key=settings.llm_api_key,base_url=settings.llm_base_url,max_retries=0,timeout=90)
    def transport(**kwargs):
        nonlocal physical
        if physical>=3:
            raise ValueError('Three-request authorization exhausted.')
        physical+=1
        result=client.chat.completions.create(**kwargs)
        (output/f'{current_tier}-raw.json').write_text(result.model_dump_json(indent=2),encoding='utf-8')
        return result
    gateway=ModelGateway(ledger,SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=transport))),default_model=settings.llm_model_id)
    service=ResearchService(sample_store,None,client=gateway,model=settings.llm_model_id)
    reports=[]
    try:
        for tier in ('economy','balanced','quality'):
            current_tier=tier
            os.environ['AI_BUDGET_TIER']=tier
            event=ChatEvent('onebot','group','authorized-v2-'+tier,'travel-answer-v2-evaluation','local-evaluation',
                '请丰富介绍已保存的南京两日行程中每个景点的文化背景、观赏重点、玩法与实用信息。遵守行程顺序；日期仍暂定，不修改行程。',
                occurred_at=datetime.now(timezone.utc))
            data=sample_store.research.get(event)
            if data is None:
                data=json.loads((ROOT/'tests/fixtures/wendao_nanjing_six_2026-10-22.json').read_text(encoding='utf-8'))
                data['query']=event.content
                sample_store.research.save(event,'partial',data)
            token=MODEL_EVENT.set(event.event_key)
            try:
                text=write_answer(service,event,data)
            finally:
                MODEL_EVENT.reset(token)
            saved=sample_store.research.get(event)['answer_write']
            title=f'# {tier} 档真实模型样例\n\n基于已保存资料；非新的景区事实核验。\n\n'
            (output/f'{tier}.md').write_text(title+text+'\n',encoding='utf-8')
            report={'tier':tier,'status':saved['status'],'model':saved.get('model'),
                    'elapsed_ms':saved.get('elapsed_ms'),'input_estimate':saved.get('input_estimate'),
                    'usage':saved.get('usage'),'error_type':saved.get('error_type'),
                    'generation':saved['result']['generation'],'degradations':saved['result']['degradations'],
                    'characters':len(text)}
            reports.append(report)
            (output/'metrics.json').write_text(json.dumps({'authorization':'Three writing calls, no supplier requests',
                'physical_calls_this_run':physical,'results':reports},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
            print(json.dumps(report,ensure_ascii=False),flush=True)
    finally:
        client.close()


if __name__=='__main__': main()
