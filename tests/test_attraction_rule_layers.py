from copy import deepcopy
from datetime import date
import json
import unittest

from core.attraction_facts import page_facts, reconcile_facts
from core.attraction_projection import attraction_report, grouped_facts, planning_facts
from core.attraction_rules import classify_fact, date_scope
from test_attraction_projection import sample_data


def fact(text, topic='ticket_price', **changes):
    value = deepcopy(sample_data(1)['facts'][0])
    value.update(value_raw=text, topic=topic,
                 evidence=[{'kind': 'web_quote', 'quote': text, 'start': 0, 'end': len(text)}], **changes)
    return value


class RuleLayersTests(unittest.TestCase):
    def test_addon_annual_and_combined_tickets_are_not_general_admission(self):
        for text, kind in [('珍宝馆门票：10元/人；', 'addon'), ('钟表馆门票：10元/人；', 'addon'),
                           ('北京市公园游览年票价格100元。', 'annual_pass'), ('本园联票80元。', 'combined')]:
            with self.subTest(text=text):
                original = fact(text, status='official_verified')
                result = classify_fact(original)
                self.assertEqual(result['scope']['product'], 'special')
                self.assertEqual(result['scope']['product_kind'], kind)
                self.assertEqual(result['rule_scope']['bucket'], 'variants')
                self.assertNotEqual(result['status'], 'official_verified')
                self.assertEqual(original['scope']['product'], 'general')
                self.assertEqual(result['evidence'], original['evidence'])

    def test_occupation_and_theme_title_are_not_general_visitors(self):
        source = {'title': '关于2026年公共交通司乘人员主题免费开放日的公告'}
        result = classify_fact(fact('申请人须提前实名预约。', 'reservation_rules'), source)
        self.assertEqual(result['scope']['visitor_type'], 'occupation')
        self.assertEqual(result['rule_scope']['bucket'], 'variants')
        self.assertEqual(result['rule_scope']['context_quote'], source['title'])
        self.assertEqual(result['status'], 'unverified_source')
        result = classify_fact(fact('环卫工人凭证免费参观。'))
        self.assertEqual(result['scope']['visitor_type'], 'occupation')

    def test_explicit_single_day_event_outside_target_is_reference_only(self):
        original = fact('2026年1月7日（周三），故宫博物院对公共交通司乘人员免费开放。')
        classified = classify_fact(original)
        self.assertEqual(classified['rule_scope']['date_relevance'], 'outside')
        self.assertEqual(classified['rule_scope']['bucket'], 'reference')
        self.assertEqual(classified['status'], 'unknown')
        data = sample_data(1)
        data['facts'] = [original, fact('全年门票60元。')]
        projection = json.loads(planning_facts(data).split('\n', 1)[1])
        self.assertNotIn(original['value_raw'], [f['value_raw'] for f in projection['facts']])
        self.assertGreater(projection['omitted_facts'], 0)

    def test_recurring_season_is_relevant_but_never_promotes_trust(self):
        summer = fact('每年4月1日至10月31日为旺季，大门票60元/人；', status='official_date_pending')
        winter = fact('每年11月1日至次年3月31日为淡季，大门票40元/人；', status='official_date_pending')
        self.assertEqual(classify_fact(summer)['rule_scope']['bucket'], 'current')
        self.assertEqual(classify_fact(summer)['status'], 'official_date_pending')
        self.assertEqual(classify_fact(winter)['rule_scope']['bucket'], 'reference')
        winter['scope']['visit_date'] = '2027-01-02'
        self.assertEqual(classify_fact(winter)['rule_scope']['date_relevance'], 'matching')

    def test_missing_year_invalid_date_and_multiple_windows_do_not_guess(self):
        for text in ['11月3日主题免费开放。', '2026年2月30日，主题免费开放。',
                     '每年12月30日至1月3日免费参观。',
                     '每年4月1日至10月31日60元；每年11月1日至次年3月31日40元。']:
            self.assertIn(date_scope(text, '2026-10-22', {})[0], {'unknown', 'unresolved'})
        labelled = {'date_status': 'applicable', 'basis': 'explicit_labelled_source_text'}
        self.assertEqual(date_scope('11月3日主题免费开放。', '2026-10-22', labelled)[0], 'unresolved')

    def test_unresolved_rules_cannot_contradict_current_verified_rules(self):
        current = fact('全年开放时间09:00-17:00。', 'opening_hours', status='official_verified',
                       verification={'identity': 'official', 'date_applicability': 'applicable'})
        unknown = fact('11月3日开放时间10:00-18:00。', 'opening_hours', status='official_verified',
                       verification={'identity': 'official', 'date_applicability': 'applicable'},
                       applicability={'date_status': 'applicable', 'basis': 'explicit_labelled_source_text'})
        rows = [classify_fact(current), classify_fact(unknown)]
        self.assertEqual(rows[1]['status'], 'official_date_pending')
        self.assertEqual(rows[1]['rule_scope']['bucket'], 'reference')
        reconcile_facts(rows)
        self.assertEqual(rows[0]['status'], 'official_verified')

    def test_synonyms_merge_only_when_the_entire_rule_has_identical_conditions(self):
        a = fact('本馆实行实名预约参观制度。', 'reservation_rules')
        b = fact('观众须实名预约。', 'reservation_rules', source_id=2)
        self.assertEqual(len(grouped_facts([a, b])), 1)
        for text in ['观众须提前实名预约。', '观众须实名预约，儿童除外。', '观众无需提前预约。',
                     '观众须通过微信实名预约。', '观众无需预约。']:
            self.assertEqual(len(grouped_facts([a, fact(text, 'reservation_rules')])), 2)
        changed = deepcopy(b)
        changed['scope']['visit_date'] = '2026-10-23'
        self.assertEqual(len(grouped_facts([a, changed])), 2)
        self.assertEqual(len(grouped_facts([fact('无需预约。', 'reservation_rules'),
                                           fact('无需提前预约。', 'reservation_rules')])), 2)
        self.assertEqual(len(grouped_facts([fact('门票60元/人。'), fact('门票60元/团。')])), 2)

    def test_closing_and_last_entry_never_merge_with_opening_role(self):
        values = [fact(t, 'opening_hours') for t in ['开放时间09:00-17:00。', '停止入馆时间16:00。', '闭馆时间17:00。']]
        self.assertEqual(len(grouped_facts(values)), 3)
        self.assertEqual({classify_fact(f)['scope']['time_role'] for f in values}, {'opening', 'last_entry', 'closing'})

    def test_shared_scope_is_written_once_and_special_rules_are_separated(self):
        data = sample_data(1)
        data['facts'] = [fact('全年门票60元。', status='official_date_pending'),
                         fact('本馆实行实名预约参观制度。', 'reservation_rules', status='official_date_pending'),
                         fact('珍宝馆门票10元。', status='official_date_pending'),
                         fact('2026年1月7日（周三），本馆对环卫工人免费开放。')]
        full = attraction_report(data, detailed=True)
        self.assertEqual(full.count('目标日期/例外待确认'), 1)
        self.assertNotIn('一般入馆，条件见原文', full)
        primary = full.split('#### 普通入馆相关条款', 1)[1].split('\n#### ', 1)[0]
        self.assertNotIn('珍宝馆', primary)
        self.assertNotIn('环卫工人', primary)
        self.assertIn('#### 票种、优惠与附加项目', full)
        self.assertIn('#### 历史、范围不符或日期待确认资料', full)
        self.assertIn('#### 原始证据与核验细节', full)
        self.assertIn('2026年1月7日', full)

    def test_original_semantic_variants_and_all_sources_remain_accessible(self):
        data = sample_data(1)
        data['sources'].append({**data['sources'][0], 'id': 2, 'url': 'https://museum.example/second'})
        data['facts'] = [fact('本馆实行实名预约参观制度。', 'reservation_rules'),
                         fact('观众须实名预约。', 'reservation_rules', source_id=2)]
        original = deepcopy(data)
        full = attraction_report(data, detailed=True)
        self.assertIn('本馆实行实名预约参观制度。', full)
        self.assertIn('同义原文[2]：观众须实名预约。', full)
        self.assertIn('[1][2]', full)
        self.assertEqual(data, original)

    def test_legacy_coverage_does_not_count_previously_misclassified_addon(self):
        data = sample_data(1)
        data['coverage'] = {'covered_count': 1}
        data['facts'] = [fact('钟表馆门票10元。', status='official_verified')]
        full = attraction_report(data, detailed=True)
        self.assertIn('逐项官方核验：0/3', full)
        self.assertEqual(data['coverage']['covered_count'], 1)

    def test_new_extraction_classifies_before_coverage_or_conflicts(self):
        text = '测试博物馆\n生效日期：2026-01-01\n有效期至：2026-12-31\n珍宝馆门票10元。'
        from test_official_attraction_research import entry, page
        from services.official_sources import OfficialSources
        source = {**page(text), 'id': 1,
                  'official_identity': OfficialSources([entry()]).verify('测试博物馆', 'https://museum.example/visitor/policy')}
        result = page_facts(source, {'name': '测试博物馆', 'visit_mode': 'entry'}, 'target-1',
                            ['ticket_price'], '2026-10-22', entry())
        self.assertEqual(result[0]['scope']['product_kind'], 'addon')
        self.assertNotEqual(result[0]['status'], 'official_verified')

    def test_adjacent_season_headings_keep_duplicate_times_in_distinct_scopes(self):
        from test_official_attraction_research import entry, page
        from services.official_sources import OfficialSources
        text = ('测试博物馆\n每年4月1日至10月31日采用旺季开放时间\n开放入馆时间：8:30\n停止入馆时间：16:00\n'
                '每年11月1日至来年3月31日采用淡季开放时间\n开放入馆时间：8:30\n停止入馆时间：15:30\n')
        source = {**page(text), 'id': 1,
                  'official_identity': OfficialSources([entry()]).verify('测试博物馆', 'https://museum.example/visitor/policy')}
        rows = page_facts(source, {'name': '测试博物馆', 'visit_mode': 'entry'}, 'target-1',
                          ['opening_hours'], '2026-10-22', entry())
        duplicate = [f for f in rows if f['value_raw'] == '开放入馆时间：8:30']
        self.assertEqual(len(duplicate), 2)
        self.assertNotEqual(duplicate[0]['evidence'][-1]['start'], duplicate[1]['evidence'][-1]['start'])
        self.assertEqual({f['rule_scope']['bucket'] for f in duplicate}, {'current', 'reference'})
        self.assertFalse(any(f['status'] == 'official_verified' for f in duplicate))
        for row in rows:
            if 'heading_evidence' in row['rule_scope']:
                h = row['rule_scope']['heading_evidence']
                self.assertEqual(text[h['start']:h['end']], h['quote'])
        old = fact('开放入馆时间：8:30', 'opening_hours')
        self.assertEqual(classify_fact(old, source)['rule_scope']['date_basis'], 'ambiguous_repeated_clause_context')

    def test_qa_promotional_text_is_not_a_rule_and_subarea_is_not_whole_park(self):
        data = sample_data(1)
        data['facts'] = [fact('问题1：北海公园门票能现场买吗？需要提前预约吗？', 'reservation_rules'),
                         fact('温馨提示：微信搜索关注公众号获取预约入口。', 'reservation_rules'),
                         fact('西天梵境建筑群免费开放，凭北海公园大门票即可进入。')]
        groups = grouped_facts(data['facts'])
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]['rule_scope']['bucket'], 'variants')

    def test_source_body_with_different_seasons_is_not_inherited_across_sections(self):
        source = {'text': '每年4月1日至10月31日采用旺季开放时间\n导览地图\n开放入馆时间：8:30'}
        result = classify_fact(fact('开放入馆时间：8:30', 'opening_hours'), source)
        self.assertNotIn('rule_window', result['scope'])
        self.assertEqual(result['rule_scope']['date_relevance'], 'unknown')


if __name__ == '__main__':
    unittest.main()
