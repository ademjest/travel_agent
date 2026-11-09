from copy import deepcopy
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import json
import tempfile
import unittest

from core.chat_transport import ChatEvent
from core.intercity_contract import intercity_request
from core.tool_result import ToolResult
from infrastructure.memory_store import MemoryStore
from tools.trip.planner import TripPlanner
from tools.trip.tool import TripTool
from tools.trip.presentation import render_answer, TripAnswerWriter, validate_answer
from test_trip_presentation import ANSWER
from test_trips import model_response
from test_travel_agent import FakeClient

FIXTURE = json.loads((Path(__file__).parent/'fixtures/rail_window_2026-11-04.json').read_text(encoding='utf-8'))
NOW = datetime.fromisoformat(FIXTURE['reference_time'])


class RailWindowRepairTests(unittest.TestCase):
    def test_incremental_departure_and_arrival_limits_coexist_and_clear_independently(self):
        old={'inbound':{'arrival_before':'18点前','field_sources':{'arrival_before':'18点前'}}}
        value={'inbound':{'departure_window':{'value':'14:00至16:00','source':'14:00至16:00出发'},
                          'time_reference':{'value':'departure','source':'出发'}}}
        result=intercity_request(value,old,'14:00至16:00出发，其他条件保持')
        self.assertEqual(result['inbound']['arrival_before'],'18点前')
        value['inbound']['arrival_before']={'value':'18:00前','source':'18:00前到达'}
        result=intercity_request(value,old,'14:00至16:00出发，18:00前到达')
        self.assertEqual(result['inbound']['arrival_before'],'18:00前')
        cleared=intercity_request({'inbound':{'arrival_before':{'value':'','source':'取消到达截止'}}},result,'取消到达截止')
        self.assertNotIn('arrival_before',cleared['inbound'])
        self.assertNotIn('arrival_before',cleared['inbound']['field_sources'])
        self.assertEqual(cleared['inbound']['departure_window'],'14:00至16:00')
        old['inbound']['time_reference']='ambiguous'
        resolved=intercity_request(value,old,'14:00至16:00出发，18:00前到达')
        self.assertEqual(resolved['inbound']['arrival_before'],'18:00前')
        self.assertEqual(resolved['inbound']['departure_window'],'14:00至16:00')

    def test_arrival_deadline_does_not_erase_city_availability_or_restrict_departure(self):
        spec=deepcopy(FIXTURE['spec'])
        legs=TripPlanner.intercity_legs(spec,NOW)
        self.assertEqual(legs['inbound']['arrival_before'],'18点前')
        self.assertEqual(TripPlanner.transport_windows(spec,{})['3']['allowed_periods'],['上午'])
        outbound=next(r for r in FIXTURE['queries'][0]['all_trains'] if r['train_code']=='G8')
        inbound=next(r for r in FIXTURE['queries'][2]['all_trains'] if r['train_code']=='G2787')
        result={'outbound':{'status':'completed','request':legs['outbound'],'candidates':[outbound]},
                'inbound':{'status':'completed','request':legs['inbound'],'candidates':[inbound]}}
        TripPlanner.choose_transport(spec,result,activity_counts={1:1,2:1,3:1})
        self.assertEqual(result['outbound']['selected']['train_code'],'G8')
        self.assertEqual(result['inbound']['selected']['train_code'],'G2787')

    def test_both_windows_do_not_turn_departure_end_into_arrival_deadline(self):
        spec=deepcopy(FIXTURE['spec'])
        spec['intercity_request']['inbound'].update(departure_window='14:00至16:00',time_reference='departure')
        legs=TripPlanner.intercity_legs(spec,NOW)
        self.assertEqual(legs['inbound']['arrival_before'],'18点前')
        self.assertEqual(spec['planning_window']['end_window'],(0,18))
        row=next(r for r in FIXTURE['queries'][2]['all_trains'] if r['train_code']=='G2787')
        result={'inbound':{'status':'completed','request':legs['inbound'],'candidates':[row]}}
        TripPlanner.choose_transport(spec,result,activity_counts={1:1,2:1,3:1})
        self.assertIn('selected',result['inbound'])

    def test_real_recorded_chain_refines_truncated_return_once_and_preserves_sights(self):
        with tempfile.TemporaryDirectory() as folder:
            store=MemoryStore(Path(folder)/'test.db');calls=[]
            def query(name,args,context):
                calls.append(deepcopy(args))
                index=0 if args['origin']=='上海' else 3 if args['departure_window'] else 1
                data=deepcopy(FIXTURE['queries'][index])
                return ToolResult('completed',name,payload=data)
            planner=TripPlanner(None,'',SimpleNamespace(non_driving_route=lambda *a,**k:{'duration_seconds':43*60}))
            tool=TripTool(store,planner,rail_executor=query,clock=lambda:NOW)
            event=ChatEvent('web','group','traffic','scope','owner','补充往返交通',occurred_at=NOW)
            spec=deepcopy(FIXTURE['spec']);plan=deepcopy(FIXTURE['plan'])
            traffic=tool._intercity(event,spec,previous_plan=plan)
            planner.connect_transport(plan,spec,FIXTURE['places'],traffic)
            self.assertEqual(len(calls),3)
            self.assertTrue(calls[-1]['departure_window'])
            self.assertLess(traffic['inbound']['selected']['arrival_at'][11:16],'18:00')
            self.assertEqual(traffic['inbound']['queried_at'],FIXTURE['queries'][3]['retrieved_at'])
            self.assertIn('上午',traffic['availability_windows']['3']['allowed_periods'])
            self.assertNotEqual(traffic['completeness'],'conflict')
            self.assertEqual(plan['days'],FIXTURE['plan']['days'])

    def test_incomplete_search_is_not_proof_of_conflict(self):
        spec=deepcopy(FIXTURE['spec']);TripPlanner.intercity_legs(spec,NOW)
        early=FIXTURE['queries'][1]['all_trains'][0]
        result={'inbound':{'status':'completed','candidate_truncated':True,'candidates':[early]}}
        TripPlanner.choose_transport(spec,result,activity_counts={1:1,2:1,3:1})
        self.assertEqual(result['completeness'],'partial')
        self.assertEqual(result['issues'][0]['code'],'candidate_coverage_incomplete')

    def test_legacy_zero_window_is_recomputed_before_revalidation(self):
        spec=deepcopy(FIXTURE['spec']);TripPlanner.intercity_legs(spec,NOW)
        spec['planning_window']['end_window']=None
        spec['intercity_windows']={'3':{'date':'2026-11-09','earliest':'2026-11-09T00:00+08:00',
                                      'latest':'2026-11-09T00:00+08:00','allowed_periods':[]}}
        candidate={'spec':spec,'plan':deepcopy(FIXTURE['plan']),'sources':deepcopy(FIXTURE['places'])}
        TripPlanner(None,'',None).reconcile_candidate(candidate,deepcopy(candidate))
        self.assertIn('上午',candidate['spec']['intercity_windows']['3']['allowed_periods'])

    def test_supplement_failure_is_bounded_and_retains_the_city_plan(self):
        with tempfile.TemporaryDirectory() as folder:
            calls=[];store=MemoryStore(Path(folder)/'failure.db')
            def query(name,args,context):
                calls.append(args)
                if args['departure_window']:return ToolResult('failed',name,error_code='timeout')
                data=deepcopy(FIXTURE['queries'][0 if args['origin']=='上海' else 1])
                return ToolResult('completed',name,payload=data)
            tool=TripTool(store,TripPlanner(None,'',None),rail_executor=query,clock=lambda:NOW)
            event=ChatEvent('web','group','failure','scope','owner','补充交通',occurred_at=NOW)
            plan=deepcopy(FIXTURE['plan']);spec=deepcopy(FIXTURE['spec'])
            result=tool._intercity(event,spec,previous_plan=plan)
            self.assertEqual(len(calls),3)
            self.assertEqual(result['completeness'],'partial')
            self.assertEqual(plan,FIXTURE['plan'])
            self.assertIn('未完成',''.join(result['limitations']))

    def test_validation_conflict_keeps_exact_path_and_direction_without_claiming_transfer(self):
        spec=deepcopy(FIXTURE['spec']);spec['city_windows']['departure']['time_text']='早上'
        result={'completeness':'partial','assumptions':{'arrival_buffer_minutes':30,'departure_buffer_minutes':60,'unknown_transfer_minutes':60},'station_transfers':{}}
        TripPlanner(None,'',None).connect_transport(deepcopy(FIXTURE['plan']),spec,FIXTURE['places'],result)
        self.assertEqual(result['issues'][-1]['path'],'days[2].activities')
        self.assertEqual(result['issues'][-1]['direction'],'inbound')
        self.assertNotIn('已查接驳',''.join(result['limitations']))

    def test_writer_receives_unresolved_transport_and_render_never_claims_completion(self):
        with tempfile.TemporaryDirectory() as folder:
            store=MemoryStore(Path(folder)/'writer.db');event=ChatEvent('web','group','writer','scope','owner','补充交通')
            store.delivery.begin_event(event.event_key)
            client=FakeClient([model_response(ANSWER)]);writer=TripAnswerWriter(store,client,'test')
            trip={'trip_id':'TR-test','version':1,'title':'测试','spec':deepcopy(FIXTURE['spec']),
                  'plan':{**deepcopy(FIXTURE['plan']),'intercity':{'completeness':'partial','inbound':{'status':'completed'},'limitations':['候选覆盖不足']}},
                  'sources':FIXTURE['places'],'answer_changes':['intercity.inbound']}
            writer.generate(event,trip,[])
            payload=json.loads(client.completions.requests[0]['messages'][-1]['content'])
            self.assertEqual(payload['transport']['completeness'],'partial')
            self.assertFalse(payload['transport']['inbound']['selected'])
            trip['answer']=validate_answer({**ANSWER,'change_summary':'往返交通已补齐。'},trip)
            text,_=render_answer(trip,TripTool.format_intercity,mode='update')
            self.assertNotIn('已补齐',text)
            self.assertIn('尚未',text)
