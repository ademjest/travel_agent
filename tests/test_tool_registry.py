from dataclasses import replace
import unittest
from unittest.mock import Mock

from core.tool_result import AgentToolContext, ToolResult
from core.tool_result import TOOLS, TOOLS_BY_NAME
from tools.registry import ToolRegistry, build_registry


class ToolRegistryTests(unittest.TestCase):
    def setUp(self):
        self.handler = Mock(side_effect=lambda name, args, context: ToolResult('completed', name))
        self.registry = build_registry(travel_query=self.handler, reservation=self.handler, artifact=self.handler)
        self.context = AgentToolContext('web', 'scope', 'owner', 'event',
                                       allowed_actions=frozenset({'cancel_reservation_plan'}),
                                       action_resources=(('cancel_reservation_plan', 'R-20300101-001'),))

    def test_original_names_and_search_actions_have_one_owner_and_schema(self):
        self.assertEqual(len(self.registry.definitions), 26)
        self.assertEqual(set(self.registry.definitions), set(TOOLS_BY_NAME))
        for name, definition in self.registry.definitions.items():
            self.assertIs(definition.schema, TOOLS_BY_NAME[name])
            self.assertEqual(definition.canonical_action, name)
            self.assertTrue(definition.owner)
        self.assertEqual(len(TOOLS), 26)

    def test_duplicate_ownership_is_rejected(self):
        definition = self.registry.definitions['get_current_weather']
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            ToolRegistry([definition, definition])

    def test_unknown_names_and_identity_injection_do_not_reach_handler(self):
        self.assertEqual(self.registry.execute('__dict__', {}, self.context).error_code, 'unknown_tool')
        for field in ['creator_id', 'allowed_actions', 'operation_key', 'canonical_action']:
            result = self.registry.execute('cancel_reservation_plan', {'plan_code': 'R-20300101-001', field: 'forged'}, self.context)
            self.assertEqual(result.error_code, 'invalid_argument')
        self.handler.assert_not_called()

    def test_operation_and_resource_permissions_are_independent(self):
        self.assertEqual(self.registry.execute('confirm_reservation_plan', {'plan_code': 'R-20300101-001'}, self.context).error_code, 'operation_denied')
        self.assertEqual(self.registry.execute('cancel_reservation_plan', {'plan_code': 'R-20300101-002'}, self.context).error_code, 'wrong_resource')
        self.assertEqual(self.registry.execute('cancel_reservation_plan', {'plan_code': 'R-20300101-001'}).error_code, 'missing_context')
        self.handler.assert_not_called()
        self.assertEqual(self.registry.execute('cancel_reservation_plan', {'plan_code': 'R-20300101-001'}, self.context).status, 'completed')
        self.assertIs(self.handler.call_args.args[2], self.context)

    def test_new_internal_registration_does_not_expand_model_catalog(self):
        original = self.registry.definitions['get_current_weather']
        definition = replace(original, name='internal_only', canonical_action='internal_only',
                             schema={'function': {'name': 'internal_only', 'parameters': {'type': 'object'}}})
        ToolRegistry([definition])
        self.assertNotIn('internal_only', TOOLS_BY_NAME)

    def test_required_arguments_and_returned_identity_are_enforced(self):
        result = self.registry.execute('get_current_weather', {})
        self.assertEqual(result.error_code, 'invalid_argument')
        self.handler.assert_not_called()
        self.handler.side_effect = None
        self.handler.return_value = ToolResult('completed', 'wrong')
        with self.assertRaises(TypeError):
            self.registry.execute('get_current_weather', {'location': '武汉'})
