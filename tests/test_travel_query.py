from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from core.chat_transport import ChatEvent
from core.event_idempotency import event_operation_key
from core.tool_result import AgentToolContext
from infrastructure.memory_store import MemoryStore
from tools.travel_query import TravelQueryTool
from test_read_only_answer import WEATHER


class TravelQueryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = MemoryStore(Path(temp.name)/'query.db')
        self.backend = SimpleNamespace(manages_read_retries=False, execute_result=Mock(return_value=WEATHER))
        self.tool = TravelQueryTool(store=self.store, backend=self.backend)
        self.event = ChatEvent('web', 'group', 'event', 'scope', 'owner', '查询天气 武汉')
        self.claim = self.store.delivery.begin_event(self.event.event_key)
        self.context = AgentToolContext('web', 'scope', 'owner', self.event.event_key)

    def test_command_task_and_agent_tool_share_successful_query_checkpoint(self):
        reply = self.tool.prepare(self.event, self.claim, self.tool.command_ir(self.event.content), source='command')
        result = self.tool.execute_scoped_result('get_current_weather', {'location': '武汉'}, self.context)
        self.assertIn('20℃', reply)
        self.assertEqual(result.data, WEATHER.data)
        self.backend.execute_result.assert_called_once_with('get_current_weather', {'location': '武汉'})

    def test_existing_operation_key_is_replayed_without_calling_provider(self):
        arguments = {'location': '武汉'}
        key = event_operation_key(self.event.event_key, 'read:get_current_weather', arguments)
        self.store.delivery.save_event_tool_result(key, self.event.event_key, 'get_current_weather', arguments, WEATHER.to_json())
        self.assertEqual(self.tool.execute_scoped_result('get_current_weather', arguments, self.context), WEATHER)
        self.backend.execute_result.assert_not_called()

    def test_contextless_command_does_not_reuse_an_owned_events_snapshot(self):
        self.tool.execute_scoped_result('get_current_weather', {'location': '武汉'}, self.context)
        for _ in range(2):
            self.assertIn('20℃', self.tool.command('查询天气 武汉'))
        self.assertEqual(self.backend.execute_result.call_count, 3)
