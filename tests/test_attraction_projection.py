from copy import deepcopy
import json
import unittest

from core.attraction_projection import attraction_report, grouped_facts, planning_facts, poi_detail_view
from core.attraction_facts import page_facts, provider_facts
from infrastructure.amap_client import normalize_poi_detail


def sample_data(target_count=19):
    data = {'fact_schema': 'attraction-facts-v1', 'plan_title': '多景点回归方案',
            'target_scope': {'targets': [{'name': f'景点{i + 1}', 'visit_mode': 'entry'} for i in range(target_count)],
                             'fields': {'topics': ['opening_hours', 'ticket_price', 'reservation_rules']}},
            'facts': [], 'sources': [], 'place_details': [],
            'coverage': {'covered_count': 0}, 'request_counts': {'amap': 4, 'pages': 4, 'search': 0}}
    for i in range(min(4, target_count)):
        sid = i + 1
        data['sources'].append({'id': sid, 'type': 'web', 'provider': 'public_web',
                               'url': f'https://museum.example/policy/{sid}', 'retrieved_at': '2026-10-20T09:00:00+00:00'})
        data['place_details'].append({'target_id': f'target-{sid}', 'status': 'matched', 'detail': None})
        for topic, text in [('opening_hours', '全年09:00-17:00开放，16:00停止入馆。'),
                            ('ticket_price', '未成年人免费参观，但须预约。未满14周岁须有成年人陪护，1名成年人最多可带3名未满14周岁未成年人。'),
                            ('reservation_rules', '未成年人免费参观，但须预约。未满14周岁须有成年人陪护，1名成年人最多可带3名未满14周岁未成年人。')]:
            data['facts'].append({'schema_version': data['fact_schema'], 'target_id': f'target-{sid}',
                'entity_poi_id': None, 'topic': topic, 'value_raw': text, 'source_id': sid,
                'scope': {'visit_date': '2026-10-22', 'visit_mode': 'entry', 'visitor_type': 'general', 'product': 'general'},
                'status': 'unverified_source', 'verification': {'identity': 'unverified', 'date_applicability': 'unknown'},
                'evidence': [{'kind': 'web_quote', 'quote': text, 'start': 0, 'end': len(text)}]})
    return data


