import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from core.chat_transport import ChatEvent
from core.tasks import TaskUpdate, TaskRecord, route_correction, read_task_continuation
from core.intent_ir import parse_intent_ir
from core.execution_outcome import TaskContext,TravelReadRequest
from core.settings import Settings
from core.tool_result import ToolResult
from infrastructure.amap_client import PlaceResolutionError
from infrastructure.memory_store import MemoryStore
from services.semantic_task_service import SemanticTaskService
from services.travel_service import TravelService


class RouteCorrectionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store=MemoryStore(Path(self.temp.name)/'test.db')
        self.old=ChatEvent('web','group','old','c','u','从华中科技大学启明门到武汉东西湖区航天信息产业研发中心驾车怎么走')
        self.correction=ChatEvent('web','group','new','c','u','我说了起点是华中科技大学的启明门')

    def task(self,status='completed',destination='武汉东西湖区航天信息产业研发中心',key='old',owner='u'):
        event=ChatEvent('web','group',key,'c',owner,self.old.content)
        update=TaskUpdate('route',status,event.content,{'origin':'华中科技大学启明门','destination':destination,'intents':['route']})
        self.store.tasks.prepare_result(event,self.store.begin_event(event.event_key),update,'old fixture')

    def service(self):
        compiler=Mock()
        travel=SimpleNamespace(execute_tool=Mock(return_value='verified route fixture'))
        return SemanticTaskService(self.store,compiler,travel_service=travel),compiler,travel

    def test_completed_route_correction_requeries_without_model_or_changing_destination(self):
        self.task()
        service,compiler,travel=self.service()
        reply=service.handle(self.correction,self.store.begin_event(self.correction.event_key))
        self.assertEqual(reply,'verified route fixture')
        compiler.try_compile.assert_not_called()
        travel.execute_tool.assert_called_once_with('get_driving_route',{'origin':'华中科技大学的启明门','destination':'武汉东西湖区航天信息产业研发中心'})
        with self.store._connect() as db:
            rows=db.execute('SELECT * FROM travel_tasks').fetchall()
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['version'],2)

    def test_failed_read_can_be_explicitly_corrected(self):
        self.task(status='failed')
        service,compiler,travel=self.service()
        self.assertEqual(service.handle(self.correction,self.store.begin_event(self.correction.event_key)),'verified route fixture')
        self.assertEqual(travel.execute_tool.call_count,1)

    def test_cancelled_or_other_owner_task_is_not_revived(self):
        for status,owner in (('cancelled','u'),('completed','other')):
            with self.subTest(status=status,owner=owner):
                self.task(status=status,owner=owner,key=status)
                service,compiler,travel=self.service()
                self.assertIn('请回复',service.handle(self.correction,self.store.begin_event(self.correction.event_key)))
                travel.execute_tool.assert_not_called()

    def test_multiple_different_routes_require_selection(self):
        self.task()
        self.task(destination='武汉站',key='second')
        service,compiler,travel=self.service()
        self.assertIn('请回复',service.handle(self.correction,self.store.begin_event(self.correction.event_key)))
        travel.execute_tool.assert_not_called()

    def test_expired_route_is_not_used(self):
        self.task()
        with self.store._connect() as db:
            db.execute("UPDATE travel_tasks SET expires_at='2000-01-01T00:00:00+00:00'")
        service,compiler,travel=self.service()
        self.assertIn('请回复',service.handle(self.correction,self.store.begin_event(self.correction.event_key)))
        travel.execute_tool.assert_not_called()

    def test_resolution_failure_preserves_fields_and_becomes_collecting(self):
        self.task()
        service,_,_=self.service()
        service.tool_executor=Mock(return_value=ToolResult('needs_input','get_driving_route',data='请提供启明门的具体道路地址',error_code='location_not_found',missing_fields=('origin',)))
        self.assertIn('具体道路',service.handle(self.correction,self.store.begin_event(self.correction.event_key)))
        with self.store._connect() as db:
            row=db.execute('SELECT * FROM travel_tasks').fetchone()
        self.assertEqual(row['status'],'collecting')
        self.assertEqual(json.loads(row['missing_slots_json']),['origin'])
        self.assertEqual(json.loads(row['slots_json'])['destination'],'武汉东西湖区航天信息产业研发中心')

    def test_locator_error_is_typed_needs_input_not_a_retryable_success(self):
        service=TravelService(Settings('','',frozenset(),'','','',''))
        service.amap=Mock(driving_route=Mock(side_effect=PlaceResolutionError('不是该大学校门',code='location_mismatch',field='origin')))
        result=service.execute_result('get_driving_route',{'origin':'华中科技大学启明门','destination':'研发中心'})
        self.assertEqual((result.status,result.error_code,result.missing_fields),('needs_input','location_mismatch',('origin',)))
        self.assertFalse(result.retryable)

    def test_same_endpoint_route_and_traffic_collapse_to_one_traffic_query(self):
        text='从大学门到研发中心驾车路线和实时路况'
        ir=parse_intent_ir({'schema_version':1,'action':'read','operations':[
            {'domain':domain,'operation':'query','origin':'大学门','destination':'研发中心'} for domain in ('route','traffic')],
            'source_spans':['大学门','研发中心']},text)
        request=TravelReadRequest.from_ir(ir,TaskContext(None,text,'2030-01-01T00:00:00+00:00'))
        self.assertEqual([query.domain for query in request.queries],['traffic'])

    def test_correction_preserves_supplemental_street_address(self):
        content='起点是华中科技大学-南大门，具体是武汉市洪山区珞喻路1037号'
        self.assertEqual(route_correction(content),{'origin':content.removeprefix('起点是')})

    def test_followup_uses_latest_endpoints_not_initial_request(self):
        task=TaskRecord('route','route','collecting',self.old.content,
                        {'origin':'华中科技大学的南大门','destination':'武汉的航天信息产业研发中心'},
                        ('destination',),3,'','')
        _,message=read_task_continuation('是航天三院信息产业研发中心',(task,))
        self.assertIn('南大门',message)
        self.assertNotIn('启明门',message)

    def test_model_cannot_roll_back_other_endpoint_on_destination_followup(self):
        event=ChatEvent('web','group','follow','c','u','是航天三院信息产业研发中心')
        task=TaskRecord('route','route','collecting',self.old.content,
                        {'origin':'华中科技大学南大门','destination':'航天信息产业研发中心'},('destination',),3,'','')
        context=TaskContext.for_read(event,(task,))
        # Deliberately use an old endpoint in a model proposal; it must not become a tool argument.
        operation={'schema_version':1,'action':'read','operations':[{'domain':'route','operation':'query',
                   'origin':'华中科技大学启明门','destination':'航天三院信息产业研发中心'}],
                   'source_spans':['航天三院信息产业研发中心']}
        ir=parse_intent_ir(operation,self.old.content+'\n'+event.content)
        request=TravelReadRequest.from_ir(ir,context)
        self.assertIsNotNone(request)
        self.assertEqual(request.queries[0].values['origin'],'华中科技大学南大门')

    def test_single_tool_lookup_also_preserves_current_origin(self):
        self.task()
        service,_,travel=self.service()
        event=ChatEvent('web','group','follow','c','u','是航天三院信息产业研发中心')
        task=self.store.tasks.recent('web',event.storage_scope_id,'u')[0]
        task=TaskRecord(task.task_id,'route','collecting',task.initial_request,
                        {**task.slots,'origin':'华中科技大学南大门'},('destination',),task.version,task.expires_at,task.last_event_id)
        context=TaskContext.for_read(event,(task,))
        ir=parse_intent_ir({'schema_version':1,'action':'read','operations':[{'domain':'route','operation':'query',
                           'origin':'华中科技大学启明门','destination':'航天三院信息产业研发中心'}],
                           'source_spans':['航天三院信息产业研发中心']},self.old.content+'\n'+event.content)
        service._travel_lookup(event,ir,self.store.begin_event(event.event_key),context)
        self.assertEqual(travel.execute_tool.call_args.args[1]['origin'],'华中科技大学南大门')

    def test_explicit_new_origin_is_not_frozen_by_state_guard(self):
        self.task()
        event=ChatEvent('web','group','change','c','u','起点改为华中科技大学南大门')
        context=TaskContext.for_read(event,self.store.tasks.recent('web',event.storage_scope_id,'u'))
        ir=parse_intent_ir({'schema_version':1,'action':'read','operations':[{'domain':'route','operation':'query',
                           'origin':'华中科技大学南大门','destination':'武汉东西湖区航天信息产业研发中心'}],
                           'source_spans':['华中科技大学南大门']},context.message)
        self.assertEqual(TravelReadRequest.from_ir(ir,context).queries[0].values['origin'],'华中科技大学南大门')

    def test_screenshot_five_turn_sequence_preserves_latest_origin_and_address(self):
        texts=[self.old.content,'那起点改成华中科技大学的南大门',
               '终点改为武汉的航天信息产业研发中心','是航天三院信息产业研发中心',
               '起点是华中科技大学-南大门，具体是武汉市洪山区珞喻路1037号']
        compiler=Mock()
        def compile_ir(text,**kwargs):
            destination=('航天三院信息产业研发中心' if '三院' in text else '武汉东西湖区航天信息产业研发中心')
            # Simulate the same stale-origin model proposal observed in the screenshot.
            return parse_intent_ir({'schema_version':1,'action':'read','operations':[{'domain':'route','operation':'query',
                                   'origin':'华中科技大学启明门','destination':destination}],
                                   'source_spans':[destination]},self.old.content+'\n'+text)
        compiler.try_compile.side_effect=compile_ir
        calls=[]
        def execute(name,args,context):
            calls.append(dict(args))
            if '三院' not in args['destination']:
                return ToolResult('needs_input',name,data='候选：航天三院信息产业研发中心，请确认终点',
                                  error_code='location_not_found',missing_fields=('destination',))
            return ToolResult('completed',name,data='verified route fixture')
        service=SemanticTaskService(self.store,compiler,travel_service=SimpleNamespace(),tool_executor=execute)
        for index,text in enumerate(texts):
            event=ChatEvent('web','group',f'sequence-{index}','c','u',text)
            with self.subTest(turn=index):
                self.assertIsInstance(service.handle(event,self.store.begin_event(event.event_key)),str)
        self.assertEqual(len(calls),5)
        self.assertEqual(calls[3]['origin'],'华中科技大学的南大门')
        self.assertEqual(calls[-1]['origin'],texts[-1].removeprefix('起点是'))
        self.assertEqual(calls[-1]['destination'],'航天三院信息产业研发中心')
        self.assertEqual(compiler.try_compile.call_count,2)
        self.assertNotIn('启明门',compiler.try_compile.call_args.args[0])
        with self.store._connect() as db:
            tasks=db.execute('SELECT * FROM travel_tasks').fetchall()
        self.assertEqual(len(tasks),1)
        self.assertEqual(tasks[0]['version'],5)


if __name__=='__main__':
    unittest.main()
