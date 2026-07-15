import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from document_service import DocumentIngestResult
from memory_store import MemoryStore
from upload_binding import UploadBindingService


class FakeDocumentService:
    def __init__(self, result=None):
        self.result = result or DocumentIngestResult(
            handled=True,
            reply="已保存旅行文档：plan.docx",
            memory_content="上传旅行文档：plan.docx",
        )
        self.calls = []

    def ingest_attachments(
            self,
            group_openid,
            member_openid,
            attachments):
        self.calls.append((group_openid, member_openid, attachments))
        return self.result


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
        attachment = object()

        first = self.service.handle_private_message(
            "private-user",
            "",
            [attachment],
        )
        second = self.service.handle_private_message(
            "private-user",
            "",
            [attachment],
        )

        self.assertIn("已保存旅行文档", first.reply)
        self.assertIn("本次绑定已失效", first.reply)
        self.assertIn("没有有效的群绑定", second.reply)
        self.assertEqual(
            self.documents.calls,
            [("group-a", "c2c:private-user", [attachment])],
        )

    def test_unsupported_attachment_keeps_binding_for_retry(self):
        self.documents.result = DocumentIngestResult(handled=False)
        self.service.issue_binding("group-a", "member-a")
        self.service.handle_private_message(
            "private-user",
            "QG-ABC234",
            [],
        )

        result = self.service.handle_private_message(
            "private-user",
            "",
            [object()],
        )

        self.assertIn("不支持", result.reply)
        self.assertIsNotNone(
            self.store.get_pending_upload_binding(
                "private-user",
                now=self.now,
            )
        )


if __name__ == "__main__":
    unittest.main()
