"""Verify a mixed itinerary/hotel/budget request with real APIs and temporary Web data."""
import asyncio
from datetime import datetime, timedelta
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


def main():
    root = Path(__file__).resolve().parents[1]
    load_dotenv(root/'.env')
    settings = Settings('', '', frozenset(), os.getenv('AMAP_API_KEY',''), os.getenv('LLM_API_KEY',''),
        os.getenv('LLM_BASE_URL',''), os.getenv('LLM_MODEL_ID',''))
    assert settings.llm_configured and settings.amap_api_key
    with tempfile.TemporaryDirectory() as folder:
        temp = Path(folder)
        app = create_web_app(WebSettings(data_dir=temp/'data'),settings,start_workers=False,
            settings_store=EnvSettingsStore(temp,inherited={}))
        with TestClient(app,base_url='http://127.0.0.1:8080') as client:
            headers = {'x-csrf-token':client.get('/api/bootstrap').json()['csrf_token']}
            scope = client.post('/api/conversations',headers=headers).json()['id']
            store = app.state.repository.store
            def event(key):
                return app.state.worker.adapter.input_event({'request_id':key,'conversation_id':scope,
                    'content':'','upload_ids':[],'time':datetime.now().astimezone().isoformat()})
            def send(text,key):
                assert client.post(f'/api/conversations/{scope}/messages',headers=headers,
                    json={'content':text,'client_request_id':key}).status_code==202
                asyncio.run(app.state.worker.run_once())
                reply = client.get(f'/api/conversations/{scope}/messages').json()['items'][-1]['content']
                print(json.dumps({'step':key,'reply':reply},ensure_ascii=False),flush=True)
                return reply
            store.research.save(event('research-fixture'),'completed',{'query':'武汉三日游攻略',
                'report':'武汉城市游参考：湖北省博物馆、东湖、黄鹤楼、江汉路。开放与预约规则需另查。','sources':[]})
            start = (datetime.now()+timedelta(days=30)).date()
            end = start+timedelta(days=2)
            send(f'根据研究结果规划武汉行程，{start.isoformat()}到{end.isoformat()}','plan')
            original = store.trips.list_for_owner(event('read'))[0]
            message = '我喜欢轻松一点的行程，然后不太喜欢打车，尽量步行或者公交地铁出行，然后酒店要舒适一点但不要太贵，价格在200-300区间'
            reply = send(message,'mixed')
            tasks = store.tasks.recent('web',event('read').storage_scope_id,event('read').sender_id)
            pending = [t for t in tasks if t.task_type=='semantic' and t.status=='collecting']
            assert len(pending)==1 and 'budget_unit' in pending[0].missing_slots, reply
            assert store.trips.get(event('read'),original['trip_id'])['version']==1
            reply = send('人民币，每间每晚200-300元','units')
            assert '确认行程修改' in reply, reply
            tasks = store.tasks.recent('web',event('read').storage_scope_id,event('read').sender_id)
            candidate = next(t.slots['candidate_trip'] for t in tasks if t.task_type=='itinerary' and t.status=='collecting')
            assert candidate['spec']['gentle'] and candidate['spec']['hotel_requested']
            assert set(candidate['spec']['transport_preferences']['preferred'])=={'walking','transit'}
            assert 'taxi' in candidate['spec']['transport_preferences']['discouraged']
            assert candidate['spec']['budgets']['accommodation']['unit_text']
            assert candidate['plan']['hotel_ids'], 'Hotel candidates were omitted'
            assert '未核实实时价格' in reply
            context = client.get(f'/api/conversations/{scope}/context').json()
            previous_preview = context['confirmations'][0]
            assert previous_preview['can_confirm'] and isinstance(previous_preview['preview'],str)
            # Expire only the temporary confirmation; its requirements should survive.
            with store._connect() as db:
                db.execute("UPDATE travel_tasks SET expires_at='2000-01-01' WHERE task_id=?",(previous_preview['id'],))
            reply = send('酒店我偏好连锁酒店，比如华住会旗下和如家旗下的','hotel-brand')
            assert '确认行程修改' in reply, reply
            context = client.get(f'/api/conversations/{scope}/context').json()
            assert context['confirmations'][0]['can_confirm'] and context['confirmations'][0]['preview']
            draft = store.trips.draft(event('read'),store.trips.get(event('read'),original['trip_id']))
            assert draft['candidate']['spec']['gentle']
            assert draft['candidate']['spec']['budgets']['accommodation']['unit_text']
            assert 'taxi' in draft['candidate']['spec']['transport_preferences']['discouraged']
            assert '舒适' in draft['candidate']['spec']['accommodation_text']
            assert '连锁' in draft['candidate']['spec']['accommodation_text']
            assert client.post(f'/api/conversations/{scope}/confirmations/{previous_preview["id"]}',headers=headers,
                json={'version':previous_preview['version'],'client_request_id':'old-confirm'}).status_code==409
            send('确认行程修改','confirm')
            current = store.trips.get(event('read'),original['trip_id'])
            assert current['version']==2
            for key in ('destination','start_date','end_date','day_count','source_context'):
                assert current['spec'][key]==original['spec'][key],key
            assert store.preferences.snapshot(event('read'))['values']=={}
            print(json.dumps({'result':'passed','trip_version':current['version'],
                'hotel_candidates':len(current['plan']['hotel_ids']),'profile_unchanged':True},ensure_ascii=False),flush=True)


if __name__=='__main__':
    main()
