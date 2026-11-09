"""Date clarification must resume a structured draft, not free-form chat."""
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import unittest
from unittest.mock import Mock

from core.intent_ir import IntentIR, IntentOperation
from core.tasks import TaskUpdate
from test_draft_actions import WorkflowHarness
from test_travel_agent import FakeClient
from test_trips import model_response, PLACES, plan_value
from tools.trip.planner import TripPlanError, TripPlanner


REQUEST = ('请帮我规划2026年11月7日至9日的南京三日游，两位成年人，节奏轻松一些。'
           '我会在第一天早上到南京市区，第三天下午离开，先不用查询跨城交通。'
           '想去六朝博物馆、总统府和玄武湖，市内尽量步行或乘地铁，少打车。先给我草案，不要直接保存。')
REQUIREMENTS = {
    'destination': '南京', 'duration_text': '三日游', 'start_date_text': '2026年11月7日',
    'end_date_text': '9日', 'preferences': ['两位成年人', '节奏轻松一些', '少打车'],
    'must_visit': ['六朝博物馆', '总统府', '玄武湖'], 'transport_text': '尽量步行或乘地铁',
    'intercity': {'scope': {'value': 'none', 'source': '先不用查询跨城交通'}},
    'city_windows': {
        'arrival': {'day_index': 1, 'time_text': '早上', 'source': '第一天早上到南京市区'},
        'departure': {'day_index': 3, 'time_text': '下午', 'source': '第三天下午离开'},
    },
}


class DateRequirementTests(unittest.TestCase):
    def test_recorded_invalid_date_retains_non_date_requirements(self):
        planner = TripPlanner(FakeClient([model_response(REQUIREMENTS)]), 'test', None)
        with self.assertRaises(TripPlanError) as raised:
            planner.extract(REQUEST, {}, datetime(2026, 11, 8, 6, tzinfo=timezone.utc))
        error = raised.exception
        self.assertEqual(error.code, 'invalid_dates')
        self.assertNotIn('提醒', str(error))
        self.assertEqual(error.spec['must_visit'], REQUIREMENTS['must_visit'])
        self.assertEqual(error.spec['city_windows'], REQUIREMENTS['city_windows'])
        self.assertEqual(error.spec['intercity_request']['scope'], 'none')
        self.assertTrue(error.spec['gentle'])

    def test_recorded_range_and_single_date_resume_preserved_requirements(self):
        now = datetime(2026, 11, 8, 6, tzinfo=timezone.utc)
        for text, fields in [('11月17日到11月19日', {}), ('11月17日', {'start_date_text': '11月17日'})]:
            with self.subTest(text=text):
                planner = TripPlanner(FakeClient([model_response(REQUIREMENTS), model_response(fields)]), 'test', None)
                with self.assertRaises(TripPlanError) as raised:
                    planner.extract(REQUEST, {}, now)
                previous = raised.exception.spec
                previous.update(start_date_text='', end_date_text='', start_date='', end_date='')
                spec = planner.extract(text, previous, now)
                self.assertEqual(spec['start_date'], '2026-11-17')
                self.assertEqual(spec['day_count'], 3)
                self.assertEqual(spec['city_windows'], REQUIREMENTS['city_windows'])
                self.assertEqual(spec['must_visit'], REQUIREMENTS['must_visit'])
                self.assertEqual(spec['transport_text'], REQUIREMENTS['transport_text'])
                self.assertIn('两位成年人', spec['preferences'])


