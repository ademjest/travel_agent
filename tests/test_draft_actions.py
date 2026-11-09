"""Bound draft controls through the real Web queue, using fixed provider receipts."""
from copy import deepcopy
from datetime import datetime,timezone
import unittest
from unittest.mock import Mock

import test_web_app as web_fixtures
from test_trips import SPEC,PLACES,plan_value,model_response
from test_travel_agent import FakeClient
from core.intent_ir import IntentIR,IntentOperation
from app.request_router import RequestRouter
from tools.trip.planner import TripPlanner
from tools.trip.tool import TripTool


NOW=datetime(2030,9,30,8,tzinfo=timezone.utc)


class WorkflowHarness:
    def setUp(self):
        self.web=web_fixtures.WebTests();self.web.setUp();self.addCleanup(self.web.doCleanups)
        self.amap=Mock();self.amap.search_places.return_value=list(PLACES.values())
        self.amap.route_for_pois.return_value={'duration_seconds':600,'distance_meters':800}
        self.models=FakeClient([model_response({**SPEC,'start_date_text':'2030年10月1日',
            'intercity':{'scope':{'value':'none','source':'不查询跨城交通'}}}),model_response(plan_value())])
        self.tool=TripTool(self.web.store,TripPlanner(self.models,'test',self.amap),clock=lambda:NOW)
        self.web.runtime.application.trip_service=self.tool
        self.compiler=Mock();self.compiler.try_compile.return_value=IntentIR(1,'none',())
        self.router=RequestRouter(self.web.store,self.compiler,trip_service=self.tool,
            read_only_service=self.web.runtime.application.read_only_service)
        self.web.runtime.application.semantic_task_service=self.router

    def create(self,key='create'):
        text='帮我规划2030年10月1日开始的武汉三天行程，带老人，必去湖北省博物馆，不查询跨城交通'
        self.compiler.try_compile.return_value=IntentIR(1,'create',(IntentOperation('trip','create',{'request_text':text}),),source_spans=(text,))
        self.web.send(text,key=key);self.assertTrue(self.web.run_job())
        return self.context()['confirmations'][0]

    def context(self):return self.web.client.get(f'/api/conversations/{self.web.identity}/context').json()

    def action(self,draft,action,key,*,run=True):
        result=self.web.client.post(f'/api/conversations/{self.web.identity}/drafts/{draft["id"]}/actions',headers=self.web.headers,
            json={'version':draft['version'],'action':action,'client_request_id':key})
        if run and result.status_code==202:self.assertTrue(self.web.run_job())
        return result

    def expire(self,draft):
        with self.web.store._connect() as db:db.execute("UPDATE travel_tasks SET expires_at='2000-01-01' WHERE task_id=?",(draft['id'],))

    def save(self,draft,key='save'):
        response=self.web.client.post(f'/api/conversations/{self.web.identity}/confirmations/{draft["id"]}',headers=self.web.headers,
            json={'version':draft['version'],'client_request_id':key})
        self.assertEqual(response.status_code,202,response.text);self.assertTrue(self.web.run_job())
        return self.web.store.trips.list_for_owner(self.web.event(''))[0]

    def candidate(self):
        return deepcopy(self.web.store.trips.current_drafts(self.web.event(''))[0].slots['candidate_trip'])

    def patch(self,changes,text,key,checks=None,quote=None):
        from core.intent_ir import parse_intent_ir
        operation={'domain':'trip','operation':'update_constraints','target_ref':'current_trip','changes':changes}
        if checks:operation['requested_checks']=checks
        self.compiler.try_compile.return_value=parse_intent_ir({'schema_version':1,'action':'update','operations':[operation],
            'source_spans':[text]},text)
        body={'content':text,'client_request_id':key}
        if quote:body['quoted_message_id']=quote
        result=self.web.client.post(f'/api/conversations/{self.web.identity}/messages',headers=self.web.headers,json=body)
        self.assertEqual(result.status_code,202,result.text);self.assertTrue(self.web.run_job())
        return self.web.messages()[-1]['content']


