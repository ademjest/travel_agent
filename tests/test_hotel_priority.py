from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from core.chat_transport import ChatEvent
from infrastructure.memory_store import MemoryStore
from tools.trip.constraints import apply_constraints
from tools.trip.planner import rank_hotels,price_details,_price_evidence,hotel_search_terms,TripPlanner,HOTEL_POLICY_VERSION
from tools.trip.presentation import TripAnswerWriter,validate_answer,presentation_trip
from tools.trip.tool import TripTool
from test_place_recommendations import PLAN,PLACES
from test_trip_presentation import ANSWER
from test_trips import model_response
from test_travel_agent import FakeClient
from test_draft_actions import WorkflowHarness


def spec():
    return {'destination':'测试城','hotel_requested':True,'day_count':2,
            'hotel_preferences':{'quality_requested':True,'near_itinerary':True},
            'budgets':{'accommodation':{'value':'350到450','unit_text':'人民币每间每晚'}}}


def hotel(name,cost=None,unit=None,stars=3):
    return {'id':name,'name':name,'type':f'住宿服务;宾馆酒店;{stars}星级宾馆',
            'location':'114.020,34.815','rating':'4.8','cost':cost,'price_unit':unit}


class HotelPriorityTests(unittest.TestCase):
    def test_person_average_is_not_a_room_night_price(self):
        row=hotel('酒店',400)
        details=price_details(row,'hotel')
        self.assertIsNone(details['average_price'])
        self.assertEqual(details['reference_cost'],400)
        self.assertNotEqual(_price_evidence(row,spec(),'accommodation','hotel')[0],'reference_in_budget')

    def test_invalid_or_zero_price_is_never_a_room_rate(self):
        for value in (None,'',[],0,-1,True,float('inf'),float('nan')):
            with self.subTest(value=value):
                self.assertIsNone(price_details(hotel('酒店',value,'人民币每间每晚'),'hotel')['average_price'])

    def test_incomparable_price_retains_its_actual_unit(self):
        details=price_details(hotel('外币房价酒店',400,'美元每间每晚'),'hotel')
        self.assertIsNone(details['average_price'])
        self.assertEqual(details['reference_cost_unit'],'美元每间每晚')

    def test_budget_excludes_known_expensive_hotels_before_quality_or_distance(self):
        places={**PLACES,'costly':hotel('昂贵酒店',1200,'人民币每间每晚',5),
                'fit':hotel('预算酒店',400,'人民币每间每晚',3)}
        rows=rank_hotels(spec(),PLAN,places)
        self.assertNotIn('costly',[r['poi_id'] for r in rows])
        self.assertEqual(rows[0]['poi_id'],'fit')
        self.assertEqual(rows[0]['price_status'],'reference_in_budget')

    def test_unknown_price_cannot_beat_known_budget_match_by_stars(self):
        places={'fit':hotel('参考房价酒店',400,'人民币每间每晚',3),
                'unknown':hotel('未知房价五星酒店',None,None,5),**{k:v for k,v in PLACES.items() if k in 'abcd'}}
        rows=rank_hotels(spec(),PLAN,places)
        self.assertEqual(rows[0]['poi_id'],'fit')
        self.assertNotEqual(rows[1]['price_status'],'reference_in_budget')

    def test_incomparable_units_and_minimum_only_do_not_claim_budget_match(self):
        for unit in ('美元每间每晚','人民币每人每天','人民币总价'):
            row=hotel('酒店',400,unit)
            self.assertNotEqual(_price_evidence(row,spec(),'accommodation','hotel')[0],'reference_in_budget')
        row=hotel('酒店',None,'人民币每间每晚');row['lowest_price']=200
        self.assertNotEqual(_price_evidence(row,spec(),'accommodation','hotel')[0],'reference_in_budget')
        row['lowest_price']=1000
        self.assertEqual(rank_hotels(spec(),PLAN,{'x':row}),[])

    def test_current_brand_preference_precedes_inherited_quality(self):
        s=spec();s['hotel_preferences'].update(preferred_brands=['如家'],brand_mode='preferred')
        rows=rank_hotels(s,PLAN,{'independent':hotel('五星独立酒店',None,None,5),'brand':hotel('如家酒店(测试店)',None,None,2)})
        self.assertEqual(rows[0]['poi_id'],'brand')
        self.assertEqual(rows[1]['status'],'fallback')
        self.assertIn('未匹配',''.join(rows[1]['issues']))

    def test_groups_resolve_through_reviewed_brand_data_without_company_name_match(self):
        s=spec();s['hotel_preferences'].update(preferred_groups=['华住会','如家'],brand_mode='required')
        places={'ji':hotel('全季酒店(测试店)'), 'hanting':hotel('汉庭酒店(测试店)'),
                'rujia':hotel('如家商旅酒店(测试店)'), 'false':hotel('华住会咨询公司酒店'),
                'independent':hotel('独立五星酒店',None,None,5)}
        rows=rank_hotels(s,PLAN,places)
        self.assertEqual({r['poi_id'] for r in rows},{'ji','hanting','rujia'})
        self.assertTrue(all(r.get('brand_match') for r in rows))
        self.assertTrue(all('门店' in ''.join(r['issues']) for r in rows))

    def test_group_search_terms_are_bounded_and_cover_each_requested_group(self):
        s=spec();s['hotel_preferences'].update(preferred_groups=['华住会','如家'],brand_mode='required')
        terms=hotel_search_terms(s)
        self.assertLessEqual(len(terms),8)
        self.assertTrue(any('汉庭' in t or '全季' in t for t in terms))
        self.assertTrue(any('如家' in t for t in terms))
        self.assertNotIn('华住会酒店',terms)
        s['hotel_preferences']['preferred_groups']=['尚未核实的集团']
        self.assertEqual(rank_hotels(s,PLAN,{'x':hotel('独立酒店')}),[])

    def test_group_contract_preserves_raw_evidence_and_budget(self):
        text='酒店我想住华住会旗下的酒店或者如家旗下的酒店'
        result,_=apply_constraints(spec(),{'accommodation':{'value':text,'source':text,
            'preferred_groups':['华住会','如家'],'brand_mode':'required'}},text)
        self.assertEqual(result['hotel_preferences']['preferred_groups'],['华住会','如家'])
        self.assertEqual(result['budgets'],spec()['budgets'])

    def test_new_brand_range_replaces_only_old_selectors_and_expansion_is_explicit(self):
        previous=spec();previous['hotel_preferences'].update(preferred_groups=['华住会'],brand_mode='required')
        text='酒店改住如家'
        change={'value':text,'source':text,'preferred_brands':['如家'],'brand_mode':'required'}
        updated,_=apply_constraints(previous,{'accommodation':change},text)
        self.assertNotIn('preferred_groups',updated['hotel_preferences'])
        self.assertTrue(updated['hotel_preferences']['quality_requested'])
        self.assertEqual(updated['budgets'],previous['budgets'])
        text='酒店也考虑如家';change.update(value=text,source=text,brand_policy='extend')
        extended,_=apply_constraints(previous,{'accommodation':change},text)
        self.assertEqual(extended['hotel_preferences']['preferred_groups'],['华住会'])
        self.assertEqual(extended['hotel_preferences']['preferred_brands'],['如家'])

    def test_legacy_view_reclassifies_unknown_price_and_does_not_modify_history(self):
        s=spec();s['hotel_preferences'].update(preferred_brands=['华住会','如家'],brand_mode='preferred')
        places={**PLACES,'old':hotel('独立五星酒店',None,None,5),'fitbrand':hotel('全季酒店(测试店)')}
        trip={'trip_id':'TR-test','version':1,'title':'测试','spec':s,'sources':places,
              'plan':{**deepcopy(PLAN),'hotel_ids':['old'],'hotel_recommendations':[{'poi_id':'old','status':'partial','price_status':'unknown','price_info':{}}]}}
        for day in trip['plan']['days']:day['date']=''
        trip['answer']={**ANSWER,'days':{},'meals':{},'stay':'这些酒店符合你的预算。'}
        before=deepcopy(trip);text=TripTool.format_trip(trip)
        self.assertIn('未核实',text)
        self.assertNotIn('这些酒店符合你的预算',text)
        self.assertEqual(presentation_trip(trip)['plan']['hotel_ids'],['old'])
        self.assertEqual(trip,before)

    def test_explicit_revalidation_recomputes_old_hotel_policy(self):
        s=spec();s['hotel_preferences'].update(preferred_groups=['华住会'],brand_mode='required')
        places={**PLACES,'old':hotel('独立五星酒店',None,None,5),'brand':hotel('全季酒店(测试店)')}
        plan={**deepcopy(PLAN),'hotel_ids':['old'],'hotel_recommendations':[{'poi_id':'old','price_status':'unknown'}]}
        for day in plan['days']:day['date']=''
        trip={'spec':s,'plan':plan,'sources':places}
        TripPlanner(None,'',None).reconcile_candidate(trip,deepcopy(trip))
        self.assertNotIn('old',trip['plan']['hotel_ids'])
        self.assertEqual(trip['plan']['hotel_recommendation_policy'],HOTEL_POLICY_VERSION)

    def test_writer_cannot_assert_budget_suitability_in_free_lodging_prose(self):
        trip={'spec':spec(),'sources':PLACES,'plan':deepcopy(PLAN)}
        with self.assertRaises(ValueError):validate_answer({**ANSWER,'stay':'这些酒店符合预算。'},trip)

    def test_writer_receives_candidate_budget_status(self):
        with tempfile.TemporaryDirectory() as folder:
            store=MemoryStore(Path(folder)/'writer.db');event=ChatEvent('web','group','hotel','scope','owner','调整酒店')
            store.delivery.begin_event(event.event_key);client=FakeClient([model_response(ANSWER)])
            trip={'spec':spec(),'sources':PLACES,'plan':{**deepcopy(PLAN),'hotel_ids':['h1'],
                'hotel_recommendations':rank_hotels(spec(),PLAN,PLACES)}}
            TripAnswerWriter(store,client,'test').generate(event,trip,[])
            payload=json.loads(client.completions.requests[0]['messages'][-1]['content'])
            self.assertIn('lodging',payload)
            self.assertTrue(all('price_status' in row for row in payload['lodging']['candidates']))


