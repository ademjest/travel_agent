from copy import deepcopy
from datetime import datetime,timezone
import json
import unittest
from unittest.mock import Mock

from infrastructure.amap_client import AmapClient,AmapError
from tools.trip.constraints import apply_constraints,food_comparison_unit
from tools.trip.planner import TripPlanner
from test_draft_actions import WorkflowHarness


class RouteEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.amap=Mock();self.planner=TripPlanner(None,'unused',self.amap)
        self.places={'a':{'id':'a','name':'甲馆','location':'118.799127,32.042836'},
                     'b':{'id':'b','name':'乙馆','location':'118.799124,32.042840'}}

    def test_empty_transit_response_has_a_distinct_non_network_error(self):
        client=AmapClient('offline-fixture')
        client._get=Mock(return_value={'status':'1','infocode':'10000','count':'0','route':{'transits':[]}})
        with self.assertRaises(AmapError) as caught:
            client.route_for_pois(self.places['a'],self.places['b'],mode='transit',city='测试市')
        self.assertEqual(caught.exception.code,'route_not_found')

    def test_allowed_walking_fallback_and_same_poi_do_not_invent_zero_travel_for_distinct_places(self):
        self.amap.route_for_pois.side_effect=[AmapError('没有公交方案',code='route_not_found'),
            {'mode':'walking','duration_seconds':300,'distance_meters':180}]
        budget={'remaining':2}
        result=self.planner.route_evidence('a','b',self.places,{'destination':'测试市','transport_text':'步行或地铁'},'transit',budget=budget)
        self.assertEqual(result['mode'],'walking');self.assertEqual(result['duration_seconds'],300)
        self.assertEqual(result['fallback_from'],'transit');self.assertEqual(budget['remaining'],0)
        same=self.planner.route_evidence('a','a',self.places,{'destination':'测试市'},'transit',budget=budget)
        self.assertEqual(same['status'],'same_place');self.assertEqual(self.amap.route_for_pois.call_count,2)

    def test_forbidden_walking_and_http_failures_do_not_trigger_mode_fallback(self):
        for spec,error in [({'transport_preferences':{'forbidden':['walking']}},AmapError('没有公交方案',code='route_not_found')),
                           ({'transport_text':'步行或地铁'},AmapError('暂时失败',code='503'))]:
            self.amap.route_for_pois.reset_mock();self.amap.route_for_pois.side_effect=error
            result=self.planner.route_evidence('a','b',self.places,{'destination':'测试市',**spec},'transit',budget={'remaining':2})
            self.assertTrue(result.get('error'));self.assertEqual(self.amap.route_for_pois.call_count,1)

    def test_long_fallback_and_query_cap_remain_unverified(self):
        self.amap.route_for_pois.side_effect=[AmapError('没有公交方案',code='route_not_found'),
            {'mode':'walking','duration_seconds':7200,'distance_meters':8000}]
        result=self.planner.route_evidence('a','b',self.places,{'destination':'测试市','transport_text':'步行或地铁','gentle':True},'transit',budget={'remaining':2})
        self.assertEqual(result['error_code'],'walking_too_long');self.assertNotIn('duration_seconds',result)
        self.amap.route_for_pois.reset_mock()
        result=self.planner.route_evidence('a','b',self.places,{'destination':'测试市'},'transit',budget={'remaining':0})
        self.assertEqual(result['error_code'],'query_limit');self.amap.route_for_pois.assert_not_called()


class FoodUnitTests(unittest.TestCase):
    def test_currency_only_meal_update_inherits_person_basis(self):
        previous={'budgets':{'food':{'value':'70以内','unit_text':'人均','comparison_unit':'per_person_meal_cny'}}}
        text='第2天的早餐我希望预算降为30元以内'
        spec,_=apply_constraints(previous,{'budget':{'scope':'food','value':'30元以内','unit_text':'元','source':text,
            'scopes':[{'day_index':2,'meal':'早餐'}]}},text)
        meal=spec['budgets']['food']['meals']['2:早餐']
        self.assertEqual(food_comparison_unit(meal['unit_text']),'per_person_meal_cny')
        self.assertTrue(meal.get('unit_inherited'));self.assertEqual(meal['source'],text)
        self.assertEqual(previous['budgets']['food']['unit_text'],'人均')

    def test_explicit_total_foreign_and_unknown_basis_are_not_relabelled_as_per_person(self):
        previous={'budgets':{'food':{'value':'70以内','unit_text':'人民币每人每餐'}}}
        for unit in ('两人合计','美元每人每餐','人民币每人每天'):
            text='早餐30'+unit
            spec,_=apply_constraints(previous,{'budget':{'scope':'food','value':'30','unit_text':unit,'source':text}},text)
            self.assertNotEqual(spec['budgets']['food']['comparison_unit'],'per_person_meal_cny')
        text='早餐30元'
        spec,_=apply_constraints({}, {'budget':{'scope':'food','value':'30','unit_text':'元','source':text}},text)
        self.assertNotEqual(spec['budgets']['food']['comparison_unit'],'per_person_meal_cny')


