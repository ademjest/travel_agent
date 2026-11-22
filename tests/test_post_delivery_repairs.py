from copy import deepcopy
from datetime import datetime,timedelta,timezone
from itertools import count
import json,unittest
from unittest.mock import Mock,patch
from pathlib import Path
from tools.trip.planner import TripPlanner,TripPlanError,_price_evidence
from tools.trip.constraints import apply_constraints
from tools.trip.tool import TripTool
from test_trips import model_response,PLACES,plan_value
from test_travel_agent import FakeClient
from test_draft_actions import WorkflowHarness

class AuditPureTests(unittest.TestCase):
    def test_F01_city_and_scenic_names_preserve_entity_identity(self):
        places={'a':{'id':'a','name':'云泽湖景区','cityname':'测试市','type':'风景名胜;公园'}}
        plan=plan_value((('a',),))
        result=TripPlanner.validate_plan(plan,{'destination':'测试市','day_count':1,'must_visit':['测试市云泽湖']},places)
        self.assertEqual(result['days'][0]['activities'][0]['poi_id'],'a')
        self.assertEqual(places['a']['name'],'云泽湖景区')

    def test_F03_missing_required_has_a_repairable_code_and_original_path(self):
        places={'a':{'id':'a','name':'甲馆'},'b':{'id':'b','name':'乙馆'}}
        planner=TripPlanner(FakeClient([model_response(plan_value((('a',),)))]),'test',Mock())
        with self.assertRaises(TripPlanError) as caught:planner.plan({'day_count':1,'must_visit':['乙馆']},places,attempts=1)
        self.assertEqual(caught.exception.code,'missing_required_entity');self.assertTrue(caught.exception.path)
        self.assertIsNotNone(getattr(caught.exception,'candidate',None))

    def test_F08_budget_math_uses_direction_and_inclusive_boundaries(self):
        cases={'不低于70元':[False,True,True],'不少于70元':[False,True,True],
            '不超过70元':[True,True,False],'低于70元':[True,False,False],'70元以下':[True,True,False],
            '高于70元':[False,False,True],'70-90元':[False,True,True]}
        for value,expected in cases.items():
            text='人均每餐'+value
            spec,_=apply_constraints({}, {'budget':{'scope':'food','value':value,'source':text,'unit_text':'人均每餐'}},text)
            self.assertEqual([_price_evidence({'cost':str(n)},spec,'food','dining')[0]=='reference_in_budget' for n in (69,70,71)],expected,value)

    def test_F09_explicit_all_meal_reset_clears_exceptions_only_with_evidence(self):
        spec,_=apply_constraints({}, {'budget':{'scope':'food','value':'70以内','source':'人均70以内','unit_text':'人均'}},'人均70以内')
        spec,_=apply_constraints(spec,{'budget':{'scope':'food','value':'90以内','source':'午餐人均90以内','unit_text':'人均',
            'scopes':[{'day_index':2,'meal':'午餐'}]}},'午餐人均90以内')
        text='所有餐次统一为人均60以内，不保留单餐例外'
        result,_=apply_constraints(spec,{'budget':{'scope':'food','value':'60以内','source':text,'unit_text':'人均',
            'meal_policy':'replace_all','meal_policy_source':text}},text)
        self.assertFalse(result['budgets']['food'].get('meals'));self.assertIn('2:午餐',spec['budgets']['food']['meals'])

    def test_F10_unmatched_specific_merchant_has_an_explicit_answer(self):
        trip={'spec':{},'plan':{'restaurant_ids':['r']},'sources':{'r':{'id':'r','name':'甲餐馆','cost':'60'}}}
        answer=TripTool.price_check_text(trip,[{'scope':'food','names':['乙餐馆']}])
        self.assertIn('未找到',answer);self.assertIn('乙餐馆',answer);self.assertIn('甲餐馆',answer)

    def test_required_subactivities_are_visible_in_narrative_and_writer_input(self):
        from tools.trip.presentation import TripAnswerWriter
        from infrastructure.memory_store import MemoryStore
        from test_trip_alternatives import PLACES as grouped
        import tempfile
        answer={'overview':'按相邻场馆游览','change_summary':'','days':[],'stay':'','meals':[]}
        trip={'trip_id':'TR-sub','title':'场馆游览','version':1,'status':'active','spec':{'day_count':1,'destination':'测试市'},
            'sources':deepcopy(grouped),'plan':{'days':[{'day_index':1,'date':'','activities':[{'poi_id':'park','period':'上午','reason':'兴趣匹配','subactivities':[{'poi_id':'hall'}]}]}]},
            'answer':{'overview':answer['overview'],'change_summary':'','days':{},'stay':'','meals':{}}}
        text=TripTool.format_trip(trip,preview=True)
        self.assertIn(grouped['hall']['name'],text);self.assertIn('票务需分别核对',text)
        with tempfile.TemporaryDirectory() as folder:
            store=MemoryStore(Path(folder)/'state.db');client=FakeClient([model_response(answer)])
            from core.chat_transport import ChatEvent
            event=ChatEvent('web','group','sub','scope','owner','生成说明')
            store.delivery.begin_event(event.event_key)
            TripAnswerWriter(store,client,'fixture').generate(event,trip,[])
            payload=json.loads(client.completions.requests[0]['messages'][1]['content'])
            self.assertEqual(payload['days'][0]['activities'][0]['subactivities'][0]['name'],grouped['hall']['name'])


