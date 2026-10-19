from copy import deepcopy
import unittest

from core.place_recommendations import apply_recommendations, rank_hotels
from core.trip_updates import apply_constraints


PLAN = {'days': [
    {'day_index': 1, 'activities': [
        {'poi_id': 'a', 'period': '上午', 'reason': '兴趣匹配'},
        {'poi_id': 'b', 'period': '下午', 'reason': '兴趣匹配'}]},
    {'day_index': 2, 'activities': [
        {'poi_id': 'c', 'period': '上午', 'reason': '兴趣匹配'},
        {'poi_id': 'd', 'period': '下午', 'reason': '兴趣匹配'}]},
]}

PLACES = {
    'a': {'id': 'a', 'name': '景点甲', 'type': '风景名胜', 'location': '114.000,34.800'},
    'b': {'id': 'b', 'name': '景点乙', 'type': '博物馆', 'location': '114.020,34.810'},
    'c': {'id': 'c', 'name': '景点丙', 'type': '公园', 'location': '114.030,34.820'},
    'd': {'id': 'd', 'name': '景点丁', 'type': '风景名胜', 'location': '114.040,34.830'},
    'h1': {'id': 'h1', 'name': '汉庭酒店（测试店）', 'type': '住宿服务;酒店',
           'location': '114.020,34.815', 'rating': '4.8', 'cost': '260', 'lowest_price': '238',
           'address': '中心路'},
    'h2': {'id': 'h2', 'name': '汉庭酒店（低评分店）', 'type': '住宿服务;酒店',
           'location': '114.015,34.812', 'rating': '4.6', 'address': '近景点路'},
    'h3': {'id': 'h3', 'name': '独立精品酒店', 'type': '住宿服务;酒店',
           'location': '114.021,34.815', 'rating': '4.9', 'address': '中心路'},
    'r1': {'id': 'r1', 'name': '开封当地菜馆', 'type': '餐饮服务;中餐厅',
           'location': '114.012,34.806', 'rating': '4.6', 'cost': '55',
           'queried_keyword': '开封当地菜餐厅', 'address': '美食路'},
    'r2': {'id': 'r2', 'name': '普通餐厅', 'type': '餐饮服务;中餐厅',
           'location': '114.010,34.805', 'rating': '4.4', 'cost': '45', 'address': '近路'},
}


class ConstraintPreferenceTests(unittest.TestCase):
    def test_hotel_high_quality_uses_default_47_and_keeps_explicit_rating(self):
        text = '酒店好一点，优先汉庭，离每天景点近一点'
        changes = {'accommodation': {'value': text, 'source': text, 'preferred_brands': ['汉庭'],
            'brand_mode': 'preferred', 'quality_requested': True, 'near_itinerary': True}}
        spec, _ = apply_constraints({}, changes, text)
        self.assertEqual(spec['hotel_preferences']['minimum_rating'], 4.7)
        self.assertEqual(spec['hotel_preferences']['rating_origin'], 'default_high_quality')
        explicit = '酒店至少4.5分'
        spec, _ = apply_constraints(spec, {'accommodation': {'value': explicit, 'source': explicit,
            'minimum_rating': 4.5}}, explicit)
        followup = '继续找离景点近一点的酒店'
        spec, _ = apply_constraints(spec, {'accommodation': {'value': followup, 'source': followup,
            'quality_requested': True, 'near_itinerary': True}}, followup)
        self.assertEqual(spec['hotel_preferences']['minimum_rating'], 4.5)
        self.assertEqual(spec['hotel_preferences']['rating_origin'], 'user')

    def test_dining_default_rating_scopes_and_two_budgets_are_preserved(self):
        text = '第二天午餐想吃当地菜，餐厅评价好；酒店300元，人民币每间每晚；吃饭60元，人民币每人每餐'
        changes = {'dining': {'value': '第二天午餐想吃当地菜，餐厅评价好',
            'source': '第二天午餐想吃当地菜，餐厅评价好', 'cuisines': ['当地菜'],
            'quality_requested': True, 'near_itinerary': True,
            'scopes': [{'day_index': 2, 'meal': '午餐'}]},
            'budgets': [
                {'value': '300元', 'scope': 'accommodation', 'unit_text': '人民币每间每晚', 'source': '酒店300元'},
                {'value': '60元', 'scope': 'food', 'unit_text': '人民币每人每餐', 'source': '吃饭60元'},
            ]}
        spec, _ = apply_constraints({}, changes, text)
        self.assertEqual(spec['dining_preferences']['minimum_rating'], 4.5)
        self.assertEqual(spec['dining_preferences']['scopes'], [{'day_index': 2, 'meal': '午餐'}])
        self.assertEqual(set(spec['budgets']), {'accommodation', 'food'})


class RecommendationTests(unittest.TestCase):
    def test_brand_rating_and_all_days_distance_determine_hotel_order(self):
        spec = {'hotel_requested': True, 'hotel_preferences': {'preferred_brands': ['汉庭'],
            'brand_mode': 'required', 'chain_mode': 'required', 'minimum_rating': 4.7,
            'rating_mode': 'required', 'near_itinerary': True},
            'budgets': {'accommodation': {'value': '200-300', 'unit_text': '人民币每间每晚'}}}
        ranked = rank_hotels(spec, deepcopy(PLAN), PLACES)
        self.assertEqual(ranked[0]['poi_id'], 'h1')
        self.assertEqual(ranked[0]['status'], 'partial')
        self.assertTrue(any('指定日期房价待核验' in issue for issue in ranked[0]['issues']))
        self.assertEqual(ranked[0]['price_info'], {'average_price': 260.0, 'lowest_price': 238.0})
        self.assertEqual(ranked[1]['status'], 'fallback')
        self.assertTrue(ranked[0]['distance']['daily'])

    def test_dining_is_bound_to_requested_day_and_meal_with_reference_budget(self):
        spec = {'restaurant_requested': True, 'dining_preferences': {'cuisines': ['当地菜'],
            'cuisine_mode': 'preferred', 'minimum_rating': 4.5, 'rating_mode': 'required',
            'near_itinerary': True, 'scopes': [{'day_index': 1, 'meal': '午餐'}]},
            'budgets': {'food': {'value': '60', 'unit_text': '人民币每人每餐'}}}
        plan = apply_recommendations(spec, deepcopy(PLAN), PLACES)
        self.assertEqual(plan['restaurant_ids'], [])
        self.assertEqual(len(plan['dining_options']), 1)
        option = plan['dining_options'][0]
        self.assertEqual((option['day_index'], option['meal']), (1, '午餐'))
        self.assertEqual(option['candidates'][0]['poi_id'], 'r1')
        self.assertEqual(option['candidates'][0]['price_status'], 'reference_in_budget')
        self.assertEqual(option['candidates'][0]['price_info']['per_capita'], 55.0)
        self.assertEqual(option['candidates'][1]['status'], 'fallback')


if __name__ == '__main__':
    unittest.main()
