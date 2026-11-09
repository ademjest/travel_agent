from copy import deepcopy
from datetime import datetime, timezone
import json
import unittest
from unittest.mock import Mock, patch

from agents.intent_compiler import IntentCompiler, IntentCompilationError
from core.intent_ir import parse_intent_ir
from tools.trip.constraints import apply_constraints, intercity_request
from tools.trip.planner import TripPlanner
from test_travel_agent import FakeClient
from test_trips import model_response
from test_draft_actions import WorkflowHarness
from test_intercity_planning import train
from core.tool_result import ToolResult


REQUEST = '现在需要补充往返交通。我从上海出发，去程希望2026年11月7日上午到南京，返程希望2026年11月9日18点前回到上海。优先高铁二等座，请按这个时间约束核对行程，先给我预览，不要直接保存。'
HOTEL = '交通条件先保持，只把酒店预算改为人民币每间每晚350到450元，餐饮要求保持。不要重新查询车票。'
NOW = datetime(2026, 11, 4, 0, tzinfo=timezone.utc)


def field(value, source):
    return {'value': value, 'source': source}


def traffic_patch(year=2026):
    return {'origin': field('上海', '我从上海出发'), 'origin_scope': field('city', '我从上海出发'),
            'scope': field('round_trip', '补充往返交通'),
            'outbound': {'date_text': field(f'{year}年11月7日', f'{year}年11月7日上午到南京'),
                         'arrival_window': field('上午', '上午到南京'),
                         'mode': field('rail', '优先高铁二等座'),
                         'train_preference': field('high_speed', '优先高铁二等座'),
                         'seat_class': field('second_class', '二等座')},
            'inbound': {'date_text': field(f'{year}年11月9日', f'{year}年11月9日18点前回到上海'),
                        'arrival_before': field('18点前', '18点前回到上海'),
                        'mode': field('rail', '优先高铁二等座'),
                        'train_preference': field('high_speed', '优先高铁二等座'),
                        'seat_class': field('second_class', '二等座')}}


def intent(changes=None, text=REQUEST, year=2026):
    changes = changes if changes is not None else {'intercity': {'value': traffic_patch(year), 'source': text}}
    return {'schema_version': 1, 'action': 'update', 'operations': [
        {'domain': 'trip', 'operation': 'update_constraints', 'target_ref': 'current_trip', 'changes': changes}],
        'requires_confirmation': True, 'source_spans': [text], 'missing_fields': []}


def rewritten_intent(text=REQUEST, year=2026):
    result = intent(text=text, year=year)
    result['operations'][0]['changes']['intercity']['value']['inbound']['arrival_before']['value'] = '18:00'
    return result


