import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from memory_store import MemoryStore


class BarrierConnection:
    def __init__(self, connection, barrier):
        self.connection = connection
        self.barrier = barrier

    def execute(self, sql, parameters=()):
        cursor = self.connection.execute(sql, parameters)
        normalized = " ".join(sql.split())
        if (
                "SELECT * FROM upload_bindings WHERE code_hash = ?" in normalized
                and not self.connection.in_transaction):
            self.barrier.wait(timeout=5)
        return cursor

    def __getattr__(self, name):
        return getattr(self.connection, name)


class RacingMemoryStore(MemoryStore):
    def __init__(self, database_path):
        self.race_barrier = None
        super().__init__(database_path)

    @contextmanager
    def _connect(self):
        with super()._connect() as connection:
            if self.race_barrier is None:
                yield connection
            else:
                yield BarrierConnection(connection, self.race_barrier)


class MemoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        database_path = Path(self.temp_dir.name) / "memory.db"
        self.store = MemoryStore(database_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_only_returns_latest_six_turns(self):
        for index in range(8):
            self.store.save_turn(
                "group",
                "member",
                f"msg-{index}",
                f"user-{index}",
                f"assistant-{index}",
            )

        turns = self.store.get_recent_turns("group", "member")

        self.assertEqual(len(turns), 6)
        self.assertEqual(turns[0].user_content, "user-2")
        self.assertEqual(turns[-1].user_content, "user-7")

    def test_event_can_only_be_claimed_once(self):
        self.assertTrue(self.store.claim_event("event-1"))
        self.assertFalse(self.store.claim_event("event-1"))

    def test_sessions_are_isolated_by_member(self):
        self.store.save_turn("group", "member-a", "a", "A问题", "A回答")
        self.store.save_turn("group", "member-b", "b", "B问题", "B回答")

        turns = self.store.get_recent_turns("group", "member-a")

        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0].user_content, "A问题")

    def test_document_context_retrieves_relevant_chunk(self):
        self.store.add_document(
            group_openid="group",
            uploader_openid="member",
            filename="青甘行程.docx",
            sha256="hash-1",
            full_text="8月16日从西宁出发。8月18日入住敦煌酒店。",
            chunks=[
                "8月16日从西宁出发前往青海湖。",
                "8月18日入住敦煌沙洲夜市附近酒店。",
            ],
        )

        context = self.store.build_document_context("group", "敦煌住哪里？")

        self.assertIn("青甘行程.docx", context)
        self.assertIn("敦煌沙洲夜市附近酒店", context)

    def test_duplicate_document_is_not_inserted_twice(self):
        first = self.store.add_document(
            "group", "member", "plan.docx", "same-hash", "行程内容", ["行程内容"]
        )
        second = self.store.add_document(
            "group", "member", "plan-copy.docx", "same-hash", "行程内容", ["行程内容"]
        )

        self.assertTrue(first.is_new)
        self.assertFalse(second.is_new)
        self.assertEqual(first.document_id, second.document_id)

    def test_redeems_valid_upload_binding_for_private_user(self):
        now = datetime(2026, 7, 15, 8, 0, tzinfo=timezone.utc)
        self.store.create_upload_binding(
            "code-hash",
            "group-a",
            "member-a",
            now + timedelta(minutes=10),
            now=now,
        )

        result = self.store.redeem_upload_binding(
            "code-hash",
            "private-user",
            now=now,
        )

        self.assertEqual(result.status, "redeemed")
        self.assertEqual(result.binding.group_openid, "group-a")
        pending = self.store.get_pending_upload_binding("private-user", now=now)
        self.assertEqual(pending.binding_id, result.binding.binding_id)

    def test_expired_upload_binding_cannot_be_redeemed(self):
        now = datetime(2026, 7, 15, 8, 0, tzinfo=timezone.utc)
        self.store.create_upload_binding(
            "expired-hash",
            "group-a",
            "member-a",
            now - timedelta(seconds=1),
            now=now - timedelta(minutes=10),
        )

        result = self.store.redeem_upload_binding(
            "expired-hash",
            "private-user",
            now=now,
        )

        self.assertEqual(result.status, "expired")
        self.assertIsNone(result.binding)

    def test_upload_binding_cannot_be_redeemed_by_another_user(self):
        now = datetime(2026, 7, 15, 8, 0, tzinfo=timezone.utc)
        self.store.create_upload_binding(
            "single-use-hash",
            "group-a",
            "member-a",
            now + timedelta(minutes=10),
            now=now,
        )
        self.store.redeem_upload_binding(
            "single-use-hash",
            "private-user-a",
            now=now,
        )

        result = self.store.redeem_upload_binding(
            "single-use-hash",
            "private-user-b",
            now=now,
        )

        self.assertEqual(result.status, "used")
        self.assertIsNone(result.binding)

    def test_upload_binding_redeem_is_atomic_for_competing_users(self):
        database_path = Path(self.temp_dir.name) / "racing.db"
        store = RacingMemoryStore(database_path)
        now = datetime(2026, 7, 15, 8, 0, tzinfo=timezone.utc)
        store.create_upload_binding(
            "racing-hash",
            "group-a",
            "member-a",
            now + timedelta(minutes=10),
            now=now,
        )
        store.race_barrier = threading.Barrier(2)

        def redeem(user_openid):
            try:
                return store.redeem_upload_binding(
                    "racing-hash",
                    user_openid,
                    now=now,
                ).status
            except Exception as exc:
                return f"error:{type(exc).__name__}"

        with ThreadPoolExecutor(max_workers=2) as executor:
            statuses = sorted(executor.map(
                redeem,
                ("private-user-a", "private-user-b"),
            ))

        self.assertEqual(statuses, ["redeemed", "used"])

    def test_consumed_upload_binding_is_no_longer_pending(self):
        now = datetime(2026, 7, 15, 8, 0, tzinfo=timezone.utc)
        self.store.create_upload_binding(
            "consumed-hash",
            "group-a",
            "member-a",
            now + timedelta(minutes=10),
            now=now,
        )
        redemption = self.store.redeem_upload_binding(
            "consumed-hash",
            "private-user",
            now=now,
        )

        self.store.consume_upload_binding(redemption.binding.binding_id, now=now)

        self.assertIsNone(
            self.store.get_pending_upload_binding("private-user", now=now)
        )

    def test_pending_upload_binding_can_only_be_claimed_once(self):
        now = datetime(2026, 7, 15, 8, 0, tzinfo=timezone.utc)
        self.store.create_upload_binding(
            "claim-hash",
            "group-a",
            "member-a",
            now + timedelta(minutes=10),
            now=now,
        )
        self.store.redeem_upload_binding(
            "claim-hash",
            "private-user",
            now=now,
        )

        first = self.store.claim_pending_upload_binding(
            "private-user",
            now=now,
        )
        second = self.store.claim_pending_upload_binding(
            "private-user",
            now=now,
        )

        self.assertEqual(first.group_openid, "group-a")
        self.assertIsNone(second)


if __name__ == "__main__":
    unittest.main()