class HotelConfirmationTests(WorkflowHarness,unittest.TestCase):
    def test_old_hotel_preview_requires_renewal_before_exact_save(self):
        draft=self.create();task=self.web.store.trips.current_drafts(self.web.event(''))[0]
        slots=deepcopy(task.slots);candidate=slots['candidate_trip']
        candidate['spec'].update(hotel_requested=True,hotel_preferences={'preferred_brands':['汉庭'],'brand_mode':'required'},
                                 budgets={'accommodation':{'value':'350-450','unit_text':'人民币每间每晚'}})
        hotel_id='hotel-budget-fixture'
        candidate['sources'][hotel_id]=hotel('汉庭酒店(测试店)');candidate['sources'][hotel_id]['id']=hotel_id
        first=candidate['plan']['days'][0]['activities'][0]['poi_id']
        candidate['sources'][hotel_id]['location']=candidate['sources'][first].get('location','114.3,30.6')
        candidate['plan'].update(hotel_ids=[hotel_id],hotel_recommendations=[{'poi_id':hotel_id,'price_status':'unknown'}])
        candidate['plan'].pop('hotel_recommendation_policy',None)
        with self.web.store._connect() as db:db.execute('UPDATE travel_tasks SET slots_json=? WHERE task_id=?',(json.dumps(slots,ensure_ascii=False),task.task_id))
        result=self.web.client.post(f'/api/conversations/{self.web.identity}/confirmations/{draft["id"]}',headers=self.web.headers,
            json={'version':draft['version'],'client_request_id':'stale-hotel-save'})
        self.assertEqual(result.status_code,202);self.assertTrue(self.web.run_job())
        self.assertFalse(self.web.store.trips.list_for_owner(self.web.event('')))
        self.assertIn('重新核对',self.web.messages()[-1]['content'])
        self.action(draft,'revalidate','renew-hotel')
        renewed=self.context()['confirmations'][0]
        self.assertEqual(self.candidate()['plan']['hotel_recommendation_policy'],HOTEL_POLICY_VERSION)
        saved=self.save(renewed,'save-renewed-hotel')
        self.assertEqual(saved['plan']['hotel_ids'],self.web.store.trips.get(self.web.event(''),saved['trip_id'])['plan']['hotel_ids'])
