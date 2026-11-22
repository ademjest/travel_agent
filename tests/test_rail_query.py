import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app.request_router import RequestRouter, bind_read_context
from core.chat_transport import ChatEvent
from core.execution_outcome import TaskContext, TravelReadRequest
from core.intent_ir import IntentIR, IntentOperation, parse_intent_ir
from core.tasks import TaskRecord
from core.tool_result import AgentToolContext, TRAIN_TICKETS_TOOL, ToolResult
from evaluation.cases.rail_mcp import FakeSession
from infrastructure.mcp_client import RailMCPConfig, RemoteMCPClient, MCPUnavailable
from infrastructure.rail_client import RailClient, validate_rail_tools
from infrastructure.memory_store import MemoryStore
from tools.registry import catalog_schemas, normalize_tool_call
from tools.travel_query import TravelQueryTool, query_argument_error, rail_parameters, project_read_answer


NOW=datetime(2026,10,27,12,tzinfo=timezone.utc)


class RailQueryContracts(unittest.TestCase):
    def test_seat_filters_are_local_and_keep_the_complete_snapshot_for_later_edits(self):
        from test_intercity_planning import train
        a=train('G1','甲','乙','2026-11-01T09:00','2026-11-01T11:00')
        b=train('G2','甲','乙','2026-11-01T10:00','2026-11-01T12:00')
        a['seats'][0]['availability']='sold_out'
        a['seats'].append({'name':'商务座','price_cny':788,'availability':'available'})
        tool=TravelQueryTool()
        tool.rail=Mock()
        tool.rail.query_sync.return_value={'status':'ok','all_trains':[a,b],'trains':[a,b]}
        result=tool.query_train_tickets('甲','乙','2026-11-01',seat_class='second_class',seat_requirement='required',
            context=AgentToolContext('web','test','owner','fixed-seat-query',execution_data={'reference_time':NOW.isoformat()}))
        self.assertEqual([r['train_code'] for r in result.payload['trains']],['G2'])
        self.assertEqual(len(result.payload['all_trains']),2)
        self.assertNotIn('seat_class',tool.rail.query_sync.call_args.kwargs)
        self.assertEqual(result.payload['request']['seat_class'],'second_class')

    def test_all_day_periods_are_explicit_full_day_without_losing_constraints(self):
        for window in ('全天','整天','一整天','全天都可以','整天都行','不限时间','不限时段','时间不限','全天出发'):
            with self.subTest(window=window):
                values,missing=rail_parameters({'origin':'出发站','destination':'到达站','travel_date':'明天',
                    'train_preference':'high_speed_only','departure_window':window},NOW)
                self.assertFalse(missing)
                self.assertEqual(values['departure_window'],(0,24))
                self.assertEqual(values['train_preference'],'high_speed_only')
        for window in ('不是全天','全天但上午不行','晚一点'):
            with self.subTest(window=window):
                self.assertEqual(rail_parameters({'origin':'出发站','destination':'到达站','travel_date':'明天',
                    'departure_window':window},NOW)[1],('departure_window',))

    def test_all_day_followups_bind_only_the_unique_rail_task(self):
        from app.request_router import rail_time_followup
        task=TaskRecord('task','rail','completed','明天出发站到到达站上午出发',
            {'origin':'出发站','destination':'到达站','travel_date':'明天','departure_window':'上午',
             'reference_time':NOW.isoformat()},(),1,'2099-01-01T00:00:00+00:00','last')
        for text in ('全天','改成整天','明天全天','10月30日整天都行','不限时段'):
            with self.subTest(text=text):
                self.assertTrue(rail_time_followup(text))
                event=ChatEvent('web','group','follow','scope','owner',text,occurred_at=NOW)
                self.assertEqual(bind_read_context(event,(task,)).task.task_id,'task')
                self.assertIsNone(bind_read_context(event,(task,replace(task,task_id='other'))).task)
        for text in ('全天去夫子庙玩','明天不要全天','查询全天营业的餐厅'):
            self.assertFalse(rail_time_followup(text))

    def test_changing_station_to_city_does_not_inherit_the_old_station_scope(self):
        text='改成武汉到南京'
        context=TaskContext(None,text,NOW.isoformat(),{'origin':'武汉站','destination':'南京南',
            'origin_scope':'station','destination_scope':'station','travel_date':'2026-10-30'},text)
        ir=IntentIR(1,'read',(IntentOperation('rail','query',{'origin':'武汉','destination':'南京'}),),source_spans=('武汉','南京'))
        request=TravelReadRequest.from_ir(ir,context)
        parameters,missing=rail_parameters(request.queries[0].values,NOW)
        self.assertFalse(missing)
        self.assertEqual((parameters['origin_scope'],parameters['destination_scope']),('city','city'))

    def test_round_trip_reference_inherits_only_the_identified_direction(self):
        text='修改返程日期，出发站和到达站'
        context=TaskContext(None,text,NOW.isoformat(),{'queries':[
            {'origin':'出发站','destination':'到达站','travel_date':'2026-10-30'},
            {'origin':'到达站','destination':'出发站','travel_date':'2026-11-01'}],
            'missing_fields':[[],[]]},text)
        ir=IntentIR(1,'read',(IntentOperation('rail','query',{'origin':'到达站','destination':'出发站'}),),source_spans=('出发站',))
        request=TravelReadRequest.from_ir(ir,context)
        self.assertEqual(request.queries[0].values['travel_date'],'2026-11-01')
        ambiguous=replace(ir,operations=(IntentOperation('rail','query',{}),))
        self.assertIsNone(TravelReadRequest.from_ir(ambiguous,context))

    def test_generic_rail_contract_repair_does_not_inject_research_schema(self):
        from agents.intent_compiler import IntentCompiler, _intent_prompt
        from test_travel_agent import FakeClient, completion, assistant_message
        text='明天武汉到南京'
        good={'schema_version':1,'action':'read','operations':[{'domain':'rail','operation':'query',
              'origin':'武汉','destination':'南京','travel_date':'明天'}],'source_spans':['明天','武汉','南京']}
        bad=json.loads(json.dumps(good));bad['operations'][0]['unknown_field']='x'
        client=FakeClient([completion(assistant_message(json.dumps(value,ensure_ascii=False))) for value in (bad,good)])
        result=IntentCompiler(client,'fake').compile(text)
        self.assertEqual(result.operations[0].domain,'rail')
        calls=client.chat.completions.requests
        self.assertEqual(len(calls),2)
        self.assertEqual(calls[1]['messages'][0]['content'],_intent_prompt())
        self.assertIn('contract_error',json.loads(calls[1]['messages'][1]['content']))

    def test_legacy_read_implementations_keep_the_two_argument_contract(self):
        tool=TravelQueryTool()
        calls=[]
        def old_read(name,arguments):
            calls.append((name,arguments))
            return ToolResult('completed',name,data='晴，20℃')
        tool.execute_result=old_read
        result=tool.execute_scoped_result('get_current_weather',{'location':'武汉'},None)
        self.assertEqual(result.status,'completed')
        self.assertEqual(calls,[('get_current_weather',{'location':'武汉'})])

    def test_frozen_older_intent_assets_do_not_require_new_fragment_keys(self):
        from agents.intent_compiler import _intent_prompt
        fragments={'protocol'+('_'+str(n) if n>1 else ''):'历史片段' for n in range(1,8)}
        with patch('agents.intent_compiler.skill_text',side_effect=lambda stage,key:fragments[key]):
            self.assertIn('历史片段',_intent_prompt())

    def test_raw_dates_and_strict_scope_are_proposed_without_model_identity(self):
        text='明天出发站到到达站的高铁'
        value={'schema_version':1,'action':'read','operations':[{'domain':'rail','operation':'query',
            'origin':'出发站','destination':'到达站','travel_date':'明天','origin_scope':'station',
            'train_preference':'high_speed_only','train_preference_text':'高铁'}],'source_spans':[text]}
        ir=parse_intent_ir(value,text)
        request=TravelReadRequest.from_ir(ir,TaskContext(None,text,NOW.isoformat()))
        self.assertIsNotNone(request)
        self.assertEqual(request.queries[0].values['travel_date'],'明天')
        self.assertFalse(query_argument_error(TRAIN_TICKETS_TOOL,
            {'origin':'出发站','destination':'到达站','travel_date':'明天','origin_scope':'station','train_preference':'high_speed_only'},request))
        self.assertTrue(query_argument_error(TRAIN_TICKETS_TOOL,
            {'origin':'出发站','destination':'到达站','travel_date':'今天'},request))
        value['operations'][0]['creator_id']='model-user'
        with self.assertRaises(ValueError): parse_intent_ir(value,text)

    def test_default_is_not_model_evidence_and_dates_cannot_be_invented(self):
        text='武汉到南京怎么坐车'
        value={'schema_version':1,'action':'read','operations':[{'domain':'rail','operation':'query',
            'origin':'武汉','destination':'南京','train_preference':'high_speed'}],'source_spans':[text]}
        with self.assertRaises(ValueError): parse_intent_ir(value,text)
        value['operations'][0].pop('train_preference')
        value['operations'][0]['travel_date']='2026-10-28'
        ir=parse_intent_ir(value,text)
        self.assertIsNone(TravelReadRequest.from_ir(ir,TaskContext(None,text,NOW.isoformat())))

    def test_date_and_time_normalization_uses_request_reference_and_boarding_day(self):
        values,missing=rail_parameters({'origin':'出发站','destination':'到达站','travel_date':'明天',
                                      'departure_window':'09:00至11:30'},NOW)
        self.assertFalse(missing)
        self.assertEqual(values['travel_date'],'2026-10-28')
        self.assertEqual(values['departure_window'],(9,11.5))
        self.assertEqual(values['origin_scope'],'station')
        for date_text in ('','某个周末','2026-02-30'):
            self.assertIn('travel_date',rail_parameters({'origin':'武汉','destination':'南京','travel_date':date_text},NOW)[1])
        self.assertEqual(rail_parameters({'origin':'武汉','destination':'南京','travel_date':'明天','departure_window':'晚一点'},NOW)[1],('departure_window',))

    def test_old_catalogs_cannot_acquire_new_rail_action(self):
        for version in ('legacy-v1','compact-v1'):
            self.assertNotIn(TRAIN_TICKETS_TOOL,[v['function']['name'] for v in catalog_schemas(catalog_version=version)])
            with self.assertRaises(ValueError):normalize_tool_call(TRAIN_TICKETS_TOOL,{},version)
        for version in ('legacy-v2','compact-v2'):
            self.assertIn(TRAIN_TICKETS_TOOL,[v['function']['name'] for v in catalog_schemas(catalog_version=version)])

    def test_followup_uses_only_unique_rail_task_and_preserves_time_reference(self):
        task=TaskRecord('task','rail','completed','明天出发站到到达站',
            {'origin':'出发站','destination':'到达站','travel_date':'明天','reference_time':NOW.isoformat()},(),1,
            '2026-10-28T00:00:00+00:00','last')
        event=ChatEvent('web','group','follow','scope','owner','改成后天，晚一点出发',occurred_at=NOW)
        bound=bind_read_context(event,(task,))
        self.assertEqual(bound.task.task_id,'task')
        self.assertEqual(bound.slots['destination'],'到达站')
        self.assertIsNone(bind_read_context(event,(task,replace(task,task_id='other'))).task)


class RailWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_day_followup_replaces_morning_and_sends_full_provider_window(self):
        recorded=[]; original=self.mcp.call
        async def capture(name,arguments):
            if name=='get-tickets':recorded.append(dict(arguments))
            return await original(name,arguments)
        self.mcp.call=capture
        first='明天出发站到到达站上午出发'
        compiler=Mock()
        compiler.try_compile.side_effect=[
            IntentIR(1,'read',(IntentOperation('rail','query',{'origin':'出发站','destination':'到达站',
                'travel_date':'明天','departure_window':'上午'}),),source_spans=(first,)),
            IntentIR(1,'read',(IntentOperation('rail','query',{'departure_window':'整天'}),),source_spans=('整天',))]
        router=RequestRouter(self.store,compiler,read_only_service=self.tool)
        for index,text in enumerate((first,'改成整天')):
            event=replace(self.event,event_id='all-day-'+str(index),content=text)
            reply,_=await asyncio.to_thread(router.route,event,self.store.delivery.begin_event(event.event_key))
        self.assertEqual([(r['earliestStartTime'],r['latestStartTime']) for r in recorded],[(9,12),(0,24)])
        self.assertIn('00:00–24:00',reply)
        self.assertIn('D1',reply)
        self.assertEqual(self.store.tasks.recent('web',event.storage_scope_id,event.sender_id)[0].slots['departure_window'],'整天')

    async def test_changed_scope_is_not_reintroduced_into_persisted_task(self):
        text='改成武汉到南京'
        context=TaskContext(None,text,NOW.isoformat(),{'origin':'武汉站','destination':'南京南',
            'origin_scope':'station','destination_scope':'station','travel_date':'2026-10-30'},text)
        ir=IntentIR(1,'read',(IntentOperation('rail','query',{'origin':'武汉','destination':'南京'}),),source_spans=('武汉','南京'))
        self.tool.tool_executor=Mock(return_value=ToolResult('needs_input',TRAIN_TICKETS_TOOL,
            data='请补充出发时段',missing_fields=('departure_window',)))
        await asyncio.to_thread(self.tool.prepare,self.event,self.claim,ir,context)
        task=self.store.tasks.recent('web',self.event.storage_scope_id,self.event.sender_id)[0]
        self.assertNotIn('origin_scope',task.slots)
        self.assertNotIn('destination_scope',task.slots)
        self.assertEqual(task.slots['origin'],'武汉')

    async def test_reference_uses_expired_turn_snapshot_and_creates_new_task(self):
        text='明天出发站到到达站上午出发'
        event=replace(self.event,content=text)
        ir=IntentIR(1,'read',(IntentOperation('rail','query',{'origin':'出发站','destination':'到达站',
            'travel_date':'明天','departure_window':'上午'}),),source_spans=(text,))
        await asyncio.to_thread(self.tool.prepare,event,self.claim,ir)
        original=self.store.tasks.recent('web',event.storage_scope_id,event.sender_id)[0]
        with self.store._connect() as db:
            db.execute('INSERT INTO outbound_message_links VALUES (?,?,?,?,?)',
                ('web',event.storage_scope_id,'web-1',event.event_key,event.sender_id))
            db.execute("UPDATE travel_tasks SET slots_json=?,expires_at='2000-01-01T00:00:00+00:00' WHERE task_id=?",
                (json.dumps({'origin':'后来更正的站'}),original.task_id))
        snapshot=self.store.tasks.rail_references('web',event.storage_scope_id,event.sender_id,['web-1'])['web-1']
        self.assertEqual(snapshot['queries'][0]['origin'],'出发站')
        self.assertEqual(snapshot['queries'][0]['travel_date'],'2026-10-28')
        self.assertFalse(self.store.tasks.rail_references('web','other',event.sender_id,['web-1']))
        follow=replace(event,event_id='quoted',content='改成明天下午',reply_to_id='web-1',occurred_at=NOW+timedelta(days=1))
        compiler=Mock()
        compiler.try_compile.return_value=IntentIR(1,'read',(IntentOperation('rail','query',
            {'travel_date':'明天','departure_window':'下午'}),),source_spans=('明天','下午'))
        router=RequestRouter(self.store,compiler,read_only_service=self.tool)
        await asyncio.to_thread(router.route,follow,self.store.delivery.begin_event(follow.event_key))
        current=self.store.tasks.recent('web',event.storage_scope_id,event.sender_id)[0]
        self.assertNotEqual(current.task_id,original.task_id)
        self.assertEqual(current.slots['origin'],'出发站')
        self.assertEqual(current.slots['travel_date'],'明天')
        self.assertEqual(current.slots['reference_event_id'],event.event_key)
        self.assertEqual(self.mcp.counts['tools_call'],6)

    async def test_reference_preserves_unanswered_optional_field_without_resubmitting(self):
        text='明天出发站到到达站晚一点出发'
        event=replace(self.event,content=text)
        ir=IntentIR(1,'clarify',(IntentOperation('rail','query',{'origin':'出发站','destination':'到达站',
            'travel_date':'明天'}),),missing_fields=('departure_window',),source_spans=(text,))
        await asyncio.to_thread(self.tool.prepare,event,self.claim,ir)
        with self.store._connect() as db:
            db.execute('INSERT INTO outbound_message_links VALUES (?,?,?,?,?)',
                ('web',event.storage_scope_id,'web-1',event.event_key,event.sender_id))
        follow=replace(event,event_id='quoted-pending',content='改成后天',reply_to_id='web-1')
        compiler=Mock(); compiler.try_compile.return_value=IntentIR(1,'read',
            (IntentOperation('rail','query',{'travel_date':'后天'}),),source_spans=('后天',))
        reply,_=await asyncio.to_thread(RequestRouter(self.store,compiler,read_only_service=self.tool).route,
            follow,self.store.delivery.begin_event(follow.event_key))
        self.assertIn('出发时段',reply)
        self.assertEqual(self.mcp.counts['tools_call'],0)

    async def test_missing_window_marker_invalidates_inherited_optional_window(self):
        text='改成后天，晚一点出发'
        context=TaskContext(None,text,NOW.isoformat(),{'origin':'出发站','destination':'到达站',
            'travel_date':'明天','departure_window':'上午'},text)
        ir=IntentIR(1,'clarify',(IntentOperation('rail','query',{'travel_date':'后天'}),),
                    missing_fields=('departure_window',),source_spans=(text,))
        reply=await asyncio.to_thread(self.tool.prepare,self.event,self.claim,ir,context)
        self.assertIn('出发时段',reply)
        self.assertEqual(self.mcp.counts['tools_call'],0)
        task=self.store.tasks.recent('web',self.event.storage_scope_id,self.event.sender_id)[0]
        self.assertEqual(task.status,'collecting')
        self.assertNotIn('departure_window',task.slots)
        self.assertEqual(task.slots['origin'],'出发站')
        self.assertEqual(task.missing_slots,('departure_window',))

    async def test_acknowledged_retry_receipts_are_replayed_without_new_submission(self):
        from mcp.types import CallToolResult, TextContent
        original=self.mcp.call;attempts=0
        async def intermittent(name,arguments):
            nonlocal attempts
            if name=='get-tickets':
                attempts+=1
                if attempts==1:
                    return CallToolResult(isError=True,content=[TextContent(type='text',text='provider failure')])
            return await original(name,arguments)
        self.mcp.call=intermittent
        args={'origin':'出发站','destination':'到达站','travel_date':'明天'}
        first=await asyncio.to_thread(self.tool.query_train_tickets,**args,context=self.ctx)
        second=await asyncio.to_thread(self.tool.query_train_tickets,**args,context=self.ctx)
        self.assertEqual(first.status,'completed')
        self.assertEqual(first.data,second.data)
        self.assertEqual(attempts,2)
        with self.store._connect() as db:
            receipts=db.execute("SELECT result_text FROM event_tool_results WHERE tool_name='rail:get-tickets'").fetchall()
        self.assertEqual(len(receipts),2)
        self.assertEqual([json.loads(r['result_text'])['result']['isError'] for r in receipts],[True,False])

    async def test_proven_unsubmitted_failure_does_not_poison_rpc_checkpoint(self):
        original=self.mcp.call;blocked=False
        async def unavailable(name,arguments):
            nonlocal blocked
            if not blocked:
                blocked=True
                raise MCPUnavailable('queue_full',submitted=False)
            return await original(name,arguments)
        self.mcp.call=unavailable
        args={'origin':'出发站','destination':'到达站','travel_date':'明天'}
        first=await asyncio.to_thread(self.tool.query_train_tickets,**args,context=self.ctx)
        second=await asyncio.to_thread(self.tool.query_train_tickets,**args,context=self.ctx)
        self.assertEqual(first.status,'failed')
        self.assertEqual(second.status,'completed')
        self.assertEqual(self.mcp.counts['tools_call'],3)

    async def test_persistent_provider_failure_replays_both_failure_receipts(self):
        from mcp.types import CallToolResult
        original=self.mcp.call;attempts=0
        async def failed(name,arguments):
            nonlocal attempts
            if name=='get-tickets':
                attempts+=1
                return CallToolResult(isError=True,content=[])
            return await original(name,arguments)
        self.mcp.call=failed
        args={'origin':'出发站','destination':'到达站','travel_date':'明天'}
        first=await asyncio.to_thread(self.tool.query_train_tickets,**args,context=self.ctx)
        second=await asyncio.to_thread(self.tool.query_train_tickets,**args,context=self.ctx)
        self.assertEqual(first.error_code,'provider_error')
        self.assertEqual(first.data,second.data)
        self.assertIn('远端工具处理失败',first.data)
        self.assertEqual(attempts,2)

    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=MemoryStore(Path(self.temp.name)/'rail.db')
        @asynccontextmanager
        async def factory(config):yield FakeSession()
        self.mcp=RemoteMCPClient(RailMCPConfig(enabled=True),validate_tools=validate_rail_tools,session_factory=factory)
        self.mcp.start();await self.mcp.wait_ready()
        self.tool=TravelQueryTool(store=self.store)
        self.tool.enabled=True;self.tool.rail=RailClient(self.mcp)
        self.event=ChatEvent('web','group','rail-request','scope','owner','明天出发站到到达站怎么坐车',occurred_at=NOW)
        self.claim=self.store.delivery.begin_event(self.event.event_key)
        self.ctx=AgentToolContext('web','scope','owner',self.event.event_key,event=self.event)

    async def asyncTearDown(self):
        await self.mcp.close();self.temp.cleanup()

    async def test_no_amap_key_required_and_same_query_replays_owned_result(self):
        arguments={'origin':'出发站','destination':'到达站','travel_date':'明天'}
        first=await asyncio.to_thread(self.tool.execute_scoped_result,TRAIN_TICKETS_TOOL,arguments,self.ctx)
        second=await asyncio.to_thread(self.tool.execute_scoped_result,TRAIN_TICKETS_TOOL,arguments,self.ctx)
        self.assertEqual(first.status,'completed')
        self.assertEqual(first,second)
        self.assertIn('2026-10-29 01:00',first.data)
        self.assertIsNone(self.tool.amap)
        self.assertEqual(self.mcp.counts['tools_call'],3)
        self.assertEqual(first.payload['defaults']['train_preference']['source'],'default')
        self.assertIsNone(first.payload['trains'][0]['seats'][0]['price_cny'])
        self.assertIn('动车备选',first.data)
        projected=project_read_answer('二等座99元且一定有票',(first,))
        self.assertNotIn('99元',projected.reply)

    async def test_missing_date_never_calls_supplier_and_only_asks_missing_field(self):
        ir=IntentIR(1,'clarify',(IntentOperation('rail','query',{'origin':'出发站','destination':'到达站'}),),source_spans=('出发站','到达站'))
        result=await asyncio.to_thread(self.tool.prepare,self.event,self.claim,ir)
        self.assertIn('乘车日期',result)
        self.assertNotIn('请补充起点',result)
        self.assertEqual(self.mcp.counts['tools_call'],0)
        self.assertFalse(self.store.trips.list_for_owner(self.event))

    async def test_runtime_loop_cannot_be_blocked_by_synchronous_bridge(self):
        with self.assertRaises(MCPUnavailable):
            self.mcp.run_sync(lambda:self.mcp.call('get-station-code-of-citys',{'citys':'武汉'}))

    async def test_confirmed_lookup_checkpoints_survive_loss_of_final_result(self):
        args={'origin':'出发站','destination':'到达站','travel_date':'明天'}
        first=await asyncio.to_thread(self.tool.query_train_tickets,**args,context=self.ctx)
        second=await asyncio.to_thread(self.tool.query_train_tickets,**args,context=self.ctx)
        self.assertEqual(first.data,second.data)
        self.assertEqual(self.mcp.counts['tools_call'],3)

    async def test_unconfirmed_remote_submission_is_never_automatically_resent(self):
        original=self.mcp.call
        calls=0
        async def failed(name,arguments):
            nonlocal calls
            if name=='get-tickets':
                calls+=1;raise MCPUnavailable('timeout')
            return await original(name,arguments)
        self.mcp.call=failed
        args={'origin':'出发站','destination':'到达站','travel_date':'明天'}
        first=await asyncio.to_thread(self.tool.query_train_tickets,**args,context=self.ctx)
        second=await asyncio.to_thread(self.tool.query_train_tickets,**args,context=self.ctx)
        self.assertEqual(first.status,'failed');self.assertEqual(second.status,'failed')
        self.assertEqual(calls,1)

    async def test_router_executes_rail_without_creating_trip_or_booking(self):
        ir=IntentIR(1,'read',(IntentOperation('rail','query',{'origin':'出发站','destination':'到达站','travel_date':'明天'}),),source_spans=('明天','出发站','到达站'))
        trip=Mock()
        router=RequestRouter(self.store,Mock(try_compile=Mock(return_value=ir)),trip_service=trip,read_only_service=self.tool)
        result=await asyncio.to_thread(router.route,self.event,self.claim)
        self.assertIn('D1',result[0] if isinstance(result,tuple) else result)
        trip.apply_text_intent.assert_not_called()

    async def test_city_transfer_explicit_driving_and_flight_never_switch_to_rail(self):
        for index,(text,domain,operation,values) in enumerate((
            ('南京南到夫子庙坐地铁怎么走','transit','query',{'origin':'南京南','destination':'夫子庙','city':'南京'}),
            ('开车从武汉到南京怎么走','route','query',{'origin':'武汉','destination':'南京'}),
            ('只查机票','system','unsupported_transport',{'request_text':'只查机票'}))):
            event=replace(self.event,event_id='other-'+str(index),content=text)
            ir=IntentIR(1,'read',(IntentOperation(domain,operation,values),),source_spans=(text,))
            router=RequestRouter(self.store,Mock(try_compile=Mock(return_value=ir)),read_only_service=self.tool)
            await asyncio.to_thread(router.route,event,self.store.delivery.begin_event(event.event_key))
        self.assertEqual(self.mcp.counts['tools_call'],0)

    async def test_generic_intercity_how_to_go_does_not_force_amap_driving(self):
        event=replace(self.event,event_id='generic',content='明天出发站到到达站怎么走')
        ir=IntentIR(1,'read',(IntentOperation('rail','query',{'origin':'出发站','destination':'到达站','travel_date':'明天'}),),source_spans=('明天','出发站','到达站'))
        result=await asyncio.to_thread(self.tool.prepare,event,self.store.delivery.begin_event(event.event_key),ir)
        self.assertIn('D1',result)

    async def test_followup_preserves_endpoints_and_date_until_time_window_is_clear(self):
        messages=['出发站到到达站上午的车次有哪些','改成后天，晚一点出发','晚上']
        values=[{'origin':'出发站','destination':'到达站','departure_window':'上午'},
                {'travel_date':'后天','departure_window':'晚一点'}, {'departure_window':'晚上'}]
        compiler=Mock()
        compiler.try_compile.side_effect=[IntentIR(1,'read',(IntentOperation('rail','query',value),),source_spans=(message,))
                                         for message,value in zip(messages,values)]
        router=RequestRouter(self.store,compiler,read_only_service=self.tool)
        replies=[]
        for index,message in enumerate(messages):
            event=replace(self.event,event_id='continue-'+str(index),content=message,
                          occurred_at=NOW+timedelta(days=1) if index==2 else NOW)
            result=await asyncio.to_thread(router.route,event,self.store.delivery.begin_event(event.event_key))
            replies.append(result[0] if isinstance(result,tuple) else result)
            if index<2:self.assertEqual(self.mcp.counts['tools_call'],0)
        self.assertIn('乘车日期',replies[0])
        self.assertIn('出发时段',replies[1])
        self.assertIn('2026-10-29',replies[2])
        self.assertIn('出发站 → 到达站',replies[2])
        self.assertEqual(self.mcp.counts['tools_call'],3)
