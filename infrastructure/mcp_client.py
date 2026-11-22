"""Bounded Remote MCP actor. One task owns the SDK session for its whole lifetime."""
import asyncio
from contextvars import ContextVar
from collections import deque
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import asynccontextmanager, AsyncExitStack
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import time
from urllib.parse import urlsplit

import httpx
import anyio
from mcp import ClientSession
from mcp.shared.exceptions import McpError
from mcp.types import CONNECTION_CLOSED
from mcp.client.streamable_http import streamable_http_client
from mcp.client.sse import sse_client

from infrastructure.public_http import public_address


class MCPUnavailable(RuntimeError):
    def __init__(self, code='unavailable', *, submitted=None):
        self.code = code
        self.submitted = submitted
        super().__init__('铁路服务不可用（' + code + '）。')


@dataclass(frozen=True)
class RailMCPConfig:
    enabled: bool = False
    transport: str = 'streamable_http'
    endpoint: str = field(default='', repr=False)
    bearer_token: str = field(default='', repr=False)
    timeout_seconds: int = 20
    expires_at: str = ''

    @classmethod
    def parse(cls, raw):
        try:
            if not isinstance(raw, str) or len(raw) > 4096:
                raise ValueError()
            data = json.loads(raw) if raw else {}
            if not isinstance(data, dict) or set(data) - set(cls.__dataclass_fields__):
                raise ValueError()
            config = cls(**data)
            if (type(config.enabled) is not bool or type(config.timeout_seconds) is not int
                    or config.transport not in ('streamable_http', 'sse')
                    or not 3 <= config.timeout_seconds <= 45
                    or any(not isinstance(v, str) or len(v) > 2048 or any(ord(c) < 32 for c in v)
                           for v in (config.endpoint, config.bearer_token, config.expires_at))):
                raise ValueError()
            if config.endpoint:
                url = urlsplit(config.endpoint)
                if (url.scheme != 'https' or not url.hostname or url.username or url.password
                        or url.fragment or url.port not in (None, 443)):
                    raise ValueError()
            if config.enabled and not config.endpoint:
                raise ValueError()
            if config.expires_at and datetime.fromisoformat(config.expires_at).tzinfo is None:
                raise ValueError()
            return config
        except (ValueError, TypeError):
            raise ValueError('铁路配置须为受限 Remote JSON，使用公网 HTTPS、布尔开关和 3–45 秒超时。') from None

    @property
    def expired(self):
        return bool(self.expires_at and datetime.fromisoformat(self.expires_at) <= datetime.now(timezone.utc))

    def status(self):
        return {'alias': 'rail-remote', 'enabled': self.enabled, 'configured': bool(self.endpoint),
                'transport': self.transport,
                'expired': self.expired, 'timeout_seconds': self.timeout_seconds, 'expires_at': self.expires_at}


class _PrivateSDKLogs(logging.Filter):
    def __init__(self, endpoint=None):
        super().__init__()
        self.endpoint = endpoint

    def filter(self, record):
        # SDK logs contain session IDs, full endpoint URLs and response bodies even at WARNING.
        return self.endpoint is not None and self.endpoint not in record.getMessage()


@asynccontextmanager
async def remote_session(config):
    for name in ('mcp.client.streamable_http', 'mcp.client.sse', 'mcp.shared.session'):
        logger = logging.getLogger(name)
        if not any(isinstance(f, _PrivateSDKLogs) for f in logger.filters):
            logger.addFilter(_PrivateSDKLogs())
    host = urlsplit(config.endpoint).hostname
    logger = logging.getLogger('httpx')
    if not any(isinstance(f, _PrivateSDKLogs) and f.endpoint == host for f in logger.filters):
        logger.addFilter(_PrivateSDKLogs(host))
    await asyncio.to_thread(public_address, config.endpoint)
    headers = {'Authorization': 'Bearer ' + config.bearer_token} if config.bearer_token else {}
    if config.transport == 'sse':
        def http_factory(headers=None, timeout=None, auth=None):
            return httpx.AsyncClient(headers=headers, timeout=timeout, auth=auth,
                                     follow_redirects=False, trust_env=False)
        async with sse_client(config.endpoint, headers=headers, timeout=config.timeout_seconds,
                              sse_read_timeout=300, httpx_client_factory=http_factory) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=config.timeout_seconds)) as session:
                yield session
        return
    # A dedicated transport avoids proxy inheritance and redirects of credentials.
    async with httpx.AsyncClient(headers=headers,
                                 timeout=config.timeout_seconds, follow_redirects=False, trust_env=False) as http:
        async with streamable_http_client(config.endpoint, http_client=http) as (read, write, _):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=config.timeout_seconds)) as session:
                yield session


