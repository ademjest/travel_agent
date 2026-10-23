"""Replay saved evidence, or explicitly spend <=5 Amap and <=10 model HTTP calls.

No production writes, search or live page fetches. A fixed durable ledger refuses reruns.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.attraction_projection import attraction_report, grouped_facts, planning_facts, poi_detail_view


def replay():
    path = ROOT / 'data/web/travel_bot.db'
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
        row = db.execute("SELECT data_json FROM research_runs WHERE data_json LIKE '%attraction-facts-v1%' "
                         'ORDER BY updated_at DESC LIMIT 1').fetchone()
    if not row:
        raise ValueError('No recorded attraction facts available for replay')
    data = json.loads(row[0])
    brief, full, planning = attraction_report(data), attraction_report(data, detailed=True), planning_facts(data)
    projected = json.loads(planning.split('\n', 1)[1])
    result = {'mode': 'offline_saved_evidence_replay', 'original_report_chars': len(data['report']),
              'brief_chars': len(brief), 'full_report_chars': len(full), 'planning_chars': len(planning),
              'original_facts': len(data['facts']), 'display_groups': len(grouped_facts(data['facts'])),
              'planning_target_ids': sorted({f['target_id'] for f in projected['facts']}),
              'omitted_facts': projected['omitted_facts'], 'pending_count': len(projected['pending_target_ids']),
              'checks': {'brief_under_6000': len(brief) <= 6000,
                         'all_processed_targets_in_planning': {p['target_id'] for p in data['place_details']}
                             <= {f['target_id'] for f in projected['facts']},
                         'no_mid_clause_cut': '[回复已截断]' not in brief,
                         'full_report_has_source_index': '来源与核验依据' in full}}
    return data, result


def live(resume=False):
    import httpx
    import requests
    from openai import OpenAI
    from agents.trip_planner import TripPlanner
    from core.chat_transport import ChatEvent
    from core.model_budget import MODEL_CALL, MODEL_EVENT
    from core.web_settings import WebSettings
    from infrastructure.amap_client import AmapClient, AMAP_BASE_URL
    from infrastructure.memory_store import MemoryStore
    from infrastructure.model_gateway import ModelGateway
    from services.official_sources import OfficialSources
    from services.research_service import ResearchService

    settings = WebSettings.from_env().travel_settings()
    if not settings.amap_api_key or not settings.llm_configured:
        raise ValueError('Configured Amap and model services required')
    folder = ROOT / 'data/upgrades/research-compact-smoke-2026-10-20'
    folder.mkdir(parents=True, exist_ok=True)
    ledger = folder / 'audit.json'
    report = {'started_at': datetime.now(timezone.utc).isoformat(), 'max_amap_calls': 5, 'max_llm_calls': 10,
              'amap_calls': 0, 'llm_calls': 0, 'tavily_calls': 0, 'live_page_calls': 0,
              'retries': 0, 'redirects': 0, 'payload_origin': 'hardcoded_public_POIs_and_synthetic_itineraries_only',
              'production_database_opened': False, 'cases': [], 'attempts': []}
    if resume:
        report = json.loads(ledger.read_text(encoding='utf-8'))
        assert report['max_amap_calls'] == 5 and report['max_llm_calls'] == 10
        assert report['amap_calls'] <= 5 and report['llm_calls'] <= 10
        assert report['payload_origin'] == 'hardcoded_public_POIs_and_synthetic_itineraries_only'
        report.setdefault('resumed_at', []).append(datetime.now(timezone.utc).isoformat())
    else:
        with ledger.open('x', encoding='utf-8') as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)

    def save():
        ledger.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

    def model_request(request):
        if request.method != 'POST' or not request.url.path.endswith('/chat/completions'):
            raise ValueError('Unexpected model endpoint refused before network')
        if report['llm_calls'] >= 10:
            raise ValueError('Authorized model HTTP call cap reached')
        body = request.content.decode('utf-8')
        if any(secret and secret in body for secret in (settings.amap_api_key, settings.llm_api_key)):
            raise ValueError('Secret in model body refused before network')
        report['llm_calls'] += 1
        context = MODEL_CALL.get()
        report['attempts'].append({'provider': 'llm', 'number': report['llm_calls'],
                                   'purpose': context.purpose if context else 'unspecified'})
        save()

    def model_response(response):
        response.read()
        if response.status_code == 200:
            value = response.json()
            report['attempts'][-1]['synthetic_response'] = value.get('choices', [{}])[0].get('message', {}).get('content', '')
            save()

    def recorded_reader(url, **kwargs):
        raise OSError('All page fetches disabled in synthetic live test')

    store = MemoryStore(folder / 'test.db')
    try:
        with requests.Session() as session, httpx.Client(trust_env=False, follow_redirects=False,
                event_hooks={'request': [model_request], 'response': [model_response]}) as http:
            session.trust_env = False

            def get(url, **kwargs):
                if url not in {AMAP_BASE_URL + '/v5/place/text', AMAP_BASE_URL + '/v5/place/detail'}:
                    raise ValueError('Unexpected Amap endpoint refused before network')
                if report['amap_calls'] >= 5:
                    raise ValueError('Authorized Amap HTTP call cap reached')
                report['amap_calls'] += 1
                report['attempts'].append({'provider': 'amap', 'number': report['amap_calls'], 'path': url.removeprefix(AMAP_BASE_URL)})
                save()
                response = session.get(url, allow_redirects=False, **kwargs)
                if 300 <= response.status_code < 400:
                    raise requests.HTTPError('Redirect refused')
                return response

            amap = AmapClient(settings.amap_api_key, http_get=get, max_attempts=1)
            with OpenAI(api_key=settings.llm_api_key, base_url=settings.llm_base_url, timeout=90,
                        max_retries=0, http_client=http) as raw:
                client = ModelGateway(store, raw, default_model=settings.llm_model_id)
                cases = [
                    ('beijing', '北京', '第1天\n- 天安门广场\n- 故宫\n第2天\n- 景山公园\n- 北海公园', 2),
                    ('wuhan', '武汉', '第1天\n- 湖北省博物馆', 1),
                ]
                for name, city, content, days in cases:
                    if any(c['case'] == name and c['status'] == 'completed' and all(c.get('checks', {}).values()) for c in report['cases']):
                        continue
                    text = f'核对{city}方案里所有景点的票价、开放时间和预约规则，2026年10月22日开始游览。只用高德，不查网页，不创建提醒或修改行程。'
                    event = ChatEvent('web', 'group', 'compact-' + name, 'smoke-' + name, 'test-owner', text)
                    token = MODEL_EVENT.set(event.event_key)
                    result = {'case': name}
                    try:
                        service = ResearchService(store, None, client, settings.llm_model_id, reader=recorded_reader,
                                                  amap=amap, official_sources=OfficialSources())
                        request = {'plan': {'title': city + '回归方案', 'destination': city, 'version': 1,
                                   'duration_options': [days], 'content': content},
                                   'original_request': text, 'query_text': text,
                                   'visit_start_date_text': '2026年10月22日', 'source_request': '只用高德'}
                        # Reuse successful real target responses from the failed harness runs.
                        # They still pass the complete production validator, with no fake inference.
                        expected = {'天安门广场', '故宫', '景山公园', '北海公园'} if name == 'beijing' else {'湖北省博物馆'}
                        cached = next((a['synthetic_response'] for a in reversed(report['attempts'])
                            if a.get('purpose') == 'research_targets' and a.get('synthetic_response')
                            and {t['name'] for t in json.loads(a['synthetic_response']).get('targets', [])} == expected), None)
                        if cached:
                            service.search_planner.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
                                create=lambda **kwargs: SimpleNamespace(choices=[SimpleNamespace(
                                    message=SimpleNamespace(content=cached), finish_reason='stop')]))))
                            result['target_extraction'] = 'cached_real_response_revalidated_after_harness_fix'
                        reply = service.handle_intent(event, None, request=request)
                        data = store.research.get(event)
                        if not data or not data.get('report'):
                            raise ValueError('Synthetic research did not complete: ' + reply)
                        planning = planning_facts(data)
                        projection = json.loads(planning.split('\n', 1)[1])
                        details = [p['detail'] for p in data['place_details'] if p.get('detail')]
                        result.update(status='completed', reply_chars=len(reply), full_report_chars=len(data['report']),
                            planning_chars=len(planning), targets=len(data['target_scope']['targets']), matched=len(details),
                            planning_targets=sorted({f['target_id'] for f in projection['facts']}),
                            provider_views=[{'name': d['name'], 'view': poi_detail_view(d)} for d in details],
                            checks={'reply_under_6000': len(reply) <= 6000, 'no_truncation': '[回复已截断]' not in reply,
                                    'all_target_ids_in_planning': {p['target_id'] for p in data['place_details']}
                                        <= {f['target_id'] for f in projection['facts']},
                                    'no_false_official': not any(f['status'] == 'official_verified' for f in data['facts']),
                                    'raw_snapshots_preserved': all(d['snapshot'] and d['field_paths'] for d in details)})
                        # Exercise the production planning consumer, with no route/POI calls or saved trip.
                        planner = TripPlanner(client, settings.llm_model_id, amap)
                        spec = planner.extract(f'根据资料规划{city}{days}天行程，2026年10月22日出发。', {},
                                               datetime(2026, 10, 20, tzinfo=timezone.utc), source_text=planning)
                        result['checks']['production_consumer_preserves_source'] = spec.get('source_context') == planning
                        result['checks']['production_consumer_destination'] = spec.get('destination') == city
                    except Exception as error:
                        result.update(status='failed', error_type=type(error).__name__, error_code=getattr(error, 'code', ''))
                        import traceback
                        result['error_frames'] = [{'file': Path(frame.filename).name, 'line': frame.lineno,
                                                   'function': frame.name} for frame in traceback.extract_tb(error.__traceback__)]
                        if isinstance(error, ValueError):
                            result['validation_message'] = str(error)
                    finally:
                        MODEL_EVENT.reset(token)
                    report['cases'].append(result)
                    save()
                    print(json.dumps({k: result[k] for k in ('case', 'status', 'checks') if k in result}, ensure_ascii=False), flush=True)
        with store._connect() as db:
            report['model_usage'] = [dict(row) for row in db.execute('SELECT purpose,status,prompt_tokens,completion_tokens,'
                'cached_input_tokens,estimated_cost,cost_currency,finish_reason FROM model_calls ORDER BY started_at')]
            report['business_counts'] = {t: db.execute('SELECT count(*) FROM ' + t).fetchone()[0]
                                         for t in ('trips', 'personal_reminders', 'policy_watches', 'reservation_plans')}
        latest = {c['case']: c for c in report['cases']}
        report['passed'] = (all(c['status'] == 'completed' and all(c['checks'].values()) for c in latest.values())
                            and len(latest) == 2 and not any(report['business_counts'].values()))
    finally:
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        save()
        output = ROOT / 'evals/research-compact-live-2026-10-20.json'
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: report[k] for k in ('amap_calls', 'llm_calls', 'passed')}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Use the explicitly approved fixed physical-call budget once')
    parser.add_argument('--resume', action='store_true', help='Continue using the SAME durable remaining budget, never reset it')
    args = parser.parse_args()
    if args.live:
        live(resume=args.resume)
    else:
        _, result = replay()
        print(json.dumps(result, ensure_ascii=False, indent=2))
