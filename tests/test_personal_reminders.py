from datetime import datetime, timedelta, timezone
import tempfile
from pathlib import Path
import unittest

from core.chat_transport import ChatEvent
from infrastructure.memory_store import MemoryStore
from services.personal_reminder_service import PersonalReminderService
from services.reminder_time import is_time_answer, parse_reminder_time
from services.reminder_scheduler import ReminderScheduler
from services.outbox_worker import OutboxWorker
from adapters.onebot_app import OneBotReplyRenderer


class PersonalReminderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = MemoryStore(Path(self.temp.name) / 'reminders.db')
        self.now = datetime(2030, 9, 9, 2, tzinfo=timezone.utc)
        self.service = PersonalReminderService(self.store, clock=lambda: self.now)
        self.scheduler = ReminderScheduler('onebot', self.store, OneBotReplyRenderer(), lambda group: True)

    def handle(self, text, event_id='create'):
        event = ChatEvent('onebot', 'group', event_id, 'group', 'owner', text)
        claim = self.store.begin_event(event.event_key)
        return self.service.handle(event, claim)

    def test_time_answer_accepts_trailing_particles_but_not_other_intents(self):
        for text in ('10点吧', '上午吧', '就上午十点吧。', '晚上八点半呀！', '10:00呢？'):
            with self.subTest(text=text):
                self.assertTrue(is_time_answer(text))
        for text in ('吧', '不要10点吧', '10点或者11点吧', '明天的天气呢', '取消提醒吧'):
            with self.subTest(text=text):
                self.assertFalse(is_time_answer(text))

    def test_spoken_time_followup_keeps_task_and_requires_period(self):
        self.assertIn('几点', self.handle('明天提醒我预约陕西历史博物馆。'))
        self.assertIn('上午还是', self.handle('10点吧', 'clock'))
        self.assertEqual(self.store.reminders.list_for_owner('onebot', 'group', 'owner'), ())
        reply = self.handle('上午吧', 'period')
        self.assertIn('2030-09-10 10:00', reply)
        rows = self.store.reminders.list_for_owner('onebot', 'group', 'owner')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['title'], '预约陕西历史博物馆')

    def test_time_table(self):
        cases = (
            ('明天上午十点', '2030-09-10', '10:00'),
            ('9月20号早上九点', '2030-09-20', '09:00'),
            ('后天晚上八点半', '2030-09-11', '20:30'),
            ('2031-01-01 09:05', '2031-01-01', '09:05'),
            ('十五分钟后', '2030-09-09', '10:15:00'),
            ('一小时后', '2030-09-09', '11:00:00'),
            ('下周五上午十点', '2030-09-20', '10:00'),
            ('20号上午十点', '2030-09-20', '10:00'),
            ('明天晚上十二点', '2030-09-11', '00:00'),
        )
        for text, day, clock in cases:
            with self.subTest(text=text):
                parsed = parse_reminder_time(text, self.now)
                self.assertEqual((parsed.day, parsed.clock, parsed.error), (day, clock, ''))

    def test_invalid_past_and_missing_times_never_guess(self):
        for text in ('昨天下午三点', '昨天上午九点', '2030-02-30 10:00', '今天09:00', '明天25:00'):
            with self.subTest(text=text):
                parsed = parse_reminder_time(text, self.now)
                self.assertIsNone(parsed.instant())
        self.assertIsNone(parse_reminder_time('明天', self.now).instant())
        self.assertIsNone(parse_reminder_time('9月20日', self.now).instant())

    def test_relative_minutes_never_round_down_to_an_earlier_time(self):
        now = self.now.replace(second=55, microsecond=500000)
        instant = parse_reminder_time('两分钟后', now).instant()
        self.assertGreaterEqual(instant, now + timedelta(minutes=2))
        self.assertLess(instant, now + timedelta(minutes=2, seconds=1))

    def test_unqualified_hour_requires_period_but_edits_can_keep_existing_period(self):
        candidate = parse_reminder_time('明天八点', self.now)
        self.assertTrue(candidate.needs_period)
        self.assertIsNone(candidate.instant())
        completed = parse_reminder_time('下午', self.now, day=candidate.day, clock=candidate.clock, period_required=True)
        self.assertEqual(completed.clock, '20:00')
        edited = parse_reminder_time('改成九点半', self.now, day='2030-09-10', clock='20:00')
        self.assertEqual(edited.clock, '21:30')

    def test_negated_or_multiple_requests_do_not_commit(self):
        for index, text in enumerate(('不要明天提醒我抢票', '我只是问一下怎么提醒我',
                                     '明天十点提醒我抢票，明天两点提醒我带证件')):
            self.handle(text, str(index))
        self.assertEqual(self.store.reminders.list_for_owner('onebot', 'group', 'owner'), ())

    async def test_restart_retry_and_receipt_state(self):
        self.handle('明天上午十点提醒我抢票')
        store = MemoryStore(self.store.database_path)
        due = self.now + timedelta(days=1)
        scheduler = ReminderScheduler('onebot', store, OneBotReplyRenderer(), lambda group: True)
        await scheduler.scan_once(due)
        class FailingTransport:
            calls = 0
            async def send(self, message):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError('temporary outage')
        transport = FailingTransport()
        worker = OutboxWorker('onebot', store, transport)
        self.assertEqual(await worker.dispatch_due_once(due), 0)
        self.assertEqual(await worker.dispatch_due_once(due + timedelta(seconds=6)), 1)
        self.assertEqual(store.reminders.list_for_owner('onebot', 'group', 'owner')[0]['delivery_status'], 'sent')
        self.assertEqual(store.get_recent_turns('onebot:group', 'owner'), ())

    async def test_stale_and_disallowed_reminders_are_visible_not_sent(self):
        self.handle('明天上午十点提醒我抢票')
        self.assertEqual(await self.scheduler.scan_once(self.now + timedelta(days=3)), 0)
        self.assertEqual(self.store.reminders.list_for_owner('onebot', 'group', 'owner')[0]['delivery_status'], 'missed')
        self.handle('明天上午十一点提醒我带证件', 'second')
        scheduler = ReminderScheduler('onebot', self.store, OneBotReplyRenderer(), lambda group: False)
        self.assertEqual(await scheduler.scan_once(self.now + timedelta(days=1, hours=1)), 0)
        self.assertEqual(self.store.reminders.list_for_owner('onebot', 'group', 'owner')[0]['delivery_status'], 'blocked')

    async def test_edit_refuses_already_claimed_delivery(self):
        self.handle('明天上午十点提醒我抢票')
        due = self.now + timedelta(days=1)
        await self.scheduler.scan_once(due)
        row = self.store.list_due_outbox('onebot', due)[0]
        self.assertIsNotNone(self.store.claim_outbox(row.outbox_id, due))
        with self.assertRaisesRegex(ValueError, '正在投递'):
            self.handle('把刚才那条改成九点半', 'edit')
        self.assertEqual(self.store.reminders.list_for_owner('onebot', 'group', 'owner')[0]['version'], 1)
