from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from core.chat_transport import ChatEvent
from infrastructure.memory_store import MemoryStore
from tools.research.tool import ResearchTool


class ResearchToolTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = MemoryStore(Path(temp.name)/'research.db')

    def test_read_only_preparation_does_not_mutate_trip_parameters(self):
        provider = Mock(configured=True)
        provider.query.return_value = {'status':'failed', 'error_code':'timeout', 'physical_calls':1}
        tool = ResearchTool(self.store, Mock(configured=False), wendao=provider)
        event = ChatEvent('web', 'group', 'prepare', 'scope', 'owner', '帮我规划武汉两天行程')
        spec = {'destination':'武汉', 'day_count':2, 'start_date':'2030-10-01', 'end_date':'2030-10-02'}
        original = deepcopy(spec)
        prepared = tool.prepare_for_planning(event, spec)
        self.assertIsNone(prepared.source)
        self.assertTrue(prepared.note)
        self.assertEqual(spec, original)
        self.assertEqual(self.store.trips.list_for_owner(event), ())
        provider.query.assert_called_once()

    def test_reader_receives_the_source_policy_without_signature_probing(self):
        search = Mock(configured=True)
        search.search.return_value = [{'url':'https://zhihu.com/a','title':'武汉攻略',
                                      'snippet':'武汉公开旅行资料。','published_at':''}]
        calls = []

        def reader(url, *, url_allowed=None):
            calls.append(url)
            self.assertTrue(url_allowed(url))
            self.assertFalse(url_allowed('https://other.example/article'))
            return {'url':url,'status':'read','text':'武汉公园适合散步，出发前核对开放安排。',
                    'content_hash':'fixture','retrieved_at':'2030-09-01T00:00:00+00:00'}

        tool = ResearchTool(self.store, search, reader=reader)
        event = ChatEvent('web', 'group', 'read', 'scope', 'owner', '只看知乎搜索武汉攻略')
        self.assertTrue(tool.handle(event, self.store.delivery.begin_event(event.event_key)))
        self.assertEqual(calls, ['https://zhihu.com/a'])
        self.assertEqual(self.store.trips.list_for_owner(event), ())

    def test_saved_navigation_and_source_limited_read_never_query_or_rewrite(self):
        from test_official_links import navigation_data
        value=navigation_data()
        from tools.research.evidence import presentation
        value['answer_write']={'result':presentation(value)}
        value['answer_write']['result']['entries'][0]['practical'][0]['text']='其他来源的旧写作文案，不应沿用。'
        saved=ChatEvent('onebot','group','saved','scope','owner','介绍景点')
        self.store.research.save(saved,'partial',value)
        before=self.store.research.get(saved)
        search=Mock(configured=True)
        reader=Mock(side_effect=AssertionError('GET must never discover'))
        tool=ResearchTool(self.store,search,reader=reader)
        first=tool.web_reports(saved)
        self.assertEqual(first,tool.web_reports(saved))
        self.assertEqual(first[0]['presentation']['entries'][0]['official_link']['url'],value['official_links']['target-1']['url'])
        event=ChatEvent('onebot','group','saved-only','scope','owner','只用问道，只看已保存资料')
        constrained=tool.handle(event,None,force=True)
        self.assertNotIn(value['official_links']['target-1']['url'],constrained)
        self.assertNotIn('其他来源的旧写作文案',constrained)
        event=ChatEvent('onebot','group','saved-site-only','scope','owner','只看已保存资料，只看知乎')
        self.assertNotIn(value['official_links']['target-1']['url'],tool.handle(event,None,force=True))
        self.assertEqual(self.store.research.get(saved),before)
        self.assertIsNone(self.store.research.get(event))
        search.search.assert_not_called();reader.assert_not_called()

    def test_writing_failure_and_unknown_recovery_keep_navigation_without_new_queries(self):
        from test_official_links import navigation_data
        from tools.research.tool import write_answer
        value=navigation_data()
        value['destination']='南京'
        event=ChatEvent('onebot','group','fallback','scope','owner','介绍景点')
        self.store.research.save(event,'partial',value)
        search=Mock(configured=False)
        reader=Mock(side_effect=AssertionError('Saved navigation must be reused'))
        from services.official_sources import OfficialSources
        tool=ResearchTool(self.store,search,reader=reader,official_sources=OfficialSources([]))
        text=write_answer(tool,event,value)
        self.assertIn(value['official_links']['target-1']['url'],text)
        saved=self.store.research.get(event)
        self.assertEqual(saved['answer_write']['status'],'fallback')
        self.assertEqual(text,write_answer(tool,event,saved))
        search.search.assert_not_called();reader.assert_not_called()