class DateContinuationWebTests(WorkflowHarness, unittest.TestCase):
    def date_question(self):
        # Keep this Web regression independent of the wall-clock calendar.
        text = REQUEST.replace('2026年11月7日', '2020年11月7日')
        requirements = {**REQUIREMENTS, 'start_date_text': '2020年11月7日'}
        self.models = FakeClient([model_response(requirements), model_response({})])
        self.tool.planner = TripPlanner(self.models, 'test', self.amap)
        self.tool.planner.add_route_evidence = Mock()
        def generated(event, spec, *args, **kwargs):
            plan = plan_value()
            for day in plan['days']:
                day['date'] = (date.fromisoformat(spec['start_date']) + timedelta(days=day['day_index'] - 1)).isoformat()
            return deepcopy(PLACES), plan
        self.tool._generate = Mock(side_effect=generated)
        self.compiler.try_compile.return_value = IntentIR(1, 'create', (
            IntentOperation('trip', 'create', {'request_text': text}),), source_spans=(text,))
        self.web.send(text, key='past-date'); self.assertTrue(self.web.run_job())
        pending = self.web.store.tasks.recent('web', self.web.event('').storage_scope_id, self.web.event('').sender_id)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].missing_slots, ('start_date',))
        self.assertEqual(pending[0].initial_request, text)
        self.tool._generate.assert_not_called()
        self.assertFalse(self.context()['confirmations'])
        self.compiler.try_compile.return_value = IntentIR(1, 'none', ())
        return pending[0]

    def test_date_range_followup_returns_web_presentation_and_save_control(self):
        self.date_question()
        # Reproduce the classifier's observed conversation decision. It must not
        # get a chance to drop the uniquely bound date clarification.
        self.compiler.try_compile.return_value = IntentIR(1, 'none', ())
        self.web.send('2099年11月17日到11月19日', key='new-date'); self.assertTrue(self.web.run_job())
        self.assertEqual(self.compiler.try_compile.call_count, 1)
        draft = self.context()['confirmations'][0]
        message = self.web.messages()[-1]
        self.assertTrue(message['trip_presentation']['is_preview'])
        candidate = self.candidate()
        self.assertEqual(candidate['spec']['start_date'], '2099-11-17')
        self.assertEqual(candidate['spec']['end_date'], '2099-11-19')
        self.assertEqual(candidate['spec']['must_visit'], REQUIREMENTS['must_visit'])
        self.assertEqual(candidate['spec']['city_windows'], REQUIREMENTS['city_windows'])
        self.assertEqual(candidate['spec']['intercity_request']['scope'], 'none')
        self.assertFalse(self.web.store.trips.list_for_owner(self.web.event('')))
        self.assertTrue(draft['can_confirm'])
        saved = self.save(draft)
        self.assertEqual(saved['version'], 1)
        self.assertEqual(saved['spec'], candidate['spec'])
        self.assertEqual(len(self.models.completions.requests), 2)

    def test_another_invalid_date_keeps_same_task_and_requirements(self):
        pending = self.date_question()
        self.web.send('2020年11月17日到11月19日', key='still-past'); self.assertTrue(self.web.run_job())
        task = self.web.store.tasks.owned(self.web.event(''), pending.task_id)
        self.assertEqual(task.missing_slots, ('start_date',))
        self.assertEqual(task.initial_request, pending.initial_request)
        self.assertEqual(task.slots['spec']['city_windows'], REQUIREMENTS['city_windows'])
        self.assertNotIn('提醒', self.web.messages()[-1]['content'])
        self.assertFalse(self.context()['confirmations'])
        self.tool._generate.assert_not_called()

    def test_unrelated_or_foreign_messages_do_not_resume_date_question(self):
        self.date_question()
        event = self.web.event('11月17日到11月19日', 'boundary')
        for other in (replace(event, scope_id='another-conversation'), replace(event, sender_id='another-owner'),
                      replace(event, content='你好')):
            with self.subTest(other=other):
                self.router.handle(other, None)
                self.tool._generate.assert_not_called()
        self.assertEqual(len(self.models.completions.requests), 1)

    def test_competing_task_or_expired_question_is_not_auto_resumed(self):
        self.date_question()
        event = self.web.event('11月17日到11月19日', 'competing')
        seed = replace(event, event_id='other-date-question', content='规划武汉三天')
        self.web.store.tasks.prepare_result(seed, self.web.store.delivery.begin_event(seed.event_key),
            TaskUpdate('itinerary', 'collecting', seed.content, {}, ('start_date',)), '哪天出发？')
        self.router.handle(event, None)
        self.tool._generate.assert_not_called()
        with self.web.store._connect() as db:
            db.execute("UPDATE travel_tasks SET expires_at='2000-01-01T00:00:00+00:00'")
        self.router.handle(replace(event, event_id='expired'), None)
        self.tool._generate.assert_not_called()
        self.assertEqual(len(self.models.completions.requests), 1)


if __name__ == '__main__':
    unittest.main()
