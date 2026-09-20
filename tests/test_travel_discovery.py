import unittest
from unittest.mock import Mock, patch

from agents.travel_decision import decide_travel_action
from infrastructure.amap_client import AmapClient, AmapError
from core.settings import Settings
from services.travel_service import TravelService
from test_amap_client import geocode_response


class TravelDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.client = AmapClient('test-key')
        self.client._get = Mock()

    def test_place_search_scopes_city_and_does_not_invent_inventory(self):
        self.client._get.return_value = {'pois': [
            {'id': 'P1', 'name': '测试博物馆', 'address': '测试路 1 号', 'location': '114,30', 'type': '博物馆', 'tel': []},
        ]}
        places = self.client.search_places('武汉', '博物馆')
        self.assertEqual(places[0]['name'], '测试博物馆')
        self.assertNotIn('tel', places[0])
        self.assertEqual(self.client._get.call_args.args[1]['citylimit'], 'true')
        self.assertNotIn('price', places[0])

    def test_walking_route_uses_walking_endpoint(self):
        self.client._get.side_effect = [
            geocode_response('起点', '114,30', '420100'), geocode_response('终点', '114.1,30.1', '420100'),
            {'route': {'paths': [{'distance': '1200', 'duration': '1000', 'steps': [{'instruction': '沿测试路步行'}]}]}},
        ]
        route = self.client.non_driving_route('起点', '终点', mode='walking')
        self.assertEqual(route['duration_seconds'], 1000)
        self.assertEqual(route['instructions'], ('沿测试路步行',))
        self.assertEqual(self.client._get.call_args.args[0], '/v3/direction/walking')

    def test_transit_route_preserves_bus_and_walking_segments(self):
        self.client._get.side_effect = [
            geocode_response('起点', '114,30', '420100'), geocode_response('终点', '114.1,30.1', '420100'),
            {'route': {'distance': '8000', 'transits': [{'duration': '1800', 'segments': [
                {'walking': {'distance': '500'}, 'bus': {'buslines': [
                    {'name': '地铁测试线', 'departure_stop': {'name': '甲站'}, 'arrival_stop': {'name': '乙站'}}]}}
            ]}]}},
        ]
        route = self.client.non_driving_route('起点', '终点', mode='transit', city='武汉')
        self.assertEqual(route['instructions'], ('步行约 500 米', '地铁测试线：甲站 → 乙站'))
        self.assertEqual(self.client._get.call_args.args[1]['city'], '武汉')

    def test_explicit_modes_exclude_driving_tool(self):
        for text, expected in (
            ('武汉站到湖北省博物馆坐地铁怎么走', 'get_transit_route'),
            ('武汉黄鹤楼到户部巷步行怎么走', 'get_walking_route'),
            ('推荐武汉的博物馆', 'search_travel_places'),
        ):
            with self.subTest(text=text):
                decision = decide_travel_action(text)
                self.assertEqual(decision.allowed_tools, (expected,))

    def test_service_labels_empty_search_and_real_data_limits(self):
        service = TravelService(Settings('', '', frozenset(), '', '', '', ''))
        service.amap = Mock()
        service.amap.search_places.return_value = ()
        reply = service.execute_tool('search_travel_places', {'city': '武汉', 'keywords': '酒店'})
        self.assertIn('未找到', reply)
        self.assertIn('不代表', reply)

    def test_no_transit_result_is_an_error(self):
        self.client._get.side_effect = [geocode_response('甲', '114,30', '420100'),
            geocode_response('乙', '115,31', '420100'), {'route': {'transits': []}}]
        with self.assertRaises(AmapError):
            self.client.non_driving_route('甲', '乙', mode='transit', city='武汉')

    def test_place_qps_error_retries_with_spacing_but_auth_error_does_not(self):
        self.client._get.side_effect = [AmapError('limited', code='10021'), {'pois': []}]
        with patch('infrastructure.amap_client.time.sleep') as sleep:
            self.assertEqual(self.client.search_places('武汉', '博物馆'), ())
            self.assertTrue(sleep.called)
        self.assertEqual(self.client._get.call_count, 2)
        self.client._get.reset_mock(side_effect=True)
        self.client._get.side_effect = AmapError('invalid key', code='10001')
        with patch('infrastructure.amap_client.time.sleep'), self.assertRaises(AmapError):
            self.client.search_places('武汉', '博物馆')
        self.assertEqual(self.client._get.call_count, 1)

    def test_place_rate_limit_has_bounded_attempts(self):
        self.client._get.side_effect = AmapError('limited', code='10021')
        with patch('infrastructure.amap_client.time.sleep'), self.assertRaises(AmapError):
            self.client.search_places('武汉', '博物馆')
        self.assertEqual(self.client._get.call_count, 3)

    def test_trip_route_uses_verified_poi_coordinates_without_geocoding_again(self):
        self.client.geocode = Mock(side_effect=AssertionError('must not geocode'))
        self.client._get.return_value = {'route': {'paths': [{'distance': '600', 'duration': '500', 'steps': []}]}}
        route = self.client.route_for_pois({'name': '甲', 'location': '114,30'},
                                         {'name': '乙', 'location': '114.1,30.1'}, mode='walking', city='武汉')
        self.assertEqual(route['duration_seconds'], 500)
        self.assertEqual(self.client._get.call_args.args[1]['origin'], '114.000000,30.000000')
