import asyncio
from datetime import datetime, timedelta, timezone
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from fastapi.testclient import TestClient
from PIL import Image

from adapters.web_app import create_web_app
from core.chat_transport import ChatEvent, OutgoingMessage
from core.settings import Settings, SettingsError
from core.tasks import TaskUpdate
from core.web_settings import WebSettings
from infrastructure.web_repository import OWNER


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = WebSettings(data_dir=Path(self.temp.name), open_browser=False)
        self.app = create_web_app(self.settings, Settings('', '', frozenset(), '', '', '', ''), start_workers=False)
        self.repo = self.app.state.repository
        self.store = self.repo.store
        self.worker = self.app.state.worker
        self.runtime = self.app.state.runtime
        self.client = TestClient(self.app, base_url='http://127.0.0.1:8080')
        self.client.__enter__()
        self.addCleanup(lambda: self.client.__exit__(None, None, None))
        response = self.client.get('/api/bootstrap')
        self.assertEqual(response.status_code, 200)
        self.headers = {'x-csrf-token': response.json()['csrf_token']}
        self.identity = self.client.post('/api/conversations', headers=self.headers).json()['id']

    def send(self, text, key='message', uploads=None, identity=None):
        return self.client.post(f'/api/conversations/{identity or self.identity}/messages', headers=self.headers,
                                json={'content': text, 'client_request_id': key, 'upload_ids': uploads or []})

    def run_job(self):
        return asyncio.run(self.worker.run_once())

    def messages(self):
        return self.client.get(f'/api/conversations/{self.identity}/messages').json()['items']

    def upload(self, filename, data, identity=None):
        return self.client.post('/api/uploads', headers=self.headers,
            data={'conversation_id': identity or self.identity}, files={'file': (filename, data, 'application/octet-stream')})

    def event(self, text, key='direct'):
        return ChatEvent('web', 'group', key, self.identity, OWNER, text)

    def test_without_qq_config_and_restart_history(self):
        self.assertEqual(self.client.get('/health/ready').status_code, 200)
        sent = self.send('ping')
        self.assertEqual(sent.status_code, 202)
        self.assertTrue(self.run_job())
        self.assertEqual([m['content'] for m in self.messages()], ['ping', 'pong'])
        other = create_web_app(self.settings, Settings('', '', frozenset(), '', '', '', ''), start_workers=False)
        self.assertEqual(other.state.repository.messages(self.identity)['items'][-1]['content'], 'pong')
        self.assertEqual(self.store.get_recent_turns(f'web:{self.identity}', OWNER)[-1].assistant_content, 'pong')

    def test_idempotent_submission_and_different_body_conflict(self):
        first = self.send('ping').json()
        self.assertEqual(self.send('ping').json()['job_id'], first['job_id'])
        self.assertEqual(self.send('别的内容').status_code, 409)
        self.run_job()
        self.assertFalse(self.run_job())
        self.assertEqual(len(self.messages()), 2)

    def test_loopback_host_origin_session_and_csrf(self):
        self.assertEqual(self.client.post('/api/conversations').status_code, 403)
        self.assertEqual(self.client.get('/api/bootstrap', headers={'Host': 'evil.example:8080'}).status_code, 403)
        self.assertEqual(self.client.get('/api/bootstrap', headers={'Origin': 'https://evil.example'}).status_code, 403)
        self.assertEqual(self.client.get('/api/bootstrap', headers={'Sec-Fetch-Site': 'cross-site'}).status_code, 403)
        self.assertEqual(self.client.post('/api/conversations', headers={**self.headers, 'Origin': 'http://localhost:9999'}).status_code, 403)
        with TestClient(self.app, base_url='http://127.0.0.1:8080') as anonymous:
            self.assertEqual(anonymous.get('/api/conversations').status_code, 401)
        with self.assertRaises(SettingsError):
            WebSettings(host='0.0.0.0').validate()
        from core.data_paths import PROJECT_ROOT
        with self.assertRaises(SettingsError):
            WebSettings(data_dir=PROJECT_ROOT / 'data').validate()

    def test_upload_scope_format_and_size_validation(self):
        u = self.upload('攻略.md', '# 武汉\n湖北省博物馆'.encode()).json()
        other = self.repo.create_conversation()['id']
        self.assertEqual(self.send('资料', uploads=[u['id']], identity=other).status_code, 404)
        self.assertEqual(self.upload('fake.png', b'not an image').status_code, 415)
        self.assertEqual(self.upload('bad.exe', b'a').status_code, 415)
        self.assertEqual(self.upload('too-big.txt', b'a' * (5*1024*1024+1)).status_code, 413)
        self.assertEqual(self.send('hello', uploads=['../../.env']).status_code, 404)
        self.assertEqual(self.client.get('/api/uploads/unknown').status_code, 404)
        self.assertEqual(self.client.get('/api/uploads/'+u['id']).status_code, 200)

    def test_document_import_and_isolation(self):
        uploaded = self.upload('武汉攻略.md', '# 武汉\n湖北省博物馆需要预约。'.encode()).json()
        self.assertEqual(self.send('', uploads=[uploaded['id']]).status_code, 202)
        self.run_job()
        ctx = self.client.get(f'/api/conversations/{self.identity}/context').json()
        self.assertEqual(ctx['documents'][0]['filename'], '武汉攻略.md')
        other = self.repo.create_conversation()['id']
        self.assertEqual(self.repo.context(other)['documents'], ())
        self.assertEqual(self.messages()[-1]['role'], 'assistant')
        self.assertNotIn('绑定码', self.messages()[-1]['content'])

    def test_image_uses_original_image_service_and_scoped_local_attachment(self):
        data = io.BytesIO()
        Image.new('RGB', (12, 12), 'white').save(data, 'PNG')
        image = self.upload('票据.png', data.getvalue()).json()
        service = self.runtime.application.image_context_service
        service.analyze = Mock(return_value={'uncertain': False, 'answer': '预约日期为10月1日', 'facts': '预约日期', 'media_id': 1})
        self.send('这张票是什么日期？', uploads=[image['id']])
        self.run_job()
        self.assertIn('10月1日', self.messages()[-1]['content'])
        attachment = service.analyze.call_args.args[1][0]
        self.assertTrue(Path(attachment.local_path).resolve().is_relative_to(Path(self.temp.name).resolve()))
        self.assertEqual(attachment.url, '')

    def test_platform_filtered_claim_does_not_take_onebot_work(self):
        self.store.inbox.submit('onebot:group:real:1', 'onebot', 'real', 'qq-user', {'message': []})
        self.send('ping')
        self.run_job()
        self.assertEqual(self.store.inbox.get('onebot:group:real:1')['status'], 'pending')
        self.assertFalse(self.run_job())
        self.store.inbox.submit('onebot:group:real:2', 'onebot', 'real', 'qq-user', {'message': []}, has_assets=True)
        self.assertIsNone(self.store.inbox.claim_capture(platform='web'))

    def test_output_replay_after_save_before_ack_is_deduplicated(self):
        message = OutgoingMessage('group', self.identity, 'request', {'text': '一次回复', 'kind': 'reply'}, 'web:outbox:123')
        first = self.repo.deliver(message)
        self.assertEqual(self.repo.deliver(message), first)
        self.assertEqual(len(self.messages()), 1)

    def test_cancel_pending_job_and_reject_unknown_task(self):
        job = self.send('ping').json()['job_id']
        self.assertEqual(self.client.post(f'/api/tasks/{job}/cancel', headers=self.headers).status_code, 200)
        self.assertFalse(self.run_job())
        self.assertEqual(self.client.get(f'/api/tasks/{job}').json()['status'], 'cancelled')
        self.assertEqual(self.client.post('/api/tasks/999/cancel', headers=self.headers).status_code, 404)
        self.assertEqual(len(self.messages()), 1)

    def test_completed_task_cannot_be_cancelled(self):
        job = self.send('ping').json()['job_id']
        self.run_job()
        self.assertEqual(self.client.post(f'/api/tasks/{job}/cancel', headers=self.headers).status_code, 409)

    def test_colloquial_time_reply_continues_reminder_without_chat_fallback(self):
        # A generic chatbot must not handle the follow-up or claim reminders are unavailable.
        self.runtime.travel_service.handle = Mock(side_effect=AssertionError('must stay in reminder service'))
        self.send('明天提醒我预约陕西历史博物馆。', key='reminder-start')
        self.run_job()
        self.assertIn('几点', self.messages()[-1]['content'])
        self.send('10点吧', key='reminder-clock')
        self.run_job()
        self.assertIn('上午还是', self.messages()[-1]['content'])
        self.assertEqual(self.repo.context(self.identity)['reminders'], ())
        self.send('上午吧', key='reminder-period')
        self.run_job()
        self.assertIn('10:00', self.messages()[-1]['content'])
        reminders = self.repo.context(self.identity)['reminders']
        self.assertEqual(len(reminders), 1)
        self.assertEqual(reminders[0]['title'], '预约陕西历史博物馆')
        self.runtime.travel_service.handle.assert_not_called()

    def test_web_calls_contextual_llm_for_nonmatching_followup(self):
        from agents.intent_compiler import IntentCompiler
        from services.semantic_task_service import SemanticTaskService
        from test_semantic_reminder_continuation import Client
        model = Client()
        self.runtime.application.semantic_task_service = SemanticTaskService(
            self.store, IntentCompiler(model, 'test'), reminder_service=self.runtime.application.personal_reminder_service)
        self.runtime.travel_service.handle = Mock(side_effect=AssertionError('must not fall through to generic chat'))
        model.value = {'schema_version': 1, 'action': 'create', 'operations':[
            {'domain':'reminder','operation':'create','title':'预约陕西历史博物馆','trigger':{'text':'明天'}}],
            'source_spans':['明天','预约陕西历史博物馆']}
        self.send('明天提醒我预约陕西历史博物馆', key='semantic-start')
        self.run_job()
        model.value = {'schema_version':1, 'action':'update', 'operations':[
            {'domain':'reminder','operation':'continue_pending','task_index':1,'disposition':'answer',
             'trigger':{'text':'上午十点','source_text':'快到中午的十点整'}}], 'source_spans':['快到中午的十点整']}
        self.send('我希望安排在快到中午的十点整，方便出门前处理', key='semantic-followup')
        self.run_job()
        self.assertIn('10:00', self.messages()[-1]['content'])
        self.assertEqual(len(self.repo.context(self.identity)['reminders']), 1)
        self.assertEqual(len(model.requests), 2)
        self.assertIn('预约陕西历史博物馆', model.requests[-1]['context'])
        self.runtime.travel_service.handle.assert_not_called()

    def test_real_reminder_service_and_notification_without_browser(self):
        self.send('1分钟后提醒我带身份证')
        self.run_job()
        rows = self.store.reminders.list_for_owner('web', self.identity, OWNER)
        self.assertEqual(len(rows), 1, self.messages())
        due = datetime.fromisoformat(rows[0]['scheduled_at_utc']) + timedelta(seconds=1)
        asyncio.run(self.runtime.reminder_scheduler.scan_once(due))
        asyncio.run(self.runtime.outbox_worker.dispatch_due_once(due))
        notices = self.repo.notifications()
        self.assertEqual(len(notices), 1)
        self.assertIn('身份证', notices[0]['content'])
        self.assertIsNone(notices[0]['read_at'])
        self.client.post(f'/api/notifications/{notices[0]["id"]}/read', headers=self.headers)
        self.assertIsNotNone(self.repo.notifications()[0]['read_at'])

    def test_versioned_confirmation_rejects_old_preview(self):
        event = self.event('修改行程')
        claim = self.store.begin_event(event.event_key)
        task = TaskUpdate('itinerary', 'collecting', event.content, {}, ('confirmation',))
        self.store.tasks.prepare_result(event, claim, task, '请核对变更')
        ctx = self.repo.context(self.identity)
        preview = ctx['confirmations'][0]
        response = self.client.post(f'/api/conversations/{self.identity}/confirmations/{preview["id"]}',
            headers=self.headers, json={'version': preview['version']+1, 'client_request_id': 'confirm-old'})
        self.assertEqual(response.status_code, 409)
        response = self.client.post(f'/api/conversations/{self.identity}/confirmations/{preview["id"]}',
            headers=self.headers, json={'version': preview['version'], 'client_request_id': 'confirm'})
        self.assertEqual(response.status_code, 202)
        with self.store._connect() as db:
            db.execute('UPDATE travel_tasks SET version=version+1 WHERE task_id=?', (preview['id'],))
        self.runtime.application.trip_service.handle = Mock(side_effect=AssertionError('must not apply stale preview'))
        self.run_job()
        self.assertIn('未执行修改', self.messages()[-1]['content'])

    def test_scheduled_query_uses_web_platform_and_due_time(self):
        service = self.runtime.application.scheduled_query_service
        service._extract = Mock(return_value={'kind': 'weather', 'arguments': {'location': '武汉'}, 'time_text': '1分钟后'})
        self.runtime.travel_service.execute_tool = Mock(return_value='武汉：晴')
        self.send('1分钟后告诉我武汉当前天气')
        self.run_job()
        rows = self.repo.context(self.identity)['scheduled_queries']
        self.assertEqual(len(rows), 1, self.messages())
        self.runtime.travel_service.execute_tool.assert_not_called()
        due = datetime.fromisoformat(rows[0]['due_at']) + timedelta(seconds=1)
        asyncio.run(self.runtime.reminder_scheduler.scan_once(due))
        service.clock = lambda: due
        asyncio.run(self.worker.run_once(now=due))
        self.assertIn('武汉：晴', self.messages()[-1]['content'])
        self.assertEqual(self.repo.notifications()[0]['kind'], 'notification')

    def test_status_no_secrets_and_api_404_not_html(self):
        status = self.client.get('/api/status')
        self.assertEqual(status.status_code, 200)
        self.assertNotIn('api_key', status.text.lower())
        self.assertEqual(self.client.get('/api/not-found').status_code, 404)
        self.assertEqual(self.client.post('/api/conversations', headers={**self.headers, 'Content-Type': 'application/json'},
                                         content=b'a' * (128*1024+1)).status_code, 413)

    def test_trip_service_creates_real_structured_sidebar_data(self):
        from agents.trip_planner import TripPlanner
        from services.trip_service import TripService
        from test_trips import SPEC, PLACES, plan_value, model_response
        from test_travel_agent import FakeClient
        amap = Mock()
        amap.search_places.return_value = list(PLACES.values())
        client = FakeClient([model_response({**SPEC, 'start_date_text': '2030年10月1日'}), model_response(plan_value())])
        self.runtime.application.trip_service = TripService(self.store, TripPlanner(client, 'test', amap))
        self.send('帮我规划2030年10月1日开始的武汉三天行程，带老人，必去湖北省博物馆')
        self.run_job()
        trips = self.repo.context(self.identity)['trips']
        self.assertEqual(len(trips), 1, self.messages())
        self.assertEqual(trips[0]['spec']['destination'], '武汉')
        self.assertEqual(len(trips[0]['plan']['days']), 3)
        self.assertEqual(trips[0]['sources']['h']['name'], '湖北省博物馆')

    def test_policy_monitor_change_delivers_web_notification_and_can_stop(self):
        from services.booking_policy import BookingPolicyResolver
        from services.policy_watch_service import PolicyWatchService
        now = datetime.now(timezone.utc)
        page = ['湖北省博物馆\n个人入馆预约可提前5天。每日0点开始放票。']
        resolver = BookingPolicyResolver(sources={'湖北省博物馆':'https://museum.example/policy'},
            fetch_text=lambda url: page[0], clock=lambda: now)
        service = PolicyWatchService(self.store, resolver, clock=lambda: now)
        self.runtime.application.policy_watch_service = service
        ending = now+timedelta(days=5)
        self.send(f'监测湖北省博物馆预约规则，到{ending.year}年{ending.month:02d}月{ending.day:02d}日')
        self.run_job()
        watch = self.repo.context(self.identity)['policy_watches'][0]
        now = datetime.fromisoformat(watch['next_check_at'])
        page[0] = page[0].replace('提前5天', '提前7天')
        asyncio.run(self.runtime.reminder_scheduler.scan_once(now))
        asyncio.run(self.worker.run_once(now=now, lane='scheduled'))
        self.assertEqual(len(self.repo.notifications()), 1)
        self.assertIn('提前 7 天', self.repo.notifications()[0]['content'])
        self.send('停止规则监测 '+watch['watch_id'], key='stop')
        self.run_job()
        self.assertEqual(self.repo.context(self.identity)['policy_watches'][0]['status'], 'cancelled')

    def test_cancel_running_request_fences_late_business_write(self):
        from threading import Event
        started, release = Event(), Event()
        def slow(text):
            started.set()
            release.wait(5)
            self.store.add_document(f'web:{self.identity}', OWNER, 'late.md', 'digest', 'late write', ['late write'])
            return 'must not publish'
        self.runtime.travel_service.handle = slow
        identity = self.send('slow').json()['job_id']
        async def run():
            execution = asyncio.create_task(self.worker.run_once())
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                result = self.client.post(f'/api/tasks/{identity}/cancel', headers=self.headers)
                self.assertEqual(result.status_code, 200)
            finally:
                release.set()
                await execution
        asyncio.run(run())
        self.assertEqual(self.repo.context(self.identity)['documents'], ())
        self.assertNotIn('must not publish', str(self.messages()))

    def test_expired_lease_recovers_prepared_result_without_reexecution(self):
        job_id = self.send('ping').json()['job_id']
        old = self.store.inbox.claim(platform='web')
        claim = self.store.begin_event(old['event_key'])
        self.store.prepare_event_reply(claim.event_id, claim.claim_token, 'saved before restart', 'ping')
        with self.store._connect() as db:
            db.execute('UPDATE inbox_jobs SET lease_until=? WHERE id=?',
                       ((datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(), job_id))
        self.runtime.travel_service.handle = Mock(side_effect=AssertionError('no reexecution'))
        self.run_job()
        self.assertEqual(self.messages()[-1]['content'], 'saved before restart')

    def test_only_one_runtime_can_own_the_data_directory(self):
        from core.web_settings import web_data_lease
        with web_data_lease(self.settings.data_dir):
            with self.assertRaises(SettingsError):
                with web_data_lease(self.settings.data_dir):
                    pass

    def test_invalid_office_file_is_rejected_without_server_error(self):
        self.assertEqual(self.upload('invalid.docx', b'not a ZIP archive').status_code, 415)


if __name__ == '__main__':
    unittest.main()
