from datetime import datetime,timezone,timedelta
from dataclasses import replace
from pathlib import Path
import json,tempfile,unittest
from unittest.mock import Mock,patch

from app.request_router import RequestRouter,bind_read_context,bind_request,read_task_compatible
from core.chat_transport import ChatEvent
from core.intent_ir import IntentIR,IntentOperation,parse_intent_ir
from core.settings import Settings
from core.tasks import TaskRecord,TaskUpdate
from infrastructure.amap_client import AmapClient,AmapError,Location,PlaceResolutionError
from infrastructure.memory_store import MemoryStore
from infrastructure.place_resolution import PLACE_CONTEXT
from tools.travel_query import TravelQueryTool


QUESTION='武汉站到华中科技大学启明门，坐地铁怎么走？'
NOW=datetime.now(timezone.utc)


def poi(name,identifier,city='武汉市',location='114.40,30.50',kind='交通设施服务;火车站'):
    return {'name':name,'id':identifier,'cityname':city,'location':location,'type':kind,
            'typecode':'150200','pname':'湖北省' if city=='武汉市' else '江苏省','adname':'','adcode':'','address':''}


def maps():
    client=AmapClient('fixed-response',max_attempts=1)
    places={
        '武汉站':[poi('武汉站','origin')],
        '华中科技大学':[poi('华中科技大学','campus',kind='科教文化服务;学校;高等院校')],
        '华中科技大学启明门':[poi('华中科技大学东校区(启明门)','gate',kind='通行设施;门')],
        '华中科技大学南大门':[poi('华中科技大学(南大门)','south',location='114.41,30.51',kind='通行设施;门')],
        '南京南站':[poi('南京南站','nanjing','南京市','118.79,31.97')],
        '夫子庙':[poi('夫子庙','temple','南京市','118.78,32.02',kind='风景名胜;景点')],
    }
    client.search_places=Mock(side_effect=lambda city,keyword,**kwargs:tuple(places.get(keyword,[])))
    client.search_place_tips=Mock(return_value=())
    client.geocode=Mock(side_effect=AmapError('没有找到地点',code='location_not_found'))
    client._get=Mock(return_value={'status':'1','route':{'transits':[{'distance':'25000','duration':'4385','segments':[]}]}})
    return client


class TransitCityTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.store=MemoryStore(Path(temp.name)/'test.db')
        self.tool=TravelQueryTool(Settings('','',frozenset(),'fixed-key','','',''),store=self.store)
        self.tool.amap=maps()
        self.event=ChatEvent('web','group','fresh','scope','owner',QUESTION,occurred_at=NOW)

    def run_ir(self,values,*,missing=(),event=None):
        event=event or self.event
        ir=IntentIR(1,'clarify' if missing else 'read',(IntentOperation('transit','query',values),),
                    missing_fields=missing,source_spans=(event.content,))
        compiler=Mock(try_compile=Mock(return_value=ir))
        result=RequestRouter(self.store,compiler,read_only_service=self.tool).route(event,self.store.delivery.begin_event(event.event_key))
        compiler.try_compile.assert_called_once()
        return result[0]

    def task(self,*,key='old',origin='武汉站',destination='华中科技大学启明门',city='',missing=('city',),owner='owner'):
        event=replace(self.event,event_id=key,sender_id=owner)
        slots={'origin':origin,'destination':destination,'intents':['transit'],'reference_time':NOW.isoformat()}
        if city:slots['city']=city
        self.store.tasks.prepare_result(event,self.store.delivery.begin_event(event.event_key),
            TaskUpdate('transit','collecting',event.content,slots,missing),'请补充')
        return self.store.tasks.recent('web',event.storage_scope_id,owner)[0]

    def test_recorded_model_city_variants_form_the_same_map_request(self):
        saved=json.loads((Path(__file__).parent/'fixtures/transit_city_2026-10-29.json').read_text(encoding='utf-8'))
        parameters=[]
        for index,case in enumerate(saved):
            self.tool.amap=maps()
            event=replace(self.event,event_id='variant-'+str(index),scope_id='variant-'+str(index))
            ir=parse_intent_ir(case['read_plan'],event.content)
            reply=RequestRouter(self.store,Mock(try_compile=Mock(return_value=ir)),read_only_service=self.tool).route(
                event,self.store.delivery.begin_event(event.event_key))[0]
            self.assertIn('高德公共交通方案',reply)
            calls=self.tool.amap._get.call_args_list
            self.assertEqual(len(calls),1)
            self.assertEqual(calls[0].args[0],'/v3/direction/transit/integrated')
            parameters.append(calls[0].args[1])
            task=self.store.tasks.recent('web',event.storage_scope_id,event.sender_id)[0]
            self.assertEqual(task.slots['city'],'武汉市')
            self.assertEqual(task.slots['city_source'],'verified_locations')
        self.assertEqual(parameters[0],parameters[1])
        self.assertEqual(parameters[0]['city'],'武汉市')

    def test_unsupported_model_city_is_derived_instead_of_becoming_a_constraint(self):
        reply=self.run_ir({'origin':'武汉站','destination':'华中科技大学启明门','city':'武汉市'})
        self.assertIn('高德公共交通方案',reply)
        self.assertEqual(self.tool.amap._get.call_args.args[1]['city'],'武汉市')

    def test_recorded_amap_responses_replay_both_model_variants(self):
        saved=json.loads((Path(__file__).parent/'fixtures/transit_city_2026-10-29.json').read_text(encoding='utf-8'))
        parameters=[]
        for index,case in enumerate(saved):
            client=AmapClient('offline',max_attempts=1,sleeper=lambda _:None)
            client._get=Mock(side_effect=saved[1]['amap_responses'])
            self.tool.amap=client
            event=replace(self.event,event_id='replay-'+str(index),scope_id='replay-'+str(index))
            ir=parse_intent_ir(case['read_plan'],event.content)
            with patch('infrastructure.amap_client.time.sleep'):
                reply=RequestRouter(self.store,Mock(try_compile=Mock(return_value=ir)),read_only_service=self.tool).route(
                    event,self.store.delivery.begin_event(event.event_key))[0]
            self.assertIn('高德公共交通方案',reply)
            self.assertEqual(client._get.call_count,5)
            parameters.append(client._get.call_args.args[1])
        self.assertEqual(parameters[0],parameters[1])
        self.assertEqual(parameters[0]['origin'],'114.424338,30.606981')
        self.assertEqual(parameters[0]['city'],'武汉市')

    def test_repeated_complete_question_continues_the_matching_pending_task(self):
        old=self.task()
        reply=self.run_ir({'origin':'武汉站','destination':'华中科技大学启明门'},missing=('city',))
        self.assertIn('高德公共交通方案',reply)
        current=self.store.tasks.recent('web',self.event.storage_scope_id,self.event.sender_id)[0]
        self.assertEqual(current.task_id,old.task_id)
        self.assertEqual(current.version,2)

    def test_short_city_answer_keeps_endpoints(self):
        old=self.task()
        reply=self.run_ir({'city':'武汉'},event=replace(self.event,content='武汉'))
        self.assertIn('高德公共交通方案',reply)
        current=self.store.tasks.recent('web',self.event.storage_scope_id,self.event.sender_id)[0]
        self.assertEqual(current.task_id,old.task_id)
        self.assertEqual(current.slots['destination'],'华中科技大学启明门')

    def test_complete_new_route_does_not_inherit_city_or_overwrite_pending_task(self):
        old=self.task(city='武汉',missing=('destination',))
        event=replace(self.event,content='南京南站到夫子庙坐地铁怎么走？')
        reply=self.run_ir({'origin':'南京南站','destination':'夫子庙'},event=event)
        self.assertIn('高德公共交通方案',reply)
        self.assertEqual(self.tool.amap._get.call_args.args[1]['city'],'南京市')
        with self.store._connect() as db:
            row=db.execute('SELECT version,status FROM travel_tasks WHERE task_id=?',(old.task_id,)).fetchone()
        self.assertEqual((row['version'],row['status']),(1,'collecting'))

    def test_explicit_gate_correction_reuses_only_the_unchanged_origin_fact(self):
        self.run_ir({'origin':'武汉站','destination':'华中科技大学启明门'})
        self.tool.amap.search_places.reset_mock()
        event=replace(self.event,event_id='correct',content='终点改为华中科技大学南大门')
        compiler=Mock(side_effect=AssertionError('Explicit correction does not need a model'))
        reply=RequestRouter(self.store,compiler,read_only_service=self.tool).route(event,self.store.delivery.begin_event(event.event_key))[0]
        self.assertIn('南大门',reply)
        self.assertFalse(any(call.args[1]=='武汉站' for call in self.tool.amap.search_places.call_args_list))
        self.assertIn('114.410000,30.510000',self.tool.amap._get.call_args.args[1]['destination'])

    def test_multiple_routes_resolve_cities_independently(self):
        text='武汉站到华中科技大学启明门坐地铁，再查南京南站到夫子庙的地铁'
        operations=(IntentOperation('transit','query',{'origin':'武汉站','destination':'华中科技大学启明门'}),
                    IntentOperation('transit','query',{'origin':'南京南站','destination':'夫子庙'}))
        ir=IntentIR(1,'clarify',operations,missing_fields=('city',),source_spans=(text,))
        from core.execution_outcome import TaskContext,TravelReadRequest
        event=replace(self.event,content=text)
        request=TravelReadRequest.from_ir(ir,TaskContext(None,text,NOW.isoformat(),{},text))
        reply=self.tool.execute(event,self.store.delivery.begin_event(event.event_key),request)
        self.assertIn('公共交通方案',reply)
        self.assertEqual([call.args[1]['city'] for call in self.tool.amap._get.call_args_list],['武汉市','南京市'])

    def test_distinct_endpoint_cities_are_not_forced_into_one_city_or_rail(self):
        reply=self.run_ir({'origin':'武汉站','destination':'南京南站'},event=replace(self.event,content='武汉站到南京南站坐地铁怎么走'))
        self.assertIn('不同城市',reply)
        self.tool.amap._get.assert_not_called()

    def test_explicit_city_conflict_never_queries_a_route(self):
        reply=self.run_ir({'origin':'武汉站','destination':'华中科技大学启明门','city':'南京'},
            event=replace(self.event,content='在南京，武汉站到华中科技大学启明门坐地铁怎么走'))
        self.assertNotIn('高德公共交通方案',reply)
        self.assertIn('城市',reply)
        self.assertIn('不一致',reply)
        self.tool.amap._get.assert_not_called()

    def test_multiple_pending_routes_do_not_guess_a_short_city_answer(self):
        self.task(key='wuhan')
        self.task(key='nanjing',origin='南京南站',destination='夫子庙')
        event=replace(self.event,content='武汉')
        compiler=Mock()
        reply=RequestRouter(self.store,compiler,read_only_service=self.tool).route(
            event,self.store.delivery.begin_event(event.event_key))[0]
        self.assertIn('多个查询',reply)
        compiler.try_compile.assert_not_called()
        self.tool.amap.search_places.assert_not_called()

    def test_multiple_pending_routes_allow_a_unique_complete_repeat(self):
        old=self.task(key='wuhan')
        self.task(key='nanjing',origin='南京南站',destination='夫子庙')
        reply=self.run_ir({'origin':'武汉站','destination':'华中科技大学启明门'})
        self.assertIn('高德公共交通方案',reply)
        with self.store._connect() as db:
            self.assertEqual(db.execute('SELECT version FROM travel_tasks WHERE task_id=?',(old.task_id,)).fetchone()[0],2)

    def test_ineligible_tasks_cannot_supply_old_city(self):
        for index,state in enumerate(('expired','cancelled','other_owner')):
            scope='ineligible-'+state
            self.event=replace(self.event,scope_id=scope,event_id='new-'+str(index))
            old=self.task(key='old-'+str(index),city='南京',owner='other' if state=='other_owner' else 'owner')
            with self.store._connect() as db:
                if state=='expired':
                    db.execute("UPDATE travel_tasks SET expires_at='2000-01-01T00:00:00+00:00' WHERE task_id=?",(old.task_id,))
                elif state=='cancelled':
                    db.execute("UPDATE travel_tasks SET status='cancelled' WHERE task_id=?",(old.task_id,))
            reply=self.run_ir({'origin':'武汉站','destination':'华中科技大学启明门'})
            self.assertIn('高德公共交通方案',reply)
            current=self.store.tasks.recent('web',self.event.storage_scope_id,'owner')[0]
            self.assertNotEqual(current.task_id,old.task_id)
            self.assertEqual(current.slots['city'],'武汉市')

    def test_only_a_verified_nonweb_reply_can_bind_a_completed_task(self):
        self.event=replace(self.event,platform='onebot')
        self.run_ir({'origin':'武汉站','destination':'华中科技大学启明门'})
        task=self.store.tasks.recent('onebot',self.event.storage_scope_id,'owner')[0]
        with self.store._connect() as db:
            db.execute('INSERT INTO outbound_message_links VALUES (?,?,?,?,?)',
                ('onebot',self.event.storage_scope_id,'verified',self.event.event_key,'owner'))
        event=replace(self.event,event_id='followup',content='再查一次',reply_to_id='verified')
        self.assertEqual(bind_request(self.store,event).read_context.task.task_id,task.task_id)
        self.assertIsNone(bind_request(self.store,replace(event,reply_to_id='unknown')).read_context.task)

    def test_missing_origin_still_asks_without_map_calls(self):
        reply=self.run_ir({'destination':'华中科技大学启明门'},missing=('origin','city'),
            event=replace(self.event,content='去华中科技大学启明门坐地铁'))
        self.assertIn('起点',reply)
        self.tool.amap.search_places.assert_not_called()

    def test_ambiguous_multicity_station_is_not_chosen_by_transit_type_preference(self):
        candidates=[poi('火车站','wuhan'),{**poi('火车站','nanjing','南京市','118.79,31.97'),'typecode':'150500'}]
        self.tool.amap.search_places=Mock(return_value=candidates)
        reply=self.run_ir({'origin':'火车站','destination':'大学南门'},event=replace(self.event,content='火车站到大学南门坐地铁怎么走'))
        self.assertNotIn('高德公共交通方案',reply)
        self.tool.amap._get.assert_not_called()

    def test_direct_coordinate_routes_keep_their_explicit_city_contract(self):
        origin=Location('起点','起点',114.4,30.5,'')
        destination=Location('终点','终点',114.41,30.51,'')
        result=self.tool.amap.non_driving_route_locations(origin,destination,mode='transit',city='武汉')
        self.assertEqual(result['city_source'],'request')
        self.assertEqual(self.tool.amap._get.call_args.args[1]['city'],'武汉')


