from copy import deepcopy
import unittest

from tools.trip.planner import apply_recommendations, rank_hotels
from tools.trip.constraints import apply_constraints


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
    def test_hotel_quality_is_separate_from_rating_and_keeps_explicit_rating(self):
        text = '酒店好一点，优先汉庭，离每天景点近一点'
        changes = {'accommodation': {'value': text, 'source': text, 'preferred_brands': ['汉庭'],
            'brand_mode': 'preferred', 'quality_requested': True, 'near_itinerary': True}}
        spec, _ = apply_constraints({}, changes, text)
        self.assertNotIn('minimum_rating',spec['hotel_preferences'])
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
        self.assertEqual(spec['dining_preferences']['meal_overrides']['2:午餐']['minimum_rating'], 4.5)
        self.assertEqual(set(spec['budgets']), {'accommodation', 'food'})


class RecommendationTests(unittest.TestCase):
    def test_quality_uses_explicit_category_evidence_and_never_infers_stars_from_rating_or_name(self):
        places=deepcopy(PLACES)
        places['h2']['type']='住宿服务;宾馆酒店;四星级宾馆'
        places['h3']['name']='五星主题精品酒店'
        ranked=rank_hotels({'hotel_preferences':{'quality_requested':True,'chain_mode':'preferred'}},PLAN,places)
        self.assertEqual(ranked[0]['poi_id'],'h2')
        self.assertEqual(ranked[0]['category_stars'],4)
        self.assertIsNone(next(r for r in ranked if r['poi_id']=='h3')['category_stars'])

    def test_small_location_differences_do_not_erase_other_candidate_evidence(self):
        ranked=rank_hotels({'hotel_preferences':{'quality_requested':True}},PLAN,PLACES)
        self.assertEqual(ranked[0]['poi_id'],'h3')
        places=deepcopy(PLACES)
        places['h3']['location']='115.0,34.8'
        ranked=rank_hotels({'hotel_preferences':{'quality_requested':True}},PLAN,places)
        self.assertNotEqual(ranked[0]['poi_id'],'h3')

    def test_quality_scope_uses_provider_lodging_category_instead_of_rating_or_name(self):
        places=deepcopy(PLACES)
        places['hostel']={'id':'hostel','name':'高分酒店青年店','type':'住宿服务;青年旅舍','rating':5,'location':'114.020,34.815'}
        places['unknown']={'id':'unknown','name':'舒适酒店','type':'住宿服务','rating':5,'location':'114.020,34.815'}
        ranked=rank_hotels({'hotel_preferences':{'quality_requested':True}},PLAN,places)
        self.assertTrue(ranked)
        self.assertFalse({'hostel','unknown'}&{r['poi_id'] for r in ranked})
        preferred=rank_hotels({'hotel_preferences':{'quality_requested':True,'lodging_types':['酒店'],
            'lodging_type_mode':'preferred'}},PLAN,places)
        self.assertFalse({'hostel','unknown'}&{r['poi_id'] for r in preferred})
        self.assertTrue(all(any('评分不代表档次' in issue for issue in r['issues']) for r in ranked))
        chosen=rank_hotels({'hotel_preferences':{'quality_requested':True,'lodging_types':['青年旅舍'],
            'lodging_type_mode':'required'}},PLAN,places)
        self.assertEqual([r['poi_id'] for r in chosen],['hostel'])
        self.assertEqual(rank_hotels({'hotel_preferences':{'quality_requested':True}},PLAN,{'unknown':places['unknown']}),[])

    def test_brand_rating_and_all_days_distance_determine_hotel_order(self):
        spec = {'hotel_requested': True, 'hotel_preferences': {'preferred_brands': ['汉庭'],
            'brand_mode': 'required', 'chain_mode': 'required', 'minimum_rating': 4.7,
            'rating_mode': 'required', 'near_itinerary': True},
            'budgets': {'accommodation': {'value': '200-300', 'unit_text': '人民币每间每晚'}}}
        places=deepcopy(PLACES)
        places['h1']['price_unit']='人民币每间每晚'  # This fixture explicitly supplies a room-night reference, unlike Amap cost.
        ranked = rank_hotels(spec, deepcopy(PLAN), places)
        self.assertEqual(ranked[0]['poi_id'], 'h1')
        self.assertEqual(ranked[0]['status'], 'partial')
        self.assertTrue(any('指定日期房价待核验' in issue for issue in ranked[0]['issues']))
        self.assertEqual(ranked[0]['price_info'], {'average_price': 260.0, 'lowest_price': 238.0,'basis':'per_room_night_cny'})
        self.assertEqual(len(ranked),1,'Hard conflicts cannot be recommended as substitutes')
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
        self.assertEqual(len(option['candidates']),1)

    def test_distant_chain_cannot_outrank_nearby_independent_by_brand_alone(self):
        places=deepcopy(PLACES)
        places['h1']['location']='115.020,34.815'
        spec={'hotel_preferences':{'chain_mode':'preferred'}}
        ranked=rank_hotels(spec,PLAN,places)
        self.assertNotEqual(ranked[0]['poi_id'],'h1')
        self.assertEqual(ranked[-1]['poi_id'],'h1')

    def test_scoped_meal_change_does_not_become_a_global_diet_constraint(self):
        spec={'restaurant_requested':True,'dining_preferences':{'allergies':['花生']}}
        text='第二天午餐清淡一点'
        spec,_=apply_constraints(spec,{'dining':{'value':text,'source':text,'keywords':['清淡'],
            'scopes':[{'day_index':2,'meal':'午餐'}]}},text)
        self.assertNotIn('keywords',spec['dining_preferences'])
        self.assertEqual(spec['dining_preferences']['allergies'],['花生'])
        self.assertEqual(spec['dining_preferences']['meal_overrides']['2:午餐']['keywords'],['清淡'])

    def test_meals_follow_arrival_and_departure_window(self):
        plan=deepcopy(PLAN)
        plan['days'][0]['date']='2030-10-01';plan['days'][1]['date']='2030-10-02'
        spec={'restaurant_requested':True,'intercity_windows':{
            '1':{'earliest':'2030-10-01T18:30+08:00','latest':'2030-10-02T00:00+08:00'},
            '2':{'earliest':'2030-10-02T00:00+08:00','latest':'2030-10-02T10:00+08:00'}}}
        plan=apply_recommendations(spec,plan,PLACES)
        self.assertEqual([(x['day_index'],x['meal']) for x in plan['dining_options']],[(1,'晚餐'),(2,'早餐')])


if __name__ == '__main__':
    unittest.main()
