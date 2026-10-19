"""Web/queue/injection contracts with scripted model outputs, not model-quality scores."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from adapters.web_app import create_web_app
from agents.context_builder import render_untrusted_context
from core.chat_transport import ChatEvent
from core.model_budget import MODEL_CALL
from core.settings import Settings
from core.summary_contract import load_summary_prompt
from core.web_settings import WebSettings
from infrastructure.env_settings_store import EnvSettingsStore
from infrastructure.web_repository import OWNER
from scripts.evaluate_summary_quality import OFFLINE_ENV, load_cases
from scripts.export_execution_timeline import export_timeline
from scripts.inspect_conversation_summary import inspect


class WebSummaryClient:
    def __init__(self, stages):
        self.stages = stages
        self.requests = []
        self.calls = self.requests
        self.source_ids = {}
        self.summary_calls = 0
        self.reply = ('A neutral discussion of descriptive writing and visual composition. ' * 20)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        purpose = MODEL_CALL.get().purpose
        self.requests.append({'purpose': purpose, **kwargs})
        if purpose == 'intent_parse':
            value = {'schema_version': 1, 'action': 'none', 'operations': [], 'missing_fields': [],
                     'requires_confirmation': False, 'source_spans': [], 'confidence': 1}
            content = json.dumps(value)
        elif purpose == 'conversation_summary':
            payload = json.loads(kwargs['messages'][-1]['content'])
            for turn in payload['new_turns']:
                for stage in self.stages:
                    for source in stage['turns']:
                        if source['user'] == turn['user']:
                            self.source_ids[source['key']] = turn['message_id']
            stage = self.stages[min(self.summary_calls, len(self.stages) - 1)]
            items = [{'kind': item['kind'], 'text': item['text'], 'sources': [{'message_id': self.source_ids[ref['turn']], 'quote': ref['quote']}
                                        for ref in item['sources']]} for item in stage['items']]
            content = json.dumps({'items': items}, ensure_ascii=False)
            self.summary_calls += 1
        else:
            content = self.reply
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop',
            message=SimpleNamespace(content=content, tool_calls=[]))],
            usage=SimpleNamespace(prompt_tokens=20, completion_tokens=20))

    def close(self):
        pass


class SummaryWebFlowTests(unittest.TestCase):
    def test_live_resume_preserves_failed_attempt_and_budget(self):
        from scripts.smoke_summary_live import Budget
        from scripts.smoke_summary_web import main
        env = {**OFFLINE_ENV, 'LLM_API_KEY': 'offline',
               'LLM_BASE_URL': 'https://offline.invalid/v1', 'LLM_MODEL_ID': 'offline-model'}
        with tempfile.TemporaryDirectory() as folder, patch.dict('os.environ', env), \
                patch('dotenv.load_dotenv', return_value=False), \
                patch('scripts.smoke_summary_web.web_flow') as flow:
            output = Path(folder) / 'audit.json'
            args = SimpleNamespace(output=output, max_calls=12, max_tokens=100000, resume_web=False)
            # Avoid Q04 generation while constructing the initial audit configuration.
            with patch('scripts.smoke_summary_web.ConversationSummaryService') as service:
                service.return_value.ensure.return_value = None
                main(args)
            original = json.loads(output.read_text(encoding='utf-8'))
            original['web_flow'] = {'initial': {'record': None}, 'mechanism_passed': False}
            output.write_text(json.dumps(original), encoding='utf-8')
            args.resume_web = True
            main(args)
            continued = Budget.resume(output).report
            self.assertEqual(continued['web_flow'], original['web_flow'])
            self.assertEqual(continued['budget'], original['budget'])
            self.assertIn('web_retry', continued)
            count = flow.call_count
            with self.assertRaises(ValueError):
                main(args)
            self.assertEqual(flow.call_count, count)

    def test_live_acceptance_driver_uses_isolated_seeded_history_and_real_web_path(self):
        from scripts.smoke_summary_web import web_flow
        corpus = load_cases(Path(__file__).resolve().parents[1] / 'evals/summary_quality_cases_v2.json')
        stages = next(case['stages'] for case in corpus['cases'] if case['id'] == 'Q02')
        sdk = WebSummaryClient(stages)
        report = {}
        for direct in (False, True):
            sdk = WebSummaryClient(stages)
            report = {}
            with self.subTest(initial_summary_direct=direct), tempfile.TemporaryDirectory() as folder, patch.dict('os.environ', OFFLINE_ENV):
                web_flow(sdk, Settings('', '', frozenset(), '', 'offline', 'https://offline.invalid', 'offline-model'),
                         corpus, Path(folder), report, lambda: None, initial_summary_direct=direct)
            self.assertTrue(report['mechanism_passed'])
            self.assertEqual(report['leakage_outside_summary'], [])
            self.assertEqual(sdk.summary_calls, 2)

    def test_incremental_summary_reaches_web_answer_after_both_histories_age_out(self):
        corpus = load_cases(Path(__file__).resolve().parents[1] / 'evals/summary_quality_cases_v2.json')
        stages = next(case['stages'] for case in corpus['cases'] if case['id'] == 'Q02')
        sdk = WebSummaryClient(stages)
        with tempfile.TemporaryDirectory() as folder, patch.dict('os.environ', OFFLINE_ENV), \
                patch('app.runtime_factory.OpenAI', return_value=sdk):
            root = Path(folder)
            app = create_web_app(WebSettings(data_dir=root / 'web', open_browser=False),
                Settings('', '', frozenset(), '', 'offline', 'https://offline.invalid', 'offline-model'),
                start_workers=False, settings_store=EnvSettingsStore(root))
            with TestClient(app, base_url='http://127.0.0.1:8080') as client:
                headers = {'x-csrf-token': client.get('/api/bootstrap').json()['csrf_token']}
                identity = client.post('/api/conversations', headers=headers).json()['id']
                worker, store = app.state.worker, app.state.repository.store

                def send(text, key):
                    sent = client.post(f'/api/conversations/{identity}/messages', headers=headers,
                        json={'content': text, 'client_request_id': key, 'upload_ids': []})
                    self.assertEqual(sent.status_code, 202, sent.text)
                    self.assertTrue(asyncio.run(worker.run_once()))
                    return sent.json()

                for index, turn in enumerate(stages[0]['turns']):
                    send(turn['user'], f'initial-{index}')
                self.assertEqual(sdk.summary_calls, 0)
                event = ChatEvent('web', 'group', 'inspection', identity, OWNER, '')
                send(stages[1]['turns'][0]['user'], 'correction')
                first = store.conversation_summaries.latest(event)
                self.assertIsNotNone(first)
                self.assertIn('360', first['summary'])
                self.assertNotIn('320', first['summary'])
                for index, turn in enumerate(stages[1]['turns'][1:]):
                    send(turn['user'], f'correction-fill-{index}')
                send(corpus['aging_messages'][0], 'aging-0')
                second = store.conversation_summaries.latest(event)
                self.assertGreater(second['version'], first['version'])
                self.assertGreater(second['covered_turn_id'], first['covered_turn_id'])
                self.assertIn('320', second['summary'])
                self.assertIn('19:00', second['summary'])
                self.assertIn('不借用', second['summary'])
                sdk.reply = '中性讨论，不增加个人事实。'
                for index, text in enumerate(corpus['aging_messages'][1:], 1):
                    send(text, f'aging-{index}')
                final = send(corpus['recall'], 'recall')
                request = next(request for request in reversed(sdk.requests) if request['purpose'] == 'tool_reasoning')
                context = app.state.runtime.application.context_builder.build(event)
                self.assertIn(context.conversation_summary, '\n'.join(message['content'] for message in request['messages']))
                outside = render_untrusted_context(replace(context, conversation_summary=''))
                for value in ('姨妈', '15分钟', '320', '19:00', '慢慢看桥'):
                    self.assertNotIn(value, outside)
                info = inspect(store.database_path, event.storage_scope_id, OWNER)
                self.assertTrue(info['valid_record'])
                self.assertTrue(info['prompt_matches_current'])
                with store._connect() as db:
                    job = db.execute('SELECT event_key FROM inbox_jobs WHERE id=?', (final['job_id'],)).fetchone()
                trace = export_timeline(store.database_path, job['event_key'])
                contexts = [row['details'] for row in trace['timeline'] if row['stage'] == 'context']
                self.assertTrue(contexts[-1]['summary_injected'])
                self.assertEqual(contexts[-1]['summary_prompt_hash'], load_summary_prompt()[1])
                self.assertEqual(contexts[-1]['summary_version'], second['version'])
                self.assertEqual(client.get(f'/api/conversations/{identity}/messages').status_code, 200)
                count = len(sdk.requests)
                self.assertEqual(client.delete(f'/api/conversations/{identity}', headers=headers).status_code, 202)
                app.state.deletion.cleanup(identity)
                with store._connect() as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM conversation_summaries').fetchone()[0], 0)
                self.assertEqual(len(sdk.requests), count)


if __name__ == '__main__':
    unittest.main()
