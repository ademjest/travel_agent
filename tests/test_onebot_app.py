import asyncio
import hashlib
import hmac
import json
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

from adapters.onebot_app import (
    OneBotAdapter,
    OneBotReplyRenderer,
    OneBotTransport,
    create_onebot_app,
)
from app.bot_application import TravelBotApplication
from core.settings import OneBotSettings, SettingsError
from infrastructure.memory_store import MemoryStore
from services.document_service import DocumentIngestResult
from services.outbox_worker import OutboxWorker
from services.upload_binding import PrivateUploadResult


class RecordingTransport:
    def __init__(self):
        self.messages = []
        self.group_file_url_calls = []
        self.group_file_url = "https://example.test/group-file"
        self.group_file_url_error = None

    async def send(self, message):
        self.messages.append(message)

    async def reply_is_from_bot(self, message_id, self_id):
        return message_id == "previous" and self_id == "30001"

    async def get_group_file_url(self, group_id, file_id):
        self.group_file_url_calls.append((group_id, file_id))
        if self.group_file_url_error is not None:
            raise self.group_file_url_error
        return self.group_file_url


class FakeTravelService:
    def handle(self, content):
        return f"reply:{content}"


class FakeDocumentService:
    def __init__(self):
        self.calls = []
        self.result = DocumentIngestResult(handled=False)

    def ingest_attachments(self, group_id, sender_id, attachments):
        self.calls.append((group_id, sender_id, attachments))
        return self.result


class FakeUploadService:
    def __init__(self):
        self.issue_calls = []

    def issue_binding(self, group_id, sender_id, **kwargs):
        self.issue_calls.append((group_id, sender_id, kwargs))
        return "binding"

    def handle_private_message(self, *args, **kwargs):
        return PrivateUploadResult(reply="private")


