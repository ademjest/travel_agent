"""Real Web API/model/tool smoke test; disposable database, no QQ transport."""
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from adapters.web_app import create_web_app
from core.web_settings import WebSettings


def main():
    configured = WebSettings.from_env()
    travel = configured.travel_settings()
    if not travel.llm_configured or not travel.amap_api_key:
        raise SystemExit('This smoke test requires configured model and Amap APIs.')
    with tempfile.TemporaryDirectory(prefix='travel-web-smoke-') as folder:
        app = create_web_app(WebSettings(data_dir=Path(folder), open_browser=False), travel)
        with TestClient(app, base_url='http://127.0.0.1:8080') as client:
            headers = {'x-csrf-token': client.get('/api/bootstrap').json()['csrf_token']}
            conversation = client.post('/api/conversations', headers=headers).json()['id']
            def send(text, key, uploads=None):
                response = client.post(f'/api/conversations/{conversation}/messages', headers=headers,
                    json={'content':text, 'client_request_id':key, 'upload_ids':uploads or []})
                response.raise_for_status()
                job_id = response.json()['job_id']
                end = time.monotonic()+240
                while time.monotonic() < end:
                    job = client.get(f'/api/tasks/{job_id}').json()
                    if job['status'] in {'completed','failed','cancelled'}:
                        if job['status'] != 'completed':
                            raise AssertionError(f'{key}: {job["status"]}')
                        messages = client.get(f'/api/conversations/{conversation}/messages').json()['items']
                        replies = [m for m in messages if m['role'] == 'assistant' and m['kind'] == 'reply']
                        # Worker marks completion just before dispatching; await durable output too.
                        with app.state.repository.store._connect() as db:
                            delivered = db.execute('SELECT 1 FROM web_deliveries WHERE conversation_id=? AND reply_to_id=? AND kind=?',
                                                   (conversation,key,'reply')).fetchone()
                        if delivered:
                            print(json.dumps({'case':key, 'status':'completed', 'reply':replies[-1]['content'][:550]}, ensure_ascii=False), flush=True)
                            return replies[-1]['content']
                    time.sleep(.5)
                raise AssertionError(f'{key}: timeout')
            assert '武汉' in send('查询天气 武汉', 'weather')
            route = send('武汉站到湖北省博物馆坐地铁怎么走？', 'transit')
            assert '地铁' in route or '公共交通' in route
            start_date = datetime.now()+timedelta(days=30)
            start = f'{start_date.year}年{start_date.month:02d}月{start_date.day:02d}日'
            send(f'帮我规划{start}开始的武汉2天行程，使用公共交通，必去湖北省博物馆。', 'trip')
            context = client.get(f'/api/conversations/{conversation}/context').json()
            assert context['trips'], 'Trip was not persisted.'
            upload = client.post('/api/uploads', headers=headers, data={'conversation_id':conversation},
                files={'file':('smoke-notes.md', '# 测试旅行资料\n目的地：武汉。同行：两位成人。'.encode(), 'text/markdown')})
            upload.raise_for_status()
            send('保存这份旅行资料', 'document', [upload.json()['id']])
            assert client.get(f'/api/conversations/{conversation}/context').json()['documents']
            send('10分钟后提醒我携带身份证', 'reminder')
            rows = client.get(f'/api/conversations/{conversation}/context').json()['reminders']
            assert rows, 'Reminder was not persisted.'
            send('取消提醒 '+rows[0]['reminder_id'], 'cancel-reminder')
            assert client.get(f'/api/conversations/{conversation}/context').json()['reminders'][0]['status'] == 'cancelled'
            print('PASS: Web weather, transit, trip, document, reminder and cancellation; temporary data only.', flush=True)


if __name__ == '__main__':
    main()
