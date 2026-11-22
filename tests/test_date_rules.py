from datetime import date
import unittest

from tools.research.evidence import rule_applicability
from core.date_rules import temporal_rule
from tools.research.evidence import date_scope
from tools.research.sources import date_check


class DateRulesTests(unittest.TestCase):
    def test_open_ended_closure_is_consistent_across_consumers(self):
        text = '自2026年10月1日起临时闭馆，恢复时间另行通知。'
        for target, matching in [('2026-09-30', False), ('2026-10-01', True), ('2026-10-23', True)]:
            with self.subTest(target=target):
                result = rule_applicability({}, text, date.fromisoformat(target))
                self.assertIsNone(result['effective_until'])
                self.assertEqual(result['date_status'], 'applicable' if matching else 'not_yet_effective')
                expected = 'matching' if matching else 'outside'
                self.assertEqual(temporal_rule(text, target)['applicability'], expected)
                self.assertEqual(date_scope(text, target, {})[0], expected)
                self.assertEqual(date_check(text, '', target)['state'], expected)

    def test_closure_reaches_activity_guard_without_promoting_provider_trust(self):
        from tools.research.evidence import page_facts
        from tools.trip.planner import validate_activity
        text = '测试博物馆\n自2026年10月1日起临时闭馆，恢复时间另行通知。'
        source = {'id': 1, 'status': 'read', 'text': text,
                  'official_identity': {'status': 'verified', 'entity_key': 'museum'}}
        entry = {'entity_key': 'museum', 'canonical_name': '测试博物馆', 'aliases': [],
                 'page_entity_markers': ['测试博物馆']}
        facts = page_facts(source, {'name': '测试博物馆', 'visit_mode': 'entry'}, 't1',
                           ['opening_hours'], '2026-10-23', entry)
        self.assertTrue(any(f['status'] == 'official_verified' for f in facts))
        with self.assertRaisesRegex(ValueError, '闭馆'):
            validate_activity({'poi_id': 'p1', 'period': '上午'},
                              {'start_date': '2026-10-23', 'planning_evidence': {'p1': facts}}, {}, 1)
        source['official_identity'] = {'status': 'unverified'}
        untrusted = page_facts(source, {'name': '测试博物馆', 'visit_mode': 'entry'}, 't1',
                               ['opening_hours'], '2026-10-23', entry)
        self.assertFalse(any(f['status'] == 'official_verified' for f in untrusted))

    def test_annual_windows_require_explicit_recurrence_and_cross_year(self):
        for text in ['11月1日至3月31日开放。', '每年11月1日至3月31日开放。',
                     '暑期开放。', '每周一闭馆，节假日除外。', '2026年2月30日临时闭馆。']:
            with self.subTest(text=text):
                self.assertIn(date_scope(text, '2027-01-02', {})[0], {'unknown', 'unresolved'})
                self.assertNotEqual(date_check(text, '', '2027-01-02')['state'], 'matching')
        text = '每年11月1日至次年3月31日开放。'
        self.assertEqual(date_check(text, '', '2027-01-02')['state'], 'matching')
        self.assertEqual(date_scope(text, '2027-04-01', {})[0], 'outside')

    def test_explicit_range_and_single_day_are_not_open_ended(self):
        for text in ['2026年10月1日至2026年10月3日临时闭馆。', '2026年10月1日临时闭馆。']:
            self.assertEqual(date_check(text, '', '2026-10-23')['state'], 'outside')
            self.assertEqual(rule_applicability({}, text, date(2026, 10, 23))['date_status'], 'outside_declared_window')


if __name__ == '__main__':
    unittest.main()
