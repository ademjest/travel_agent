"""Real-model continuation of the user's Kaifeng conversation on a temporary database copy."""
import json
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
from adapters.web_app import create_web_app
from core.chat_transport import ChatEvent
from core.settings import Settings
from core.web_settings import WebSettings
from infrastructure.env_settings_store import EnvSettingsStore
from infrastructure.web_repository import OWNER


FIRST='我想去开封玩，但是还没确定时间，想先了解一下大概的攻略参考，最好可以参考一下网上的攻略比如小红书'
THIRD='那还是按照最开始的规划安排吧，但是要加上酒店推荐'
INITIAL='''开封参考规划\n方案A：2天1晚\n第1天：清明上河园；第2天：开封府。\n方案B：3天2晚\n第1天：老城；第2天：清明上河园；第3天：开封府。'''
RESEARCH='''开封四日研究方案\n第1天：老城；第2天：清明上河园；第3天：开封府；第4天：轻松补漏。'''


def main():
    root=Path(__file__).resolve().parents[1]
    load_dotenv(root/'.env')
    settings=Settings('', '', frozenset(),os.getenv('AMAP_API_KEY',''),os.getenv('LLM_API_KEY',''),
        os.getenv('LLM_BASE_URL',''),os.getenv('LLM_MODEL_ID',''))
    assert settings.llm_configured and settings.amap_api_key
    scope='synthetic-kaifeng-context'
    with tempfile.TemporaryDirectory() as folder:
        data=Path(folder)/'data'; data.mkdir()
        app=create_web_app(WebSettings(data_dir=data,open_browser=False),settings,start_workers=False,
            settings_store=EnvSettingsStore(Path(folder),inherited={}))
        store=app.state.repository.store
        semantic=app.state.runtime.application.semantic_task_service
        trip_service=app.state.runtime.application.trip_service
        def event(text,key): return ChatEvent('web','group',key,scope,OWNER,text)
        store.planning.capture(event('我想去开封玩','initial-source'),INITIAL)
        store.planning.capture(event('帮我搜索开封四日游攻略','research-source'),RESEARCH,
            kind='research_backed',destination='开封',duration_options=[4])
        initial_ir=semantic.compiler.compile(FIRST)
        assert any(op.domain=='research' and op.operation=='search_travel_guides' for op in initial_ir.operations),initial_ir
        third=event(THIRD,'context-third')
        answer=semantic.handle(third,store.begin_event(third.event_key))
        print(json.dumps({'step':'select-earliest','reply':answer},ensure_ascii=False),flush=True)
        assert '2天和3天' in answer and '酒店' in answer
        duration=event('采用3天2晚','context-duration')
        preview=semantic.handle(duration,store.begin_event(duration.event_key))
        print(json.dumps({'step':'create-preview','reply':preview[:1200]},ensure_ascii=False),flush=True)
        assert '行程创建预览' in preview and '住宿候选' in preview
        assert store.trips.list_for_owner(event('','read'))==()
        confirm=event('确认行程修改','context-confirm')
        saved=trip_service.handle(confirm,store.begin_event(confirm.event_key))
        trips=store.trips.list_for_owner(event('','read'))
        assert len(trips)==1 and trips[0]['spec']['destination']=='开封' and trips[0]['spec']['day_count']==3
        assert trips[0]['spec']['hotel_requested'] and trips[0]['spec']['defer_start_date']
        print(json.dumps({'result':'passed','saved_reply':saved[:500],
            'artifacts':[{'ordinal':p['ordinal'],'kind':p['kind'],'duration_options':p['duration_options'],'status':p['status']}
                         for p in store.planning.list_for_owner(event('','read'),include_deleted=True)],
            'synthetic_temporary_data':True},ensure_ascii=False),flush=True)
        model=getattr(getattr(app.state.runtime.application,'travel_agent',None),'client',None)
        if getattr(model,'close',None): model.close()


if __name__=='__main__':
    main()
