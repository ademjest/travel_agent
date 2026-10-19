"""Bounded live summary/Web acceptance using synthetic historical fixtures and an isolated database."""
import argparse
import asyncio
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient
from adapters.web_app import create_web_app
from core.chat_transport import ChatEvent
from core.conversation_summary import render_summary
from core.model_budget import MODEL_EVENT
from core.model_policy import current_policy
from core.settings import Settings
from core.summary_contract import load_summary_prompt
from core.summary_semantics import SEMANTICS_VERSION
from core.web_settings import WebSettings
from infrastructure.env_settings_store import EnvSettingsStore
from infrastructure.memory_store import MemoryStore
from infrastructure.model_gateway import ModelGateway
from infrastructure.web_repository import OWNER
from scripts.evaluate_summary_quality import load_cases, seed_stage
from scripts.export_execution_timeline import export_timeline
from scripts.smoke_summary_live import Budget, BoundedClient, CountingTransport
from services.conversation_summary_service import ConversationSummaryService


def web_flow(sdk, settings, corpus, root, report, save, *, initial_summary_direct=False,
             captured_initial=None, correction_summary_direct=False, captured_correction=None):
    stages = next(case['stages'] for case in corpus['cases'] if case['id'] == 'Q02')
    with patch('app.runtime_factory.OpenAI', return_value=sdk):
        app = create_web_app(WebSettings(data_dir=root / 'web', open_browser=False), settings,
            start_workers=False, settings_store=EnvSettingsStore(root))
    with TestClient(app, base_url='http://127.0.0.1:8080') as client:
        headers = {'x-csrf-token': client.get('/api/bootstrap').json()['csrf_token']}
        identity = client.post('/api/conversations', headers=headers).json()['id']
        store = app.state.repository.store
        event = ChatEvent('web', 'group', 'fixture', identity, OWNER, corpus['recall'])
        mapping = {}

        def send(text, key):
            response = client.post(f'/api/conversations/{identity}/messages', headers=headers,
                json={'content': text, 'client_request_id': key, 'upload_ids': []})
            if response.status_code != 202:
                raise ValueError('Web message was not accepted')
            asyncio.run(app.state.worker.run_once())
            with store._connect() as db:
                job = dict(db.execute('SELECT * FROM inbox_jobs WHERE id=?', (response.json()['job_id'],)).fetchone())
            if job['status'] != 'completed':
                raise ValueError('Web job did not complete')
            return job['event_key']

        for label, stage in zip(('initial', 'correction'), stages):
            seed_stage(store, event, stage, corpus, mapping)
            if ((label == 'initial' and initial_summary_direct)
                    or (label == 'correction' and correction_summary_direct)):
                diagnostics = []
                summary_sdk = sdk
                captured = captured_initial if label == 'initial' else captured_correction
                if captured is not None:
                    response = SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop',
                        message=SimpleNamespace(content=captured['content']))])
                    summary_sdk = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs: response)))
                    report[label + '_restored_from_physical_attempt'] = captured['physical_attempt']
                record = ConversationSummaryService(store.conversation_summaries,
                    ModelGateway(store, summary_sdk), settings.llm_model_id, diagnostics.append).ensure(event)
                report[label] = {'record': record, 'diagnostics': diagnostics,
                                 'entry': 'direct-summary-service-not-Web'}
            else:
                key = send(corpus['recall'], label)
                record = store.conversation_summaries.latest(event)
                report[label] = {'record': record, 'trace': export_timeline(store.database_path, key)}
            save()
            if record is None:
                report['mechanism_passed'] = False
                return
        if report['correction']['record']['version'] <= report['initial']['record']['version']:
            raise ValueError('Correction did not publish a newer summary')
        # Real Web messages/queue/delivery, with no model call needed for ping.
        for index in range(16):
            send('ping', f'aging-{index}')
        request_start = len(sdk.calls)
        key = send(corpus['recall'], 'recall-after-aging')
        final_calls = sdk.calls[request_start:]
        answer_request = next(request for request in reversed(final_calls) if not request.get('response_format'))
        record = store.conversation_summaries.latest(event)
        rendered = render_summary(json.loads(record['items_json']), 1800)
        body = '\n'.join(message.get('content', '') for message in answer_request['messages']
                         if message['role'] != 'system')
        injected = rendered in body
        outside = body.replace(rendered, '', 1)
        leakage = [value for value in ('姨妈', '15分钟', '320', '19:00', '慢慢看桥') if value in outside]
        report.update({'final_record': record, 'summary_injected': injected,
            'leakage_outside_summary': leakage, 'final_trace': export_timeline(store.database_path, key),
            'final_answer': app.state.repository.messages(identity)['items'][-1]['content'],
            'mechanism_passed': injected and not leakage,
            'note': ('Historical turns and assistant fillers are seeded fixtures. '
                     + ('Initial and corrected summaries are restored unchanged from captured real responses and revalidated; only aged recall uses Web routing/models.'
                        if captured_correction is not None else
                        'Initial summary is restored unchanged from a captured real response; correction uses the real summary service; only aged recall uses Web routing/models.'
                        if captured_initial is not None else
                        'Initial summary uses the real summary service directly; correction and aged recall use Web routing/models.'
                        if initial_summary_direct else 'Three current acceptance messages use Web routing/models.')
                     + ' Not every historical turn was generated by a real model.')})
        save()


