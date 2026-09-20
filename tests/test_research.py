import asyncio
import json
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from adapters.web_app import create_web_app
from core.chat_transport import ChatEvent
from core.settings import Settings
from core.web_settings import WebSettings
from infrastructure.memory_store import MemoryStore
from infrastructure.public_http import request_public, public_address, PublicHTTPError
from infrastructure.search_client import SearchClient
from services.research_service import ResearchService
from test_travel_agent import FakeClient, assistant_message, completion


class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = MemoryStore(self.root/'test.db')
        self.search = Mock(configured=True)
        self.search.search.return_value = [{'url': 'https://travel.example/a', 'title': '公开来源', 'snippet': '武汉可游览公园。', 'published_at': ''}]
        self.reader = Mock(return_value={'url': 'https://travel.example/a', 'text': '武汉公园可散步。节假日开放另行通知。',
                         'content_hash': 'hash', 'status': 'read', 'retrieved_at': '2030-09-01T00:00:00+00:00'})
        self.service = ResearchService(self.store, self.search, reader=self.reader)

    def event(self, text='帮我搜索武汉攻略', scope='a', owner='u', key='test'):
        return ChatEvent('web', 'group', key, scope, owner, text)

    def handle(self, event=None):
        event = event or self.event()
        return self.service.handle(event, self.store.begin_event(event.event_key))

    def test_search_read_report_scope_and_checkpoint_no_requery(self):
        reply = self.handle()
        self.assertIn('已读正文', reply)
        self.assertIn('发布时间：未知', reply)
        self.assertEqual(len(self.store.research.latest(self.event())), 1)
        self.assertEqual(self.store.research.latest(self.event(scope='b')), [])
        self.assertEqual(self.store.research.latest(self.event(owner='v')), [])
        self.service.handle(self.event(), None)
        self.search.search.assert_called_once()
        self.reader.assert_called_once()
        self.assertEqual(self.store.preferences.snapshot(self.event())['values'], {})
        self.assertEqual(self.store.reminders.list_for_owner('web', 'a', 'u'), ())

    def test_failed_page_is_only_snippet_and_model_citation_is_checked(self):
        self.reader.side_effect = ValueError('unavailable')
        self.service.client = FakeClient([completion(assistant_message(content=json.dumps({'items': [
            {'text': 'invented', 'sources': [999]}]})))])
        reply = self.handle()
        self.assertIn('仅搜索摘要', reply)
        self.assertIn('未通过引用校验', reply)
        self.assertNotIn('invented', reply)

    def test_model_valid_citations_and_source_date(self):
        self.store.preferences.update(self.event(), 0, {'pace': '轻松'})
        self.service.client = FakeClient([completion(assistant_message(content=json.dumps({'items': [
            {'text': '公园可散步，节假日开放待核实。', 'sources': [1]}]})))])
        self.assertIn('待核实。 [1]', self.handle())
        request = json.loads(self.service.client.completions.requests[0]['messages'][1]['content'])
        self.assertEqual(request['preference_defaults'], {'pace': '轻松'})
        self.assertNotIn('轻松', self.search.search.call_args.args[0])

    def test_missing_search_configuration_is_not_fake_success(self):
        self.service.search = SearchClient()
        self.assertIn('网页搜索未配置', self.handle())
        self.reader.assert_not_called()

    def test_resumed_checkpoint_stage_and_negative_request(self):
        event = self.event()
        self.store.research.save(event, 'reading', {'stage': 'searching', 'sources': []})
        self.assertEqual(self.store.research.latest(event)[0]['stage'], 'reading')
        self.assertIn('没有发起', self.handle(self.event('不要联网搜索武汉攻略', key='negative')))
        self.search.search.assert_not_called()

    def test_direct_link_works_without_search_key(self):
        self.service.search = SearchClient()
        reply = self.handle(self.event('读取攻略 https://travel.example/a'))
        self.assertIn('已读正文', reply)
        self.reader.assert_called_once()

    def test_missing_destination_collects_and_resumes_owned_task(self):
        self.assertIn('哪个城市', self.handle(self.event('帮我查攻略', key='start')))
        other = self.event('武汉', owner='other', key='other')
        self.assertIsNone(self.service.handle(other, self.store.begin_event(other.event_key)))
        self.assertIn('网页研究结果', self.handle(self.event('武汉', key='city')))
        self.assertIn('目的地：武汉', self.search.search.call_args.args[0])

    def test_research_then_plan_only_on_explicit_user_request(self):
        self.service.trip_service = Mock()
        self.service.trip_service.handle.return_value = '行程草案'
        self.handle()
        self.service.trip_service.handle.assert_not_called()
        reply = self.handle(self.event('根据研究结果规划武汉三天行程', key='plan'))
        self.assertEqual(reply, '行程草案')
        self.assertIn('不是操作授权', self.service.trip_service.handle.call_args.kwargs['source_text_override'])

    def test_network_pins_public_ip_tls_hostname_and_no_credential_redirect(self):
        for host in ('127.0.0.1', '10.1.1.1', '169.254.169.254', '::1'):
            with patch('infrastructure.public_http.resolve_host', return_value=(host,)):
                with self.assertRaises(PublicHTTPError): public_address('https://example.test')
        response = Mock(status=302, headers={'Location': 'https://other.test'})
        pool = Mock()
        pool.urlopen.return_value = response
        with patch('infrastructure.public_http.resolve_host', return_value=('8.8.8.8',)), patch('infrastructure.public_http.urllib3.HTTPSConnectionPool', return_value=pool) as factory:
            with self.assertRaises(PublicHTTPError): request_public('https://example.test', headers={'Authorization': 'Bearer fixture'}, redirects=2)
            self.assertEqual(factory.call_args.args[0], '8.8.8.8')
            self.assertEqual(factory.call_args.kwargs['assert_hostname'], 'example.test')
            self.assertEqual(pool.urlopen.call_args.kwargs['headers']['Host'], 'example.test')
            self.assertEqual(pool.urlopen.call_count, 1)
            response.close.assert_called_once()

    def test_tavily_request_does_not_include_profile_or_history(self):
        with patch('infrastructure.search_client.request_public', return_value=(b'{"results":[]}', '', 'application/json')) as request:
            SearchClient('fixture').search('武汉攻略')
        self.assertEqual(request.call_args.kwargs['payload']['query'], '武汉攻略')
        self.assertNotIn('api_key', request.call_args.kwargs['payload'])
        self.assertEqual(request.call_args.args[0], 'https://api.tavily.com/search')

    def test_web_delete_purges_research_candidates_but_keeps_confirmed_preferences(self):
        app = create_web_app(WebSettings(data_dir=self.root/'web'), Settings('', '', frozenset(), '', '', '', ''), start_workers=False)
        with TestClient(app, base_url='http://127.0.0.1:8080') as client:
            headers = {'x-csrf-token': client.get('/api/bootstrap').json()['csrf_token']}
            identity = client.post('/api/conversations', headers=headers).json()['id']
            event = self.event(scope=identity, owner='local-owner')
            store = app.state.repository.store
            store.preferences.update(event, 0, {'pace': '轻松'})
            store.preferences.suggest(event, 1, {'night': '不安排夜游'})
            store.research.save(event, 'completed', {'report': 'fixture'})
            client.delete(f'/api/conversations/{identity}', headers=headers)
            app.state.deletion.cleanup(identity)
            self.assertEqual(store.research.latest(event), [])
            self.assertIsNone(store.preferences.candidate(event))
            self.assertEqual(store.preferences.snapshot(event)['values']['pace'], '轻松')

    def test_cancel_during_read_prevents_late_report_and_delivery(self):
        app = create_web_app(WebSettings(data_dir=self.root/'cancel'), Settings('', '', frozenset(), '', '', '', ''), start_workers=False)
        service = app.state.runtime.application.research_service
        service.search = self.search
        entered, release = Event(), Event()
        def slow(url):
            entered.set()
            release.wait(5)
            return self.reader(url)
        service.reader = slow
        with TestClient(app, base_url='http://127.0.0.1:8080') as client:
            headers = {'x-csrf-token': client.get('/api/bootstrap').json()['csrf_token']}
            identity = client.post('/api/conversations', headers=headers).json()['id']
            client.post(f'/api/conversations/{identity}/messages', headers=headers,
                json={'content': '搜索武汉攻略', 'client_request_id': 'cancel-test'})
            store = app.state.repository.store
            async def scenario():
                running = asyncio.create_task(app.state.worker.run_once())
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                    self.assertTrue(store.inbox.cancel(f'web:group:{identity}:cancel-test', 'web', identity, 'local-owner'))
                finally:
                    release.set()
                    await running
            asyncio.run(scenario())
            rows = client.get(f'/api/conversations/{identity}/research').json()
            self.assertEqual(rows[0]['stage'], 'cancelled')
            self.assertEqual(rows[0]['report'], '')
            self.assertEqual(len(client.get(f'/api/conversations/{identity}/messages').json()['items']), 1)
