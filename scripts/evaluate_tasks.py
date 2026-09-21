"""OneBot task/state evaluation in temporary databases; never sends QQ messages.

Offline uses scripted model responses. --live uses the configured real model but
the same fixed POI/weather/policy snapshots. Results do not measure live APIs.
"""
import argparse
import asyncio
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from openai import OpenAI
from adapters.onebot_app import OneBotAdapter, OneBotReplyRenderer
from agents.reminder_intent import ReminderIntentParser
from agents.travel_agent import TravelAgent
from agents.trip_planner import indoor_place
from app.runtime_factory import build_runtime
from core.chat_transport import ChatEvent
from core.settings import Settings, OneBotSettings
from infrastructure.memory_store import MemoryStore
from infrastructure.model_gateway import ModelGateway, usage_summary
from services.booking_policy import BookingPolicyResolver


class ScriptedModel:
    def __init__(self):
        self.responses = []
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs):
        if not self.responses:
            raise AssertionError('Unexpected model invocation; no scripted response')
        value = self.responses.pop(0)
        calls = []
        if 'tool' in value:
            calls = [SimpleNamespace(id='eval-call', function=SimpleNamespace(name=value['tool'],
                arguments=json.dumps(value['arguments'], ensure_ascii=False)))]
        content = json.dumps(value['json'], ensure_ascii=False) if 'json' in value else value.get('text')
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=calls))])


class RecordingTransport:
    def __init__(self): self.messages = []
    async def send(self, message):
        self.messages.append(message)
        return f'eval-out-{len(self.messages)}'


class SnapshotPlaces:
    def search_places(self, city, keyword):
        return [dict(id=str(i), name='湖北省博物馆' if i == 0 else f'评测美术馆{i}',
            type='博物馆' if i == 0 else '美术馆', address=f'评测街{i}号', location=f'114.{300+i},30.55') for i in range(14)]


async def evaluate(case, live, reference):
    start = time.monotonic()
    calls, faults, replies = [], [], []
    with tempfile.TemporaryDirectory(prefix='travel-eval-') as directory:
        path = Path(directory) / 'eval.db'
        store = MemoryStore(path)
        transport = RecordingTransport()
        scripted = ScriptedModel()
        settings = Settings('', '', frozenset({'eval'}), '', os.getenv('LLM_API_KEY', '') if live else 'script',
            os.getenv('LLM_BASE_URL', '') if live else 'https://example.test', os.getenv('LLM_MODEL_ID', '') if live else 'script-v1')
        base_client = OpenAI(api_key=settings.llm_api_key, base_url=settings.llm_base_url, max_retries=1) if live else scripted
        client = ModelGateway(store, base_client)

        def read_tool(name, arguments):
            calls.append(dict(name=name, arguments=arguments))
            return f"【{'天气预报' if name == 'get_weather_forecast' else '当前天气'}】{arguments.get('location', '')}\n天气：晴，温度：25℃。发布时间：2030-09-09 10:00。评测固定快照。"

        def runtime_for(store):
            runtime = build_runtime(Settings('', '', frozenset({'eval'}), '', '', '', ''), platform='onebot',
                store=store, transport=transport, reply_renderer=OneBotReplyRenderer(), group_allowed=lambda group: group == 'eval')
            app = runtime.application
            app.travel_agent = TravelAgent(settings, read_tool, client=client)
            app.personal_reminder_service.intent_parser = ReminderIntentParser(client, settings.llm_model_id)
            app.personal_reminder_service.clock = lambda: reference
            resolver = BookingPolicyResolver(sources={'湖北省博物馆': 'https://fixture.example/policy'},
                fetch_text=lambda url: case.get('policy', '湖北省博物馆\n个人入馆预约可提前5天。每日0点开始放票。'), clock=lambda: reference)
            app.booking_reminder_service.resolver = resolver
            app.booking_reminder_service.clock = lambda: reference
            app.booking_reminder_service.client = client
            app.booking_reminder_service.model = settings.llm_model_id
            app.trip_service.policy_resolver = resolver
            app.trip_service.clock = lambda: reference
            planner = app.trip_service.planner
            planner.client, planner.model, planner.amap = client, settings.llm_model_id, SnapshotPlaces()
            return runtime, OneBotAdapter(OneBotSettings('http://localhost', 'unused', 'unused', frozenset({'eval'})), app, store)

        runtime, adapter = runtime_for(store)
        try:
            for index, step in enumerate(case['steps']):
                if step.get('control') == 'restart':
                    store = MemoryStore(path)
                    runtime, adapter = runtime_for(store)
                    continue
                if not live:
                    scripted.responses.extend(step.get('responses', []))
                segments = [{'type': 'at', 'data': {'qq': 'bot'}}] if step.get('at') else []
                segments.append({'type': 'text', 'data': {'text': step['text']}})
                before = len(transport.messages)
                result = await adapter.handle(dict(post_type='message', message_type='group', group_id='eval',
                    user_id=step.get('user', 'owner'), self_id='bot', time=reference.timestamp(),
                    message_id=step.get('message_id', f'm{index}'), message=segments))
                replies.extend(str(message.payload.get('message', '')) for message in transport.messages[before:])
                if step.get('status') and result['status'] != step['status']:
                    faults.append(f"step {index}: expected {step['status']}, got {result['status']}")
            expected = case['expect']
            all_reminders = store.reminders.list_for_owner('onebot', 'eval', 'owner')
            reminders = [row for row in all_reminders if row['status'] == 'active']
            if case['category'] == 'reminder' and not expected['reminders']:
                if len(all_reminders) != 1 or all_reminders[0]['status'] != 'cancelled':
                    faults.append('cancellation did not preserve cancelled record')
            if len(reminders) != expected['reminders']:
                faults.append(f"reminders: expected {expected['reminders']}, got {len(reminders)}")
            if [call['name'] for call in calls] != expected['calls']:
                faults.append(f"tool sequence: expected {expected['calls']}, got {[call['name'] for call in calls]}")
            if expected.get('location') and any(call['arguments'].get('location') != expected['location'] for call in calls):
                faults.append('wrong query location')
            if reminders and expected.get('title'):
                if reminders[0]['title'] != expected['title']: faults.append('wrong reminder title')
                if reminders[0]['scheduled_at_utc'] != expected['due']: faults.append('wrong reminder time')
            if reminders and expected.get('policy_days'):
                source = json.loads(reminders[0]['source_json'])
                opening = datetime.combine(date(2030, 10, 1)-timedelta(days=expected['policy_days']), datetime.min.time(),
                    tzinfo=timezone(timedelta(hours=8))).astimezone(timezone.utc).isoformat()
                if reminders[0]['scheduled_at_utc'] != opening: faults.append('wrong policy-derived time')
                if not source.get('policy_fingerprint'): faults.append('missing policy evidence')
            event = ChatEvent('onebot', 'group', 'read', 'eval', 'owner', '')
            trips = store.trips.list_for_owner(event)
            if 'trips' in expected:
                if len(trips) != expected['trips']: faults.append('wrong trip count')
                if trips:
                    trip = trips[0]
                    days = trip['plan']['days']
                    if len(days) != expected['days'] or days[0]['date'] != expected['start']: faults.append('wrong trip dates')
                    if any(len(day['activities']) != 2 for day in days): faults.append('wrong daily activity count')
                    ids = [activity['poi_id'] for day in days for activity in day['activities']]
                    if len(ids) != len(set(ids)) or '0' not in ids: faults.append('duplicated or missing mandatory POI')
                    if expected['indoor'] and any(not indoor_place(trip['sources'][activity['poi_id']]) for activity in days[1]['activities']):
                        faults.append('indoor constraint violated')
            # All scenarios must retain cross-owner isolation, including explicit writes.
            if store.reminders.list_for_owner('onebot', 'eval', 'other'): faults.append('cross-owner reminder leak')
            other = ChatEvent('onebot', 'group', 'read-other', 'eval', 'other', '')
            if store.trips.list_for_owner(other): faults.append('cross-owner trip leak')
        except Exception as exc:
            faults.append(f'{type(exc).__name__}: {exc}')
        result = dict(id=case['id'], category=case['category'], split=case['split'], passed=not faults,
            failures=faults, elapsed_seconds=round(time.monotonic()-start, 3), usage=usage_summary(store),
            calls=calls, replies=replies)
        if live: base_client.close()
        return result


