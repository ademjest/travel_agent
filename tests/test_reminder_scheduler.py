import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from memory_store import MemoryStore
from outbox_worker import OutboxWorker
from reminder_scheduler import ReminderScheduler
from reservation_service import ReservationExtractionItem, ReservationService


class FakeRenderer:
    def render_reminder(self, recipient_id, text):
        return {"content": f"@{recipient_id} {text}"}


class RecordingTransport:
    def __init__(self, failures=0):
        self.failures = failures
        self.messages = []

    async def send(self, message):
        self.messages.append(message)
        if self.failures:
            self.failures -= 1
            raise RuntimeError("send failed")


class ReminderSchedulerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.temp_dir.name) / "scheduler.db")
        image, unused = self.store.create_reservation_image(
            storage_scope_id="group-a",
            platform="qq_official",
            group_id="group-a",
            uploader_id="member-a",
            sha256="e" * 64,
            file_path="data/images/ee/image.jpg",
            content_type="image/jpeg",
            byte_size=10,
            model_id="vision-model",
        )
        service = ReservationService(self.store)
        plan = self.store.create_reservation_draft(
            image_id=image.image_id,
            platform="qq_official",
            group_id="group-a",
            creator_id="member-a",
            items=({
                "extraction": ReservationExtractionItem(
                    attraction_name="青海湖",
                    price_text="90元",
                    opening_hours="08:00-19:00",
                    requires_reservation=True,
                    advance_value=1,
                    advance_unit="day",
                    booking_channel="官方小程序",
                    source_text="提前1天",
                    confidence=0.96,
                ),
                "visit_date": date(2026, 8, 16),
                "booking_date": date(2026, 8, 15),
                "date_candidates": (date(2026, 8, 16),),
                "custom_reminder_times": (),
                "reminder_policy": "default",
                "status": "ready",
            },),
            now=datetime(2026, 7, 22, tzinfo=timezone.utc),
        )
        confirmed = service.confirm_plan(
            "qq_official",
            "group-a",
            "member-a",
            plan.plan_code,
        )
        self.service = service
        self.public_code = confirmed.items[0].public_code
        self.renderer = FakeRenderer()

    def tearDown(self):
        self.temp_dir.cleanup()

    async def test_due_reminder_creates_one_outbox_without_direct_send(self):
        scheduler = ReminderScheduler(
            platform="qq_official",
            store=self.store,
            renderer=self.renderer,
            group_allowed=lambda group_id: True,
        )
        queued = await scheduler.scan_once(
            now=datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
        )
        self.assertEqual(queued, 1)
        due = self.store.list_due_outbox(
            "qq_official",
            datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0].reply_to_id, "")
        self.assertEqual(due[0].event_id, "reservation-reminder:1")

    async def test_offline_due_reminder_is_marked_delayed_in_text(self):
        scheduler = ReminderScheduler(
            "qq_official",
            self.store,
            self.renderer,
            lambda group_id: True,
        )
        await scheduler.scan_once(
            now=datetime(2026, 8, 14, 12, 20, tzinfo=timezone.utc)
        )
        due = self.store.list_due_outbox(
            "qq_official",
            datetime(2026, 8, 14, 12, 20, tzinfo=timezone.utc),
        )
        self.assertIn("延迟补发", due[0].payload["content"])
        self.assertIn("2026-08-14 20:00", due[0].payload["content"])

    async def test_past_visit_date_expires_without_outbox(self):
        scheduler = ReminderScheduler(
            "qq_official",
            self.store,
            self.renderer,
            lambda group_id: True,
        )
        queued = await scheduler.scan_once(
            now=datetime(2026, 8, 17, 1, 0, tzinfo=timezone.utc)
        )
        self.assertEqual(queued, 0)
        self.assertEqual(
            self.store.list_due_outbox(
                "qq_official",
                datetime(2026, 8, 17, 1, 0, tzinfo=timezone.utc),
            ),
            (),
        )
        reminders = self.store.list_all_reservation_reminders()
        self.assertTrue(all(item.status == "expired" for item in reminders))

    async def test_removed_group_is_blocked_without_outbox(self):
        scheduler = ReminderScheduler(
            "qq_official",
            self.store,
            self.renderer,
            lambda group_id: False,
        )
        queued = await scheduler.scan_once(
            now=datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
        )
        self.assertEqual(queued, 0)
        self.assertEqual(
            {
                item.status
                for item in self.store.list_all_reservation_reminders()
            },
            {"blocked", "pending"},
        )

    def test_two_queue_attempts_create_one_event_and_one_outbox(self):
        reminder = self.store.list_all_reservation_reminders()[0]
        now = reminder.scheduled_at_utc

        def queue_once():
            return self.store.queue_reservation_reminder(
                reminder.reminder_id,
                {"content": "预约提醒"},
                "预约提醒",
                now,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda unused: queue_once(), (1, 2)))
        self.assertEqual(results[0], results[1])
        rows = self.store.list_outbox_for_event(
            f"reservation-reminder:{reminder.reminder_id}"
        )
        self.assertEqual(len(rows), 1)

    async def test_successful_outbox_delivery_marks_reminder_sent(self):
        due_at = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
        scheduler = ReminderScheduler(
            "qq_official",
            self.store,
            self.renderer,
            lambda group_id: True,
        )
        await scheduler.scan_once(now=due_at)
        transport = RecordingTransport()
        worker = OutboxWorker("qq_official", self.store, transport)
        delivered = await worker.dispatch_due_once(now=due_at)
        self.assertEqual(delivered, 1)
        reminders = self.store.list_all_reservation_reminders()
        self.assertEqual(reminders[0].status, "sent")
        turns = self.store.get_recent_turns("group-a", "member-a")
        self.assertEqual(turns[-1].user_content, "自动预约提醒")

    async def test_failed_delivery_keeps_queued_reminder_and_error(self):
        due_at = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
        scheduler = ReminderScheduler(
            "qq_official",
            self.store,
            self.renderer,
            lambda group_id: True,
        )
        await scheduler.scan_once(now=due_at)
        worker = OutboxWorker(
            "qq_official",
            self.store,
            RecordingTransport(failures=1),
        )
        delivered = await worker.dispatch_due_once(now=due_at)
        self.assertEqual(delivered, 0)
        reminder = self.store.list_all_reservation_reminders()[0]
        self.assertEqual(reminder.status, "queued")
        self.assertEqual(reminder.last_error, "RuntimeError")
        self.assertEqual(
            self.store.list_due_outbox("qq_official", due_at),
            (),
        )
        self.assertEqual(
            len(self.store.list_due_outbox(
                "qq_official",
                due_at + timedelta(seconds=5),
            )),
            1,
        )

    async def test_cancel_while_sending_prevents_failed_retry(self):
        due_at = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
        scheduler = ReminderScheduler(
            "qq_official",
            self.store,
            self.renderer,
            lambda group_id: True,
        )
        await scheduler.scan_once(now=due_at)
        outbox = self.store.list_due_outbox("qq_official", due_at)[0]
        token = self.store.claim_outbox(outbox.outbox_id, now=due_at)
        result = self.service.cancel_item(
            "qq_official",
            "group-a",
            "member-a",
            self.public_code,
        )
        self.assertTrue(result.sending_warning)
        self.assertTrue(self.store.mark_outbox_failed(
            outbox.outbox_id,
            token,
            "RuntimeError",
            due_at + timedelta(seconds=5),
        ))
        self.assertEqual(
            self.store.list_due_outbox(
                "qq_official",
                due_at + timedelta(seconds=5),
            ),
            (),
        )


if __name__ == "__main__":
    unittest.main()
