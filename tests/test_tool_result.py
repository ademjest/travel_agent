import unittest

from core.tool_result import ToolResult


class ToolResultTests(unittest.TestCase):
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
