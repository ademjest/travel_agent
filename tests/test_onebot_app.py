import asyncio
import os
import tempfile
import unittest
import warnings
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

warnings.filterwarnings(
    "ignore",
    message="Using `httpx` with `starlette.testclient` is deprecated.*",
)
from fastapi.testclient import TestClient

from bot_application import TravelBotApplication
from document_service import DocumentIngestResult
from memory_store import MemoryStore
from onebot_app import (
    OneBotAdapter,
    OneBotReplyRenderer,
    OneBotTransport,
    create_onebot_app,
)
from outbox_worker import OutboxWorker
from settings import OneBotSettings, SettingsError
from upload_binding import PrivateUploadResult


class RecordingTransport:
    def __init__(self):
        self.messages = []

    async def send(self, message):
        self.messages.append(message)

    async def reply_is_from_bot(self, message_id, self_id):
        return message_id == "previous" and self_id == "30001"


class FakeTravelService:
    def handle(self, content):
        return f"reply:{content}"


class FakeDocumentService:
    def ingest_attachments(self, group_id, sender_id, attachments):
        return DocumentIngestResult(handled=False)


class FakeUploadService:
    def issue_binding(self, group_id, sender_id):
        return "binding"

    def handle_private_message(self, *args, **kwargs):
        return PrivateUploadResult(reply="private")


class OneBotAppTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.temp_dir.name) / "memory.db")
        self.transport = RecordingTransport()
        self.settings = OneBotSettings(
            http_url="http://127.0.0.1:3000",
            access_token="outbound-token",
            inbound_token="inbound-token",
            allowed_group_ids=frozenset({"10001"}),
            bind_host="127.0.0.1",
            bind_port=8000,
        )
        worker = OutboxWorker("onebot", self.store, self.transport)
        self.scheduler = SimpleNamespace(
            scan_once=AsyncMock(return_value=0),
            run=AsyncMock(),
        )
        application = TravelBotApplication(
            store=self.store,
            travel_service=FakeTravelService(),
            travel_agent=None,
            document_service=FakeDocumentService(),
            upload_binding_service=FakeUploadService(),
            outbox_worker=worker,
            reply_renderer=OneBotReplyRenderer(),
            reminder_scheduler=self.scheduler,
            group_allowed=self.settings.allows_group,
        )
        app = create_onebot_app(self.settings, application, self.store)
        self.client = TestClient(app)
        self.headers = {"Authorization": "Bearer inbound-token"}

    def tearDown(self):
        self.client.close()
        self.temp_dir.cleanup()

    @staticmethod
    def payload(message_id, message, group_id=10001):
        return {
            "post_type": "message",
            "message_type": "group",
            "message_id": message_id,
            "group_id": group_id,
            "user_id": 20001,
            "self_id": 30001,
            "raw_message": "",
            "message": message,
        }

    def test_missing_tokens_fail_settings_startup(self):
        with patch.dict(os.environ, {
            "ONEBOT_ACCESS_TOKEN": "",
            "ONEBOT_INBOUND_TOKEN": "",
        }, clear=True):
            with self.assertRaises(SettingsError):
                OneBotSettings.from_env()

    def test_disallowed_group_is_rejected(self):
        response = self.client.post(
            "/onebot",
            headers=self.headers,
            json=self.payload(
                1,
                [{"type": "text", "data": {"text": "普通消息"}}],
                group_id=99999,
            ),
        )

        self.assertEqual(response.status_code, 403)

    def test_invalid_inbound_token_is_rejected(self):
        response = self.client.post(
            "/onebot",
            headers={"Authorization": "Bearer wrong-token"},
            json=self.payload(
                10,
                [{"type": "text", "data": {"text": "普通消息"}}],
            ),
        )

        self.assertEqual(response.status_code, 401)

    def test_non_at_message_is_stored_without_invoking_agent(self):
        response = self.client.post(
            "/onebot",
            headers=self.headers,
            json=self.payload(
                2,
                [{"type": "text", "data": {"text": "明早八点集合"}}],
            ),
        )

        self.assertEqual(response.status_code, 200)
        messages = self.store.get_recent_chat_messages("onebot", "10001")
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].content, "明早八点集合")
        self.assertEqual(self.transport.messages, [])

    def test_at_message_invokes_application(self):
        response = self.client.post(
            "/onebot",
            headers=self.headers,
            json=self.payload(3, [
                {"type": "at", "data": {"qq": "30001"}},
                {"type": "text", "data": {"text": " 查询天气 西宁"}},
            ]),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.transport.messages), 1)
        self.assertEqual(
            self.transport.messages[0].payload["message"],
            "reply:查询天气 西宁",
        )

    def test_reply_to_bot_invokes_application(self):
        response = self.client.post(
            "/onebot",
            headers=self.headers,
            json=self.payload(4, [
                {
                    "type": "reply",
                    "data": {"id": "previous"},
                },
                {"type": "text", "data": {"text": "继续分析"}},
            ]),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.transport.messages), 1)

    def test_duplicate_message_creates_one_context_and_outbox(self):
        payload = self.payload(5, [
            {"type": "at", "data": {"qq": "30001"}},
            {"type": "text", "data": {"text": " 状态"}},
        ])

        first = self.client.post("/onebot", headers=self.headers, json=payload)
        second = self.client.post("/onebot", headers=self.headers, json=payload)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        messages = self.store.get_recent_chat_messages("onebot", "10001")
        self.assertEqual(len(messages), 1)
        self.assertEqual(len(self.transport.messages), 1)

    def test_onebot_reminder_renderer_uses_at_segment(self):
        payload = OneBotReplyRenderer().render_reminder(
            "10001",
            "景点预约提醒：青海湖",
        )

        self.assertEqual(payload["message"][0], {
            "type": "at",
            "data": {"qq": "10001"},
        })
        self.assertEqual(payload["message"][1]["type"], "text")

    def test_onebot_image_segment_keeps_declared_size(self):
        attachment = OneBotAdapter._attachments([{
            "type": "image",
            "data": {
                "name": "booking.jpg",
                "url": "https://example.test/booking.jpg",
                "content_type": "image/jpeg",
                "size": 2048,
            },
        }])[0]

        self.assertEqual(attachment.size, 2048)

    def test_lifespan_scans_before_dispatch_and_cancels_both_tasks(self):
        order = []
        stopped = []

        async def scan_once():
            order.append("scan")

        async def dispatch_due_once():
            order.append("dispatch")

        async def run_forever(name):
            try:
                await asyncio.Event().wait()
            finally:
                stopped.append(name)

        transport = SimpleNamespace(aclose=AsyncMock())
        application = SimpleNamespace(
            reminder_scheduler=SimpleNamespace(
                scan_once=scan_once,
                run=lambda: run_forever("reminder"),
            ),
            outbox_worker=SimpleNamespace(
                dispatch_due_once=dispatch_due_once,
                run=lambda: run_forever("outbox"),
                transport=transport,
            ),
        )
        app = create_onebot_app(self.settings, application, self.store)

        with TestClient(app):
            self.assertEqual(order, ["scan", "dispatch"])

        self.assertEqual(set(stopped), {"outbox", "reminder"})
        transport.aclose.assert_awaited_once_with()


class OneBotTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_error_is_failure_before_json_parsing(self):
        async def handler(request):
            return httpx.Response(500, text="upstream failed")

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://onebot.test",
        )
        transport = OneBotTransport(
            "http://onebot.test",
            "token",
            client=client,
        )
        from chat_transport import OutgoingMessage

        with self.assertRaises(httpx.HTTPStatusError):
            await transport.send(OutgoingMessage(
                channel="group",
                target_id="10001",
                reply_to_id="message-1",
                payload={"message": "hello"},
            ))
        await client.aclose()
