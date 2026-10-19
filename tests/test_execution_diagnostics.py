import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from unittest.mock import Mock
from datetime import datetime, timedelta, timezone

from app.runtime_factory import build_runtime
from core.chat_transport import ChatEvent
from core.settings import Settings
from infrastructure.memory_store import MemoryStore
from scripts.export_execution_timeline import export_timeline
from core.model_budget import MODEL_EVENT, ModelBudgetExceeded
from test_runtime_factory import FakeRenderer, FakeTransport


class ExecutionDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'diagnostics.db'
        self.store = MemoryStore(self.path)
        self.event = ChatEvent('onebot', 'group', 'event', 'group', 'owner', 'private message')
        self.store.save_chat_message(self.event.event_key, 'onebot', 'group', 'owner',
                                    'event', '', 'user', self.event.content)

    def test_runtime_connects_summary_and_context_without_model_calls(self):
        runtime = build_runtime(Settings('', '', frozenset(), '', '', '', ''), platform='onebot',
            transport=FakeTransport(), reply_renderer=FakeRenderer(), group_allowed=lambda _: True, store=self.store)
        with patch.dict('os.environ', {'AI_SUMMARY_ENABLED': 'false'}):
            runtime.application.context_builder.build(self.event)
        rows = export_timeline(self.path, self.event.event_key)['timeline']
        self.assertEqual([r['stage'] for r in rows], ['summary', 'context'])
        self.assertEqual(rows[0]['details']['status'], 'disabled')
        self.assertFalse(rows[1]['details']['summary_injected'])
        self.assertNotIn('private message', json.dumps(rows))

    def test_only_allowlisted_metadata_is_stored(self):
        self.store.diagnostics.record('summary', {'event_key': self.event.event_key, 'status': 'validation_failed',
            'prompt_hash': 'a' * 64, 'raw_text': 'secret', 'api_key': 'key',
            'validation': {'code': 'sources_count', 'path': 'items[0].sources', 'actual': 5, 'max': 4, 'quote': 'secret'}})
        rows = export_timeline(self.path, self.event.event_key)['timeline']
        self.assertEqual(rows[0]['details']['validation']['actual'], 5)
        self.assertNotIn('secret', json.dumps(rows))
        self.assertNotIn('api_key', json.dumps(rows))

    def test_write_failure_is_isolated_and_does_not_log_exception_body(self):
        with patch.object(self.store.diagnostics, '_write', side_effect=ValueError('secret exception')):
            with self.assertLogs('infrastructure.execution_diagnostics', level='WARNING') as logs:
                self.store.diagnostics.record('context', {'event_key': self.event.event_key})
        self.assertNotIn('secret exception', str(logs.output))

    def test_unknown_event_cannot_create_detached_diagnostics(self):
        self.store.diagnostics.record('context', {'event_key': 'missing', 'total_chars': 12})
        self.assertEqual(export_timeline(self.path, 'missing')['timeline'], [])

    def test_runtime_correlates_model_context_and_tool_metadata(self):
        from test_travel_agent import FakeClient, completion, assistant_message, tool_call
        from tools.agent_tools import AgentToolContext
        from core.tool_result import ToolResult
        client = FakeClient([completion(assistant_message(tool_calls=[
            tool_call('weather', 'get_current_weather', '{"location":"武汉"}') ])),
            completion(assistant_message(content='固定天气结果'))])
        runtime = build_runtime(Settings('', '', frozenset(), '', 'fake', 'https://example.test', 'fake'),
            platform='onebot', transport=FakeTransport(), reply_renderer=FakeRenderer(), group_allowed=lambda _: True,
            store=self.store, client=client)
        runtime.travel_service.execute_result = Mock(return_value=ToolResult('completed', 'get_current_weather', data='固定天气结果'))
        token = MODEL_EVENT.set(self.event.event_key)
        self.addCleanup(MODEL_EVENT.reset, token)
        with patch.dict('os.environ', {'AI_SUMMARY_ENABLED': 'false'}):
            context = runtime.application.context_builder.build(self.event)
            result = runtime.travel_agent.run('武汉天气', context, tool_context=AgentToolContext(
                'onebot', 'group', 'owner', self.event.event_key))
        self.assertEqual(result.status, 'completed')
        report = export_timeline(self.path, self.event.event_key)
        stages = [row['stage'] for row in report['timeline']]
        self.assertTrue({'model', 'context', 'tool', 'summary'} <= set(stages))
        self.assertEqual(report['model_calls'], 2)
        model_rows = [row for row in report['timeline'] if row['stage'] == 'model']
        self.assertEqual(len({row['details']['call_id'] for row in model_rows}), 2)
        self.assertNotIn('固定天气结果', json.dumps(report, ensure_ascii=False))
        with self.assertRaises(ModelBudgetExceeded):
            runtime.travel_agent.client.create(model='fake', messages=[{'role':'user', 'content':'甲' * 70000}])
        self.assertEqual(export_timeline(self.path, self.event.event_key)['timeline'][-1]['details']['status'], 'budget_blocked')

    def test_diagnostics_retention_and_schema_initialization_are_idempotent(self):
        from services.maintenance import MaintenanceService
        self.store.diagnostics.record('summary', {'event_key': self.event.event_key, 'status': 'disabled'})
        with self.store._connect() as db:
            db.execute('UPDATE execution_diagnostics SET created_at=?',
                       ((datetime.now(timezone.utc) - timedelta(days=40)).isoformat(),))
        MemoryStore(self.path)
        self.assertEqual(len(export_timeline(self.path, self.event.event_key)['timeline']), 1)
        report = MaintenanceService(self.store, self.path.parent / 'images').run_once()
        self.assertEqual(report['execution_diagnostics'], 1)
        self.assertEqual(export_timeline(self.path, self.event.event_key)['timeline'], [])

    def test_stale_task_commit_is_identifiable_without_recording_reply(self):
        from core.tasks import TaskUpdate
        from infrastructure.task_repository import TaskConflict
        claim = self.store.begin_event(self.event.event_key)
        update = TaskUpdate('weather', 'collecting', 'private request', {}, ('location',))
        self.store.tasks.prepare_result(self.event, claim, update, 'private reply')
        self.store.complete_event(claim.event_id, claim.claim_token)
        with self.assertRaises(TaskConflict):
            self.store.tasks.prepare_result(self.event, claim, update, 'private reply')
        report = export_timeline(self.path, self.event.event_key)
        self.assertEqual(report['timeline'][-1]['details']['status'], 'task_conflict')
        self.assertNotIn('private reply', json.dumps(report))

    def test_export_is_readonly_and_missing_database_is_not_created(self):
        before = self.path.read_bytes()
        export_timeline(self.path, self.event.event_key)
        self.assertEqual(self.path.read_bytes(), before)
        absent = self.path.parent / 'absent.db'
        with self.assertRaises(FileNotFoundError):
            export_timeline(absent, 'missing')
        self.assertFalse(absent.exists())

    def test_deleted_web_conversation_rejects_late_diagnostics(self):
        from adapters.web_app import create_web_app
        from core.web_settings import WebSettings
        app = create_web_app(WebSettings(data_dir=Path(self.folder.name) / 'web', open_browser=False),
                             Settings('', '', frozenset(), '', '', '', ''), start_workers=False)
        repo = app.state.repository
        identity = repo.create_conversation('test')['id']
        event = ChatEvent('web', 'group', 'message', identity, 'local-owner', 'private')
        repo.store.save_chat_message(event.event_key, 'web', identity, 'local-owner', 'message', '', 'user', 'private')
        details = {'event_key': event.event_key, 'status': 'disabled'}
        repo.store.diagnostics.record('summary', details)
        self.assertEqual(len(export_timeline(repo.store.database_path, event.event_key)['timeline']), 1)
        app.state.deletion.request(identity)
        repo.store.diagnostics.record('summary', details)
        app.state.deletion.cleanup(identity)
        repo.store.diagnostics.record('summary', details)
        self.assertEqual(export_timeline(repo.store.database_path, event.event_key)['timeline'], [])


if __name__ == '__main__':
    unittest.main()