class RouteReviewTests(WorkflowHarness,unittest.TestCase):
    def route_draft(self,critical=False):
        draft=self.create();task=self.web.store.trips.current_drafts(self.web.event(''))[0];slots=deepcopy(task.slots)
        candidate=slots['candidate_trip']
        route={'from_poi':'h','to_poi':'a','mode':'transit','queried_at':'2020-01-01T00:00:00+00:00',
               'duration_seconds':600,'distance_meters':400}
        if critical:candidate['plan']['days'][0]['legs']=[route]
        else:candidate['plan']['hotel_recommendations']=[{'poi_id':'h','routes':[{'outbound':route}]}]
        candidate['spec']['transport_text']='仅公共交通'
        with self.web.store._connect() as db:db.execute('UPDATE travel_tasks SET slots_json=?,expires_at=? WHERE task_id=?',
            (json.dumps(slots),'2000-01-01',task.task_id))
        return draft

    def test_optional_no_route_creates_partial_preview_and_invalidates_old_duration(self):
        draft=self.route_draft()
        self.amap.route_for_pois.side_effect=AmapError('没有公交方案',code='route_not_found')
        self.action(draft,'revalidate','review-no-route')
        current=self.context()['confirmations'][0]
        self.assertNotEqual(current['id'],draft['id']);self.assertTrue(current['can_confirm'])
        route=self.candidate()['plan']['hotel_recommendations'][0]['routes'][0]['outbound']
        self.assertTrue(route.get('error'));self.assertNotIn('duration_seconds',route)
        self.assertIn('仍待确认',self.web.messages()[-1]['content'])
        with self.web.store._connect() as db:
            diagnostic=json.loads(db.execute("SELECT details_json FROM execution_diagnostics WHERE stage='planning' AND json_extract(details_json,'$.name')='route_review' ORDER BY id DESC LIMIT 1").fetchone()[0])
        self.assertEqual(diagnostic['status'],'partial')

    def test_required_route_failure_keeps_old_draft_and_reports_the_segment(self):
        draft=self.route_draft(critical=True)
        self.amap.route_for_pois.side_effect=AmapError('没有公交方案',code='route_not_found')
        self.action(draft,'revalidate','review-required')
        self.assertEqual(self.context()['confirmations'][0]['id'],draft['id'])
        reply=self.web.messages()[-1]['content'];self.assertIn('湖北省博物馆',reply);self.assertIn('测试美术馆',reply)

    def test_duplicate_auxiliary_segments_and_fresh_no_route_are_not_requeried(self):
        draft=self.route_draft();task=self.web.store.trips.current_drafts(self.web.event(''))[0]
        slots=deepcopy(task.slots);routes=slots['candidate_trip']['plan']['hotel_recommendations'][0]['routes']
        routes.append(deepcopy(routes[0]))
        with self.web.store._connect() as db:db.execute('UPDATE travel_tasks SET slots_json=? WHERE task_id=?',(json.dumps(slots),task.task_id))
        self.amap.route_for_pois.side_effect=AmapError('没有公交方案',code='route_not_found')
        self.action(draft,'revalidate','duplicates')
        self.assertEqual(self.amap.route_for_pois.call_count,1)
        fresh=self.context()['confirmations'][0]
        self.action(fresh,'revalidate','fresh-negative')
        self.assertEqual(self.amap.route_for_pois.call_count,1)
        self.assertIn('仍待确认',self.web.messages()[-1]['content'])

    def test_old_partial_meal_unit_is_repaired_in_new_preview_without_changing_old_task(self):
        draft=self.create();task=self.web.store.trips.current_drafts(self.web.event(''))[0];slots=deepcopy(task.slots)
        slots['candidate_trip']['spec']['budgets']={'food':{'value':'70以内','unit_text':'人均',
            'meals':{'2:早餐':{'value':'30元以内','unit_text':'元','comparison_unit':'incomparable'}}}}
        with self.web.store._connect() as db:db.execute('UPDATE travel_tasks SET slots_json=? WHERE task_id=?',(json.dumps(slots),task.task_id))
        self.action(draft,'revalidate','review-unit')
        meal=self.candidate()['spec']['budgets']['food']['meals']['2:早餐']
        self.assertEqual(meal['comparison_unit'],'per_person_meal_cny')
        with self.web.store._connect() as db:
            old=json.loads(db.execute('SELECT slots_json FROM travel_tasks WHERE task_id=?',(task.task_id,)).fetchone()[0])
        self.assertEqual(old['candidate_trip']['spec']['budgets']['food']['meals']['2:早餐']['unit_text'],'元')
