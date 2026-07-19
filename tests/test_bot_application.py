import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from bot_application import TravelBotApplication
from chat_transport import ChatEvent
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

    def run(self, content, history, knowledge_context):
        self.calls.append((content, history, knowledge_context))
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
        self.application = TravelBotApplication(
            store=self.store,
            travel_service=self.travel_service,
            travel_agent=self.travel_agent,
            document_service=self.document_service,
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
            reply_to_id=event_id,
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

        knowledge_context = self.travel_agent.calls[0][2]
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
