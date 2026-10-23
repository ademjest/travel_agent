from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from agents.intent_compiler import IntentCompiler
from agents.trip_planner import TripPlanner
from core.chat_transport import ChatEvent
from infrastructure.memory_store import MemoryStore
from services.semantic_task_service import SemanticTaskService
from services.trip_service import TripService
from services.planning_service import PlanningService
from test_trips import PLACES, model_response, plan_value
from test_travel_agent import FakeClient


INITIAL='''开封参考规划
方案A：2天1晚
第1天：清明上河园；第2天：开封府。
方案B：3天2晚
第1天：老城；第2天：清明上河园；第3天：开封府。'''


def ir(value,text):
    return IntentCompiler(FakeClient([model_response(value)]),'test').try_compile(text)


class ConversationPlanContextTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store=MemoryStore(Path(self.temp.name)/'test.db')
        self.now=datetime(2030,9,1,tzinfo=timezone.utc)
        self.event=lambda text,key='event':ChatEvent('web','group',key,'conversation','owner',text,occurred_at=self.now)

    def test_artifacts_persist_order_restart_isolation_and_soft_delete(self):
        first=self.store.planning.capture(self.event('我想去开封玩','first'),INITIAL)
        second=self.store.planning.capture(self.event('搜索开封四日游攻略','second'),
            '开封四日研究方案\n第1天古城，第2天清明上河园，第3天开封府，第4天休整。',kind='research_backed')
        self.assertEqual(first['duration_options'],[2,3])
        self.assertEqual(second['duration_options'],[4])
        self.assertEqual([p['ordinal'] for p in self.store.planning.list_for_owner(self.event(''))],[1,2])
        restarted=MemoryStore(self.store.database_path)
        self.assertEqual(len(restarted.planning.list_for_owner(self.event(''))),2)
        other=ChatEvent('web','group','other','other-conversation','owner','')
        self.assertEqual(restarted.planning.list_for_owner(other),())
        restarted.planning.delete(self.event(''),first['artifact_id'],first['version'])
        self.assertEqual([p['artifact_id'] for p in restarted.planning.list_for_owner(self.event(''))],[second['artifact_id']])

    def test_user_can_list_and_delete_plan_drafts_without_touching_others(self):
        first=self.store.planning.capture(self.event('我想去开封玩','first-command'),INITIAL)
        second=self.store.planning.capture(self.event('搜索开封四日游攻略','second-command'),
            '开封四日研究方案\n第1天古城，第2天景区，第3天人文，第4天休整。',kind='research_backed')
        service=PlanningService(self.store.planning)
        self.assertIn('方案 1',service.handle(self.event('查看方案草稿')))
        self.assertIn('已删除',service.handle(self.event('删除方案草稿 '+first['artifact_id'])))
        self.assertEqual([p['artifact_id'] for p in self.store.planning.list_for_owner(self.event(''))],[second['artifact_id']])

    def test_natural_online_reference_dispatches_research_service(self):
        text='我想去开封玩，但是还没确定时间，想先了解一下大概的攻略参考，最好可以参考一下网上的攻略比如小红书'
        value={'schema_version':1,'action':'read','operations':[{'domain':'research','operation':'search_travel_guides',
            'query_text':'了解一下大概的攻略参考','source_request':'网上的攻略比如小红书'}],
            'missing_fields':[],'requires_confirmation':False,
            'source_spans':['了解一下大概的攻略参考','网上的攻略比如小红书']}
        research=Mock(); research.handle_intent.return_value='已搜索小红书和国内攻略'
        service=SemanticTaskService(self.store,Mock(try_compile=Mock(return_value=ir(value,text))),
            research_service=research,mode='execute')
        self.assertEqual(service.handle(self.event(text),object()),'已搜索小红书和国内攻略')
        research.handle_intent.assert_called_once()

    def trip_service(self):
        places={**PLACES,'hotel':{'id':'hotel','name':'测试连锁酒店','address':'测试路','type':'住宿服务'}}
        amap=Mock(); amap.search_places.return_value=list(places.values()); amap.route_for_pois.return_value={'duration_seconds':600}
        extract={'destination':'开封','duration_text':'3天','start_date_text':'','end_date_text':'',
                 'preferences':[],'must_visit':[],'budget_text':'','transport_text':''}
        client=FakeClient([model_response(extract),model_response({**plan_value(),'hotel_ids':['hotel']})])
        return TripService(self.store,TripPlanner(client,'test',amap),clock=lambda:self.now)

    def test_exact_reference_clarifies_duration_then_creates_unsaved_preview_and_confirms(self):
        artifact=self.store.planning.capture(self.event('我想去开封玩','initial'),INITIAL)
        trip_service=self.trip_service()
        first_text='那还是按照最开始的规划安排吧，但是要加上酒店推荐'
        first={'schema_version':1,'action':'clarify','operations':[{'domain':'trip','operation':'create_from_plan_artifact',
            'plan_index':1,'plan_reference':'最开始的规划','changes':{
                'accommodation':{'value':'酒店推荐','requested':True,'source':'要加上酒店推荐'}}}],
            'missing_fields':['plan_duration'],'requires_confirmation':True,
            'source_spans':['最开始的规划','要加上酒店推荐']}
        service=SemanticTaskService(self.store,Mock(try_compile=Mock(return_value=ir(first,first_text))),trip_service=trip_service)
        event=self.event(first_text,'choose')
        reply=service.handle(event,self.store.begin_event(event.event_key))
        self.assertIn('2天和3天',reply)
        self.assertEqual(self.store.trips.list_for_owner(self.event('')),())

        second_text='采用3天2晚'
        second={'schema_version':1,'action':'create','operations':[{'domain':'trip','operation':'create_from_plan_artifact',
            'selected_days':3}], 'missing_fields':[],'requires_confirmation':True,'source_spans':['采用3天2晚']}
        service.compiler=Mock(try_compile=Mock(return_value=ir(second,first_text+'\n用户补充：'+second_text)))
        event=self.event(second_text,'duration')
        preview=service.handle(event,self.store.begin_event(event.event_key))
        self.assertIn('行程创建预览',preview)
        self.assertIn('测试连锁酒店',preview)
        self.assertEqual(self.store.trips.list_for_owner(self.event('')),())
        tasks=self.store.tasks.recent('web',self.event('').storage_scope_id,'owner')
        self.assertTrue(any(t.task_type=='itinerary' and t.slots.get('new_trip_pending') for t in tasks))

        event=self.event('确认行程修改','confirm')
        result=trip_service.handle(event,self.store.begin_event(event.event_key))
        self.assertIn('已创建行程',result)
        trips=self.store.trips.list_for_owner(self.event(''))
        self.assertEqual(len(trips),1)
        self.assertEqual(trips[0]['spec']['destination'],'开封')
        self.assertEqual(trips[0]['spec']['day_count'],3)
        self.assertTrue(trips[0]['spec']['date_is_tentative'])
        self.assertTrue(trips[0]['spec']['hotel_requested'])
        self.assertEqual(self.store.planning.get(self.event(''),artifact['artifact_id'])['status'],'adopted')

    def test_update_without_saved_trip_recovers_earliest_artifact(self):
        self.store.planning.capture(self.event('我想去开封玩','initial'),INITIAL)
        text='那还是按照最开始的规划安排吧，但是要加上酒店推荐'
        value={'schema_version':1,'action':'update','operations':[{'domain':'trip','operation':'update_constraints',
            'target_ref':'current_trip','changes':{'accommodation':{'value':'酒店推荐','requested':True,'source':'要加上酒店推荐'}}}],
            'missing_fields':[],'requires_confirmation':True,'source_spans':['最开始的规划','要加上酒店推荐']}
        service=SemanticTaskService(self.store,Mock(try_compile=Mock(return_value=ir(value,text))),trip_service=self.trip_service())
        event=self.event(text,'recover')
        self.assertIn('2天和3天',service.handle(event,self.store.begin_event(event.event_key)))
