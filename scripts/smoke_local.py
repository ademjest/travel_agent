"""Opt-in local integration checks. Never sends a QQ message or uses production storage."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv
import requests

from agents.travel_agent import TravelAgent
from core.settings import Settings, OneBotSettings
from infrastructure.memory_store import ConversationTurn, MemoryStore
from services.reservation_service import ReservationService
from services.travel_service import TravelService
from tools.agent_tools import AgentToolContext
from tools.reservation_tools import AgentToolRouter


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--napcat', action='store_true')
    parser.add_argument('--live-model', action='store_true')
    parser.add_argument('--check', help='Run only this named check')
    args = parser.parse_args()
    load_dotenv()
    results = []

    def check(name, operation):
        if args.check and args.check != name:
            return
        try:
            details = operation()
            results.append({'check': name, 'status': 'passed', **details})
        except Exception as exc:
            results.append({'check': name, 'status': 'failed', 'error_type': type(exc).__name__})

    if args.napcat:
        def napcat():
            settings = OneBotSettings.from_env()
            with requests.Session() as session:
                session.trust_env = False
                response = session.post(settings.http_url + '/get_status', json={},
                    headers={'Authorization': 'Bearer ' + settings.access_token}, timeout=10)
                response.raise_for_status()
                payload = response.json()
                assert payload.get('retcode') == 0 and payload.get('data', {}).get('online') is True
                return {'online': True, 'good': payload.get('data', {}).get('good')}
        check('napcat_readonly_status', napcat)

    if args.live_model:
        settings = Settings('', '', frozenset(), os.getenv('AMAP_API_KEY', ''),
            os.getenv('LLM_API_KEY', ''), os.getenv('LLM_BASE_URL', ''), os.getenv('LLM_MODEL_ID', ''))
        with tempfile.TemporaryDirectory() as folder:
            store = MemoryStore(Path(folder) / 'smoke.db')
            router = AgentToolRouter(TravelService(settings), ReservationService(store), None)
            agent = TravelAgent(settings, router.execute_result)
            context = AgentToolContext('onebot', 'smoke-group', 'smoke-user', 'smoke-event')

            def clarify():
                result = agent.run('现在天气怎么样？', tool_context=context)
                if result.status != 'needs_input' or result.traces:
                    print(json.dumps({'clarification_diagnostic': result.reply,
                        'state': result.status, 'tools': [t.name for t in result.traces]}, ensure_ascii=False))
                    raise AssertionError('clarification did not finish cleanly')
                return {'state': result.status}
            check('live_weather_clarification', clarify)

            def resume():
                history = (ConversationTurn('现在天气怎么样？', '想查询哪个城市？', datetime.now(timezone.utc).isoformat()),)
                result = agent.run('西宁', history, tool_context=context)
                assert result.status == 'completed' and any(t.name == 'get_current_weather' for t in result.traces)
                return {'state': result.status, 'tools': [t.name for t in result.traces]}
            check('live_weather_resume_and_amap', resume)

            def multiple():
                result = agent.run('西宁明天天气怎么样？从西宁站到西宁曹家堡机场的路况怎么样？', tool_context=context)
                names = {t.name for t in result.traces}
                assert result.status == 'completed' and {'get_weather_forecast', 'get_route_traffic'} <= names
                return {'state': result.status, 'tools': sorted(names)}
            check('live_multiple_travel_tools', multiple)

            def reservations():
                result = agent.run('帮我查看目前的预约提醒', tool_context=context)
                assert result.status == 'completed' and [t.name for t in result.traces] == ['list_reservation_plans']
                return {'state': result.status, 'tools': [t.name for t in result.traces]}
            check('live_reservation_readonly', reservations)
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 1 if any(item['status'] == 'failed' for item in results) else 0


if __name__ == '__main__':
    raise SystemExit(main())
