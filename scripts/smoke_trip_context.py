"""Real model/AMap verification; all business data is isolated in a temporary Web store."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
from fastapi.testclient import TestClient
from adapters.web_app import create_web_app
from core.settings import Settings
from core.web_settings import WebSettings
from infrastructure.env_settings_store import EnvSettingsStore
from infrastructure.model_gateway import usage_summary


def main():
    root = Path(__file__).resolve().parents[1]
    load_dotenv(root/'.env')
    settings = Settings('', '', frozenset(), os.getenv('AMAP_API_KEY',''), os.getenv('LLM_API_KEY',''),
        os.getenv('LLM_BASE_URL',''), os.getenv('LLM_MODEL_ID',''))
    assert settings.llm_configured and settings.amap_api_key, 'Live model and map configuration required'
    with tempfile.TemporaryDirectory() as folder:
        temp = Path(folder)
        app = create_web_app(WebSettings(data_dir=temp/'data'), settings, start_workers=False,
                            settings_store=EnvSettingsStore(temp,inherited={}))
        with TestClient(app,base_url='http://127.0.0.1:8080') as client:
            headers = {'x-csrf-token':client.get('/api/bootstrap').json()['csrf_token']}
            scope = client.post('/api/conversations',headers=headers).json()['id']
            store = app.state.runtime.application.store
            def event(text,key):
                return app.state.worker.adapter.input_event({'request_id':key,'conversation_id':scope,
                    'content':text,'upload_ids':[],'time':'2026-09-20T00:00:00+00:00'})
            def send(text,key):
                response = client.post(f'/api/conversations/{scope}/messages',headers=headers,
                    json={'content':text,'client_request_id':key})
                assert response.status_code==202
                asyncio.run(app.state.worker.run_once())
                reply = client.get(f'/api/conversations/{scope}/messages').json()['items'][-1]['content']
                print(json.dumps({'step':key,'reply':reply},ensure_ascii=False),flush=True)
                return reply
            # Deterministic research fixture isolates this test from the already tested search provider.
            store.research.save(event('帮我搜索武汉三日游攻略','research-fixture'),'completed',{
                'query':'武汉三日游攻略','report':'武汉三日游研究资料：可考虑湖北省博物馆、东湖、黄鹤楼、江汉路。开放及预约规则需另查。',
                'sources':[]})
            reply = send('根据研究结果规划行程，我将在国庆出去玩，10月1日到10月三日','range-plan')
            trips = store.trips.list_for_owner(event('','read'))
            assert len(trips)==1, 'Date range did not produce saved trip: '+reply
            original = trips[0]
            assert original['spec']['destination']=='武汉'
            assert original['spec']['day_count']==3
            assert original['spec']['start_date']=='2026-10-01'
            message = '我喜欢轻松一点的行程，然后不太喜欢打车，尽量步行或者公交地铁出行'
            reply = send(message,'screenshot-preference')
            assert '确认行程修改' in reply, 'No update preview: '+reply
            assert store.trips.get(event('','read'),original['trip_id'])['version']==1
            send('确认行程修改','confirm')
            updated = store.trips.get(event('','read'),original['trip_id'])
            assert updated['version']==2
            for key in ('destination','start_date','end_date','day_count','source_context'):
                assert updated['spec'][key]==original['spec'][key], key
            assert updated['spec']['gentle']
            assert set(updated['spec']['transport_preferences']['preferred'])=={'walking','transit'}
            assert 'taxi' in updated['spec']['transport_preferences']['discouraged']
            assert store.preferences.snapshot(event('','read'))['values']=={}
            with store._connect() as db:
                assert db.execute("SELECT count(*) FROM travel_tasks WHERE task_type IN ('transit','walking')").fetchone()[0]==0
            reply = send('安排别那么赶，路上我更愿意坐公共交通，不必每段都走过去','paraphrase')
            assert '确认行程修改' in reply, 'Paraphrase not understood: '+reply
            pending = [t for t in store.tasks.recent('web',event('','read').storage_scope_id,event('','read').sender_id)
                if t.task_type=='itinerary' and t.status=='collecting'][0]
            assert 'taxi' in pending.slots['candidate_trip']['spec']['transport_preferences']['discouraged']
            reply = send('记住，以后默认轻松节奏；也请把当前行程调整得轻松些','compound')
            assert '确认时同时保存长期偏好' in reply, 'Compound request swallowed: '+reply
            assert store.preferences.snapshot(event('','read'))['values']=={}
            send('确认行程修改','compound-confirm')
            assert store.preferences.snapshot(event('','read'))['values']['pace']=='轻松'
            before = store.trips.get(event('','read'),original['trip_id'])['version']
            reply = send('记住，以后默认紧凑节奏','long-term-only')
            assert '已保存偏好' in reply, 'Long-term-only request was misrouted: '+reply
            assert store.trips.get(event('','read'),original['trip_id'])['version']==before
            print(json.dumps({'result':'passed','checks':['web_research_fixture_to_dated_plan','real_model_original_preference',
                'same_trip_preview_confirmation','no_implicit_profile_write','no_transit_task','real_model_paraphrase',
                'previous_constraints_retained','compound_atomic_confirmation','long_term_only_leaves_trip_unchanged'],
                'usage':usage_summary(store)},ensure_ascii=False),flush=True)


if __name__=='__main__':
    main()
