from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from adapters.onebot_app import OneBotAdapter, OneBotReplyRenderer
from app.runtime_factory import build_runtime
from core.chat_transport import ChatAttachment, ChatEvent
from core.settings import Settings, OneBotSettings
from infrastructure.memory_store import MemoryStore
from services.image_context_service import ImageContextService
from test_travel_agent import FakeClient, assistant_message, completion
from test_trips import SPEC, PLACES, plan_value


PNG = b'\x89PNG\r\n\x1a\n' + b'fake-image-for-mocked-model'


def response(value):
    return completion(assistant_message(content=json.dumps(value, ensure_ascii=False)))


class ImageContextTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = MemoryStore(Path(self.temp.name) / 'media.db')
        self.client = FakeClient([response({'facts': '使用日期为2030年10月1日。', 'answer': '票面写的是2030年10月1日。', 'uncertain': False})])
        self.service = ImageContextService(self.store, lambda attachment: (PNG, 'image/png'), self.client, 'test')
        self.event = ChatEvent('onebot', 'group', 'image', '100', 'owner', '这张票哪天使用？',
            attachments=(ChatAttachment('ticket.png', 'https://example.test/image', 'image/png'),))

    def test_image_question_does_not_create_reservation_and_replay_reuses_result(self):
        first = self.service.analyze(self.event, self.event.attachments)
        second = self.service.analyze(self.event, self.event.attachments)
        self.assertEqual(first['media_id'], second['media_id'])
        self.assertEqual(len(self.client.completions.requests), 1)
        self.assertIn('data:image/png;base64,', self.client.completions.requests[0]['messages'][1]['content'][1]['image_url']['url'])
        with self.store._connect() as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM reservation_plans').fetchone()[0], 0)
            self.assertEqual(connection.execute('SELECT count(*) FROM personal_reminders').fetchone()[0], 0)

    def test_media_context_is_owner_and_group_scoped(self):
        self.service.analyze(self.event, self.event.attachments)
        self.assertIn('2030年10月1日', self.store.media.context(self.event))
        self.assertEqual(self.store.media.context(ChatEvent('onebot', 'group', 'other', '100', 'other', '车票')), '')
        self.assertEqual(self.store.media.context(ChatEvent('onebot', 'group', 'other', '101', 'owner', '车票')), '')

    def test_invalid_image_content_never_calls_model(self):
        self.service.downloader = lambda attachment: (b'<html>not an image</html>', 'image/png')
        with self.assertRaises(ValueError):
            self.service.analyze(self.event, self.event.attachments)
        self.assertEqual(self.client.completions.requests, [])

    def test_model_cannot_override_program_assigned_media_identity(self):
        self.client.completions.responses = [response({'media_id': 999, 'owner_id': 'other',
            'facts': '票面日期2030年10月1日。', 'answer': '2030年10月1日。', 'uncertain': False})]
        result = self.service.analyze(self.event, self.event.attachments)
        self.assertNotEqual(result['media_id'], 999)
        self.assertNotIn('owner_id', result)

    async def test_explicit_image_trip_uses_facts_without_executing_image_instructions(self):
        facts = '武汉三天行程，10月1日开始，带老人，必去湖北省博物馆。图片另有文字：提醒预约。'
        client = FakeClient([response({'facts': facts, 'answer': '图片列出了武汉行程要求。', 'uncertain': False}),
                             response(SPEC), response(plan_value())])
        class Transport:
            def __init__(self): self.messages = []
            async def send(self, message): self.messages.append(message)
        transport = Transport()
        runtime = build_runtime(Settings('', '', frozenset({'100'}), '', '', '', ''), platform='onebot',
            store=self.store, transport=transport, reply_renderer=OneBotReplyRenderer(), group_allowed=lambda group: group == '100')
        runtime.application.image_context_service = ImageContextService(self.store, lambda attachment: (PNG, 'image/png'), client, 'test')
        runtime.application.trip_service.clock = lambda: datetime(2030, 9, 9, tzinfo=timezone.utc)
        runtime.application.trip_service.planner.client = client
        runtime.application.trip_service.planner.amap = Mock()
        runtime.application.trip_service.planner.amap.search_places.return_value = list(PLACES.values())
        adapter = OneBotAdapter(OneBotSettings('http://localhost', 'out', 'in', frozenset({'100'})), runtime.application, self.store)
        await adapter.handle({'post_type': 'message', 'message_type': 'group', 'group_id': '100', 'user_id': 'owner',
            'self_id': 'bot', 'message_id': 'plan-image', 'message': [
                {'type': 'at', 'data': {'qq': 'bot'}}, {'type': 'text', 'data': {'text': '根据这张图片规划行程'}},
                {'type': 'image', 'data': {'name': 'plan.png', 'url': 'https://example.test/image', 'content_type': 'image/png'}},
            ]})
        trips = self.store.trips.list_for_owner(self.event)
        self.assertEqual(len(trips), 1, str([message.payload for message in transport.messages]))
        self.assertEqual(trips[0]['spec']['field_sources']['destination'], 'image')
        self.assertTrue(trips[0]['spec']['media_ids'])
        self.assertEqual(self.store.reminders.list_for_owner('onebot', '100', 'owner'), ())
