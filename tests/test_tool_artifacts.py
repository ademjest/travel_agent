from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from infrastructure.memory_store import MemoryStore
from tools.agent_tools import AgentToolContext, READ_TOOL_ARTIFACT_TOOL
from tools.reservation_tools import AgentToolRouter


class ToolArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = MemoryStore(Path(self.temp.name) / 'artifacts.db')
        travel = SimpleNamespace(execute_tool=lambda name, arguments: '甲' * 7000)
        self.router = AgentToolRouter(travel, SimpleNamespace(), SimpleNamespace(), self.store.tool_artifacts)
        self.context = AgentToolContext('web', 'conversation-a', 'owner', 'event-a')

    def test_large_result_is_persisted_and_can_be_read_in_pages(self):
        result = self.router.execute_result('get_current_weather', {'location': '西宁'}, self.context)
        self.assertEqual(result.status, 'completed')
        self.assertIn('TA-', result.data)
        artifact_id = result.data.split('TA-', 1)[1].split('；', 1)[0]
        artifact_id = 'TA-' + artifact_id

        page = self.router.execute_result(READ_TOOL_ARTIFACT_TOOL,
            {'artifact_id': artifact_id, 'cursor': 0, 'max_chars': 1000}, self.context)
        self.assertEqual(page.status, 'completed')
        self.assertIn('next_cursor', page.data)

    def test_artifact_is_isolated_by_conversation_and_owner(self):
        result = self.router.execute_result('get_current_weather', {'location': '西宁'}, self.context)
        artifact_id = 'TA-' + result.data.split('TA-', 1)[1].split('；', 1)[0]
        other = AgentToolContext('web', 'conversation-b', 'owner', 'event-b')
        denied = self.router.execute_result(READ_TOOL_ARTIFACT_TOOL,
            {'artifact_id': artifact_id}, other)
        self.assertEqual(denied.error_code, 'permission_denied')


if __name__ == '__main__':
    unittest.main()
