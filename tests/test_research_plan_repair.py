"""Regression of the 21st-arrival / 22nd-sightseeing research incident."""
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from agents.intent_compiler import IntentCompiler
from agents.research_search_planner import ResearchSearchPlanner
from core.chat_transport import ChatEvent
from core.intent_ir import parse_intent_ir, to_jsonable
from core.research_contract import project_request, ResearchRequestError
from infrastructure.execution_diagnostics import metadata
from infrastructure.execution_report import explain
from infrastructure.memory_store import MemoryStore
from services.research_context import research_dates, resolve_plan
from services.research_service import ResearchService
from services.semantic_task_service import SemanticTaskService
from test_travel_agent import FakeClient
from test_trips import model_response


TEXT = '我希望你再查一下这个方案里涉及的所有景点的具体票价、开放时间、预约规则，我计划10月21日去北京，不过得22号才能开始旅游。'
PLAN = '''北京三日参考方案
第一天：天安门广场、故宫、景山公园；时间充裕可去北海公园。
第二天：八达岭长城一日游。不建议同一天再塞其他大型景点。
第三天：颐和园、圆明园；国家博物馆可选。晚上外观鸟巢、水立方。'''
INTRO = '''我是彼岸旅行助手，可以协助你规划和推进旅行。
行程规划、方案比较、目的地攻略和旅行待办都是我的能力。
例如：
> “北京三日方案：第1天故宫，第2天长城，第3天颐和园。”'''


def ir_value(**extras):
    return {'schema_version':1,'action':'read','operations':[{
        'domain':'research','operation':'search_travel_guides','query_text':TEXT,
        'plan_reference':'这个方案','topics':['ticket_price','opening_hours','reservation_rules'],
        'arrival_date_text':'10月21日','visit_start_date_text':'22号',**extras}],
        'missing_fields':[],'requires_confirmation':False,'source_spans':['这个方案','10月21日','22号']}


def target(name, day=1, optional=False, mode='entry'):
    return {'name':name,'source_text':name,'day_index':day,'optional':optional,'visit_mode':mode}


def scope_value(targets=None):
    return {'targets':targets or [target('故宫')],'topics':['ticket_price','opening_hours','reservation_rules'],
            'arrival_date_text':'10月21日','visit_start_date_text':'22号',
            'language':'zh-cn','requested_domains':[],'excluded_domains':[],'strict_sources':False}


class ResearchPlanRepairTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = MemoryStore(Path(folder.name)/'test.db')
        self.event = ChatEvent('web','group','request','conversation','owner',TEXT,
                               occurred_at=datetime(2026,10,19,2,tzinfo=timezone.utc))
        self.plan_event = replace(self.event,event_id='plan',content='帮我规划北京三天旅行')
        self.artifact = self.store.planning.capture(self.plan_event,PLAN)
        self.request = {**project_request(ir_value()['operations'][0] | {},TEXT)[0],
                        'original_request':TEXT,'plan':self.artifact}
        # The operation envelope is never forwarded as executable parameters.
        self.request.pop('domain',None)
        self.request.pop('operation',None)

    def save_turn(self, event, content):
        self.store.save_turn(event.storage_scope_id,event.sender_id,event.event_key,event.content,content)

    def test_inert_extensions_remain_raw_but_never_execute_or_repair(self):
        value = ir_value(reason='explanation',notes={'arbitrary':'untrusted'},confidence=.9)
        client = FakeClient([model_response(value)])
        ir = IntentCompiler(client,'test').compile(TEXT)
        self.assertEqual(len(client.completions.requests),1)
        self.assertIn('notes',to_jsonable(ir)['operations'][0])
        projection, ignored = project_request(ir.operations[0].values,TEXT)
        self.assertEqual(ignored,['confidence','notes','reason'])
        self.assertNotIn('notes',projection)
        self.assertEqual(projection['visit_start_date_text'],'22号')

    def test_invalid_core_field_gets_one_targeted_repair(self):
        client = FakeClient([model_response(ir_value(topics='ticket_price')),model_response(ir_value())])
        self.assertIsNotNone(IntentCompiler(client,'test').try_compile(TEXT))
        self.assertEqual(len(client.completions.requests),2)
        self.assertIn('invalid_topics',client.completions.requests[1]['messages'][0]['content'])

    def test_failed_repair_never_adds_default_model_third_attempt(self):
        bad = ir_value(arrival_date_text='2030年1月1日')
        client = FakeClient([model_response(bad),model_response(bad)])
        with patch.dict('os.environ',{'LLM_INTENT_MODEL_ID':'small'}):
            self.assertIsNone(IntentCompiler(client,'default').try_compile(TEXT))
        self.assertEqual(len(client.completions.requests),2)

    def test_extra_identity_and_actions_stay_strict(self):
        for extras in ({'owner_id':'invented'},{'notes':{'trip_id':'invented'}},
                       {'artifact_id':'invented'},{'actions':['delete']},{'plan_index':True}):
            with self.subTest(extras=extras), self.assertRaises(ValueError):
                parse_intent_ir(ir_value(**extras),TEXT)
        with self.assertRaises(ResearchRequestError):
            project_request({'query_text':TEXT,'topics':['invented']},TEXT)

    def test_alias_metadata_does_not_override_grounded_core_dates(self):
        projection,ignored = project_request(ir_value(arrival_date='2050-01-01')['operations'][0],TEXT)
        self.assertEqual(projection['arrival_date_text'],'10月21日')
        self.assertIn('arrival_date',ignored)

    def test_known_semantic_aliases_are_grounded_without_mutating_raw_ir(self):
        values = {'query_text':TEXT,'plan_ref':'这个方案','arrival_date':'10月21日','visit_start_date':'22号','notes':'inert'}
        projected, ignored = project_request(values,TEXT)
        self.assertEqual(projected['plan_reference'],'这个方案')
        self.assertEqual(projected['arrival_date_text'],'10月21日')
        self.assertEqual(ignored,['notes'])
        self.assertNotIn('arrival_date_text',values)
        with self.assertRaises(ResearchRequestError):
            project_request({'query_text':TEXT,'arrival_date':'2050-01-01'},TEXT)

    def test_arrival_and_sightseeing_are_separate_and_month_is_inherited(self):
        dates = research_dates(self.request,self.event,[3])
        self.assertEqual(dates['arrival_date'],'2026-10-21')
        self.assertEqual(dates['visit_start_date'],'2026-10-22')
        self.assertEqual(dates['visit_end_date'],'2026-10-24')
        self.assertTrue(dates['year_inferred'])
        dates = research_dates({'arrival_date_text':'10月21日'},self.event,[3])
        self.assertEqual(dates['visit_start_date'],'')

    def test_conflicting_or_invalid_date_requires_clarification(self):
        for arrival,visit in [('10月21日','20号'),('10月32日','22号'),('9月21日','22号')]:
            with self.subTest(arrival=arrival),self.assertRaises(ValueError):
                research_dates({'arrival_date_text':arrival,'visit_start_date_text':visit},self.event,[3])

    def test_introduction_code_quotes_and_fact_report_are_not_plans(self):
        for body in (INTRO,'```\n'+PLAN+'\n```','\n'.join('> '+line for line in PLAN.splitlines()),
                     '“'+PLAN+'”','北京门票研究报告\n'+'故宫票价待核对。'*30):
            with self.subTest(body=body[:20]):
                self.assertIsNone(self.store.planning.capture(replace(self.event,event_id=body[:20]),body,kind='research_backed'))
        self.assertEqual(self.store.planning._destination('介绍一下自己',INTRO),'')

    def test_total_days_excludes_local_day_trip(self):
        self.assertEqual(self.artifact['duration_options'],[3])
        self.assertEqual(self.store.planning._durations('',PLAN),[3])
        alternatives = '开封参考规划\n方案A：2天1晚\n第1天：古城；第2天：开封府。\n方案B：3天2晚\n第1天：古城；第2天：园林；第3天：博物馆。'
        record = self.store.planning.capture(replace(self.event,event_id='alternatives',content='想去开封玩'),alternatives)
        self.assertEqual(record['duration_options'],[2,3])

    def test_conflicting_total_days_or_missing_day_are_not_auto_captured(self):
        self.assertIsNone(self.store.planning.capture(replace(self.event,content='北京两天行程'),PLAN))
        self.assertIsNone(self.store.planning.capture(self.event,PLAN.replace('第二天','第三天')))

    def test_declared_variant_and_injected_duration_must_match_actual_structure(self):
        self.assertIsNone(self.store.planning.capture(replace(self.event,content='北京四天行程'),PLAN,duration_options=[4]))
        self.assertIsNone(self.store.planning.capture(self.event,'北京参考方案\n方案A：3天\n第1天：故宫；第2天：长城。'))

    def test_metadata_revalidation_is_versioned_idempotent_and_keeps_ordinals(self):
        self.save_turn(self.plan_event,PLAN)
        with self.store._connect() as db:
            db.execute("UPDATE planning_artifacts SET duration_options_json='[3,1]',title='北京参考方案（3天／1天）',capture_version=1")
            db.execute('''INSERT INTO planning_artifacts
                SELECT 'P-introduction',platform,scope_id,owner_id,2,kind,'intro-source','和推进参考方案','和推进','[]',
                summary,?,'active',1,created_at,updated_at,1,'' FROM planning_artifacts LIMIT 1''',(INTRO,))
        self.store.planning.ensure_history(self.event)
        current = self.store.planning.get(self.event,self.artifact['artifact_id'])
        invalid = self.store.planning.get(self.event,'P-introduction')
        self.assertEqual(current['duration_options'],[3])
        self.assertEqual(current['version'],2)
        self.assertEqual(current['ordinal'],1)
        self.assertEqual(invalid['status'],'invalid')
        self.assertEqual(invalid['content'],INTRO)
        self.store.planning.ensure_history(self.event)
        self.assertEqual(self.store.planning.get(self.event,current['artifact_id'])['version'],2)
        self.assertEqual(len(self.store.planning.list_for_owner(self.event)),1)

    def test_deleted_and_adopted_artifacts_are_not_revalidated_or_restored(self):
        for status in ('deleted','adopted'):
            with self.store._connect() as db:
                db.execute('UPDATE planning_artifacts SET status=?,capture_version=1 WHERE artifact_id=?',(status,self.artifact['artifact_id']))
            self.store.planning.revalidate(self.event)
            row = self.store.planning.get(self.event,self.artifact['artifact_id'])
            self.assertEqual(row['status'],status)
            self.assertEqual(row['version'],1)

    def test_legacy_revalidation_is_bounded_and_unvalidated_rows_cannot_be_adopted(self):
        with self.store._connect() as db:
            db.execute('UPDATE planning_artifacts SET capture_version=1')
            for i in range(2,53):
                db.execute('''INSERT INTO planning_artifacts
                    SELECT ?,platform,scope_id,owner_id,?,kind,?,title,destination,duration_options_json,
                    summary,content,status,version,created_at,updated_at,capture_version,validation_reason
                    FROM planning_artifacts WHERE artifact_id=?''',(f'P-legacy-{i}',i,f'legacy-{i}',self.artifact['artifact_id']))
        self.store.planning.revalidate(self.event)
        self.assertEqual(len(self.store.planning.list_for_owner(self.event)),50)
        self.store.planning.revalidate(self.event)
        self.assertEqual(len(self.store.planning.list_for_owner(self.event)),52)

    def test_plan_resolver_unique_adjacent_ambiguous_and_scope_isolation(self):
        self.assertEqual(resolve_plan(self.store,self.event,self.request)[1],'unique_plan')
        second = replace(self.plan_event,event_id='second')
        self.store.planning.capture(second,PLAN)
        self.assertEqual(resolve_plan(self.store,self.event,self.request)[1],'ambiguous_plan')
        self.save_turn(second,PLAN)
        resolved,reason = resolve_plan(self.store,self.event,self.request)
        self.assertEqual(reason,'adjacent_plan')
        self.assertEqual(resolved['source_event_id'],second.event_key)
        self.assertIsNone(resolve_plan(self.store,replace(self.event,sender_id='other'),self.request)[0])

    def test_natural_request_dispatch_passes_full_text_plan_and_date_context(self):
        research = Mock()
        research.handle_intent.return_value = 'research result'
        ir = parse_intent_ir(ir_value(reason='ignored'),TEXT)
        service = SemanticTaskService(self.store,Mock(try_compile=Mock(return_value=ir)),research_service=research)
        self.assertEqual(service.handle(self.event,None),'research result')
        forwarded = research.handle_intent.call_args.kwargs['request']
        self.assertEqual(forwarded['original_request'],TEXT)
        self.assertEqual(forwarded['plan']['content'],PLAN)
        self.assertEqual(forwarded['dates']['visit_end_date'],'2026-10-24')
        self.assertNotIn('reason',forwarded)

    def test_ambiguous_plan_clarifies_before_searching(self):
        self.store.planning.capture(replace(self.plan_event,event_id='second'),PLAN)
        research = Mock()
        service = SemanticTaskService(self.store,Mock(),research_service=research)
        reply = service._research(self.event,None,parse_intent_ir(ir_value(),TEXT))
        self.assertIn('哪份方案',reply)
        research.handle_intent.assert_not_called()

    def test_explicit_reference_and_saved_trip_resolution(self):
        self.store.planning.capture(replace(self.plan_event,event_id='second'),PLAN)
        selected, reason = resolve_plan(self.store,self.event,{**self.request,'plan_index':1,'plan_reference':'最开始的方案'})
        self.assertEqual(selected['artifact_id'],self.artifact['artifact_id'])
        self.assertEqual(reason,'explicit_reference')
        with self.store._connect() as db:
            db.execute("UPDATE planning_artifacts SET status='adopted'")
        trip = {'trip_id':'TR-saved','title':'北京已保存行程','spec':{'destination':'北京','day_count':3},
                'version':2,'plan':{'days':[]},'sources':{}}
        self.store.trips.list_for_owner = Mock(return_value=[trip])
        with patch('services.trip_service.TripService.format_trip',return_value=PLAN):
            selected, reason = resolve_plan(self.store,self.event,self.request)
        self.assertEqual(reason,'saved_trip')
        self.assertEqual(selected['content'],PLAN)
        self.assertEqual(selected['itinerary_snapshot']['trip_id'],'TR-saved')

    def test_target_extraction_grounding_and_exterior_optional(self):
        client = FakeClient([model_response(scope_value([target('鸟巢',3,False,'exterior'),target('国家博物馆',3,True)]))])
        result = ResearchSearchPlanner(client,'test').plan_targets(self.request,self.artifact)
        self.assertEqual(result['targets'][0]['visit_mode'],'exterior')
        self.assertTrue(result['targets'][1]['optional'])
        client = FakeClient([model_response(scope_value([target('不存在的景点')]))])
        with self.assertRaises(ValueError):
            ResearchSearchPlanner(client,'test').plan_targets(self.request,self.artifact)

    def test_target_extraction_rejects_invented_source_restriction(self):
        value = scope_value()
        value['requested_domains'] = ['xiaohongshu.com']
        with self.assertRaises(ValueError):
            ResearchSearchPlanner(FakeClient([model_response(value)]),'test').plan_targets(self.request,self.artifact)

    def test_full_original_request_recovers_topics_missing_from_initial_ir(self):
        request = {**self.request,'topics':['ticket_price']}
        result = ResearchSearchPlanner(FakeClient([model_response(scope_value())]),'test').plan_targets(request,self.artifact)
        self.assertEqual(result['fields']['topics'],['ticket_price','opening_hours','reservation_rules'])

    def test_wrong_day_is_corrected_and_exterior_is_preserved_from_plan(self):
        for day in (1,3):
            value = scope_value([target('鸟巢',day,False,'entry')])
            planner = ResearchSearchPlanner(FakeClient([model_response(value)]),'test')
            row = planner.plan_targets(self.request,self.artifact)['targets'][0]
            self.assertEqual(row['day_index'],3)
            self.assertEqual(row['visit_mode'],'exterior')

    def fact_service(self, coverage=None, targets=None):
        names = targets or [target('故宫')]
        client = FakeClient([model_response(scope_value(names)),model_response({'coverage':coverage or []}),
                             model_response({'coverage':[]})])
        search = Mock(configured=True)
        search.search.return_value = [{'url':'https://example.org/palace','title':'故宫游客须知','snippet':'故宫信息','published_at':''}]
        reader = Mock(return_value={'status':'read','text':'故宫成人门票60元。\n生效日期：2026-10-01\n有效期至：2026-10-31',
                                    'retrieved_at':'2026-10-19T02:00:00+00:00','content_hash':'hash'})
        return ResearchService(self.store,search,client,'test',reader=reader)

    def test_fact_research_evidence_dates_checkpoints_and_no_implicit_writes(self):
        coverage = [{'target_index':1,'topic':'ticket_price','item':{
            'text':'故宫成人门票60元。','sources':[1],'evidence':[{'source_id':1,'quote':'故宫成人门票60元。'}]}}]
        service = self.fact_service(coverage)
        reply = service.handle_intent(self.event,None,request=self.request)
        self.assertIn('2026-10-22 至 2026-10-24',reply)
        self.assertIn('60元',reply)
        self.assertIn('开放时间：未知',reply)
        self.assertIn('预约规则：未知',reply)
        self.assertEqual(self.store.research.get(self.event)['coverage']['covered_count'],1)
        self.assertEqual(self.store.research.get(self.event)['stage'],'partial')
        self.assertEqual(service.handle_intent(self.event,None,request=self.request),reply)
        self.assertEqual(service.search.search.call_count,1)
        self.assertEqual(service.reader.call_count,1)
        self.assertEqual(self.store.trips.list_for_owner(self.event),())
        self.assertEqual(len(self.store.planning.list_for_owner(self.event)),1)
        self.assertEqual(self.store.reminders.list_for_owner('web','conversation','owner'),())
        query = service.search.search.call_args.args[0]
        self.assertIn('2026-10-22',query)
        self.assertNotIn('owner',query)
        self.assertNotIn('我希望你',query)

    def test_batch_limit_lists_all_remaining_targets(self):
        names = [target(n,d) for n,d in (('天安门广场',1),('故宫',1),('景山公园',1),('北海公园',1),
                                        ('八达岭长城',2),('颐和园',3),('圆明园',3))]
        service = self.fact_service(targets=names)
        reply = service.handle_intent(self.event,None,request=self.request)
        self.assertEqual(service.search.search.call_count,4)
        self.assertIn('尚未完成核验的景点：八达岭长城、颐和园、圆明园',reply)
        self.assertEqual(self.store.research.get(self.event)['coverage']['target_count'],7)

    def test_soft_budget_returns_partial_without_losing_targets(self):
        service = self.fact_service()
        with patch('services.research_service.time.monotonic',side_effect=[0,200]):
            reply = service.handle_intent(self.event,None,request=self.request)
        self.assertIn('尚未完成核验的景点：故宫',reply)
        service.search.search.assert_not_called()

    def test_page_checkpoints_resume_without_repeating_saved_search(self):
        service = self.fact_service()
        scope = {'targets':[target('故宫')],'fields':{k:self.request[k] for k in
            ('query_text','topics','arrival_date_text','visit_start_date_text')},
            'requested_domains':[],'excluded_domains':[],'strict_sources':False,'language':'zh-cn'}
        rows = service.search.search.return_value
        self.store.research.save(self.event,'reading',{'query':TEXT,'sources':[],'target_scope':scope,
            'dates':research_dates(self.request,self.event,[3]),'plan_title':'北京参考方案','plan_version':1,
            'fact_batches':[{'start':0,'searches':[{'results':rows}],'sources':[]}]})
        service.client = FakeClient([model_response({'coverage':[]})])
        service.handle_intent(self.event,None,request=self.request)
        service.search.search.assert_not_called()
        service.reader.assert_called_once()

    def test_invalid_quote_or_wrong_entity_does_not_count_as_coverage(self):
        for quote in ('故宫成人门票99元。','八达岭门票60元。'):
            service = self.fact_service()
            service.client = FakeClient([model_response({'coverage':[{'target_index':1,'topic':'ticket_price',
                'item':{'text':'故宫成人门票60元。','sources':[1],'evidence':[{'source_id':1,'quote':quote}]}}]})])
            source = {'id':1,'status':'read','title':'故宫','text':'故宫成人门票60元。'}
            self.assertEqual(service._fact_synthesis([target('故宫')],['ticket_price'],[source],{}),[])

    def test_snippet_and_expired_evidence_are_not_success(self):
        coverage = [{'target_index':1,'topic':'ticket_price','item':{'text':'故宫成人门票60元。',
            'sources':[1],'evidence':[{'source_id':1,'quote':'故宫成人门票60元。'}]}}]
        for status,expected in [('snippet',[]),('read','conflict')]:
            service = self.fact_service()
            service.client = FakeClient([model_response({'coverage':coverage})])
            source = {'id':1,'status':status,'title':'故宫','text':'故宫成人门票60元。\n有效期至：2026-10-20'}
            result = service._fact_synthesis([target('故宫')],['ticket_price'],[source],{'visit_start_date':'2026-10-22'})
            self.assertEqual(result if status=='snippet' else result[0]['status'],expected)

    def test_conflicting_sources_are_labelled_without_selecting_a_winner(self):
        service = self.fact_service()
        quotes = ['故宫成人门票60元。','故宫成人门票80元。']
        coverage = [{'target_index':1,'topic':'ticket_price','conflict':True,'item':{
            'text':'故宫成人门票存在60元与80元两种说明，待核对。','sources':[1,2],
            'evidence':[{'source_id':i+1,'quote':q} for i,q in enumerate(quotes)]}}]
        service.client = FakeClient([model_response({'coverage':coverage})])
        sources = [{'id':i+1,'status':'read','text':q} for i,q in enumerate(quotes)]
        rows = service._fact_synthesis([target('故宫')],['ticket_price'],sources,{})
        self.assertEqual(rows[0]['status'],'conflict')

    def test_diagnostics_keep_field_names_only_and_flag_business_rejection(self):
        clean = metadata({'adopted_fields':['query_text'],'ignored_fields':['reason','private value!'],
                          'raw':'secret','schema_version':'research-request-v2','status':'research_rejected'})
        self.assertEqual(clean['ignored_fields'],['reason'])
        self.assertNotIn('secret',json.dumps(clean))
        self.assertTrue(explain({'id':1,'stage':'routing','created_at':'now','details':clean})['warning'])
        self.store.save_chat_message(self.event.event_key,'web',self.event.scope_id,self.event.sender_id,
                                     self.event.event_id,'','user',TEXT)
        self.store.diagnostics.record('routing',{'event_key':self.event.event_key,**clean})
        with self.store._connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM execution_diagnostics WHERE stage='routing'").fetchone()[0],1)
