import json
from pathlib import Path
import unittest
from unittest.mock import Mock

from infrastructure.amap_client import AmapClient, normalize_poi_detail


class AttractionDetailTests(unittest.TestCase):
    def test_v5_params_and_no_duplicate_detail_call(self):
        client = AmapClient('test-key', sleeper=lambda seconds: None)
        client._get = Mock(return_value={'pois': [{'id': 'B00001', 'name': '测试馆', 'business': {'cost': '50'}}]})
        rows = client.search_poi_details('武汉', '测试馆')
        path, params = client._get.call_args.args
        self.assertEqual(path, '/v5/place/text')
        self.assertEqual(params['region'], '武汉')
        self.assertEqual(params['show_fields'], 'business,navi,children')
        self.assertEqual(params['city_limit'], 'true')
        self.assertFalse(set(params) & {'city', 'citylimit', 'offset', 'extensions'})
        self.assertEqual(client._get.call_count, 1)
        self.assertEqual(rows[0]['average_cost_raw'], '50')
        self.assertNotIn('ticket_price', rows[0])

    def test_empty_fields_and_identity_are_preserved(self):
        value = normalize_poi_detail({'id': 'B00001', 'name': '测试馆入口', 'parent': 'B00002',
            'business': {'opentime_today': [], 'opentime_week': '周二 09:00-12:00 14:00-17:00'}, 'navi': {}},
            '/v5/place/detail', '2026-10-18T16:01:00+00:00')
        self.assertIsNone(value['opening_today_raw'])
        self.assertIsNone(value['entrance_location'])
        self.assertEqual(value['parent_id'], 'B00002')
        self.assertEqual(value['field_paths']['opening_week_raw'], 'business.opentime_week')
        self.assertIn('14:00', value['opening_week_raw'])

    def test_id_response_must_match_requested_id(self):
        client = AmapClient('test', sleeper=lambda seconds: None)
        client._get = Mock(return_value={'pois': [{'id': 'B00002'}]})
        self.assertIsNone(client.get_poi_detail('B00001'))

    def test_ten_sanitized_live_samples_replay_without_network(self):
        fixture = json.loads((Path(__file__).parent / 'fixtures/amap_details_2026-10-19.json').read_text(encoding='utf-8'))
        self.assertEqual(fixture['physical_calls'], 10)
        self.assertEqual(len(fixture['cases']), 10)
        for case in fixture['cases']:
            row = case['selected']
            self.assertTrue(row['poi_id'])
            self.assertEqual(row['city'].removesuffix('市'), case['city'])
            self.assertNotIn('ticket_price', row)
            self.assertNotIn('key', row['snapshot'])
            for field, raw in (('opening_today_raw', 'opentime_today'), ('opening_week_raw', 'opentime_week')):
                self.assertEqual(row[field], row['snapshot']['business'][raw])


if __name__ == '__main__':
    unittest.main()