class DraftActionTests(WorkflowHarness,unittest.TestCase):
    def test_expired_create_revalidate_then_save_without_replanning_or_models(self):
        first=self.create();original=deepcopy(self.web.store.trips.current_drafts(self.web.event(''))[0].slots['candidate_trip'])
        self.expire(first)
        expired=self.context()['confirmations'][0];self.assertFalse(expired['can_confirm'])
        self.assertEqual(self.action(first,'revalidate','renew').status_code,202)
        fresh=self.context()['confirmations'][0]
        self.assertNotEqual(fresh['id'],first['id']);self.assertTrue(fresh['can_confirm'])
        renewed=self.web.store.trips.current_drafts(self.web.event(''))[0].slots['candidate_trip']
        self.assertEqual(original['spec'],renewed['spec']);self.assertEqual(original['plan']['days'],renewed['plan']['days'])
        saved=self.save(fresh)
        self.assertEqual(saved['version'],1)
        self.assertEqual(len(self.models.completions.requests),2)
        self.assertFalse(self.web.store.planning.list_for_owner(self.web.event('')),'Replayed controls must not create exploratory plans')
        self.assertEqual(self.compiler.try_compile.call_count,1,'Buttons bypass intent compilation')

    def test_expired_discard_is_targeted_and_does_not_create_a_formal_trip(self):
        draft=self.create();self.expire(draft)
        self.assertEqual(self.action(draft,'discard','drop').status_code,202)
        self.assertFalse(self.context()['confirmations'])
        self.assertFalse(self.web.store.trips.current_drafts(self.web.event('')))
        self.assertFalse(self.web.store.trips.list_for_owner(self.web.event('')))
        self.assertEqual(self.action(draft,'discard','drop',run=False).status_code,202)
        self.assertFalse(self.web.run_job(),'A replay must not enqueue another operation')
        with self.web.store._connect() as db:
            self.assertEqual(db.execute('SELECT status FROM travel_tasks WHERE task_id=?',(draft['id'],)).fetchone()[0],'cancelled')
        self.assertEqual(len(self.models.completions.requests),2)

    def test_duplicate_ids_have_one_renewal_and_payload_reuse_is_rejected(self):
        draft=self.create()
        self.assertEqual(self.action(draft,'revalidate','one',run=False).status_code,202)
        self.assertEqual(self.action(draft,'revalidate','two',run=False).status_code,202)
        self.web.run_job();self.web.run_job()
        with self.web.store._connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM travel_tasks WHERE task_type='itinerary'").fetchone()[0],2)
        self.assertEqual(self.action(draft,'discard','one',run=False).status_code,409)
        self.assertEqual(len(self.models.completions.requests),2)

    def test_J12_expired_modification_preserves_unsaved_budgets(self):
        original=self.save(self.create())
        text='住宿300-400元，人均70以下'
        changes={'budgets':[{'scope':'accommodation','value':'300-400','source':'住宿300-400元'},
            {'scope':'food','value':'70以下','unit_text':'人均','source':'人均70以下'}]}
        self.patch(changes,text,'budgets');draft=self.context()['confirmations'][0];self.expire(draft)
        candidate=self.candidate();self.action(draft,'revalidate','renew-edit')
        self.assertEqual(self.candidate()['spec'],candidate['spec'])
        self.assertEqual(self.web.store.trips.get(self.web.event(''),original['trip_id'])['version'],1)
        saved=self.save(self.context()['confirmations'][0],'save-edit')
        self.assertEqual(saved['version'],2);self.assertEqual(saved['spec']['budgets']['food']['value'],'70以下')

    def test_J14_multiple_drafts_discard_only_bound_target(self):
        first=self.create();self.models.completions.responses.extend([model_response({**SPEC,'start_date_text':'2030年10月1日'}),model_response(plan_value())])
        second=self.create('second');drafts=self.web.store.trips.current_drafts(self.web.event(''))
        self.assertEqual(len(drafts),2);other=next(d for d in drafts if d.task_id!=first['id'])
        self.action(first,'discard','drop-first')
        remaining=self.web.store.trips.current_drafts(self.web.event(''))
        self.assertEqual([(d.task_id,d.version,d.slots) for d in remaining],[(other.task_id,other.version,other.slots)])

    def test_J15_old_page_and_old_confirmation_do_not_touch_replacement(self):
        first=self.create();self.action(first,'revalidate','renew')
        latest=self.candidate()
        self.assertEqual(self.action(first,'discard','old-discard',run=False).status_code,409)
        self.assertEqual(self.action(first,'revalidate','old-renew').status_code,202) # Completed operation replay, no new effect
        response=self.web.client.post(f'/api/conversations/{self.web.identity}/confirmations/{first["id"]}',headers=self.web.headers,
            json={'version':first['version'],'client_request_id':'old-save'})
        self.assertEqual(response.status_code,409);self.assertEqual(self.candidate(),latest)

    def test_bound_confirmation_saves_one_of_two_drafts_without_reselecting(self):
        first=self.create();self.models.completions.responses.extend([model_response({**SPEC,'start_date_text':'2030年10月1日'}),model_response(plan_value())])
        self.create('second-before-save')
        before=self.web.store.trips.current_drafts(self.web.event(''))
        other=next(t for t in before if t.task_id!=first['id'])
        saved=self.save(first)
        self.assertEqual(saved['version'],1)
        self.assertEqual([(t.task_id,t.version,t.slots) for t in self.web.store.trips.current_drafts(self.web.event(''))],
            [(other.task_id,other.version,other.slots)])
        self.assertEqual(len(self.models.completions.requests),4)

    def test_J17_base_change_reapplies_nonconflicting_edits_and_detects_conflicts(self):
        import json
        saved=self.save(self.create());text='住宿300元'
        self.patch({'budget':{'scope':'accommodation','value':'300','source':text}},text,'hotel')
        draft=self.context()['confirmations'][0]
        def advance(spec):
            updated={**saved,'version':saved['version']+1,'spec':spec}
            with self.web.store._connect() as db:
                db.execute('UPDATE trips SET version=?,spec_json=? WHERE trip_id=?',(updated['version'],json.dumps(spec),saved['trip_id']))
                db.execute('INSERT INTO trip_versions VALUES (?,?,?,?,?)',(saved['trip_id'],updated['version'],'external',json.dumps(updated),NOW.isoformat()))
        advance({**saved['spec'],'transport_preferences':{'preferred':['walking']}})
        self.action(draft,'revalidate','rebase')
        self.assertEqual(self.candidate()['spec']['budgets']['accommodation']['value'],'300')
        self.assertEqual(self.candidate()['spec']['transport_preferences']['preferred'],['walking'])
        current=self.context()['confirmations'][0]
        with self.web.store._connect() as db:
            spec=json.loads(db.execute('SELECT spec_json FROM trips').fetchone()[0]);spec['budgets']={'accommodation':{'value':'500','source':'新预算'}}
            db.execute('UPDATE trips SET version=3,spec_json=?',(json.dumps(spec),))
        self.action(current,'revalidate','conflict')
        self.assertEqual(self.web.store.trips.current_drafts(self.web.event(''))[0].task_id,current['id'])
        self.assertIn('相同项目',self.web.messages()[-1]['content'])

    def test_J18_discard_while_review_is_inflight_fences_late_publication(self):
        import asyncio,threading
        draft=self.create();started=threading.Event();released=threading.Event()
        original=self.tool.planner.validate_plan
        def wait(*args,**kwargs):
            started.set();released.wait(10);return original(*args,**kwargs)
        self.tool.planner.validate_plan=wait
        self.action(draft,'revalidate','slow',run=False)
        async def scenario():
            task=asyncio.create_task(self.web.worker.run_once(lane='normal'))
            self.assertTrue(await asyncio.to_thread(started.wait,5))
            self.action(draft,'discard','during',run=False)
            self.assertTrue(await self.web.worker.run_once(lane='normal'))
            released.set();await task
        try:asyncio.run(scenario())
        finally:released.set()
        self.assertFalse(self.web.store.trips.current_drafts(self.web.event('')))
        with self.web.store._connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM travel_tasks WHERE task_type='itinerary'").fetchone()[0],1)
            self.assertEqual(db.execute("SELECT status FROM inbox_jobs WHERE json_extract(payload_json,'$.request_id')='slow'").fetchone()[0],'cancelled')
            self.assertGreater(db.execute("SELECT count(*) FROM execution_diagnostics WHERE stage='draft_action' AND json_extract(details_json,'$.status')='cancelled'").fetchone()[0],0)

    def test_J19_failed_review_keeps_original_and_allows_explicit_retry(self):
        draft=self.create();old=self.candidate();original=self.tool.planner.validate_plan
        self.tool.planner.validate_plan=Mock(side_effect=ValueError('invalid fixture'))
        self.action(draft,'revalidate','failed')
        context=self.context()['confirmations'][0]
        self.assertFalse(context['can_confirm']);self.assertTrue(context['draft_state']['can_discard'])
        self.assertEqual(context['draft_state']['operation']['status'],'failed');self.assertEqual(self.candidate(),old)
        self.tool.planner.validate_plan=original;self.action(draft,'revalidate','explicit-retry')
        self.assertNotEqual(self.context()['confirmations'][0]['id'],draft['id'])

    def test_J20_accepted_request_survives_restart_and_response_loss(self):
        from adapters.web_app import create_web_app
        from core.settings import Settings
        draft=self.create();accepted=self.action(draft,'revalidate','lost',run=False).json()
        second=create_web_app(self.web.settings,Settings('','',frozenset(),'','','',''),start_workers=False)
        # The persisted Inbox is reused by a newly assembled worker; providers remain fixed.
        second.state.runtime.application.trip_service=self.tool
        second.state.runtime.application.semantic_task_service=self.router
        import asyncio
        self.assertTrue(asyncio.run(second.state.worker.run_once()))
        self.assertEqual(self.action(draft,'revalidate','lost',run=False).json()['job_id'],accepted['job_id'])
        self.assertFalse(self.web.run_job());self.assertEqual(len(self.web.store.trips.current_drafts(self.web.event(''))),1)
        self.assertEqual(len(self.models.completions.requests),2)

    def test_J22_cross_scope_forged_versions_and_mixed_payload_are_rejected(self):
        draft=self.create();other=self.web.client.post('/api/conversations',headers=self.web.headers).json()['id']
        path=f'/api/conversations/{other}/drafts/{draft["id"]}/actions'
        body={'version':draft['version'],'action':'discard','client_request_id':'cross'}
        self.assertEqual(self.web.client.post(path,headers=self.web.headers,json=body).status_code,409)
        self.assertEqual(self.action({**draft,'version':999},'discard','fake',run=False).status_code,409)
        path=f'/api/conversations/{self.web.identity}/drafts/{draft["id"]}/actions'
        self.assertEqual(self.web.client.post(path,headers=self.web.headers,json={**body,'candidate_trip':{}}).status_code,422)
        self.assertEqual(self.web.client.post(path,headers=self.web.headers,json={**body,'version':True}).status_code,422)
        self.assertEqual(len(self.models.completions.requests),2)

    def test_commit_guard_rejects_formal_change_during_review(self):
        self.save(self.create());text='住宿300元'
        self.patch({'budget':{'scope':'accommodation','value':'300','source':text}},text,'hotel-edit')
        draft=self.context()['confirmations'][0];original=self.tool.planner.validate_plan;changed=False
        def advance(*args,**kwargs):
            nonlocal changed
            if not changed:
                with self.web.store._connect() as db:db.execute('UPDATE trips SET version=2')
                changed=True
            return original(*args,**kwargs)
        self.tool.planner.validate_plan=advance;self.action(draft,'revalidate','change-during')
        self.assertEqual(self.context()['confirmations'][0]['id'],draft['id'])
        self.assertIn('再次变化',self.web.messages()[-1]['content'])

    def test_explicit_new_budget_resolves_only_the_corresponding_stale_conflict(self):
        import json
        saved=self.save(self.create());text='住宿300元'
        self.patch({'budget':{'scope':'accommodation','value':'300','source':text}},text,'budget-old')
        with self.web.store._connect() as db:
            spec=deepcopy(saved['spec']);spec['budgets']={'accommodation':{'value':'500','unit_text':'人民币每间每晚','source':'新住宿预算'}}
            db.execute('UPDATE trips SET version=2,spec_json=?',(json.dumps(spec),))
        text='住宿预算最终改为600元'
        reply=self.patch({'budget':{'scope':'accommodation','value':'600','source':text}},text,'resolve-budget')
        self.assertIn('预览',reply);self.assertEqual(self.candidate()['spec']['budgets']['accommodation']['value'],'600')
        self.assertEqual(self.web.store.trips.get(self.web.event(''),saved['trip_id'])['spec']['budgets']['accommodation']['value'],'500')