class IntercityContractTests(unittest.TestCase):
    def test_nonverbatim_clock_is_rejected_before_domain_execution_with_path(self):
        with self.assertRaises(ValueError) as caught:
            parse_intent_ir(rewritten_intent(), REQUEST)
        self.assertEqual(getattr(caught.exception, 'code', None), 'value_not_in_source')
        self.assertEqual(getattr(caught.exception, 'path', None), 'intercity.inbound.arrival_before')

    def test_field_failure_is_identifiable_without_recording_private_content(self):
        value = traffic_patch(); value['inbound']['arrival_before']['source'] = '私人地址缺少匹配'
        with self.assertRaises(ValueError) as caught:
            intercity_request(value, {}, REQUEST)
        self.assertEqual(getattr(caught.exception, 'code', None), 'source_not_in_message')
        self.assertEqual(getattr(caught.exception, 'path', None), 'intercity.inbound.arrival_before')
        self.assertNotIn('私人地址', str(caught.exception))

    def test_clock_and_date_format_errors_get_exactly_one_existing_compiler_repair(self):
        for direction, name, replacement in [('inbound', 'arrival_before', '18:00'),
                                              ('outbound', 'date_text', '2026-11-07')]:
            invalid = intent()
            invalid['operations'][0]['changes']['intercity']['value'][direction][name]['value'] = replacement
            client = FakeClient([model_response(invalid), model_response(intent())]); diagnostics = Mock()
            result = IntentCompiler(client, 'test', diagnostics).try_compile(REQUEST)
            self.assertEqual(len(client.completions.requests), 2)
            self.assertEqual(result.operations[0].values['changes'], intent()['operations'][0]['changes'])
            repair = json.loads(client.completions.requests[1]['messages'][-1]['content'])
            self.assertIn('intercity.' + direction + '.' + name, repair['contract_error'])
            self.assertEqual(repair['message'], REQUEST)
            details = diagnostics.record.call_args_list[0].args[1]
            self.assertEqual(details['validation'], {'code': 'value_not_in_source', 'path': 'intercity.' + direction + '.' + name})

    def test_repair_exhaustion_does_not_start_another_model_or_hide_the_field(self):
        client = FakeClient([model_response(rewritten_intent()), model_response(rewritten_intent())])
        diagnostics = Mock()
        with patch.dict('os.environ', {'LLM_INTENT_MODEL_ID': 'other-model'}):
            with self.assertRaises(IntentCompilationError) as caught:
                IntentCompiler(client, 'default', diagnostics).try_compile(REQUEST)
        self.assertEqual(len(client.completions.requests), 2)
        self.assertIn('返程', getattr(caught.exception, 'detail', ''))
        self.assertIn('到达', caught.exception.detail)
        self.assertTrue(any(c.args[1].get('status') == 'intent_failed' for c in diagnostics.record.call_args_list))

    def test_outer_evidence_and_unknown_fields_cannot_bypass_preflight(self):
        for edit in ('source', 'field', 'enum'):
            raw = intent(); change = raw['operations'][0]['changes']['intercity']
            if edit == 'source':change['source'] = '用户没有说过的要求'
            elif edit == 'field':change['value']['inbound']['unexpected'] = field('18点', '18点前回到上海')
            else:change['value']['outbound']['mode']['value'] = 'high_speed'
            with self.subTest(edit=edit), self.assertRaises(ValueError):parse_intent_ir(raw, REQUEST)

    def test_seat_enum_contract_is_supplied_and_repair_has_valid_choices(self):
        invalid = intent()
        invalid['operations'][0]['changes']['intercity']['value']['outbound']['seat_class']['value'] = '二等座'
        client = FakeClient([model_response(invalid), model_response(intent())])
        result = IntentCompiler(client, 'test').try_compile(REQUEST)
        prompt = client.completions.requests[0]['messages'][0]['content']
        self.assertIn('"second_class": "二等座"', prompt)
        self.assertIn('"seat_requirement": ["preferred", "required"]', prompt)
        repair = json.loads(client.completions.requests[1]['messages'][-1]['content'])['contract_error']
        self.assertIn('second_class', repair)
        self.assertEqual(result.operations[0].values['changes']['intercity']['value']['outbound']['seat_class']['value'], 'second_class')

    def test_original_evidence_survives_server_calendar_and_arrival_normalization(self):
        ir = parse_intent_ir(intent(), REQUEST)
        previous = {'destination': '南京', 'start_date': '2026-11-07', 'day_count': 3,
                    'intercity_request': {'scope': 'none'}, 'budgets': {'food': {'value': '60元以内'}}}
        original = deepcopy(previous)
        spec, _ = apply_constraints(previous, ir.operations[0].values['changes'], REQUEST)
        legs = TripPlanner.intercity_legs(spec, NOW)
        self.assertEqual(legs['outbound']['travel_date'], '2026-11-07')
        self.assertEqual(legs['inbound']['travel_date'], '2026-11-09')
        self.assertEqual(legs['outbound']['arrival_window'], '上午')
        self.assertEqual(legs['inbound']['arrival_before'], '18点前')
        self.assertEqual(legs['inbound']['destination'], '上海')
        self.assertFalse(legs['outbound']['departure_window'])
        self.assertEqual(spec['intercity_request']['inbound']['field_sources']['arrival_before'], '18点前回到上海')
        self.assertEqual(previous, original)
        self.assertEqual(spec['budgets'], previous['budgets'])


