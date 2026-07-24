import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from document_service import PreparedDocument
from memory_store import MemoryStore
from upload_binding import UploadBindingService


class FakeDocumentService:
    def __init__(self, prepared=None):
        self.prepared = prepared or (
            PreparedDocument(
                filename="plan.docx",
                sha256="document-hash",
                full_text="8月16日从西宁前往青海湖，晚上住宿茶卡镇。",
                chunks=("8月16日从西宁前往青海湖，晚上住宿茶卡镇。",),
                summary="青甘自驾行程",
            ),
        )
        self.calls = []

    def prepare_attachments(self, attachments):
        self.calls.append(attachments)
        return self.prepared


class UploadBindingServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        database_path = Path(self.temp_dir.name) / "memory.db"
        self.store = MemoryStore(database_path)
        self.documents = FakeDocumentService()
        self.now = datetime(2026, 7, 15, 8, 0, tzinfo=timezone.utc)
        self.service = UploadBindingService(
            self.store,
            self.documents,
            code_factory=lambda: "QG-ABC234",
            now_provider=lambda: self.now,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def handle_attachment(self, event_id, attachments):
        claim = self.store.begin_event(event_id, now=self.now)
        return self.service.handle_private_message(
            "private-user",
            "",
            attachments,
            event_id=event_id,
            claim_token=claim.claim_token,
            platform="qq_official",
            reply_to_id=event_id,
        )

    def test_issue_binding_returns_private_upload_instructions(self):
        reply = self.service.issue_binding("group-a", "member-a")

        self.assertIn("QG-ABC234", reply)
        self.assertIn("10 分钟", reply)
        self.assertIn("私聊", reply)
        self.assertIn(".xlsx", reply)

    def test_event_replay_reuses_original_binding_code(self):
        codes = iter(("QG-ABC234", "QG-DEF567"))
        service = UploadBindingService(
            self.store,
            self.documents,
            code_factory=lambda: next(codes),
            now_provider=lambda: self.now,
        )
        first_claim = self.store.begin_event("binding-event", now=self.now)
        first = service.issue_binding(
            "group-a",
            "member-a",
            event_id=first_claim.event_id,
            claim_token=first_claim.claim_token,
        )
        self.store.fail_event(
            first_claim.event_id,
            first_claim.claim_token,
            "simulated crash",
            now=self.now,
        )
        second_claim = self.store.begin_event("binding-event", now=self.now)

        second = service.issue_binding(
            "group-a",
            "member-a",
            event_id=second_claim.event_id,
            claim_token=second_claim.claim_token,
        )

        self.assertEqual(second, first)
        self.assertIn("QG-ABC234", second)
        self.assertNotIn("QG-DEF567", second)
        with self.store._connect() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM upload_bindings "
                "WHERE source_event_id = ?",
                ("binding-event",),
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_private_code_redeems_target_group(self):
        self.service.issue_binding("group-a", "member-a")

        result = self.service.handle_private_message(
            "private-user",
            "绑定 QG-ABC234",
            [],
        )

        self.assertIn("绑定成功", result.reply)
        self.assertIn(".xlsx", result.reply)
        self.assertEqual(result.group_openid, "group-a")

    def test_private_upload_without_binding_is_rejected(self):
        result = self.service.handle_private_message(
            "private-user",
            "",
            [object()],
        )

        self.assertIn("没有有效的群绑定", result.reply)
        self.assertEqual(self.documents.calls, [])

    def test_private_attachment_is_ingested_into_bound_group_once(self):
        self.service.issue_binding("group-a", "member-a")
        self.service.handle_private_message(
            "private-user",
            "QG-ABC234",
            [],
        )
        attachment = SimpleNamespace(filename="plan.docx")

        first = self.handle_attachment("private-event-1", [attachment])
        second = self.handle_attachment("private-event-2", [attachment])

        self.assertIn("已保存旅行文档", first.reply)
        self.assertIn("本次绑定已失效", first.reply)
        self.assertIn("没有有效的群绑定", second.reply)
        self.assertEqual(
            self.documents.calls,
            [[attachment]],
        )
        self.assertIsNotNone(first.outbox_id)

    def test_unsupported_attachment_consumes_binding(self):
        self.documents.prepared = ()
        self.service.issue_binding("group-a", "member-a")
        self.service.handle_private_message(
            "private-user",
            "QG-ABC234",
            [],
        )

        result = self.handle_attachment(
            "private-event-unsupported",
            [SimpleNamespace(filename="photo.jpg")],
        )

        self.assertIn("不支持", result.reply)
        self.assertIn("本次绑定已失效", result.reply)
        self.assertIsNotNone(result.outbox_id)
        self.assertIsNone(
            self.store.get_pending_upload_binding(
                "private-user",
                now=self.now,
            )
        )

    def test_multiple_private_attachments_are_rejected_before_download(self):
        self.service.issue_binding("group-a", "member-a")
        self.service.handle_private_message(
            "private-user", "QG-ABC234", []
        )

        result = self.handle_attachment(
            "private-event-multiple",
            [
                SimpleNamespace(filename="one.docx"),
                SimpleNamespace(filename="two.docx"),
            ],
        )

        self.assertIn("一次只能上传一个", result.reply)
        self.assertEqual(self.documents.calls, [])

    def test_legacy_xls_private_upload_requests_xlsx_conversion(self):
        self.documents.prepared = ()
        self.service.issue_binding("group-a", "member-a")
        self.service.handle_private_message(
            "private-user",
            "QG-ABC234",
            [],
        )

        result = self.handle_attachment(
            "private-event-old-xls",
            [SimpleNamespace(filename="old-plan.xls")],
        )

        self.assertIn("另存为 .xlsx", result.reply)
        self.assertIn("本次绑定已失效", result.reply)

    def test_supported_attachment_keeps_binding_until_commit(self):
        class InspectingDocumentService(FakeDocumentService):
            def prepare_attachments(inner_self, attachments):
                inner_self.pending_during_ingest = (
                    self.store.get_pending_upload_binding(
                        "private-user",
                        now=self.now,
                    )
                )
                return super().prepare_attachments(attachments)

        documents = InspectingDocumentService()
        service = UploadBindingService(
            self.store,
            documents,
            code_factory=lambda: "QG-XYZ234",
            now_provider=lambda: self.now,
        )
        service.issue_binding("group-a", "member-a")
        service.handle_private_message("private-user", "QG-XYZ234", [])

        claim = self.store.begin_event("private-event-inspect", now=self.now)
        service.handle_private_message(
            "private-user",
            "",
            [SimpleNamespace(filename="plan.docx")],
            event_id=claim.event_id,
            claim_token=claim.claim_token,
            platform="qq_official",
            reply_to_id=claim.event_id,
        )

        self.assertIsNotNone(documents.pending_during_ingest)
        self.assertIsNone(
            self.store.get_pending_upload_binding(
                "private-user",
                now=self.now,
            )
        )

    def test_replayed_unsupported_attachment_reuses_original_reply(self):
        self.documents.prepared = ()
        self.service.issue_binding("group-a", "member-a")
        self.service.handle_private_message(
            "private-user",
            "QG-ABC234",
            [],
        )
        attachment = SimpleNamespace(filename="photo.jpg")
        first_claim = self.store.begin_event(
            "private-unsupported-replay",
            now=self.now,
        )
        first = self.service.handle_private_message(
            "private-user",
            "",
            [attachment],
            event_id=first_claim.event_id,
            claim_token=first_claim.claim_token,
            platform="qq_official",
            reply_to_id=first_claim.event_id,
        )
        self.store.fail_event(
            first_claim.event_id,
            first_claim.claim_token,
            "simulated interruption",
            now=self.now,
        )
        second_claim = self.store.begin_event(
            first_claim.event_id,
            now=self.now,
        )

        replay = self.service.handle_private_message(
            "private-user",
            "",
            [attachment],
            event_id=second_claim.event_id,
            claim_token=second_claim.claim_token,
            platform="qq_official",
            reply_to_id=second_claim.event_id,
        )

        self.assertEqual(replay.reply, first.reply)
        self.assertEqual(replay.outbox_id, first.outbox_id)

    def test_disallowed_redeemed_binding_is_consumed_with_outbox(self):
        service = UploadBindingService(
            self.store,
            self.documents,
            group_allowed=lambda group_id: False,
            code_factory=lambda: "QG-NPQ234",
            now_provider=lambda: self.now,
        )
        service.issue_binding("group-a", "member-a")
        claim = self.store.begin_event("private-disallowed", now=self.now)

        result = service.handle_private_message(
            "private-user",
            "QG-NPQ234",
            [],
            event_id=claim.event_id,
            claim_token=claim.claim_token,
            platform="qq_official",
            reply_to_id=claim.event_id,
        )

        self.assertIn("不在机器人允许列表", result.reply)
        self.assertIsNotNone(result.outbox_id)
        self.assertIsNone(
            self.store.get_pending_upload_binding(
                "private-user",
                now=self.now,
            )
        )

    def test_replayed_document_event_reuses_original_outbox_reply(self):
        self.service.issue_binding("group-a", "member-a")
        self.service.handle_private_message(
            "private-user",
            "QG-ABC234",
            [],
        )
        attachment = SimpleNamespace(filename="plan.docx")
        first_claim = self.store.begin_event("private-event-replay", now=self.now)

        first = self.service.handle_private_message(
            "private-user",
            "",
            [attachment],
            event_id=first_claim.event_id,
            claim_token=first_claim.claim_token,
            platform="qq_official",
            reply_to_id=first_claim.event_id,
        )
        self.store.fail_event(
            first_claim.event_id,
            first_claim.claim_token,
            "simulated interruption",
            now=self.now,
        )
        second_claim = self.store.begin_event(
            "private-event-replay",
            now=self.now,
        )

        replay = self.service.handle_private_message(
            "private-user",
            "",
            [attachment],
            event_id=second_claim.event_id,
            claim_token=second_claim.claim_token,
            platform="qq_official",
            reply_to_id=second_claim.event_id,
        )

        self.assertEqual(replay.reply, first.reply)
        self.assertEqual(replay.outbox_id, first.outbox_id)
        self.assertNotIn("没有有效的群绑定", replay.reply)
        self.assertEqual(self.documents.calls, [[attachment]])

    def test_onebot_document_is_stored_in_platform_scope(self):
        self.service.issue_binding("group-a", "member-a")
        self.service.handle_private_message(
            "private-user",
            "QG-ABC234",
            [],
        )
        claim = self.store.begin_event("onebot-private-document", now=self.now)

        self.service.handle_private_message(
            "private-user",
            "",
            [SimpleNamespace(filename="plan.docx")],
            event_id=claim.event_id,
            claim_token=claim.claim_token,
            platform="onebot",
            reply_to_id=claim.event_id,
        )

        self.assertIn(
            "茶卡镇",
            self.store.build_document_context("onebot:group-a", "住宿"),
        )
        self.assertEqual(
            self.store.build_document_context("group-a", "住宿"),
            "",
        )


if __name__ == "__main__":
    unittest.main()
