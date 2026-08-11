import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from core.chat_transport import ChatAttachment
from infrastructure.memory_store import MemoryStore
from services.reservation_draft_creator import ReservationDraftCreator
from tools.agent_tools import (
    CREATE_RESERVATION_DRAFT_TOOL,
    AgentToolContext,
    TOOLS_BY_NAME,
)
from tools.reservation_tools import AgentToolRouter


class FakeImageService:
    def __init__(self):
        self.calls = []

    @staticmethod
    def is_supported_attachment(attachment):
        return attachment.content_type.startswith("image/")

    def process_attachment(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            image=SimpleNamespace(storage_scope_id=kwargs["storage_scope_id"]),
            extraction=SimpleNamespace(items=()),
        )


class FakeReservationService:
    def __init__(self, store):
        self.store = store
        self.created = []
        self.finished = []
        self.updated = []

    def create_draft(self, image, items, source_event_id=""):
        self.created.append((image, items, source_event_id))
        return SimpleNamespace(plan_code="R-20260802-001", items=())

    def format_draft(self, plan):
        return f"预约计划 {plan.plan_code}"

    def finish_workflow(self, platform, group_id, creator_id):
        self.finished.append((platform, group_id, creator_id))

    def update_draft_item(self, **kwargs):
        self.updated.append(kwargs)
        return SimpleNamespace(plan_code=kwargs["plan_code"], items=())

    def update_draft_items(self, **kwargs):
        self.updated.append(kwargs)
        return SimpleNamespace(plan_code=kwargs["plan_code"], items=())


class FakeTravelService:
    def execute_tool(self, name, arguments):
        return "unused"


class ReservationCreationToolTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.temp_dir.name) / "memory.db")
        self.image_service = FakeImageService()
        self.reservation_service = FakeReservationService(self.store)
        creator = ReservationDraftCreator(
            self.image_service,
            self.reservation_service,
        )
        self.router = AgentToolRouter(
            FakeTravelService(),
            self.reservation_service,
            creator,
        )
        self.event_id = "onebot:group:10001:message-1"
        self.store.begin_event(self.event_id)

    def tearDown(self):
        self.temp_dir.cleanup()

    def context(self, attachments):
        return AgentToolContext(
            platform="onebot",
            group_id="10001",
            creator_id="20001",
            event_id=self.event_id,
            attachments=attachments,
        )

    def test_creation_tool_is_registered(self):
        self.assertIn(CREATE_RESERVATION_DRAFT_TOOL, TOOLS_BY_NAME)

    def test_draft_update_tool_is_registered_and_executes(self):
        self.assertIn("update_reservation_draft_item", TOOLS_BY_NAME)

        result = self.router.execute(
            "update_reservation_draft_item",
            {
                "plan_code": "R-20260811-001",
                "item_index": 6,
                "visit_date": "2026-08-21",
                "requires_reservation": False,
            },
            self.context(()),
        )

        self.assertEqual(result, "预约计划 R-20260811-001")
        self.assertEqual(len(self.reservation_service.updated), 1)
        self.assertEqual(
            self.reservation_service.updated[0]["visit_date"],
            date(2026, 8, 21),
        )

    def test_batch_draft_update_tool_executes_once_by_attraction_name(self):
        self.assertIn("update_reservation_draft_items", TOOLS_BY_NAME)

        result = self.router.execute(
            "update_reservation_draft_items",
            {
                "plan_code": "R-20260811-001",
                "updates": [
                    {
                        "attraction_name": "嘉峪关",
                        "visit_date": "2026-08-21",
                        "requires_reservation": False,
                    },
                    {
                        "attraction_name": "水上雅丹",
                        "visit_status": "skip",
                    },
                ],
            },
            self.context(()),
        )

        self.assertEqual(result, "预约计划 R-20260811-001")
        self.assertEqual(len(self.reservation_service.updated), 1)
        updates = self.reservation_service.updated[0]["updates"]
        self.assertEqual(updates[0]["attraction_name"], "嘉峪关")
        self.assertEqual(updates[0]["visit_date"], date(2026, 8, 21))
        self.assertEqual(updates[1]["visit_status"], "skip")

    def test_creation_tool_uses_current_attachment_and_is_idempotent(self):
        attachment = ChatAttachment(
            filename="booking.jpg",
            url="https://example.test/booking.jpg",
            content_type="image/jpeg",
        )
        context = self.context((attachment,))

        first = self.router.execute(
            CREATE_RESERVATION_DRAFT_TOOL,
            {"attachment_index": 1},
            context,
        )
        second = self.router.execute(
            CREATE_RESERVATION_DRAFT_TOOL,
            {"attachment_index": 1},
            context,
        )

        self.assertEqual(first, "预约计划 R-20260802-001")
        self.assertEqual(second, first)
        self.assertEqual(len(self.image_service.calls), 1)
        self.assertEqual(len(self.reservation_service.created), 1)
        self.assertEqual(
            self.reservation_service.created[0][2],
            self.event_id,
        )
        self.assertEqual(
            self.reservation_service.finished,
            [("onebot", "10001", "20001")],
        )

    def test_creation_tool_rejects_missing_current_image(self):
        result = self.router.execute(
            CREATE_RESERVATION_DRAFT_TOOL,
            {"attachment_index": 1},
            self.context(()),
        )

        self.assertIn("当前消息没有预约图片", result)
        self.assertEqual(self.image_service.calls, [])


if __name__ == "__main__":
    unittest.main()
