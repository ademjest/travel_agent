from copy import deepcopy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock

from core.chat_transport import ChatEvent
from infrastructure.memory_store import MemoryStore
from infrastructure.skill_manager import skill_scope
from tools.trip.alternatives import Alternatives
from tools.trip.planner import TripPlanner, TripPlanError, place_groups
from tools.research.tool import ResearchTool, saved_trip_plan, structured_target_scope, attach_answer
from test_travel_agent import FakeClient
from test_trips import model_response, plan_value


PLACES={'park':{'id':'park','name':'测试园区','address':'园区路1号','location':'109,34','cityname':'测试市','type':'公园'},
        'hall':{'id':'hall','name':'测试园区自然馆','address':'园区路1号','location':'109.0001,34.0001','cityname':'测试市','type':'博物馆'},
        'other':{'id':'other','name':'另一博物馆','address':'另一路','location':'109.03,34.03','cityname':'测试市','type':'博物馆'}}


class PlaceGroupsTests(unittest.TestCase):
    def test_name_address_location_group_a_park_without_deleting_its_hall(self):
        self.assertEqual(place_groups(PLACES),{'park':'park','hall':'park','other':'other'})
        places=deepcopy(PLACES);places['hall']['address']='另一个地址'
        self.assertNotEqual(place_groups(places)['park'],place_groups(places)['hall'])

    def test_duplicate_names_with_different_ids_are_not_two_activities(self):
        places=deepcopy(PLACES);places['other']={**places['park'],'id':'other','name':'测试 园区'}
        with self.assertRaises(TripPlanError):TripPlanner.validate_plan(plan_value((('park','other'),)),{'day_count':1},places)

    def test_child_is_preserved_and_satisfies_must_visit(self):
        value=plan_value((('park',),));value['days'][0]['activities'][0]['subactivities']=[{'poi_id':'hall'}]
        clean=TripPlanner.validate_plan(value,{'day_count':1,'must_visit':['测试园区自然馆']},PLACES)
        self.assertEqual(clean['days'][0]['activities'][0]['subactivities'],[{'poi_id':'hall'}])
        trip={'trip_id':'TR-t','title':'测试','version':1,'spec':{'destination':'测试市','day_count':1},'sources':PLACES,'plan':clean}
        plan=saved_trip_plan(trip)
        scope=structured_target_scope({'original_request':'这些景点的信息','query_text':'这些景点的信息'},plan)
        self.assertEqual([t['name'] for t in scope['targets']],['测试园区','测试园区自然馆'])

    def test_unrelated_child_and_repeated_main_visit_are_rejected(self):
        value=plan_value((('park','hall'),))
        with self.assertRaises(TripPlanError):TripPlanner.validate_plan(value,{'day_count':1},PLACES)
        value=plan_value((('park',),));value['days'][0]['activities'][0]['subactivities']=[{'poi_id':'other'}]
        with self.assertRaises(TripPlanError):TripPlanner.validate_plan(value,{'day_count':1},PLACES)


class AlternativesTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.store=MemoryStore(Path(temp.name)/'test.db')
        self.event=ChatEvent('web','group','dual','scope','owner','规划测试市一天')
        self.store.delivery.begin_event(self.event.event_key)
        self.spec={'destination':'测试市','day_count':1,'must_visit':[]}
        self.plan=plan_value((('park','other'),))
        self.plan['days'][0]['date']=''
        self.client=FakeClient([model_response(self.plan),model_response({'choice':'amap','reason':'减少折返'})])
        self.amap=Mock(search_places=Mock(return_value=list(PLACES.values())))
        self.planner=TripPlanner(self.client,'test',self.amap)
        self.provider=Mock(configured=True)
        self.provider.query.return_value={'status':'completed','physical_calls':1,'retrieved_at':'2026-10-27T00:00:00+00:00',
            'content_hash':'test','text':'第1天上午参观测试园区，下午另一博物馆。\n### 测试园区\n介绍：风景优美。\n开放时间：全天\n### 另一博物馆\n介绍：地方文化展陈。\n开放时间：全天'}
        self.research=ResearchTool(self.store,Mock(configured=False),wendao=self.provider)

    def test_independent_parallel_drafts_and_idempotent_comparison(self):
        gate=threading.Barrier(2,timeout=5)
        query=self.provider.query.side_effect=lambda *a,**kw:(gate.wait(),self.provider.query.return_value)[1]
        collect=self.planner.collect_places
        def independent(spec):
            self.assertNotIn('research_context',spec);gate.wait();return collect(spec)
        self.planner.collect_places=independent
        self.spec['research_context']={'targets':[{'name':'不能污染A'}]}
        with skill_scope():
            first=Alternatives(self.store,self.planner,self.research).generate(self.event,self.spec)
        self.assertEqual(self.spec['planning_decision']['choice'],'amap')
        self.assertEqual(self.provider.query.call_count,1)
        self.assertNotIn('amap_plan',self.provider.query.call_args.args[2])
        calls=len(self.client.chat.completions.requests)
        with skill_scope():
            second=Alternatives(self.store,self.planner,self.research).generate(self.event,self.spec)
        self.assertEqual(first[1],second[1]);self.assertEqual(len(self.client.chat.completions.requests),calls)
        self.assertEqual(self.provider.query.call_count,1)

    def test_comparison_timeout_falls_back_without_resubmission(self):
        self.client=FakeClient([model_response(self.plan)])
        self.planner.client=self.client
        with skill_scope():places,plan=Alternatives(self.store,self.planner,self.research).generate(self.event,self.spec)
        self.assertEqual(plan['days'][0]['activities'][0]['poi_id'],'park')
        self.assertNotEqual(self.spec['planning_decision']['status'],'completed')

    def test_invalid_choice_keeps_diagnostic_candidate_and_repairs_at_most_once(self):
        self.client.completions.responses=[model_response(self.plan),model_response({'choice':'invalid','plan':self.plan}),
                                           model_response({'choice':'amap','reason':'保留有效方案'})]
        with skill_scope():Alternatives(self.store,self.planner,self.research).generate(self.event,self.spec)
        self.assertTrue(self.spec['planning_decision']['repaired'])
        self.assertEqual(len(self.client.completions.requests),3)
        with self.store._connect() as db:
            row=db.execute("SELECT result_text FROM event_tool_results WHERE operation_key LIKE '%:compare:result'").fetchone()
        value=json.loads(row['result_text'])
        self.assertEqual(value['error_code'],'invalid_choice');self.assertEqual(value['validation_path'],'choice')
        self.assertEqual(value['rejected_candidate']['choice'],'invalid')
        self.assertNotIn('rejected_candidate',self.spec['planning_decision'])

    def test_period_headings_are_comparable_without_fabricating_day_grounding(self):
        self.provider.query.return_value={**self.provider.query.return_value,'text':
            '## 分时安排\n### 上午（09:00）测试园区\n园内参观\n### 下午（14:00）另一博物馆\n文化参观\n'
            '## 景点详情\n### 测试园区\n介绍：不应进入比较的重复介绍。\n开放时间：全天\n'
            '### 另一博物馆\n介绍：地方展陈。\n开放时间：全天'}
        with skill_scope():Alternatives(self.store,self.planner,self.research).generate(self.event,self.spec)
        self.assertEqual(self.spec['planning_decision']['status'],'completed')
        request=json.loads(self.client.completions.requests[-1]['messages'][1]['content'])
        self.assertFalse(request['wendao_plan']['days_grounded'])
        self.assertIn('上午',request['wendao_plan']['text'])
        self.assertNotIn('不应进入比较的重复介绍',request['wendao_plan']['text'])

    def test_followup_queries_amap_only_places_and_does_not_mutate_trip(self):
        trip={'trip_id':'TR-A','title':'测试市一天','version':1,'spec':self.spec,'sources':PLACES,'plan':self.plan}
        original=deepcopy(trip);snapshot=saved_trip_plan(trip)
        request={'original_request':'这些景点的信息','query_text':'这些景点的信息'}
        scope=structured_target_scope(request,snapshot)
        data={'sources':[],'target_scope':{**scope,'city':'测试市'},'provider_dates':{'day_count':1},'query':request['query_text']}
        attach_answer(self.research,self.event,data,targets=scope['targets'])
        query=self.provider.query.call_args.args[2]
        self.assertIn('另一博物馆',query);self.assertIn('只介绍下列既定对象',query)
        self.assertEqual(trip,original)


if __name__=='__main__':unittest.main()
