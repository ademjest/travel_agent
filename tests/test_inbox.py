import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import subprocess
import sys
import textwrap
from threading import Event
import unittest
from unittest.mock import Mock

from fastapi.testclient import TestClient

from adapters.onebot_app import OneBotAdapter, OneBotReplyRenderer, create_onebot_app
from agents.travel_agent import TravelAgent
from app.runtime_factory import build_runtime
from core.execution_scope import CURRENT_EXECUTION, ExecutionScope, ExecutionRevoked
from core.settings import Settings, OneBotSettings
from infrastructure.attachment_cache import AttachmentCache
from infrastructure.memory_store import MemoryStore
from services.inbox_worker import InboxWorker
from test_travel_agent import FakeClient, assistant_message, completion, tool_call


class RecordingTransport:
    def __init__(self):
        self.messages = []

    async def send(self, message):
        self.messages.append(message)
        return f'local-{len(self.messages)}'


class InboxTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = MemoryStore(Path(self.temp.name) / 'inbox.db')
        self.transport = RecordingTransport()
        self.settings = Settings('', '', frozenset({'100'}), '', '', '', '')
        self.onebot = OneBotSettings('http://localhost', 'out', 'in', frozenset({'100'}))
        self.runtime = build_runtime(self.settings, platform='onebot', store=self.store, transport=self.transport,
            reply_renderer=OneBotReplyRenderer(), group_allowed=lambda group: group == '100')
        self.adapter = OneBotAdapter(self.onebot, self.runtime.application, self.store)
        self.worker = InboxWorker(self.store, self.adapter)

    @staticmethod
    def payload(text, message_id='1', owner='200', *, mention=False):
        segments = ([{'type': 'at', 'data': {'qq': '300'}}] if mention else [])
        segments.append({'type': 'text', 'data': {'text': text}})
        return {'post_type': 'message', 'message_type': 'group', 'group_id': '100', 'user_id': owner,
                'self_id': '300', 'message_id': message_id, 'message': segments}

    async def test_acceptance_is_persistent_and_does_not_execute_inline(self):
        result = await self.worker.submit(self.payload('ping'))
        self.assertEqual(result['status'], 'accepted')
        self.assertEqual(self.transport.messages, [])
        self.assertEqual(self.store.inbox.get('onebot:group:100:1')['status'], 'pending')
        await self.worker.run_once()
        self.assertEqual(self.transport.messages[-1].payload['message'], 'pong')
        self.assertEqual(self.store.inbox.get('onebot:group:100:1')['status'], 'completed')
        await self.worker.submit(self.payload('ping'))
        self.assertFalse(await self.worker.run_once())
        self.assertEqual(len(self.transport.messages), 1)

    async def test_queued_city_reply_waits_for_original_task_state(self):
        calls = []
        client = FakeClient([
            completion(assistant_message(tool_calls=[tool_call('w', 'get_current_weather', '{"location":"武汉"}')])),
            completion(assistant_message(content='武汉晴')),
        ])
        self.runtime.application.travel_agent = TravelAgent(Settings('', '', frozenset(), '', 'fake', 'https://example.test', 'fake'),
            lambda name, args: calls.append((name, args)) or '武汉晴', client=client)
        await self.worker.submit(self.payload('现在天气怎么样', 'first', mention=True))
        await self.worker.submit(self.payload('武汉', 'second'))
        await self.worker.run_once()
        await self.worker.run_once()
        self.assertEqual(calls, [('get_current_weather', {'location': '武汉'})])
        self.assertEqual(self.transport.messages[-1].payload['message'], '武汉晴')

    async def test_owner_order_does_not_block_other_owners(self):
        started, release = Event(), Event()
        def handle(text):
            if text == 'slow':
                started.set()
                release.wait(5)
            return 'done:' + text
        self.runtime.travel_service.handle = handle
        await self.worker.submit(self.payload('slow', 'a', mention=True))
        await self.worker.submit(self.payload('ping', 'b'))
        first = asyncio.create_task(self.worker.run_once())
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 2))
            self.assertFalse(await self.worker.run_once())
            await self.worker.submit(self.payload('ping', 'c', owner='other'))
            self.assertTrue(await self.worker.run_once())
            self.assertEqual(self.store.inbox.get('onebot:group:100:c')['status'], 'completed')
        finally:
            release.set()
            await first
        await self.worker.run_once()
        self.assertEqual(self.store.inbox.get('onebot:group:100:b')['status'], 'completed')

    async def test_cancel_fences_late_legacy_database_writes(self):
        started, release = Event(), Event()
        def handle(text):
            started.set()
            release.wait(5)
            self.store.add_document('onebot:100', '200', 'late.md', 'late', 'must not save', ['must not save'])
            return 'saved'
        self.runtime.travel_service.handle = handle
        await self.worker.submit(self.payload('slow', 'slow', mention=True))
        running = asyncio.create_task(self.worker.run_once())
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 2))
            await self.worker.submit(self.payload('取消正在处理的请求', 'cancel'))
            self.assertEqual(self.store.inbox.get('onebot:group:100:slow')['status'], 'cancelled')
        finally:
            release.set()
            await running
        self.assertEqual(self.store.list_document_contents('onebot:100'), ())
        self.assertNotIn('saved', str([message.payload for message in self.transport.messages]))

    async def test_stale_job_recovers_prepared_reply_without_rerunning_business(self):
        await self.worker.submit(self.payload('ping', 'recover'))
        old = self.store.inbox.claim()
        event = self.store.begin_event(old['event_key'])
        self.store.prepare_event_reply(event.event_id, event.claim_token, 'already prepared', 'ping')
        with self.store._connect() as connection:
            connection.execute('UPDATE inbox_jobs SET lease_until=? WHERE id=?',
                ((datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(), old['id']))
        self.runtime.travel_service.handle = Mock(side_effect=AssertionError('must not rerun'))
        await self.worker.run_once()
        self.assertEqual(self.transport.messages[-1].payload['message'], 'already prepared')
        scope = CURRENT_EXECUTION.set(ExecutionScope(str(self.store.database_path), old['id'], old['claim_token']))
        try:
            with self.assertRaises(ExecutionRevoked):
                self.store.save_turn('onebot:100', '200', 'late', 'late', 'late')
        finally:
            CURRENT_EXECUTION.reset(scope)

    async def test_attachment_survives_url_expiration_and_worker_restart(self):
        payload = self.payload('', 'file')
        payload['message'] = [{'type': 'file', 'data': {'name': 'trip.md', 'url': 'https://example.test/temporary', 'size': 30}}]
        document_text = '武汉三天行程：第一天参观湖北省博物馆，第二天游览东湖，第三天参观黄鹤楼。'
        self.worker.cache.downloader = lambda attachment: (document_text.encode(), 'text/plain')
        await self.worker.submit(payload)
        self.assertFalse(await self.worker.run_once())
        await self.worker.capture_once()
        self.assertEqual(len(self.store.inbox.assets('onebot:group:100:file')), 1)
        self.runtime.document_service.session.get = Mock(side_effect=AssertionError('expired URL must not be fetched'))
        restarted = InboxWorker(self.store, OneBotAdapter(self.onebot, self.runtime.application, self.store))
        await restarted.run_once()
        documents = self.store.list_document_contents('onebot:100')
        self.assertTrue(documents, str([message.payload for message in self.transport.messages]))
        self.assertEqual(documents[0].chunks, (document_text,))
        self.runtime.document_service.session.get.assert_not_called()

    async def test_payload_cannot_inject_local_attachment_paths(self):
        payload = self.payload('', 'injection')
        payload['message'] = [{'type': 'file', 'data': {'name': 'trip.md', 'url': 'https://example.test/file',
            'local_path': 'E:/secret.txt', 'local_sha256': 'fake'}}]
        await self.worker.submit(payload)
        saved = self.store.inbox.get('onebot:group:100:injection')['payload_json']
        self.assertNotIn('secret', saved)
        self.assertNotIn('local_path', saved)

    async def test_worker_shutdown_releases_lease_and_fences_old_thread(self):
        started, release, finished = Event(), Event(), Event()
        def slow(text):
            started.set()
            release.wait(5)
            try:
                self.store.save_turn('onebot:100', '200', 'old-thread', 'late', 'late')
            finally:
                finished.set()
            return 'old'
        self.runtime.travel_service.handle = slow
        await self.worker.submit(self.payload('slow', 'shutdown', mention=True))
        running = asyncio.create_task(self.worker.run_once())
        self.assertTrue(await asyncio.to_thread(started.wait, 2))
        running.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await running
        self.assertEqual(self.store.inbox.get('onebot:group:100:shutdown')['status'], 'retry')
        release.set()
        self.assertTrue(await asyncio.to_thread(finished.wait, 2))
        self.assertEqual(self.store.get_recent_turns('onebot:100', '200'), ())
        self.runtime.travel_service.handle = lambda text: 'recovered'
        await self.worker.run_once()
        self.assertEqual(self.transport.messages[-1].payload['message'], 'recovered')

    async def test_execution_timeout_cannot_write_later_and_retry_gets_event_lease(self):
        started, release, finished = Event(), Event(), Event()
        def slow(text):
            started.set()
            release.wait(5)
            try:
                self.store.save_turn('onebot:100', '200', 'timed-out', 'late', 'late')
            finally:
                finished.set()
            return 'old'
        self.runtime.travel_service.handle = slow
        self.worker.deadline_seconds = 0.3
        await self.worker.submit(self.payload('slow', 'timeout', mention=True))
        await self.worker.run_once()
        release.set()
        self.assertTrue(await asyncio.to_thread(finished.wait, 2))
        self.assertEqual(self.store.get_recent_turns('onebot:100', '200'), ())
        self.runtime.travel_service.handle = lambda text: 'retry succeeded'
        with self.store._connect() as connection:
            connection.execute("UPDATE inbox_jobs SET next_attempt_at=? WHERE event_key=?",
                ((datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(), 'onebot:group:100:timeout'))
        self.worker.deadline_seconds = 5
        await self.worker.run_once()
        self.assertEqual(self.transport.messages[-1].payload['message'], 'retry succeeded')

    async def test_cancellation_is_owner_scoped(self):
        await self.worker.submit(self.payload('ping', 'owned'))
        row = self.store.inbox.get('onebot:group:100:owned')
        await self.worker.submit(self.payload(f"取消请求 {row['id']}", 'wrong-owner', owner='other'))
        self.assertEqual(self.store.inbox.get('onebot:group:100:owned')['status'], 'pending')

    async def test_capture_checkpoint_survives_failure_of_second_file(self):
        payload = self.payload('', 'two-files')
        payload['message'] = [{'type': 'file', 'data': {'name': f'{i}.md', 'url': f'https://example.test/{i}'}} for i in (1, 2)]
        calls = []
        def download(attachment):
            calls.append(attachment.filename)
            if attachment.filename == '2.md' and calls.count('2.md') == 1:
                raise ValueError('temporary failure')
            return (('武汉旅行资料' * 10).encode(), 'text/plain')
        self.worker.cache.downloader = download
        await self.worker.submit(payload)
        await self.worker.capture_once()
        self.assertEqual(len(self.store.inbox.assets('onebot:group:100:two-files')), 1)
        with self.store._connect() as connection:
            connection.execute("UPDATE inbox_jobs SET next_attempt_at=? WHERE event_key=?",
                ((datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(), 'onebot:group:100:two-files'))
        await self.worker.capture_once()
        self.assertEqual(calls.count('1.md'), 1)
        self.assertEqual(len(self.store.inbox.assets('onebot:group:100:two-files')), 2)

    async def test_allowlist_revocation_blocks_queued_input_before_execution(self):
        await self.worker.submit(self.payload('ping', 'revoked'))
        adapter = OneBotAdapter(OneBotSettings('http://localhost', 'out', 'in', frozenset()), self.runtime.application, self.store)
        restarted = InboxWorker(self.store, adapter)
        await restarted.run_once()
        self.assertEqual(self.store.inbox.get('onebot:group:100:revoked')['status'], 'blocked')
        self.assertEqual(self.transport.messages, [])

    async def test_allowlist_revocation_blocks_attachment_download(self):
        payload = self.payload('', 'revoked-file')
        payload['message'] = [{'type': 'file', 'data': {'name': 'trip.md', 'url': 'https://example.test/file'}}]
        await self.worker.submit(payload)
        adapter = OneBotAdapter(OneBotSettings('http://localhost', 'out', 'in', frozenset()), self.runtime.application, self.store)
        restarted = InboxWorker(self.store, adapter)
        restarted.cache.downloader = Mock(side_effect=AssertionError('must not download'))
        await restarted.capture_once()
        restarted.cache.downloader.assert_not_called()
        self.assertEqual(self.store.inbox.get('onebot:group:100:revoked-file')['status'], 'blocked')

    async def test_hard_process_exit_recovers_committed_checkpoint(self):
        await self.worker.submit(self.payload('ping', 'process-crash'))
        code = textwrap.dedent('''
            import sys, time
            import infrastructure.inbox_repository as inbox
            from infrastructure.memory_store import MemoryStore
            inbox.JOB_LEASE_SECONDS = 0.1
            store = MemoryStore(sys.argv[1])
            job = store.inbox.claim()
            claim = store.begin_event(job['event_key'])
            store.prepare_event_reply(claim.event_id, claim.claim_token, 'process checkpoint', 'ping')
            print('checkpoint-ready', flush=True)
            time.sleep(30)
        ''')
        process = subprocess.Popen([sys.executable, '-c', code, str(self.store.database_path)],
            cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            line = await asyncio.wait_for(asyncio.to_thread(process.stdout.readline), timeout=5)
            self.assertEqual(line.strip(), 'checkpoint-ready')
            process.kill()
            await asyncio.to_thread(process.wait, 5)
            await asyncio.sleep(0.2)
            self.runtime.travel_service.handle = Mock(side_effect=AssertionError('must reuse checkpoint'))
            await self.worker.run_once()
            self.assertEqual(self.transport.messages[-1].payload['message'], 'process checkpoint')
            self.assertEqual(self.store.inbox.get('onebot:group:100:process-crash')['status'], 'completed')
        finally:
            if process.poll() is None:
                process.kill()
                await asyncio.to_thread(process.wait, 5)
            process.stdout.close()
            process.stderr.close()

    async def test_replayed_cancel_keeps_original_target(self):
        await self.worker.submit(self.payload('ping', 'old-target'))
        control = self.payload('取消正在处理的请求', 'control')
        key, scope, owner, clean, _ = self.adapter.normalize_for_inbox(control)
        entry = self.store.inbox.submit(key, 'onebot', scope, owner, clean, priority=1)
        claimed = self.store.inbox.claim(job_id=entry['id'])
        self.store.inbox.bind_control_target(key, 'onebot:group:100:old-target')
        self.store.inbox.cancel('onebot:group:100:old-target', 'onebot', '100', '200')
        with self.store._connect() as connection:
            connection.execute('UPDATE inbox_jobs SET lease_until=? WHERE id=?',
                ((datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(), claimed['id']))
        await self.worker.submit(self.payload('ping', 'new-target'))
        await self.worker.run_once()
        self.assertEqual(self.store.inbox.get('onebot:group:100:new-target')['status'], 'pending')
        self.assertEqual(self.store.inbox.get(key)['status'], 'completed')

    async def test_cancel_pending_dialogue_task_does_not_cancel_different_queued_request(self):
        from core.chat_transport import ChatEvent
        from core.tasks import TaskUpdate
        event = ChatEvent('onebot', 'group', 'waiting-city', '100', '200', '天气怎么样')
        self.store.tasks.prepare_result(event, self.store.begin_event(event.event_key),
            TaskUpdate('weather', 'collecting', event.content, {}, ('location',)), '哪个城市？')
        await self.worker.submit(self.payload('ping', 'separate-request'))
        await self.worker.submit(self.payload('取消当前任务', 'cancel-dialogue'))
        self.assertEqual(self.store.inbox.get('onebot:group:100:separate-request')['status'], 'pending')
        self.assertFalse(self.store.tasks.recent('onebot', 'onebot:100', '200'))

    def test_default_http_endpoint_queues_and_rejects_bad_auth_or_large_bodies(self):
        app = create_onebot_app(self.onebot, self.runtime.application, self.store)
        with TestClient(app, raise_server_exceptions=True) as client:
            response = client.post('/onebot', headers={'Authorization': 'Bearer wrong'}, json=self.payload('ping'))
            self.assertEqual(response.status_code, 401)
            response = client.post('/onebot', headers={'Authorization': 'Bearer in'}, content=b'x' * (128*1024+1))
            self.assertEqual(response.status_code, 413)
            response = client.post('/onebot', headers={'Authorization': 'Bearer in'}, json=self.payload('ping', 'http'))
            self.assertEqual(response.json()['status'], 'accepted')
            self.assertIsNotNone(self.store.inbox.get('onebot:group:100:http'))
