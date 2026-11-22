import unittest

from core.tool_result import ToolResult


class ToolResultTests(unittest.TestCase):
    def test_agent_requires_typed_action_and_explicit_write_receipt(self):
        from agents.travel_agent import TravelAgent
        from core.settings import Settings
        from types import SimpleNamespace
        settings = Settings('', '', frozenset(), '', 'fake', 'https://example.test', 'fake')
        for receipt in ['预约已确认', ToolResult('completed', 'wrong_action'),
                        ToolResult('completed', 'confirm_reservation_plan', data='预约已确认')]:
            with self.subTest(receipt=receipt):
                agent = TravelAgent(settings, lambda name, args, context: receipt, client=SimpleNamespace())
                with self.assertRaises(TypeError):
                    agent._invoke_tool('confirm_reservation_plan', {}, None)

    def test_query_state_originates_in_typed_executor(self):
        from core.settings import Settings
        from tools.travel_query import TravelQueryTool
        from unittest.mock import patch
        from types import SimpleNamespace
        service = TravelQueryTool(Settings('', '', frozenset(), '', '', '', ''))
        with patch.object(ToolResult, 'from_text', side_effect=AssertionError('text is not a protocol')):
            self.assertEqual(service.execute_result('get_current_weather', {}).error_code, 'not_available')
            service.amap = SimpleNamespace(search_places=lambda city, keywords: [])
            result = service.execute_result('search_travel_places', {'city': '武汉', 'keywords': '测试馆'})
            self.assertEqual((result.status, result.payload), ('completed', {'places': []}))
            self.assertEqual(service.execute_result('get_current_weather', {}).error_code, 'invalid_argument')

    def test_canonical_action_does_not_change_persisted_receipt(self):
        import json
        result = ToolResult('completed', 'get_current_weather', operation_key='existing-key')
        encoded = json.loads(result.to_json())
        self.assertEqual(result.canonical_action, encoded['action'])
        self.assertEqual(ToolResult(**encoded), result)

    def test_transient_and_permanent_errors_are_distinguished(self):
        transient = ToolResult.from_text('search', '工具错误：上游网络暂时超时，请稍后重试。')
        invalid = ToolResult.from_text('search', '工具错误：缺少 location 参数。')
        denied = ToolResult.from_text('search', '工具错误：没有权限读取该资源。')

        self.assertEqual((transient.error_code, transient.retryable), ('temporary_upstream', True))
        self.assertEqual(transient.retry_after_seconds, 5)
        self.assertEqual((invalid.error_code, invalid.retryable), ('invalid_argument', False))
        self.assertEqual((denied.error_code, denied.retryable), ('permission_denied', False))


if __name__ == '__main__':
    unittest.main()
