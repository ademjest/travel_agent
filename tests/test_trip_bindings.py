"""Multi-plan identity, immutable copy and continuation through the real Web queue."""
from copy import deepcopy
import unittest

import test_message_references as fixtures
from test_trips import SPEC, plan_value, model_response
from core.intent_ir import IntentOperation, parse_intent_ir


class TripBindingFlowTests(unittest.TestCase):
    setUp=fixtures.MessageReferenceFlowTests.setUp
    submit=fixtures.MessageReferenceFlowTests.submit
    context=fixtures.MessageReferenceFlowTests.context
    def create(self, key='a'):
        if self.models.completions.requests:
            self.models.completions.responses.extend([
                model_response({**SPEC,'start_date_text':'2030年10月1日'}),model_response(plan_value())])
        text='规划2030年10月1日武汉三天行程，带老人，必去湖北省博物馆'
        return self.submit(text,key,IntentOperation('trip','create',{'request_text':text}))

    def copy(self, quote, key='copy', changes=None, text=None):
        text=text or '基于引用的历史方案另建独立方案，餐饮人均不超过60元，保留日期、酒店和景点'
        if changes is None:changes={'budget':{'scope':'food','value':'不超过60元','unit_text':'人均','source':'餐饮人均不超过60元'}}
        return self.submit(text,key,IntentOperation('trip','create',{
            'request_text':text,'basis':'reference','changes':changes}),quote)

    def drafts(self):
        return {t.slots['trip_id']:t for t in self.web.store.trips.current_drafts(self.web.event(''))}

    def test_historical_copy_uses_snapshot_and_independent_identity(self):
        first=self.create();a=next(iter(self.drafts().values()));before=deepcopy(a)
        text='住宿预算改为每间每晚400元'
        self.submit(text,'edit-a',IntentOperation('trip','update_constraints',{'changes':{
            'budget':{'scope':'accommodation','value':'400元','unit_text':'每间每晚','source':text}}}),first['id'])
        latest=deepcopy(self.drafts()[a.slots['trip_id']])
        result=self.copy(first['id'])
        self.assertIn('创建独立方案',result['content']);self.assertIn('原提醒未复制',result['content'])
        drafts=self.drafts();self.assertEqual(len(drafts),2)
        b=next(t for tid,t in drafts.items() if tid!=a.slots['trip_id'])
        self.assertNotEqual(a.task_id,b.task_id)
        for field in ('start_date','end_date','day_count','must_visit'):
            self.assertEqual(b.slots['candidate_trip']['spec'].get(field),before.slots['candidate_trip']['spec'].get(field))
        self.assertEqual(b.slots['candidate_trip']['plan']['days'],before.slots['candidate_trip']['plan']['days'])
        self.assertNotIn('accommodation',b.slots['candidate_trip']['spec'].get('budgets',{}))
        self.assertEqual(self.drafts()[a.slots['trip_id']],latest)
        self.assertEqual(b.slots['trip_binding']['source']['draft']['version'],before.version)
        self.assertEqual(len(self.models.completions.requests),2)

    def test_fresh_creation_keeps_unsaved_a(self):
        self.create();a=deepcopy(next(iter(self.drafts().values())))
        self.create('b');self.assertEqual(len(self.drafts()),2)
        self.assertEqual(self.drafts()[a.slots['trip_id']],a)

    def test_missing_reference_never_defaults_to_unique_trip(self):
        self.create();before=deepcopy(self.drafts())
        text='只修改我引用的这份方案，餐饮人均不超过60元'
        reply=self.submit(text,'missing',IntentOperation('trip','update_constraints',{'changes':{
            'budget':{'scope':'food','value':'不超过60元','unit_text':'人均','source':'餐饮人均不超过60元'}}}))
        self.assertIn('没有可验证',reply['content']);self.assertEqual(self.drafts(),before)

    def test_reference_create_missing_source_keeps_mode_and_budget(self):
        quote=self.create();before=deepcopy(self.drafts())
        reply=self.copy(None);self.assertIn('作为基础',reply['content']);self.assertEqual(self.drafts(),before)
        # New explicit source resumes the missing-source operation with its collected cap.
        reply=self.copy(quote['id'],'answer',changes={},text='基于这份历史方案另建，餐饮人均不超过60元')
        self.assertIn('创建独立方案',reply['content'])

    def test_copy_without_changes_preserves_every_business_field(self):
        a=self.create();source=deepcopy(next(iter(self.drafts().values())))
        reply=self.copy(a['id'],changes={},text='基于引用的这版历史内容另建独立方案，保留全部要求')
        self.assertIn('创建独立方案',reply['content'])
        b=next(t for tid,t in self.drafts().items() if tid!=source.slots['trip_id'])
        for key in ('plan','sources'):
            self.assertEqual(b.slots['candidate_trip'][key],source.slots['candidate_trip'][key])
        self.assertEqual(len(self.models.completions.requests),2)

    def test_new_reference_copy_does_not_absorb_pending_update(self):
        a=self.create();source=deepcopy(next(iter(self.drafts().values())))
        text='只修改我引用的方案，餐饮人均50元'
        self.submit(text,'unbound-update',IntentOperation('trip','update_constraints',{'changes':{
            'budget':{'scope':'food','value':'50元','unit_text':'人均','source':'餐饮人均50元'}}}))
        pending=next(t for t in self.web.store.tasks.recent('web','web:'+self.web.identity,'local-owner') if t.task_type=='semantic')
        reply=self.copy(a['id'],changes={},text='基于引用的这版方案另建独立方案，全部内容和预算保持不变')
        self.assertIn('创建独立方案',reply['content'])
        b=next(t for tid,t in self.drafts().items() if tid!=source.slots['trip_id'])
        self.assertEqual(b.slots['candidate_trip']['spec'].get('budgets'),source.slots['candidate_trip']['spec'].get('budgets'))
        self.assertEqual(self.web.store.tasks.owned(self.web.event(''),pending.task_id),pending)

    def test_new_explicit_target_is_not_swallowed_by_copy_question(self):
        a=self.create();b=self.create('b');before=deepcopy(self.drafts())
        text='基于引用的方案另建，餐饮人均60元'
        self.submit(text,'copy-question',IntentOperation('trip','create',{'request_text':text,'basis':'reference','changes':{
            'budget':{'scope':'food','value':'60元','unit_text':'人均','source':'餐饮人均60元'}}}),a['id'],action='clarify',missing=('budget_scope',))
        pending=next(t for t in self.web.store.tasks.recent('web','web:'+self.web.identity,'local-owner') if t.task_type=='semantic')
        text='只修改我引用的这份方案，餐饮人均70元'
        self.submit(text,'modify-b',IntentOperation('trip','update_constraints',{'target_ref':'message_reference','changes':{
            'budget':{'scope':'food','value':'70元','unit_text':'人均','source':'餐饮人均70元'}}}),b['id'])
        self.assertEqual(len(self.drafts()),2)
        self.assertEqual(self.drafts()[b['trip_presentation']['trip']['trip_id']].slots['candidate_trip']['spec']['budgets']['food']['value'],'70元')
        self.assertEqual(self.drafts()[a['trip_presentation']['trip']['trip_id']],before[a['trip_presentation']['trip']['trip_id']])
        self.assertEqual(self.web.store.tasks.owned(self.web.event(''),pending.task_id),pending)

    def test_explicit_reference_and_named_other_target_conflict(self):
        first=self.create();self.create('b');before=deepcopy(self.drafts())
        other=next(tid for tid in before if tid!=first['trip_presentation']['trip']['trip_id'])
        text=f'修改行程 {other} 的餐饮人均不超过60元'
        reply=self.submit(text,'conflict',IntentOperation('trip','update_constraints',{'changes':{
            'budget':{'scope':'food','value':'不超过60元','unit_text':'人均','source':'餐饮人均不超过60元'}}}),first['id'])
        self.assertIn('不同方案',reply['content']);self.assertEqual(self.drafts(),before)

    def test_same_operation_two_rounds_use_latest_working_copy(self):
        first=self.create()
        for key,scope,value in [('hotel','accommodation','400元'),('food','food','60元')]:
            text=('住宿预算每间每晚' if scope=='accommodation' else '餐饮预算人均')+value
            self.submit(text,key,IntentOperation('trip','update_constraints',{'target_ref':'message_reference','changes':{
                'budget':{'scope':scope,'value':value,'unit_text':'每间每晚' if scope=='accommodation' else '人均','source':text}}}),first['id'] if key=='hotel' else None)
        candidate=next(iter(self.drafts().values())).slots['candidate_trip']
        self.assertEqual(candidate['spec']['budgets']['accommodation']['value'],'400元')
        self.assertEqual(candidate['spec']['budgets']['food']['value'],'60元')

    def test_copy_expired_readable_draft_keeps_source_but_new_confirmation(self):
        first=self.create();a=next(iter(self.drafts().values()))
        with self.web.store._connect() as db:db.execute("UPDATE travel_tasks SET expires_at='2000-01-01'")
        self.copy(first['id']);self.assertEqual(len(self.drafts()),2)
        self.assertEqual(self.web.store.tasks.owned(self.web.event(''),a.task_id).version,a.version)

    def test_enqueued_edit_does_not_rebind_after_other_update(self):
        first=self.create();a=next(iter(self.drafts().values()))
        text='餐饮预算人均60元'
        op=IntentOperation('trip','update_constraints',{'changes':{
            'budget':{'scope':'food','value':'60元','unit_text':'人均','source':text}}})
        queued=self.web.client.post(f'/api/conversations/{self.web.identity}/messages',headers=self.web.headers,
            json={'content':text,'client_request_id':'queued','quoted_message_id':first['id']})
        self.assertEqual(queued.status_code,202)
        with self.web.store._connect() as db:db.execute('UPDATE travel_tasks SET version=version+1 WHERE task_id=?',(a.task_id,))
        from core.intent_ir import IntentIR
        self.compiler.try_compile.return_value=IntentIR(1,'update',(op,),source_spans=(text,))
        self.web.run_job()
        self.assertIn('历史版本',[m for m in self.web.messages() if m['role']=='assistant'][-1]['content'])
        self.assertNotIn('food',self.drafts()[a.slots['trip_id']].slots['candidate_trip']['spec'].get('budgets',{}))

    def test_create_contract_rejects_model_identity(self):
        text='基于引用另建独立方案'
        value={'schema_version':1,'action':'create','operations':[{'domain':'trip','operation':'create',
            'request_text':text,'basis':'reference','task_id':'forged'}],'source_spans':[text]}
        with self.assertRaises(ValueError):parse_intent_ir(value,text)

    def test_copy_meal_conflict_then_unquoted_answer_keeps_full_source(self):
        first=self.create()
        text='第二天午餐人均90元，第二天晚餐人均80元'
        quote=self.submit(text,'meals',IntentOperation('trip','update_constraints',{'changes':{'budgets':[
            {'scope':'food','value':'90元','source':'第二天午餐人均90元','unit_text':'人均','scopes':[{'day_index':2,'meal':'午餐'}]},
            {'scope':'food','value':'80元','source':'第二天晚餐人均80元','unit_text':'人均','scopes':[{'day_index':2,'meal':'晚餐'}]}]}}),first['id'])
        before=deepcopy(self.drafts());result=self.copy(quote['id'])
        self.assertIn('第2天午餐',result['content']);self.assertIn('第2天晚餐',result['content'])
        self.assertNotIn('想去哪个城市',result['content']);self.assertEqual(self.drafts(),before)
        text='所有餐次统一60元，清除单餐例外'
        answer=self.submit(text,'unify',IntentOperation('trip','update_constraints',{'changes':{
            'budget':{'scope':'food','value':'60元','source':text,'meal_policy':'replace_all','meal_policy_source':text}}}))
        self.assertIn('创建独立方案',answer['content'])
        b=next(t for tid,t in self.drafts().items() if tid not in before)
        self.assertFalse(b.slots['candidate_trip']['spec']['budgets']['food'].get('meals'))
        for tid,task in before.items():self.assertEqual(self.drafts()[tid],task)
        self.assertEqual(len(self.models.completions.requests),2)
        self.assertFalse(any(t.task_type=='semantic' and t.status=='collecting'
            for t in self.web.store.tasks.recent('web','web:'+self.web.identity,'local-owner')))

    def test_explicit_selection_switches_and_unquoted_followup_uses_b(self):
        a=self.create();self.create('b');before=deepcopy(self.drafts())
        for key,quote,value in [('select-a',a['id'],'60元'),('select-b',None,'70元'),('continue-b',None,'80元')]:
            target=next(tid for tid in before if tid!=a['trip_presentation']['trip']['trip_id'])
            text=(f'修改行程 {target}，' if key=='select-b' else '')+f'餐饮人均{value}'
            self.submit(text,key,IntentOperation('trip','update_constraints',{'changes':{
                'budget':{'scope':'food','value':value,'unit_text':'人均','source':f'餐饮人均{value}'}}}),quote)
            if key=='select-a':a_after=deepcopy(self.drafts()[a['trip_presentation']['trip']['trip_id']])
        self.assertEqual(self.drafts()[target].slots['candidate_trip']['spec']['budgets']['food']['value'],'80元')
        self.assertEqual(self.drafts()[a['trip_presentation']['trip']['trip_id']],a_after)

    def test_explicit_new_creation_bypasses_a_question(self):
        a=self.create();text='住宿预算200-400'
        self.submit(text,'ask-a',IntentOperation('trip','update_constraints',{'changes':{
            'budget':{'scope':'accommodation','value':'200-400','source':text}}}),a['id'],action='clarify',missing=('dining_style',))
        question=next(t for t in self.web.store.tasks.recent('web','web:'+self.web.identity,'local-owner') if t.task_type=='semantic')
        self.create('fresh-b');self.assertEqual(len(self.drafts()),2)
        self.assertEqual(self.web.store.tasks.owned(self.web.event(''),question.task_id),question)

    def test_copy_retry_is_idempotent_and_target_changes_conflict(self):
        a=self.create();self.copy(a['id']);before=deepcopy(self.drafts())
        text='基于引用的历史方案另建独立方案，餐饮人均不超过60元，保留日期、酒店和景点'
        body={'content':text,'client_request_id':'copy','quoted_message_id':a['id']}
        result=self.web.client.post(f'/api/conversations/{self.web.identity}/messages',headers=self.web.headers,json=body)
        self.assertEqual(result.status_code,202);self.assertEqual(self.drafts(),before)
        body['trip_continuation']=False
        self.assertEqual(self.web.client.post(f'/api/conversations/{self.web.identity}/messages',headers=self.web.headers,json=body).status_code,409)

    def test_cross_conversation_credential_is_rejected_before_enqueue(self):
        self.create();task=next(iter(self.drafts().values()))
        other=self.web.client.post('/api/conversations',headers=self.web.headers).json()['id']
        response=self.web.client.post(f'/api/conversations/{other}/messages',headers=self.web.headers,json={
            'content':'继续修改餐饮预算','client_request_id':'forged','trip_continuation':{'task_id':task.task_id,'version':task.version}})
        self.assertEqual(response.status_code,409)

    def test_save_copy_and_compare_do_not_call_models_or_save_a(self):
        a=self.create();before=deepcopy(self.drafts());self.copy(a['id'])
        b=next(t for tid,t in self.drafts().items() if tid not in before)
        c=next(c for c in self.context()['confirmations'] if c['id']==b.task_id)
        response=self.web.client.post(f'/api/conversations/{self.web.identity}/confirmations/{c["id"]}',headers=self.web.headers,
            json={'version':c['version'],'client_request_id':'save-copy'})
        self.assertEqual(response.status_code,202);self.web.run_job()
        saved=self.web.store.trips.list_for_owner(self.web.event(''))
        self.assertEqual(len(saved),1);self.assertEqual(saved[0]['trip_id'],b.slots['trip_id']);self.assertEqual(saved[0]['version'],1)
        self.assertEqual(saved[0]['spec'],b.slots['candidate_trip']['spec'])
        self.assertEqual(self.drafts(),before)
        self.assertEqual(len(self.models.completions.requests),2)
        self.assertEqual(self.web.store.trips.linked_reminders(self.web.event(''),saved[0]['trip_id']),())

    def test_matching_legacy_incomplete_copy_is_replaced_without_default_dates(self):
        a=self.create();source=deepcopy(next(iter(self.drafts().values())))
        text='基于这个历史内容，新建行程，餐饮人均不超过60元'
        self.models.completions.responses.append(model_response({'budget_text':'餐饮人均不超过60元'}))
        self.submit(text,'legacy-copy',IntentOperation('trip','create',{'request_text':text}),a['id'])
        with self.web.store._connect() as db:
            db.execute("UPDATE travel_tasks SET slots_json=json_remove(slots_json,'$.trip_binding') WHERE source_event_id LIKE '%:legacy-copy'")
            identity=db.execute("SELECT task_id FROM travel_tasks WHERE source_event_id LIKE '%:legacy-copy'").fetchone()[0]
        old=self.web.store.tasks.owned(self.web.event(''),identity)
        self.copy(a['id'],'copy-restored')
        replaced=self.web.store.tasks.owned(self.web.event(''),identity)
        self.assertEqual(replaced.status,'superseded');self.assertEqual(replaced.version,old.version+1)
        b=next(t for tid,t in self.drafts().items() if tid!=source.slots['trip_id'])
        self.assertEqual(b.slots['candidate_trip']['spec']['start_date'],source.slots['candidate_trip']['spec']['start_date'])
        self.assertEqual(b.slots['trip_binding']['replaces_task']['task_id'],identity)
        self.assertEqual(self.drafts()[source.slots['trip_id']],source)

    def test_ending_focus_preserves_drafts_and_does_not_auto_resume(self):
        self.create();self.create('b');before=deepcopy(self.drafts())
        focus=self.context()['current_operation']
        response=self.web.client.post(f'/api/conversations/{self.web.identity}/trip-operations/{focus["task_id"]}/end',headers=self.web.headers,
            json={'version':focus['version'],'client_request_id':'close-focus'})
        self.assertEqual(response.status_code,200);self.assertIsNone(self.context()['current_operation'])
        text='餐饮预算人均60元'
        reply=self.submit(text,'after-end',IntentOperation('trip','update_constraints',{'changes':{
            'budget':{'scope':'food','value':'60元','unit_text':'人均','source':text}}}))
        self.assertIn('哪一份',reply['content']);self.assertEqual(self.drafts(),before)
