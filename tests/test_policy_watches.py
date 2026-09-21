import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

from adapters.onebot_app import OneBotAdapter, OneBotReplyRenderer
from app.runtime_factory import build_runtime
from core.chat_transport import ChatEvent
from core.settings import Settings, OneBotSettings
from infrastructure.memory_store import MemoryStore
from services.booking_policy import BookingPolicyResolver
from services.inbox_worker import InboxWorker
from services.policy_watch_service import PolicyWatchService


class PolicyWatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = MemoryStore(Path(self.temp.name) / 'test.db')
        self.now = datetime(2030, 9, 9, 2, tzinfo=timezone.utc)
        self.page = '湖北省博物馆\n个人入馆预约可提前5天。每日0点开始放票。'
        self.error = False
        def fetch(url):
            if self.error:
                raise ValueError('offline')
            return self.page
        self.resolver = BookingPolicyResolver(sources={'湖北省博物馆': 'https://museum.example/policy'},
            fetch_text=fetch, clock=lambda: self.now)
        class Transport:
            def __init__(self): self.messages = []
            async def send(self, message): self.messages.append(message)
        self.transport = Transport()
        self.runtime = build_runtime(Settings('', '', frozenset({'100'}), '', '', '', ''), platform='onebot',
            store=self.store, transport=self.transport, reply_renderer=OneBotReplyRenderer(), group_allowed=lambda group: group == '100')
        self.service = PolicyWatchService(self.store, self.resolver, clock=lambda: self.now)
        self.runtime.application.policy_watch_service = self.service
        self.worker = InboxWorker(self.store, OneBotAdapter(OneBotSettings('http://localhost', 'out', 'in', frozenset({'100'})), self.runtime.application, self.store))

    def event(self, text='', key='read', owner='owner'):
        return ChatEvent('onebot', 'group', key, '100', owner, text)

    def handle(self, text, key='create'):
        event = self.event(text, key)
        return self.service.handle(event, self.store.begin_event(event.event_key))

    def create(self, ending='到10月1日'):
        return self.handle('监测湖北省博物馆预约规则，' + ending)

    def row(self):
        return self.store.policy_watches.list_for_owner(self.event())[0]

    async def poll(self):
        self.now = datetime.fromisoformat(self.row()['next_check_at'])
        await self.runtime.reminder_scheduler.scan_once(self.now)
        await self.worker.run_once(now=self.now, lane='scheduled')

    async def test_quiet_baseline_page_noise_change_failure_and_recovery(self):
        self.create()
        await self.poll()
        self.assertEqual(self.transport.messages, [])
        self.page += '\n友情链接更新。'
        await self.poll()
        self.assertEqual(self.transport.messages, [])
        self.page = self.page.replace('提前5天', '提前7天')
        await self.poll()
        self.assertIn('现在：提前 7 天', str(self.transport.messages[-1].payload))
        self.assertEqual(len(self.transport.messages), 1)
        self.error = True
        await self.poll()
        await self.poll()
        self.assertEqual(len(self.transport.messages), 2)
        self.error = False
        await self.poll()
        self.assertEqual(len(self.transport.messages), 3)
        self.assertIn('恢复读取', str(self.transport.messages[-1].payload))
        self.assertEqual(self.store.reminders.list_for_owner('onebot', '100', 'owner'), ())

    def test_cutoff_clock_and_period_collection(self):
        reply = self.create('到明天下午三点')
        self.assertIn('15:00', reply)
        self.assertEqual(self.row()['ends_at'], '2030-09-10T07:00:00+00:00')

    def test_ambiguous_clock_requires_period(self):
        reply = self.create('到明天三点')
        self.assertIn('上午', reply)
        self.assertEqual(self.store.policy_watches.list_for_owner(self.event()), ())
        self.handle('下午', 'period')
        self.assertEqual(self.row()['ends_at'], '2030-09-10T07:00:00+00:00')

    def test_source_continuation_does_not_rebase_relative_deadline(self):
        self.now = datetime(2030, 9, 9, 15, 59, tzinfo=timezone.utc)
        self.error = True
        self.assertIn('还没有建立', self.create('到明天'))
        self.now += timedelta(minutes=2)
        self.error = False
        self.handle('https://museum.example/policy', 'url')
        self.assertEqual(self.row()['ends_at'], '2030-09-10T15:59:59+00:00')

    async def test_owner_scope_stop_queued_and_deadline(self):
        self.create()
        row = self.row()
        self.assertFalse(self.store.policy_watches.stop(self.event(owner='other'), row['watch_id']))
        self.assertEqual(self.store.policy_watches.list_for_owner(self.event(owner='other')), ())
        self.now = datetime.fromisoformat(row['next_check_at'])
        await self.runtime.reminder_scheduler.scan_once(self.now)
        self.assertTrue(self.store.policy_watches.stop(self.event(), row['watch_id']))
        self.assertFalse(await self.worker.run_once(now=self.now, lane='scheduled'))
        self.assertEqual(self.row()['status'], 'cancelled')

    async def test_deadline_does_not_query(self):
        self.create()
        self.now = datetime.fromisoformat(self.row()['ends_at'])
        with patch.object(self.resolver, 'resolve', side_effect=AssertionError('must not query')):
            self.assertEqual(await self.runtime.reminder_scheduler.scan_once(self.now), 0)
        self.assertEqual(self.row()['status'], 'completed')

    async def test_committed_notice_recovers_without_second_query(self):
        self.create()
        self.page = self.page.replace('提前5天', '提前7天')
        self.now = datetime.fromisoformat(self.row()['next_check_at'])
        await self.runtime.reminder_scheduler.scan_once(self.now)
        job = self.store.inbox.claim(now=self.now, lane='scheduled')
        with patch.object(self.store, 'prepare_event_outbox', side_effect=RuntimeError('crash before outbox')):
            with self.assertRaises(RuntimeError):
                await self.service.execute_job(job, OneBotReplyRenderer())
        self.assertEqual(self.row()['status'], 'active')
        self.now += timedelta(hours=1)
        with patch.object(self.resolver, 'resolve', side_effect=AssertionError('cached receipt required')):
            await self.service.execute_job(job, OneBotReplyRenderer())
        self.assertEqual(len(self.store.list_outbox_for_event(job['event_key'])), 1)

    async def test_stop_while_fetching_fences_late_writes(self):
        self.create()
        row = self.row()
        self.now = datetime.fromisoformat(row['next_check_at'])
        await self.runtime.reminder_scheduler.scan_once(self.now)
        entered, release = Event(), Event()
        original = self.resolver.resolve
        def slow(*args, **kwargs):
            entered.set()
            release.wait(5)
            return original(*args, **kwargs)
        with patch.object(self.resolver, 'resolve', side_effect=slow):
            running = asyncio.create_task(self.worker.run_once(now=self.now, lane='scheduled'))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                self.store.policy_watches.stop(self.event(), row['watch_id'])
            finally:
                release.set()
                await running
        self.assertEqual(self.row()['status'], 'cancelled')
        self.assertEqual(self.transport.messages, [])

    async def test_terminal_input_failure_updates_watch(self):
        self.create()
        self.now = datetime.fromisoformat(self.row()['next_check_at'])
        await self.runtime.reminder_scheduler.scan_once(self.now)
        with self.store._connect() as connection:
            connection.execute("UPDATE inbox_jobs SET status='failed'")
        self.assertEqual(self.row()['status'], 'failed')
