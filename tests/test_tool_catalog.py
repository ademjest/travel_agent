from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from agents.travel_agent import TravelAgent
from core.settings import Settings
from core.tool_result import AgentToolContext, ToolResult, validate_arguments
from infrastructure.memory_store import MemoryStore
from core.tool_result import TOOLS_BY_NAME
from tools.registry import (COMPACT_CATALOG, LEGACY_CATALOG, DEFAULT_CATALOG, ToolRegistry,
    TOOL_DEFINITIONS, catalog_schemas, normalize_tool_call)
from test_travel_agent import FakeClient, completion, assistant_message, tool_call, execution_policy


class ToolCatalogTests(unittest.TestCase):
    def registry(self):
        self.calls = []
        def execute(action, arguments, context):
            self.calls.append((action, arguments))
            return ToolResult('completed', action, arguments.get('plan_code', arguments.get('item_code', '')),
                              data='已提交', effect_status='committed')
        return ToolRegistry(replace(row, handler=execute) for row in TOOL_DEFINITIONS.values())

    def test_catalog_counts_and_default(self):
        self.assertEqual(DEFAULT_CATALOG, 'legacy-v4')
        self.assertEqual(catalog_schemas(), list(TOOLS_BY_NAME.values()))
        compact = catalog_schemas(catalog_version=COMPACT_CATALOG)
        self.assertEqual(len(compact), 12)
        self.assertEqual(len({row['function']['name'] for row in compact}), 12)

    def test_weather_scope_cannot_expand_to_forecast(self):
        registry = self.registry()
        context = AgentToolContext('web', 'c', 'owner', 'event',
            allowed_actions=frozenset({'get_current_weather'}), catalog_version=COMPACT_CATALOG)
        schema = catalog_schemas(context.allowed_actions, COMPACT_CATALOG)[0]
        self.assertEqual(schema['function']['parameters']['properties']['period']['enum'], ['current'])
        result = registry.execute('query_weather', {'location': '武汉', 'period': 'forecast'}, context)
        self.assertEqual(result.error_code, 'operation_denied')
        self.assertEqual(self.calls, [])

    def test_cancel_item_cannot_expand_to_plan_or_another_item(self):
        registry = self.registry()
        context = AgentToolContext('web', 'c', 'owner', 'event',
            allowed_actions=frozenset({'cancel_reservation_item'}),
            action_resources=(('cancel_reservation_item', 'A-000001'),), catalog_version=COMPACT_CATALOG)
        for kind, code in [('plan', 'R-20301001-001'), ('item', 'A-000002')]:
            with self.subTest(kind=kind):
                result = registry.execute('cancel_reservation', {'target_kind': kind, 'target_code': code}, context)
                self.assertEqual(result.status, 'failed')
        self.assertEqual(self.calls, [])
        result = registry.execute('cancel_reservation', {'target_kind': 'item', 'target_code': 'A-000001'}, context)
        self.assertEqual(result.action, 'cancel_reservation_item')
        self.assertEqual(self.calls, [('cancel_reservation_item', {'item_code': 'A-000001'})])

    def test_cross_operation_fields_and_route_modes_are_rejected(self):
        for name, values in [
            ('reservation_draft', {'operation': 'refresh', 'plan_code': 'R-20301001-001', 'attachment_index': 1}),
            ('query_route', {'mode': 'walking', 'origin': '武汉站', 'destination': '博物馆', 'traffic': False}),
            ('modify_reservation_item', {'operation': 'date', 'item_code': 'A-000001', 'visit_date': '2030-10-02', 'reminder_times': ['2030-10-01 10:00']}),
            ('cancel_reservation', {'target_kind': 'item', 'target_code': 'A-000001', 'plan_code': 'R-20301001-001'}),
        ]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                normalize_tool_call(name, values, COMPACT_CATALOG)

    def test_traffic_and_walking_permissions_keep_valid_walking_schema(self):
        rows = catalog_schemas(['get_route_traffic', 'get_walking_route'], COMPACT_CATALOG)
        route = next(row['function']['parameters'] for row in rows if row['function']['name'] == 'query_route')
        self.assertFalse(validate_arguments({'mode': 'walking', 'origin': '甲', 'destination': '乙'}, route))
        self.assertEqual(route['properties']['traffic']['enum'], [True])

    def test_catalog_freezes_across_default_change_and_old_retry_stays_legacy(self):
        with tempfile.TemporaryDirectory() as folder:
            store = MemoryStore(Path(folder)/'catalog.db')
            store.delivery.begin_event('new')
            self.assertEqual(store.delivery.freeze_tool_catalog('new', COMPACT_CATALOG), COMPACT_CATALOG)
            self.assertEqual(store.delivery.freeze_tool_catalog('new', LEGACY_CATALOG), COMPACT_CATALOG)
            now = datetime.now(timezone.utc)
            store.delivery.begin_event('old', now=now-timedelta(hours=1))
            store.delivery.begin_event('old', now=now)
            self.assertEqual(store.delivery.freeze_tool_catalog('old', COMPACT_CATALOG), LEGACY_CATALOG)

    def test_compact_replay_reuses_legacy_operation_key_without_second_mutation(self):
        from test_reservation_tools import FakeReservationService
        from tools.reservation.tool import reservation_result
        with tempfile.TemporaryDirectory() as folder:
            store = MemoryStore(Path(folder)/'replay.db')
            service = FakeReservationService(store)
            store.delivery.begin_event('replay')
            def execute(action, arguments, context):
                return reservation_result(service, None, action, arguments, context)
            registry = ToolRegistry(replace(row, handler=execute) for row in TOOL_DEFINITIONS.values())
            context = AgentToolContext('web', 'c', 'owner', 'replay',
                allowed_actions=frozenset({'update_reservation_draft_item'}),
                action_resources=(('update_reservation_draft_item', 'R-20301001-001'),))
            arguments = {'plan_code': 'R-20301001-001', 'item_index': 1, 'visit_status': 'skip'}
            first = registry.execute('update_reservation_draft_item', arguments, context)
            second = registry.execute('reservation_draft', {'operation': 'update_item', **arguments},
                                      replace(context, catalog_version=COMPACT_CATALOG))
            self.assertEqual(first.status, 'completed')
            self.assertEqual(first.operation_key, second.operation_key)
            self.assertEqual(len(service.updated), 1)

    def test_agent_normalizes_surface_before_authorization_and_records_canonical_receipt(self):
        calls = []
        def execute(name, arguments, context):
            calls.append((name, arguments))
            return ToolResult('completed', name, arguments['plan_code'], data='已刷新草稿', effect_status='committed')
        client = FakeClient([completion(assistant_message(tool_calls=[tool_call('1', 'reservation_draft', json.dumps({
            'operation': 'refresh', 'plan_code': 'R-20301001-001'}))]))])
        settings = Settings('', '', frozenset(), '', 'key', 'https://example.invalid/v1', 'fake')
        agent = TravelAgent(settings, execute, client=client, catalog_version=COMPACT_CATALOG)
        policy = execution_policy(('reservation',), ('refresh_reservation_plan',),
            resources=(('refresh_reservation_plan', 'R-20301001-001'),))
        result = agent.run('刷新预约草稿 R-20301001-001', decision_override=policy,
            tool_context=AgentToolContext('web', 'c', 'owner', 'event'))
        self.assertEqual(calls, [('refresh_reservation_plan', {'plan_code': 'R-20301001-001'})])
        self.assertEqual(result.traces[0].name, 'refresh_reservation_plan')
        self.assertEqual(result.traces[0].surface_name, 'reservation_draft')
        self.assertEqual(result.receipts[0].action, 'refresh_reservation_plan')