class ReadTaskRelationTests(unittest.TestCase):
    def test_unlocated_weather_can_change_query_time_without_reopening_domain_fallback(self):
        task=TaskRecord('old','weather','collecting','现在天气怎么样',{},('location',),1,'','old')
        ir=IntentIR(1,'read',(IntentOperation('forecast','query',{}),))
        self.assertTrue(read_task_compatible(task,ir,'明天天气怎么样'))
        route=replace(task,task_type='transit',missing_slots=('origin','destination'))
        self.assertFalse(read_task_compatible(route,IntentIR(1,'read',(IntentOperation('transit','query',{}),)),'地铁怎么走'))

    def test_shared_domain_is_not_identity_and_same_domain_batches_do_not_bind(self):
        task=TaskRecord('old','transit','collecting',QUESTION,{'origin':'武汉站','destination':'华中科技大学启明门','city':'武汉'},
                        ('destination',),1,'','old')
        text='南京南站到夫子庙坐地铁怎么走？'
        operation=IntentOperation('transit','query',{'origin':'南京南站','destination':'夫子庙'})
        self.assertFalse(read_task_compatible(task,IntentIR(1,'read',(operation,)),text))
        self.assertFalse(read_task_compatible(task,IntentIR(1,'read',(operation,operation)),text))

    def test_pending_endpoint_refinement_is_compatible(self):
        task=TaskRecord('old','route','collecting','',{'origin':'大学南大门','destination':'研发中心'},('destination',),1,'','old')
        ir=IntentIR(1,'read',(IntentOperation('route','query',{'origin':'大学启明门','destination':'航天三院研发中心'}),))
        self.assertTrue(read_task_compatible(task,ir,'是航天三院研发中心'))
