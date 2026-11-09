"""Web queue/preview/save integration with explicitly scripted provider replies."""
from copy import deepcopy
import hashlib
import unittest
from unittest.mock import Mock

from test_draft_actions import WorkflowHarness
from test_trips import PLACES, model_response
from test_trip_presentation import ANSWER
from test_travel_agent import FakeClient
from tools.research.tool import ResearchTool
from tools.trip.presentation import TripAnswerWriter


class CulturalDraftWebTests(WorkflowHarness, unittest.TestCase):
    def test_lists_survive_dual_planning_preview_save_and_read_without_requery(self):
        places = {pid: {**row, 'cityname': '武汉市'} for pid, row in PLACES.items()}
        self.amap.search_places.return_value = list(places.values())
        source = ('## 第1天\n上午湖北省博物馆，下午测试美术馆。\n'
                  '## 第2天\n上午测试公园甲，下午测试公园乙。\n'
                  '## 第3天\n上午测试景点甲，下午测试景点乙。\n## 景点详情\n')
        source += '\n'.join('### '+p['name']+'\n- **特色亮点**：\n'
                            '  - **建筑看点**：院落与展陈共同记录当地文化记忆。\n'
                            '- **开放时间**：具体开放时间待核对。' for p in places.values())
        provider = Mock(configured=True)
        provider.query.return_value = {'status': 'completed', 'physical_calls': 1, 'text': source,
            'content_hash': hashlib.sha256(source.encode()).hexdigest(), 'retrieved_at': '2030-09-30T08:00:00+00:00'}
        self.tool.research_service = ResearchTool(self.web.store, Mock(configured=False), wendao=provider)
        self.models.completions.responses.append(model_response({'choice': 'amap', 'reason': '保留已核对的活动顺序'}))
        writer = FakeClient([model_response(ANSWER)])
        self.tool.answer_writer = TripAnswerWriter(self.web.store, writer, 'test')
        draft = self.create()
        candidate = self.candidate()
        self.assertTrue(all(a.get('description', {}).get('intro') for d in candidate['plan']['days'] for a in d['activities']))
        preview = deepcopy(self.web.messages()[-1])
        self.assertIn('建筑看点', preview['content'])
        self.assertTrue(preview['trip_presentation']['is_preview'])
        saved = self.save(draft)
        self.web.send('查看行程', key='read-saved'); self.assertTrue(self.web.run_job())
        self.assertIn('建筑看点', self.web.messages()[-1]['content'])
        self.assertEqual(saved['version'], 1)
        self.assertEqual(saved['plan'], candidate['plan'])
        self.assertEqual(provider.query.call_count, 1)
        self.assertEqual(len(self.models.completions.requests), 3)
        self.assertEqual(len(writer.completions.requests), 1)
        self.assertEqual(next(m['content'] for m in self.web.messages() if m['id'] == preview['id']), preview['content'])


if __name__ == '__main__':
    unittest.main()
