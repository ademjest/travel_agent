import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

from evaluation import support
from evaluation.runner import main


class EvaluationRunnerTests(unittest.TestCase):
    def test_rail_scenario_defaults_to_fake_without_live_requests(self):
        with tempfile.TemporaryDirectory() as folder:
            report,native=Path(folder)/'report.json',Path(folder)/'rail.json'
            self.assertEqual(main(['--report',str(report),'rail_mcp','--output',str(native)]),0)
            value=json.loads(native.read_text(encoding='utf-8'))
            self.assertEqual(value['mode'],'offline')
            self.assertTrue(value['sample_gate_passed'])
            self.assertEqual(value['model_calls'],0)

    def test_real_rail_snapshot_filters_wrong_stations_and_preserves_midnight(self):
        with tempfile.TemporaryDirectory() as folder:
            report,native=Path(folder)/'report.json',Path(folder)/'rail.json'
            snapshot=support.ROOT/'tests/fixtures/rail_mcp_2026-10-27.json'
            self.assertEqual(main(['--report',str(report),'rail_mcp','--replay',str(snapshot),'--output',str(native)]),0)
            value=json.loads(native.read_text(encoding='utf-8'))
            self.assertEqual(value['mode'],'offline_replay')
            self.assertEqual(value['results'][3]['result']['status'],'empty')
            trains=value['results'][1]['result']['trains']
            self.assertEqual(len(trains),5)
            self.assertTrue(all(v['origin_station']=='北京南' and v['destination_station']=='上海虹桥' for v in trains))
            self.assertTrue(any(v['arrival_at'].startswith('2026-10-31') for v in value['results'][2]['result']['trains']))

    def test_support_import_does_not_load_environment_or_initialize_database(self):
        script = """from unittest.mock import patch
with patch('dotenv.load_dotenv', side_effect=AssertionError('Environment file read')), patch('infrastructure.memory_store.MemoryStore.__init__', side_effect=AssertionError('Database initialized')):
    import evaluation.support
"""
        result = subprocess.run([sys.executable, '-c', script], cwd=support.ROOT,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_list_does_not_execute_scenario(self):
        with patch('evaluation.runner.runpy.run_module') as run, redirect_stdout(io.StringIO()):
            self.assertEqual(main(['--list']), 0)
        run.assert_not_called()

    def test_official_navigation_scenario_is_offline_and_exercises_the_shared_workflow(self):
        with tempfile.TemporaryDirectory() as folder, redirect_stdout(io.StringIO()):
            report, native = Path(folder)/'report.json', Path(folder)/'navigation.json'
            self.assertEqual(main(['--report',str(report),'official_navigation','--output',str(native)]),0)
            result=json.loads(native.read_text(encoding='utf-8'))
            self.assertEqual(result['summary'],{'total':3,'passed':3})
            self.assertEqual(result['real_api_calls'],0)
            self.assertTrue(json.loads(report.read_text(encoding='utf-8'))['source_unchanged'])

    def test_offline_mode_rejects_live_escape_flag(self):
        with patch('evaluation.runner.runpy.run_module') as run, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                main(['tasks', '--live'])
        run.assert_not_called()

    def test_real_mode_requires_explicit_budget(self):
        with patch('evaluation.runner.runpy.run_module') as run, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                main(['--mode', 'real_model_fixed_tools', 'tasks'])
        run.assert_not_called()

    def test_offline_execution_blocks_an_accidental_provider_request(self):
        import requests
        with tempfile.TemporaryDirectory() as folder:
            report = Path(folder)/'report.json'
            with patch('evaluation.runner.runpy.run_module', side_effect=lambda *_args, **_kwargs: requests.get('https://example.invalid')):
                with self.assertRaisesRegex(AssertionError, 'Offline evaluation forbids'):
                    main(['--report', str(report), 'tasks'])
            self.assertEqual(json.loads(report.read_text(encoding='utf-8'))['execution_status'], 'failed')

    def test_failed_execution_does_not_change_native_task_metrics(self):
        with tempfile.TemporaryDirectory() as folder:
            report, native = Path(folder)/'report.json', Path(folder)/'native.json'
            native.write_text(json.dumps({'summary': {'total': 2, 'passed': 1}}), encoding='utf-8')
            with patch('evaluation.runner.runpy.run_module', side_effect=SystemExit(1)):
                self.assertEqual(main(['--report', str(report), 'tasks', '--output', str(native)]), 1)
            result = json.loads(report.read_text(encoding='utf-8'))
            self.assertEqual(result['execution_status'], 'failed')
            self.assertEqual(result['task_metrics'], {'total': 2, 'passed': 1})
            self.assertIsNone(result['actual_usage'])

    def test_reservation_covers_utf8_input_and_output_limit(self):
        with tempfile.TemporaryDirectory() as folder:
            budget = support.Budget(Path(folder)/'budget.json', max_tokens=50000)
            request = {'messages': [{'role': 'user', 'content': '测试中文额度'}], 'max_completion_tokens': 128}
            budget.reserve(request)
            payload = {'messages': request['messages'], 'tools': []}
            self.assertGreaterEqual(budget.pending['reservation'], len(json.dumps(payload, ensure_ascii=False).encode())+128)

    def test_invalid_output_limit_cannot_reduce_the_reserved_budget(self):
        with tempfile.TemporaryDirectory() as folder:
            budget = support.Budget(Path(folder)/'budget.json', max_tokens=50000)
            with self.assertRaises(ValueError):
                budget.reserve({'messages': [], 'max_completion_tokens': -5000})
            self.assertEqual(budget.physical_calls, 0)
            self.assertEqual(budget.tokens, 0)

    def test_bounded_client_sends_the_default_output_cap_to_the_sdk(self):
        sdk = unittest.mock.Mock()
        with tempfile.TemporaryDirectory() as folder:
            budget = support.Budget(Path(folder)/'budget.json', max_tokens=50000)
            support.BoundedClient(sdk, budget).create(model='fake', messages=[])
            self.assertEqual(sdk.chat.completions.create.call_args.kwargs['max_completion_tokens'], 8192)

    def test_usage_index_uses_receipts_without_another_model_request(self):
        records = [{'usage_complete': True, 'prompt_tokens': 100, 'completion_tokens': 20,
                    'total_tokens': 120, 'accounted_tokens': 120}]
        usage = support.reported_usage(records)
        self.assertEqual(usage['total_tokens'], 120)
        self.assertEqual(usage['physical_calls'], 1)
        self.assertEqual(usage['unknown_usage_calls'], 0)

    def test_unknown_usage_is_not_reported_as_zero_actual_tokens(self):
        usage = support.reported_usage([{'usage_complete': False, 'accounted_tokens': 3000}])
        self.assertIsNone(usage['total_tokens'])
        self.assertEqual(usage['unknown_usage_calls'], 1)
        self.assertEqual(usage['accounted_tokens'], 3000)
