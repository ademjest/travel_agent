import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from document_service import PreparedDocument
from memory_store import MemoryStore


@contextmanager
def sqlite_connection(database_path):
    with closing(sqlite3.connect(database_path)) as connection:
        with connection:
            yield connection


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


class TracingMemoryStore(MemoryStore):
    def __init__(self, database_path):
        self.statements = []
        super().__init__(database_path)

    @contextmanager
    def _connect(self):
        with super()._connect() as connection:
            connection.set_trace_callback(self.statements.append)
            yield connection


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

    def test_completed_event_cannot_be_reclaimed(self):
        claim = self.store.begin_event("completed-event")

        self.assertTrue(
            self.store.complete_event(
                claim.event_id,
                claim.claim_token,
            )
        )
        self.assertEqual(
            self.store.get_event_status("completed-event"),
            "completed",
        )
        self.assertIsNone(self.store.begin_event("completed-event"))

    def test_prepared_event_creates_one_pending_outbox_row(self):
        claim = self.store.begin_event("qq_official:group:g1:m1")

        outbox_id = self.store.prepare_event_outbox(
            event_id=claim.event_id,
            claim_token=claim.claim_token,
            platform="qq_official",
            channel="group",
            target_id="g1",
            sender_id="u1",
            reply_to_id="m1",
            payload={"msg_type": 0, "content": "reply"},
            memory_content="question",
        )

        pending = self.store.list_due_outbox("qq_official")
        self.assertEqual([item.outbox_id for item in pending], [outbox_id])
        self.assertEqual(pending[0].payload["content"], "reply")

    def test_repreparing_same_event_does_not_duplicate_outbox(self):
        event_id = "qq_official:group:g1:m2"
        first = self.store.begin_event(event_id)
        first_id = self.store.prepare_event_outbox(
            event_id=event_id,
            claim_token=first.claim_token,
            platform="qq_official",
            channel="group",
            target_id="g1",
            sender_id="u1",
            reply_to_id="m2",
            payload={"msg_type": 0, "content": "reply"},
            memory_content="question",
        )
        self.store.fail_event(event_id, first.claim_token, "send failed")
        second = self.store.begin_event(event_id)

        second_id = self.store.prepare_event_outbox(
            event_id=event_id,
            claim_token=second.claim_token,
            platform="qq_official",
            channel="group",
            target_id="g1",
            sender_id="u1",
            reply_to_id="m2",
            payload={"msg_type": 0, "content": "changed"},
            memory_content="changed question",
        )

        self.assertEqual(second_id, first_id)
        rows = self.store.list_outbox_for_event(event_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].payload["content"], "reply")

    def test_sending_row_is_recovered_after_lease_expiry(self):
        now = datetime(2026, 7, 19, 8, 0, tzinfo=timezone.utc)
        claim = self.store.begin_event(
            "qq_official:group:g1:m3",
            now=now,
        )
        outbox_id = self.store.prepare_event_outbox(
            event_id=claim.event_id,
            claim_token=claim.claim_token,
            platform="qq_official",
            channel="group",
            target_id="g1",
            sender_id="u1",
            reply_to_id="m3",
            payload={"msg_type": 0, "content": "reply"},
            memory_content="question",
            now=now,
        )

        token = self.store.claim_outbox(
            outbox_id,
            now=now,
            lease_duration=timedelta(minutes=1),
        )

        self.assertIsNotNone(token)
        self.assertEqual(
            self.store.list_due_outbox(
                "qq_official",
                now + timedelta(seconds=59),
            ),
            (),
        )
        due = self.store.list_due_outbox(
            "qq_official",
            now + timedelta(minutes=1),
        )
        self.assertEqual([item.outbox_id for item in due], [outbox_id])

    def test_mark_outbox_sent_completes_group_event_and_saves_turn(self):
        now = datetime(2026, 7, 19, 8, 0, tzinfo=timezone.utc)
        event_id = "qq_official:group:g1:m4"
        claim = self.store.begin_event(event_id, now=now)
        outbox_id = self.store.prepare_event_outbox(
            event_id=event_id,
            claim_token=claim.claim_token,
            platform="qq_official",
            channel="group",
            target_id="g1",
            sender_id="u1",
            reply_to_id="m4",
            payload={"msg_type": 0, "content": "reply"},
            memory_content="question",
            now=now,
        )
        token = self.store.claim_outbox(outbox_id, now=now)

        self.assertTrue(
            self.store.mark_outbox_sent(outbox_id, token, now=now)
        )
        self.assertEqual(self.store.get_event_status(event_id), "completed")
        turns = self.store.get_recent_turns("g1", "u1")
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0].user_content, "question")
        self.assertEqual(turns[0].assistant_content, "reply")

    def test_onebot_group_turn_uses_platform_scoped_session(self):
        event_id = "onebot:group:10001:message-1"
        claim = self.store.begin_event(event_id)
        outbox_id = self.store.prepare_event_outbox(
            event_id=event_id,
            claim_token=claim.claim_token,
            platform="onebot",
            channel="group",
            target_id="10001",
            sender_id="20001",
            reply_to_id="message-1",
            payload={"message": "reply"},
            memory_content="question",
        )
        token = self.store.claim_outbox(outbox_id)

        self.assertTrue(self.store.mark_outbox_sent(outbox_id, token))
        self.assertEqual(
            len(self.store.get_recent_turns("onebot:10001", "20001")),
            1,
        )
        self.assertEqual(
            self.store.get_recent_turns("10001", "20001"),
            (),
        )

    def test_failed_event_can_be_reclaimed_immediately(self):
        first_claim = self.store.begin_event("failed-event")

        self.assertTrue(
            self.store.fail_event(
                first_claim.event_id,
                first_claim.claim_token,
                "send failed",
            )
        )
        self.assertEqual(
            self.store.get_event_status("failed-event"),
            "failed",
        )

        second_claim = self.store.begin_event("failed-event")

        self.assertIsNotNone(second_claim)
        self.assertNotEqual(
            first_claim.claim_token,
            second_claim.claim_token,
        )

    def test_prepared_reply_survives_failure_for_retry(self):
        first_claim = self.store.begin_event("prepared-event")
        self.assertTrue(
            self.store.prepare_event_reply(
                first_claim.event_id,
                first_claim.claim_token,
                "prepared reply",
                "original message",
            )
        )
        self.assertTrue(
            self.store.fail_event(
                first_claim.event_id,
                first_claim.claim_token,
                "network failed",
            )
        )

        retry_claim = self.store.begin_event("prepared-event")

        self.assertEqual(retry_claim.prepared_reply, "prepared reply")
        self.assertEqual(
            retry_claim.prepared_memory_content,
            "original message",
        )

    def test_processing_event_can_be_reclaimed_after_lease_expires(self):
        now = datetime(2026, 7, 16, 8, 0, tzinfo=timezone.utc)
        first_claim = self.store.begin_event(
            "expired-lease-event",
            now=now,
            lease_duration=timedelta(minutes=1),
        )

        self.assertIsNone(
            self.store.begin_event(
                "expired-lease-event",
                now=now + timedelta(seconds=59),
            )
        )
        second_claim = self.store.begin_event(
            "expired-lease-event",
            now=now + timedelta(minutes=1),
        )

        self.assertIsNotNone(second_claim)
        self.assertFalse(
            self.store.complete_event(
                first_claim.event_id,
                first_claim.claim_token,
                now=now + timedelta(minutes=2),
            )
        )
        self.assertTrue(
            self.store.complete_event(
                second_claim.event_id,
                second_claim.claim_token,
                now=now + timedelta(minutes=2),
            )
        )

    def test_competing_event_claims_have_only_one_winner(self):
        def claim(_):
            return self.store.begin_event("racing-event")

        with ThreadPoolExecutor(max_workers=4) as executor:
            claims = list(executor.map(claim, range(4)))

        self.assertEqual(
            sum(event_claim is not None for event_claim in claims),
            1,
        )

    def test_legacy_processed_events_are_migrated_as_completed(self):
        database_path = Path(self.temp_dir.name) / "legacy.db"
        with sqlite_connection(database_path) as connection:
            connection.execute(
                """
                CREATE TABLE processed_events (
                    event_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "INSERT INTO processed_events (event_id, created_at) "
                "VALUES (?, ?)",
                ("legacy-event", "2026-07-15T08:00:00+00:00"),
            )

        migrated_store = MemoryStore(database_path)

        self.assertEqual(
            migrated_store.get_event_status("legacy-event"),
            "completed",
        )
        self.assertIsNone(migrated_store.begin_event("legacy-event"))
        new_claim = migrated_store.begin_event("new-event")
        self.assertIsNotNone(new_claim)

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

    def test_document_context_uses_at_most_two_relevant_chunks(self):
        self.store.add_document(
            group_openid="group",
            uploader_openid="member",
            filename="plan.docx",
            sha256="context-limit",
            full_text="青海湖行程",
            chunks=[
                "青海湖 第一片段 " + "甲" * 1000,
                "青海湖 第二片段 " + "乙" * 1000,
                "青海湖 第三片段 " + "丙" * 1000,
            ],
            summary="青海湖行程摘要",
        )

        context = self.store.build_document_context("group", "青海湖")

        self.assertLessEqual(len(context), 3200)
        self.assertIn("第一片段", context)
        self.assertIn("第二片段", context)
        self.assertNotIn("第三片段", context)

    def test_relevant_chunk_is_not_pushed_out_by_many_summaries(self):
        self.store.add_document(
            group_openid="group",
            uploader_openid="member",
            filename="早期祁连行程.docx",
            sha256="older-relevant-document",
            full_text="祁连山住宿安排在卓尔山附近。",
            chunks=["祁连山住宿安排在卓尔山附近，第二天早上八点集合。"],
            summary="祁连山旧行程摘要",
        )
        for index in range(10):
            self.store.add_document(
                group_openid="group",
                uploader_openid="member",
                filename=f"最近资料-{index}.docx",
                sha256=f"recent-summary-{index}",
                full_text="与当前问题无关的资料",
                chunks=["与当前问题无关的资料片段"],
                summary="无关摘要" * 300,
            )

        context = self.store.build_document_context(
            "group",
            "祁连山住宿怎么安排？",
        )

        self.assertLessEqual(len(context), 3200)
        self.assertIn("祁连山住宿安排在卓尔山附近", context)
        self.assertLess(
            context.index("祁连山住宿安排在卓尔山附近"),
            context.index("群内已保存的旅行文档"),
        )

    def test_document_excerpt_keeps_match_near_chunk_tail(self):
        self.store.add_document(
            group_openid="group",
            uploader_openid="member",
            filename="海西行程.md",
            sha256="tail-match",
            full_text="甲" * 1700 + "翡翠湖集合点在景区东门。",
            chunks=["甲" * 1700 + "翡翠湖集合点在景区东门。"],
        )

        context = self.store.build_document_context(
            "group",
            "翡翠湖集合点在哪里？",
        )

        self.assertIn("翡翠湖集合点在景区东门", context)

    def test_two_character_document_search_is_group_scoped(self):
        self.store.add_document(
            "group-a",
            "member",
            "西宁安排.md",
            "two-char-a",
            "西宁住宿安排在城西区。",
            ["西宁住宿安排在城西区。"],
        )
        self.store.add_document(
            "group-b",
            "member",
            "其他群秘密.md",
            "two-char-b",
            "西宁住宿安排在秘密地点。",
            ["西宁住宿安排在秘密地点。"],
        )

        context = self.store.build_document_context("group-a", "西宁")

        self.assertIn("西宁住宿安排在城西区", context)
        self.assertNotIn("秘密地点", context)

    def test_fts_hit_does_not_also_run_substring_full_scan(self):
        database_path = Path(self.temp_dir.name) / "tracing.db"
        store = TracingMemoryStore(database_path)
        store.add_document(
            "group",
            "member",
            "青海湖.md",
            "fts-no-fallback",
            "青海湖二郎剑景区集合。",
            ["青海湖二郎剑景区早上九点集合。"],
        )
        if not store._document_fts_available:
            self.skipTest("SQLite build does not provide FTS5 trigram")

        store.statements.clear()
        context = store.build_document_context("group", "青海湖集合")
        statements = "\n".join(store.statements).lower()

        self.assertIn("青海湖二郎剑景区早上九点集合", context)
        self.assertIn("document_chunks_fts match", statements)
        self.assertNotIn("instr(lower(c.content)", statements)

    def test_existing_document_chunks_are_backfilled_into_search_index(self):
        database_path = Path(self.temp_dir.name) / "legacy-documents.db"
        with sqlite_connection(database_path) as connection:
            connection.executescript(
                """
                CREATE TABLE documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_openid TEXT NOT NULL,
                    uploader_openid TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    preview TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    text_length INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(group_openid, sha256)
                );
                CREATE TABLE document_chunks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    document_id INTEGER NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    FOREIGN KEY(document_id) REFERENCES documents(id)
                        ON DELETE CASCADE,
                    UNIQUE(document_id, chunk_index)
                );
                INSERT INTO documents (
                    group_openid, uploader_openid, filename, sha256,
                    preview, summary, text_length, created_at
                ) VALUES (
                    'group', 'member', '旧行程.md', 'legacy-document',
                    '青海湖二郎剑集合', '', 8, '2026-07-15T08:00:00+00:00'
                );
                INSERT INTO document_chunks (document_id, chunk_index, content)
                VALUES (1, 0, '青海湖二郎剑景区早上九点集合。');
                """
            )

        migrated_store = MemoryStore(database_path)
        context = migrated_store.build_document_context(
            "group",
            "青海湖二郎剑几点集合？",
        )

        self.assertIn("青海湖二郎剑景区早上九点集合", context)
        with sqlite_connection(database_path) as connection:
            migration = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE name = ?",
                ("document_chunks_fts_v1",),
            ).fetchone()
        if migrated_store._document_fts_available:
            self.assertIsNotNone(migration)

    def test_lists_document_contents_newest_first_with_ordered_chunks(self):
        self.store.add_document(
            "group-a",
            "member",
            "older.md",
            "older-hash",
            "older first\nolder second",
            ["older first", "older second"],
        )
        self.store.add_document(
            "group-a",
            "member",
            "newer.md",
            "newer-hash",
            "newer only",
            ["newer only"],
        )
        self.store.add_document(
            "group-b",
            "member",
            "secret.md",
            "secret-hash",
            "other group",
            ["other group"],
        )

        documents = self.store.list_document_contents("group-a")

        self.assertEqual(
            tuple(document.filename for document in documents),
            ("newer.md", "older.md"),
        )
        self.assertEqual(documents[0].chunks, ("newer only",))
        self.assertEqual(
            documents[1].chunks,
            ("older first", "older second"),
        )

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

    def test_competing_document_events_consume_binding_once(self):
        now = datetime(2026, 7, 19, 8, 0, tzinfo=timezone.utc)
        self.store.create_upload_binding(
            "document-race-hash",
            "group-a",
            "member-a",
            now + timedelta(minutes=10),
            now=now,
        )
        redemption = self.store.redeem_upload_binding(
            "document-race-hash",
            "private-user",
            now=now,
        )
        first_claim = self.store.begin_event("private-event-1", now=now)
        second_claim = self.store.begin_event("private-event-2", now=now)
        document = PreparedDocument(
            filename="plan.txt",
            sha256="same-document",
            full_text="8月16日从西宁前往青海湖，晚上住宿茶卡镇。",
            chunks=("8月16日从西宁前往青海湖，晚上住宿茶卡镇。",),
            summary="青甘自驾行程",
        )

        def commit(event_id, claim_token):
            try:
                self.store.commit_private_document_event(
                    event_id=event_id,
                    claim_token=claim_token,
                    platform="qq_official",
                    binding_id=redemption.binding.binding_id,
                    group_openid="group-a",
                    uploader_openid="c2c:private-user",
                    document=document,
                    reply="已保存旅行文档：plan.txt",
                    target_user_openid="private-user",
                    reply_to_id=event_id,
                    now=now,
                )
                return "committed"
            except RuntimeError:
                return "rejected"

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = sorted(executor.map(
                lambda args: commit(*args),
                (
                    (first_claim.event_id, first_claim.claim_token),
                    (second_claim.event_id, second_claim.claim_token),
                ),
            ))

        self.assertEqual(results, ["committed", "rejected"])
        outbox_count = sum(
            len(self.store.list_outbox_for_event(event_id))
            for event_id in ("private-event-1", "private-event-2")
        )
        self.assertEqual(outbox_count, 1)


if __name__ == "__main__":
    unittest.main()
