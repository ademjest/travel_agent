from pathlib import Path
import tempfile
import unittest

from core.chat_transport import ChatAttachment
from core.tool_result import AgentToolContext
from infrastructure.memory_store import MemoryStore
from tools.registry import AgentToolRouter
from tools.reservation.tool import ReservationTool, ReservationImageService, ImageVisionExtractor
from test_vision_service import FakeClient, FakeResponse, FakeSession, extraction_json


class ReservationToolTests(unittest.TestCase):
    def test_owned_image_to_draft_replays_once_without_confirming_reminders(self):
        with tempfile.TemporaryDirectory() as folder:
            store = MemoryStore(Path(folder)/'reservation.db')
            session = FakeSession([FakeResponse(b'\x89PNG\r\n\x1a\nfixture', 'image/png')])
            client = FakeClient([extraction_json()])
            images = ReservationImageService(
                store, ImageVisionExtractor('test', client), image_root=Path(folder)/'images',
                session=session, resolver=lambda hostname: ('93.184.216.34',))
            owner = ReservationTool(store, image_service=images)
            router = AgentToolRouter(None, owner)
            context = AgentToolContext('web', 'scope', 'owner', 'image-event',
                attachments=(ChatAttachment('reservation.png', 'https://example.test/image', 'image/png'),),
                allowed_actions=frozenset({'create_reservation_draft_from_image'}))
            store.delivery.begin_event(context.event_id)
            first = router.execute_result('create_reservation_draft_from_image', {'attachment_index': 1}, context)
            replay = router.execute_result('create_reservation_draft_from_image', {'attachment_index': 1}, context)
            self.assertEqual(first, replay)
            self.assertEqual((first.status, first.effect_status), ('completed', 'committed'))
            self.assertEqual(len(session.calls), 1)
            self.assertEqual(len(client.chat.completions.calls), 1)
            plans = store.reservations.list_reservation_plans_for_creator('web', 'scope', 'owner')
            self.assertEqual(len(plans), 1)
            self.assertEqual(plans[0].status, 'draft')
            self.assertEqual(store.reservations.list_reservation_reminders('web', 'scope', 'owner'), ())
