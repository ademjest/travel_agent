import json
import unittest

from agents.reminder_intent import ReminderIntentParser
from test_travel_agent import FakeClient, assistant_message, completion


class ReminderIntentTests(unittest.TestCase):
    def parser(self, value):
        return ReminderIntentParser(FakeClient([completion(assistant_message(content=json.dumps(value, ensure_ascii=False)))]), 'test')

    def test_original_spans_can_reorder_reminder_request(self):
        text = '提醒我明天上午十点带身份证'
        value = self.parser({'action': 'create', 'title': '带身份证', 'time_text': '明天上午十点'}).parse(text)
        self.assertEqual(value['title'], '带身份证')

    def test_model_cannot_invent_time_or_title(self):
        for value in (
            {'action': 'create', 'title': '带身份证', 'time_text': '2030-10-01 10:00'},
            {'action': 'create', 'title': '购买机票', 'time_text': '明天上午十点'},
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.parser(value).parse('提醒我明天上午十点带身份证')

    def test_unknown_action_is_not_a_write(self):
        with self.assertRaises(ValueError):
            self.parser({'action': 'send_money'}).parse('明天提醒我抢票')