class AuditRevalidationTests(WorkflowHarness,unittest.TestCase):
    def test_F05_normalized_dates_are_used_and_additional_plan_fields_survive(self):
        self.create();task=self.web.store.trips.current_drafts(self.web.event(''))[0];slots=deepcopy(task.slots)
        slots['candidate_trip']['spec']['start_date']='2030-10-02'
        slots['candidate_trip']['plan']['intercity']={'completeness':'not_required','marker':'preserve'}
        with self.web.store._connect() as db:db.execute('UPDATE travel_tasks SET slots_json=? WHERE task_id=?',(json.dumps(slots),task.task_id))
        self.action({'id':task.task_id,'version':task.version},'revalidate','normalize')
        candidate=self.candidate()
        self.assertEqual(candidate['plan']['days'][0]['date'],'2030-10-02')
        self.assertEqual(candidate['plan']['intercity']['marker'],'preserve')

    def test_F07_draft_action_reaches_persistent_diagnostics(self):
        draft=self.create();self.action(draft,'discard','diagnose-discard')
        with self.web.store._connect() as db:
            rows=[json.loads(r[0]) for r in db.execute("SELECT details_json FROM execution_diagnostics WHERE stage='draft_action'")]
        self.assertTrue(any(r['status']=='started' for r in rows));self.assertTrue(any(r['status']=='completed' for r in rows))

    def test_V03_pending_budget_is_not_applied_by_a_readonly_question(self):
        from core.intent_ir import IntentIR,IntentOperation
        self.create();self.patch({'budget':{'scope':'food','value':'70','source':'预算70'}},'预算70','pending-unit')
        original=self.candidate();text='只问当前餐馆价格，不要修改'
        self.compiler.try_compile.return_value=IntentIR(1,'read',(IntentOperation('trip','check_prices',{
            'requested_checks':[{'scope':'food','source':text}]}),),source_spans=(text,))
        self.web.send(text,'pure-read');self.web.run_job()
        self.assertEqual(self.candidate(),original);self.assertIn('当前候选的价格说明',self.web.messages()[-1]['content'])
        with self.web.store._connect() as db:self.assertEqual(db.execute("SELECT count(*) FROM travel_tasks WHERE task_type='semantic' AND status='collecting'").fetchone()[0],1)

