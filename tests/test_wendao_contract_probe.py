from datetime import date
import io
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from scripts.probe_wendao_contract import (
    VERSION, build_request, decode_response, main, validate_contract,
)


class WendaoContractProbeTests(unittest.TestCase):
    def setUp(self):
        self.targets, self.query = build_request('small', date(2026, 10, 31))
        self.answer = [{'name': t['name'], 'introduction': '历史文化介绍',
                        'opening_hours': None, 'adult_ticket_price': None, 'reservation': None}
                       for t in self.targets]

    def validate(self, value=None):
        return validate_contract(self.answer if value is None else value, self.targets)

    def test_direct_json_and_known_transport_envelopes(self):
        raw = json.dumps(self.answer, ensure_ascii=False)
        for body in (raw, json.dumps({'result': raw}), json.dumps({'result': {'content': raw}})):
            value, _ = decode_response(body)
            self.assertTrue(self.validate(value)['contract_pass'])

    def test_whole_json_fence_and_enveloped_fence_are_accepted(self):
        raw = json.dumps(self.answer)
        fenced = '```json\n' + raw + '\n```'
        for body in (fenced, '```\n' + raw + '\n```', json.dumps({'result': {'content': fenced}})):
            value, envelope = decode_response(body)
            self.assertTrue(self.validate(value)['contract_pass'])
            self.assertIn('json_code_fence', envelope)

    def test_surrounding_prose_multiple_blocks_truncation_and_invalid_json_are_rejected(self):
        raw = json.dumps(self.answer)
        fenced = '```json\n' + raw + '\n```'
        for body in ('说明\n' + fenced, fenced + '\n总结', fenced + '\n' + fenced,
                     '```python\n' + raw + '\n```', raw[:-1], '{"a":1,"a":2}', '{"a":NaN}'):
            with self.subTest(body=body[:30]), self.assertRaises(ValueError):
                decode_response(body)

    def test_missing_duplicate_and_foreign_targets_are_rejected(self):
        variants = [self.answer[:-1], self.answer + [self.answer[0]],
                    [{**self.answer[0], 'name': '外滩'}, *self.answer[1:]], [],
                    [self.answer[0], {**self.answer[0], 'name': '<六朝博物馆>'}, self.answer[2]]]
        for entries in variants:
            self.assertFalse(self.validate(entries)['contract_pass'])
        self.answer[0]['name'] = '外滩'
        self.assertFalse(self.validate()['contract_pass'])

    def test_null_unknown_and_zero_free_are_distinct(self):
        self.assertEqual(self.validate()['known_price_target_count'], 0)
        self.answer[0]['adult_ticket_price'] = 0
        result = self.validate()
        self.assertTrue(result['contract_pass'])
        self.assertEqual(result['known_price_target_count'], 1)
        for invalid in (True, -1, '免费', float('inf')):
            self.answer[0]['adult_ticket_price'] = invalid
            self.assertFalse(self.validate()['contract_pass'])

    def test_flat_fields_reject_nested_values_missing_keys_and_old_complex_protocol(self):
        self.answer[0]['opening_hours'] = '09:00-17:00'
        result = self.validate()
        self.assertTrue(result['contract_pass'])
        self.assertEqual(result['date_applicability'], 'not_checked_by_flat_protocol')
        for invalid in ({}, {'text': '09:00-17:00', 'applicable_date': None}, '', 0, False):
            self.answer[0]['opening_hours'] = invalid
            self.assertFalse(self.validate()['contract_pass'])
        self.answer[0]['opening_hours'] = None
        self.assertFalse(self.validate([{**self.answer[0], 'additional': 1}, *self.answer[1:]])['contract_pass'])
        self.assertFalse(self.validate([{k: v for k, v in self.answer[0].items() if k != 'reservation'},
                                       *self.answer[1:]])['contract_pass'])
        self.assertFalse(self.validate({'schema_version': 'wendao-probe-v1', 'city': '南京',
                                        'entries': self.answer})['contract_pass'])
        self.assertFalse(self.validate(self.answer[0])['contract_pass'])

    def test_twelve_target_case_and_dates(self):
        targets, query = build_request('itinerary', date(2026, 10, 31))
        self.assertEqual(len(targets), 12)
        self.assertEqual(targets[-1]['visit_date'], '2026-11-03')
        self.assertTrue(all(t['name'] in query and t['visit_date'] in query for t in targets))
        self.assertIn('游玩4天', query)
        self.assertLess(len(query), 8000)

    def test_all_prompts_start_with_travel_and_use_the_same_flat_fields(self):
        fields = {'name', 'introduction', 'opening_hours', 'adult_ticket_price', 'reservation'}
        for case, count in (('single', 1), ('small', 3), ('itinerary', 12)):
            targets, query = build_request(case, date(2026, 10, 31))
            self.assertEqual(len(targets), count)
            self.assertTrue(query.startswith('我计划'))
            self.assertTrue(all(t['name'] in query for t in targets))
            for technical in ('测试结构化', '数据协议', 'schema_version', 'target_id', 'applicable_date', 'entries'):
                self.assertNotIn(technical, query)
            example = json.loads(query.split('示例一致，不要代码围栏或额外说明：')[1])
            self.assertEqual(set(example if case == 'single' else example[0]), fields)
            if case != 'single':
                self.assertIn('返回JSON数组', query)

    def test_itinerary_flat_array_covers_twelve_names_even_when_reordered(self):
        targets, _ = build_request('itinerary', date(2026, 10, 31))
        entries = [{**self.answer[0], 'name': t['name']} for t in reversed(targets)]
        result = validate_contract(entries, targets, 'itinerary')
        self.assertTrue(result['contract_pass'])
        self.assertEqual(result['received_count'], 12)
        entries.pop()
        self.assertFalse(validate_contract(entries, targets, 'itinerary')['contract_pass'])

    def test_single_prompt_target_and_date_are_consistent(self):
        targets, query = build_request('single', date(2027, 1, 2))
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0]['visit_date'], '2027-01-02')
        self.assertIn('2027年1月2日', query)
        self.assertIn(targets[0]['name'], query)
        self.assertIn('adult_ticket_price', query)
        for target in self.targets:
            self.assertIn(target['name'], self.query)

    def test_single_unknown_free_and_invalid_amounts(self):
        targets, _ = build_request('single', date(2026, 10, 31))
        value = {'name': targets[0]['name'], 'introduction': '文化介绍',
                 'opening_hours': None, 'adult_ticket_price': None, 'reservation': None}
        for amount in (None, 0, 27, 27.5):
            value['adult_ticket_price'] = amount
            result = validate_contract(value, targets, 'single')
            self.assertTrue(result['contract_pass'])
            self.assertEqual(result['known_price_target_count'], int(amount is not None))
        for amount in ('27元', True, -1, float('inf')):
            value['adult_ticket_price'] = amount
            self.assertFalse(validate_contract(value, targets, 'single')['contract_pass'])

    def test_single_name_wrapper_is_reported_but_foreign_names_and_bad_fields_are_rejected(self):
        targets, _ = build_request('single', date(2026, 10, 31))
        value = {'name': '<六朝博物馆>', 'introduction': '文化介绍',
                 'opening_hours': None, 'adult_ticket_price': None, 'reservation': None}
        result = validate_contract(value, targets, 'single')
        self.assertTrue(result['contract_pass'])
        self.assertEqual(result['format_adjustments'], ['name_angle_brackets'])
        self.assertEqual(value['name'], '<六朝博物馆>')
        for name in ('外滩', '<外滩>', '<b>六朝博物馆</b>', None):
            self.assertFalse(validate_contract({**value, 'name': name}, targets, 'single')['contract_pass'])
        self.assertFalse(validate_contract({**value, 'extra': 1}, targets, 'single')['contract_pass'])
        self.assertFalse(validate_contract({**value, 'opening_hours': {}}, targets, 'single')['contract_pass'])
        self.assertFalse(validate_contract(self.answer, targets, 'single')['contract_pass'])
        self.assertFalse(self.validate(value)['contract_pass'])

    def test_single_offline_replay_distinguishes_strict_and_compatible_results(self):
        value = {'name': '六朝博物馆', 'introduction': '文化介绍',
                 'opening_hours': '09:00-17:30', 'adult_ticket_price': None, 'reservation': '公众号预约'}
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            for mode in ('direct', 'wrapped'):
                raw = folder / (mode + '.txt')
                text = json.dumps({**value, 'name': '<六朝博物馆>'} if mode == 'wrapped' else value)
                raw.write_text('```json\n' + text + '\n```' if mode == 'wrapped' else text, encoding='utf-8')
                with patch('dotenv.dotenv_values', side_effect=AssertionError('credential read')), \
                     patch('infrastructure.public_http.request_public', side_effect=AssertionError('network')), \
                     redirect_stdout(io.StringIO()):
                    self.assertEqual(main(['--case', 'single', '--response', str(raw),
                        '--output', str(folder / mode), '--start-date', '2026-10-31']), 0)
                report = json.loads((folder / mode / 'report.json').read_text(encoding='utf-8'))
                self.assertEqual(report['strict_contract_pass'], mode == 'direct')
                self.assertEqual(report['submission_attempts'], 0)
                self.assertEqual(len(report['targets']), 1)
                self.assertEqual(report['response_source'], str(raw.resolve()))
                if mode == 'wrapped':
                    self.assertEqual(report['format_adjustments'], ['name_angle_brackets', 'json_code_fence'])

    def test_array_name_wrappers_are_compatible_without_accepting_foreign_names(self):
        entries = [{**e, 'name': '<' + e['name'] + '>'} for e in self.answer]
        result = self.validate(entries)
        self.assertTrue(result['contract_pass'])
        self.assertEqual(result['format_adjustments'], ['name_angle_brackets'])
        entries[0]['name'] = '<外滩>'
        self.assertFalse(self.validate(entries)['contract_pass'])

    def test_preview_does_not_read_credentials_or_use_network(self):
        with patch('dotenv.dotenv_values', side_effect=AssertionError('credential read')), \
             patch('infrastructure.public_http.request_public', side_effect=AssertionError('network')), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(main(['--start-date', '2026-10-31']), 0)

    def test_offline_replay_and_refusal_to_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            raw = folder / 'raw.txt'
            raw.write_text(json.dumps(self.answer), encoding='utf-8')
            args = ['--response', str(raw), '--output', str(folder / 'audit'), '--start-date', '2026-10-31']
            with patch('dotenv.dotenv_values', side_effect=AssertionError('credential read')), \
                 patch('infrastructure.public_http.request_public', side_effect=AssertionError('network')), \
                 redirect_stdout(io.StringIO()):
                self.assertEqual(main(args), 0)
                with self.assertRaises(FileExistsError):
                    main(args)
            report = json.loads((folder / 'audit' / 'report.json').read_text(encoding='utf-8'))
            self.assertEqual(report['submission_attempts'], 0)
            self.assertEqual(report['protocol_version'], VERSION)

    def test_live_makes_one_submission_and_redacts_token(self):
        raw = json.dumps([*self.answer, {'note': 'probe-secret'}], ensure_ascii=False).encode()
        with tempfile.TemporaryDirectory() as temp, \
             patch.dict('os.environ', {'WENDAO_API_KEY': 'probe-secret'}), \
             patch('infrastructure.public_http.request_public', return_value=(raw, '', 'text/plain')) as send, \
             redirect_stdout(io.StringIO()):
            self.assertEqual(main(['--live', '--output', temp + '/audit']), 1)
            send.assert_called_once()
            self.assertEqual(send.call_args.kwargs['max_attempts'], 1)
            self.assertFalse(send.call_args.kwargs['persist'])
            for file in (Path(temp) / 'audit').iterdir():
                self.assertNotIn('probe-secret', file.read_text(encoding='utf-8'))

    def test_live_transport_failure_does_not_retry(self):
        from infrastructure.public_http import PublicHTTPError
        with tempfile.TemporaryDirectory() as temp, \
             patch.dict('os.environ', {'WENDAO_API_KEY': 'probe-secret'}), \
             patch('infrastructure.public_http.request_public', side_effect=PublicHTTPError('network_error')) as send, \
             redirect_stdout(io.StringIO()):
            self.assertEqual(main(['--live', '--output', temp + '/audit']), 1)
            send.assert_called_once()
            report = json.loads((Path(temp) / 'audit' / 'report.json').read_text(encoding='utf-8'))
            self.assertEqual(report['error_code'], 'network_error')
            self.assertEqual(report['submission_attempts'], 1)
            self.assertIsNone(report['contract_pass'])

    def test_unreadable_credentials_do_not_submit_or_claim_protocol_failure(self):
        with tempfile.TemporaryDirectory() as temp, \
             patch.dict('os.environ', {'WENDAO_API_KEY': ''}), \
             patch('dotenv.dotenv_values', side_effect=PermissionError()), \
             patch('infrastructure.public_http.request_public') as send, \
             redirect_stdout(io.StringIO()):
            self.assertEqual(main(['--live', '--output', temp + '/audit']), 1)
            send.assert_not_called()
            report = json.loads((Path(temp) / 'audit' / 'report.json').read_text(encoding='utf-8'))
            self.assertEqual(report['status'], 'blocked')
            self.assertEqual(report['submission_attempts'], 0)
            self.assertIsNone(report['contract_pass'])


if __name__ == '__main__':
    unittest.main()
