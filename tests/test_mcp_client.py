import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import json
import logging
from types import SimpleNamespace
import unittest

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.client.sse import sse_client
from mcp.shared.exceptions import McpError
from mcp.types import CONNECTION_CLOSED, ErrorData

from evaluation.cases.rail_mcp import FakeSession, fake_session
from infrastructure.mcp_client import MCPUnavailable, RailMCPConfig, RemoteMCPClient, _PrivateSDKLogs
from infrastructure.rail_client import validate_rail_tools


class ConfigTests(unittest.TestCase):
    def test_rejects_arbitrary_commands_invalid_transports_expiry_and_private_url_syntax(self):
        for data in ({'command':'node'}, {'transport':'stdio'}, {'endpoint':'http://example.com'},
                     {'endpoint':'https://u:p@example.com'}, {'expires_at':'2026-10-27'},
                     {'timeout_seconds':True}, {'enabled':'true'}, {'enabled':True}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                RailMCPConfig.parse(json.dumps(data))

    def test_config_repr_status_and_log_filter_hide_credentials(self):
        raw={'enabled':True,'transport':'sse','endpoint':'https://rail.example/private/sse?key=secret','bearer_token':'secret'}
        cfg=RailMCPConfig.parse(json.dumps(raw))
        self.assertNotIn('secret',repr(cfg)+json.dumps(cfg.status()))
        record=logging.LogRecord('httpx',logging.INFO,'',0,'HTTP Request: %s',(cfg.endpoint,),None)
        self.assertFalse(_PrivateSDKLogs('https://rail.example/private').filter(record))


class RemoteMCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_submission_is_not_replayed_but_next_request_recovers(self):
        submissions=[]
        @asynccontextmanager
        async def factory(config):
            generation=client.counts['connections']
            session=FakeSession()
            original=session.call_tool
            async def call(name,arguments):
                submissions.append((generation,name))
                if generation==1:
                    raise McpError(ErrorData(code=CONNECTION_CLOSED,message='private provider detail'))
                return await original(name,arguments)
            session.call_tool=call
            yield session
        client=RemoteMCPClient(RailMCPConfig(enabled=True),validate_tools=validate_rail_tools,session_factory=factory)
        client.start();await client.wait_ready()
        try:
            with self.assertRaises(MCPUnavailable):
                await client.call('get-station-code-of-citys',{'citys':'first'})
            await asyncio.to_thread(client.run_sync,lambda:client.call('get-station-code-of-citys',{'citys':'next'}))
            self.assertEqual(client.counts['connections'],2)
            self.assertEqual(len(submissions),2)
            self.assertNotIn('private',json.dumps(client.status()))
        finally:
            await client.close()

    async def test_concurrent_requests_share_one_idle_probe_and_reconnection(self):
        @asynccontextmanager
        async def factory(config):
            session=FakeSession()
            generation=client.counts['connections']
            async def ping():
                if generation==1:
                    raise McpError(ErrorData(code=CONNECTION_CLOSED,message='closed'))
            session.send_ping=ping
            yield session
        client=RemoteMCPClient(RailMCPConfig(enabled=True),validate_tools=validate_rail_tools,session_factory=factory)
        client.start();await client.wait_ready()
        client._last_activity-=21
        try:
            await asyncio.gather(*(client.call('get-station-code-of-citys',{'citys':str(i)}) for i in range(5)))
            self.assertEqual(client.counts['connections'],2)
            self.assertEqual(client.counts['pings'],1)
            self.assertEqual(client.counts['tools_call'],5)
        finally:
            await client.close()

    async def test_real_sdk_sse_idle_disconnect_recovers_before_business_submission(self):
        closed=asyncio.Event();methods=[]
        @asynccontextmanager
        async def factory(config):
            generation=client.counts['connections']
            events=asyncio.Queue()
            class Stream(httpx.AsyncByteStream):
                async def __aiter__(self):
                    yield b'event: endpoint\ndata: /messages\n\n'
                    while True:
                        try:
                            payload=await asyncio.wait_for(events.get(),0.1) if generation==1 else await events.get()
                        except TimeoutError:
                            closed.set()
                            raise httpx.ReadTimeout('simulated idle disconnect') from None
                        yield ('event: message\ndata: '+json.dumps(payload)+'\n\n').encode()
                async def aclose(self): pass
            async def handler(request):
                if request.method=='GET':
                    return httpx.Response(200,headers={'content-type':'text/event-stream'},stream=Stream())
                data=json.loads(request.content);methods.append(data['method'])
                if 'id' in data:
                    if data['method']=='initialize':
                        result={'protocolVersion':'2025-11-25','capabilities':{'tools':{}},'serverInfo':{'name':'fake','version':'1'}}
                    elif data['method']=='tools/list':
                        result={'tools':[v.model_dump() for v in (await FakeSession().list_tools()).tools]}
                    elif data['method']=='ping': result={}
                    else: result={'content':[{'type':'text','text':'{}'}]}
                    await events.put({'jsonrpc':'2.0','id':data['id'],'result':result})
                return httpx.Response(202)
            def http_factory(**kwargs):
                return httpx.AsyncClient(transport=httpx.MockTransport(handler),**kwargs)
            async with sse_client('https://fake.example/sse',httpx_client_factory=http_factory) as (read,write):
                async with ClientSession(read,write) as session:
                    yield session
        client=RemoteMCPClient(RailMCPConfig(enabled=True,timeout_seconds=1),validate_tools=validate_rail_tools,session_factory=factory)
        client.start();await client.wait_ready()
        try:
            await asyncio.wait_for(closed.wait(),2)
            client._last_activity-=21
            await client.call('get-station-code-of-citys',{'citys':'city'})
            self.assertEqual(client.counts['connections'],2)
            self.assertEqual(methods.count('tools/call'),1)
        finally:
            await client.close()

    async def test_explicit_connection_budget_and_close_are_not_bypassed_by_recovery(self):
        @asynccontextmanager
        async def factory(config):
            session=FakeSession()
            async def failed(*args): raise ConnectionError('private')
            session.call_tool=failed
            yield session
        client=RemoteMCPClient(RailMCPConfig(enabled=True),validate_tools=validate_rail_tools,session_factory=factory,max_connections=1)
        client.start();await client.wait_ready()
        with self.assertRaises(MCPUnavailable): await client.call('get-station-code-of-citys',{'citys':'city'})
        client._connect_attempts=type(client._connect_attempts)(t-61 for t in client._connect_attempts)
        with self.assertRaises(MCPUnavailable) as caught:
            await client.call('get-station-code-of-citys',{'citys':'city'})
        self.assertEqual(caught.exception.code,'connection_limit')
        self.assertFalse(caught.exception.submitted)
        await client.close()
        with self.assertRaises(MCPUnavailable): await client.call('get-station-code-of-citys',{'citys':'city'})
        self.assertEqual(client.counts['connections'],1)

    async def test_production_cooldown_allows_later_request_without_unbounded_reconnect(self):
        @asynccontextmanager
        async def factory(config):
            if client.counts['connections']<=2:
                raise ConnectionError('private')
            yield FakeSession()
        client=RemoteMCPClient(RailMCPConfig(enabled=True),validate_tools=validate_rail_tools,session_factory=factory)
        try:
            for _ in range(3):
                with self.assertRaises(MCPUnavailable):
                    await client.call('get-station-code-of-citys',{'citys':'city'})
            self.assertEqual(client.counts['connections'],2)
            client._connect_attempts=type(client._connect_attempts)(t-61 for t in client._connect_attempts)
            await client.call('get-station-code-of-citys',{'citys':'city'})
            self.assertEqual(client.counts['connections'],3)
            self.assertEqual(client.counts['tools_call'],1)
        finally:
            await client.close()

    async def test_acknowledged_tool_error_keeps_session_and_respects_call_cap(self):
        from mcp.types import CallToolResult
        @asynccontextmanager
        async def factory(config):
            session=FakeSession()
            async def failed(*args):return CallToolResult(isError=True,content=[])
            session.call_tool=failed
            yield session
        client=RemoteMCPClient(RailMCPConfig(enabled=True),validate_tools=validate_rail_tools,session_factory=factory,max_calls=1)
        client.start();await client.wait_ready()
        try:
            self.assertTrue((await client.call('get-station-code-of-citys',{'citys':'city'})).isError)
            self.assertEqual(client.status()['state'],'ready')
            with self.assertRaises(MCPUnavailable) as caught:
                await client.call('get-station-code-of-citys',{'citys':'city'})
            self.assertEqual(caught.exception.code,'call_limit')
            self.assertFalse(caught.exception.submitted)
            self.assertEqual(client.counts['connections'],1)
            self.assertEqual(client.counts['tools_call'],1)
        finally:
            await client.close()

    async def test_session_timeout_closes_owner_without_resubmitting(self):
        entered=asyncio.Event()
        closed=asyncio.Event()
        @asynccontextmanager
        async def factory(config):
            session=FakeSession()
            async def stall(*args):
                entered.set()
                await asyncio.Event().wait()
            session.call_tool=stall
            try: yield session
            finally: closed.set()
        client=RemoteMCPClient(RailMCPConfig(enabled=True,timeout_seconds=0.05),validate_tools=validate_rail_tools,session_factory=factory)
        client.start();await client.wait_ready()
        with self.assertRaises(MCPUnavailable):
            await client.call('get-station-code-of-citys',{'citys':'测试'})
        await asyncio.wait_for(closed.wait(),1)
        self.assertEqual(client.counts['tools_call'],1)
        await client.close()

    async def test_queue_capacity_and_cancelled_pending_queries_never_replay(self):
        entered=asyncio.Event()
        @asynccontextmanager
        async def factory(config):
            session=FakeSession()
            async def stall(*args):
                entered.set()
                await asyncio.Event().wait()
            session.call_tool=stall
            yield session
        client=RemoteMCPClient(RailMCPConfig(enabled=True),validate_tools=validate_rail_tools,session_factory=factory)
        client.start();await client.wait_ready()
        first=asyncio.create_task(client.call('get-station-code-of-citys',{'citys':'测试'}))
        await entered.wait()
        queued=[asyncio.create_task(client.call('get-station-code-of-citys',{'citys':'测试'})) for _ in range(8)]
        await asyncio.sleep(0)
        with self.assertRaises(MCPUnavailable) as error:
            await client.call('get-station-code-of-citys',{'citys':'测试'})
        self.assertEqual(error.exception.code,'queue_full')
        for task in [first,*queued]: task.cancel()
        await asyncio.gather(first,*queued,return_exceptions=True)
        await client.close()
        self.assertEqual(client.counts['tools_call'],1)

    async def test_real_sdk_protocol_initialization_discovery_and_session_reuse(self):
        submissions=[]
        async def handler(request):
            if request.method != 'POST':
                return httpx.Response(405)
            data=json.loads(request.content)
            submissions.append(data['method'])
            if 'id' not in data:
                return httpx.Response(202)
            if data['method']=='initialize':
                result={'protocolVersion':'2025-11-25','capabilities':{'tools':{}},'serverInfo':{'name':'fake','version':'1'}}
            elif data['method']=='tools/list':
                result={'tools':[v.model_dump() for v in (await FakeSession().list_tools()).tools]}
            else:
                result={'content':[{'type':'text','text':'{}'}]}
            return httpx.Response(200,json={'jsonrpc':'2.0','id':data['id'],'result':result})
        @asynccontextmanager
        async def factory(config):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                async with streamable_http_client('https://rail.example/mcp',http_client=http) as (read,write,_):
                    async with ClientSession(read,write) as session:
                        yield session
        client=RemoteMCPClient(RailMCPConfig(enabled=True),validate_tools=validate_rail_tools,session_factory=factory)
        client.start()
        try:
            await client.wait_ready()
            await client.call('get-station-code-of-citys',{'citys':'测试'})
            await client.call('get-station-code-of-citys',{'citys':'测试'})
            self.assertEqual(submissions.count('initialize'),1)
            self.assertEqual(submissions.count('tools/call'),2)
            with self.assertRaises(MCPUnavailable):
                await client.call('buy-ticket',{})
        finally:
            await client.close()
        self.assertTrue(client._task.done())

    async def test_expired_configuration_never_opens_transport(self):
        expiry=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
        @asynccontextmanager
        async def forbidden(config):
            raise AssertionError('network attempted')
            yield
        client=RemoteMCPClient(RailMCPConfig(enabled=True,expires_at=expiry),validate_tools=validate_rail_tools,session_factory=forbidden)
        client.start()
        with self.assertRaises(MCPUnavailable):
            await client.wait_ready()
        self.assertEqual(client.status()['state'],'expired')
        await client.close()

    async def test_schema_drift_fails_before_tools_call(self):
        @asynccontextmanager
        async def factory(config):
            session=FakeSession()
            async def empty(): return SimpleNamespace(tools=[])
            session.list_tools=empty
            yield session
        client=RemoteMCPClient(RailMCPConfig(enabled=True),validate_tools=validate_rail_tools,session_factory=factory)
        client.start()
        with self.assertRaises(MCPUnavailable):
            await client.wait_ready()
        self.assertEqual(client.status()['state'],'schema_mismatch')
        self.assertEqual(client.counts['tools_call'],0)
        await client.close()

    async def test_call_limit_cancelled_queue_and_single_task_cleanup(self):
        owners=[]
        @asynccontextmanager
        async def factory(config):
            owners.append(asyncio.current_task())
            try: yield FakeSession()
            finally: owners.append(asyncio.current_task())
        client=RemoteMCPClient(RailMCPConfig(enabled=True),validate_tools=validate_rail_tools,session_factory=factory,max_calls=1)
        client.start(); await client.wait_ready()
        await client.call('get-station-code-of-citys',{'citys':'测试'})
        with self.assertRaises(MCPUnavailable) as caught:
            await client.call('get-station-code-of-citys',{'citys':'测试'})
        self.assertEqual(caught.exception.code,'call_limit')
        await client.close()
        self.assertIs(owners[0],owners[1])

    async def test_failed_connection_does_not_retry_forever(self):
        @asynccontextmanager
        async def factory(config):
            raise RuntimeError('https://sensitive.example/secret')
            yield
        client=RemoteMCPClient(RailMCPConfig(enabled=True),validate_tools=validate_rail_tools,session_factory=factory)
        for _ in range(3):
            client.start()
            with self.assertRaises(MCPUnavailable): await client.wait_ready()
        self.assertEqual(client.counts['connections'],2)
        self.assertNotIn('sensitive',json.dumps(client.status()))
        await client.close()

    async def test_cancellation_before_dispatch_does_not_submit(self):
        client=RemoteMCPClient(RailMCPConfig(enabled=True),validate_tools=validate_rail_tools,session_factory=fake_session)
        client.start(); await client.wait_ready()
        task=asyncio.create_task(client.call('get-station-code-of-citys',{'citys':'测试'}))
        task.cancel()
        await asyncio.gather(task,return_exceptions=True)
        self.assertEqual(client.counts['tools_call'],0)
        await client.close()