class CreationAuditTests(WorkflowHarness,unittest.TestCase):
    def setUp(self):
        super().setUp()
        from tools.research.tool import ResearchTool
        from tools.trip.tool import TripTool
        self.fixture=json.loads((Path(__file__).parent/'fixtures/request_208_public_2026-11-02.json').read_text(encoding='utf-8'))
        self.request=self.fixture['request'];self.now=datetime(2026,11,2,11,24,tzinfo=timezone.utc)
        clock=patch('infrastructure.task_repository.utc_now',lambda:self.now);clock.start();self.addCleanup(clock.stop)
        stamps=count()
        web_clock=patch('infrastructure.web_repository.timestamp',
            lambda:(self.now+timedelta(microseconds=next(stamps))).isoformat())
        web_clock.start();self.addCleanup(web_clock.stop)
        self.amap.search_places.return_value=list(self.fixture['places'].values())
        self.amap.route_for_pois.return_value={'duration_seconds':600,'distance_meters':800}
        spec=self.fixture['requirements']
        self.requirements={k:spec[k] for k in ('destination','duration_text','start_date_text','end_date_text','must_visit','transport_text','preferences')}
        self.requirements.update(intercity={'scope':{'value':'none','source':'先不用查询跨城交通'}},city_windows={
            'arrival':{'day_index':1,'time_text':'早上','source':'第一天早上到南京市区'},
            'departure':{'day_index':3,'time_text':'下午','source':'第三天下午离开'}})
        self.ids=[]
        for name in ('六朝博物馆','总统府','玄武湖景区'):
            self.ids.append(next(pid for pid,p in self.fixture['places'].items() if p['name']==name and '风景' in p.get('type','') or p['name']==name and name=='六朝博物馆'))
        self.valid=plan_value(tuple((pid,) for pid in self.ids))
        self.provider=Mock(configured=True)
        self.provider.query.return_value={'status':'completed','physical_calls':1,'retrieved_at':self.now.isoformat(),
            'content_hash':'request208-fixed','text':self.fixture['wendao_text']}
        self.research=ResearchTool(self.web.store,Mock(configured=False),wendao=self.provider)
        self.tool=TripTool(self.web.store,TripPlanner(self.models,'test',self.amap),clock=lambda:self.now, research_reader=self.research)
        self.web.runtime.application.trip_service=self.tool;self.router.trip_service=self.tool
        self.tool.rail_executor=Mock(side_effect=AssertionError('No intercity query authorized'))

    def run_create(self,responses,enabled=True,key='first-create'):
        from core.intent_ir import IntentIR,IntentOperation
        self.models.completions.responses=[model_response(v) for v in [self.requirements,*responses]]
        if not enabled:self.tool.research_service=None
        self.compiler.try_compile.return_value=IntentIR(1,'create',(IntentOperation('trip','create',{'request_text':self.request}),),source_spans=(self.request[:150],))
        self.assertFalse(self.web.store.trips.current_drafts(self.web.event('')))
        self.assertFalse(self.web.store.trips.list_for_owner(self.web.event('')))
        self.web.send(self.request,key);self.web.run_job()
        return self.context()

    def test_A01_A04_request208_blank_creation_retains_places_and_repairs_once(self):
        bad=deepcopy(self.valid);bad['days'][0]['activities'][0]['period']='无效时段'
        old=self.fixture['rejected_candidate']
        context=self.run_create([bad,old,{'choice':'merge','reason':'保留必去对象','plan':self.valid}])
        self.assertTrue(context['confirmations'],self.web.messages()[-1]['content'])
        candidate=self.candidate();selected={a['poi_id'] for d in candidate['plan']['days'] for a in d['activities']}
        self.assertTrue(set(self.ids)<=selected);self.assertEqual(len(self.models.completions.requests),4)
        self.assertEqual(self.provider.query.call_count,1)
        self.assertNotIn('南京总统府',[c.args[1] for c in self.amap.search_places.call_args_list])
        self.assertEqual(candidate['spec']['intercity_request']['scope'],'none');self.tool.rail_executor.assert_not_called()
        self.assertFalse(self.web.store.trips.list_for_owner(self.web.event('')))
        self.assertNotIn('晚上',[a['period'] for a in candidate['plan']['days'][-1]['activities']])

    def test_A02_A06_disabled_wendao_creates_from_blank_and_keeps_city_windows(self):
        context=self.run_create([self.valid],enabled=False)
        self.assertTrue(context['confirmations'],self.web.messages()[-1]['content']);self.provider.query.assert_not_called()
        candidate=self.candidate();self.assertEqual(candidate['spec']['city_windows']['departure']['day_index'],3)
        from tools.trip.planner import meal_scopes
        self.assertNotIn((3,'晚餐'),{(m['day_index'],m['meal']) for m in meal_scopes(candidate['spec'],candidate['plan'])})
        self.assertEqual(candidate['spec']['intercity_request']['scope'],'none')
        saved=self.save(context['confirmations'][0]);self.assertEqual(saved['version'],1)

    def test_A05_failed_creation_keeps_requirements_and_resumes_without_provider_requery(self):
        bad=deepcopy(self.valid);bad['days'][0]['activities'][0]['period']='无效时段'
        context=self.run_create([bad,{'choice':'merge','plan':bad},{'choice':'merge','plan':bad}])
        self.assertFalse(context['confirmations']);self.assertFalse(self.web.store.trips.list_for_owner(self.web.event('')))
        self.assertIn('尚无可确认草案',self.web.messages()[-1]['content'])
        with self.web.store._connect() as db:
            task=json.loads(db.execute("SELECT slots_json FROM travel_tasks WHERE status='collecting' ORDER BY rowid DESC").fetchone()[0])
        self.assertTrue(task['failure_places']);self.assertEqual(task['spec']['must_visit'],self.fixture['requirements']['must_visit'])
        searches=self.amap.search_places.call_count;providers=self.provider.query.call_count
        self.models.completions.responses.append(model_response(self.valid))
        self.web.send('继续规划','continue-failed');self.web.run_job()
        self.assertTrue(self.context()['confirmations'],self.web.messages()[-1]['content'])
        self.assertEqual(self.amap.search_places.call_count,searches);self.assertEqual(self.provider.query.call_count,providers)


    def test_complete_creation_with_semicolons_remains_one_workflow(self):
        self.request=self.request.replace('，先不用查询跨城交通','；先不用查询跨城交通')
        context=self.run_create([self.valid],enabled=False,key='semicolon-create')
        self.assertTrue(context['confirmations'],self.web.messages()[-1]['content'])
        self.assertEqual(self.compiler.try_compile.call_count,1)

    def test_A20_blank_full_script_to_save_discard_and_readback(self):
        context=self.run_create([self.valid],enabled=False)
        self.assertTrue(context['confirmations'])
        extra={f'r{cost}':{'id':f'r{cost}','name':f'南京特色菜馆{cost}','type':'餐饮服务;中餐厅','address':f'测试街{cost}号',
            'cost':str(cost),'rating':'4.9' if cost==71 else '4.8'} for cost in (59,69,70,71)}
        extra['hotel']={'id':'hotel','name':'全季酒店测试店','address':'测试酒店路','type':'住宿服务;宾馆酒店;四星级宾馆','cost':'350','rating':'4.7'}
        self.amap.search_places.return_value=[*self.fixture['places'].values(),*extra.values()]
        text='酒店高档一点，尽量连锁，300-400'
        self.patch({'accommodation':{'value':'酒店高档一点','source':'酒店高档一点','quality_requested':True,'chain_mode':'preferred'},
            'budget':{'scope':'accommodation','value':'300-400','source':text}},text,'hotel')
        hotel=deepcopy(self.candidate()['plan']['hotel_recommendations']);self.assertTrue(hotel,self.web.messages()[-1]['content'])
        text='每天午餐和晚餐想吃南京特色菜'
        self.patch({'dining':{'value':text,'source':text,'cuisines':['南京特色菜'],
            'scopes':[{'day_index':d,'meal':m} for d in (1,2,3) for m in ('午餐','晚餐')]}},text,'meals')
        self.assertEqual({(m['day_index'],m['meal']) for m in self.candidate()['plan']['dining_options']},
            {(1,'午餐'),(1,'晚餐'),(2,'午餐'),(2,'晚餐'),(3,'午餐')})
        text='这些餐馆价格如何，人均70以下'
        self.patch({'budget':{'scope':'food','value':'70以下','unit_text':'人均','source':'人均70以下'}},text,'food70',
            checks=[{'scope':'food','source':'这些餐馆价格如何'}])
        base=self.candidate()
        for meal in base['plan']['dining_options']:
            self.assertEqual({c['poi_id'] for c in meal['candidates']},{'r59','r69','r70'})
        text='第二天午餐人均90以内'
        self.patch({'budget':{'scope':'food','value':'90以内','unit_text':'人均','source':text,'scopes':[{'day_index':2,'meal':'午餐'}]}},text,'meal90')
        current=self.candidate()
        for old,new in zip(base['plan']['dining_options'],current['plan']['dining_options']):
            if (old['day_index'],old['meal'])!=(2,'午餐'):self.assertEqual(old,new)
        self.assertEqual(current['plan']['hotel_recommendations'],hotel)
        text='所有餐次统一改为人均60以内，不保留第二天午餐例外'
        self.patch({'budget':{'scope':'food','value':'60以内','unit_text':'人均','source':text,'meal_policy':'replace_all','meal_policy_source':text}},text,'clear-exceptions')
        current=self.candidate();self.assertFalse(current['spec']['budgets']['food'].get('meals'))
        self.assertTrue(all({c['poi_id'] for c in m['candidates']}=={'r59'} for m in current['plan']['dining_options']))
        from core.intent_ir import IntentIR,IntentOperation
        text='只解释当前餐次预算，不要修改'
        self.compiler.try_compile.return_value=IntentIR(1,'read',(IntentOperation('trip','check_prices',{
            'requested_checks':[{'scope':'food','source':text}]}),),source_spans=(text,))
        before=self.web.store.trips.current_drafts(self.web.event(''))
        self.web.send(text,'read-before-save');self.web.run_job()
        self.assertEqual(self.web.store.trips.current_drafts(self.web.event('')),before)
        self.assertIn('第2天午餐预算：60以内',self.web.messages()[-1]['content'])
        calls=len(self.models.completions.requests);draft=self.context()['confirmations'][0]
        self.action(draft,'revalidate','recheck-all');fresh=self.context()['confirmations'][0]
        saved=self.save(fresh);self.assertEqual(saved['version'],1)
        self.assertEqual(len(self.models.completions.requests),calls)
        self.assertEqual(saved['spec']['budgets']['accommodation']['value'],'300-400')
        self.assertEqual(saved['plan']['days'],current['plan']['days'])
        text='第二天晚餐人均80以内'
        self.patch({'budget':{'scope':'food','value':'80以内','unit_text':'人均','source':text,'scopes':[{'day_index':2,'meal':'晚餐'}]}},text,'draft80')
        self.action(self.context()['confirmations'][0],'discard','discard80')
        formal=self.web.store.trips.get(self.web.event(''),saved['trip_id'])
        self.assertEqual(formal['spec']['budgets']['food']['value'],'60以内');self.assertFalse(formal['spec']['budgets']['food'].get('meals'))
        self.assertEqual(formal['version'],1);self.tool.rail_executor.assert_not_called()


