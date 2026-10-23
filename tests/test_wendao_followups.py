"""Observed Nanjing follow-up; constructed extractor/HTTP responses, no live calls."""
from datetime import datetime, timezone
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from agents.research_search_planner import ResearchSearchPlanner
from agents.trip_planner import TripPlanner
from core.chat_transport import ChatEvent
from infrastructure.memory_store import MemoryStore
from infrastructure.wendao_client import WendaoClient, ENDPOINT
from services.research_context import research_dates, resolve_plan
from services.official_sources import OfficialSources
from services.research_service import ResearchService
from services.trip_service import TripService
from test_travel_agent import FakeClient
from test_trips import model_response
from test_wendao import ANSWER, PLACES, PLAN

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/wendao_nanjing_followup_2026-10-21.json').read_text(encoding='utf-8'))
NOW = datetime(2026, 10, 21, 8, 32, tzinfo=timezone.utc)


class NanjingFollowupTests(unittest.TestCase):
    def test_plan_date_copy_is_not_mistaken_for_current_user_date(self):
        request = {'query_text': FIXTURE['followup'], 'original_request': FIXTURE['followup'],
                   'topics': ['opening_hours', 'ticket_price', 'reservation_rules']}
        response = {'targets': [{'name': '六朝博物馆', 'source_text': '六朝博物馆', 'day_index': 1,
                                'optional': False, 'visit_mode': 'entry'}],
                    'topics': request['topics'], 'arrival_date_text': '', 'visit_start_date_text': '2026-10-22',
                    'requested_domains': [], 'excluded_domains': [], 'strict_sources': False, 'language': 'zh-cn'}
        client = FakeClient([model_response(response)])
        result = ResearchSearchPlanner(client, 'test').plan_targets(request, FIXTURE['plan'])
        self.assertNotIn('visit_start_date_text', result['fields'])
        self.assertIn('visit_start_date_text', result['ignored_date_fields'])
        event = ChatEvent('onebot', 'group', 'followup', 'g', 'u', FIXTURE['followup'], occurred_at=NOW)
        dates = research_dates(result['fields'], event, [2], planning_spec=FIXTURE['planning_spec'])
        self.assertEqual(dates['visit_start_date'], '2026-10-22')
        self.assertTrue(dates['date_is_tentative'])
        self.assertEqual(dates['basis'], 'saved_trip_system_reference')

    def test_observed_followup_reaches_provider_with_saved_tentative_dates(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = MemoryStore(Path(temp.name) / 'test.db')
        names = [line for line in FIXTURE['plan']['content'].splitlines() if not line.startswith('第')]
        output = {'targets': [{'name': n, 'source_text': n, 'day_index': 1 if i < 3 else 2,
                              'optional': False, 'visit_mode': 'entry'} for i, n in enumerate(names)],
                  'topics': ['opening_hours', 'ticket_price', 'reservation_rules'],
                  'visit_start_date_text': '2026-10-22', 'arrival_date_text': '',
                  'requested_domains': [], 'excluded_domains': [], 'strict_sources': False, 'language': 'zh-cn'}
        answer = '\n'.join('### ' + n + '\n这里的展陈适合慢慢参观。\n- **开放时间**：每日开放时间09:00-17:00。' for n in names[:4])
        http = Mock(return_value=(answer.encode(), ENDPOINT, 'text/markdown'))
        amap = Mock()
        amap.search_poi_details.return_value = []
        service = ResearchService(store, Mock(configured=False), FakeClient([model_response(output)]), 'test',
            amap=amap, official_sources=OfficialSources([]), wendao=WendaoClient('fixture-key', True, transport=http))
        event = ChatEvent('onebot', 'group', 'followup', 'g', 'u', FIXTURE['followup'], occurred_at=NOW)
        reply = service.handle_intent(event, None, request={'original_request': event.content, 'query_text': event.content,
            'plan': {**FIXTURE['plan'], 'planning_spec': FIXTURE['planning_spec']}, 'topics': output['topics']})
        self.assertEqual(http.call_count, 1)
        query = http.call_args.kwargs['payload']['query']
        self.assertIn('实际出发日尚未确定', query)
        self.assertIn('2026-10-22', query)
        self.assertIn('六朝博物馆', query)
        self.assertNotIn('研究请求校验失败', reply)
        data = store.research.get(event)
        self.assertTrue(data['provider_dates']['date_is_tentative'])
        self.assertEqual(data['dates']['visit_start_date'], '2026-10-22')
        self.assertFalse(store.trips.list_for_owner(event))

    def test_explicit_current_date_overrides_saved_reference(self):
        event = ChatEvent('onebot', 'group', 'dates', 'g', 'u', '改按10月25日开始查询', occurred_at=NOW)
        dates = research_dates({'visit_start_date_text': '10月25日'}, event, [2], planning_spec=FIXTURE['planning_spec'])
        self.assertEqual(dates['visit_start_date'], '2026-10-25')
        self.assertFalse(dates['date_is_tentative'])


class AutomaticResearchTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = MemoryStore(Path(temp.name) / 'test.db')
        self.http = Mock(return_value=(ANSWER.encode(), ENDPOINT, 'text/markdown'))
        self.wendao = WendaoClient('fixture-key', True, transport=self.http)
        extract = {'destination': '成都', 'duration_text': '一天', 'preferences': [], 'must_visit': []}
        self.client = FakeClient([model_response(extract), model_response(PLAN)])
        self.amap = Mock()
        self.amap.search_places.return_value = list(PLACES.values())
        self.trip = TripService(self.store, TripPlanner(self.client, 'test', self.amap), clock=lambda: NOW)
        self.research = ResearchService(self.store, Mock(), trip_service=self.trip, wendao=self.wendao)
        self.trip.research_service = self.research

    def test_enabled_provider_enriches_plain_planning_request_and_view_does_not_resubmit(self):
        event = ChatEvent('onebot', 'group', 'create', 'g', 'u', '我想去成都玩一天，后面的某个周末', occurred_at=NOW)
        reply = self.trip.handle(event, self.store.begin_event(event.event_key))
        self.assertEqual(self.http.call_count, 1)
        self.assertIn('仿古文化街区', reply)
        self.assertIn('实际出发日尚未确定', self.http.call_args.kwargs['payload']['query'])
        saved = self.store.trips.list_for_owner(event)[0]
        self.assertEqual(saved['spec']['start_date'], '2026-10-22')
        self.assertTrue(saved['spec']['date_is_tentative'])
        self.assertIn('description', saved['plan']['days'][0]['activities'][0])
        artifact = self.store.planning.capture(event, reply, destination='成都', duration_options=[1])
        followup = ChatEvent('onebot', 'group', 'followup', 'g', 'u', '查询这个行程的景点信息', occurred_at=NOW)
        selected, _ = resolve_plan(self.store, followup, {'plan_index': artifact['ordinal'], 'plan_reference': '这个行程'})
        self.assertEqual(selected['planning_spec']['start_date'], '2026-10-22')
        self.assertTrue(selected['planning_spec']['date_is_tentative'])
        view = ChatEvent('onebot', 'group', 'view', 'g', 'u', '查看行程', occurred_at=NOW)
        self.trip.handle(view, self.store.begin_event(view.event_key))
        self.assertEqual(self.http.call_count, 1)

    def test_source_restrictions_and_disabled_setting_do_not_auto_submit(self):
        for text in ('不要联网', '不联网', '只看官网', '只使用官网', '只用高德', '不要用问道', '只看携程网站', '只看携程'):
            event = ChatEvent('onebot', 'group', text, 'g', 'u', text, occurred_at=NOW)
            spec = {'destination': '成都', 'day_count': 1, 'start_date': '2026-10-22', 'date_is_tentative': True}
            self.assertIsNone(self.research.prepare_planning(event, spec))
        self.wendao.enabled = False
        event = ChatEvent('onebot', 'group', 'disabled', 'g', 'u', '规划成都一天行程', occurred_at=NOW)
        self.assertIsNone(self.research.prepare_planning(event, {'destination': '成都', 'day_count': 1}))
        self.http.assert_not_called()

    def test_failed_automatic_query_degrades_without_retrying(self):
        from infrastructure.public_http import PublicHTTPError
        self.http.side_effect = PublicHTTPError('network_error', attempts=1)
        event = ChatEvent('onebot', 'group', 'failed', 'g', 'u', '规划成都一天行程', occurred_at=NOW)
        reply = self.trip.handle(event, self.store.begin_event(event.event_key))
        self.assertIn('问道本次未取得完整回答', reply)
        self.assertEqual(self.http.call_count, 1)
        self.assertTrue(self.store.trips.list_for_owner(event))


class NanjingWebFlowTests(unittest.TestCase):
    def test_plain_plan_then_observed_followup_through_web_queue(self):
        from fastapi.testclient import TestClient
        from adapters.web_app import create_web_app
        from app.runtime_factory import build_runtime
        from core.settings import Settings
        from core.web_settings import WebSettings
        from infrastructure.env_settings_store import EnvSettingsStore

        names = [n for n in FIXTURE['plan']['content'].splitlines() if not n.startswith('第')]
        places = [{'id': 'B'+str(i), 'name': n, 'cityname': '南京市', 'address': '离线测试地址', 'type': '博物馆'}
                  for i, n in enumerate(names)]
        plan = {'days': [{'day_index': d+1, 'activities': [
            {'poi_id': 'B'+str(i), 'period': ('上午', '下午', '晚上')[i % 3], 'reason': '文化参观'}
            for i in range(d*3, d*3+3)]} for d in range(2)]}
        none = {'schema_version': 1, 'action': 'none', 'operations': [], 'missing_fields': [],
                'requires_confirmation': False, 'source_spans': [], 'confidence': 0.99}
        ir = {'schema_version': 1, 'action': 'read', 'operations': [{'domain': 'research',
            'operation': 'search_travel_guides', 'query_text': FIXTURE['followup'], 'plan_reference': '这个行程',
            'topics': ['opening_hours', 'ticket_price', 'reservation_rules']}], 'missing_fields': [],
            'requires_confirmation': False, 'source_spans': [FIXTURE['followup']], 'confidence': 0.99}
        extract = {'destination': '南京', 'duration_text': '两天', 'start_date_text': '后面的某个周末',
                   'preferences': [], 'must_visit': []}
        targets = {'targets': [{'name': n, 'source_text': n, 'day_index': i//3+1, 'optional': False,
                               'visit_mode': 'entry'} for i, n in enumerate(names)],
                   'topics': ir['operations'][0]['topics'], 'visit_start_date_text': '2026-10-22',
                   'arrival_date_text': '', 'requested_domains': [], 'excluded_domains': [],
                   'strict_sources': False, 'language': 'zh-cn'}
        model = FakeClient([model_response(none), model_response(extract), model_response(plan),
                            model_response(ir), model_response(targets)])
        model.close = Mock()
        amap = Mock()
        amap.search_places.return_value = places
        amap.search_poi_details.return_value = []
        answer = '\n'.join('### '+n+'\n这里的展陈与空间适合慢慢参观。\n- **开放时间**：每日开放时间09:00-17:00。'
                           for n in names)
        http = Mock(return_value=(answer.encode(), ENDPOINT, 'text/markdown'))
        def build(settings, **kwargs):
            return build_runtime(settings, **kwargs, client=model, amap_client=amap, search_client=Mock(configured=False))
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch('adapters.web_app.build_runtime', side_effect=build):
                app = create_web_app(WebSettings(data_dir=root, open_browser=False),
                    Settings('', '', frozenset(), 'fake', 'fake', 'https://example.test', 'fake'),
                    start_workers=False, settings_store=EnvSettingsStore(root, inherited={}))
            service = app.state.runtime.application.research_service
            service.wendao = WendaoClient('fixture-key', True, transport=http)
            service.attraction_research.registry = OfficialSources([])
            with TestClient(app, base_url='http://127.0.0.1:8080') as client:
                headers = {'x-csrf-token': client.get('/api/bootstrap').json()['csrf_token']}
                cid = client.post('/api/conversations', headers=headers).json()['id']
                for i, text in enumerate((FIXTURE['initial_request'], FIXTURE['followup'])):
                    response = client.post(f'/api/conversations/{cid}/messages', headers=headers,
                        json={'content': text, 'client_request_id': 'offline-'+str(i)})
                    self.assertEqual(response.status_code, 202)
                    asyncio.run(app.state.worker.run_once())
                replies = [m['content'] for m in client.get(f'/api/conversations/{cid}/messages').json()['items']
                           if m['role'] == 'assistant']
                self.assertEqual(len(replies), 2)
                self.assertTrue(all('研究请求校验失败' not in r for r in replies))
                self.assertIn('已获取携程问道参考', replies[0])
                self.assertIn('这里的展陈', replies[0])
                for _ in range(2):
                    self.assertTrue(client.get(f'/api/conversations/{cid}/research').json())
                self.assertEqual(http.call_count, 2)
                self.assertTrue(all('实际出发日尚未确定' in c.kwargs['payload']['query'] for c in http.call_args_list))
                state = client.get(f'/api/conversations/{cid}/context').json()
                self.assertEqual(len(state['trips']), 1)
                self.assertFalse(state['reminders'])


if __name__ == '__main__':
    unittest.main()
