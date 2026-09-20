from datetime import datetime, timedelta, timezone, date
from pathlib import Path
import tempfile
import unittest

from adapters.onebot_app import OneBotReplyRenderer
from core.chat_transport import ChatEvent
from infrastructure.memory_store import MemoryStore
from services.personal_reminder_service import PersonalReminderService
from services.reservation_service import ReservationService
from services.reminder_scheduler import ReminderScheduler
from services.outbox_worker import OutboxWorker


class DeliveryExpiryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = MemoryStore(Path(self.temp.name) / 'expiry.db')
        self.now = datetime(2030, 9, 9, 2, tzinfo=timezone.utc)
        class Transport:
            def __init__(self): self.messages = []
            async def send(self, message): self.messages.append(message)
        self.transport = Transport()
        self.worker = OutboxWorker('onebot', self.store, self.transport)
        self.scheduler = ReminderScheduler('onebot', self.store, OneBotReplyRenderer(), lambda group: True)

    async def test_already_queued_personal_reminder_expires_after_long_outage(self):
        event = ChatEvent('onebot', 'group', 'create', 'g', 'u', '明天上午十点提醒我带证件')
        PersonalReminderService(self.store, clock=lambda: self.now).handle(event, self.store.begin_event(event.event_key))
        due = self.now + timedelta(days=1)
        self.assertEqual(await self.scheduler.scan_once(due), 1)
        self.assertEqual(await self.worker.dispatch_due_once(due + timedelta(hours=25)), 0)
        self.assertEqual(self.transport.messages, [])
        self.assertEqual(self.store.reminders.list_for_owner('onebot', 'g', 'u')[0]['delivery_status'], 'missed')

    async def test_already_queued_reservation_reminder_does_not_outlive_visit(self):
        service = ReservationService(self.store)
        image, _ = self.store.create_reservation_image('onebot:g', 'onebot', 'g', 'u', 'sha', 'image.jpg', 'image/jpeg', 10, 'fake')
        plan = service.create_draft(image, ())
        service.add_manual_item(platform='onebot', group_id='g', creator_id='u', plan_code=plan.plan_code,
            attraction_name='测试景点', visit_date=date(2030, 9, 11), advance_value=1, advance_unit='day', requires_reservation=True)
        service.confirm_plan('onebot', 'g', 'u', plan.plan_code)
        await self.scheduler.scan_once(self.now + timedelta(days=1))
        self.assertEqual(await self.worker.dispatch_due_once(self.now + timedelta(days=4)), 0)
        self.assertEqual(self.transport.messages, [])
        with self.store._connect() as connection:
            statuses = {row[0] for row in connection.execute('SELECT status FROM reservation_reminders')}
        self.assertEqual(statuses, {'expired'})

    async def test_normal_reply_is_not_mistaken_for_expiring_reminder(self):
        claim = self.store.begin_event('ordinary', now=self.now)
        self.store.prepare_event_outbox('ordinary', claim.claim_token, 'onebot', 'group', 'g', 'u', '',
            {'message': '已保存的普通回复'}, '问题', now=self.now)
        self.assertEqual(await self.worker.dispatch_due_once(self.now + timedelta(days=2)), 1)

    async def test_old_weather_reply_is_not_freshened_by_outbox_recovery(self):
        from core.tasks import TaskUpdate
        event = ChatEvent('onebot', 'group', 'old-weather', 'g', 'u', '现在天气')
        claim = self.store.begin_event(event.event_key, now=self.now)
        with self.store._connect() as connection:
            self.store.tasks.apply(connection, event, TaskUpdate('weather', 'completed', event.content, {'location': '武汉'}), self.now)
        later = self.now + timedelta(days=2)
        self.store.prepare_event_outbox(event.event_key, claim.claim_token, 'onebot', 'group', 'g', 'u', '',
            {'message': '当时的天气'}, event.content, now=later)
        self.assertEqual(await self.worker.dispatch_due_once(later), 0)
