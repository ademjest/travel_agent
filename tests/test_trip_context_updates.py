from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from agents.intent_compiler import IntentCompiler
from agents.trip_planner import TripPlanner, TripPlanError
from core.chat_transport import ChatEvent
from core.tasks import TaskUpdate
from core.trip_updates import apply_constraints, date_range
from infrastructure.amap_client import AmapError
from infrastructure.memory_store import MemoryStore
from infrastructure.preference_repository import PreferenceConflict
from infrastructure.task_repository import TaskConflict
from services.preference_service import PreferenceService
from services.semantic_task_service import SemanticTaskService
from services.trip_service import TripService
from test_trips import PLACES, SPEC, model_response, plan_value
from test_travel_agent import FakeClient


MESSAGE = '我喜欢轻松一点的行程，然后不太喜欢打车，尽量步行或者公交地铁出行'
CHANGES = {'pace': {'value': 'relaxed', 'source': '轻松一点'},
           'transport': {'preferred': ['walking','transit'], 'discouraged': ['taxi'],
                         'source': '不太喜欢打车，尽量步行或者公交地铁出行'}}


def intent(changes=None, **extra):
    return {'schema_version':1,'action':'update','operations':[{'domain':'trip','operation':'update_constraints',
        'target_ref':'current_trip','changes':deepcopy(CHANGES if changes is None else changes),**extra}],
        'source_spans':[c['source'] for c in (CHANGES if changes is None else changes).values()],
        'requires_confirmation':True}


class ContextUpdateTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = MemoryStore(Path(temp.name)/'test.db')
        self.now = datetime.now(timezone.utc)
        self.spec = {**SPEC,'start_date':'2030-10-01','end_date':'2030-10-03','day_count':3,
            'gentle':False,'source_context':'研究武汉三日游攻略；引用#1','document_ids':['12'],
            'preference_version':0,'applied_preferences':{}}
        self.amap = Mock()
        self.amap.search_places.return_value = list(PLACES.values())
        self.amap.route_for_pois.return_value = {'duration_seconds':600,'distance_meters':800}
        self.planner = TripPlanner(FakeClient([model_response(plan_value())]),'test',self.amap)
        self.service = TripService(self.store,self.planner,clock=lambda:self.now)
        self.saved = self.save('a')

    def event(self, text, key='change', **kwargs):
        return ChatEvent('web','group',key,'conversation','owner',text,occurred_at=self.now,**kwargs)

    def save(self, key, city='武汉', owner='owner', scope='conversation'):
        e = replace(self.event('创建行程',key),sender_id=owner,scope_id=scope)
        trip = {'trip_id':self.store.trips.new_id(e.event_key),'title':city+'三日行程','status':'active',
                'spec':{**deepcopy(self.spec),'destination':city},
                'plan':self.planner.validate_plan(plan_value(),self.spec,PLACES),'sources':deepcopy(PLACES)}
        self.store.trips.commit(e,self.store.begin_event(e.event_key),
            TaskUpdate('itinerary','completed','创建行程',{'trip_id':trip['trip_id']}),'created',trip=trip)
        return self.store.trips.get(e,trip['trip_id'])

    def dispatch(self, value=None, text=MESSAGE, key='change', compiler=None):
        e = self.event(text,key)
        compiler = compiler or IntentCompiler(FakeClient([model_response(value or intent())]),'test')
        service = SemanticTaskService(self.store,compiler,self.service,clock=lambda:self.now)
        return service.handle(e,self.store.begin_event(e.event_key))

    def confirm(self, key='confirm'):
        e = self.event('确认行程修改',key)
        return self.service.handle(e,self.store.begin_event(e.event_key))

    def current(self):
        return self.store.trips.get(self.event(''),self.saved['trip_id'])

    def test_original_sentence_previews_then_commits_same_trip_and_preserves_context(self):
        client = FakeClient([model_response(intent())])
        reply = self.dispatch(compiler=IntentCompiler(client,'test'))
        self.assertIn('保留原城市、日期与天数',reply)
        self.assertNotIn('起点和终点',reply)
        self.assertEqual(self.current()['version'],1)
        ctx = json.loads(json.loads(client.completions.requests[0]['messages'][1]['content'])['context'])
        self.assertEqual(ctx['current_trip']['destination'],'武汉')
        self.assertIn('研究武汉',ctx['current_trip']['research_summary'])
        self.assertIn('已应用',self.confirm())
        trip = self.current()
        self.assertEqual(trip['version'],2)
        for field in ('destination','start_date','end_date','day_count','must_visit','source_context','document_ids','preference_version'):
            self.assertEqual(trip['spec'][field],self.spec[field])
        self.assertTrue(trip['spec']['gentle'])
        self.assertEqual(trip['spec']['transport_preferences']['discouraged'],['taxi'])
        self.assertEqual(self.store.preferences.snapshot(self.event(''))['values'],{})
        with self.store._connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM travel_tasks WHERE task_type IN ('transit','walking')").fetchone()[0],0)

    def test_mixed_hotel_budget_clarification_keeps_all_changes_and_target(self):
        text = MESSAGE + '，然后酒店要舒适一点但不要太贵，价格在200-300区间'
        changes = {**deepcopy(CHANGES),
            'accommodation':{'value':'酒店要舒适一点但不要太贵','requested':True,'source':'酒店要舒适一点但不要太贵'},
            'budget':{'value':'200-300','scope':'accommodation','source':'价格在200-300区间'}}
        first = intent(changes,question='200-300 的币种和计价单位是什么？')
        first.update(action='clarify',missing_fields=['budget_unit'])
        reply = self.dispatch(first,text)
        self.assertIn('200-300 的币种和计价单位',reply)
        self.assertIn('酒店要舒适一点',reply)
        self.assertEqual(self.current()['version'],1)
        # Even after another trip is saved, the answer belongs to the original trip.
        self.save('another-trip','南京')
        hotel = {'id':'hotel','name':'测试舒适酒店','address':'测试路','type':'住宿服务'}
        self.amap.search_places.return_value = [*PLACES.values(),hotel]
        self.planner.client.completions.responses = [model_response({**plan_value(),'hotel_ids':['hotel']})]
        changes['budget']['unit_text'] = '人民币每间每晚'
        reply = self.dispatch(intent(changes),'人民币每间每晚','budget-answer')
        self.assertIn('确认行程修改',reply)
        self.assertIn('住宿预算目标：200-300；人民币每间每晚',reply)
        self.assertIn('未核实实时价格',reply)
        self.confirm()
        trip = self.current()
        self.assertEqual(trip['version'],2)
        self.assertTrue(trip['spec']['gentle'])
        self.assertTrue(trip['spec']['hotel_requested'])
        self.assertEqual(trip['plan']['hotel_ids'],['hotel'])
        self.assertEqual(trip['spec']['budgets']['accommodation']['unit_text'],'人民币每间每晚')
        for field in ('destination','start_date','day_count','must_visit','source_context'):
            self.assertEqual(trip['spec'][field],self.spec[field])
        self.assertEqual(self.store.preferences.snapshot(self.event(''))['values'],{})

    def test_budget_clarification_rejects_stale_trip(self):
        changes = {'budget':{'value':'200-300','scope':'accommodation','source':'200-300'}}
        first = intent(changes)
        first.update(action='clarify',missing_fields=['budget_unit'])
        self.dispatch(first,'酒店预算200-300')
        with self.store._connect() as db:
            db.execute('UPDATE trips SET version=version+1')
        changes['budget']['unit_text'] = '人民币每间每晚'
        reply = self.dispatch(intent(changes),'人民币每间每晚','answer')
        self.assertIn('已经变化',reply)

    def mixed_draft(self):
        text = MESSAGE + '；酒店舒适一点；200-300元，人民币每间每晚'
        changes = {**deepcopy(CHANGES),
            'accommodation':{'value':'酒店舒适一点','source':'酒店舒适一点'},
            'budget':{'value':'200-300元','source':'200-300元','scope':'accommodation','unit_text':'人民币每间每晚'}}
        self.dispatch(intent(changes),text)
        return self.store.trips.draft(self.event(''),self.current())

    def test_expired_draft_keeps_budget_and_transport_when_adding_hotel_brand(self):
        previous = self.mixed_draft()
        with self.store._connect() as db:
            db.execute("UPDATE travel_tasks SET expires_at='2000-01-01' WHERE task_id=?",(previous['task'].task_id,))
        self.assertIn('没有仍然有效',self.confirm('expired-confirm'))
        self.planner.client.completions.responses = [model_response(plan_value())]
        changes = {'accommodation':{'value':'偏好连锁酒店','source':'偏好连锁酒店'}}
        client = FakeClient([model_response(intent(changes))])
        self.dispatch(text='偏好连锁酒店',key='brand',compiler=IntentCompiler(client,'test'))
        model_context = json.loads(json.loads(client.completions.requests[0]['messages'][1]['content'])['context'])
        self.assertTrue(model_context['current_trip']['gentle'])
        self.assertEqual(model_context['current_trip']['budgets']['accommodation']['value'],'200-300元')
        updated = self.store.trips.draft(self.event(''),self.current())
        self.assertNotEqual(updated['task'].task_id,previous['task'].task_id)
        self.assertTrue(updated['candidate']['spec']['gentle'])
        self.assertEqual(updated['candidate']['spec']['transport_preferences']['preferred'],['walking','transit'])
        self.assertEqual(updated['candidate']['spec']['budgets']['accommodation']['value'],'200-300元')
        self.assertEqual(self.current()['version'],1)
        self.confirm()
        self.assertEqual(self.current()['version'],2)

    def test_legacy_superseded_constraints_are_recovered_when_regenerating(self):
        previous = self.mixed_draft()
        with self.store._connect() as db:
            db.execute("UPDATE travel_tasks SET slots_json=json_remove(slots_json,'$.draft_context_complete') WHERE task_id=?",(previous['task'].task_id,))
        text='偏好连锁酒店'
        candidate=deepcopy(self.saved)
        candidate['version']=2
        candidate['spec'],_=apply_constraints(candidate['spec'],{'accommodation':{'value':text,'source':text}},text)
        e=self.event(text,'legacy')
        self.store.trips.commit(e,self.store.begin_event(e.event_key),TaskUpdate('itinerary','collecting',text,
            {'trip_id':self.saved['trip_id'],'candidate_trip':candidate,'expected_trip_version':1,'trip_already_saved':False},
            ('confirmation',)),'legacy preview')
        with self.store._connect() as db:
            db.execute("UPDATE travel_tasks SET expires_at='2000-01-01'")
        self.planner.client.completions.responses=[model_response(plan_value())]
        e=self.event('重新生成行程预览','regenerate')
        reply=self.service.handle(e,self.store.begin_event(e.event_key))
        self.assertIn('200-300元',reply)
        spec=self.store.trips.draft(self.event(''),self.current())['candidate']['spec']
        self.assertTrue(spec['gentle'])
        self.assertIn(text,spec['accommodation_text'])
        self.assertIn('酒店舒适一点',spec['accommodation_text'])
        self.assertEqual(spec['transport_preferences']['discouraged'],['taxi'])

    def test_draft_changed_during_planning_cannot_be_overwritten(self):
        previous=self.mixed_draft()
        def changed(*args,**kwargs):
            with self.store._connect() as db:
                db.execute('UPDATE travel_tasks SET version=version+1 WHERE task_id=?',(previous['task'].task_id,))
            return self.planner.validate_plan(plan_value(),args[0],args[2])
        self.planner.update_recommendations=Mock(side_effect=changed)
        with self.assertRaisesRegex(TaskConflict,'草稿已被另一条消息更新'):
            self.dispatch(intent({'accommodation':{'value':'偏好连锁酒店','source':'偏好连锁酒店'}}),'偏好连锁酒店','race')
        self.assertEqual(self.current()['version'],1)

    def test_cancelled_draft_is_not_reintroduced_into_new_request(self):
        self.mixed_draft()
        e=self.event('取消当前任务','cancel')
        self.store.tasks.command(e,self.store.begin_event(e.event_key))
        self.planner.client.completions.responses=[model_response(plan_value())]
        self.dispatch(intent({'accommodation':{'value':'偏好连锁酒店','source':'偏好连锁酒店'}}),'偏好连锁酒店','fresh')
        spec=self.store.trips.draft(self.event(''),self.current())['candidate']['spec']
        self.assertFalse(spec['gentle'])
        self.assertNotIn('budgets',spec)

    def test_budget_without_units_is_clarified_even_if_model_marks_update(self):
        changes = {'budget':{'value':'200-300','scope':'accommodation','source':'200-300'}}
        reply = self.dispatch(intent(changes),'酒店预算200-300')
        self.assertIn('预算币种与计价单位',reply)
        self.assertEqual(self.current()['version'],1)

    def test_free_form_travel_requirement_reaches_planner(self):
        changes = {'requirements':{'value':'午后留出一段午休时间','source':'午后留出一段午休时间'}}
        self.dispatch(intent(changes),'午后留出一段午休时间')
        request = json.loads(self.planner.client.completions.requests[0]['messages'][1]['content'])
        self.assertIn('午后留出一段午休时间',request['requirements']['preferences'])
        self.assertEqual(self.current()['version'],1)

    def test_expired_tasks_and_restart_keep_trip_context(self):
        with self.store._connect() as db:
            db.execute("UPDATE travel_tasks SET expires_at='2000-01-01'")
        self.store = MemoryStore(self.store.database_path)
        self.service = TripService(self.store,self.planner)
        self.assertIn('保留原城市',self.dispatch())

    def test_foreign_owner_and_conversation_are_not_candidates(self):
        self.save('foreign',owner='other')
        self.save('other-scope',scope='another')
        self.assertIn('保留原城市',self.dispatch())

    def test_unknown_reply_does_not_choose_arbitrary_trip(self):
        self.save('b','南京')
        e = self.event(MESSAGE,reply_to_id='unknown')
        compiler = IntentCompiler(FakeClient([model_response(intent())]),'test')
        reply = SemanticTaskService(self.store,compiler,self.service).handle(e,self.store.begin_event(e.event_key))
        self.assertIn('要调整哪一份',reply)

    def test_reply_to_expired_planning_task_still_resolves_saved_trip(self):
        self.save('b','南京')
        with self.store._connect() as db:
            db.execute("UPDATE travel_tasks SET expires_at='2000-01-01'")
            db.execute('INSERT INTO outbound_message_links VALUES (?,?,?,?,?)',
                ('web',self.event('').storage_scope_id,'old-plan-reply',self.event('','a').event_key,'owner'))
        e = self.event(MESSAGE,reply_to_id='old-plan-reply')
        compiler = IntentCompiler(FakeClient([model_response(intent())]),'test')
        reply = SemanticTaskService(self.store,compiler,self.service).handle(e,self.store.begin_event(e.event_key))
        self.assertIn(self.saved['trip_id'],reply)
        self.assertIn('保留原城市',reply)

    def test_no_trip_asks_for_plan_without_creating_transit_task(self):
        with self.store._connect() as db:
            db.execute("UPDATE trips SET status='cancelled'")
        self.assertIn('还没有已保存行程',self.dispatch())

    def test_linked_reminder_change_invalidates_constraint_preview(self):
        self.dispatch()
        e = self.event('提醒预约','reminder')
        self.store.reminders.commit(e,self.store.begin_event(e.event_key),
            TaskUpdate('reminder','completed',e.content,{}),'created',action='create',reminder={
                'reminder_id':'M-linked','title':'预约湖北省博物馆','scheduled_at_utc':'2030-09-27T00:00:00+00:00',
                'source':{'trip_id':self.saved['trip_id'],'entity':'湖北省博物馆','visit_date':'2030-10-01'}})
        with self.assertRaisesRegex(TripPlanError,'关联提醒集合'):
            self.confirm()
        self.assertEqual(self.current()['version'],1)

    def test_multiple_trips_ask_only_target_then_resume(self):
        other = self.save('b','南京')
        self.assertIn('要调整哪一份',self.dispatch())
        text = self.saved['trip_id']
        self.assertIn('保留原城市',self.dispatch(text=text,key='selection'))
        self.confirm()
        self.assertEqual(self.current()['version'],2)
        self.assertEqual(self.store.trips.get(self.event(''),other['trip_id'])['version'],1)
        self.assertFalse(any(t.task_type=='semantic' and t.status=='collecting' for t in
            self.store.tasks.recent('web',self.event('').storage_scope_id,'owner')))

    def test_explicit_invalid_target_does_not_modify_unique_owned_trip(self):
        self.assertIn('没有找到',self.dispatch(text=MESSAGE+' TR-000000000000'))
        self.assertEqual(self.current()['version'],1)

    def test_concurrent_change_during_compilation_is_rejected(self):
        class ConcurrentCompiler:
            def try_compile(inner,text,**kwargs):
                with self.store._connect() as db:
                    db.execute('UPDATE trips SET version=version+1')
                return IntentCompiler(FakeClient([model_response(intent())]),'test').try_compile(text)
        with self.assertRaisesRegex(TripPlanError,'已经变化'):
            self.dispatch(compiler=ConcurrentCompiler())

    def test_stale_confirmation_is_rejected(self):
        self.dispatch()
        with self.store._connect() as db:
            db.execute('UPDATE trips SET version=version+1')
        with self.assertRaisesRegex(TripPlanError,'旧预览'):
            self.confirm()

    def test_long_term_only_save_does_not_change_trip(self):
        e = self.event('记住，以后默认轻松节奏','pref')
        PreferenceService(self.store.preferences).handle(e,self.store.begin_event(e.event_key))
        self.assertEqual(self.current()['version'],1)
        self.assertFalse(self.current()['spec']['gentle'])

    def test_compound_profile_and_trip_commit_together_after_confirmation(self):
        changes = {'pace':{'value':'relaxed','source':'轻松'}}
        text = '记住，以后默认轻松节奏；也请调整当前行程'
        reply = self.dispatch(intent(changes,save_preference_text='记住，以后默认轻松节奏'),text)
        self.assertIn('确认时同时保存长期偏好',reply)
        self.assertEqual(self.store.preferences.snapshot(self.event(''))['values'],{})
        self.confirm()
        self.assertEqual(self.current()['version'],2)
        self.assertEqual(self.store.preferences.snapshot(self.event(''))['values'],{'pace':'轻松'})

    def test_profile_conflict_rolls_back_trip_confirmation(self):
        changes = {'pace':{'value':'relaxed','source':'轻松'}}
        self.dispatch(intent(changes,save_preference_text='记住，以后默认轻松节奏'),
            '记住，以后默认轻松节奏；也请调整当前行程')
        self.store.preferences.update(self.event(''),0,{'pace':'紧凑'})
        with self.assertRaises(PreferenceConflict):
            self.confirm()
        self.assertEqual(self.current()['version'],1)
        self.assertEqual(self.store.preferences.snapshot(self.event(''))['values'],{'pace':'紧凑'})

    def test_real_route_is_not_hijacked(self):
        ir = {'schema_version':1,'action':'read','operations':[{'domain':'route','operation':'transit',
            'origin':'黄鹤楼','destination':'武汉站'}],'source_spans':['黄鹤楼','武汉站']}
        self.assertIsNone(self.dispatch(ir,'从黄鹤楼到武汉站怎么坐地铁'))

    def test_compiler_failure_does_not_fall_through_to_transit(self):
        compiler = Mock()
        compiler.try_compile.return_value = None
        self.assertIn('未能可靠识别',self.dispatch(compiler=compiler))

    def test_only_identical_wrong_transit_task_is_superseded(self):
        for i, text in enumerate((MESSAGE,'从黄鹤楼到武汉站坐公交')):
            e = self.event(text,'transit'+str(i))
            self.store.tasks.prepare_result(e,self.store.begin_event(e.event_key),
                TaskUpdate('transit','collecting',text,{},('origin','destination')),'question')
        self.dispatch()
        with self.store._connect() as db:
            rows = dict(db.execute("SELECT initial_request,status FROM travel_tasks WHERE task_type='transit'"))
        self.assertEqual(rows[MESSAGE],'superseded')
        self.assertEqual(rows['从黄鹤楼到武汉站坐公交'],'collecting')


