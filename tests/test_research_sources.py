from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from tools.research import sources


class ResearchSourcesTests(unittest.TestCase):
    def test_same_entity_can_have_distinct_dated_sections_or_reuse_undated_material(self):
        dated={'id':1,'text':'## 第一天\n### 测试馆\n门票：20元\n## 第二天\n### 测试馆\n门票：30元'}
        targets=[{'name':'测试馆','visit_mode':'entry','day_index':day} for day in (1,2)]
        result=sources.extract_candidates(dated,targets,{1:'2030-10-01',2:'2030-10-02'})
        prices=[f for f in result['facts'] if f['topic']=='ticket_price']
        self.assertEqual([(f['value_raw'],f['scope']['visit_date']) for f in prices],[('20元','2030-10-01'),('30元','2030-10-02')])
        shared={'id':1,'text':'## 测试馆\n门票：20元'}
        result=sources.extract_candidates(shared,targets,{1:'2030-10-01',2:'2030-10-02'})
        self.assertEqual({f['scope']['visit_date'] for f in result['facts']},{'2030-10-01','2030-10-02'})

    def test_free_base_admission_and_paid_extras_have_separate_product_scopes(self):
        for text in ('免费；园内探险项目另收费78元','免费（街区开放式）；实景互动体验9.9元'):
            source={'id':1,'text':'## 测试园\n普通票价：'+text+'\n预约规则：免费入园无需预约'}
            targets=[{'name':'测试园','visit_mode':'entry','day_index':1}]
            facts=sources.extract_candidates(source,targets,{1:'2030-10-01'})['facts']
            base=[f for f in facts if f['topic']=='ticket_price' and f['scope']['product']=='general']
            extras=[f for f in facts if f['topic']=='ticket_price' and f['scope']['product']=='special']
            self.assertEqual(len(base),1)
            self.assertEqual(base[0]['price_basis'],'direct')
            self.assertIn('免费',base[0]['value_raw'])
            self.assertEqual(len(extras),1)

    def test_independent_details_do_not_inherit_last_itinerary_day(self):
        text='## 第一天\n### 甲馆\n开放时间：09:00-17:00\n## 第三天\n### 乙园\n门票：免费\n## 景点介绍\n### 丙馆\n门票：20元\n### 甲馆\n开放时间：09:00-17:00'
        targets=sources.answer_targets(text)
        self.assertEqual([(t['name'],t['day_index']) for t in targets],[('甲馆',1),('乙园',3),('丙馆',None),('甲馆',None)])
        self.assertEqual(text.count('门票'),2)

    def setUp(self):
        self.source = {'id': 1, 'text': (
            '## 测试馆\n测试馆始建于1980年，是城市历史展览空间。每日09:00-17:00开放，门票20元。'
            '室内庭院保留传统建筑风貌。\n'
            '- **开放时间**：每日09:00-17:00\n'
            '- **玩法建议**：按兴趣欣赏展品。\n'
            '[来源](https://m.ctrip.com/sight?poiId=7&poiType=3)\n')}
        self.targets = [{'name': '测试馆', 'day_index': 1, 'visit_mode': 'entry'}]

    def test_current_source_never_invokes_legacy_and_preserves_original_spans(self):
        original = deepcopy(self.source)
        with patch.object(sources, '_legacy_extract', side_effect=AssertionError('legacy fallback')):
            result = sources.extract_candidates(self.source, self.targets, {1: '2030-10-01'})
        material = result['activity_materials'][0]
        self.assertIn('1980', material['intro'][0]['text'])
        self.assertFalse(any('20元' in row['text'] for row in material['intro']))
        for row in material['intro'] + material['tips']:
            self.assertEqual(self.source['text'][row['start']:row['end']], row['text'])
        for fact in result['facts']:
            self.assertEqual(fact['verification']['identity'], 'provider')
        self.assertEqual(result['provider_mappings'][0]['ctrip_poi_id'], '7')
        self.assertEqual(self.source, original)

    def test_legacy_requires_explicit_supported_version_and_unknown_does_not_guess(self):
        with self.assertRaises(TypeError):
            sources._legacy_extract(self.source, self.targets, {})
        tagged = {**self.source, 'extraction_version': sources.LEGACY_EXTRACTION_VERSION}
        with patch.object(sources, 'extract_blocks', side_effect=AssertionError('new parser on legacy path')):
            self.assertTrue(sources.extract_candidates(tagged, self.targets, {})['facts'])
            from tools.research.evidence import answer_view
            saved = {'sources': [{**tagged, 'provider': 'ctrip_wendao'}], 'provider_targets': self.targets}
            original = deepcopy(saved)
            self.assertEqual(answer_view(saved)['metrics']['target_count'], 1)
            self.assertEqual(saved, original)
        with self.assertRaises(ValueError):
            sources.extract_candidates({**self.source, 'extraction_version': 'unknown-future-version'}, self.targets, {})

    def test_source_binding_uses_prepared_policy_without_reinterpreting(self):
        selection = sources.source_selection('搜索测试馆的公开资料', automatic=True)
        with patch.object(sources, 'provider_policy', side_effect=AssertionError('duplicate selection')):
            policy, scope = sources.bind_source_selection(selection, {'strict_sources': True, 'requested_domains': ['example.com']})
        self.assertFalse(policy['requested'])
        self.assertEqual(scope['requested_domains'], ['example.com'])
        explicit = sources.source_selection('只用问道查询测试馆资料', automatic=True)
        self.assertTrue(sources.bind_source_selection(explicit, {'strict_sources': True})[0]['requested'])
        self.assertTrue(sources.source_selection('别再搜索，只整理已有资料')['offline'])

    def test_field_decoration_does_not_change_ticket_boundaries_or_evidence_spans(self):
        for header in ('- **普通票价**（适用日期2030-10-01，来源：景点信息）：',
                       '- **普通票价（适用日期2030-10-01，来源：景点信息）**：',
                       '- **普通票价：**', '普通票价：', '### 普通票价：'):
            with self.subTest(header=header):
                source = {'id': 1, 'text': '### 测试馆\n开放时间：每日09:00-17:00\n' + header +
                    '\n  - 成人票：27元\n  - 学生票：13元\n预约要求：公众号购票。\n'}
                original = deepcopy(source)
                result = sources.extract_candidates(source, self.targets, {1: '2030-10-01'})
                facts = result['facts']
                self.assertEqual(sum(f['topic'] == 'ticket_price' for f in facts), 2)
                opening = next(f for f in facts if f['topic'] == 'opening_hours')
                self.assertNotIn('票', opening['value_raw'])
                for fact in facts:
                    for span in [*fact['evidence'], fact['context_evidence']]:
                        self.assertEqual(source['text'][span['start']:span['end']], span['quote'])
                self.assertEqual(source, original)

    def test_request_113_real_response_keeps_prices_separate_from_hours(self):
        from tools.research.evidence import answer_view
        path = Path(__file__).parent / 'fixtures/wendao_nanjing_twelve_2026-10-24.json'
        data = json.loads(path.read_text(encoding='utf-8'))
        before = deepcopy(data)
        view = answer_view(data)
        self.assertEqual(view['metrics']['collected_target_count'], 12)
        for index, amount in ((0, '27元'), (2, '27元'), (4, '27元'), (5, '免费'), (6, '免费'), (7, '0元')):
            entry = view['entries'][index]
            price = next(s for s in entry['slots'] if s['topic'] == 'ticket_price')
            opening = next(s for s in entry['slots'] if s['topic'] == 'opening_hours')
            self.assertIn(amount, price['text'])
            self.assertIn(price['answer_state'], {'reference', 'limited'})
            self.assertNotIn('普通票价', opening['text'])
        price = next(s for s in view['entries'][0]['slots'] if s['topic'] == 'ticket_price')
        self.assertIn('2026-10-25', price['text'])
        self.assertIn('目标日期不同', price['text'])
        for index in (1, 3, 8, 9, 10):
            price = next(s for s in view['entries'][index]['slots'] if s['topic'] == 'ticket_price')
            self.assertNotIn('27元', price['text'])
            self.assertEqual(price['answer_state'], 'limited')
            self.assertRegex(price['text'], r'暂无官方(?:票价数据|数字)')
            self.assertIn('实时查询为准', price['text'])
        president = next(s for s in view['entries'][1]['slots'] if s['topic'] == 'ticket_price')
        self.assertNotEqual(president['text'], '免费')
        unknown = next(s for s in view['entries'][8]['slots'] if s['topic'] == 'ticket_price')
        self.assertNotIn('优惠', unknown['text'])
        self.assertEqual(data, before)

    def test_priority_policy_list_is_not_general_free_admission(self):
        from tools.research.evidence import answer_view
        for heading in ('优待政策', '- 优待政策', '- **优惠政策**'):
            source = {**self.source, 'provider': 'ctrip_wendao', 'text':
                '### 测试馆\n普通票价：\n- 成人票：暂无官方票价数据\n' + heading +
                '：儿童免费；老人优惠。\n预约要求：公众号预约。'}
            view = answer_view({'sources': [source], 'provider_targets': self.targets})
            price = next(s for s in view['entries'][0]['slots'] if s['topic'] == 'ticket_price')
            self.assertEqual(price['answer_state'], 'limited')
            self.assertIn('成人票：暂无官方票价数据', price['text'])
            self.assertIn('儿童免费；老人优惠', price['text'])
