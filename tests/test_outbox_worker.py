import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from chat_transport import OutgoingMessage
from memory_store import MemoryStore
from outbox_worker import MAX_OUTBOX_ATTEMPTS, OutboxWorker, retry_delay


class FakeTransport:
    def __init__(self, failures=0):
        self.failures = failures
        self.messages = []

    async def send(self, message):
        self.messages.append(message)
        if self.failures:
            self.failures -= 1
            raise RuntimeError("send failed")


class OutboxWorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        database_path = Path(self.temp_dir.name) / "memory.db"
        self.store = MemoryStore(database_path)
        self.now = datetime(2026, 7, 19, 8, 0, tzinfo=timezone.utc)
        self.expected = OutgoingMessage(
            channel="group",
            target_id="group-a",
            reply_to_id="message-1",
            payload={"msg_type": 0, "content": "reply"},
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def enqueue(self, event_id="qq_official:group:group-a:message-1"):
        claim = self.store.begin_event(event_id, now=self.now)
        return self.store.prepare_event_outbox(
            event_id=event_id,
            claim_token=claim.claim_token,
            platform="qq_official",
            channel="group",
            target_id="group-a",
            sender_id="member-a",
            reply_to_id="message-1",
            payload={"msg_type": 0, "content": "reply"},
            memory_content="question",
            now=self.now,
        )

    async def test_successful_send_completes_event(self):
        self.enqueue()
        transport = FakeTransport()
        worker = OutboxWorker("qq_official", self.store, transport)

        delivered = await worker.dispatch_due_once(now=self.now)

        self.assertEqual(delivered, 1)
        self.assertEqual(transport.messages, [self.expected])
        self.assertEqual(
            self.store.get_event_status(
                "qq_official:group:group-a:message-1"
            ),
            "completed",
        )

    async def test_transient_failure_is_retried_without_regenerating_reply(self):
        self.enqueue()
        transport = FakeTransport(failures=1)
        worker = OutboxWorker("qq_official", self.store, transport)

        first = await worker.dispatch_due_once(now=self.now)
        before_retry = await worker.dispatch_due_once(
            now=self.now + timedelta(seconds=4)
        )
        second = await worker.dispatch_due_once(
            now=self.now + timedelta(seconds=5)
        )

        self.assertEqual((first, before_retry, second), (0, 0, 1))
        self.assertEqual(transport.messages, [self.expected, self.expected])

    async def test_repeated_failures_use_a_capped_retry_delay(self):
        self.enqueue()
        transport = FakeTransport(failures=6)
        worker = OutboxWorker("qq_official", self.store, transport)
        current = self.now

        for seconds in (5, 15, 60, 300, 900, 900):
            await worker.dispatch_due_once(now=current)
            current += timedelta(seconds=seconds)

        self.assertEqual(len(transport.messages), 6)
        self.assertEqual(retry_delay(6), timedelta(seconds=900))
        self.assertEqual(
            self.store.get_event_status(
                "qq_official:group:group-a:message-1"
            ),
            "processing",
        )

    async def test_poison_message_moves_to_dead_letter_and_stops_retrying(self):
        self.enqueue()
        transport = FakeTransport(failures=MAX_OUTBOX_ATTEMPTS + 1)
        worker = OutboxWorker("qq_official", self.store, transport)
        current = self.now

        for attempt in range(MAX_OUTBOX_ATTEMPTS):
            await worker.dispatch_due_once(now=current)
            current += retry_delay(attempt + 1)

        self.assertEqual(len(transport.messages), MAX_OUTBOX_ATTEMPTS)
        self.assertEqual(
            self.store.get_event_status(
                "qq_official:group:group-a:message-1"
            ),
            "dead_letter",
        )
        self.assertEqual(
            self.store.list_due_outbox("qq_official", now=current),
            (),
        )
        await worker.dispatch_due_once(now=current + timedelta(days=1))
        self.assertEqual(len(transport.messages), MAX_OUTBOX_ATTEMPTS)

    async def test_startup_recovery_sends_row_after_lease_expiry(self):
        outbox_id = self.enqueue()
        self.store.claim_outbox(
            outbox_id,
            now=self.now,
            lease_duration=timedelta(minutes=1),
        )
        transport = FakeTransport()
        worker = OutboxWorker("qq_official", self.store, transport)

        delivered = await worker.dispatch_due_once(
            now=self.now + timedelta(minutes=1)
        )

        self.assertEqual(delivered, 1)
        self.assertEqual(transport.messages, [self.expected])

    async def test_concurrent_workers_do_not_send_the_same_row_twice(self):
        self.enqueue()
        first_transport = FakeTransport()
        second_transport = FakeTransport()
        first_worker = OutboxWorker(
            "qq_official", self.store, first_transport
        )
        second_worker = OutboxWorker(
            "qq_official", self.store, second_transport
        )

        await asyncio.gather(
            first_worker.dispatch_due_once(now=self.now),
            second_worker.dispatch_due_once(now=self.now),
        )

        self.assertEqual(
            len(first_transport.messages) + len(second_transport.messages),
            1,
        )

    async def test_each_row_gets_a_fresh_lease_time_in_a_slow_batch(self):
        second_event = "qq_official:group:group-a:message-2"
        self.enqueue()
        claim = self.store.begin_event(second_event, now=self.now)
        self.store.prepare_event_outbox(
            event_id=second_event,
            claim_token=claim.claim_token,
            platform="qq_official",
            channel="group",
            target_id="group-a",
            sender_id="member-a",
            reply_to_id="message-2",
            payload={"msg_type": 0, "content": "second"},
            memory_content="question-2",
            now=self.now,
        )
        clock = SimpleClock(self.now)

        class SlowBatchTransport(FakeTransport):
            async def send(inner_self, message):
                inner_self.messages.append(message)
                if len(inner_self.messages) == 1:
                    clock.current += timedelta(minutes=3)
                    return
                due = self.store.list_due_outbox(
                    "qq_official",
                    now=clock.current,
                )
                self.assertEqual(due, ())

        transport = SlowBatchTransport()
        worker = OutboxWorker(
            "qq_official",
            self.store,
            transport,
            clock=clock,
        )

        delivered = await worker.dispatch_due_once()

        self.assertEqual(delivered, 2)


class SimpleClock:
    def __init__(self, current):
        self.current = current

    def __call__(self):
        return self.current
