from app.request_router import bind_read_context
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

from core.chat_transport import ChatEvent
from core.execution_outcome import TaskContext,TravelReadRequest
from core.intent_ir import IntentIR,IntentOperation
from core.settings import Settings
from core.tool_result import ToolResult
from infrastructure.memory_store import MemoryStore
from tools.travel_query import TravelQueryTool, ReadCoverageError
from agents.travel_agent import TravelAgent, run_read_agent
from app.conversation_context import AgentContext
from test_travel_agent import FakeClient,completion,assistant_message,tool_call
from test_read_only_answer import WEATHER


class ReadOnlyServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=MemoryStore(Path(self.tmp.name)/'test.db')
        self.event=ChatEvent('web','group','read','c','owner','武汉天气')

    def test_scope_and_single_persisted_owner_for_command(self):
        tool=Mock(return_value=WEATHER)
        owner=TravelQueryTool(store=self.store, backend=object(), tool_executor=tool)
        event=replace(self.event,content='查询天气 武汉')
        claim=self.store.delivery.begin_event(event.event_key)
        reply=owner.prepare(event,claim,owner.command_ir(event.content),source='command')
        self.assertIn('20℃',reply)
        self.assertEqual(tool.call_args.args[2].creator_id,'owner')
        with self.store._connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM task_event_results').fetchone()[0],1)
        self.assertIn('20℃',owner.resume(event,claim))
        self.assertEqual(tool.call_count,1)

    def test_write_ir_and_uncovered_command_never_gain_tools(self):
        tool=Mock();owner=TravelQueryTool(store=self.store, backend=object(), tool_executor=tool)
        ir=IntentIR(1,'update',(IntentOperation('trip','update_constraints',{}),),source_spans=('武汉天气',))
        reply=owner.prepare(self.event,None,ir)
        self.assertIn('未通过校验',reply)
        self.assertIsNone(owner.command_ir('查询天气武汉，顺便取消预约'))
        tool.assert_not_called()

    def test_contextless_command_does_not_use_another_events_checkpoints(self):
        class Travel:
            manages_read_retries = False
            def execute_result(self,name,values):return WEATHER
        scoped=Mock(side_effect=AssertionError('Contextless read reached scoped executor'))
        owner=TravelQueryTool(store=self.store, backend=Travel(), tool_executor=scoped)
        self.assertIn('20℃',owner.command('查询天气 武汉'))
        self.assertIn('20℃',owner.command('查询天气 武汉'))
        scoped.assert_not_called()
        with self.store._connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM event_tool_results').fetchone()[0],0)

    def test_coverage_hints_cannot_expand_ir_or_confuse_entity_names(self):
        owner=TravelQueryTool(store=self.store, backend=object(), tool_executor=Mock())
        event=replace(self.event,content='从武汉站去武汉天气博物馆，分析一下路线')
        ir=IntentIR(1,'read',(IntentOperation('route','query',{'origin':'武汉站','destination':'武汉天气博物馆'}),),source_spans=('武汉站','武汉天气博物馆'))
        request=owner.prepare(event,None,ir)
        self.assertIsInstance(request,TravelReadRequest)
        self.assertEqual([q.domain for q in request.queries],['route'])
        event=replace(event,content='从武汉站去武汉天气博物馆，分析一下路线，并告诉我是否带伞')
        with self.assertRaises(ReadCoverageError):owner.prepare(event,None,ir)
        owner.tool_executor.assert_not_called()

    def test_read_request_is_authoritative_without_agent_reclassification(self):
        event=replace(self.event,content='武汉天气')
        request=TravelReadRequest((IntentOperation('weather','query',{'location':'武汉'}),),bind_read_context(event,()))
        client=FakeClient([completion(assistant_message(tool_calls=[tool_call('q','get_current_weather','{"location":"武汉"}')])),completion(assistant_message('武汉40℃'))])
        agent=TravelAgent(Settings('','',frozenset(),'','script','https://example.test','script'),lambda *args:WEATHER,client)
        with patch('app.request_router.RequestRouter.handle',side_effect=AssertionError('Agent must not reroute')):
            result=run_read_agent(event,request,agent,AgentContext((),'', '', ''),None)
        self.assertIn('20℃',result.reply)
        self.assertNotIn('40℃',result.reply)

    def test_forecast_followup_keeps_existing_read_identity(self):
        from core.tasks import TaskRecord
        task=TaskRecord('task','weather','completed','武汉天气',{'location':'武汉','intents':['weather']},(),2,'','old')
        event=replace(self.event,content='那明天呢')
        request=TravelQueryTool.from_context(bind_read_context(event,(task,)))
        self.assertEqual(request.queries[0].domain,'forecast')
        self.assertEqual(request.queries[0].values,{'location':'武汉'})
        self.assertEqual(request.context.task.version,2)

    def test_missing_weather_location_keeps_zero_model_clarification(self):
        from test_travel_agent import execution_policy
        event=replace(self.event,content='现在天气怎么样')
        client=FakeClient([]);tool=Mock(side_effect=AssertionError('Missing location reached a tool'))
        agent=TravelAgent(Settings('','',frozenset(),'','script','https://example.test','script'),tool,client)
        result=run_read_agent(event,None,agent,AgentContext((),'', '', ''),None,
            decision=execution_policy(('weather',),('get_current_weather',)))
        self.assertEqual(result.status,'needs_input')
        self.assertEqual(result.task_update.missing_slots,('location',))
        self.assertEqual(client.completions.requests,[])
        tool.assert_not_called()

    def test_same_domain_query_coverage_is_not_certified_by_one_success(self):
        event=replace(self.event,content='比较武汉和长沙天气')
        queries=tuple(IntentOperation('weather','query',{'location':city}) for city in ('武汉','长沙'))
        request=TravelReadRequest(queries,bind_read_context(event,()),explain=True)
        client=FakeClient([completion(assistant_message(tool_calls=[tool_call('q','get_current_weather','{"location":"武汉"}')])),completion(assistant_message('都已完成'))])
        agent=TravelAgent(Settings('','',frozenset(),'','script','https://example.test','script'),lambda *args:WEATHER,client)
        result=run_read_agent(event,request,agent,AgentContext((),'', '', ''),None)
        self.assertEqual(result.status,'partial')
        self.assertIn('尚未完成：长沙',result.reply)
        self.assertIn('missing_queries',result.answer_reasons)

    def test_batch_requires_usable_facts_for_each_operation_and_replays_once(self):
        event=replace(self.event,content='查询天气 武汉；查询路线 武汉→宜昌')
        queries=(IntentOperation('weather','query',{'location':'武汉'}),
                 IntentOperation('route','query',{'origin':'武汉','destination':'宜昌'}))
        request=TravelReadRequest(queries,bind_read_context(event,()),source='rule')
        tool=Mock(side_effect=[WEATHER,ToolResult('completed','get_driving_route',data='查询成功')])
        owner=TravelQueryTool(store=self.store, backend=object(), tool_executor=tool)
        claim=self.store.delivery.begin_event(event.event_key)
        reply=owner.execute(event,claim,request)
        self.assertIn('20℃',reply)
        self.assertIn('尚未完成',reply)
        with self.store._connect() as db:
            row=db.execute('SELECT status,slots_json FROM travel_tasks').fetchone()
        import json
        self.assertEqual(row['status'],'failed')
        operations=json.loads(row['slots_json'])['operations']
        self.assertEqual([o['status'] for o in operations],['completed','failed'])
        self.assertEqual(owner.execute(event,claim,request),reply)
        self.assertEqual(tool.call_count,2)

    def test_agent_does_not_complete_a_city_whose_tool_returned_no_facts(self):
        event=replace(self.event,content='比较武汉和长沙天气')
        request=TravelReadRequest(tuple(IntentOperation('weather','query',{'location':c}) for c in ('武汉','长沙')),
                                 bind_read_context(event,()),explain=True)
        client=FakeClient([completion(assistant_message(tool_calls=[
            tool_call('a','get_current_weather','{"location":"武汉"}'),
            tool_call('b','get_current_weather','{"location":"长沙"}')])),completion(assistant_message('都已完成'))])
        def tool(name,values,*rest):
            return WEATHER if values['location']=='武汉' else ToolResult('completed',name,data='查询成功')
        agent=TravelAgent(Settings('','',frozenset(),'','script','https://example.test','script'),tool,client)
        result=run_read_agent(event,request,agent,AgentContext((),'', '', ''),None)
        self.assertEqual(result.status,'partial')
        self.assertIn('长沙',result.reply)
        self.assertIn('20℃',result.reply)


if __name__=='__main__':unittest.main()