class DerivedStateAuditTests(WorkflowHarness,unittest.TestCase):
    def prepare_budget_draft(self,positions=None):
        self.amap.search_places.return_value=[*PLACES.values(),{'id':'r','name':'测试菜馆','type':'餐饮服务;餐厅','cost':'60','rating':'4.8'}]
        if positions:
            combined={p['id']:deepcopy(p) for p in self.amap.search_places.return_value};combined.update(positions)
            self.amap.search_places.return_value=list(combined.values())
        self.create();text='推荐每天午餐和晚餐'
        self.patch({'dining':{'value':text,'source':text,'scopes':[{'day_index':i,'meal':m} for i in (1,2,3) for m in ('午餐','晚餐')]}},text,'add-meals')
        self.patch({'budget':{'scope':'food','value':'100以内','unit_text':'人均','source':'人均100以内'}},'人均100以内','food-100')
        self.saved=self.save(self.context()['confirmations'][0]);self.patch({'budget':{'scope':'food','value':'70以内','unit_text':'人均','source':'人均70以内'}},'人均70以内','food-70')
        return self.context()['confirmations'][0]

    def test_A08_F06_changed_source_price_recomputes_recommendations_without_unrelated_queries(self):
        draft=self.prepare_budget_draft();source=deepcopy(self.saved['sources']);source['r']['cost']='95'
        with self.web.store._connect() as db:db.execute('UPDATE trips SET version=2,sources_json=? WHERE trip_id=?',(json.dumps(source),self.saved['trip_id']))
        searches=self.amap.search_places.call_count;routes=self.amap.route_for_pois.call_count
        self.action(draft,'revalidate','new-facts');candidate=self.candidate()
        self.assertEqual(candidate['sources']['r']['cost'],'95');self.assertEqual(candidate['spec']['budgets']['food']['value'],'70以内')
        self.assertTrue(candidate['plan']['dining_options'])
        self.assertTrue(all(not m['candidates'] and m.get('note') for m in candidate['plan']['dining_options']))
        self.assertEqual(self.amap.search_places.call_count,searches);self.assertEqual(self.amap.route_for_pois.call_count,routes)
        saved=self.save(self.context()['confirmations'][0],'save-consistent');self.assertEqual(saved['version'],3)
        self.assertTrue(all(not m['candidates'] for m in saved['plan']['dining_options']))

    def test_A09_merged_activity_change_rebinds_meals_and_drops_old_routes(self):
        positions={pid:{**deepcopy(PLACES[pid]),'location':location} for pid,location in [('h','114,30'),('a','114,30'),('g','115,30')]}
        positions.update({'r':{'id':'r','name':'旧游览区餐馆','type':'餐饮服务;餐厅','cost':'60','rating':'4.8','location':'114,30'},
            'r-new':{'id':'r-new','name':'新游览区餐馆','type':'餐饮服务;餐厅','cost':'60','rating':'4.8','location':'115,30'}})
        draft=self.prepare_budget_draft(positions)
        old=self.candidate();old_meal=next(m for m in old['plan']['dining_options'] if (m['day_index'],m['meal'])==(1,'晚餐'))
        self.assertEqual(old_meal['candidates'][0]['poi_id'],'r')
        plan=deepcopy(self.saved['plan']);plan['days'][0]['activities'][1]['poi_id']='g'
        plan['days'][0]['legs']=[{'from_poi':'h','to_poi':'a','mode':'walking','duration_seconds':600,'queried_at':'2030-09-30T08:00:00+00:00'}]
        with self.web.store._connect() as db:db.execute('UPDATE trips SET version=2,plan_json=? WHERE trip_id=?',(json.dumps(plan),self.saved['trip_id']))
        self.action(draft,'revalidate','merge-new-area');current=self.candidate()
        dinner=next(m for m in current['plan']['dining_options'] if (m['day_index'],m['meal'])==(1,'晚餐'))
        self.assertEqual(dinner['candidates'][0]['poi_id'],'r-new')
        self.assertEqual(current['plan']['days'][0]['activities'][1]['poi_id'],'g')
        self.assertFalse(current['plan']['days'][0].get('legs'))
        self.assertEqual(current['spec']['budgets']['food']['value'],'70以内')
        self.assertEqual(current['plan']['days'][1:],old['plan']['days'][1:])

    def test_A10_unproven_old_narrative_is_removed_without_rewriting_history(self):
        self.create();task=self.web.store.trips.current_drafts(self.web.event(''))[0];slots=deepcopy(task.slots)
        slots['candidate_trip']['answer']={'overview':'仍去旧景点','change_summary':'','days':{},'stay':'住旧酒店','meals':{}}
        history=deepcopy(self.web.messages())
        with self.web.store._connect() as db:db.execute('UPDATE travel_tasks SET slots_json=? WHERE task_id=?',(json.dumps(slots),task.task_id))
        self.action({'id':task.task_id,'version':task.version},'revalidate','remove-old-answer')
        self.assertNotIn('answer',self.candidate());self.assertNotIn('旧酒店',self.web.messages()[-1]['content'])
        self.assertEqual([m['content'] for m in self.web.messages()[:len(history)]],[m['content'] for m in history])

    def test_A07_inconsistent_window_is_rejected_instead_of_published(self):
        self.create();task=self.web.store.trips.current_drafts(self.web.event(''))[0];slots=deepcopy(task.slots)
        candidate=slots['candidate_trip'];candidate['spec']['start_date']='2030-10-02'
        candidate['spec']['planning_window']={'start':'2030-10-01','end':'2030-10-03','start_window':[6,10],'end_window':[12,18]}
        with self.web.store._connect() as db:db.execute('UPDATE travel_tasks SET slots_json=? WHERE task_id=?',(json.dumps(slots),task.task_id))
        self.action({'id':task.task_id,'version':task.version},'revalidate','bad-window')
        self.assertEqual(self.web.store.trips.current_drafts(self.web.event(''))[0].task_id,task.task_id)
        self.assertIn('窗口',self.web.messages()[-1]['content'])

    def test_A12_reset_and_default_update_have_different_persisted_effects(self):
        self.prepare_budget_draft();text='第二天午餐人均90以内'
        patch_value={'scope':'food','value':'90以内','unit_text':'人均','source':text,'scopes':[{'day_index':2,'meal':'午餐'}]}
        self.patch({'budget':patch_value},text,'meal-exception')
        self.patch({'budget':{'scope':'food','value':'60以内','unit_text':'人均','source':'默认人均60以内'}},'默认人均60以内','default60')
        self.assertEqual(self.candidate()['spec']['budgets']['food']['meals']['2:午餐']['value'],'90以内')
        text='全部餐次统一人均60以内，不保留单餐例外'
        self.patch({'budget':{'scope':'food','value':'60以内','unit_text':'人均','source':text,'meal_policy':'replace_all','meal_policy_source':text}},text,'all60')
        self.assertFalse(self.candidate()['spec']['budgets']['food'].get('meals'))
        self.assertTrue(all({c['poi_id'] for c in meal['candidates']}=={'r'} for meal in self.candidate()['plan']['dining_options']))
        saved=self.save(self.context()['confirmations'][0],'save-all60')
        self.assertFalse(saved['spec']['budgets']['food'].get('meals'))

    def test_A18_V01_saved_restrictions_cover_routes_rail_and_linked_policy(self):
        from core.tasks import utc_now
        policies=[{'research_context':{'provider_policy':{'offline':True}}},
            {'source_context':{'provider_policy':{'only':True}}},
            {'research_context':{'strict_sources':True,'preferred_domains':['example.org']}}]
        self.create();saved=self.save(self.context()['confirmations'][0]);event=self.web.event('')
        with self.web.store._connect() as db:
            owner_task=db.execute('SELECT task_id FROM travel_tasks ORDER BY rowid DESC LIMIT 1').fetchone()[0]
            self.web.store.reminders.apply_change(db,event,owner_task,'create',{
                'reminder_id':'R-source-limit','title':'测试关联预约','scheduled_at_utc':'2030-10-01T00:00:00+00:00',
                'source':{'kind':'booking_policy','trip_id':saved['trip_id'],'entity':'湖北省博物馆','visit_date':'2030-10-02'}},self.tool.clock())
        for i,policy in enumerate(policies):
            spec={**deepcopy(saved['spec']),**policy}
            with self.web.store._connect() as db:db.execute('UPDATE trips SET spec_json=? WHERE trip_id=?',(json.dumps(spec),saved['trip_id']))
            text='住宿预算350'
            self.tool.policy_resolver=Mock();self.tool.policy_resolver.resolve.side_effect=AssertionError('No rule HTTP')
            self.patch({'budget':{'scope':'accommodation','value':'350','source':text}},text,'restricted-edit-'+str(i))
            task=self.web.store.trips.current_drafts(event)[0];slots=deepcopy(task.slots);candidate=slots['candidate_trip']
            for key in ('research_context','source_context'):candidate['spec'].pop(key,None)
            candidate['spec'].update(policy)
            candidate['sources']['h']['location']='114,30';candidate['sources']['a']['location']='114.01,30.01'
            candidate['plan']['days'][0]['legs']=[{'from_poi':'h','to_poi':'a','mode':'walking','duration_seconds':600,'distance_meters':1000,'queried_at':'2020-01-01T00:00:00+00:00'}]
            candidate['plan']['intercity']={'completeness':'complete','outbound':{'selected':{
                'train_code':'G-test','origin_station':'甲站','destination_station':'乙站','departure_at':'2030-10-01T06:00:00+08:00',
                'arrival_at':'2030-10-01T08:00:00+08:00'},'queried_at':'2020-01-01T00:00:00+00:00'}}
            with self.web.store._connect() as db:
                db.execute('UPDATE travel_tasks SET slots_json=? WHERE task_id=?',(json.dumps(slots),task.task_id))
                old=tuple(tuple(r) for r in db.execute('SELECT * FROM personal_reminders'))
            self.tool.planner.amap=Mock();self.tool.planner.amap.route_for_pois.side_effect=AssertionError('No map HTTP')
            self.tool.rail_executor=Mock(side_effect=AssertionError('No rail HTTP'))
            self.action({'id':task.task_id,'version':task.version},'revalidate','restricted-review-'+str(i))
            fresh=self.web.store.trips.current_drafts(event)[0]
            self.assertNotEqual(fresh.task_id,task.task_id,self.web.messages()[-1]['content'])
            self.assertTrue(fresh.slots['revalidation']['pending'])
            self.tool.planner.amap.route_for_pois.assert_not_called();self.tool.rail_executor.assert_not_called();self.tool.policy_resolver.resolve.assert_not_called()
            with self.web.store._connect() as db:self.assertEqual(old,tuple(tuple(r) for r in db.execute('SELECT * FROM personal_reminders')))
            self.action({'id':fresh.task_id,'version':fresh.version},'discard','end-'+str(i))

    def test_A16_operation_receipts_diagnostics_and_chinese_report_agree(self):
        self.create();draft=self.context()['confirmations'][0]
        accepted=self.action(draft,'revalidate','diagnostic-ok').json()
        report=self.web.client.get(f'/api/conversations/{self.web.identity}/jobs/{accepted["job_id"]}/diagnostics').json()
        rows=[r for r in report['timeline'] if r['stage']=='draft_action']
        self.assertTrue(any(r['details']['status']=='started' and '已开始' in r['description'] for r in rows))
        self.assertTrue(any(r['details']['status']=='completed' and '已完成' in r['description'] for r in rows))
        self.assertTrue(all(r['details'].get('target_id')==draft['id'] for r in rows))
        replay=self.action(draft,'revalidate','diagnostic-replay').json()
        report=self.web.client.get(f'/api/conversations/{self.web.identity}/jobs/{replay["job_id"]}/diagnostics').json()
        self.assertTrue(any(r['stage']=='draft_action' and r['details']['status']=='reused' and '没有重复执行' in r['description'] for r in report['timeline']))


    def test_A17_V04_restart_after_provider_return_reuses_persisted_receipt(self):
        import asyncio
        from infrastructure.amap_client import AmapClient
        from services.inbox_worker import InboxWorker
        class PowerLoss(BaseException):pass
        self.create();task=self.web.store.trips.current_drafts(self.web.event(''))[0];slots=deepcopy(task.slots)
        slots['candidate_trip']['sources']['h']['location']='114,30';slots['candidate_trip']['sources']['a']['location']='114.01,30.01'
        slots['candidate_trip']['plan']['days'][0]['legs']=[{'from_poi':'h','to_poi':'a','mode':'walking','duration_seconds':600,'distance_meters':1000,'queried_at':'2020-01-01T00:00:00+00:00'}]
        with self.web.store._connect() as db:db.execute('UPDATE travel_tasks SET slots_json=? WHERE task_id=?',(json.dumps(slots),task.task_id))
        response=Mock(status_code=200);response.json.return_value={'status':'1','infocode':'10000','route':{'paths':[{'distance':'1000','duration':'600','steps':[]}]}}
        getter=Mock(return_value=response);amap=AmapClient('fixture',http_get=getter,max_attempts=1);amap.checkpoint_store=self.web.store
        self.tool.planner.amap=amap;original=self.tool._preview_candidate
        self.tool._preview_candidate=Mock(side_effect=PowerLoss())
        draft={'id':task.task_id,'version':task.version}
        accepted=self.action(draft,'revalidate','crash-return',run=False).json()
        with self.assertRaises(PowerLoss):self.web.run_job()
        self.assertEqual(getter.call_count,1)
        with self.web.store._connect() as db:
            db.execute("UPDATE inbox_jobs SET lease_until='2000-01-01T00:00:00+00:00' WHERE id=?",(accepted['job_id'],))
            db.execute("UPDATE processed_events SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE event_id=(SELECT event_key FROM inbox_jobs WHERE id=?)",(accepted['job_id'],))
        self.tool._preview_candidate=original
        worker=InboxWorker(self.web.store,self.web.worker.adapter)
        self.assertTrue(asyncio.run(worker.run_once()))
        self.assertEqual(getter.call_count,1,'The returned provider receipt must not be submitted again')
        self.assertNotEqual(self.context()['confirmations'][0]['id'],draft['id'])
        self.assertEqual(self.action(draft,'revalidate','crash-return',run=False).json()['job_id'],accepted['job_id'])
        with self.web.store._connect() as db:self.assertEqual(db.execute('SELECT attempts FROM inbox_jobs WHERE id=?',(accepted['job_id'],)).fetchone()[0],2)

    def test_A15_multitarget_clarification_then_read_then_edit_preserves_other_target(self):
        from core.intent_ir import IntentIR,IntentOperation
        from test_trips import SPEC
        self.create();first=self.web.store.trips.current_drafts(self.web.event(''))[0]
        self.models.completions.responses.extend([model_response({**SPEC,'start_date_text':'2030年10月1日'}),model_response(plan_value())])
        self.create('second-target');other=next(t for t in self.web.store.trips.current_drafts(self.web.event('')) if t.task_id!=first.task_id)
        self.patch({'budget':{'scope':'food','value':'70','source':'餐饮预算70'}},'这些方案餐饮预算70','ambiguous')
        text='选择'+first.slots['trip_id']+'，人民币每人每餐'
        self.patch({'budget':{'scope':'food','unit_text':'人民币每人每餐','source':'人民币每人每餐'}},text,'select-first')
        def current():return next(t for t in self.web.store.trips.current_drafts(self.web.event('')) if t.task_id==first.task_id)
        selected=current();self.assertEqual(selected.slots['candidate_trip']['spec']['budgets']['food']['value'],'70')
        reference=next(c['reference_message_id'] for c in self.context()['confirmations'] if c['id']==first.task_id)
        text='只问这些餐馆价格，不要修改'
        self.compiler.try_compile.return_value=IntentIR(1,'read',(IntentOperation('trip','check_prices',{'requested_checks':[{'scope':'food','source':text}]}),),source_spans=(text,))
        response=self.web.client.post(f'/api/conversations/{self.web.identity}/messages',headers=self.web.headers,
            json={'content':text,'client_request_id':'read-selected','quoted_message_id':reference});self.assertEqual(response.status_code,202);self.web.run_job()
        self.assertEqual(current().slots,selected.slots)
        text='只修改'+first.slots['trip_id']+'的餐饮预算80以内'
        self.patch({'budget':{'scope':'food','value':'80以内','source':'餐饮预算80以内'}},text,'continue-edit')
        self.assertEqual(current().slots['candidate_trip']['spec']['budgets']['food']['value'],'80以内')
        after=next(t for t in self.web.store.trips.current_drafts(self.web.event('')) if t.task_id==other.task_id)
        self.assertEqual((after.version,after.slots),(other.version,other.slots))


