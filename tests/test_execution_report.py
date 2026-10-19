import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from adapters.web_app import create_web_app
from core.settings import Settings
from core.web_settings import WebSettings
from infrastructure.execution_report import web_report


class ExecutionReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = create_web_app(WebSettings(data_dir=Path(self.temp.name), open_browser=False),
                                  Settings('', '', frozenset(), '', '', '', ''), start_workers=False)
        self.client = TestClient(self.app, base_url='http://127.0.0.1:8080')
        self.client.__enter__()
        self.addCleanup(lambda: self.client.__exit__(None, None, None))
        self.headers = {'x-csrf-token': self.client.get('/api/bootstrap').json()['csrf_token']}
        self.identity = self.app.state.repository.create_conversation()['id']
        result = self.client.post(f'/api/conversations/{self.identity}/messages', headers=self.headers,
            json={'content': 'private question', 'client_request_id': 'diagnostic-test', 'upload_ids': []})
        self.job = self.app.state.repository.job(result.json()['job_id'])
        self.path = f"/api/conversations/{self.identity}/jobs/{self.job['id']}/diagnostics"

    def record(self, stage, **details):
        self.app.state.repository.store.diagnostics.record(stage, {'event_key': self.job['event_key'], **details})

    def test_empty_records_do_not_claim_a_cause(self):
        report = self.client.get(self.path).json()
        self.assertEqual(report['timeline'], [])
        self.assertIn('无法确认', report['note'])

    def test_recovery_requires_the_same_identified_operation(self):
        self.record('tool',status='failed',operation_key='first',error_code='rate_limit')
        self.record('tool',status='completed',operation_key='second')
        store=self.app.state.repository.store
        report=web_report(store.database_path,{**self.job,'status':'completed'})
        self.assertFalse(report['timeline'][0]['recovered'])
        self.record('tool',status='completed',operation_key='first')
        report=web_report(store.database_path,{**self.job,'status':'completed'})
        self.assertTrue(report['timeline'][0]['recovered'])
        self.assertIn('已恢复',report['state'])

    def test_runtime_and_index_status_do_not_load_or_embed(self):
        store=self.app.state.repository.store
        from infrastructure.semantic_index import SemanticIndex
        calls=unittest.mock.Mock(side_effect=AssertionError('must not infer'))
        def embed(texts):
            return calls(texts)
        store.semantic_index=SemanticIndex(store,embed,'toy',2)
        first=self.client.get('/api/runtime-status').json()
        second=self.client.get('/api/runtime-status').json()
        self.assertEqual(first['backend_id'],second['backend_id'])
        result=self.client.get(f'/api/conversations/{self.identity}/retrieval-status')
        self.assertEqual(result.status_code,200)
        self.assertEqual(result.json()['index']['state'],'awaiting_check')
        calls.assert_not_called()

    def test_summary_threshold_is_normal_but_fallback_is_visible(self):
        self.record('summary', status='threshold_not_met')
        normal = self.client.get(self.path).json()['timeline'][0]
        self.assertFalse(normal['warning'])
        self.record('summary', status='validation_failed', previous_reused=True,
                    validation={'code': 'value_scope', 'path': 'items[0].text'})
        step = self.client.get(self.path).json()['timeline'][-1]
        self.assertTrue(step['warning'])
        self.assertIn('沿用有效旧版', step['description'])

    def test_failed_tool_is_reported_even_if_job_finished(self):
        self.record('tool', status='failed', error_code='rate_limit', attempts=3)
        job = {**self.job, 'status': 'completed'}
        report = web_report(self.app.state.repository.store.database_path, job)
        self.assertIn('降级', report['state'])
        self.assertIn('限流', report['report_text'])
        self.assertIn('3', report['timeline'][0]['description'])

    def test_scope_and_session_are_checked(self):
        other = self.app.state.repository.create_conversation()['id']
        self.assertEqual(self.client.get(self.path.replace(self.identity, other)).status_code, 404)
        with TestClient(self.app, base_url='http://127.0.0.1:8080') as anonymous:
            self.assertEqual(anonymous.get(self.path).status_code, 401)

    def test_report_is_metadata_only_and_never_executes_worker(self):
        self.record('model', status='completed', purpose='tool_reasoning', api_key='private-key', raw_text='private question')
        store = self.app.state.repository.store
        before = store.database_path.read_bytes()
        with patch.object(self.app.state.worker, 'run_once', side_effect=AssertionError('must not replay')):
            response = self.client.get(self.path)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(before, store.database_path.read_bytes())
        self.assertNotIn('private question', response.text)
        self.assertNotIn('private-key', response.text)
        self.assertFalse(response.json()['replayed'])

    def test_deleted_conversation_cannot_export(self):
        self.app.state.deletion.request(self.identity)
        self.assertEqual(self.client.get(self.path).status_code, 410)

    def test_real_message_job_is_automatically_correlated(self):
        # Use deterministic ping and the actual worker/outbox, without an LLM.
        sent = self.client.post(f'/api/conversations/{self.identity}/messages', headers=self.headers,
            json={'content': 'ping', 'client_request_id': 'ping', 'upload_ids': []}).json()
        self.app.state.repository.store.inbox.cancel(self.job['event_key'], 'web', self.identity, 'local-owner')
        asyncio.run(self.app.state.worker.run_once())
        path = f"/api/conversations/{self.identity}/jobs/{sent['job_id']}/diagnostics"
        report = self.client.get(path).json()
        self.assertEqual(report['job_status'], 'completed')
        self.assertTrue(any(step['description'] == '回复已发送。' for step in report['timeline']))