async def main(args):
    load_dotenv(ROOT / '.env')
    raw = (ROOT / 'evals/task_cases_v1.json').read_bytes()
    corpus = json.loads(raw)
    selected = [case for case in corpus['cases'] if args.split == 'all' or case['split'] == args.split]
    if args.ids: selected = [case for case in selected if case['id'] in args.ids.split(',')]
    if not selected: raise ValueError('No matching cases')
    results = []
    report = dict(mode='real_model_fixed_tools' if args.live else 'scripted_model_contracts',
        corpus_sha256=hashlib.sha256(raw).hexdigest(), model=os.getenv('LLM_MODEL_ID') if args.live else 'script-v1',
        reference_time=corpus['reference_time'], results=results)
    for case in selected:
        result = await evaluate(case, args.live, datetime.fromisoformat(corpus['reference_time']))
        results.append(result)
        print(f"{result['id']}: {'PASS' if result['passed'] else 'FAIL'} {result['failures']}", flush=True)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    report['summary'] = dict(total=len(results), passed=sum(result['passed'] for result in results),
        model_calls=sum(result['usage']['calls'] for result in results),
        prompt_tokens=sum(result['usage']['prompt_tokens'] for result in results),
        completion_tokens=sum(result['usage']['completion_tokens'] for result in results),
        usage_missing=sum(result['usage']['usage_missing'] for result in results),
        p50_seconds=statistics.median(result['elapsed_seconds'] for result in results),
        p95_seconds=sorted(result['elapsed_seconds'] for result in results)[max(0, int(len(results)*.95)-1)])
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(report['summary']))
    if any(not result['passed'] for result in results): raise SystemExit(1)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--split', choices=['all', 'development', 'holdout'], default='all')
    parser.add_argument('--ids', default='')
    parser.add_argument('--output', type=Path, default=ROOT / 'evals/results-offline.json')
    asyncio.run(main(parser.parse_args()))
