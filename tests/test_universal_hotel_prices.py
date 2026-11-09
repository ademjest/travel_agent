"""Independent hotel requests through public catalog, read owner and persistent continuations."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime,timezone
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import Mock,patch

from app.request_router import RequestRouter, bind_read_context, read_task_continuation
from agents.travel_agent import TravelAgent
from core.chat_transport import ChatEvent
from core.execution_outcome import TaskContext, TravelReadRequest, CONVERSATION_POLICY
from core.hotel_contract import evidenced_request
from core.intent_ir import IntentIR, IntentOperation, parse_intent_ir, trip_read_check
from core.settings import Settings
from core.tasks import TaskRecord
from core.tool_result import AgentToolContext, ToolResult, DEFAULT_CATALOG, HOTEL_CATALOG, COMPACT_HOTEL_CATALOG
from infrastructure.memory_store import MemoryStore
from infrastructure.wendao_client import WendaoClient, ENDPOINT
from tools.hotel_prices.tool import HotelQuotes
from tools.registry import AgentToolRouter, catalog_schemas, normalize_tool_call
from tools.travel_query import TravelQueryTool
from test_hotel_quotes import fixture, STAY, TARGET
from test_travel_agent import FakeClient, completion, assistant_message, tool_call
from test_trips import model_response
from test_read_only_answer import WEATHER

NAME=TARGET['h']['name']
NOW=datetime.now(timezone.utc)
FULL=f'测试城{NAME}，2030-01-07入住、2030-01-09退房，1间房2名成人，无儿童，确认房价'
REQUEST={'targets':[{'name':NAME,'city':'测试城'}],'stay':deepcopy(STAY)}

class UniversalHotelTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.store=MemoryStore(Path(tmp.name)/'hotel.db')
        raw,offer=fixture();self.transport=Mock(return_value=(raw.encode(),ENDPOINT,'text/markdown'))
        self.models=FakeClient([model_response({'offers':[offer]}) for _ in range(8)])
        self.map=Mock();self.map.search_places.return_value=list(TARGET.values())
        self.hotel=HotelQuotes(self.store,WendaoClient('synthetic',True,transport=self.transport),self.models,'test',amap=self.map)
        self.owner=TravelQueryTool(store=self.store,backend=Mock());self.owner.hotel_prices=self.hotel
        self.router=AgentToolRouter(self.owner,Mock(),self.store.tool_artifacts,delivery_repository=self.store.delivery)
        self.owner.tool_executor=self.router.execute_result
        self.counter=0

    def event(self,text=FULL,**changes):
        self.counter+=1
        return replace(ChatEvent('onebot','group',str(self.counter),'scope','owner',text,occurred_at=NOW),**changes)

    def call(self,request=None,event=None,slots=None):
        event=event or self.event();request=deepcopy(REQUEST if request is None else request)
        context=AgentToolContext(event.platform,event.scope_id,event.sender_id,event.event_key,event=event,
            catalog_version=DEFAULT_CATALOG,allowed_actions=frozenset({'query_hotel_prices'}),execution_data={'reference_time':NOW.isoformat(),**(slots or {})})
        return self.router.execute_result('query_hotel_prices',request,context)

    def turn(self,text,values,context=None):
        event=self.event(text);claim=self.store.delivery.begin_event(event.event_key)
        ir=IntentIR(1,'read',(IntentOperation('hotel','query',values),),source_spans=(text,))
        ctx=context or bind_read_context(event,self.store.tasks.recent(event.platform,event.storage_scope_id,event.sender_id,now=NOW))
        # The read owner persists its receipt and task; no model/intent replacement is claimed here.
        reply=self.owner.prepare(event,claim,ir,ctx)
        with self.store._connect() as db:
            row=db.execute('SELECT task_id FROM task_event_results WHERE event_id=?',(event.event_key,)).fetchone()
        with self.store._connect() as db:
            record=db.execute('SELECT * FROM travel_tasks WHERE task_id=?',(row['task_id'],)).fetchone() if row else None
        task=self.store.tasks._record(record) if record else None
        return reply,task,event

    def test_public_catalog_versions_and_read_boundary(self):
        for version in (HOTEL_CATALOG,COMPACT_HOTEL_CATALOG):
            self.assertIn('query_hotel_prices',{s['function']['name'] for s in catalog_schemas(catalog_version=version)})
        for version in ('legacy-v1','compact-v1','legacy-v2','compact-v2'):
            self.assertNotIn('query_hotel_prices',{s['function']['name'] for s in catalog_schemas(catalog_version=version)})
            with self.assertRaises(ValueError):normalize_tool_call('query_hotel_prices',{},version)
        definition=self.router.registry.definitions['query_hotel_prices']
        self.assertFalse(definition.parallel_safe);self.assertFalse(definition.repeatable)
        self.assertEqual(definition.mode,'read');self.assertTrue(definition.requires_context)
        result=self.router.execute_result('query_hotel_prices',{},None)
        self.assertEqual(result.error_code,'missing_context')

    def test_no_trip_no_budget_and_no_formal_writes(self):
        result=self.call()
        self.assertEqual(result.status,'completed',result.data);self.assertEqual(len(result.payload['offers']),1)
        self.assertIn('399',result.data);self.assertEqual(self.transport.call_count,1)
        self.assertFalse(self.store.trips.list_for_owner(self.event()))
        self.assertEqual(result.effect_status,'none');self.assertNotIn('budget',result.payload['request'])

    def test_missing_fields_and_empty_children_are_not_schema_errors(self):
        for stay,missing in (({}, {'check_in','check_out','rooms','adults','children_ages'}),
                             ({'children_ages':[]},{'check_in','check_out','rooms','adults'})):
            request={'targets':REQUEST['targets'],'stay':stay}
            result=self.call(request)
            self.assertEqual(result.status,'needs_input',result.data);self.assertEqual(set(result.missing_fields),missing)
        self.transport.assert_not_called();self.map.search_places.assert_not_called()
        self.assertEqual(self.call({'stay':{'rooms':'1'}}).error_code,'invalid_argument')

    def test_partial_three_turn_continuation_without_query_word(self):
        reply,task,_=self.turn(f'确认测试城{NAME}的房价',{'targets':REQUEST['targets']})
        self.assertEqual(task.status,'collecting');self.assertIn('儿童',reply)
        reply,task,_=self.turn('2030-01-07入住，2030-01-09退房',{'stay':{k:STAY[k] for k in ('check_in','check_out')}})
        self.assertEqual(task.slots['hotel_request']['targets'],REQUEST['targets']);self.assertNotIn('入住日期',task.missing_slots)
        reply,task,_=self.turn('1间房，两人，不带孩子',{'stay':{'rooms':1,'adults':2,'children_ages':[]}})
        self.assertEqual(task.status,'completed',reply);self.assertIn('399',reply);self.assertEqual(self.transport.call_count,1)

    def test_mode_lookup_cache_refresh_and_saved_are_distinct(self):
        first=self.call();second=self.call();self.assertTrue(second.payload['cache_hit']);self.assertEqual(self.transport.call_count,1)
        event=self.event();refresh={**REQUEST,'mode':'refresh'}
        self.call(refresh,event);self.call(refresh,event);self.assertEqual(self.transport.call_count,2)
        historical=self.call({'mode':'saved'},self.event('不要重新查询，查看酒店报价原文'))
        self.assertEqual(historical.status,'completed');self.assertIn('### 已保存的问道原文',historical.data)
        self.assertEqual(self.transport.call_count,2)

    def test_current_confirmation_is_not_forced_to_saved_or_write(self):
        text='那确认一下酒店的房价'
        for mode in ('lookup','refresh'):
            check=trip_read_check({'scope':'accommodation','mode':mode,'source':text},text)
            self.assertEqual(check['mode'],mode)
        reply,task,_=self.turn(text,{})
        self.assertEqual(task.status,'collecting');self.assertEqual(task.slots['hotel_request']['mode'],'lookup')
        self.assertIn('入住日期',reply);self.transport.assert_not_called()

    def test_real_model_chinese_dates_and_nested_missing_names_regression(self):
        from core.hotel_contract import resolve_stay
        stay=resolve_stay({'check_in':'2030年1月7日','check_out':'2030年1月9日'},NOW)
        self.assertEqual(stay,{'check_in':'2030-01-07','check_out':'2030-01-09'})
        text='那确认一下酒店的房价';original='酒店我想住华住会旗下的酒店或者如家旗下的酒店'
        combined=original+'\n用户补充：'+text
        ir=IntentIR(1,'clarify',(IntentOperation('hotel','query',{'mode':'lookup','discovery':{'preferred_groups':['华住会','如家']}}),),
            missing_fields=('city','check_in','rooms','adults','children_ages'),source_spans=(text,))
        event=self.event(text);context=TaskContext(None,combined,NOW.isoformat(),{'hotel_evidence':combined},text)
        reply=self.owner.prepare(event,None,ir,context)
        self.assertIn('城市',reply);self.assertIn('入住',reply);self.transport.assert_not_called()
        extended=resolve_stay(STAY,NOW,'再住一晚',STAY)
        self.assertEqual(extended['check_out'],'2030-01-10')
        numbered=resolve_stay({'check_in':'7号','check_out':'9号'},NOW,previous=STAY)
        self.assertEqual(numbered,{'check_in':'2030-01-07','check_out':'2030-01-09'})
        self.assertEqual(evidenced_request({'stay':{'children_ages':[6]}},'六岁儿童')['stay']['children_ages'],[6])

    def test_owned_reference_order_and_target_changes(self):
        first=self.call();snapshot=deepcopy(first.payload['snapshot'])
        other={**TARGET['h'],'id':'other','name':'另一酒店(北门店)'}
        snapshot['targets']={'other':other,**snapshot['targets']}
        slots={'hotel_request':{'targets':[{'name':p['name'],'city':'测试城'} for p in snapshot['targets'].values()],'stay':STAY},'hotel_targets':snapshot['targets']}
        result=self.call({'reference':{'selection':'第二家','ordinal':2}},self.event('第二家那两晚多少钱'),slots)
        self.assertEqual(list(result.payload['resolved_targets']),['h']);self.assertTrue(result.payload['cache_hit'])
        missing=self.call({'reference':{'selection':'这几家'}},self.event('这几家多少钱'))
        self.assertEqual(missing.status,'needs_input');self.assertIn('targets',missing.missing_fields)

    def test_ambiguous_branch_and_cross_city_do_not_borrow_prices(self):
        self.map.search_places.return_value=[*TARGET.values(),{**TARGET['h'],'id':'h2'}]
        result=self.call();self.assertEqual(result.status,'needs_input');self.assertEqual(len(result.payload['target_candidates']),2)
        request=deepcopy(REQUEST);request['targets'].append({'name':'异地酒店','city':'其他城'})
        result=self.call(request,self.event(FULL+'和其他城异地酒店'))
        self.assertEqual(result.status,'needs_input');self.transport.assert_not_called()

    def test_source_limits_configuration_and_historical_view(self):
        self.call();calls=self.transport.call_count
        for text in ('只用高德','不联网','只用官网'):
            result=self.call({**REQUEST,'mode':'refresh'},self.event(FULL+'，'+text))
            self.assertEqual(result.status,'failed',text);self.assertEqual(self.transport.call_count,calls)
        result=self.call({**REQUEST,'mode':'refresh'},self.event(FULL+'，仅用问道',scope_id='fresh-scope'))
        self.assertEqual(result.error_code,'identity_unverified');self.assertEqual(self.transport.call_count,calls)
        self.hotel.enabled=False
        saved=self.call({'mode':'saved'},self.event('之前的酒店价格'))
        self.assertEqual(saved.status,'completed');self.assertTrue(saved.payload['historical'])
        self.assertEqual(self.transport.call_count,calls)

    def test_foreign_scope_cannot_read_raw_or_linked_records(self):
        self.call()
        for changes in ({'sender_id':'other'},{'scope_id':'other'},{'platform':'other'}):
            result=self.call({'mode':'saved'},self.event('查看旧报价',**changes))
            self.assertNotIn('399',result.data);self.assertFalse(result.payload['offers'])
        self.assertEqual(self.transport.call_count,1)

    def test_unsupported_rooms_invalid_dates_and_no_adult_guess(self):
        for stay,text,expected in (({**STAY,'rooms':2},FULL.replace('1间房','2间房'),'failed'),
              ({**STAY,'check_out':'2030-01-06'},FULL.replace('2030-01-09','2030-01-06'),'needs_input')):
            result=self.call({**REQUEST,'stay':stay},self.event(text));self.assertEqual(result.status,expected,result.data)
        with self.assertRaises(ValueError):evidenced_request({'stay':{'adults':2}},'两人')
        self.transport.assert_not_called()

    def test_cancellation_and_ir_consistency(self):
        _,task,_=self.turn(f'测试城{NAME}房价',{'targets':REQUEST['targets']})
        cancelled=replace(task,status='cancelled')
        self.assertIsNone(read_task_continuation('1间房',(cancelled,))[0])
        self.assertIsNone(read_task_continuation('谢谢',(task,))[0])
        with self.assertRaises(ValueError):parse_intent_ir({'schema_version':1,'action':'none','requested_domains':['hotel']},'确认价格')

    def test_date_answer_does_not_choose_between_rail_and_hotel(self):
        _,hotel,_=self.turn(f'测试城{NAME}价格',{'targets':REQUEST['targets']})
        rail=replace(hotel,task_type='rail',task_id='rail',missing_slots=('travel_date',),slots={'origin':'甲城','destination':'乙城'})
        self.assertIsNone(read_task_continuation('明天',(hotel,rail))[0])
        selected=read_task_continuation('1间房',(hotel,rail))[0]
        self.assertEqual(selected.task_id,hotel.task_id);self.transport.assert_not_called()

    def test_brand_discovery_is_bounded_and_unknown_group_is_not_replaced(self):
        places=[{**TARGET['h'],'id':'brand'+str(i),'name':brand+'(湖区店)'} for i,brand in enumerate(('全季酒店','汉庭酒店'))]
        self.map.search_places.return_value=places
        request={'discovery':{'city':'测试城','preferred_groups':['华住会']},'stay':STAY}
        result=self.call(request,self.event(FULL+'，测试城华住会旗下酒店'))
        self.assertEqual(result.status,'completed',result.data);self.assertEqual(self.transport.call_count,1)
        self.assertLessEqual(self.map.search_places.call_count,2)
        bad={**request,'discovery':{'city':'测试城','preferred_groups':['未核对集团']}}
        result=self.call(bad,self.event(FULL+'，未核对集团'))
        self.assertEqual(result.status,'needs_input');self.assertEqual(self.transport.call_count,1)

    def test_saved_multiple_lists_need_an_owned_message_reference(self):
        self.call()
        request=deepcopy(REQUEST);request['stay'].update(check_in='2030-01-08',check_out='2030-01-10')
        self.call(request,self.event(FULL.replace('2030-01-07','2030-01-08').replace('2030-01-09','2030-01-10')))
        result=self.call({'mode':'saved'},self.event('查看之前的酒店价格'))
        self.assertEqual(result.status,'needs_input');self.assertIn('多份',result.data)
        self.assertEqual(self.transport.call_count,2)

    def test_multiple_pending_queries_are_not_arbitrarily_merged(self):
        _,task,_=self.turn(f'测试城{NAME}价格',{'targets':REQUEST['targets']})
        other=replace(task,task_id='other',slots={'hotel_request':{'targets':[{'name':'另一酒店','city':'测试城'}]}})
        self.assertIsNone(read_task_continuation('1间房',(task,other))[0])
        compiler=Mock();compiler.try_compile.return_value=IntentIR(1,'read',(IntentOperation('hotel','query',{'stay':{'rooms':1}}),),source_spans=('1间房',))
        router=RequestRouter(self.store,compiler,read_only_service=self.owner)
        event=self.event('1间房')
        ctx=TaskContext(None,event.content,NOW.isoformat(),{},event.content)
        reply=router._read_intent(event,None,compiler.try_compile.return_value,ctx,[task,other],event.content,{})
        self.assertIn('多个酒店',reply);self.transport.assert_not_called()

    def test_general_agent_tool_selection_preserves_needs_input_task(self):
        client=FakeClient([completion(assistant_message(tool_calls=[tool_call('h','query_hotel_prices',json.dumps({'targets':REQUEST['targets']},ensure_ascii=False))]))])
        agent=TravelAgent(Settings('','',frozenset(),'','script','https://example.test','script'),self.router.execute_result,client=client)
        event=self.event(f'确认测试城{NAME}房价');context=AgentToolContext('onebot','scope','owner',event.event_key,event=event)
        result=agent.run(event.content,tool_context=context,decision_override=CONVERSATION_POLICY)
        self.assertEqual(result.status,'needs_input',result.reply);self.assertEqual(result.task_update.task_type,'hotel')
        self.assertEqual(result.task_update.slots['hotel_request']['targets'],REQUEST['targets'])
        self.transport.assert_not_called()

    def test_hotel_weather_batch_continues_only_unfinished_item(self):
        hotel_event=self.event(f'测试城{NAME}房价和武汉天气')
        claim=self.store.delivery.begin_event(hotel_event.event_key)
        original=self.owner.tool_executor
        def execute(name,arguments,context):
            return WEATHER if name=='get_current_weather' else original(name,arguments,context)
        weather_calls=Mock(side_effect=execute);self.owner.tool_executor=weather_calls
        ir=IntentIR(1,'read',(IntentOperation('hotel','query',{'targets':REQUEST['targets']}),IntentOperation('weather','query',{'location':'武汉'})),source_spans=(hotel_event.content,))
        reply=self.owner.prepare(hotel_event,claim,ir)
        tasks=self.store.tasks.recent('onebot',hotel_event.storage_scope_id,'owner',now=NOW)
        self.assertEqual(tasks[0].task_type,'read_batch');self.assertEqual(tasks[0].status,'collecting');self.assertIn('20℃',reply)
        text='2030-01-07入住，2030-01-09退房，1间房2名成人，无儿童'
        follow=self.event(text);bound=bind_read_context(follow,tasks)
        self.assertEqual(bound.task.task_type,'hotel')
        next_ir=IntentIR(1,'read',(IntentOperation('hotel','query',{'stay':STAY}),),source_spans=(text,))
        claim=self.store.delivery.begin_event(follow.event_key)
        reply=self.owner.prepare(follow,claim,next_ir,bound)
        self.assertIn('20℃',reply);self.assertIn('399',reply)
        self.assertEqual([c.args[0] for c in weather_calls.call_args_list].count('get_current_weather'),1)

    def test_different_stays_continue_in_next_event_without_repeating_first_batch(self):
        request=deepcopy(REQUEST);request['stay'].update(check_in='2030-01-08',check_out='2030-01-10')
        text=FULL+'；另一组2030-01-08入住、2030-01-10退房，其余相同'
        event=self.event(text);claim=self.store.delivery.begin_event(event.event_key)
        ir=IntentIR(1,'read',(IntentOperation('hotel','query',REQUEST),IntentOperation('hotel','query',request)),source_spans=(text,))
        reply=self.owner.prepare(event,claim,ir)
        tasks=self.store.tasks.recent('onebot',event.storage_scope_id,'owner')
        self.assertEqual(tasks[0].status,'collecting');self.assertEqual(self.transport.call_count,1)
        follow=self.event('继续下一批');context=bind_read_context(follow,tasks)
        ir=IntentIR(1,'read',(IntentOperation('hotel','query',{'reference':{'selection':'下一批'}}),),source_spans=('下一批',))
        claim=self.store.delivery.begin_event(follow.event_key);reply=self.owner.prepare(follow,claim,ir,context)
        self.assertEqual(self.transport.call_count,2,reply)
        task=self.store.tasks.recent('onebot',follow.storage_scope_id,'owner')[0]
        self.assertEqual(task.status,'completed');self.assertEqual(len(task.slots['receipts']),2)

    def test_reentrant_submission_does_not_overwrite_the_first_extractor(self):
        event=self.event();observed=[];raw,_=fixture()
        def transport(*args,**kwargs):
            observed.append(self.call({**REQUEST,'mode':'refresh'},event))
            return raw.encode(),ENDPOINT,'text/markdown'
        self.transport.side_effect=transport
        result=self.call({**REQUEST,'mode':'refresh'},event)
        self.assertEqual(len(result.payload['offers']),1,result.data)
        self.assertEqual(observed[0].status,'failed');self.assertFalse(observed[0].retryable)
        self.assertEqual(self.transport.call_count,1);self.assertEqual(len(self.models.completions.requests),1)

    def test_unknown_transport_and_failed_extraction_preserve_evidence_without_retry(self):
        self.transport.side_effect=TimeoutError('synthetic timeout')
        event=self.event();failed=self.call(event=event)
        self.assertEqual(failed.status,'failed');self.assertFalse(failed.retryable)
        self.call(event=event);self.assertEqual(self.transport.call_count,1)
        self.transport.side_effect=None;self.models.completions.responses=[model_response({'offers':'invalid'})]
        result=self.call(event=self.event())
        self.assertTrue(result.payload['snapshot']['raw_text']);self.assertFalse(result.payload['offers'])

    def test_next_batch_and_brand_discovery_are_bounded(self):
        # Distinct real identities are needed; duplicate targets must not create additional submissions.
        names=[{**TARGET['h'],'id':str(i),'name':f'云泽酒店({i}号店)'} for i in range(4)]
        self.map.search_places.side_effect=lambda city,name: [p for p in names if p['name']==name]
        request={**REQUEST,'targets':[{'name':p['name'],'city':'测试城'} for p in names]}
        text='测试城'+','.join(p['name'] for p in names)+FULL
        result=self.call(request,self.event(text))
        self.assertEqual(self.transport.call_count,1);self.assertEqual(self.map.search_places.call_count,3)
        self.assertEqual(result.payload['unfinished'],[names[3]['name']]);self.assertIn('尚未查询',result.data)
        slots={'hotel_request':result.payload['request'],'hotel_remaining_targets':result.payload['remaining_targets']}
        next_result=self.call({'reference':{'selection':'继续下一批'}},self.event('继续下一批'),slots)
        self.assertEqual(self.transport.call_count,2);self.assertEqual(list(next_result.payload['resolved_targets']),['3'])
        self.assertEqual(next_result.payload['remaining_targets'],[])

    def test_proposed_trip_dates_are_only_adopted_after_bound_confirmation(self):
        slots={'hotel_request':{'targets':REQUEST['targets']},'hotel_date_proposal':{k:STAY[k] for k in ('check_in','check_out')}}
        first=self.call({},self.event('确认一下房价'),slots)
        self.assertEqual(first.status,'needs_input');self.assertIn('是否按',first.data)
        self.assertNotIn('check_in',first.payload['request']['stay'])
        second=self.call({'stay':{'rooms':1,'adults':2,'children_ages':[]}},self.event('是，1间，两位成人，不带儿童'),slots)
        self.assertEqual(second.status,'completed',second.data);self.assertEqual(self.transport.call_count,1)

    def test_thanks_cannot_use_a_complete_pending_request_to_query(self):
        result=self.call({},self.event('谢谢'),{'hotel_request':REQUEST})
        self.assertEqual(result.error_code,'operation_denied');self.transport.assert_not_called()

    def test_expired_task_retains_conditions_and_requires_continuation(self):
        reply,task,_=self.turn(f'确认测试城{NAME}房价',{'targets':REQUEST['targets']})
        with self.store._connect() as db:db.execute("UPDATE travel_tasks SET expires_at='2000-01-01T00:00:00+00:00'")
        compiler=Mock();compiler.try_compile.return_value=IntentIR(1,'read',(IntentOperation('hotel','query',{'stay':{'rooms':1}}),),source_spans=('1间房',))
        router=RequestRouter(self.store,compiler,read_only_service=self.owner)
        event=self.event('1间房');claim=self.store.delivery.begin_event(event.event_key)
        reply,_=router.route(event,claim)
        self.assertIn('已过期',reply);self.transport.assert_not_called()
        pending=self.store.tasks.recent(event.platform,event.storage_scope_id,event.sender_id)
        self.assertEqual(pending[0].slots['hotel_request']['targets'],REQUEST['targets'])
        self.assertEqual(pending[0].slots['hotel_request']['stay']['rooms'],1)

    def test_failed_request_can_be_corrected_without_losing_known_fields(self):
        values=deepcopy(REQUEST);values['stay']['rooms']=2
        _,failed,_=self.turn(FULL.replace('1间房','2间房'),values)
        self.assertEqual(failed.status,'failed');self.transport.assert_not_called()
        compiler=Mock();compiler.try_compile.return_value=IntentIR(1,'read',(IntentOperation('hotel','query',{'stay':{'rooms':1}}),),source_spans=('1间房',))
        router=RequestRouter(self.store,compiler,read_only_service=self.owner)
        event=self.event('改为1间房');claim=self.store.delivery.begin_event(event.event_key)
        reply,_=router.route(event,claim)
        self.assertIn('399',reply);self.assertIn('上一次',reply);self.assertEqual(self.transport.call_count,1)
        pending=self.store.tasks.recent(event.platform,event.storage_scope_id,event.sender_id)
        self.assertEqual(pending[0].task_id,failed.task_id);self.assertEqual(pending[0].status,'completed')

    def test_latest_public_prose_replay_preserves_approximate_price_and_admin_suffix(self):
        from tools.hotel_prices.quotes import validate_quotes,evaluate,same_address
        folder=Path(__file__).parent/'fixtures'
        data=json.loads((folder/'wendao_hotels_public_2026-11-06.json').read_text(encoding='utf-8'))
        raw=(folder/'wendao_hotels_public_2026-11-06.md').read_text(encoding='utf-8')
        offers,rejected=validate_quotes({'offers':data['offers']},raw,data['targets'],data['stay'],'南京')
        self.assertEqual(rejected,[]);self.assertEqual(len(offers),2)
        self.assertEqual({o['amount_qualifier'] for o in offers},{'approximate'})
        result=evaluate({'destination':'南京','hotel_stay':data['stay'],'budgets':{'accommodation':{'value':'1000以内','unit_text':'人民币住宿总价'}}},
            {'stay':data['stay'],'offers':offers,'retrieved_at':data['received_at']},datetime.fromisoformat(data['received_at']))
        self.assertEqual({o['numeric_comparison'] for o in result},{'not_comparable'})
        target=next(iter(data['targets'].values()))
        self.assertFalse(same_address('中山东路139号，其他市玄武区',target,'南京'))

    def test_explicit_reference_adoption_only_builds_preview_and_shares_quote(self):
        from tools.trip.tool import TripTool
        from tools.trip.planner import TripPlanner
        from test_hotel_quotes import trip
        first=self.event();self.call(event=first);current=trip();before=deepcopy(current)
        event=self.event('就用第一家',message_reference={'event_id':first.event_key})
        tool=TripTool(self.store,TripPlanner(None,'',None),hotel_quotes=self.hotel)
        ir=IntentIR(1,'update',(IntentOperation('trip','adopt_hotel_quote',{'reference':{'selection':'第一家','ordinal':1}}),),requires_confirmation=True,source_spans=('第一家',))
        with patch.object(tool,'_select_trip',return_value=current),patch.object(tool,'draft',return_value=None),patch.object(tool.planner,'reconcile_candidate',side_effect=lambda c,p:c),patch.object(tool,'_preview_candidate',return_value='住宿预览') as preview:
            reply=tool.adopt_hotel_quote(event,None,ir)
        self.assertEqual(reply,'住宿预览');self.assertEqual(current,before)
        candidate=preview.call_args.args[2]
        self.assertEqual(candidate['plan']['hotel_ids'],['h']);self.assertEqual(candidate['spec']['hotel_preferences']['selected_poi_id'],'h')
        self.assertEqual(self.transport.call_count,1);self.assertEqual(candidate['plan']['hotel_quotes']['request_id'],self.hotel.saved(first,{})['request_id'])


class HotelWebQueueTests(unittest.TestCase):
    def test_independent_quote_visible_and_owned_original_reuses_the_message(self):
        from test_web_app import WebTests
        web=WebTests();web.setUp();self.addCleanup(web.doCleanups)
        raw,offer=fixture();transport=Mock(return_value=(raw.encode(),ENDPOINT,'text/markdown'))
        amap=Mock();amap.search_places.return_value=list(TARGET.values())
        hotel=HotelQuotes(web.store,WendaoClient('synthetic',True,transport=transport),FakeClient([model_response({'offers':[offer]})]),'test',amap=amap)
        web.runtime.travel_service.hotel_prices=hotel
        compiler=Mock();router=RequestRouter(web.store,compiler,read_only_service=web.runtime.travel_service)
        web.runtime.application.semantic_task_service=router
        compiler.try_compile.return_value=IntentIR(1,'read',(IntentOperation('hotel','query',REQUEST),),source_spans=(FULL,))
        url=f'/api/conversations/{web.identity}/messages'
        response=web.client.post(url,headers=web.headers,json={'content':FULL,'client_request_id':'hotel-independent'})
        self.assertEqual(response.status_code,202,response.text);self.assertTrue(web.run_job())
        messages=web.messages();self.assertIn('399',messages[-1]['content']);quoted=messages[-1]['id']
        compiler.try_compile.return_value=IntentIR(1,'read',(IntentOperation('hotel','query',{'mode':'saved'}),),source_spans=('查看酒店报价原文',))
        response=web.client.post(url,headers=web.headers,json={'content':'查看酒店报价原文','client_request_id':'hotel-original','quoted_message_id':quoted})
        self.assertEqual(response.status_code,202,response.text);self.assertTrue(web.run_job())
        self.assertIn('已保存的问道原文',web.messages()[-1]['content']);self.assertEqual(transport.call_count,1)
        self.assertFalse(web.store.trips.list_for_owner(web.event('')))

if __name__=='__main__':unittest.main()
