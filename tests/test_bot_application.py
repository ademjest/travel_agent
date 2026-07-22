import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from bot_application import TravelBotApplication
from chat_transport import ChatAttachment, ChatEvent
from document_service import DocumentIngestResult
from memory_store import MemoryStore
from outbox_worker import OutboxWorker
from upload_binding import PrivateUploadResult


class FakeTransport:
    def __init__(self):
        self.messages = []

    async def send(self, message):
        self.messages.append(message)


class FakeRenderer:
    def render(self, channel, command_content, reply_text):
        return {"msg_type": 0, "content": reply_text}


class FakeTravelService:
    def __init__(self):
        self.calls = []

    def handle(self, content):
        self.calls.append(content)
        return f"fixed:{content}"


class FakeTravelAgent:
    def __init__(self):
        self.calls = []

    def run(self, content, context):
        self.calls.append((content, context))
        return SimpleNamespace(reply=f"agent:{content}", traces=())


class FakeDocumentService:
    def __init__(self):
        self.calls = []

    def ingest_attachments(self, group_id, sender_id, attachments):
        self.calls.append((group_id, sender_id, attachments))
        return DocumentIngestResult(handled=False)


class FakeUploadBindingService:
    def __init__(self):
        self.issue_calls = []
        self.private_calls = []

    def issue_binding(self, group_id, sender_id):
        self.issue_calls.append((group_id, sender_id))
        return "binding-code"

    def handle_private_message(
            self,
            user_id,
            content,
            attachments,
            **kwargs):
        self.private_calls.append((user_id, content, attachments, kwargs))
        return PrivateUploadResult(reply="private-reply")


