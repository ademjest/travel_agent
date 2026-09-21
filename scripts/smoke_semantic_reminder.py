"""Exercise real semantic reminder continuation through Web API with disposable data."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from adapters.web_app import create_web_app
from core.web_settings import WebSettings


def main():
    configured = WebSettings.from_env()
    travel = configured.travel_settings()
    if not travel.llm_configured:
        raise SystemExit('Configured LLM API required; no QQ transport is used.')
    with tempfile.TemporaryDirectory(prefix='semantic-reminder-') as folder:
        app = create_web_app(WebSettings(data_dir=Path(folder), open_browser=False), travel, start_workers=False)
        app.state.runtime.application.semantic_task_service.mode = 'execute'
        if '--show-ir' in sys.argv:
            compiler = app.state.runtime.application.semantic_task_service.compiler
            original_client = compiler.client
            def trace(**kwargs):
                response = original_client.chat.completions.create(**kwargs)
                print('SYNTHETIC TEST IR: '+(response.choices[0].message.content or ''), flush=True)
                return response
            compiler.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=trace)))
        with TestClient(app, base_url='http://127.0.0.1:8080') as client:
            headers = {'x-csrf-token': client.get('/api/bootstrap').json()['csrf_token']}
            conversation = client.post('/api/conversations', headers=headers).json()['id']
            def send(text, key):
                response = client.post(f'/api/conversations/{conversation}/messages', headers=headers,
                    json={'content': text, 'client_request_id': key})
                response.raise_for_status()
                assert asyncio.run(app.state.worker.run_once())
                messages = client.get(f'/api/conversations/{conversation}/messages').json()['items']
                reply = [item for item in messages if item['role'] == 'assistant' and item['kind'] == 'reply'][-1]['content']
                print(json.dumps({'case': key, 'input': text, 'reply': reply}, ensure_ascii=False), flush=True)
                return reply
            def reminders():
                return client.get(f'/api/conversations/{conversation}/context').json()['reminders']
            assert '几点' in send('明天提醒我预约陕西历史博物馆。', 'start')
            reply = send('我想选十点整', 'clock')
            assert not reminders(), 'Ambiguous hour must not be committed.'
            assert '上午' in reply or '晚上' in reply, reply
            send('我说的是天亮以后那个时间，别放到晚上', 'period')
            assert len(reminders()) == 1, 'LLM continuation did not complete the existing reminder.'
            assert reminders()[0]['scheduled_at_utc'][11:16] == '02:00', reminders()[0]['scheduled_at_utc']
            send('明天上午十点，买车票这件事到时候喊我一声', 'no-keyword')
            assert len(reminders()) == 2
            send('后天提醒我收拾行李', 'pending-cancel')
            send('我改主意了，这个安排先作罢', 'cancel-setup')
            assert len(reminders()) == 2, 'Cancelling a pending setup must not create a reminder.'
            assert not app.state.runtime.application.semantic_task_service._pending_reminders(
                app.state.worker.adapter.input_event({'request_id':'inspect','conversation_id':conversation,
                    'content':'', 'time':'2030-01-01T00:00:00+00:00'}))
            print('PASS: contextual LLM continuation, ambiguous clock, new paraphrase, pending cancellation.', flush=True)


if __name__ == '__main__':
    main()