class AttractionProjectionTests(unittest.TestCase):
    def test_amap_preprocessing_retains_raw_and_both_time_roles(self):
        detail = normalize_poi_detail({'id': 'test', 'business': {
            'tel': '027-123；027-123; 027-456', 'opentime_today': ' 09:00-17:00\n停止入馆16:00 ',
            'opentime_week': '09:00-17:00 停止入馆16:00'}}, '/v5/place/text', '2026-10-20T09:00:00+00:00')
        original = deepcopy(detail)
        view = poi_detail_view(detail)
        self.assertEqual(view['phone'], '027-123；027-456')
        self.assertEqual(len(view['openings']), 1)
        self.assertEqual(view['openings'][0]['fields'], ['opening_today_raw', 'opening_week_raw'])
        self.assertEqual(detail, original)

    def test_groups_identical_clause_across_topics_but_not_scope_or_trust(self):
        data = sample_data(1)
        groups = grouped_facts(data['facts'])
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[1]['topics'], ['ticket_price', 'reservation_rules'])
        changed = deepcopy(data['facts'][-1])
        changed['scope']['visit_date'] = '2026-10-23'
        self.assertEqual(len(grouped_facts([*data['facts'], changed])), 3)
        changed = deepcopy(data['facts'][-1])
        changed['status'] = 'official_date_pending'
        self.assertEqual(len(grouped_facts([*data['facts'], changed])), 3)

    def test_same_provider_text_displays_once_without_losing_today_week_applicability(self):
        data = sample_data(1)
        detail = normalize_poi_detail({'id': 'poi', 'business': {
            'opentime_today': '09:00-17:00', 'opentime_week': '09:00-17:00'}},
            '/v5/place/text', '2026-10-20T09:00:00+00:00')
        data['facts'] = provider_facts(detail, 1, 'target-1', '2026-10-22', 'entry')
        groups = grouped_facts(data['facts'])
        self.assertEqual(len(groups), 1)
        full = attraction_report(data, detailed=True)
        self.assertEqual(full.count('09:00-17:00'), 1)
        self.assertIn('不适用于目标日', full)
        self.assertIn('常规字段', full)
        value = json.loads(planning_facts(data).split('\n', 1)[1])
        observations = value['facts'][0]['provider_observations']
        self.assertEqual({o['verification']['date_applicability'] for o in observations}, {'not_target_day', 'unknown'})

    def test_full_report_has_complete_conditions_and_centralized_sources(self):
        data = sample_data()
        data['facts'] *= 10
        full = attraction_report(data, detailed=True)
        clause = data['facts'][1]['value_raw']
        self.assertEqual(full.count(clause), 4)
        self.assertEqual(full.count('https://museum.example/policy/1'), 1)
        self.assertIn('尚未查询', full)
        self.assertIn('景点19', full)

    def test_brief_bounds_whole_clauses_and_covers_all_processed_targets(self):
        data = sample_data()
        for fact in data['facts']:
            fact['value_raw'] = '条件完整' * 2000 + '但不得单独入馆。'
        brief = attraction_report(data)
        self.assertLessEqual(len(brief), 6000)
        self.assertNotIn('条件完整', brief)
        self.assertIn('4/19', brief)
        self.assertIn('待处理 15', brief)
        for i in range(1, 5):
            self.assertIn(f'### 景点{i}', brief)
        self.assertIn('完整条款超出', brief)
        self.assertNotIn('回复已截断', brief)

    def test_planning_round_robins_all_four_targets_without_cutting_evidence(self):
        data = sample_data()
        data['facts'] = [f for i in range(1, 5) for f in data['facts'] if f['target_id'] == f'target-{i}'] * 20
        text = planning_facts(data)
        value = json.loads(text.split('\n', 1)[1])
        self.assertLessEqual(len(text), 6000)
        self.assertEqual({f['target_id'] for f in value['facts']}, {f'target-{i}' for i in range(1, 5)})
        self.assertEqual(len(value['pending_target_ids']), 15)
        self.assertTrue(all(f['evidence'][0]['quote'] == f['value_raw'] for f in value['facts']))
        self.assertTrue(all(f['source_id'] in {s['id'] for s in value['sources']} for f in value['facts']))

    def test_oversized_fact_does_not_block_later_targets(self):
        data = sample_data()
        data['facts'][0]['value_raw'] = '很长' * 5000
        text = planning_facts(data)
        value = json.loads(text.split('\n', 1)[1])
        self.assertLessEqual(len(text), 6000)
        self.assertTrue(any(f['target_id'] == 'target-4' for f in value['facts']))
        self.assertGreater(value['omitted_facts'], 0)

    def test_conflict_is_not_presented_as_one_conclusion_or_one_sided_planning(self):
        data = sample_data(1)
        fact = data['facts'][0]
        fact['status'] = 'conflict'
        other = deepcopy(fact)
        other['value_raw'] = '全年10:00-18:00开放。'
        data['facts'].append(other)
        brief = attraction_report(data)
        self.assertIn('规则冲突，不能确定', brief)
        self.assertNotIn(fact['value_raw'], brief)
        value = json.loads(planning_facts(data).split('\n', 1)[1])
        self.assertEqual(len([f for f in value['facts'] if f['status'] == 'conflict']), 2)

    def test_navigation_and_duplicate_paragraphs_do_not_fill_fact_quota(self):
        clause = '测试馆全年开放时间09:00-17:00。'
        source = {'id': 1, 'text': '提前预订测试馆门票\n' + '\n'.join([clause] * 30), 'status': 'read'}
        facts = page_facts(source, {'name': '测试馆', 'visit_mode': 'entry'}, 'target-1',
                           ['opening_hours', 'reservation_rules'], '2026-10-22')
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]['value_raw'], clause)
        self.assertEqual(source['omitted_fact_count'], 0)


if __name__ == '__main__':
    unittest.main()