class BoundaryAuditTests(unittest.TestCase):
    def test_partial_city_window_survives_until_duration_and_date_are_known(self):
        from tools.trip.constraints import apply_city_windows
        spec={'day_count':0,'start_date':''};text='第一天早上到测试市'
        apply_city_windows(spec,{'arrival':{'day_index':1,'time_text':'早上','source':text}},text)
        self.assertEqual(spec['city_windows']['arrival']['day_index'],1)
        self.assertNotIn('planning_window',spec)
        spec.update(day_count=3,start_date='2030-10-01');apply_city_windows(spec)
        self.assertEqual(spec['planning_window']['start_window'],(6,10))

    def test_A03_entity_resolution_keeps_cities_entrances_and_ambiguous_places_separate(self):
        from infrastructure.place_resolution import bind_visit_entities
        p={'main':{'id':'main','name':'云泽湖景区','cityname':'甲市','type':'风景名胜;公园','address':'湖路1号'},
           'other-city':{'id':'other-city','name':'云泽湖景区','cityname':'乙市','type':'风景名胜;公园'},
           'gate':{'id':'gate','name':'云泽湖景区-南门','cityname':'甲市','type':'风景名胜'},
           'station':{'id':'station','name':'云泽湖','cityname':'甲市','type':'交通设施;地铁站'}}
        self.assertEqual(bind_visit_entities(['甲市云泽湖'],p,'甲市')['甲市云泽湖']['poi_id'],'main')
        self.assertEqual(bind_visit_entities(['云泽'],p,'甲市')['云泽']['status'],'not_found')
        p['duplicate']={**p['main'],'id':'duplicate','address':'湖路2号'}
        self.assertEqual(bind_visit_entities(['云泽湖景区'],p,'甲市')['云泽湖景区']['status'],'ambiguous')
        qualified=bind_visit_entities(['云泽湖景区（湖路1号）'],p,'甲市')['云泽湖景区（湖路1号）']
        self.assertEqual(qualified['poi_id'],'main')

    def test_A11_negated_ranges_do_not_invert_conflicting_bounds(self):
        from tools.trip.constraints import budget_bounds
        for text,expected in [('不能低于70元',(70,None,True,True)),('不得超过70元',(None,70,True,True)),
                ('高于70且低于90',(70,90,False,False)),('70元以上至90元以下',(70,90,True,True))]:
            b=budget_bounds(text);self.assertEqual(tuple(b[k] for k in ('lower','upper','lower_inclusive','upper_inclusive')),expected)
        with self.assertRaises(ValueError):budget_bounds('高于90且低于70')
        self.assertEqual(budget_bounds('70元左右')['kind'],'approximate')

    def test_V02_incomparable_units_are_not_treated_as_over_budget(self):
        from tools.trip.planner import apply_recommendations
        for unit in ('人民币每人每天','人民币两人总额','英镑每人每餐','AUD每人每餐'):
            text='预算70 '+unit
            spec,_=apply_constraints({'restaurant_requested':True}, {'budget':{'scope':'food','value':'70','source':text,'unit_text':unit}},text)
            plan={'days':[{'day_index':1,'date':'2030-10-01','activities':[{'poi_id':'a','period':'上午','reason':'文化参观'}]}]}
            places={'a':{'id':'a','name':'展馆','type':'博物馆'},'r':{'id':'r','name':'餐馆','cost':'60','type':'餐饮服务;餐厅'}}
            result=apply_recommendations(spec,plan,places)
            self.assertTrue(result['dining_options'])
            self.assertTrue(all(m['price_check_status']=='incomparable_budget' and not m['candidates'] and '未自动换算' in m['note'] for m in result['dining_options']),unit)

    def test_A13_same_name_branches_do_not_share_prices_or_timestamp(self):
        trip={'spec':{},'plan':{'restaurant_ids':['known','unknown']},'sources':{
            'known':{'id':'known','name':'甲餐馆（甲店）','address':'甲路','cost':'60','queried_at':'2030-10-01T08:00:00+00:00'},
            'unknown':{'id':'unknown','name':'甲餐馆（乙店）','address':'乙路'}}}
        answer=TripTool.price_check_text(trip,[{'scope':'food','names':['甲餐馆（乙店）','丙餐馆']}])
        self.assertIn('价格待确认',answer);self.assertNotIn('约60元',answer);self.assertIn('丙餐馆',answer);self.assertIn('未找到',answer)
        self.assertIn('资料时间未提供',answer)