class ConstraintAndDateTests(unittest.TestCase):
    now = datetime(2026,9,20,tzinfo=timezone.utc)

    def extract(self,text,value,previous=None):
        return TripPlanner(FakeClient([model_response(value)]),'test',Mock()).extract(text,previous or {},self.now)

    def test_empty_extraction_preserves_existing_fields(self):
        previous = {**SPEC,'day_count':3,'start_date':'2026-10-01','budget_text':'200元','transport_text':'公共交通'}
        result = self.extract('继续',{'destination':'','duration_text':'','start_date_text':'','preferences':[],
            'must_visit':[],'budget_text':'','transport_text':''},previous)
        for k in previous:
            self.assertEqual(result[k],previous[k])

    def test_explicit_clear_and_forged_clear(self):
        previous = {**SPEC,'budget_text':'200元'}
        self.assertEqual(self.extract('取消预算限制',{'clear_fields':{'budget_text':'取消预算限制'}},previous)['budget_text'],'')
        with self.assertRaises(TripPlanError):
            self.extract('继续',{'clear_fields':{'budget_text':'取消预算限制'}},previous)
        self.assertEqual(self.extract('取消必去要求',{'clear_fields':{'must_visit':'取消必去要求'}},previous)['must_visit'],[])

    def test_chinese_end_date_and_short_end_date(self):
        for text in ('10月1日到10月三日','10月1日到3日','2026-10-01至2026-10-03'):
            result = self.extract(text,{'start_date_text':'','duration_text':''})
            self.assertEqual((result['start_date'],result['end_date'],result['day_count']),('2026-10-01','2026-10-03',3))

    def test_date_conflict_and_invalid_ranges(self):
        with self.assertRaisesRegex(TripPlanError,'不一致'):
            self.extract('10月1日到10月3日，四天',{'duration_text':'四天'})
        with self.assertRaisesRegex(TripPlanError,'不一致'):
            self.extract('10月1日到10月3日，四天',{})
        for text in ('10月3日到10月1日','12月31日到1月2日','2026年2月30日到3月2日'):
            with self.assertRaises(ValueError):
                date_range(text,self.now)

    def test_explicit_start_year_is_inherited_by_end_and_cross_year_requires_year(self):
        self.assertEqual(date_range('2030年10月1日到10月3日',self.now)['end'],'2030-10-03')
        self.assertEqual(date_range('2026年12月31日到2027年1月2日',self.now)['days'],3)

    def test_typed_constraints_do_not_mutate_original(self):
        previous = {**SPEC,'transport_preferences':{'forbidden':['driving']}}
        before = deepcopy(previous)
        spec,_ = apply_constraints(previous,CHANGES,MESSAGE)
        self.assertEqual(previous,before)
        self.assertEqual(spec['transport_preferences']['forbidden'],['driving'])
        self.assertEqual(spec['transport_preferences']['discouraged'],['taxi'])
        spec,_ = apply_constraints(spec,{'transport':{'discouraged':['walking'],'source':'少走路'}},'少走路')
        self.assertEqual(spec['transport_preferences']['discouraged'],['taxi','walking'])
        with self.assertRaises(ValueError):
            apply_constraints(previous,CHANGES,'只是闲聊')

    def test_budget_scopes_remain_separate_and_fabricated_units_are_rejected(self):
        previous = {'budgets':{'trip':{'value':'5000元','unit_text':'每人全程'}}}
        changes = {'budget':{'value':'200-300','scope':'accommodation','source':'200-300'}}
        result,_ = apply_constraints(previous,changes,'酒店200-300')
        self.assertEqual(result['budgets']['trip'],previous['budgets']['trip'])
        self.assertNotIn('unit_text',result['budgets']['accommodation'])
        changes['budget']['unit_text']='人民币每间每晚'
        with self.assertRaises(ValueError):
            apply_constraints(previous,changes,'酒店200-300')

    def test_unmentioned_unsupported_field_is_not_silently_accepted(self):
        with self.assertRaisesRegex(ValueError,'无法执行的修改字段'):
            apply_constraints({}, {'arbitrary_database_write':{'source':'请帮我'}},'请帮我')

    def test_mixed_route_short_walk_long_transit_and_failure(self):
        spec,_ = apply_constraints({'destination':'武汉'},CHANGES,MESSAGE)
        for walking_time, fail, expected in ((600,False,'walking'),(3600,False,'transit'),(3600,True,'transit')):
            amap = Mock()
            def route(*args,mode,**kwargs):
                if mode=='transit' and fail:
                    raise AmapError('公交暂无证据')
                return {'duration_seconds':walking_time if mode=='walking' else 900}
            amap.route_for_pois.side_effect = route
            planner = TripPlanner(None,'test',amap)
            plan = plan_value((('h','a'),))
            planner.add_route_evidence(plan,spec,PLACES)
            leg = plan['days'][0]['legs'][0]
            self.assertEqual(leg['mode'],expected)
            self.assertEqual(bool(leg.get('error')),fail)
            self.assertFalse(any(c.kwargs['mode'] in ('taxi','driving') for c in amap.route_for_pois.call_args_list))

    def test_clear_modes_clears_stale_legs(self):
        plan = plan_value((('h','a'),))
        plan['days'][0]['legs']=[{'mode':'driving'}]
        TripPlanner(None,'test',Mock()).add_route_evidence(plan,{'transport_preferences':{'preferred':[]}},PLACES)
        self.assertEqual(plan['days'][0]['legs'],[])