def main(args):
    import httpx
    from dotenv import load_dotenv
    from openai import OpenAI
    load_dotenv(ROOT / '.env', override=False)
    load_dotenv(ROOT.parent / '.env', override=False)
    resume = getattr(args, 'resume_web', False)
    budget = Budget.resume(args.output) if resume else Budget(args.output, args.max_calls, args.max_tokens)
    key, base, model = (os.getenv(name, '').strip() for name in ('LLM_API_KEY', 'LLM_BASE_URL', 'LLM_MODEL_ID'))
    if not all((key, base, model)):
        raise ValueError('Model configuration is incomplete')
    tier = current_policy().tier
    corpus_path = ROOT / 'evals/summary_quality_cases_v2.json'
    corpus = load_cases(corpus_path)
    configuration = {'model': model, 'summary_model': os.getenv('LLM_SUMMARY_MODEL_ID', '') or model,
        'provider_host': urlparse(base).hostname, 'budget_tier': tier, 'validator_version': SEMANTICS_VERSION,
        'summary_prompt_hash': load_summary_prompt()[1], 'corpus_hash': hashlib.sha256(corpus_path.read_bytes()).hexdigest()}
    if resume:
        restore = getattr(args, 'restore_initial_attempt', None)
        restore_correction = getattr(args, 'restore_correction_attempt', None)
        attempt_key = ('web_revalidated_correction_recall' if restore_correction is not None else
                       'web_restored_initial_retry' if restore is not None else
                       'web_service_initial_retry' if getattr(args, 'initial_summary_direct', False) else 'web_retry')
        validator_upgrade = (restore_correction is not None
            and budget.report.get('validator_version') == 'summary-semantics-v4.2'
            and SEMANTICS_VERSION == 'summary-semantics-v4.3')
        if ((budget.max_calls, budget.max_tokens) != (args.max_calls, args.max_tokens)
                or any(budget.report.get(name) != value for name, value in configuration.items()
                       if not (validator_upgrade and name == 'validator_version'))
                or attempt_key in budget.report):
            raise ValueError('Resume must preserve configuration/budget and cannot overwrite a prior retry')
        captured = None
        if restore is not None:
            captured = next(call for call in budget.records if call['physical_attempt'] == restore)
            expected = budget.report['web_service_initial_retry']['initial']['record']
            if (captured['purpose'] != 'conversation_summary' or not expected
                    or json.loads(captured['request']['messages'][-1]['content'])['previous_items']
                    or json.loads(captured['content'])['items'] != [
                        {**item, 'sources': [{k: v for k, v in source.items() if k != 'source_hash'}
                                            for source in item['sources']]}
                        for item in json.loads(expected['items_json'])]):
                raise ValueError('Restoration must use the exact previously published initial response')
        corrected = None
        if restore_correction is not None:
            corrected = next(call for call in budget.records if call['physical_attempt'] == restore_correction)
            if (captured is None or corrected['purpose'] != 'conversation_summary'
                    or json.loads(corrected['request']['messages'][-1]['content'])['previous_items']
                       != json.loads(captured['content'])['items']):
                raise ValueError('Correction must use the exact captured predecessor')
        report = budget.report[attempt_key] = {'validator_version': SEMANTICS_VERSION}
    else:
        budget.report.update({**configuration,
        'sdk_retries': 0, 'transport_retries': 0, 'production_database_accessed': False, 'web_flow': {}})
        report = budget.report['web_flow']
    budget.save()
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {'AI_SUMMARY_ENABLED': 'true', 'AI_VECTOR_ENABLED': 'false',
                                                   'AI_BUDGET_TIER': tier, 'SEARCH_API_KEY': ''}))
        stack.enter_context(patch('requests.sessions.Session.request', side_effect=AssertionError('External tools forbidden')))
        stack.enter_context(patch('infrastructure.public_http.request_public', side_effect=AssertionError('External tools forbidden')))
        stack.enter_context(patch('infrastructure.search_client.request_public', side_effect=AssertionError('External tools forbidden')))
        stack.enter_context(patch('infrastructure.web_reader.request_public', side_effect=AssertionError('External tools forbidden')))
        http = stack.enter_context(httpx.Client(transport=CountingTransport(budget), follow_redirects=False, timeout=90))
        sdk = stack.enter_context(OpenAI(api_key=key, base_url=base, max_retries=0, http_client=http, timeout=90))
        bounded = BoundedClient(sdk, budget)
        folder = stack.enter_context(tempfile.TemporaryDirectory(prefix='summary-web-acceptance-'))
        root = Path(folder)
        try:
            if resume:
                web_flow(bounded, Settings('', '', frozenset(), '', key, base, model), corpus,
                         root, report, budget.save,
                         initial_summary_direct=getattr(args, 'initial_summary_direct', False) or captured is not None,
                         captured_initial=captured, correction_summary_direct=captured is not None,
                         captured_correction=corrected)
                return
            q04 = next(case for case in corpus['cases'] if case['id'] == 'Q04')
            store = MemoryStore(root / 'q04.db')
            event = ChatEvent('web', 'group', 'q04', 'q04', 'test-owner', corpus['recall'])
            seed_stage(store, event, q04['stages'][0], corpus, {})
            diagnostics = []
            token = MODEL_EVENT.set('summary-web-acceptance:Q04')
            try:
                record = ConversationSummaryService(store.conversation_summaries,
                    ModelGateway(store, bounded), model, diagnostics.append).ensure(event)
            finally:
                MODEL_EVENT.reset(token)
            budget.report['q04'] = {'record': record, 'diagnostics': diagnostics}
            budget.save()
            if not budget.stop_reason:
                web_flow(bounded, Settings('', '', frozenset(), '', key, base, model), corpus,
                         root, budget.report['web_flow'], budget.save)
        except Exception as error:
            if resume:
                report['failure_type'] = type(error).__name__
            else:
                budget.report['failure_type'] = type(error).__name__
        finally:
            budget.save()
    print(json.dumps({'budget': budget.report['budget'], 'q04_published': bool(budget.report.get('q04', {}).get('record')),
                      'web_mechanism_passed': budget.report['web_flow'].get('mechanism_passed'),
                      'failure_type': budget.report.get('failure_type')}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--max-calls', type=int, default=12)
    parser.add_argument('--max-tokens', type=int, default=100000)
    parser.add_argument('--resume-web', action='store_true', help='Retry Web once within the same remaining audited budget')
    parser.add_argument('--initial-summary-direct', action='store_true', help='On resume, generate initial summary through its real service; Web handles correction and recall')
    parser.add_argument('--restore-initial-attempt', type=int, help='On resume, restore a published captured initial response; generate correction directly and test aged Web recall')
    parser.add_argument('--restore-correction-attempt', type=int, help='Revalidate the exact captured correction; only aged Web recall uses new requests')
    main(parser.parse_args())
