import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import (
    QQOfficialReplyRenderer,
    QQOfficialTransport,
    TravelRiskBot,
)
from bot_application import TravelBotApplication
from chat_transport import ChatEvent, OutgoingMessage
from document_service import DocumentIngestResult
from memory_store import MemoryStore
from outbox_worker import OutboxWorker
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


class QQOfficialTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_official_active_group_message_omits_msg_id(self):
        api = FakeApi()
        transport = QQOfficialTransport(api)

        await transport.send(OutgoingMessage(
            channel="group",
            target_id="group-a",
            reply_to_id="",
            payload={"msg_type": 0, "content": "主动提醒"},
        ))

        self.assertEqual(len(api.group_messages), 1)
        self.assertNotIn("msg_id", api.group_messages[0])

    async def test_official_passive_reply_keeps_msg_id(self):
        api = FakeApi()
        transport = QQOfficialTransport(api)

        await transport.send(OutgoingMessage(
            channel="group",
            target_id="group-a",
            reply_to_id="message-1",
            payload={"msg_type": 0, "content": "被动回复"},
        ))

        self.assertEqual(api.group_messages[0]["msg_id"], "message-1")


class QQOfficialReplyRendererTests(unittest.TestCase):
    def test_official_reminder_renderer_mentions_recipient(self):
        payload = QQOfficialReplyRenderer().render_reminder(
            "member-a",
            "景点预约提醒：青海湖",
        )

        self.assertEqual(payload["msg_type"], 0)
        self.assertIn("member-a", payload["content"])
        self.assertIn("景点预约提醒：青海湖", payload["content"])


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

    def issue_binding(self, group_openid, member_openid, **kwargs):
        self.issue_calls.append((group_openid, member_openid, kwargs))
        return "一次性绑定码：QG-ABC234"

    def handle_private_message(
            self,
            user_openid,
            content,
            attachments,
            **kwargs):
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
        self.bot.reminder_scheduler = SimpleNamespace(
            scan_once=AsyncMock(return_value=0),
            run=AsyncMock(),
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def use_api(self, api):
        self.bot.reply_renderer = QQOfficialReplyRenderer()
        self.bot.outbox_worker = OutboxWorker(
            "qq_official",
            self.bot.memory_store,
            QQOfficialTransport(api),
        )
        self.bot.application = TravelBotApplication(
            store=self.bot.memory_store,
            travel_service=self.bot.travel_service,
            travel_agent=self.bot.travel_agent,
            document_service=self.bot.document_service,
            upload_binding_service=self.bot.upload_binding_service,
            outbox_worker=self.bot.outbox_worker,
            reply_renderer=self.bot.reply_renderer,
            reminder_scheduler=self.bot.reminder_scheduler,
            group_allowed=self.bot.settings.allows_group,
        )
        return api

    @staticmethod
    def group_event_key(message):
        return f"qq_official:group:{message.group_openid}:{message.id}"

    @staticmethod
    def private_event_key(message):
        return f"qq_official:private:private-user:{message.id}"

    async def test_group_callback_normalizes_botpy_event(self):
        application = SimpleNamespace(handle=AsyncMock())
        self.bot.application = application
        attachment = SimpleNamespace(
            filename="plan.txt",
            url="https://example.test/plan.txt",
            content_type="text/plain",
            size=1234,
        )
        message = SimpleNamespace(
            group_openid="group-a",
            id="group-normalized",
            content="查询天气 西宁",
            attachments=[attachment],
            author=SimpleNamespace(member_openid="member-a"),
        )

        await self.bot.on_group_at_message_create(message)

        event = application.handle.await_args.args[0]
        self.assertIsInstance(event, ChatEvent)
        self.assertEqual(event.scope_id, "group-a")
        self.assertEqual(event.sender_id, "member-a")
        self.assertEqual(event.attachments[0].filename, "plan.txt")
        self.assertEqual(event.attachments[0].size, 1234)

    async def test_on_ready_logs_build_revision(self):
        self.bot._connection = SimpleNamespace(
            state=SimpleNamespace(robot=SimpleNamespace(name="travel-bot"))
        )
        order = []
        worker = SimpleNamespace(
            dispatch_due_once=AsyncMock(
                side_effect=lambda: order.append("dispatch")
            ),
            run=AsyncMock(),
        )
        self.bot.outbox_worker = worker
        self.bot.reminder_scheduler = SimpleNamespace(
            scan_once=AsyncMock(side_effect=lambda: order.append("scan")),
            run=AsyncMock(),
        )

        with patch.dict(
            os.environ,
            {"APP_GIT_REF": "main", "APP_GIT_SHA": "f6f0617abcdef"},
        ):
            with patch("bot.logger.info") as info, patch(
                "bot.asyncio.create_task"
            ) as create_task:
                create_task.side_effect = (
                    lambda coroutine, **kwargs: coroutine.close()
                )
                await self.bot.on_ready()

        messages = "\n".join(str(call) for call in info.call_args_list)
        self.assertIn("main", messages)
        self.assertIn("f6f0617abcde", messages)
        self.assertEqual(order, ["scan", "dispatch"])
        worker.dispatch_due_once.assert_awaited_once_with()

    async def test_repeated_on_ready_does_not_duplicate_background_tasks(self):
        self.bot._connection = SimpleNamespace(
            state=SimpleNamespace(robot=SimpleNamespace(name="travel-bot"))
        )
        self.bot.outbox_worker = SimpleNamespace(
            dispatch_due_once=AsyncMock(return_value=0),
            run=AsyncMock(),
        )
        self.bot.reminder_scheduler = SimpleNamespace(
            scan_once=AsyncMock(return_value=0),
            run=AsyncMock(),
        )
        running_task = SimpleNamespace(done=lambda: False)

        with patch("bot.asyncio.create_task") as create_task:
            def keep_running(coroutine, **kwargs):
                coroutine.close()
                return running_task

            create_task.side_effect = keep_running
            await self.bot.on_ready()
            await self.bot.on_ready()

        self.assertEqual(create_task.call_count, 2)
        self.bot.reminder_scheduler.scan_once.assert_awaited()
        self.bot.outbox_worker.dispatch_due_once.assert_awaited()

    async def test_on_ready_drains_a_restored_pending_reply(self):
        api = self.use_api(FakeApi())
        self.bot._connection = SimpleNamespace(
            state=SimpleNamespace(robot=SimpleNamespace(name="travel-bot"))
        )
        claim = self.bot.memory_store.begin_event("restored-event")
        self.bot.memory_store.prepare_event_outbox(
            event_id=claim.event_id,
            claim_token=claim.claim_token,
            platform="qq_official",
            channel="private",
            target_id="private-user",
            sender_id="private-user",
            reply_to_id="private-message",
            payload={"msg_type": 0, "content": "restored reply"},
            memory_content=None,
        )

        with patch("bot.asyncio.create_task") as create_task:
            create_task.side_effect = (
                lambda coroutine, **kwargs: coroutine.close()
            )
            await self.bot.on_ready()

        self.assertEqual(api.private_messages[0]["content"], "restored reply")
        self.assertEqual(
            self.bot.memory_store.get_event_status("restored-event"),
            "completed",
        )

    async def test_group_upload_command_issues_binding_code(self):
        api = self.use_api(FakeApi())
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
            self.bot.upload_binding_service.issue_calls[0][:2],
            ("group-a", "member-a"),
        )
        self.assertIn("QG-ABC234", api.group_messages[0]["content"])
        self.assertEqual(api.group_messages[0]["msg_type"], 0)
        self.assertEqual(
            len(self.bot.memory_store.list_outbox_for_event(
                self.group_event_key(message)
            )),
            1,
        )

    async def test_group_help_uses_markdown_command_panel(self):
        api = self.use_api(FakeApi())
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
        api = self.use_api(FakeApi())
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
        turns = self.bot.memory_store.get_recent_turns(
            "group-a",
            "member-a",
        )
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0].assistant_content, "unexpected travel reply")

    async def test_failed_group_send_retries_prepared_rich_reply(self):
        api = self.use_api(FakeApi(group_failures=1))
        message = SimpleNamespace(
            group_openid="group-a",
            id="group-message-rich-retry",
            content="/菜单",
            attachments=[],
            author=SimpleNamespace(member_openid="member-a"),
            _api=api,
        )

        await self.bot.on_group_at_message_create(message)

        self.assertEqual(
            self.bot.memory_store.get_event_status(
                self.group_event_key(message)
            ),
            "processing",
        )
        retry_time = datetime.now(timezone.utc) + timedelta(seconds=6)
        await self.bot.outbox_worker.dispatch_due_once(now=retry_time)

        self.assertEqual(self.bot.travel_service.handle_calls, ["/菜单"])
        self.assertEqual(len(api.group_messages), 1)
        sent = api.group_messages[0]
        self.assertEqual(sent["msg_type"], 2)
        self.assertIn("markdown", sent)
        self.assertIn("keyboard", sent)
        self.assertEqual(
            self.bot.memory_store.get_event_status(
                self.group_event_key(message)
            ),
            "completed",
        )

    async def test_c2c_file_event_is_sent_to_private_upload_workflow(self):
        api = self.use_api(FakeApi())
        attachment = object()
        message = SimpleNamespace(
            id="private-message-1",
            content=None,
            attachments=[attachment],
            author=SimpleNamespace(user_openid="private-user"),
            _api=api,
        )

        await self.bot.on_c2c_message_create(message)

        private_call = self.bot.upload_binding_service.private_calls[0]
        self.assertEqual(private_call[:2], ("private-user", ""))
        self.assertEqual(len(private_call[2]), 1)
        self.assertEqual(
            api.private_messages[0]["openid"],
            "private-user",
        )
        self.assertIn(
            "已保存旅行文档",
            api.private_messages[0]["content"],
        )

    async def test_duplicate_c2c_event_is_ignored(self):
        api = self.use_api(FakeApi())
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
        api = self.use_api(FakeApi(private_failures=1))
        attachment = object()
        message = SimpleNamespace(
            id="private-message-retry",
            content=None,
            attachments=[attachment],
            author=SimpleNamespace(user_openid="private-user"),
            _api=api,
        )

        await self.bot.on_c2c_message_create(message)

        self.assertEqual(
            self.bot.memory_store.get_event_status(
                self.private_event_key(message)
            ),
            "processing",
        )

        retry_time = datetime.now(timezone.utc) + timedelta(seconds=6)
        await self.bot.outbox_worker.dispatch_due_once(now=retry_time)

        self.assertEqual(len(self.bot.upload_binding_service.private_calls), 1)
        self.assertEqual(len(api.private_messages), 1)
        self.assertEqual(
            self.bot.memory_store.get_event_status(
                self.private_event_key(message)
            ),
            "completed",
        )

    async def test_group_reply_is_persisted_before_transport_send(self):
        api = self.use_api(FakeApi())
        message = SimpleNamespace(
            group_openid="group-a",
            id="group-message-storage-retry",
            content="上传文档",
            attachments=[],
            author=SimpleNamespace(member_openid="member-a"),
            _api=api,
        )
        original_send = self.bot.outbox_worker.transport.send

        async def assert_persisted_before_send(outgoing):
            rows = self.bot.memory_store.list_outbox_for_event(
                self.group_event_key(message)
            )
            self.assertEqual(len(rows), 1)
            await original_send(outgoing)

        with patch.object(
                self.bot.outbox_worker.transport,
                "send",
                side_effect=assert_persisted_before_send):
            await self.bot.on_group_at_message_create(message)

        self.assertEqual(len(self.bot.upload_binding_service.issue_calls), 1)
        self.assertEqual(len(api.group_messages), 1)
        self.assertEqual(
            self.bot.memory_store.get_event_status(
                self.group_event_key(message)
            ),
            "completed",
        )


if __name__ == "__main__":
    unittest.main()
