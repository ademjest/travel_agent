import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from bot import TravelRiskBot
from document_service import DocumentIngestResult
from memory_store import MemoryStore
from upload_binding import PrivateUploadResult


class FakeSettings:
    def allows_group(self, group_openid):
        return group_openid == "group-a"


class FakeApi:
    def __init__(self):
        self.group_messages = []
        self.private_messages = []

    async def post_group_message(self, **kwargs):
        self.group_messages.append(kwargs)

    async def post_c2c_message(self, **kwargs):
        self.private_messages.append(kwargs)


class FakeDocumentService:
    def ingest_attachments(self, group_openid, member_openid, attachments):
        return DocumentIngestResult(handled=False)


class FakeTravelService:
    def handle(self, content):
        return "unexpected travel reply"


class FakeUploadBindingService:
    def __init__(self):
        self.issue_calls = []
        self.private_calls = []

    def issue_binding(self, group_openid, member_openid):
        self.issue_calls.append((group_openid, member_openid))
        return "一次性绑定码：QG-ABC234"

    def handle_private_message(self, user_openid, content, attachments):
        self.private_calls.append((user_openid, content, attachments))
        return PrivateUploadResult(
            reply="已保存旅行文档：plan.docx\n本次绑定已失效。",
            group_openid="group-a",
        )


class BotUploadEventTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        database_path = Path(self.temp_dir.name) / "memory.db"
        self.bot = TravelRiskBot.__new__(TravelRiskBot)
        self.bot.settings = FakeSettings()
        self.bot.memory_store = MemoryStore(database_path)
        self.bot.document_service = FakeDocumentService()
        self.bot.travel_service = FakeTravelService()
        self.bot.travel_agent = None
        self.bot.upload_binding_service = FakeUploadBindingService()

    def tearDown(self):
        self.temp_dir.cleanup()

    async def test_group_upload_command_issues_binding_code(self):
        api = FakeApi()
        message = SimpleNamespace(
            group_openid="group-a",
            id="group-message-1",
            content="上传文档",
            attachments=[],
            author=SimpleNamespace(member_openid="member-a"),
            _api=api,
        )

        await self.bot.on_group_at_message_create(message)

        self.assertEqual(
            self.bot.upload_binding_service.issue_calls,
            [("group-a", "member-a")],
        )
        self.assertIn("QG-ABC234", api.group_messages[0]["content"])

    async def test_c2c_file_event_is_sent_to_private_upload_workflow(self):
        api = FakeApi()
        attachment = object()
        message = SimpleNamespace(
            id="private-message-1",
            content=None,
            attachments=[attachment],
            author=SimpleNamespace(user_openid="private-user"),
            _api=api,
        )

        await self.bot.on_c2c_message_create(message)

        self.assertEqual(
            self.bot.upload_binding_service.private_calls,
            [("private-user", "", [attachment])],
        )
        self.assertEqual(
            api.private_messages[0]["openid"],
            "private-user",
        )
        self.assertIn(
            "已保存旅行文档",
            api.private_messages[0]["content"],
        )

    async def test_duplicate_c2c_event_is_ignored(self):
        api = FakeApi()
        attachment = object()
        message = SimpleNamespace(
            id="private-message-duplicate",
            content=None,
            attachments=[attachment],
            author=SimpleNamespace(user_openid="private-user"),
            _api=api,
        )

        await self.bot.on_c2c_message_create(message)
        await self.bot.on_c2c_message_create(message)

        self.assertEqual(len(self.bot.upload_binding_service.private_calls), 1)
        self.assertEqual(len(api.private_messages), 1)


if __name__ == "__main__":
    unittest.main()
