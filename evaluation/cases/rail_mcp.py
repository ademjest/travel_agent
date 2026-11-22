"""U2 contract/effect trial; fake by default, explicit independent live-call ceilings."""
import argparse
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace

from mcp.types import CallToolResult, TextContent, Tool
from infrastructure.mcp_client import RailMCPConfig, RemoteMCPClient
from infrastructure.rail_client import REQUIRED_TOOLS, RailClient, validate_rail_tools


class FakeSession:
    async def initialize(self):
        return SimpleNamespace(serverInfo=SimpleNamespace(name='fake-12306', version='fixture-v1'))

    async def list_tools(self):
        return SimpleNamespace(tools=[Tool(name=name, inputSchema={'type': 'object',
            'properties': {key: {'type': kind} for key, kind in fields.items()}}) for name, fields in REQUIRED_TOOLS.items()])

    async def call_tool(self, name, arguments):
        if name == 'get-tickets':
            data = [{'start_train_code': 'D1', 'start_time': '23:30', 'arrive_time': '01:00', 'lishi': '01:30',
                     'from_station_telecode': 'ABC', 'to_station_telecode': 'ABC',
                     'from_station': '测试东', 'to_station': '测试南', 'prices': [{'seat_name': '二等座', 'num': '--', 'price': None}]}]
        else:
            key=next(iter(arguments.values()))
            data = {key.removesuffix('站') if name=='get-station-code-by-names' else key: {'station_code': 'ABC'}}
        return CallToolResult(content=[TextContent(type='text', text=json.dumps(data))])


@asynccontextmanager
async def fake_session(config):
    yield FakeSession()


async def run(args):
    config = RailMCPConfig.parse(os.getenv('RAIL_MCP_CONFIG', '')) if args.authorize_live else RailMCPConfig(enabled=True)
    if args.authorize_live and not config.enabled:
        raise ValueError('Live trial requires an enabled Remote configuration.')
    snapshot = json.loads(args.replay.read_text(encoding='utf-8')) if args.replay else None
    if snapshot:
        class ReplaySession(FakeSession):
            def __init__(self): self.cursor = 0
            async def call_tool(self, name, arguments):
                saved = snapshot['responses'][self.cursor]
                self.cursor += 1
                if name != saved['tool'] or any(arguments.get(k) != v for k,v in saved['arguments'].items() if k!='limitedNum'):
                    raise ValueError('Replay request mismatch')
                return CallToolResult.model_validate(saved['response'])
        @asynccontextmanager
        async def replay_session(config):
            yield ReplaySession()
    kwargs = {} if args.authorize_live else {'session_factory': replay_session if snapshot else fake_session}
    mcp = RemoteMCPClient(config, validate_tools=validate_rail_tools, max_calls=args.max_calls,
                          max_connections=args.max_connections, **kwargs)
    started = time.monotonic()
    results, raw = [], []
    report = {'mode': 'live_provider' if args.authorize_live else 'offline_replay' if snapshot else 'offline', 'results': results,
              'raw_responses': raw, 'remote_internal_http_requests': None, 'model_calls': 0,
              'provider_cost': None, 'limits': {'tools_call': args.max_calls, 'connections': args.max_connections, 'seconds': args.seconds}}
    class Recording:
        async def call(self, name, arguments):
            value = await mcp.call(name, arguments)
            raw.append({'tool': name, 'arguments': arguments, 'response': value.model_dump(mode='json')})
            return value
    try:
        async with asyncio.timeout(args.seconds):
            mcp.start()
            await mcp.wait_ready()
            rail = RailClient(Recording())
            queries = [('city', '北京', '上海', 'high_speed', (0,24)),
                       ('station', '北京南', '上海虹桥', 'high_speed', (0,24)),
                       ('city', '北京', '上海', 'all', (18,24)),
                       ('station', '南京南', '上海虹桥', 'all', (0,1))] if args.authorize_live or snapshot else [
                       ('station', '测试东', '测试南', 'all', (0,24))]
            for scope, origin, destination, preference, window in queries:
                begin = time.monotonic()
                result = await rail.query(origin, destination, args.date, origin_scope=scope,
                    destination_scope=scope, train_preference=preference, departure_window=window)
                results.append({'request': {'origin':origin,'destination':destination,'date':args.date,'scope':scope,
                                           'preference':preference,'window':window},
                                'result': result, 'elapsed_seconds': round(time.monotonic()-begin,3),
                                'passed': result['status'] in ('ok','empty')})
    except Exception as error:
        report['failure_type'] = type(error).__name__
    finally:
        report['connection'] = mcp.status()
        report['tool_schemas'] = mcp._schemas
        await mcp.close()
        report['elapsed_seconds'] = round(time.monotonic()-started,3)
        report['tested_at'] = datetime.now(timezone.utc).isoformat()
        report['summary'] = {'total':len(results),'passed':sum(r['passed'] for r in results)}
        # A nonempty exact-station result is required; connection alone never passes.
        report['sample_gate_passed'] = bool(results) and all(r['passed'] for r in results) and any(
            r['request']['scope']=='station' and r['result']['status']=='ok' for r in results)
        report['limitations'] = ['Small sample only; city-code completeness and upstream prices/seats are not independently verified.']
        serialized = json.dumps(report,ensure_ascii=False,indent=2)
        for secret in (config.endpoint, config.bearer_token):
            if secret:
                serialized = serialized.replace(secret,'[redacted]')
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(serialized+'\n',encoding='utf-8')
    return report['sample_gate_passed']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--authorize-live',action='store_true')
    parser.add_argument('--replay',type=Path)
    parser.add_argument('--date',default='2026-10-30')
    parser.add_argument('--max-calls',type=int,default=12)
    parser.add_argument('--max-connections',type=int,default=1)
    parser.add_argument('--seconds',type=int,default=120)
    args=parser.parse_args()
    if (not 1<=args.max_calls<=12 or not 1<=args.max_connections<=2 or not 1<=args.seconds<=120
            or args.output.exists() or args.authorize_live and args.replay):
        parser.error('Use fresh output and bounded limits: 12 calls, 2 connections, 120 seconds.')
    raise SystemExit(0 if asyncio.run(run(args)) else 1)


if __name__=='__main__':
    main()
