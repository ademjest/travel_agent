from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from agents.trip_planner import TripPlanner, TripPlanError
from core.trip_diff import facts
from services.semantic_task_service import SemanticTaskService
from test_travel_agent import FakeClient
from test_trips import model_response


class ReferenceDateTests(unittest.TestCase):
    def extract(self, text, value, now=None, previous=None):
        return TripPlanner(FakeClient([model_response(value)]), 'test', None).extract(
            text, previous or {}, now or datetime(2026, 10, 21, 6, tzinfo=timezone.utc))

    def test_no_date_uses_message_tomorrow_and_full_two_day_range(self):
        result = self.extract('去南京玩两天', {'destination': '南京', 'duration_text': '两天'})
        self.assertEqual((result['start_date'], result['end_date']), ('2026-10-22', '2026-10-23'))
        self.assertTrue(result['date_is_tentative'])
        self.assertEqual(result['field_sources']['start_date'], 'system_reference')
        displayed = {r['field']: r for r in facts(result)}
        self.assertIn('暂定参考', displayed['start_date']['value'])
        self.assertIn('实际出发日期尚未确定', displayed['start_date']['source'])

    def test_actual_nanjing_vague_weekend_case_preserves_preference(self):
        text = '我想去南京玩两天，计划是后面的某个周末，我希望你为我规划一个旅游计划，从每天的出行到住宿以及餐饮。'
        result = self.extract(text, {'destination': '南京', 'duration_text': '两天', 'start_date_text': '后面的某个周末'})
        self.assertEqual(result['start_date'], '2026-10-22')
        self.assertEqual(result['date_preference_text'], '后面的某个周末')

    def test_beijing_midnight_year_boundary_and_leap_day(self):
        for instant, expected in [(datetime(2026, 12, 31, 15, 59, tzinfo=timezone.utc), '2027-01-01'),
                                  (datetime(2026, 12, 31, 16, 1, tzinfo=timezone.utc), '2027-01-02'),
                                  (datetime(2028, 2, 28, 6, tzinfo=timezone.utc), '2028-02-29')]:
            with self.subTest(instant=instant):
                result = self.extract('南京两天', {'destination': '南京', 'duration_text': '两天'}, now=instant)
                self.assertEqual(result['start_date'], expected)

    def test_reference_does_not_drift_when_user_adds_constraints_later(self):
        previous = self.extract('南京两天', {'destination': '南京', 'duration_text': '两天'})
        result = self.extract('轻松一点', {'preferences': ['轻松一点']},
                              now=datetime(2026, 10, 22, 6, tzinfo=timezone.utc), previous=previous)
        self.assertEqual(result['start_date'], '2026-10-22')
        self.assertEqual(result['date_reference_day'], '2026-10-21')
        self.assertTrue(result['date_is_tentative'])

    def test_explicit_date_and_date_range_override_reference(self):
        previous = self.extract('南京两天', {'destination': '南京', 'duration_text': '两天'})
        for text, value, expected in [('10月25日开始', {'start_date_text': '10月25日'}, ('2026-10-25', '2026-10-26')),
                                      ('10月25日到10月26日', {}, ('2026-10-25', '2026-10-26')),
                                      ('后天开始', {'start_date_text': '后天'}, ('2026-10-23', '2026-10-24'))]:
            with self.subTest(text=text):
                result = self.extract(text, value, previous=previous)
                self.assertEqual((result['start_date'], result['end_date']), expected)
                self.assertFalse(result['date_is_tentative'])
                self.assertNotIn('date_reference_day', result)

    def test_invalid_explicit_date_is_not_silently_replaced(self):
        with self.assertRaises(TripPlanError):
            self.extract('2026年2月30日去南京两天', {'destination': '南京', 'duration_text': '两天',
                                                   'start_date_text': '2026年2月30日'})

    def test_user_can_explicitly_ask_for_day_numbers_only(self):
        result = self.extract('南京两天，不要指定具体日期', {'destination': '南京', 'duration_text': '两天'})
        self.assertEqual(result['start_date'], '')
        self.assertTrue(result['defer_start_date'])
        next_spec = self.extract('轻松一点', {'preferences': ['轻松一点']}, previous=result)
        self.assertEqual(next_spec['start_date'], '')

    def test_clear_rejection_of_tomorrow_does_not_override_explicit_date(self):
        result = self.extract('不要按明天，按10月25日开始玩两天', {'duration_text': '两天', 'start_date_text': '10月25日'})
        self.assertEqual(result['start_date'], '2026-10-25')
        self.assertFalse(result['date_is_tentative'])

    def test_semantic_date_followup_uses_unique_reference_without_model(self):
        store = SimpleNamespace(trips=SimpleNamespace(list_for_owner=Mock(return_value=[{'spec': {'date_is_tentative': True}}])))
        compiler, trip_service = Mock(), Mock()
        trip_service.handle.return_value = '日期已更新'
        service = SemanticTaskService(store, compiler, trip_service=trip_service, mode='execute')
        service._pending = Mock(return_value=None)
        service._pending_reminders = Mock(return_value=[])
        event = SimpleNamespace(content='我确定10月25日出发', trip_edit=None)
        self.assertEqual(service.handle(event, None), '日期已更新')
        compiler.try_compile.assert_not_called()


if __name__ == '__main__':
    unittest.main()