class FakeReservationImageService:
    def __init__(self):
        self.calls = []
        self.error = None

    @staticmethod
    def is_supported_attachment(attachment):
        return attachment.content_type.startswith("image/")

    def process_attachment(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        image = SimpleNamespace(
            image_id=1,
            storage_scope_id=kwargs["storage_scope_id"],
            platform=kwargs["platform"],
            group_id=kwargs["group_id"],
            uploader_id=kwargs["uploader_id"],
            status="extracted",
        )
        extraction = SimpleNamespace(items=())
        return SimpleNamespace(image=image, extraction=extraction)


class FakeReservationService:
    def __init__(self):
        self.created = []
        self.commands = []

    def create_draft(self, image, items):
        self.created.append((image, items))
        return SimpleNamespace(plan_code="R-20260722-001", items=())

    def format_draft(self, plan):
        return f"预约计划 {plan.plan_code}"

    def handle_command(self, command, event):
        self.commands.append((command, event))
        return "预约命令已处理"


class TravelBotApplicationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.temp_dir.name) / "memory.db")
        self.transport = FakeTransport()
        self.worker = OutboxWorker(
            "qq_official",
            self.store,
            self.transport,
        )
        self.travel_service = FakeTravelService()
        self.travel_agent = FakeTravelAgent()
        self.document_service = FakeDocumentService()
        self.upload_service = FakeUploadBindingService()
        self.reservation_image_service = FakeReservationImageService()
        self.reservation_service = FakeReservationService()
        self.application = TravelBotApplication(
            store=self.store,
            travel_service=self.travel_service,
            travel_agent=self.travel_agent,
            document_service=self.document_service,
            reservation_image_service=self.reservation_image_service,
            reservation_service=self.reservation_service,
            upload_binding_service=self.upload_service,
            outbox_worker=self.worker,
            reply_renderer=FakeRenderer(),
            group_allowed=lambda group_id: group_id == "group-a",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def group_event(event_id, content):
        return ChatEvent(
            platform="qq_official",
            channel="group",
            event_id=event_id,
            scope_id="group-a",
            sender_id="member-a",
            content=content,
        )

    async def test_fixed_command_creates_and_delivers_outbox_reply(self):
        event = self.group_event("message-1", "查询天气 西宁")

        await self.application.handle(event)

        self.assertEqual(self.travel_service.calls, ["查询天气 西宁"])
        self.assertEqual(len(self.transport.messages), 1)
        self.assertEqual(
            self.transport.messages[0].payload["content"],
            "fixed:查询天气 西宁",
        )
        self.assertEqual(self.store.get_event_status(event.event_key), "completed")

    async def test_unknown_group_message_uses_llm_agent(self):
        event = self.group_event("message-llm", "帮我评估明天的行程")

        await self.application.handle(event)

        self.assertEqual(len(self.travel_agent.calls), 1)
        self.assertEqual(
            self.transport.messages[0].payload["content"],
            "agent:帮我评估明天的行程",
        )

    async def test_document_context_is_passed_to_agent(self):
        self.store.add_document(
            "group-a",
            "member-a",
            "plan.txt",
            "plan-hash",
            "8月16日从西宁前往青海湖，晚上住宿茶卡镇。",
            ["8月16日从西宁前往青海湖，晚上住宿茶卡镇。"],
        )
        event = self.group_event("message-doc", "茶卡住哪里")

        await self.application.handle(event)

        knowledge_context = self.travel_agent.calls[0][1].document_context
        self.assertIn("茶卡镇", knowledge_context)

    async def test_disallowed_group_is_ignored_before_claiming_event(self):
        event = ChatEvent(
            platform="qq_official",
            channel="group",
            event_id="message-blocked",
            scope_id="group-blocked",
            sender_id="member-a",
            content="查询天气 西宁",
        )

        await self.application.handle(event)

        self.assertEqual(self.travel_service.calls, [])
        self.assertIsNone(self.store.get_event_status(event.event_key))
        self.assertEqual(self.transport.messages, [])

    async def test_duplicate_event_is_not_processed_twice(self):
        event = self.group_event("message-duplicate", "查询天气 西宁")

        await self.application.handle(event)
        await self.application.handle(event)

        self.assertEqual(self.travel_service.calls, ["查询天气 西宁"])
        self.assertEqual(len(self.transport.messages), 1)

    async def test_group_event_is_persisted_as_normalized_observation(self):
        event = self.group_event("message-observed", "明早八点集合")

        await self.application.handle(event)

        messages = self.store.get_recent_chat_messages(
            "qq_official",
            "group-a",
        )
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].member_id, "member-a")
        self.assertEqual(messages[0].content, "明早八点集合")

    async def test_single_image_is_routed_before_document_and_agent(self):
        event = ChatEvent(
            platform="qq_official",
            channel="group",
            event_id="image-1",
            scope_id="group-a",
            sender_id="member-a",
            content="",
            attachments=(
                ChatAttachment(
                    filename="booking.jpg",
                    url="https://example.test/booking.jpg",
                    content_type="image/jpeg",
                ),
            ),
        )
        await self.application.handle(event)
        self.assertEqual(len(self.reservation_image_service.calls), 1)
        self.assertEqual(len(self.reservation_service.created), 1)
        self.assertEqual(self.document_service.calls, [])
        self.assertEqual(self.travel_agent.calls, [])

    async def test_multiple_images_are_rejected_without_model_call(self):
        attachments = tuple(
            ChatAttachment(
                filename=f"booking-{index}.jpg",
                url=f"https://example.test/{index}.jpg",
                content_type="image/jpeg",
            )
            for index in (1, 2)
        )
        event = ChatEvent(
            platform="qq_official",
            channel="group",
            event_id="image-2",
            scope_id="group-a",
            sender_id="member-a",
            content="",
            attachments=attachments,
        )
        await self.application.handle(event)
        self.assertEqual(self.reservation_image_service.calls, [])
        sent = self.transport.messages[0].payload
        self.assertIn("逐张发送", str(sent))

    async def test_image_download_failure_creates_no_plan(self):
        self.reservation_image_service.error = ValueError(
            "图片超过 5 MB 限制"
        )
        event = ChatEvent(
            platform="qq_official",
            channel="group",
            event_id="image-failed",
            scope_id="group-a",
            sender_id="member-a",
            content="",
            attachments=(
                ChatAttachment(
                    filename="booking.jpg",
                    url="https://example.test/booking.jpg",
                    content_type="image/jpeg",
                ),
            ),
        )
        await self.application.handle(event)
        self.assertEqual(self.reservation_service.created, [])
        self.assertIn("5 MB", str(self.transport.messages[0].payload))

    async def test_reservation_command_runs_before_travel_agent(self):
        event = self.group_event("reservation-list", "查看预约提醒")
        await self.application.handle(event)
        self.assertEqual(len(self.reservation_service.commands), 1)
        self.assertEqual(self.travel_agent.calls, [])