class PriceWorkflowTests(WorkflowHarness,unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.menu={f'r{p}':{'id':f'r{p}','name':'本地菜馆'+str(p),'address':f'测试路{p}号','type':'餐饮服务;中餐厅',
            'cost':str(p),'rating':'4.8'} for p in (69,70,71)}
        self.menu['rMissing']={'id':'rMissing','name':'本地菜馆待确认','address':'支路','type':'餐饮服务;中餐厅','rating':'4.8'}
        self.hotel={'hotel':{'id':'hotel','name':'连锁商务酒店','address':'测试酒店路','type':'住宿服务;酒店','cost':'350'}}
        self.amap.search_places.return_value=list({**PLACES,**self.menu,**self.hotel}.values())

    def dining(self):
        text='推荐午餐和晚餐的本地菜馆'
        return self.patch({'dining':{'value':text,'source':text,'cuisines':['本地菜'],
            'scopes':[{'day_index':day,'meal':meal} for day in (1,2,3) for meal in ('午餐','晚餐')]}},text,'dining')

    def budget(self,value='70以下',key='budget',scopes=None):
        text=('第二天午餐' if scopes else '')+'这些餐馆价格如何，人均消费'+value
        budget={'scope':'food','value':value,'source':'人均消费'+value,'unit_text':'人均消费'}
        if scopes:budget['scopes']=scopes
        return self.patch({'budget':budget},text,key,[{'scope':'food','source':'这些餐馆价格如何'}])

    def test_J01_J02_full_chain_without_quotes_keeps_all_requirements_until_save(self):
        self.create();text='酒店高档一点，300-400，尽量连锁'
        self.patch({'accommodation':{'value':'酒店高档一点','source':'酒店高档一点','quality_requested':True,
            'chain_mode':'preferred'},'budget':{'scope':'accommodation','value':'300-400','source':text}},text,'hotel')
        self.dining();before=self.candidate();calls=self.amap.search_places.call_count
        reply=self.budget();after=self.candidate()
        self.assertIn('当前候选的价格说明',reply);self.assertNotIn('这组操作暂不能',reply)
        self.assertEqual(after['spec']['budgets']['accommodation']['value'],'300-400')
        self.assertEqual(after['spec']['budgets']['accommodation']['unit_text'],'人民币每间每晚')
        for key in ('days','hotel_ids','hotel_recommendations'):self.assertEqual(before['plan'][key],after['plan'][key])
        self.assertEqual(self.amap.search_places.call_count,calls)
        self.assertEqual(self.web.store.trips.list_for_owner(self.web.event('')),())
        for meal in after['plan']['dining_options']:
            self.assertEqual({c['poi_id'] for c in meal['candidates']},{'r69','r70'})
        saved=self.save(self.context()['confirmations'][0]);self.assertEqual(saved['version'],1)
        self.assertEqual(saved['spec']['budgets'],after['spec']['budgets'])
        self.assertEqual(len(self.models.completions.requests),2)

    def test_J03_history_price_read_uses_old_ids_and_does_not_mutate_latest(self):
        self.create();self.dining();old=self.context()['confirmations'][0]
        quote=old['reference_message_id'];self.budget();current=self.candidate()
        self.compiler.try_compile.return_value=IntentIR(1,'read',(IntentOperation('trip','check_prices',{
            'requested_checks':[{'scope':'food','source':'只解释这些餐馆价格'}]}),),source_spans=('只解释这些餐馆价格',))
        result=self.web.client.post(f'/api/conversations/{self.web.identity}/messages',headers=self.web.headers,
            json={'content':'只解释这些餐馆价格','client_request_id':'history-read','quoted_message_id':quote})
        self.assertEqual(result.status_code,202);self.web.run_job()
        self.assertIn('71元',self.web.messages()[-1]['content']);self.assertEqual(self.candidate(),current)

    def test_J04_ambiguous_target_keeps_budget_until_selection(self):
        self.create();self.models.completions.responses.extend([model_response({**SPEC,'start_date_text':'2030年10月1日'}),model_response(plan_value())])
        self.create('second');before=self.web.store.trips.current_drafts(self.web.event(''))
        reply=self.budget();self.assertIn('哪一份',reply)
        after=self.web.store.trips.current_drafts(self.web.event(''));self.assertEqual(before,after)
        with self.web.store._connect() as db:
            import json
            row=db.execute("SELECT slots_json FROM travel_tasks WHERE task_type='semantic' ORDER BY rowid DESC").fetchone()
            self.assertEqual(json.loads(row[0])['semantic_ir']['operations'][0]['changes']['budget']['value'],'70以下')

    def test_J05_readonly_prices_and_budget_explanation_never_write_a_draft(self):
        self.create();self.dining();self.budget();before=self.web.store.trips.current_drafts(self.web.event(''))
        self.compiler.try_compile.return_value=IntentIR(1,'read',(IntentOperation('trip','check_prices',{
            'requested_checks':[{'scope':'food','source':'只解释价格和预算'},{'scope':'accommodation','source':'只解释价格和预算'}]}),),source_spans=('只解释价格和预算',))
        calls=self.amap.search_places.call_count;self.web.send('只解释价格和预算','explain');self.web.run_job()
        self.assertEqual(before,self.web.store.trips.current_drafts(self.web.event('')))
        self.assertEqual(calls,self.amap.search_places.call_count);self.assertIn('参考',self.web.messages()[-1]['content'])

    def test_J06_J07_unknown_zero_package_other_branch_and_strict_boundary(self):
        from tools.trip.planner import _price_evidence,price_details
        self.create();self.dining()
        for index,(value,expected) in enumerate([('70以下',{'r69','r70'}),('低于70',{'r69'}),('68以内',set())]):
            self.budget(value,key='boundary-'+str(index))
            for meal in self.candidate()['plan']['dining_options']:self.assertEqual({c['poi_id'] for c in meal['candidates']},expected)
        spec=self.candidate()['spec']
        for price in (None,'',0,'0','70元套餐'):
            self.assertEqual(_price_evidence({'cost':price},spec,'food','dining')[0],'unknown')
        self.assertIsNone(price_details({'cost':'60','price_unit':'双人套餐'},'dining')['per_capita'])
        self.assertIsNone(price_details({'name':self.menu['r69']['name'],'id':'other-branch'},'dining')['per_capita'])

    def test_J08_J09_single_meal_preserves_other_meals_hotels_dates_and_transport(self):
        self.create();self.dining();self.budget();before=self.candidate()
        reply=self.budget('90以内','one-meal',[{'day_index':2,'meal':'午餐'}]);after=self.candidate()
        self.assertIn('第2天午餐预算',reply)
        self.assertEqual(after['spec']['budgets']['food']['value'],'70以下')
        self.assertEqual(after['spec']['budgets']['food']['meals']['2:午餐']['value'],'90以内')
        for old,new in zip(before['plan']['dining_options'],after['plan']['dining_options']):
            if (old['day_index'],old['meal'])!=(2,'午餐'):self.assertEqual(old,new)
            else:self.assertEqual({c['poi_id'] for c in new['candidates']},{'r69','r70','r71'})
        self.assertEqual(before['plan']['days'],after['plan']['days']);self.assertEqual(before['plan']['hotel_ids'],after['plan']['hotel_ids'])

    def test_J24_source_restriction_and_tentative_date_keep_local_changes_offline(self):
        import json
        self.create();self.dining()
        with self.web.store._connect() as db:
            task=self.web.store.trips.current_drafts(self.web.event(''))[0];slots=deepcopy(task.slots)
            slots['candidate_trip']['spec'].update(date_is_tentative=True,date_reference_day='2030-09-30',research_context={'provider_policy':{'offline':True}})
            db.execute('UPDATE travel_tasks SET slots_json=? WHERE task_id=?',(json.dumps(slots),task.task_id))
        self.amap.search_places.side_effect=AssertionError('No new search');self.amap.route_for_pois.side_effect=AssertionError('No new route')
        self.tool.rail_executor=Mock(side_effect=AssertionError('No rail request'))
        self.budget();self.assertEqual(self.candidate()['spec']['budgets']['food']['value'],'70以下')
        self.assertTrue(self.candidate()['spec']['date_is_tentative'])

    def test_afternoon_departure_retains_new_budget_in_clarification_without_adding_dinner(self):
        import json
        self.create();self.dining();self.budget()
        task=self.web.store.trips.current_drafts(self.web.event(''))[0];slots=deepcopy(task.slots)
        spec=slots['candidate_trip']['spec'];spec['planning_window']={'start':spec['start_date'],'end':slots['candidate_trip']['plan']['days'][-1]['date'],
            'start_window':[6,10],'end_window':[13,18],'start_text':'首日早上','end_text':'末日下午'}
        with self.web.store._connect() as db:db.execute('UPDATE travel_tasks SET slots_json=? WHERE task_id=?',(json.dumps(slots),task.task_id))
        before=self.candidate();text='第三天晚餐人均120以内'
        reply=self.patch({'budget':{'scope':'food','value':'120以内','source':text,'unit_text':'人均',
            'scopes':[{'day_index':3,'meal':'晚餐'}]}},text,'impossible-meal')
        self.assertIn('离开',reply);self.assertEqual(self.candidate(),before)
        with self.web.store._connect() as db:
            pending=json.loads(db.execute("SELECT slots_json FROM travel_tasks WHERE task_type='semantic' ORDER BY rowid DESC").fetchone()[0])
        self.assertEqual(pending['semantic_ir']['operations'][0]['changes']['budget']['value'],'120以内')


    def test_J01_referenced_price_and_budget_chain_uses_the_quoted_current_draft(self):
        self.create();self.dining();before=self.candidate();draft=self.context()['confirmations'][0]
        text='这些餐馆价格如何，我希望不要太贵的，人均消费70以下。'
        self.patch({'budget':{'scope':'food','value':'70以下','unit_text':'人均消费','source':'人均消费70以下'}},
            text,'quoted-original',[{'scope':'food','source':'这些餐馆价格如何'}],quote=draft['reference_message_id'])
        self.assertEqual(self.candidate()['trip_id'],before['trip_id'])
        self.assertEqual(self.candidate()['plan']['days'],before['plan']['days'])
        saved=self.save(self.context()['confirmations'][0]);self.assertEqual(saved['spec']['budgets']['food']['value'],'70以下')

    def test_independent_research_is_not_silently_merged_into_a_budget_change(self):
        self.create();text='查景点开放时间，同时餐饮人均70以内'
        independent=IntentIR(1,'update',(IntentOperation('research','search_travel_guides',{'query_text':text,'topics':['opening_hours']}),
            IntentOperation('trip','update_constraints',{'changes':{'budget':{'scope':'food','value':'70以内','unit_text':'人均','source':'餐饮人均70以内'}}})),source_spans=(text,))
        self.compiler.try_compile.return_value=independent;before=self.candidate();self.web.send(text,'independent');self.web.run_job()
        self.assertEqual(self.candidate(),before);self.assertIn('暂不能一起',self.web.messages()[-1]['content'])


    def test_writer_scope_contains_only_the_requested_meals(self):
        from tools.trip.planner import meal_scopes
        self.create();self.dining();candidate=self.candidate()
        self.assertEqual({s['meal'] for s in meal_scopes(candidate['spec'],candidate['plan'])},{'午餐','晚餐'})


    def test_price_explanation_is_not_reused_as_current_evidence_in_later_edits(self):
        self.create();self.dining();self.budget();text='住宿350元'
        reply=self.patch({'budget':{'scope':'accommodation','value':'350','source':text}},text,'hotel-after-prices')
        self.assertNotIn('当前候选的价格说明',reply)
        self.assertNotIn('price_check_text',self.web.store.trips.current_drafts(self.web.event(''))[0].slots)
