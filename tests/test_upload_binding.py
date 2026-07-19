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

    def test_private_code_redeems_target_group(self):
        self.service.issue_binding("group-a", "member-a")

        result = self.service.handle_private_message(
            "private-user",
            "绑定 QG-ABC234",
            [],
        )

        self.assertIn("绑定成功", result.reply)
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
        self.assertIsNone(
            self.store.get_pending_upload_binding(
                "private-user",
                now=self.now,
            )
        )

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


if __name__ == "__main__":
    unittest.main()
