from copy import deepcopy
from datetime import datetime,timezone,timedelta
from dataclasses import replace
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from core.chat_transport import ChatEvent
from core.tool_result import ToolResult,TRAIN_TICKETS_TOOL
from infrastructure.memory_store import MemoryStore
from tools.trip.constraints import intercity_request,apply_constraints
from tools.trip.planner import TripPlanner,TripPlanError,REASONS
from tools.trip.tool import TripTool


NOW=datetime(2026,10,29,0,tzinfo=timezone.utc)


def train(code,origin,destination,departure,arrival,duration=120,price=100):
    return {'train_code':code,'origin_station':origin,'destination_station':destination,
            'departure_at':departure+'+08:00','arrival_at':arrival+'+08:00','duration_minutes':duration,
            'seats':[{'name':'二等座','price_cny':price,'availability':'available'}]}


class IntercityPlanningTests(unittest.TestCase):
    def test_ambiguous_return_never_submits_a_guessed_ticket_query(self):
        self.spec['intercity_request']['inbound']={'date_text':'11月2日下午','time_reference':'ambiguous'}
        result=self.tool._intercity(self.event,self.spec)
        self.assertIn('time_reference',result['inbound']['missing_fields'])
        self.assertFalse(result['inbound']['candidates'])
        self.assertEqual(len(self.calls),1,'Only the unambiguous outbound may be queried')

    def test_explicit_arrival_window_filters_evening_arrivals(self):
        self.spec['intercity_request']['inbound']={'arrival_window':'下午','time_reference':'arrival'}
        result=self.tool._intercity(self.event,self.spec)
        self.assertFalse(result['inbound']['candidates'])
        self.assertEqual(result['inbound']['request']['arrival_window'],'下午')
        self.assertNotIn('selected',result['inbound'])

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=MemoryStore(Path(self.temp.name)/'trip.db')
        self.calls=[]
        self.out=train('G1','甲城南','乙城南','2026-11-01T08:00','2026-11-01T10:00')
        self.back=train('G2','乙城南','甲城南','2026-11-02T19:00','2026-11-02T21:00')
        def query(name,args,ctx):
            self.assertEqual(name,TRAIN_TICKETS_TOOL)
            self.assertEqual(ctx.allowed_actions,frozenset({name}))
            self.calls.append((deepcopy(args),ctx))
            row=self.out if args['origin'].startswith('甲城') else self.back
            return ToolResult('completed',name,data='供应商已返回候选',payload={'status':'ok','trains':[deepcopy(row)],
                'all_trains':[deepcopy(row)],'retrieved_at':NOW.isoformat(),'provider':'12306-mcp'})
        self.planner=TripPlanner(None,'',None)
        self.tool=TripTool(self.store,self.planner,clock=lambda:NOW,rail_executor=query)
        self.event=ChatEvent('web','group','new','scope','owner','从甲城去乙城两天含来回',occurred_at=NOW)
        self.spec={'destination':'乙城','start_date':'2026-11-01','day_count':2,'no_night':True,
                   'intercity_request':{'schema_version':1,'origin':'甲城'}}
        self.places={'p1':{'id':'p1','name':'博物馆','type':'博物馆','location':'118.0,32.0'},
                     'p2':{'id':'p2','name':'公园','type':'公园','location':'118.1,32.1'}}
        self.reason=next(iter(REASONS))

    def plan(self):
        return {'days':[{'day_index':1,'date':'2026-11-01','activities':[{'poi_id':'p1','period':'下午','reason':self.reason}]},
                        {'day_index':2,'date':'2026-11-02','activities':[{'poi_id':'p2','period':'上午','reason':self.reason}]}],
                'hotel_ids':[],'restaurant_ids':[]}

    def test_two_legs_use_one_authorized_query_and_unchanged_snapshots_are_reused(self):
        result=self.tool._intercity(self.event,self.spec)
        self.assertEqual(len(self.calls),2)
        self.assertEqual(self.calls[0][0]['travel_date'],'2026-11-01')
        self.assertEqual(self.calls[1][0]['travel_date'],'2026-11-02')
        self.assertNotIn('train_preference',self.calls[0][0])
        self.assertEqual(result['outbound']['request']['mode_source'],'default')
        again=self.tool._intercity(self.event,self.spec,result)
        self.assertEqual(len(self.calls),2)
        self.assertEqual(again['outbound']['queried_at'],result['outbound']['queried_at'])
        self.assertEqual(result['ticket_cost']['total_cny'],200)

    def test_tentative_dates_missing_origin_same_city_and_explicit_modes_never_query_rail(self):
        for patch in ({'date_is_tentative':True},{'intercity_request':{'schema_version':1}},
                      {'intercity_request':{'schema_version':1,'origin':'乙城'}},
                      {'intercity_request':{'schema_version':1,'origin':'甲城','scope':'none'}},
                      {'intercity_request':{'schema_version':1,'origin':'甲城','outbound':{'mode':'driving'},'inbound':{'mode':'flight'}}}):
            spec={**deepcopy(self.spec),**patch}
            self.tool._intercity(self.event,spec)
        self.assertEqual(self.calls,[])

    def test_one_way_does_not_append_return_and_city_bus_is_not_intercity_mode(self):
        self.spec['intercity_request']['scope']='one_way'
        self.spec['transport_text']='公共交通，尽量步行'
        result=self.tool._intercity(self.event,self.spec)
        self.assertEqual(len(self.calls),1)
        self.assertNotIn('inbound',result)
        self.assertEqual(result['outbound']['request']['mode'],'rail')

    def test_removing_an_activity_preserves_selected_train_seat_and_cost(self):
        traffic=self.tool._intercity(self.event,self.spec)
        candidate={'spec':deepcopy(self.spec),'plan':{**self.plan(),'intercity':traffic},'sources':self.places}
        self.tool._remove_activity(candidate,1,'博物馆')
        self.assertEqual(candidate['plan']['intercity']['outbound']['selected'],traffic['outbound']['selected'])
        self.assertEqual(candidate['plan']['intercity']['outbound']['selected_seat']['name'],'二等座')
        self.assertEqual(candidate['plan']['intercity']['ticket_cost']['total_cny'],200)
        self.assertEqual(candidate['plan']['days'][0]['activities'],[])

    def test_shortening_transport_calendar_replans_instead_of_reusing_old_days(self):
        self.spec.update(start_date='2026-10-31',day_count=3,planning_window={
            'start':'2026-10-31','end':'2026-11-02','days':3,'start_text':'10月31日下午',
            'end_text':'11月2日下午','start_window':(12,18),'end_window':(12,18),'end_event':'departure'})
        self.spec['intercity_request'].update(outbound={'date_text':'11月1日早上'},inbound={'date_text':'11月2日下午'})
        self.back.update(departure_at='2026-11-02T16:00+08:00',arrival_at='2026-11-02T18:00+08:00')
        old=self.plan();old['days'].insert(0,{'day_index':1,'date':'2026-10-31',
            'activities':[{'poi_id':'p1','period':'晚上','reason':self.reason}]})
        for index,day in enumerate(old['days'],1):day['day_index']=index
        original=deepcopy(old);self.spec['_transport_edit']=True
        with patch.object(self.planner,'collect_places',return_value=self.places),\
             patch.object(self.planner,'plan',return_value=self.plan()) as plan:
            _,result=self.tool._generate(self.event,self.spec,old,previous_sources=self.places)
        self.assertEqual(self.spec['day_count'],2)
        self.assertEqual(len(result['days']),2)
        self.assertEqual(result['intercity']['outbound']['selected']['train_code'],'G1')
        self.assertEqual(result['intercity']['inbound']['selected']['train_code'],'G2')
        plan.assert_called_once();self.assertEqual(old,original)

    def test_mismatched_calendar_is_a_business_validation_error(self):
        traffic=self.tool._intercity(self.event,self.spec)
        self.spec['day_count']=1
        with self.assertRaises(TripPlanError) as rejected:
            self.planner.connect_transport(self.plan(),self.spec,self.places,traffic)
        self.assertEqual(rejected.exception.code,'calendar_conflict')

    def test_date_shift_replans_even_when_day_count_stays_the_same(self):
        self.spec['planning_window']={'start':'2026-11-01','end':'2026-11-02','days':2,
            'start_text':'11月1日早上','end_text':'11月2日下午','start_window':(6,10),'end_window':(12,18),'end_event':'departure'}
        self.spec['intercity_request'].update(outbound={'date_text':'11月2日早上'},inbound={'date_text':'11月3日下午'})
        self.out.update(departure_at='2026-11-02T08:00+08:00',arrival_at='2026-11-02T10:00+08:00')
        self.back.update(departure_at='2026-11-03T16:00+08:00',arrival_at='2026-11-03T18:00+08:00')
        shifted=self.plan()
        for day,stamp in zip(shifted['days'],('2026-11-02','2026-11-03')):day['date']=stamp
        self.spec['_transport_edit']=True
        with patch.object(self.planner,'collect_places',return_value=self.places),\
             patch.object(self.planner,'plan',return_value=shifted) as plan:
            _,result=self.tool._generate(self.event,self.spec,self.plan(),previous_sources=self.places)
        self.assertEqual(self.spec['day_count'],2)
        self.assertEqual([d['date'] for d in result['days']],['2026-11-02','2026-11-03'])
        plan.assert_called_once()

    def test_editing_a_past_boarding_date_retains_the_draft_and_asks_for_a_new_date(self):
        self.spec['intercity_request'].update(scope='one_way',outbound={'date_text':'2026-10-01'})
        result=self.tool._intercity(self.event,self.spec)
        self.assertEqual(self.calls,[])
        self.assertEqual(result['outbound']['missing_fields'],['travel_date'])
        self.assertIn('过去',''.join(result['limitations']))
        self.assertEqual(self.spec['destination'],'乙城')

    def test_required_seat_reselects_the_train_and_keeps_return_snapshot(self):
        self.out['seats']=[{'name':'二等座','price_cny':232,'availability':'sold_out'},
                           {'name':'商务座','price_cny':788,'availability':'available'}]
        cheaper=train('G3','甲城东','乙城南','2026-11-01T08:10','2026-11-01T10:20',130,203)
        original=self.tool.rail_executor
        def query(name,args,ctx):
            result=original(name,args,ctx)
            if args['origin']=='甲城':
                result.payload['all_trains'].append(deepcopy(cheaper))
            return result
        self.tool.rail_executor=query
        old=self.tool._intercity(self.event,self.spec)
        self.assertEqual(old['outbound']['selected']['train_code'],'G1')
        self.spec['intercity_request']['outbound']={'seat_class':'second_class','seat_requirement':'required',
                                                  'price_preference':'lower_price'}
        changed=self.tool._intercity(self.event,self.spec,old)
        self.assertEqual(changed['outbound']['selected']['train_code'],'G3')
        self.assertEqual(changed['outbound']['selected_seat']['name'],'二等座')
        self.assertEqual(changed['ticket_cost']['total_cny'],303)
        self.assertEqual(changed['inbound']['selected'],old['inbound']['selected'])
        self.assertEqual(changed['outbound']['queried_at'],old['outbound']['queried_at'])
        self.assertEqual(len(self.calls),2)

    def test_required_seat_never_silently_falls_back_to_business_or_standing(self):
        self.spec['intercity_request'].update(scope='one_way',outbound={'seat_class':'second_class'})
        self.out['seats']=[{'name':'二等座','price_cny':100,'availability':'waitlist'},
                           {'name':'商务座','price_cny':500,'availability':'available'},
                           {'name':'无座','price_cny':100,'availability':'available'}]
        result=self.tool._intercity(self.event,self.spec)
        self.assertNotIn('selected',result['outbound'])
        self.assertEqual(result['outbound']['status'],'seat_unavailable')
        self.assertIsNone(result['ticket_cost']['total_cny'])

    def test_seat_contract_does_not_pollute_sight_preferences_and_keeps_mixed_changes(self):
        from tools.trip.constraints import transport_only
        text='去程换二等座'
        changes={'intercity':{'value':{'outbound':{
            'seat_class':{'value':'second_class','source':text},
            'seat_requirement':{'value':'required','source':text}}},'source':text},
            'requirements':{'value':text,'source':text}}
        spec,_=apply_constraints(self.spec,changes,text)
        self.assertEqual(spec['intercity_request']['outbound']['seat_class'],'second_class')
        self.assertNotIn(text,spec.get('preferences',[]))
        self.assertTrue(transport_only(changes))
        changes['requirements']={'value':'少走路','source':'少走路'}
        self.assertFalse(transport_only(changes))

    def test_price_unknown_is_not_zero_and_preferred_seat_remains_explicit(self):
        from core.rail_contract import select_seat
        self.out['seats']=[{'name':'二等座','price_cny':None,'availability':'available'},
                           {'name':'商务座','price_cny':788,'availability':'available'}]
        self.spec['intercity_request'].update(scope='one_way',outbound={'seat_class':'second_class'})
        result=self.tool._intercity(self.event,self.spec)
        self.assertEqual(result['outbound']['selected_seat']['name'],'二等座')
        self.assertIsNone(result['ticket_cost']['total_cny'])
        self.out['seats'][0]['availability']='sold_out'
        seat=select_seat(self.out,{'seat_class':'second_class','seat_requirement':'preferred'})
        self.assertEqual(seat['seat_class'],'business_class')

    def test_transport_only_edit_uses_existing_plan_without_planning_or_research(self):
        self.spec['_transport_edit']=True
        self.planner.collect_places=lambda *a,**k:self.fail('No new place search')
        self.planner.plan=lambda *a,**k:self.fail('No new planning model')
        old=self.plan()
        places,plan=self.tool._generate(self.event,self.spec,old,previous_sources=self.places)
        self.assertEqual(places,self.places)
        self.assertEqual(plan['days'],old['days'])
        self.assertEqual(self.spec['planning_decision']['status'],'skipped')

    def test_seat_change_reads_full_owned_receipt_before_shortlisting_and_expiry_requeries(self):
        old=self.tool._intercity(self.event,self.spec)
        old['outbound']['candidates_complete']=False
        full=[deepcopy(self.out) for _ in range(7)]
        for row in full:row['seats']=[{'name':'商务座','price_cny':788,'availability':'available'}]
        full.append(train('G9','甲城南','乙城南','2026-11-01T08:10','2026-11-01T10:20',130,203))
        query={k:v for k,v in old['outbound']['request'].items() if k in {'origin','destination','travel_date','origin_scope','destination_scope','train_preference'}}
        query['departure_window']=[0,24]
        with patch.object(self.store.trips,'rail_snapshots',return_value=[{'request':query,'all_trains':full,
                'retrieved_at':NOW.isoformat(),'candidate_truncated':False}]) as read:
            self.spec['intercity_request']['outbound']={'seat_class':'second_class'}
            changed=self.tool._intercity(self.event,self.spec,old)
            self.assertEqual(changed['outbound']['selected']['train_code'],'G9')
            self.assertEqual(len(self.calls),2)
            read.assert_called_once()
        self.tool.clock=lambda:NOW+timedelta(minutes=6)
        self.spec['intercity_request']['outbound']['price_preference']='lower_price'
        self.tool._intercity(self.event,self.spec,changed)
        self.assertEqual(len(self.calls),3)

    def test_lower_price_never_removes_the_existing_days_activity_window(self):
        late=train('G9','甲城南','乙城南','2026-11-01T19:00','2026-11-01T23:00',240,1)
        original=self.tool.rail_executor
        def query(name,args,ctx):
            result=original(name,args,ctx)
            if args['origin']=='甲城':result.payload['all_trains'].append(late)
            return result
        self.tool.rail_executor=query
        self.spec['_transport_edit']=True
        self.spec['intercity_request']['outbound']={'seat_class':'second_class','price_preference':'lower_price'}
        _,plan=self.tool._generate(self.event,self.spec,self.plan(),previous_sources=self.places)
        self.assertEqual(plan['intercity']['outbound']['selected']['train_code'],'G1')
        self.assertEqual(plan['days'],self.plan()['days'])

    def test_late_return_arrival_cannot_claim_two_days_including_travel(self):
        self.back['arrival_at']='2026-11-03T01:00+08:00'
        result=self.tool._intercity(self.event,self.spec)
        self.assertEqual(result['completeness'],'conflict')
        self.assertEqual(self.spec['day_count'],2)

    def test_transport_days_allow_empty_but_do_not_drop_explicit_required_activities(self):
        self.out['arrival_at']='2026-11-01T23:00+08:00'
        result=self.tool._intercity(self.event,self.spec)
        self.assertIn(1,self.spec['transport_days'])
        plan=self.plan();plan['days'][0]['activities']=[]
        clean=self.planner.validate_plan(plan,self.spec,self.places)
        self.assertEqual(clean['days'][0]['kind'],'transport')
        self.spec['must_visit']=['博物馆']
        with self.assertRaises(TripPlanError):self.planner.validate_plan(plan,self.spec,self.places)

    def test_saved_ticket_uses_given_times_and_unknown_price_is_not_zero(self):
        self.spec['intercity_request'].update(scope='one_way',outbound={'mode':'owned_ticket',
            'ticket_departure':'2026-11-01 08:00','ticket_arrival':'2026-11-01 10:00','ticket_code':'G100'})
        result=self.tool._intercity(self.event,self.spec)
        self.assertEqual(self.calls,[])
        self.assertEqual(result['outbound']['status'],'provided')
        self.assertIsNone(result['ticket_cost']['total_cny'])

    def test_partial_failure_keeps_outbound_and_does_not_delete_city_plan(self):
        original=self.tool.rail_executor
        self.tool.rail_executor=lambda name,args,ctx: original(name,args,ctx) if args['origin']=='甲城' else ToolResult('failed',name,data='不可用',error_code='timeout')
        result=self.tool._intercity(self.event,self.spec)
        plan=self.plan()
        self.planner.connect_transport(plan,self.spec,self.places,result)
        self.assertEqual(result['outbound']['selected']['train_code'],'G1')
        self.assertEqual(result['inbound']['status'],'failed')
        self.assertEqual(len(plan['days']),2)
        self.assertEqual(result['completeness'],'partial')

    def test_transfer_binding_reuses_known_route_and_buffers_remain_assumptions(self):
        calls=[]
        self.planner.amap=SimpleNamespace(non_driving_route=lambda *a,**k: calls.append((a,k)) or {'duration_seconds':600})
        result=self.tool._intercity(self.event,self.spec)
        plan=self.plan();self.planner.connect_transport(plan,self.spec,self.places,result)
        self.assertEqual(len(calls),2)
        self.assertEqual(result['station_transfers']['outbound']['duration_minutes'],10)
        self.planner.connect_transport(plan,self.spec,self.places,result,deepcopy(result))
        self.assertEqual(len(calls),2)
        self.assertEqual(result['availability_windows']['1']['precision'],'period_reference')

    def test_return_change_keeps_outbound_and_uses_only_changed_leg(self):
        result=self.tool._intercity(self.event,self.spec)
        self.spec['intercity_request']['inbound']={'departure_window':'18:00至22:00'}
        newer=self.tool._intercity(self.event,self.spec,result)
        self.assertEqual(len(self.calls),3)
        self.assertEqual(newer['outbound']['selected'],result['outbound']['selected'])

    def test_bounded_return_edit_reuses_outbound_and_updates_the_calendar_window(self):
        self.out.update(departure_at='2026-11-01T09:00+08:00',arrival_at='2026-11-01T11:00+08:00')
        self.back.update(departure_at='2026-11-02T16:00+08:00',arrival_at='2026-11-02T18:00+08:00')
        self.spec['planning_window']={'start':'2026-11-01','end':'2026-11-02','days':2,
            'start_text':'11月1日上午','end_text':'11月2日下午','start_window':(9,12),'end_window':(12,18),'end_event':'departure'}
        self.spec['intercity_request'].update(outbound={'date_text':'11月1日上午'},inbound={'date_text':'11月2日下午'})
        old=self.tool._intercity(self.event,self.spec)
        self.spec['intercity_request']['inbound']['departure_window']='晚上'
        self.back.update(departure_at='2026-11-02T19:00+08:00',arrival_at='2026-11-02T21:00+08:00')
        changed=self.tool._intercity(self.event,self.spec,old)
        self.assertEqual(len(self.calls),3)
        self.assertEqual(changed['outbound']['selected'],old['outbound']['selected'])
        self.assertEqual(self.spec['planning_window']['end_window'],(18,24))

    def test_arrival_window_filters_arrivals_instead_of_departures(self):
        self.back.update(departure_at='2026-11-02T11:00+08:00',arrival_at='2026-11-02T13:00+08:00')
        self.spec['intercity_request']['inbound']={'date_text':'11月2日','arrival_window':'下午'}
        result=self.tool._intercity(self.event,self.spec)
        self.assertEqual(result['inbound']['selected']['train_code'],'G2')
        self.assertEqual(self.calls[-1][0]['departure_window'],'')

    def test_destination_days_are_preserved_with_explicit_extra_transport_dates(self):
        self.spec['intercity_request'].update(day_count_basis='destination_days',
            outbound={'date_text':'10月31日'},inbound={'date_text':'11月3日'})
        self.out.update(departure_at='2026-10-31T20:00+08:00',arrival_at='2026-11-01T06:00+08:00')
        self.back.update(departure_at='2026-11-03T10:00+08:00',arrival_at='2026-11-03T12:00+08:00')
        self.tool._intercity(self.event,self.spec)
        self.assertEqual(self.spec['destination_day_count'],2)
        self.assertEqual(self.spec['day_count'],4)

    def test_evidence_and_intracity_preferences_stay_separate(self):
        raw={'origin':{'value':'甲城','source':'从甲城出发'},'outbound':{'mode':{'value':'rail','source':'坐高铁'}}}
        result=intercity_request(raw,{},'从甲城出发坐高铁')
        spec,_=apply_constraints({'transport_preferences':{'preferred':['transit']}},
            {'intercity':{'value':raw,'source':'从甲城出发坐高铁'}},'从甲城出发坐高铁')
        self.assertEqual(result['origin'],'甲城')
        self.assertEqual(spec['transport_preferences']['preferred'],['transit'])
        with self.assertRaises(ValueError):intercity_request(raw,{},'只有乙城')

    def test_relative_dates_are_frozen_until_a_new_explicit_date_change(self):
        self.spec.update(start_date='2026-10-30')
        self.spec['intercity_request']['outbound']={'date_text':'明天'}
        self.out.update(departure_at='2026-10-30T08:00+08:00',arrival_at='2026-10-30T10:00+08:00')
        self.back.update(departure_at='2026-10-31T19:00+08:00',arrival_at='2026-10-31T21:00+08:00')
        old=self.tool._intercity(self.event,self.spec)
        later=replace(self.event,event_id='next-day',occurred_at=NOW+timedelta(days=1))
        self.tool._intercity(later,self.spec,old)
        self.assertEqual(len(self.calls),2)
        self.assertEqual(old['outbound']['request']['travel_date'],'2026-10-30')
        changed=intercity_request({'outbound':{'date_text':{'value':'明天','source':'明天'}}},self.spec['intercity_request'],'明天')
        self.assertNotIn('resolved_date',changed['outbound'])

    def test_source_restriction_does_not_reuse_an_excluded_quote(self):
        old=self.tool._intercity(self.event,self.spec)
        limited=self.tool._intercity(self.event,self.spec,old,allowed=False)
        self.assertEqual(len(self.calls),2)
        self.assertNotIn('selected',limited['outbound'])
        self.assertEqual(limited['outbound']['status'],'blocked_source')

    def test_explicit_address_uses_shared_resolution_and_only_city_goes_to_rail(self):
        self.spec['intercity_request'].update(origin='甲城测试路10号',origin_scope='address')
        routes=[]
        self.planner.amap=SimpleNamespace(non_driving_route=lambda *a,**k: routes.append((a,k)) or {'duration_seconds':600})
        with patch('infrastructure.place_resolution.resolve_route_location',return_value=SimpleNamespace(city='甲城市',coordinates='118.0,32.0')) as resolver:
            result=self.tool._intercity(self.event,self.spec)
            self.assertEqual(self.calls[0][0]['origin'],'甲城市')
            self.assertEqual(self.calls[0][0]['origin_scope'],'city')
            self.assertNotIn('测试路',str(self.calls[0][0]))
            self.planner.connect_transport(self.plan(),self.spec,self.places,result)
            self.assertIn('origin_access',result['station_transfers'])
            self.assertIn('home_return',result['station_transfers'])
            self.tool._intercity(self.event,self.spec,result)
            resolver.assert_called_once()


if __name__=='__main__':unittest.main()