class RemoteMCPClient:
    def __init__(self, config, *, validate_tools, session_factory=remote_session, max_calls=None, max_connections=None):
        self.config, self.validate_tools, self.session_factory = config, validate_tools, session_factory
        self.max_calls = max_calls
        self.max_connections = max_connections
        self._queue = asyncio.Queue(maxsize=8)
        self._ready = asyncio.Event()
        self._task = None
        self._loop = None
        self._closed = False
        self._connection_lock = asyncio.Lock()
        self._connect_attempts = deque()
        self._last_activity = 0
        self._busy = False
        self._last_failure = {}
        self._state = 'disabled' if not config.enabled else 'stopped'
        self._info = {}
        self.counts = {'connections': 0, 'initializations': 0, 'list_tools': 0, 'tools_call': 0, 'pings': 0}
        self._schemas = {}

    def status(self):
        return {**self.config.status(), 'state': 'expired' if self.config.expired else self._state,
                **self._info, 'operations': dict(self.counts), 'last_failure': dict(self._last_failure)}

    def start(self):
        if not self._task or self._task.done():
            self._ready.clear()
            self._loop = asyncio.get_running_loop()
            if self._closed or not self.config.enabled or self.config.expired:
                self._state = 'stopped' if self._closed else 'expired' if self.config.expired else 'disabled'
                self._ready.set()
                return
            now = time.monotonic()
            while self._connect_attempts and now-self._connect_attempts[0] >= 60:
                self._connect_attempts.popleft()
            # Explicit evaluation caps are lifetime totals; production is bounded per minute.
            if ((self.max_connections is not None and self.counts['connections'] >= self.max_connections)
                    or len(self._connect_attempts) >= 2):
                self._state = 'connection_limit'
                self._ready.set()
                return
            self._connect_attempts.append(now)
            self._task = asyncio.create_task(self._run(), name='rail-remote-session')

    async def wait_ready(self):
        try:
            await asyncio.wait_for(self._ready.wait(), self.config.timeout_seconds + 1)
        except TimeoutError:
            raise MCPUnavailable('timeout',submitted=False) from None
        if self._state != 'ready':
            raise MCPUnavailable(self._state, submitted=False)
        return self.status()

    async def ensure_ready(self):
        """Recover only before submission, serialized on the original application loop."""
        if self._loop is not None and self._loop is not asyncio.get_running_loop():
            raise MCPUnavailable('wrong_event_loop', submitted=False)
        async with self._connection_lock:
            if self._closed or not self.config.enabled or self.config.expired:
                raise MCPUnavailable('stopped' if self._closed else 'expired' if self.config.expired else 'disabled', submitted=False)
            if self._task and not self._task.done() and self._state in ('connecting','stopped'):
                await self.wait_ready()
            if self._task and not self._task.done() and self._state == 'ready':
                if self._busy or not self._queue.empty() or time.monotonic()-self._last_activity < self.config.timeout_seconds:
                    return
                try:
                    await self._submit(None,{})  # Protocol ping, never a ticket submission.
                    return
                except MCPUnavailable:
                    self._task.cancel()
            if self._task and not self._task.done():
                await asyncio.shield(asyncio.gather(self._task,return_exceptions=True))
            if self._state in ('unauthorized','forbidden','schema_mismatch','invalid_public_url'):
                raise MCPUnavailable(self._state, submitted=False)
            self.start()
            await self.wait_ready()

    async def _run(self):
        pending = None
        try:
            if not self.config.enabled or self.config.expired:
                self._state = 'expired' if self.config.expired else 'disabled'
                return
            self._state = 'connecting'
            self.counts['connections'] += 1
            async with AsyncExitStack() as stack:
                async with asyncio.timeout(self.config.timeout_seconds):
                    session = await stack.enter_async_context(self.session_factory(self.config))
                    self.counts['initializations'] += 1
                    initialized = await session.initialize()
                    self.counts['list_tools'] += 1
                    tools = (await session.list_tools()).tools
                    self._schemas = self.validate_tools(tools)
                    fingerprint = hashlib.sha256(json.dumps(self._schemas, sort_keys=True).encode()).hexdigest()
                    import re
                    server = initialized.serverInfo
                    self._info = {'server': {key: value if re.fullmatch(r'[\w. -]{1,80}', value) else 'remote'
                                             for key, value in (('name', server.name), ('version', server.version))},
                                  'schema_sha256': fingerprint}
                self._state = 'ready'
                self._last_activity = time.monotonic()
                self._ready.set()
                while True:
                    pending = await self._queue.get()
                    if pending is None:
                        break
                    name, arguments, deadline, submission, future = pending
                    if future.cancelled() or asyncio.get_running_loop().time() >= deadline:
                        if not future.done():
                            future.set_exception(MCPUnavailable('timeout',submitted=False))
                        continue
                    if self.config.expired:
                        future.set_exception(MCPUnavailable('expired',submitted=False))
                        continue
                    if name is not None and self.max_calls is not None and self.counts['tools_call'] >= self.max_calls:
                        future.set_exception(MCPUnavailable('call_limit',submitted=False))
                        continue
                    self._busy = True
                    async with asyncio.timeout_at(deadline):
                        if name is None:
                            self.counts['pings'] += 1
                            result = await session.send_ping()
                        else:
                            self.counts['tools_call'] += 1
                            submission['started'] = True
                            if submission.get('observer'):submission['observer']()
                            result = await session.call_tool(name, arguments)
                    self._busy = False
                    self._last_activity = time.monotonic()
                    if not future.done():
                        future.set_result(result)
                    pending = None
        except asyncio.CancelledError:
            self._state = 'stopped'
            raise
        except Exception as error:
            failures = [error]
            self._last_failure = {}
            self._state = 'unavailable'
            while failures:
                failure = failures.pop()
                if isinstance(failure, BaseExceptionGroup):
                    failures.extend(failure.exceptions)
                elif isinstance(failure, MCPUnavailable):
                    self._state = failure.code
                elif isinstance(failure, (TimeoutError, httpx.TimeoutException)):
                    self._state = 'timeout'
                elif isinstance(failure, httpx.HTTPStatusError):
                    self._state = {401:'unauthorized',403:'forbidden',404:'endpoint_unavailable',429:'rate_limited'}.get(failure.response.status_code,'unavailable')
                elif isinstance(failure, McpError):
                    self._state = 'connection_lost' if failure.error.code == CONNECTION_CLOSED else 'protocol_error'
                    self._last_failure['protocol_code'] = failure.error.code
                elif isinstance(failure, (anyio.ClosedResourceError,anyio.BrokenResourceError,anyio.EndOfStream,httpx.NetworkError)):
                    self._state = 'connection_lost'
                if not isinstance(failure, BaseExceptionGroup):
                    self._last_failure['error_type'] = type(failure).__name__
            self._last_failure.update(error_code=self._state,at=datetime.now(timezone.utc).isoformat())
            logging.getLogger(__name__).warning('Rail MCP session failed: code=%s type=%s protocol_code=%s',
                self._state,self._last_failure.get('error_type'),self._last_failure.get('protocol_code'))
        finally:
            self._busy = False
            self._ready.set()
            if pending is not None and not pending[-1].done():
                pending[-1].set_exception(MCPUnavailable(self._state,submitted=pending[-2]['started']))
            while not self._queue.empty():
                item = self._queue.get_nowait()
                if item is not None and not item[-1].done():
                    item[-1].set_exception(MCPUnavailable(self._state,submitted=False))
            if self._state == 'ready':
                self._state = 'stopped'

    async def call(self, name, arguments):
        await self.ensure_ready()
        if name not in self._schemas:
            raise MCPUnavailable('tool_not_allowed',submitted=False)
        from jsonschema import validate, ValidationError
        try:
            validate(arguments, self._schemas[name])
        except ValidationError:
            raise MCPUnavailable('invalid_arguments',submitted=False) from None
        loop = asyncio.get_running_loop()
        if self._task.get_loop() is not loop:
            raise MCPUnavailable('wrong_event_loop',submitted=False)
        return await self._submit(name,arguments)

    async def _submit(self, name, arguments):
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        submission = {'started':False,'observer':MCP_SUBMISSION_OBSERVER.get()}
        deadline = loop.time() + self.config.timeout_seconds
        try:
            self._queue.put_nowait((name, arguments, deadline, submission, future))
        except asyncio.QueueFull:
            raise MCPUnavailable('queue_full',submitted=False) from None
        try:
            async with asyncio.timeout_at(deadline):
                return await future
        except TimeoutError:
            raise MCPUnavailable('timeout',submitted=submission['started']) from None

    async def close(self):
        self._closed = True
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self._state = 'stopped'

    def run_sync(self, factory):
        """Worker-thread bridge; all coroutine work stays on the application's existing loop."""
        loop=self._loop
        if loop is None or not loop.is_running() or self._closed:
            raise MCPUnavailable('stopped',submitted=False)
        try:
            if asyncio.get_running_loop() is loop:
                raise MCPUnavailable('wrong_event_loop')
        except RuntimeError as error:
            if isinstance(error,MCPUnavailable):
                raise
        async def execute():
            await self.ensure_ready()
            return await factory()
        future=asyncio.run_coroutine_threadsafe(execute(),loop)
        # Three RPCs, one acknowledged retry each, plus connection readiness.
        deadline=time.monotonic()+self.config.timeout_seconds*8+2
        try:
            from core.execution_scope import ensure_execution_active
            while True:
                ensure_execution_active()
                if time.monotonic()>=deadline:
                    raise MCPUnavailable('timeout')
                try:
                    return future.result(timeout=0.25)
                except FutureTimeout:
                    continue
        finally:
            if not future.done():
                future.cancel()
MCP_SUBMISSION_OBSERVER=ContextVar('mcp_submission_observer',default=None)