class InlineAdapterInbox:
    """These tests isolate adapter/HTTP contracts; test_inbox covers the real queued runtime."""
    def __init__(self, store, adapter):
        self.adapter = adapter

    async def submit(self, payload):
        return await self.adapter.handle(payload)

    async def run(self):
        await asyncio.Event().wait()


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
        self.documents = FakeDocumentService()
        self.uploads = FakeUploadService()
        application = TravelBotApplication(
            store=self.store,
            travel_service=FakeTravelService(),
            travel_agent=None,
            document_service=self.documents,
            upload_binding_service=self.uploads,
            outbox_worker=worker,
            reply_renderer=OneBotReplyRenderer(),
            reminder_scheduler=self.scheduler,
            group_allowed=self.settings.allows_group,
        )
        app = create_onebot_app(self.settings, application, self.store, inbox_factory=InlineAdapterInbox)
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

    def test_valid_napcat_signature_is_accepted(self):
        payload = {
            "post_type": "meta_event",
            "meta_event_type": "heartbeat",
        }
        body = json.dumps(
            payload,
            separators=(",", ":"),
        ).encode("utf-8")
        signature = "sha1=" + hmac.new(
            b"inbound-token",
            body,
            hashlib.sha1,
        ).hexdigest()

        response = self.client.post(
            "/onebot",
            headers={
                "Content-Type": "application/json",
                "X-Signature": signature,
            },
            content=body,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ignored"})

    def test_invalid_napcat_signature_is_rejected(self):
        response = self.client.post(
            "/onebot",
            headers={
                "Content-Type": "application/json",
                "X-Signature": "sha1=invalid",
            },
            content=b'{"post_type":"meta_event"}',
        )

        self.assertEqual(response.status_code, 401)

    def test_missing_required_message_id_is_rejected(self):
        payload = self.payload(
            1,
            [{"type": "text", "data": {"text": "普通消息"}}],
        )
        payload.pop("message_id")

        response = self.client.post(
            "/onebot",
            headers=self.headers,
            json=payload,
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("message_id", response.json()["detail"])

    def test_health_reports_tasks_and_storage_without_tokens(self):
        response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn(payload["status"], {"ok", "degraded"})
        self.assertIn("onebot-outbox", payload["tasks"])
        self.assertIn("outbox", payload["storage"])
        self.assertEqual(payload['semantic_compiler'], {'enabled': False, 'mode': 'disabled'})
        rendered = str(payload)
        self.assertNotIn("outbound-token", rendered)
        self.assertNotIn("inbound-token", rendered)

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

    def test_non_at_fixed_command_invokes_application(self):
        response = self.client.post(
            "/onebot",
            headers=self.headers,
            json=self.payload(
                20,
                [{"type": "text", "data": {"text": "ping"}}],
            ),
        )

        self.assertEqual(response.json(), {"status": "handled"})
        self.assertEqual(len(self.transport.messages), 1)

    def test_help_uses_onebot_specific_text_menu(self):
        response = self.client.post(
            "/onebot",
            headers=self.headers,
            json=self.payload(
                201,
                [{"type": "text", "data": {"text": "帮助"}}],
            ),
        )

        self.assertEqual(response.json(), {"status": "handled"})
        message = self.transport.messages[0].payload["message"]
        self.assertIn("OneBot/NapCat", message)
        self.assertIn("无需 @", message)
        self.assertIn("不需绑定码", message)

    def test_upload_command_prompts_direct_group_upload_without_binding(self):
        response = self.client.post(
            "/onebot",
            headers=self.headers,
            json=self.payload(
                202,
                [{"type": "text", "data": {"text": "上传文档"}}],
            ),
        )

        self.assertEqual(response.json(), {"status": "handled"})
        message = self.transport.messages[0].payload["message"]
        self.assertIn("直接发送到当前群", message)
        self.assertIn("不需一次性绑定码", message)
        self.assertEqual(self.uploads.issue_calls, [])

    def test_non_at_reservation_start_invokes_application(self):
        response = self.client.post(
            "/onebot",
            headers=self.headers,
            json=self.payload(
                21,
                [{"type": "text", "data": {"text": "制定预约"}}],
            ),
        )

        self.assertEqual(response.json(), {"status": "handled"})
        self.assertEqual(len(self.transport.messages), 1)

    def test_non_at_plan_code_edit_invokes_application(self):
        response = self.client.post(
            "/onebot",
            headers=self.headers,
            json=self.payload(211, [{
                "type": "text",
                "data": {
                    "text": (
                        "把 R-20260811-001 的嘉峪关日期补为 2026-08-21，"
                        "水上雅丹不去参观了"
                    ),
                },
            }]),
        )

        self.assertEqual(response.json(), {"status": "handled"})
        self.assertEqual(len(self.transport.messages), 1)

    def test_non_at_xlsx_question_invokes_application(self):
        response = self.client.post(
            "/onebot",
            headers=self.headers,
            json=self.payload(212, [{
                "type": "text",
                "data": {
                    "text": (
                        "根据我上传的青甘大环线自驾行程安排.xlsx，"
                        "列出8月17日的全部行程"
                    ),
                },
            }]),
        )

        self.assertEqual(response.json(), {"status": "handled"})
        self.assertEqual(len(self.transport.messages), 1)

    def test_active_reservation_workflow_accepts_next_image_without_at(self):
        self.store.start_reservation_workflow(
            "onebot",
            "10001",
            "20001",
        )

        response = self.client.post(
            "/onebot",
            headers=self.headers,
            json=self.payload(22, [{
                "type": "image",
                "data": {
                    "name": "booking.jpg",
                    "url": "https://example.test/booking.jpg",
                },
            }]),
        )

        self.assertEqual(response.json(), {"status": "handled"})
        self.assertEqual(len(self.transport.messages), 1)

    def test_explicit_reservation_image_intent_does_not_require_at(self):
        response = self.client.post(
            "/onebot",
            headers=self.headers,
            json=self.payload(23, [
                {
                    "type": "text",
                    "data": {"text": "按这张攻略帮我制定预约"},
                },
                {
                    "type": "image",
                    "data": {
                        "name": "booking.png",
                        "url": "https://example.test/booking.png",
                    },
                },
            ]),
        )

        self.assertEqual(response.json(), {"status": "handled"})
        self.assertEqual(len(self.transport.messages), 1)

    def test_plain_image_without_active_workflow_is_only_observed(self):
        response = self.client.post(
            "/onebot",
            headers=self.headers,
            json=self.payload(24, [{
                "type": "image",
                "data": {
                    "name": "scenery.jpg",
                    "url": "https://example.test/scenery.jpg",
                },
            }]),
        )

        self.assertEqual(response.json(), {"status": "observed"})
        self.assertEqual(self.transport.messages, [])

    def test_unsupported_group_file_is_observed_without_url_lookup(self):
        response = self.client.post(
            "/onebot",
            headers=self.headers,
            json=self.payload(29, [{
                "type": "file",
                "data": {
                    "file_id": "archive-29",
                    "busid": 102,
                    "name": "photos.zip",
                },
            }]),
        )

        self.assertEqual(response.json(), {"status": "observed"})
        self.assertEqual(self.transport.group_file_url_calls, [])
        self.assertEqual(self.transport.messages, [])

    def test_supported_group_document_does_not_require_at(self):
        self.documents.result = DocumentIngestResult(
            handled=True,
            reply="已导入行程",
            memory_content="上传旅行文档 plan.xlsx",
        )

        response = self.client.post(
            "/onebot",
            headers=self.headers,
            json=self.payload(25, [{
                "type": "file",
                "data": {
                    "name": "plan.xlsx",
                    "url": "https://example.test/plan.xlsx",
                },
            }]),
        )

        self.assertEqual(response.json(), {"status": "handled"})
        self.assertEqual(len(self.documents.calls), 1)
        self.assertEqual(len(self.transport.messages), 2)
        self.assertIn(
            "正在下载并解析",
            self.transport.messages[0].payload["message"],
        )
        self.assertEqual(
            self.transport.messages[1].payload["message"],
            "已导入行程",
        )

    def test_group_file_segment_resolves_missing_url(self):
        self.documents.result = DocumentIngestResult(
            handled=True,
            reply="已导入行程",
        )

        response = self.client.post(
            "/onebot",
            headers=self.headers,
            json=self.payload(26, [{
                "type": "file",
                "data": {
                    "file_id": "file-26",
                    "busid": 102,
                    "name": "plan.xlsx",
                    "size": 2048,
                },
            }]),
        )

        self.assertEqual(response.json(), {"status": "handled"})
        self.assertEqual(
            self.transport.group_file_url_calls,
            [("10001", "file-26")],
        )
        attachment = self.documents.calls[0][2][0]
        self.assertEqual(attachment.url, self.transport.group_file_url)

    def test_group_upload_notice_is_processed_once(self):
        self.documents.result = DocumentIngestResult(
            handled=True,
            reply="已导入行程",
        )
        payload = {
            "post_type": "notice",
            "notice_type": "group_upload",
            "group_id": 10001,
            "user_id": 20001,
            "self_id": 30001,
            "file": {
                "id": "notice-file-1",
                "name": "plan.xlsx",
                "size": 4096,
                "busid": 102,
            },
        }

        first = self.client.post(
            "/onebot",
            headers=self.headers,
            json=payload,
        )
        self.transport.group_file_url_error = RuntimeError("expired URL")
        second = self.client.post(
            "/onebot",
            headers=self.headers,
            json=payload,
        )

        self.assertEqual(first.json(), {"status": "handled"})
        self.assertEqual(second.json(), {"status": "handled"})
        self.assertEqual(len(self.documents.calls), 1)
        self.assertEqual(len(self.transport.messages), 2)
        self.assertIn(
            "正在下载并解析",
            self.transport.messages[0].payload["message"],
        )
        self.assertEqual(
            self.transport.messages[1].payload["message"],
            "已导入行程",
        )
        self.assertEqual(
            self.transport.group_file_url_calls,
            [("10001", "notice-file-1")],
        )

    def test_group_upload_notice_does_not_require_legacy_busid(self):
        self.documents.result = DocumentIngestResult(
            handled=True,
            reply="已导入行程",
        )
        payload = {
            "post_type": "notice",
            "notice_type": "group_upload",
            "group_id": 10001,
            "user_id": 20001,
            "self_id": 30001,
            "file": {
                "id": "notice-file-without-busid",
                "name": "plan.xlsx",
                "size": 4096,
            },
        }

        response = self.client.post(
            "/onebot",
            headers=self.headers,
            json=payload,
        )

        self.assertEqual(response.json(), {"status": "handled"})
        self.assertEqual(
            self.transport.group_file_url_calls,
            [("10001", "notice-file-without-busid")],
        )

    def test_unresolvable_file_message_waits_for_upload_notice(self):
        self.transport.group_file_url_error = RuntimeError(
            "raw file UUID cannot be resolved"
        )

        response = self.client.post(
            "/onebot",
            headers=self.headers,
            json=self.payload(261, [{
                "type": "file",
                "data": {
                    "file": "plan.xlsx",
                    "file_id": "raw-file-uuid",
                    "file_size": 2048,
                },
            }]),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "deferred"})
        self.assertEqual(self.documents.calls, [])

    def test_messages_and_notices_sent_by_bot_are_ignored(self):
        message = self.payload(27, [
            {"type": "at", "data": {"qq": "30001"}},
            {"type": "text", "data": {"text": "状态"}},
        ])
        message["user_id"] = 30001
        notice = {
            "post_type": "notice",
            "notice_type": "group_upload",
            "group_id": 10001,
            "user_id": 30001,
            "self_id": 30001,
            "file": {
                "id": "own-file",
                "name": "plan.xlsx",
                "busid": 102,
            },
        }

        message_response = self.client.post(
            "/onebot",
            headers=self.headers,
            json=message,
        )
        notice_response = self.client.post(
            "/onebot",
            headers=self.headers,
            json=notice,
        )

        self.assertEqual(message_response.json(), {"status": "ignored"})
        self.assertEqual(notice_response.json(), {"status": "ignored"})
        self.assertEqual(self.transport.messages, [])
        self.assertEqual(self.documents.calls, [])

    def test_group_upload_notice_keeps_group_allowlist(self):
        response = self.client.post(
            "/onebot",
            headers=self.headers,
            json={
                "post_type": "notice",
                "notice_type": "group_upload",
                "group_id": 99999,
                "user_id": 20001,
                "self_id": 30001,
                "file": {
                    "id": "blocked-file",
                    "name": "plan.xlsx",
                    "busid": 102,
                },
            },
        )

        self.assertEqual(response.status_code, 403)

    def test_group_file_url_failure_is_reported(self):
        self.transport.group_file_url_error = RuntimeError(
            "retcode=1200"
        )

        response = self.client.post(
            "/onebot",
            headers=self.headers,
            json={
                "post_type": "notice",
                "notice_type": "group_upload",
                "group_id": 10001,
                "user_id": 20001,
                "self_id": 30001,
                "file": {
                    "id": "file-28",
                    "name": "plan.xlsx",
                },
            },
        )

        self.assertEqual(response.status_code, 502)
        self.assertIn("group file URL", response.json()["detail"])

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
    async def test_get_group_file_url_returns_napcat_url(self):
        requests = []

        async def handler(request):
            requests.append(request)
            return httpx.Response(200, json={
                "status": "ok",
                "retcode": 0,
                "data": {"url": "https://example.test/plan.xlsx"},
            })

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://onebot.test",
        )
        transport = OneBotTransport(
            "http://onebot.test",
            "token",
            client=client,
        )

        url = await transport.get_group_file_url(
            "10001",
            "file-1",
        )

        self.assertEqual(url, "https://example.test/plan.xlsx")
        self.assertEqual(requests[0].url.path, "/get_group_file_url")
        self.assertEqual(
            json.loads(requests[0].content),
            {"group_id": "10001", "file_id": "file-1"},
        )
        await client.aclose()

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
        from core.chat_transport import OutgoingMessage

        with self.assertRaises(httpx.HTTPStatusError):
            await transport.send(OutgoingMessage(
                channel="group",
                target_id="10001",
                reply_to_id="message-1",
                payload={"message": "hello"},
            ))
        await client.aclose()
