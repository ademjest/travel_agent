from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core.chat_transport import ChatEvent
from infrastructure.memory_store import MemoryStore
from tools.reminders.policy import BookingPolicy
from tools.reminders.tool import RemindersTool, change_plan


class RemindersToolTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = MemoryStore(Path(temp.name)/'reminders.db')
        self.now = datetime(2030, 9, 1, 2, tzinfo=timezone.utc)
        self.tool = RemindersTool(self.store, clock=lambda: self.now)

    def event(self, text, key='request'):
        return ChatEvent('web', 'group', key, 'scope', 'owner', text, occurred_at=self.now)

    def test_prepared_operation_reads_tasks_once_and_does_not_route_again(self):
        event = self.event('查看我的提醒')
        with patch.object(self.store.tasks, 'recent', wraps=self.store.tasks.recent) as recent:
            selection = self.tool.prepare_request(event)
            self.assertEqual((selection.kind, selection.operation), ('personal', 'list'))
            with patch('tools.reminders.tool.reminder_request', side_effect=AssertionError('duplicate routing')):
                self.assertIn('没有通用提醒', self.tool.execute(selection, event, None))
            self.assertEqual(recent.call_count, 1)

    def test_future_visit_date_does_not_authorize_a_reminder(self):
        event = self.event('我明天去武汉，天气怎么样')
        self.assertIsNone(self.tool.prepare_request(event))
        self.assertEqual(self.store.reminders.list_for_owner('web', 'scope', 'owner'), ())

    def test_bound_semantic_fields_execute_without_keyword_routing(self):
        event = self.event('两分钟后，记得通知我喝水')
        with patch.object(self.tool, 'prepare_request', side_effect=AssertionError('must use bound intent')):
            reply = self.tool.handle_intent(event, self.store.delivery.begin_event(event.event_key),
                                           title='喝水', time_text='两分钟后')
        self.assertIn('已设置', reply)
        self.assertEqual(len(self.store.reminders.list_for_owner('web', 'scope', 'owner')), 1)

    def test_change_plan_requests_policy_then_returns_pure_uncommitted_changes(self):
        trip = {'trip_id': 'TR-test', 'version': 1, 'status': 'active',
                'spec': {'reminder_entities': ['测试馆']},
                'plan': {'days': [{'date': '2030-10-01', 'activities': [{'poi_id': 'p'}]}]},
                'sources': {'p': {'name': '测试馆'}}}
        original = deepcopy(trip)
        requested = change_plan('event', trip, [], now=self.now)
        self.assertEqual(requested.changes, [])
        self.assertEqual(requested.policy_requests, [('测试馆', '2030-10-01', '')])
        policy = BookingPolicy('测试馆', 'https://museum.example/policy', '每日0点可提前5天预约',
                               5, '00:00', self.now.isoformat(), 'fixture', True,
                               date_applicability={'date_status': 'applicable'})
        resolved = change_plan('event', trip, [], now=self.now, policies={'测试馆': policy})
        self.assertEqual(resolved.changes[0][0], 'create')
        self.assertEqual(resolved.changes[0][1]['reminder_id'], '')
        self.assertFalse(resolved.policy_requests)
        self.assertEqual(trip, original)
        self.assertEqual(self.store.reminders.list_for_owner('web', 'scope', 'owner'), ())
        for unsafe in [replace(policy, official=False),
                       replace(policy, date_applicability={'date_status': 'expired'})]:
            denied = change_plan('event', trip, [], now=self.now, policies={'测试馆': unsafe})
            self.assertFalse(denied.changes)
            self.assertTrue(denied.errors)
