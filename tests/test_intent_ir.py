import json
from types import SimpleNamespace
import unittest

from agents.intent_compiler import IntentCompiler
from core.intent_ir import parse_intent_ir, to_jsonable


class FakeClient:
    def __init__(self, value):
        self.value = value
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(self.value, ensure_ascii=False)))])


class IntentIRTests(unittest.TestCase):
    def test_compiler_parses_compound_trip_and_linked_reminder(self):
        text = '删除湖北省博物馆的行程并取消湖北省博物馆的预约提醒'
        value = {'schema_version': 1, 'action': 'update', 'operations': [
            {'domain': 'trip', 'operation': 'remove_activity', 'activity': {'name': '湖北省博物馆'}},
            {'domain': 'reminder', 'operation': 'cancel_linked_reminder', 'entity': '湖北省博物馆'}],
            'missing_fields': [], 'requires_confirmation': True,
            'source_spans': ['湖北省博物馆', '预约提醒'], 'confidence': .98}
        ir = IntentCompiler(FakeClient(value), 'test').compile(text)
        self.assertEqual(ir.action, 'update')
        self.assertEqual([item.operation for item in ir.operations], ['remove_activity', 'cancel_linked_reminder'])
        self.assertTrue(ir.requires_confirmation)
        self.assertEqual(to_jsonable(ir)['schema_version'], 1)

    def test_confirmation_and_read_are_structured(self):
        for text, action, operation in [('确认取消调整，请变更行程', 'confirm', 'confirm_pending_operation'), ('你还记得我的行程吗', 'read', 'view_current_trip')]:
            ir = IntentCompiler(FakeClient({'schema_version': 1, 'action': action,
                'operations': [{'domain': 'trip', 'operation': operation}], 'missing_fields': [],
                'requires_confirmation': False, 'source_spans': [], 'confidence': .9}), 'test').compile(text)
            self.assertEqual(ir.operations[0].operation, operation)

    def test_invalid_model_values_are_rejected(self):
        text = '删除湖北省博物馆的行程'
        for value in ({'schema_version': 2, 'action': 'update', 'operations': []},
                      {'schema_version': 1, 'action': 'update', 'operations': [{'domain': 'trip', 'operation': 'remove_activity', 'trip_id': 'TR-forged'}], 'source_spans': []},
                      {'schema_version': 1, 'action': 'update', 'operations': [{'domain': 'trip', 'operation': 'remove_activity', 'activity': {'trip_id': 'TR-forged'}}], 'source_spans': []},
                      {'schema_version': 1, 'action': 'none', 'operations': [{'domain': 'trip', 'operation': 'remove_activity'}], 'source_spans': []}):
            with self.assertRaises(ValueError): parse_intent_ir(value, text)

    def test_plain_chatter_is_not_actionable(self):
        ir = IntentCompiler(FakeClient({'schema_version': 1, 'action': 'none', 'operations': [],
            'missing_fields': [], 'requires_confirmation': False, 'source_spans': [], 'confidence': .99}), 'test').compile('午饭吃什么呀')
        self.assertFalse(ir.actionable)
