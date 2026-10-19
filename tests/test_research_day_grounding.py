"""Real itinerary structure; model responses are constructed, not recovered logs."""
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from agents.research_search_planner import ResearchSearchPlanner
from core.chat_transport import ChatEvent
from infrastructure.memory_store import MemoryStore
from services.research_service import ResearchService
from test_research_plan_repair import TEXT, target, scope_value
from test_travel_agent import FakeClient
from test_trips import model_response


BEIJING_PLAN = (Path(__file__).parent/'fixtures/research-day-sections-beijing-2026-10-19.md').read_text(encoding='utf-8')


class ResearchDayGroundingTests(unittest.TestCase):
    def setUp(self):
        self.plan = {'content':BEIJING_PLAN,'destination':'北京','duration_options':[3],
                     'title':'北京参考方案（3天）','version':2}
        self.request = {'original_request':TEXT,'query_text':TEXT,'plan':self.plan,
                        'topics':['ticket_price','opening_hours','reservation_rules'],
                        'arrival_date_text':'10月21日','visit_start_date_text':'22号'}

    def extract(self, rows, content=None):
        plan = {**self.plan,'content':content} if content is not None else self.plan
        client = FakeClient([model_response(scope_value(rows))])
        result = ResearchSearchPlanner(client,'test').plan_targets(self.request,plan)
        self.assertEqual(len(client.completions.requests),1)
        return result

    def test_markdown_prefix_in_source_quote_is_valid_day_evidence(self):
        row = {**target('故宫'), 'source_text':'### 第一天：天安门—故宫—景山—什刹海'}
        self.assertEqual(self.extract([row])['targets'][0]['day_index'],1)

    def test_quote_can_cross_next_heading_without_changing_named_attraction_day(self):
        start = BEIJING_PLAN.index('**注意：**')
        end = BEIJING_PLAN.index('**上午**',BEIJING_PLAN.index('### 第二天'))
        row = {**target('八达岭长城',2),'source_text':BEIJING_PLAN[start:end]}
        self.assertEqual(self.extract([row])['targets'][0]['day_index'],2)

    def test_wrong_model_day_is_corrected_from_actual_day_section(self):
        for hint in (1,8,'22号',True,None):
            with self.subTest(hint=hint):
                result = self.extract([target('鸟巢',hint,False,'entry')])
                self.assertEqual(result['targets'][0]['day_index'],3)
                self.assertEqual(result['targets'][0]['visit_mode'],'exterior')

    def test_overview_reference_is_resolved_against_itinerary_not_last_day(self):
        row = {**target('故宫',3),'source_text':'故宫、天安门区域、国家博物馆等热门场所，应提前查看官方预约要求。'}
        self.assertEqual(self.extract([row])['targets'][0]['day_index'],1)

    def test_plain_mentions_and_non_day_markdown_headings_do_not_split_days(self):
        content = '## 行程\n### 第一天：故宫\n提醒：第三天可能下雨。\n- 故宫可游览。\n### 第二天：长城\n- 长城游览。\n## 预算\n- 故宫价格待查。'
        row = {**target('故宫'),'source_text':'故宫可游览。'}
        self.assertEqual(self.extract([row],content)['targets'][0]['day_index'],1)

    def test_unassigned_or_ambiguous_day_does_not_use_unsupported_date(self):
        for content in ('北京参考方案：故宫与景山公园。',
                        '### 第一天：故宫\n故宫散步。\n### 第二天：故宫\n故宫游览。'):
            with self.subTest(content=content):
                result = self.extract([target('故宫',1)],content)
                row = result['targets'][0]
                self.assertIsNone(row['day_index'])
                self.assertEqual(ResearchService._target_date(row,{'visit_start_date':'2026-10-22'}),'')
                self.assertEqual(result['day_unresolved_count'],1)

    def test_invented_name_or_quote_is_still_rejected(self):
        for row in (target('不存在的景点'),{**target('故宫'),'source_text':'故宫当天已预订成功'}):
            with self.subTest(row=row),self.assertRaises(ValueError):
                self.extract([row])

    def test_plain_and_inline_day_headings_still_work(self):
        for content in ('第一天：故宫。\n第二天：景山公园。',
                        '第1天：故宫；第2天：景山公园。',
                        '## Day 1: 故宫\n## Day 2: 景山公园'):
            self.assertEqual(self.extract([target('景山公园',1)],content)['targets'][0]['day_index'],2)

    def test_bad_day_metadata_does_not_block_other_targets_or_search(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        store = MemoryStore(Path(folder.name)/'test.db')
        plan = {**self.plan,'content':BEIJING_PLAN+'\n## 补充景点\n- 北京动物园可选，尚未安排到具体一天。\n'}
        rows = [target('鸟巢',1,False,'entry'),target('北京动物园',2,True)]
        client = FakeClient([model_response(scope_value(rows)),model_response({'coverage':[]})])
        search = Mock(configured=True)
        search.search.return_value=[]
        service = ResearchService(store,search,client,'test',reader=Mock())
        event = ChatEvent('web','group','day-grounding','conversation','owner',TEXT,
                          occurred_at=datetime(2026,10,19,2,tzinfo=timezone.utc))
        reply = service.handle_intent(event,None,request={**self.request,'plan':plan})
        self.assertEqual(search.search.call_count,2)
        self.assertIn('2026-10-24',search.search.call_args_list[0].args[0])
        self.assertNotIn('2026-10-',search.search.call_args_list[1].args[0])
        self.assertIn('游览日待确认',reply)
        self.assertNotIn('尚未搜索',reply)
        data = store.research.get(event)
        self.assertEqual(data['target_scope']['day_corrected_count'],1)
        self.assertEqual(data['target_scope']['day_unresolved_count'],1)
