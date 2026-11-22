"""Budget admission and bounded intent recovery, independent failure baselines."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch
import unittest
import json
import sqlite3
from datetime import datetime,timezone
from pathlib import Path
import tempfile

from agents.intent_compiler import IntentCompiler, IntentCompilationError
from tools.trip.planner import rank_hotels, assess_hotels
from test_hotel_priority import hotel, spec
from test_trips import model_response
from core.chat_transport import ChatEvent
from core.tasks import TaskUpdate
from core.settings import Settings
from agents.travel_agent import TravelAgent
from tools.trip.tool import TripTool
from tools.trip.planner import TripPlanner
from infrastructure.memory_store import MemoryStore
from infrastructure.schema import migrate_wendao_steps
import test_universal_hotel_prices as hotel_fixtures
import test_hotel_trip_reference as trip_fixtures
from test_hotel_quotes import trip,fixture,STAY,TARGET
from test_travel_agent import FakeClient,completion,assistant_message,tool_call
import test_hotel_quotes as quote_fixtures


class AdmissionTests(unittest.TestCase):
    def test_all_unknown_candidates_do_not_complete_a_budget_recommendation(self):
        places={str(i):hotel('候选酒店'+str(i),stars=5) for i in range(3)}
        plan={'days':[]}
        self.assertEqual(len(assess_hotels(spec(),plan,places)),3)
        self.assertEqual(rank_hotels(spec(),plan,places),[])

    def test_exact_bounds_and_evidence_requirements(self):
        s=quote_fixtures.spec('300-400')
        for price,accepted in ((299,False),(300,True),(400,True),(401,False)):
            quote=quote_fixtures.snapshot(price,price);quote['retrieved_at']=datetime.now(timezone.utc).isoformat()
            self.assertEqual(bool(rank_hotels(s,{'days':[],'hotel_quotes':quote},TARGET)),accepted)
        for change in ({'taxes_and_fees_status':'unknown'},{'price_basis':'from'},{'currency':'USD'},{'amount_qualifier':'approximate'}):
            quote=quote_fixtures.snapshot();quote['retrieved_at']=datetime.now(timezone.utc).isoformat();quote['offers'][0].update(change)
            self.assertEqual(rank_hotels(s,{'days':[],'hotel_quotes':quote},TARGET),[])
        quote=quote_fixtures.snapshot(399,399);quote['retrieved_at']=datetime.now(timezone.utc).isoformat()
        different=deepcopy(s);different['hotel_stay']['adults']=3
        self.assertEqual(rank_hotels(different,{'days':[],'hotel_quotes':quote},TARGET),[])

    def test_goal_contract_does_not_turn_read_only_or_reference_into_authorization(self):
        from core.intent_ir import parse_intent_ir
        text='只查这几家，不替换'
        value={'schema_version':1,'action':'update','operations':[{'domain':'trip','operation':'prepare_accommodation',
            'request_text':text,'allow_preview':True}],'source_spans':[text]}
        with self.assertRaises(ValueError):parse_intent_ir(value,text)
        text='查看候选云泽酒店(湖畔店)'
        value['operations'][0].update(request_text=text,selection={'names':[TARGET['h']['name']],'source':text});value['source_spans']=[text]
        with self.assertRaises(ValueError):parse_intent_ir(value,text)


class IntentRecoveryTests(unittest.TestCase):
    def test_length_recovers_once_before_validation(self):
        truncated=SimpleNamespace(choices=[SimpleNamespace(finish_reason='length',message=SimpleNamespace(content='{}'))])
        valid=model_response({'schema_version':1,'action':'none','operations':[]})
        create=Mock(side_effect=[truncated,valid]);client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with patch('agents.intent_compiler.parse_intent_ir',wraps=__import__('core.intent_ir',fromlist=['parse_intent_ir']).parse_intent_ir) as parse:
            IntentCompiler(client,'test').try_compile('查询候选，不合预算就替换')
        self.assertEqual(create.call_count,2)
        self.assertEqual(parse.call_count,1)

    def test_filter_is_distinct_and_never_retried(self):
        response=SimpleNamespace(choices=[SimpleNamespace(finish_reason='content_filter',message=SimpleNamespace(content='{}'))])
        create=Mock(return_value=response);client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with self.assertRaises(IntentCompilationError) as caught:IntentCompiler(client,'test').try_compile('查询酒店')
        self.assertEqual(caught.exception.code,'content_filter')
        self.assertEqual(create.call_count,1)

    def test_second_length_and_contract_repair_share_one_recovery(self):
        for first in ('length','stop'):
            responses=[SimpleNamespace(choices=[SimpleNamespace(finish_reason=first,message=SimpleNamespace(content='{}'))]),
                SimpleNamespace(choices=[SimpleNamespace(finish_reason='length',message=SimpleNamespace(content='{}'))])]
            create=Mock(side_effect=responses);client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
            with self.assertRaises(IntentCompilationError) as caught:IntentCompiler(client,'test').try_compile('核对酒店')
            self.assertEqual(caught.exception.code,'output_truncated');self.assertEqual(create.call_count,2)

    def test_legacy_fallback_and_truncation_never_multiply_recovery(self):
        for reasons,expected in ((['length','bad_json'],'invalid_json'),(['bad_json','length'],'output_truncated')):
            responses=[SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop' if r=='bad_json' else r,
                message=SimpleNamespace(content='not JSON' if r=='bad_json' else '{}'))]) for r in reasons]
            create=Mock(side_effect=responses);client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
            with patch('agents.intent_compiler.model_for_purpose',return_value='other'),self.assertRaises(IntentCompilationError) as caught:
                IntentCompiler(client,'default').try_compile('查询住宿候选')
            self.assertEqual(caught.exception.code,expected);self.assertEqual(create.call_count,2)



class StepStorageTests(unittest.TestCase):
    def test_transactional_migration_keeps_legacy_payloads_and_indexes(self):
        with tempfile.TemporaryDirectory() as d:
            db=sqlite3.connect(Path(d)/'old.db');db.row_factory=sqlite3.Row
            db.execute('CREATE TABLE schema_migrations(name TEXT PRIMARY KEY,applied_at TEXT)')
            db.execute('CREATE TABLE wendao_requests(event_key TEXT,purpose TEXT,platform TEXT,scope TEXT,owner TEXT,result_json TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(event_key,purpose))')
            db.execute('CREATE INDEX legacy_owner_index ON wendao_requests(owner)')
            old=('event','hotel_quotes','web','scope','owner','{ "opaque": "unchanged" }','2026-11-11')
            db.execute('INSERT INTO wendao_requests VALUES(?,?,?,?,?,?,?)',old);db.commit()
            migrate_wendao_steps(db);migrate_wendao_steps(db)
            row=tuple(db.execute('SELECT * FROM wendao_requests').fetchone())
            self.assertEqual(row,(*old,'',''))
            self.assertTrue(db.execute("SELECT 1 FROM sqlite_master WHERE name='legacy_owner_index'").fetchone())
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok');db.close()

    def test_migration_failure_rolls_back_original_table(self):
        db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
        db.execute('CREATE TABLE schema_migrations(name TEXT PRIMARY KEY,applied_at TEXT)')
        db.execute('CREATE TABLE wendao_requests(event_key,purpose,platform,scope,owner,result_json,created_at,PRIMARY KEY(event_key,purpose))')
        db.execute("INSERT INTO wendao_requests VALUES('event','hotel_quotes','web','scope','owner','raw','date')");db.commit()
        class FailRename:
            def execute(self,sql,*args):
                if sql.startswith('ALTER TABLE'):raise RuntimeError('injected migration failure')
                return db.execute(sql,*args)
            def commit(self):db.commit()
            def rollback(self):db.rollback()
        with self.assertRaises(RuntimeError):migrate_wendao_steps(FailRename())
        self.assertEqual(db.execute('SELECT result_json FROM wendao_requests').fetchone()[0],'raw')
        self.assertNotIn('step_key',[r['name'] for r in db.execute('PRAGMA table_info(wendao_requests)')]);db.close()


class OrchestrationTests(unittest.TestCase):
    setUp=hotel_fixtures.UniversalHotelTests.setUp
    event=hotel_fixtures.UniversalHotelTests.event
    seed=trip_fixtures.HotelTripReferenceTests.seed

    def prepare(self,responses,*,reference=None,text='先核对这些候选房价，不满足预算就重新选择',values=None,task=None):
        self.agent_models=FakeClient(responses)
        self.tool=TripTool(self.store,TripPlanner(None,'',self.map),hotel_quotes=self.hotel)
        self.tool._reminder_changes=Mock(return_value=([],[],[],{},None))
        self.router.trip_tool=self.tool
        self.tool.travel_agent=TravelAgent(Settings('app','secret',frozenset(),'map','key','https://invalid','test'),self.router.execute_result,client=self.agent_models)
        event=self.event(text,message_reference=reference)
        reply=self.tool.prepare_accommodation(event,self.store.delivery.begin_event(event.event_key),
            values or {'request_text':text,'allow_preview':True,'replace_only_if_budget_mismatch':True},task=task)
        return reply,event

    def query(self,args=None):return completion(assistant_message(tool_calls=[tool_call('query','query_hotel_prices',json.dumps(args or {},ensure_ascii=False))]))
    def preview(self,ids):return completion(assistant_message(tool_calls=[tool_call('preview','preview_accommodation_candidates',json.dumps({'candidate_ids':ids}))]))

    def test_matching_original_is_retained_without_discovery_or_preview(self):
        reference,_=self.seed(complete=True)
        reply,event=self.prepare([self.query(),completion(assistant_message('原候选符合预算'))],reference=reference)
        self.assertIn('保留原住宿',reply);self.assertEqual(self.transport.call_count,1);self.map.search_places.assert_not_called()
        self.assertFalse(self.store.trips.list_for_owner(event))
        self.assertEqual(len(self.store.research.hotel_quotes_for_event(event,event.event_key)),1)

    def test_unpriced_original_searches_new_objects_and_generates_only_preview(self):
        reference,original=self.seed(complete=True)
        raw,offer=fixture();new_raw=raw.replace(TARGET['h']['name'],'替代酒店').replace('测试路8号','测试路9号')
        new_offer={**offer,'target_id':'new','hotel_name':'替代酒店','address':'测试路9号','hotel_text':new_raw}
        self.transport.side_effect=[('原酒店目标日期房价未知'.encode(),'https://example.invalid','text/markdown'),(new_raw.encode(),'https://example.invalid','text/markdown')]
        self.models.completions.responses=[model_response({'offers':[]}),model_response({'offers':[new_offer]})]
        new_target={**TARGET['h'],'id':'new','name':'替代酒店','address':'测试路9号'}
        self.map.search_places.return_value=[TARGET['h'],new_target]
        reply,event=self.prepare([self.query(),self.query({'discovery':{'city':'测试城','area':'中心'}}),self.preview(['new'])],reference=reference)
        self.assertIn('核对后可保存',reply);self.assertEqual(self.transport.call_count,2)
        self.assertEqual(self.models.completions.requests.__len__(),2)
        self.assertFalse(self.store.trips.list_for_owner(event))
        latest=self.store.trips.current_drafts(event)[0]
        self.assertEqual(latest.slots['candidate_trip']['plan']['hotel_ids'],['new'])
        for key in ('days','intercity','dining_options','restaurant_ids'):
            self.assertEqual(latest.slots['candidate_trip']['plan'].get(key),reference['trip']['plan'].get(key))
        quotes=self.store.research.hotel_quotes_for_event(event,event.event_key)
        self.assertEqual(len(quotes),2);self.assertNotEqual(quotes[0]['source_event_id'],quotes[1]['source_event_id'])
        self.assertEqual(len({q['step_key'] for q in quotes}),2)
        for quote in quotes:
            self.assertEqual(self.hotel.original(event,quote['source_event_id'])['text'],quote['raw_text'])
        self.assertIsNone(self.store.research.hotel_quote_for_event(event,event.event_key),'ambiguous event must not pick first step')

    def test_missing_stay_preserves_full_goal_and_only_asks_missing_fields(self):
        reference,_=self.seed(complete=False)
        reply,event=self.prepare([self.query()],reference=reference)
        task=self.store.tasks.for_event(event,event.event_key)
        self.assertEqual(task.status,'collecting');self.assertIn('不满足预算',task.initial_request)
        self.assertNotIn('adults',task.missing_slots);self.assertNotIn('targets',task.missing_slots)
        self.assertTrue(task.slots['accommodation_goal']['allow_preview']);self.transport.assert_not_called()
        self.assertIn('房间数',reply)
        from app.request_router import bind_request
        continuation=bind_request(self.store,self.event('继续'))
        self.assertEqual(continuation.read_context.task.task_id,task.task_id)
        self.assertEqual(continuation.budget_run_key,task.slots['budget_run_key'])
        blocked={**task.slots,'model_budget_block':{'code':'run_call_limit','details':{}}}
        with self.store._connect() as db:db.execute('UPDATE travel_tasks SET slots_json=? WHERE task_id=?',(json.dumps(blocked),task.task_id))
        restart=bind_request(self.store,self.event('开始新一轮酒店核价'))
        self.assertEqual(restart.read_context.task.task_id,task.task_id)
        self.assertNotEqual(restart.budget_run_key,task.slots['budget_run_key'])
        self.assertEqual(restart.read_context.task.slots['budget_run_parent']['run_key'],task.slots['budget_run_key'])

    def test_model_budget_restatement_does_not_replace_bound_budget_evidence(self):
        reference,_=self.seed(complete=False)
        reply,event=self.prepare([self.query({'budget':{'value':'300-400','unit_text':'人民币每间每晚','source':'模型复述草案预算'}})],reference=reference)
        task=self.store.tasks.for_event(event,event.event_key)
        self.assertNotIn('budget.source',reply);self.assertIn('房间数',reply)
        self.assertEqual(task.slots['hotel_request']['budget']['source'],'300-400')
        self.assertNotIn('模型复述',str(task.slots));self.transport.assert_not_called()

    def test_followup_merges_stay_then_resumes_whole_goal(self):
        reference,_=self.seed(complete=False)
        _,first=self.prepare([self.query()],reference=reference)
        task=self.store.tasks.for_event(first,first.event_key)
        reply,event=self.prepare([self.query(),completion(assistant_message('原酒店预算内'))],text='是，一间房，无儿童',
            values={'hotel_query':{'proposal_response':{'kind':'stay_dates','decision':'accept','source':'是'},'stay':{'rooms':1,'children_ages':[]}}},task=task)
        updated=self.store.tasks.for_event(event,event.event_key)
        self.assertEqual(updated.task_id,task.task_id);self.assertEqual(updated.slots['hotel_request']['stay'],STAY)
        self.assertEqual(updated.slots['hotel_request']['budget']['value'],'300-400');self.assertIn('保留原住宿',reply)
        self.assertEqual(self.transport.call_count,1)

    def test_explicit_dates_close_the_proposal_without_reasking_confirmation(self):
        reference,_=self.seed(complete=False)
        _,event=self.prepare([self.query()],reference=reference)
        task=self.store.tasks.for_event(event,event.event_key)
        reply,event=self.prepare([self.query(),completion(assistant_message('保留原候选'))],text='2030年1月7日入住，2030年1月9日退房，一间房，无儿童',
            values={'hotel_query':{'stay':{'check_in':'2030年1月7日','check_out':'2030年1月9日','rooms':1,'children_ages':[]}}},task=task)
        updated=self.store.tasks.for_event(event,event.event_key)
        self.assertEqual(updated.slots['hotel_date_proposal']['status'],'accepted')
        self.assertFalse(any(a['kind'] in {'accept_dates','reject_dates'} for a in self.hotel.actions(event,updated,event.event_key)))

    def test_cannot_preview_an_expired_or_changed_source_after_read_only_query(self):
        reference,original=self.seed(expired=True,complete=True)
        # A verified original is readable; a forced replacement preview cannot revive its old confirmation.
        reply,event=self.prepare([self.query(),self.preview(['h']),completion(assistant_message('结束'))],reference=reference)
        self.assertIn('保留原住宿',reply);self.assertEqual(self.store.tasks.owned(event,original.task_id).version,original.version)

    def test_cancelled_goal_or_foreign_reference_never_queries(self):
        reference,_=self.seed(complete=False)
        _,event=self.prepare([self.query()],reference=reference)
        task=self.store.tasks.for_event(event,event.event_key)
        with self.store._connect() as db:db.execute("UPDATE travel_tasks SET status='cancelled' WHERE task_id=?",(task.task_id,))
        reply,_=self.prepare([],values={'hotel_query':{'stay':{'rooms':1}}},task=task)
        self.assertIn('取消',reply);self.transport.assert_not_called()
        foreign=self.event('调整这些酒店',message_reference=reference,sender_id='foreign')
        reply=self.tool.prepare_accommodation(foreign,self.store.delivery.begin_event(foreign.event_key),{'request_text':foreign.content,'allow_preview':True})
        self.assertIn('不属于',reply);self.transport.assert_not_called()

    def test_pinned_hotel_with_unknown_price_is_not_replaced(self):
        reference,_=self.seed(complete=True)
        self.models.completions.responses=[model_response({'offers':[]})]
        text='指定'+TARGET['h']['name']+'，请核对预算并保留该门店'
        reply,event=self.prepare([self.query()],reference=reference,text=text,values={
            'request_text':text,'allow_preview':True,'selection':{'names':[TARGET['h']['name']],'source':text}})
        self.assertIn('尚未完成',reply);self.assertEqual(self.transport.call_count,1);self.map.search_places.assert_not_called()
        task=self.store.tasks.for_event(event,event.event_key)
        self.assertEqual(task.slots['accommodation_goal']['selection']['names'],[TARGET['h']['name']])

    def test_global_transport_unknown_stops_without_searching_other_hotels(self):
        reference,_=self.seed(complete=True)
        self.transport.side_effect=TimeoutError()
        reply,event=self.prepare([self.query()],reference=reference)
        self.assertIn('尚未完成',reply);self.map.search_places.assert_not_called();self.assertEqual(self.transport.call_count,1)

    def test_required_brand_survives_implicit_and_model_broadened_discovery(self):
        candidate=trip();candidate['spec']['hotel_preferences']={'preferred_brands':['汉庭'],'brand_mode':'required'}
        reference,_=self.seed(candidate=candidate,complete=True)
        brand={**TARGET['h'],'id':'brand','name':'汉庭酒店(测试店)'}
        self.map.search_places.return_value=[TARGET['h'],brand]
        self.models.completions.responses=[model_response({'offers':[]})]
        reply,event=self.prepare([self.query({'discovery':{'city':'测试城','preferred_brands':['如家']}}),
            *[completion(assistant_message('本轮资料不足')) for _ in range(3)]],reference=reference)
        query=self.transport.call_args.kwargs['payload']['query']
        self.assertIn(brand['name'],query);self.assertNotIn(TARGET['h']['name'],query)
        self.assertTrue(all('汉庭' in c.args[1] for c in self.map.search_places.call_args_list))
        self.assertEqual(self.transport.call_count,1)

    def test_same_logical_step_is_deduplicated_despite_target_order(self):
        event=self.event();self.store.delivery.begin_event(event.event_key)
        targets={**TARGET,'second':{**TARGET['h'],'id':'second','name':'第二酒店'}}
        kwargs={'targets':targets,'policy':{'requested':True},'step_binding':{'trip_id':'bound','version':1},'run_key':'run'}
        request={'city':'测试城','stay':STAY}
        one=self.hotel.lookup(event,request,**kwargs)
        kwargs['targets']=dict(reversed(list(targets.items())))
        two=self.hotel.lookup(event,request,refresh=True,**kwargs)
        self.assertEqual(self.transport.call_count,1);self.assertEqual(len(self.models.completions.requests),1)
        self.assertEqual(one['source_event_id'],two['source_event_id'])

    def test_run_and_parent_event_intersect_and_unknown_does_not_release_reservation(self):
        event=self.event();repo=self.store.research
        repo.wendao_call(event,purpose='research',reserve={'status':'submitted'})
        repo.wendao_call(event,purpose='hotel_quotes',step_key='a',run_key='run',query_hash='a',fingerprint='a',reserve={'status':'submitted'})
        with self.assertRaises(ValueError):repo.wendao_call(event,purpose='hotel_quotes',step_key='b',run_key='run',reserve={})
        newer=self.event()
        repo.wendao_call(newer,purpose='hotel_quotes',step_key='b',run_key='run',reserve={'status':'submitted'})
        with self.assertRaisesRegex(ValueError,'两个'):repo.wendao_call(self.event(),purpose='hotel_quotes',step_key='c',run_key='run',reserve={})

    def test_explicit_budget_goal_keeps_city_available_while_lodging_collects(self):
        self.prepare([],reference=None)  # Configure the same real agent/tool registry, without a bound trip.
        self.agent_models.completions.responses=[self.query()]
        candidate=trip();candidate['spec'].pop('hotel_stay');candidate['spec']['preferences']=['两位成年人']
        candidate['spec'].update(start_date=STAY['check_in'],end_date=STAY['check_out'])
        event=self.event('规划旅行，住宿300-400元每间每晚')
        from tools.trip.accommodation import prepare
        reply=prepare(self.tool,event,self.store.delivery.begin_event(event.event_key),{'request_text':event.content,'allow_preview':True},
            initial=(candidate,None,None,{'trip_id':candidate['trip_id'],'trip_version':0}))
        task=self.store.tasks.for_event(event,event.event_key)
        self.assertEqual(task.task_type,'itinerary');self.assertIn('candidate_trip',task.slots)
        self.assertTrue(task.slots['accommodation_goal']['allow_preview']);self.assertIn('房间数',reply)
        from app.request_router import bind_request
        binding=bind_request(self.store,self.event('一间房，无儿童'))
        self.assertEqual(binding.budget_run_key,task.slots['budget_run_key'])
        self.assertEqual(binding.read_context.task.task_id,task.task_id)
        self.agent_models.completions.responses=[self.query(),self.preview(['h'])]
        follow=self.event('是，一间房，无儿童')
        reply=self.tool.prepare_accommodation(follow,self.store.delivery.begin_event(follow.event_key),
            {'hotel_query':{'proposal_response':{'kind':'stay_dates','decision':'accept','source':'是'},'stay':{'rooms':1,'children_ages':[]}}},task=task)
        new=self.store.tasks.for_event(follow,follow.event_key)
        self.assertIn('核对后可保存',reply)
        self.assertEqual(new.missing_slots,('confirmation',));self.assertEqual(new.slots['candidate_trip']['plan']['hotel_ids'],['h'])
        self.assertNotIn('accommodation_goal',new.slots)

    def test_intent_budget_failure_cannot_change_an_itinerary_goal_into_a_read_task(self):
        self.prepare([])
        self.agent_models.completions.responses=[self.query()]
        candidate=trip();candidate['spec'].pop('hotel_stay');candidate['spec']['preferences']=['两位成年人']
        event=self.event('规划旅行，住宿300-400元每间每晚')
        from tools.trip.accommodation import prepare
        prepare(self.tool,event,self.store.delivery.begin_event(event.event_key),{'request_text':event.content,'allow_preview':True},
            initial=(candidate,None,None,{'trip_id':candidate['trip_id'],'trip_version':0}))
        original=self.store.tasks.for_event(event,event.event_key)
        from app.request_router import RequestRouter,CURRENT_REQUEST,bind_request
        next_event=self.event('一间房，无儿童')
        compiler=Mock(try_compile=Mock(side_effect=IntentCompilationError('budget_exceeded','本轮模型次数已用完',budget_code='run_call_limit')))
        router=RequestRouter(self.store,compiler,trip_service=self.tool,read_only_service=self.owner)
        binding=bind_request(self.store,next_event);token=CURRENT_REQUEST.set(binding)
        try:router.handle(next_event,self.store.delivery.begin_event(next_event.event_key))
        finally:CURRENT_REQUEST.reset(token)
        updated=self.store.tasks.owned(next_event,original.task_id)
        self.assertEqual(updated.task_type,'itinerary');self.assertIn('candidate_trip',updated.slots)
        self.assertEqual(updated.slots['model_budget_block']['code'],'run_call_limit')

    def test_unproven_preview_cannot_be_made_success_by_model_prose(self):
        reference,_=self.seed(complete=True)
        reply,event=self.prepare([self.preview(['h']),completion(assistant_message('已成功选择合格酒店')),
            completion(assistant_message('已经完成')),completion(assistant_message('完成'))],reference=reference)
        self.assertIn('尚未完成',reply);self.assertNotIn('已成功选择',reply);self.transport.assert_not_called()
        self.assertEqual(self.store.trips.current_drafts(event)[0].version,1)

    def test_new_price_receipt_allows_rejected_preview_to_recover_and_then_stops(self):
        self.prepare([])
        self.agent_models.completions.responses=[self.preview(['h']),self.query(),self.preview(['h'])]
        candidate=trip();candidate['spec'].update(start_date=STAY['check_in'],end_date=STAY['check_out'])
        event=self.event('规划住宿，沿用完整预算与住宿条件')
        reply=self.tool._preview_candidate(event,self.store.delivery.begin_event(event.event_key),candidate)
        self.assertIn('核对后可保存',reply)
        self.assertEqual(len(self.agent_models.completions.requests),3,'预览成功后不再调用模型或执行后续工具')
        self.assertEqual(self.transport.call_count,1)
        self.assertFalse(self.store.trips.list_for_owner(event))
