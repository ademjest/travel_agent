"""Owned message references and draft continuation through the actual Web queue."""
from copy import deepcopy
import json
import unittest
from unittest.mock import Mock

import test_web_app as fixtures
from test_trips import SPEC,PLACES,plan_value,model_response
from test_travel_agent import FakeClient
from app.request_router import RequestRouter
from core.intent_ir import IntentIR,IntentOperation
from tools.trip.planner import TripPlanner
from tools.trip.tool import TripTool


class MessageReferenceFlowTests(unittest.TestCase):
    def setUp(self):
        self.web=fixtures.WebTests();self.web.setUp();self.addCleanup(self.web.doCleanups)
        self.hotel={'id':'hotel','name':'测试酒店','type':'住宿服务;酒店','address':'测试路1号','location':'114.3,30.5','rating':4.8}
        amap=Mock()
        amap.search_places.side_effect=lambda city,keyword,**kw:[self.hotel] if '酒店' in keyword else list(PLACES.values())
        self.models=FakeClient([model_response({**SPEC,'start_date_text':'2030年10月1日'}),model_response(plan_value())])
        self.service=TripTool(self.web.store,TripPlanner(self.models,'test',amap))
        self.web.runtime.application.trip_service=self.service
        self.compiler=Mock()
        self.compiler.try_compile.return_value=IntentIR(1,'none',())
        self.web.runtime.application.semantic_task_service=RequestRouter(self.web.store,self.compiler,
            trip_service=self.service,read_only_service=self.web.runtime.application.read_only_service)

    def submit(self,text,key,operation,quote=None,action=None,missing=()):
        ir=IntentIR(1,action or ('create' if operation.operation=='create' else 'update'),(operation,),missing_fields=missing,source_spans=(text,))
        self.compiler.try_compile.return_value=ir
        body={'content':text,'client_request_id':key}
        if quote:body['quoted_message_id']=quote
        response=self.web.client.post(f'/api/conversations/{self.web.identity}/messages',headers=self.web.headers,json=body)
        self.assertEqual(response.status_code,202,response.text)
        self.assertTrue(self.web.run_job())
        return [m for m in self.web.messages() if m['role']=='assistant'][-1]

    def context(self):
        return self.web.client.get(f'/api/conversations/{self.web.identity}/context').json()

    def assert_budget_continuation(self,quoted,save=True):
        text='帮我规划2030年10月1日开始的武汉三天行程，带老人，必去湖北省博物馆'
        self.submit(text,'create',IntentOperation('trip','create',{'request_text':text}))
        if save:
            c=self.context()['confirmations'][0]
            self.web.client.post(f'/api/conversations/{self.web.identity}/confirmations/{c["id"]}',headers=self.web.headers,
                json={'version':c['version'],'client_request_id':'save'})
            self.web.run_job()
            saved=self.web.store.trips.list_for_owner(self.web.event(''))[0]
        else:saved=deepcopy(self.web.store.trips.current_drafts(self.web.event(''))[0].slots['candidate_trip'])
        text='酒店高档一点，200-400，尽量是连锁'
        question=self.submit(text,'ask',IntentOperation('trip','update_constraints',{'changes':{
            'accommodation':{'value':'高档一点','source':text,'quality_requested':True,'chain_mode':'preferred'},
            'budget':{'value':'200-400','scope':'accommodation','source':'200-400'}}}),
            action='clarify',missing=('budget_unit',))
        text='200-400人民币，每间每晚'
        self.submit(text,'answer',IntentOperation('trip','update_constraints',{'changes':{
            'budget':{'value':'200-400','scope':'accommodation','unit_text':'人民币，每间每晚','source':text}}}),
            question['id'] if quoted else None)
        tasks=self.web.store.tasks.recent('web','web:'+self.web.identity,'local-owner')
        self.assertFalse(any(t.task_type=='semantic' and t.status=='collecting' for t in tasks))
        draft=self.web.store.trips.current_drafts(self.web.event(''))[0].slots['candidate_trip']
        self.assertTrue(draft['spec']['hotel_preferences']['quality_requested'])
        self.assertEqual(draft['spec']['hotel_preferences']['chain_mode'],'preferred')
        self.assertEqual(draft['spec']['budgets']['accommodation']['unit_text'],'人民币，每间每晚')
        self.assertEqual(draft['plan']['days'],saved['plan']['days'])
        self.assertEqual(draft['plan'].get('intercity'),saved['plan'].get('intercity'))
        self.assertEqual(len(self.models.completions.requests),2)

    def test_quoted_budget_answer_keeps_validated_quality_and_chain(self):
        self.assert_budget_continuation(True)

    def test_unquoted_budget_answer_keeps_validated_quality_and_chain(self):
        self.assert_budget_continuation(False)

    def test_new_unsaved_draft_uses_the_same_partial_continuation_without_replanning(self):
        self.assert_budget_continuation(True,save=False)

    def test_expired_quote_edit_new_confirmation_then_natural_hotel_and_removal(self):
        text='帮我规划2030年10月1日开始的武汉三天行程，带老人，必去湖北省博物馆'
        first=self.submit(text,'create',IntentOperation('trip','create',{'request_text':text}))
        initial=self.context()['confirmations'][0]
        self.assertTrue(initial['can_confirm'])
        with self.web.store._connect() as db:db.execute("UPDATE travel_tasks SET expires_at='2000-01-01'")
        expired=self.context()['confirmations'][0]
        self.assertFalse(expired['can_confirm']);self.assertEqual(expired['state'],'expired')
        self.assertEqual(expired['reference_message_id'],first['id'])
        self.assertTrue(self.context()['trips'][0]['is_preview'])
        text='去程改为二等座'
        changes={'intercity':{'value':{'outbound':{'seat_class':{'value':'second_class','source':'二等座'}}},'source':text}}
        newer=self.submit(text,'seat',IntentOperation('trip','update_constraints',{'changes':changes}),first['id'])
        self.assertIn('行程创建预览',newer['content'])
        current=self.context()['confirmations'][0]
        self.assertNotEqual(current['id'],expired['id']);self.assertTrue(current['can_confirm'])
        self.assertFalse(self.web.store.trips.list_for_owner(self.web.event('')))
        old=self.web.client.post(f'/api/conversations/{self.web.identity}/confirmations/{expired["id"]}',headers=self.web.headers,
            json={'version':expired['version'],'client_request_id':'old-confirm'})
        self.assertEqual(old.status_code,409)
        # A historical source cannot silently replace the newly edited draft.
        stale=self.submit(text,'stale',IntentOperation('trip','update_constraints',{'changes':changes}),first['id'])
        self.assertIn('历史版本',stale['content'])
        accepted=self.web.client.post(f'/api/conversations/{self.web.identity}/confirmations/{current["id"]}',headers=self.web.headers,
            json={'version':current['version'],'client_request_id':'new-confirm'})
        self.assertEqual(accepted.status_code,202,accepted.text);self.web.run_job()
        saved=self.web.store.trips.list_for_owner(self.web.event(''))[0]
        self.assertEqual(saved['spec']['intercity_request']['outbound']['seat_class'],'second_class')
        original_days=deepcopy(saved['plan']['days'])
        text='我希望再加上住宿推荐'
        hotel=self.submit(text,'hotel',IntentOperation('trip','update_constraints',{'changes':{
            'accommodation':{'value':'住宿推荐','source':'住宿推荐','requested':True}}}))
        self.assertIn('行程修改预览',hotel['content'])
        draft=self.web.store.trips.current_drafts(self.web.event(''))[0].slots['candidate_trip']
        self.assertTrue(draft['spec']['hotel_requested']);self.assertEqual(draft['plan']['days'],original_days)
        text='我不想去湖北省博物馆'
        removed=self.submit(text,'remove',IntentOperation('trip','remove_activity',{'place':'湖北省博物馆'}),action='delete')
        self.assertIn('行程修改预览',removed['content'])
        draft=self.web.store.trips.current_drafts(self.web.event(''))[0].slots['candidate_trip']
        self.assertTrue(draft['spec']['hotel_requested'])
        self.assertNotIn('湖北省博物馆',draft['spec']['must_visit'])
        self.assertTrue(all(a['poi_id']!='h' for day in draft['plan']['days'] for a in day['activities']))
        self.assertEqual(draft['plan']['intercity']['outbound']['request']['seat_class'],'second_class')
        self.assertIn('hotel',draft['plan']['hotel_ids'])
        self.assertTrue(draft['plan']['hotel_recommendations'])
        self.assertEqual(len(self.models.completions.requests),2,'Only initial requirements and planning models; edits stay local')

    def test_plain_reference_reaches_intent_and_answer_context_without_rail_binding(self):
        from app.conversation_context import ContextBuilder,render_untrusted_context
        from app.request_router import bind_request
        from adapters.web_adapter import WebAdapter
        self.web.send('普通历史问题',key='ordinary');self.web.run_job()
        user=next(m for m in self.web.messages() if m['role']=='user')
        response=self.web.client.post(f'/api/conversations/{self.web.identity}/messages',headers=self.web.headers,
            json={'content':'解释这条历史问题','client_request_id':'explain','quoted_message_id':user['id']})
        with self.web.store._connect() as db:
            row=db.execute('SELECT payload_json FROM inbox_jobs WHERE id=?',(response.json()['job_id'],)).fetchone()
        event=WebAdapter(self.web.runtime.application,self.web.repo,context_builder=lambda _:{}).input_event(json.loads(row['payload_json']))
        self.assertEqual(bind_request(self.web.store,event).read_context.slots,{})
        context=ContextBuilder(self.web.store).build(event)
        self.assertIn('普通历史问题',render_untrusted_context(context))
        self.assertIn('不代表最新状态',render_untrusted_context(context))

    def test_saved_trip_accepts_quoted_and_natural_intercity_edits(self):
        text='帮我规划2030年10月1日开始的武汉三天行程，带老人，必去湖北省博物馆'
        self.submit(text,'create',IntentOperation('trip','create',{'request_text':text}))
        for key,direction,field,value,text,quoted in (
                ('morning','outbound','departure_window','早上','去程改为早上出发',True),
                ('seat','inbound','seat_class','second_class','返程改为二等座',False)):
            c=self.context()['confirmations'][0]
            response=self.web.client.post(f'/api/conversations/{self.web.identity}/confirmations/{c["id"]}',headers=self.web.headers,
                json={'version':c['version'],'client_request_id':'confirm-'+key})
            self.assertEqual(response.status_code,202,response.text);self.web.run_job()
            saved=self.web.store.trips.list_for_owner(self.web.event(''))[0]
            quote=[m for m in self.web.messages() if m['role']=='assistant'][-1]['id'] if quoted else None
            source='早上' if field=='departure_window' else '二等座'
            changes={'intercity':{'value':{direction:{field:{'value':value,'source':source}}},'source':text}}
            reply=self.submit(text,key,IntentOperation('trip','update_constraints',{'changes':changes}),quote)
            self.assertIn('行程修改预览',reply['content'])
            draft=self.web.store.trips.current_drafts(self.web.event(''))[0].slots['candidate_trip']
            self.assertEqual(draft['spec']['intercity_request'][direction][field],value)
            self.assertEqual(draft['plan']['days'],saved['plan']['days'])
            self.assertEqual(draft['spec']['fact_sources']['intercity_request']['source'],text)
            self.assertEqual(self.web.store.trips.list_for_owner(self.web.event(''))[0]['version'],saved['version'])
        self.assertEqual(len(self.models.completions.requests),2)