class IntercityWorkflowTests(WorkflowHarness, unittest.TestCase):
    reference_time = NOW.replace(year=2030)

    def setUp(self):
        super().setUp()
        self.year = self.reference_time.year
        self.request = REQUEST.replace('2026', str(self.year))
        self.tool.clock = lambda: self.reference_time
        self.models.completions.responses.clear()
        sources = {f'p{i}': {'id': f'p{i}', 'name': f'合成文化景点{i}', 'type': '风景名胜;博物馆',
                             'location': f'118.{70+i},32.0', 'address': '合成地址'} for i in (1, 2, 3)}
        self.amap.search_places.return_value = list(sources.values())
        seed = self.web.event('合成已有南京三日行程，仅用于交通增量测试', 'synthetic-seed')
        candidate = {'trip_id': self.web.store.trips.new_id(seed.event_key), 'version': 1, 'title': '合成南京三日行程',
                     'status': 'active', 'sources': sources,
                     'spec': {'destination': '南京', 'start_date': f'{self.year}-11-07', 'end_date': f'{self.year}-11-09', 'day_count': 3,
                              'intercity_request': {'scope': 'none'}, 'preferences': [],
                              'budgets': {'food': {'value': '60元以内', 'unit_text': '人均'}}},
                     'plan': {'days': [{'day_index': i, 'date': f'{self.year}-11-0' + str(i+6),
                                       'activities': [{'poi_id': f'p{i}', 'period': '下午' if i == 1 else '上午',
                                                       'reason': '文化参观'}]} for i in (1, 2, 3)],
                              'hotel_ids': [], 'restaurant_ids': []}}
        claim = self.web.store.delivery.begin_event(seed.event_key)
        self.tool._preview_candidate(seed, claim, candidate)
        self.saved = self.save(self.context()['confirmations'][0], 'save-seed')
        self.rail_calls = []
        self.rail_rows = {
            '上海': [train('G101', '上海虹桥', '南京南', f'{self.year}-11-07T08:00', f'{self.year}-11-07T10:00'),
                     train('G103', '上海虹桥', '南京南', f'{self.year}-11-07T09:00', f'{self.year}-11-07T13:00')],
            '南京': [train('G102', '南京南', '上海虹桥', f'{self.year}-11-09T15:30', f'{self.year}-11-09T17:30'),
                     train('G104', '南京南', '上海虹桥', f'{self.year}-11-09T17:00', f'{self.year}-11-09T19:00')]}
        def query(name, arguments, context):
            self.rail_calls.append(deepcopy(arguments))
            rows = deepcopy(self.rail_rows[arguments['origin']])
            return ToolResult('completed', name, payload={'status': 'ok', 'all_trains': rows, 'trains': rows,
                              'retrieved_at': self.reference_time.isoformat(), 'provider': 'offline_fixture'})
        self.tool.rail_executor = query

    def use_compiler(self, replies):
        client = FakeClient([model_response(reply) for reply in replies])
        self.router.compiler = IntentCompiler(client, 'test', self.web.store.diagnostics)
        return client

    def test_real_compiler_queue_arrival_preview_then_hotel_without_rail_requery(self):
        hotel = intent({'budget': {'scope': 'accommodation', 'value': '350到450',
                                   'unit_text': '人民币每间每晚', 'source': '人民币每间每晚350到450元'}}, HOTEL)
        client = self.use_compiler([rewritten_intent(self.request, self.year), intent(text=self.request, year=self.year), hotel])
        response = self.web.send(self.request, 'transport'); self.assertEqual(response.status_code, 202)
        self.assertTrue(self.web.run_job())
        self.assertTrue(self.context()['confirmations'], self.web.messages()[-1]['content'])
        candidate = self.candidate(); traffic = candidate['plan']['intercity']
        self.assertEqual(traffic['outbound']['selected']['train_code'], 'G101')
        self.assertEqual(traffic['inbound']['selected']['train_code'], 'G102')
        self.assertEqual(len(self.rail_calls), 2)
        self.assertEqual(self.web.store.trips.get(self.web.event(''), self.saved['trip_id']), self.saved)
        with self.web.store._connect() as db:
            details = [json.loads(row[0]) for row in db.execute("SELECT details_json FROM execution_diagnostics WHERE stage='routing'")]
        self.assertTrue(any(row.get('validation', {}).get('path') == 'intercity.inbound.arrival_before' for row in details))
        self.web.send(HOTEL, 'hotel'); self.assertTrue(self.web.run_job())
        updated = self.candidate()
        self.assertEqual(updated['spec']['budgets']['accommodation']['value'], '350到450')
        self.assertEqual(updated['spec']['budgets']['food'], candidate['spec']['budgets']['food'])
        self.assertEqual(updated['plan']['days'], candidate['plan']['days'])
        self.assertEqual(updated['plan']['intercity'], traffic)
        self.assertEqual(len(self.rail_calls), 2)
        self.assertEqual(len(client.completions.requests), 3)
        self.assertEqual(self.web.store.trips.get(self.web.event(''), self.saved['trip_id']), self.saved)

    def test_failed_repair_is_explained_and_has_no_business_effect(self):
        client = self.use_compiler([rewritten_intent(self.request, self.year), rewritten_intent(self.request, self.year)])
        self.web.send(self.request, 'failed'); self.assertTrue(self.web.run_job())
        reply = self.web.messages()[-1]['content']
        self.assertIn('返程的最晚到达时间', reply)
        self.assertIn('一次自动修复', reply)
        self.assertFalse(self.context()['confirmations'])
        self.assertFalse(self.rail_calls)
        self.assertEqual(len(client.completions.requests), 2)
        self.assertEqual(self.web.store.trips.get(self.web.event(''), self.saved['trip_id']), self.saved)


if __name__ == '__main__':
    unittest.main()
