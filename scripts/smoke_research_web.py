"""Actual model/public-page check with temporary Web data and recording-only delivery."""
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
    load_dotenv(root / '.env')
    settings = Settings('', '', frozenset(), os.getenv('AMAP_API_KEY', ''), os.getenv('LLM_API_KEY', ''),
        os.getenv('LLM_BASE_URL', ''), os.getenv('LLM_MODEL_ID', ''))
    results = []
    with tempfile.TemporaryDirectory() as folder:
        temp = Path(folder)
        app = create_web_app(WebSettings(data_dir=temp/'data'), settings, start_workers=False,
                             settings_store=EnvSettingsStore(temp, inherited={}))
        with TestClient(app, base_url='http://127.0.0.1:8080') as client:
            headers = {'x-csrf-token': client.get('/api/bootstrap').json()['csrf_token']}
            first = client.post('/api/conversations', headers=headers).json()['id']
            second = client.post('/api/conversations', headers=headers).json()['id']
            def send(scope, text, key):
                response = client.post(f'/api/conversations/{scope}/messages', headers=headers,
                    json={'content': text, 'client_request_id': key})
                assert response.status_code == 202
                asyncio.run(app.state.worker.run_once())
                return client.get(f'/api/conversations/{scope}/messages').json()['items'][-1]['content']
            assert '已保存偏好' in send(first, '记住，以后优先公共交通，轻松节奏', 'prefs')
            assert '公共交通' in send(second, '查看我的偏好', 'read')
            results.append({'check': 'web_preferences_cross_conversation', 'status': 'passed'})
            reply = send(first, '读取公开页面 https://www.hbww.org.cn/fuwu/index.html', 'research')
            rows = client.get(f'/api/conversations/{first}/research').json()
            assert rows and rows[0]['source_count'] == 1 and '已读正文' in reply, 'public source reading did not complete'
            assert client.get(f'/api/conversations/{second}/research').json() == []
            results.append({'check': 'real_page_research_with_model' if settings.llm_configured else 'real_page_without_model', 'status': 'passed'})
            if settings.llm_configured and settings.amap_api_key:
                plan_reply = send(first, '根据研究结果规划武汉两天行程，每天两处景点，先不定日期', 'plan-from-research')
                trips = app.state.runtime.application.store.trips.list_for_owner(
                    app.state.worker.adapter.input_event({'request_id': 'read', 'conversation_id': first,
                        'content': '', 'upload_ids': [], 'time': '2030-09-19T00:00:00+00:00'}))
                assert len(trips) == 1 and len(trips[0]['plan']['days']) == 2, 'research-to-trip did not save: ' + plan_reply[:1200]
                assert trips[0]['spec']['transport_text'] == '公共交通', 'saved preference not applied'
                results.append({'check': 'research_to_trip_real_model_and_amap_with_preferences', 'status': 'passed'})
            if os.getenv('SEARCH_API_KEY'):
                reply = send(second, '帮我搜索武汉三日游攻略', 'search')
                row = client.get(f'/api/conversations/{second}/research').json()[0]
                assert row['stage'] in ('completed', 'partial') and row['source_count'] > 0
                results.append({'check': 'tavily_live_search', 'status': 'passed', 'sources': row['source_count']})
            else:
                results.append({'check': 'tavily_live_search', 'status': 'not_run', 'reason': 'SEARCH_API_KEY not configured'})
            assert '已忘记' in send(second, '忘记所有偏好', 'forget')
            assert client.get('/api/preferences').json()['values'] == {}
            print(json.dumps({'results': results, 'usage': usage_summary(app.state.repository.store)}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
