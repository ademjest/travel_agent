import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bot import TravelRiskBot
from document_service import DocumentIngestResult
from memory_store import MemoryStore
from upload_binding import PrivateUploadResult


class FakeSettings:
    def allows_group(self, group_openid):
        return group_openid == "group-a"


class FakeApi:
    def __init__(self, group_failures=0, private_failures=0):
        self.group_messages = []
        self.private_messages = []
        self.group_failures = group_failures
        self.private_failures = private_failures

    async def post_group_message(self, **kwargs):
        if self.group_failures:
            self.group_failures -= 1
            raise RuntimeError("group send failed")
        self.group_messages.append(kwargs)

    async def post_c2c_message(self, **kwargs):
        if self.private_failures:
            self.private_failures -= 1
            raise RuntimeError("private send failed")
        self.private_messages.append(kwargs)


class FakeDocumentService:
    def ingest_attachments(self, group_openid, member_openid, attachments):
        return DocumentIngestResult(handled=False)


class FakeTravelService:
    def __init__(self):
        self.handle_calls = []

    def handle(self, content):
        self.handle_calls.append(content)
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

    async def test_on_ready_logs_build_revision(self):
        self.bot._connection = SimpleNamespace(
            state=SimpleNamespace(robot=SimpleNamespace(name="travel-bot"))
        )

        with patch.dict(
            os.environ,
            {"APP_GIT_REF": "main", "APP_GIT_SHA": "f6f0617abcdef"},
        ):
            with patch("bot.logger.info") as info:
                await self.bot.on_ready()

        messages = "\n".join(str(call) for call in info.call_args_list)
        self.assertIn("main", messages)
        self.assertIn("f6f0617abcde", messages)

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
        self.assertEqual(api.group_messages[0]["msg_type"], 0)

    async def test_group_help_uses_markdown_command_panel(self):
        api = FakeApi()
        message = SimpleNamespace(
            group_openid="group-a",
            id="group-message-help",
            content="/菜单",
            attachments=[],
            author=SimpleNamespace(member_openid="member-a"),
            _api=api,
        )

        await self.bot.on_group_at_message_create(message)

        sent = api.group_messages[0]
        self.assertEqual(sent["msg_type"], 2)
        self.assertIn("青甘自驾助手", sent["markdown"]["content"])
        self.assertIn("keyboard", sent)
        self.assertNotIn("content", sent)

    async def test_ordinary_group_reply_remains_plain_text(self):
        api = FakeApi()
        message = SimpleNamespace(
            group_openid="group-a",
            id="group-message-plain",
            content="随便聊聊",
            attachments=[],
            author=SimpleNamespace(member_openid="member-a"),
            _api=api,
        )

        await self.bot.on_group_at_message_create(message)

        sent = api.group_messages[0]
        self.assertEqual(sent["msg_type"], 0)
        self.assertEqual(sent["content"], "unexpected travel reply")
        self.assertNotIn("markdown", sent)
        self.assertNotIn("keyboard", sent)

    async def test_failed_group_send_retries_prepared_rich_reply(self):
        api = FakeApi(group_failures=1)
        message = SimpleNamespace(
            group_openid="group-a",
            id="group-message-rich-retry",
            content="/菜单",
            attachments=[],
            author=SimpleNamespace(member_openid="member-a"),
            _api=api,
        )

        with self.assertRaisesRegex(RuntimeError, "group send failed"):
            await self.bot.on_group_at_message_create(message)

        self.assertEqual(
            self.bot.memory_store.get_event_status(message.id),
            "failed",
        )
        await self.bot.on_group_at_message_create(message)

        self.assertEqual(self.bot.travel_service.handle_calls, ["/菜单"])
        self.assertEqual(len(api.group_messages), 1)
        sent = api.group_messages[0]
        self.assertEqual(sent["msg_type"], 2)
        self.assertIn("markdown", sent)
        self.assertIn("keyboard", sent)
        self.assertEqual(
            self.bot.memory_store.get_event_status(message.id),
            "completed",
        )

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

    async def test_failed_private_send_retries_prepared_reply(self):
        api = FakeApi(private_failures=1)
        attachment = object()
        message = SimpleNamespace(
            id="private-message-retry",
            content=None,
            attachments=[attachment],
            author=SimpleNamespace(user_openid="private-user"),
            _api=api,
        )

        with self.assertRaisesRegex(RuntimeError, "private send failed"):
            await self.bot.on_c2c_message_create(message)

        self.assertEqual(
            self.bot.memory_store.get_event_status(message.id),
            "failed",
        )

        await self.bot.on_c2c_message_create(message)

        self.assertEqual(len(self.bot.upload_binding_service.private_calls), 1)
        self.assertEqual(len(api.private_messages), 1)
        self.assertEqual(
            self.bot.memory_store.get_event_status(message.id),
            "completed",
        )

    async def test_group_storage_failure_marks_event_failed_and_retries(self):
        api = FakeApi()
        message = SimpleNamespace(
            group_openid="group-a",
            id="group-message-storage-retry",
            content="上传文档",
            attachments=[],
            author=SimpleNamespace(member_openid="member-a"),
            _api=api,
        )
        original_save_turn = self.bot.memory_store.save_turn
        save_attempts = 0

        def flaky_save_turn(*args):
            nonlocal save_attempts
            save_attempts += 1
            if save_attempts == 1:
                raise RuntimeError("turn storage failed")
            return original_save_turn(*args)

        with patch.object(
                self.bot.memory_store,
                "save_turn",
                side_effect=flaky_save_turn):
            with self.assertRaisesRegex(RuntimeError, "turn storage failed"):
                await self.bot.on_group_at_message_create(message)

            self.assertEqual(
                self.bot.memory_store.get_event_status(message.id),
                "failed",
            )
            await self.bot.on_group_at_message_create(message)

        self.assertEqual(len(self.bot.upload_binding_service.issue_calls), 1)
        self.assertEqual(len(api.group_messages), 1)
        self.assertEqual(
            self.bot.memory_store.get_event_status(message.id),
            "completed",
        )


if __name__ == "__main__":
    unittest.main()
