"""Former classifier cases now exercise the actual router and typed read-policy boundary."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from app.request_router import RequestRouter
from core.chat_transport import ChatEvent
from core.execution_outcome import TravelReadRequest
from core.intent_ir import IntentIR, IntentOperation
from infrastructure.memory_store import MemoryStore
from tools.reservation.tool import ReservationTool
from tools.travel_query import TravelQueryTool, read_turn_policy


class RecordingQuery(TravelQueryTool):
    def execute(self, event, claim, request, *, key=''):
        return request


class RecordingReservation(ReservationTool):
    def __init__(self, store):
        super().__init__(store)
        self.commands = []

    def handle_command(self, command, event):
        self.commands.append(command.name)
        return '只记录路由，不执行业务写入。'


def route_probe(text, operations=(), *, action='none'):
    with tempfile.TemporaryDirectory() as folder:
        store = MemoryStore(Path(folder)/'routing.db')
        event = ChatEvent('web', 'group', 'route', 'conversation', 'owner', text)
        ir = IntentIR(1, action, tuple(operations), source_spans=(text,) if operations else ())
        compiler = Mock(try_compile=Mock(return_value=ir))
        query = RecordingQuery(store=store, tool_executor=Mock())
        reservations = RecordingReservation(store)
        router = RequestRouter(store, compiler, read_only_service=query, reservation_tool=reservations)
        reply, policy = router.route(event, None)
        if isinstance(reply, TravelReadRequest):
            policy, _ = read_turn_policy(reply, policy, '')
        return reply, policy, reservations.commands


def route_policy(text, operations=(), *, action='none'):
    return route_probe(text, operations, action=action)[1]


class RouterPolicyRegressionTests(unittest.TestCase):
    def test_current_weather_requires_current_weather_tool(self):
        decision = route_policy('查询天气 西宁')
        self.assertEqual(decision.intent, 'weather')
        self.assertTrue(decision.require_live_data)
        self.assertEqual(decision.allowed_tools, ('get_current_weather',))

    def test_route_traffic_excludes_duplicate_driving_route_tool(self):
        decision = route_policy('查询路况 西宁 -> 青海湖')
        self.assertEqual(decision.intent, 'traffic')
        self.assertEqual(decision.allowed_tools, ('get_route_traffic',))
        self.assertNotIn('get_driving_route', decision.allowed_tools)

    def test_missing_route_endpoints_requires_clarification(self):
        text = '帮我看看路况'
        request, decision, _ = route_probe(text, [IntentOperation('traffic', 'query', {})], action='clarify')
        self.assertIsInstance(request, TravelReadRequest)
        execute = Mock(side_effect=AssertionError('Missing endpoints cannot call a provider'))
        query = TravelQueryTool(tool_executor=execute)
        reply = query.execute(ChatEvent('web', 'group', 'missing', 'c', 'owner', text), None, request)
        self.assertIn('起点', reply)
        self.assertIn('终点', reply)
        execute.assert_not_called()

    def test_document_question_does_not_force_amap_tools(self):
        reply, decision, commands = route_probe('文档里的住宿安排是什么？')
        self.assertNotIsInstance(reply, TravelReadRequest)
        self.assertFalse(decision.require_live_data)
        self.assertEqual(decision.allowed_tools, ('query_hotel_prices',))
        self.assertEqual(commands, [])

    def test_xlsx_filename_question_does_not_grant_route_tools(self):
        decision = route_policy('根据我上传的青甘大环线自驾行程安排.xlsx，列出8月17日的全部行程')
        self.assertEqual(decision.allowed_tools, ('query_hotel_prices',))

    def test_weather_and_traffic_can_be_selected_together(self):
        text = '明天从西宁到青海湖，天气和路况怎么样？'
        operations = [IntentOperation('traffic', 'query', {'origin': '西宁', 'destination': '青海湖'}),
                      IntentOperation('forecast', 'query', {'location': '青海湖'})]
        decision = route_policy(text, operations, action='read')
        self.assertEqual(decision.intents, ('traffic', 'forecast'))
        self.assertEqual(decision.allowed_tools, ('get_route_traffic', 'get_weather_forecast'))
        self.assertFalse(decision.needs_clarification)

    def test_natural_reservation_confirmation_requires_bound_operation(self):
        text = '帮我确认预约 R-20260722-001'
        decision = route_policy(text, [IntentOperation('booking', 'confirm_reservation_plan',
            {'reference': 'R-20260722-001', 'authorization': text})], action='confirm')
        self.assertEqual(decision.intent, 'reservation')
        self.assertIn('list_reservation_plans', decision.allowed_tools)
        self.assertIn('confirm_reservation_plan', decision.allowed_tools)
        self.assertEqual(decision.action_resources, (('confirm_reservation_plan', 'R-20260722-001'),))

    def test_plan_code_edit_has_only_the_typed_draft_operation(self):
        text = '把 R-20260811-001 的嘉峪关日期补为 2026-08-21，水上雅丹不去参观了'
        decision = route_policy(text, [IntentOperation('booking', 'update_reservation_draft_items',
            {'reference': 'R-20260811-001', 'authorization': text})], action='update')
        self.assertEqual(decision.intent, 'reservation')
        self.assertEqual(decision.allowed_tools, ('update_reservation_draft_items',))
        self.assertEqual(decision.required_tool_groups, (('update_reservation_draft_items',),))

    def test_image_words_without_a_current_attachment_do_not_grant_creation(self):
        decision = route_policy('按这张攻略帮我制定预约')
        self.assertNotIn('create_reservation_draft_from_image', decision.allowed_tools)
