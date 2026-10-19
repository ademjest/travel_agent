import unittest
from unittest.mock import Mock
from pathlib import Path
import tempfile

import requests

from core.settings import Settings
from core.tool_execution import retry_read
from core.tool_result import ToolResult
from infrastructure.amap_client import AmapClient, AmapError
from services.travel_service import TravelService
from core.model_budget import MODEL_EVENT
from core.execution_scope import CURRENT_EXECUTION, ExecutionScope, ExecutionRevoked
from infrastructure.memory_store import MemoryStore


def response(value=None, status=200, retry_after=None):
    result = Mock(status_code=status, headers={} if retry_after is None else {'Retry-After': retry_after})
    result.json.return_value = value
    if status >= 400:
        result.raise_for_status.side_effect = requests.HTTPError(response=result)
    return result


class ReadReliabilityTests(unittest.TestCase):
    def test_http_transient_retry_does_not_repeat_successful_geocode(self):
        get = Mock(side_effect=[response({'status': '1', 'geocodes': [{'formatted_address': '武汉',
            'location': '114.3,30.5', 'adcode': '420100'}]}), response(status=503),
            response({'status': '1', 'lives': [{'city': '武汉', 'weather': '晴', 'temperature': '20',
                                               'reporttime': '2030-01-01 10:00:00'}]})])
        waits = []
        client = AmapClient('test-key', http_get=get, sleeper=waits.append)
        self.assertEqual(client.current_weather('武汉').weather, '晴')
        self.assertEqual(get.call_count, 3)
        self.assertEqual(len(waits), 1)

    def test_retry_after_above_wait_limit_is_not_shortened(self):
        get = Mock(return_value=response(status=429, retry_after='120'))
        waits = []
        client = AmapClient('test-key', http_get=get, sleeper=waits.append)
        with self.assertRaises(AmapError) as error:
            client._get('/weather', {})
        self.assertEqual(error.exception.retry_after_seconds, 120)
        self.assertEqual(get.call_count, 1)
        self.assertEqual(waits, [])

    def test_permission_error_never_retries_and_does_not_expose_key(self):
        get = Mock(return_value=response(status=403))
        client = AmapClient('secret-key', http_get=get, sleeper=lambda _: self.fail('must not wait'))
        with self.assertRaises(AmapError) as error:
            client._get('/weather', {})
        self.assertEqual(get.call_count, 1)
        self.assertNotIn('secret-key', str(error.exception))

    def test_malformed_envelope_is_typed_and_not_retried(self):
        get = Mock(return_value=response(['not an object']))
        service = TravelService(Settings('', '', frozenset(), '', '', '', ''))
        service.amap = AmapClient('test-key', http_get=get, sleeper=lambda _: self.fail('must not wait'))
        result = service.execute_result('get_current_weather', {'location': '武汉'})
        self.assertEqual((result.status, result.error_code, result.retryable), ('failed', 'invalid_response', False))
        self.assertEqual(get.call_count, 1)

    def test_retries_have_bounded_jitter_and_explicit_zero_is_honored(self):
        waits = []
        results = [ToolResult('failed', 'read', retryable=True), ToolResult('completed', 'read')]
        retry_read(lambda: results.pop(0), sleeper=waits.append, jitter=lambda limit: limit / 2)
        self.assertEqual(waits, [1])
        results = [ToolResult('failed', 'read', retryable=True, retry_after_seconds=0), ToolResult('completed', 'read')]
        retry_read(lambda: results.pop(0), sleeper=waits.append)
        self.assertEqual(waits, [1, 0])

    def test_rate_limit_text_with_retry_advice_keeps_rate_limit_category(self):
        self.assertEqual(ToolResult.from_text('read', '工具错误：429，请稍后重试').error_code, 'rate_limit')

    def test_missing_weather_fields_are_not_reported_as_success(self):
        service = TravelService(Settings('', '', frozenset(), '', '', '', ''))
        service.amap = AmapClient('key', http_get=Mock(return_value=response({'status': '1', 'lives': [{'city': '武汉'}]})))
        service.amap.geocode = Mock(return_value=Mock(adcode='420100'))
        result = service.execute_result('get_current_weather', {'location': '武汉'})
        self.assertEqual((result.status, result.error_code), ('failed', 'invalid_response'))

    def test_http_attempt_limit_survives_client_recreation(self):
        with tempfile.TemporaryDirectory() as folder:
            store = MemoryStore(Path(folder) / 'reads.db')
            store.begin_event('event')
            token = MODEL_EVENT.set('event')
            self.addCleanup(MODEL_EVENT.reset, token)
            get = Mock(return_value=response(status=503))
            for _ in range(2):
                client = AmapClient('test-key', http_get=get, sleeper=lambda _: None)
                client.checkpoint_store = store
                with self.assertRaises(AmapError):
                    client._get('/weather', {})
            self.assertEqual(get.call_count, 3)

    def test_successful_http_checkpoint_is_reused_after_recreation(self):
        with tempfile.TemporaryDirectory() as folder:
            store = MemoryStore(Path(folder) / 'reads.db')
            store.begin_event('event')
            token = MODEL_EVENT.set('event')
            self.addCleanup(MODEL_EVENT.reset, token)
            get = Mock(return_value=response({'status': '1', 'value': 'fixed'}))
            for _ in range(2):
                client = AmapClient('test-key', http_get=get)
                client.checkpoint_store = store
                self.assertEqual(client._get('/weather', {})['value'], 'fixed')
            self.assertEqual(get.call_count, 1)

    def test_cancel_during_wait_prevents_next_http_request(self):
        with tempfile.TemporaryDirectory() as folder:
            store = MemoryStore(Path(folder) / 'cancel.db')
            store.inbox.submit('event', 'web', 'scope', 'owner', {}, False)
            job = store.inbox.claim()
            token = CURRENT_EXECUTION.set(ExecutionScope(str(store.database_path.resolve()), job['id'], job['claim_token']))
            self.addCleanup(CURRENT_EXECUTION.reset, token)
            get = Mock(return_value=response(status=503))
            client = AmapClient('key', http_get=get, sleeper=lambda _: store.inbox.cancel('event', 'web', 'scope', 'owner'))
            with self.assertRaises(ExecutionRevoked):
                client._get('/weather', {})
            self.assertEqual(get.call_count, 1)

    def test_retry_after_beyond_deadline_is_not_sent_early(self):
        with tempfile.TemporaryDirectory() as folder:
            store = MemoryStore(Path(folder) / 'deadline.db')
            store.inbox.submit('event', 'web', 'scope', 'owner', {}, False)
            job = store.inbox.claim(deadline_seconds=15)
            token = CURRENT_EXECUTION.set(ExecutionScope(str(store.database_path.resolve()), job['id'], job['claim_token']))
            self.addCleanup(CURRENT_EXECUTION.reset, token)
            get = Mock(return_value=response(status=429, retry_after='20'))
            client = AmapClient('key', http_get=get, sleeper=lambda _: self.fail('must not wait'))
            with self.assertRaises(AmapError):
                client._get('/weather', {})
            self.assertEqual(get.call_count, 1)

    def test_planner_routes_use_the_same_http_retry_boundary(self):
        from agents.trip_planner import TripPlanner
        get = Mock(side_effect=[response(status=503), response({'status': '1', 'route': {'paths': [
            {'distance': '1000', 'cost': {'duration': '600', 'tolls': '0'}}]}})])
        client = AmapClient('key', http_get=get, sleeper=lambda _: None)
        planner = TripPlanner(None, '', client)
        places = {'a': {'id': 'a', 'name': 'A', 'location': '114.30,30.50'},
                  'b': {'id': 'b', 'name': 'B', 'location': '114.31,30.51'}}
        plan = {'days': [{'activities': [{'poi_id': 'a'}, {'poi_id': 'b'}]}]}
        planner._add_preferred_routes(plan, {'destination': '武汉', 'transport_preferences': {'preferred': ['driving']}}, places)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(plan['days'][0]['legs'][0]['duration_seconds'], 600)
