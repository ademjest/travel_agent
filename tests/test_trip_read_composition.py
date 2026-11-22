from copy import deepcopy
import json
import unittest

from core.intent_ir import IntentIR,IntentOperation,parse_intent_ir
from test_draft_actions import PriceWorkflowTests


class TripReadCompositionTests(PriceWorkflowTests):
    def query(self, operations, text, key='read', quote=None):
        self.compiler.try_compile.return_value=parse_intent_ir({'schema_version':1,'action':'read',
            'operations':operations,'source_spans':[text],'requires_confirmation':False},text)
        body={'content':text,'client_request_id':key}
        if quote:body['quoted_message_id']=quote
        response=self.web.client.post(f'/api/conversations/{self.web.identity}/messages',headers=self.web.headers,json=body)
        self.assertEqual(response.status_code,202,response.text);self.assertTrue(self.web.run_job())
        return self.web.messages()[-1]

    def prepared(self):
        self.create();self.dining();self.budget()
        self.budget('90以内','lunch',[{'day_index':2,'meal':'午餐'}])
        return self.save(self.context()['confirmations'][0])

    def test_view_and_budget_after_discard_reads_saved_seventy(self):
        saved=self.prepared()
        self.budget('120以内','dinner',[{'day_index':2,'meal':'晚餐'}])
        self.action(self.context()['confirmations'][0],'discard','drop')
        searches=self.amap.search_places.call_count;models=len(self.models.completions.requests)
        text='查看行程，并告诉我第二天晚餐现在的预算是多少。'
        message=self.query([{'domain':'trip','operation':'view_current_trip'},
            {'domain':'trip','operation':'check_prices','target_ref':'current_trip',
             'requested_checks':[{'scope':'food','source':'第二天晚餐现在的预算是多少'}]}],text)
        self.assertIn('第2天晚餐预算：70以下',message['content'])
        self.assertIn('旅行行程',message['content']);self.assertNotIn('暂不能一起',message['content'])
        self.assertNotIn('120',message['content'])
        self.assertEqual(self.web.store.trips.get(self.web.event(''),saved['trip_id']),saved)
        self.assertFalse(self.web.store.trips.current_drafts(self.web.event('')))
        self.assertEqual(self.amap.search_places.call_count,searches)
        self.assertEqual(len(self.models.completions.requests),models)
        self.assertIn('trip_presentation',message)
        visible=''.join(s.get('text','') for s in message['trip_presentation']['sections'])+message['trip_presentation'].get('prefix','')
        self.assertIn('第2天晚餐预算：70以下',visible)

    def test_merged_and_split_read_shapes_have_the_same_selected_budget(self):
        self.prepared();text='查看行程，第二天午餐预算是多少'
        check={'scope':'food','kind':'budget','source':'第二天午餐预算是多少','day_index':2,'meal':'午餐'}
        forms=[[{'domain':'trip','operation':'view_current_trip','requested_checks':[check]}],
               [{'domain':'trip','operation':'view_current_trip'},
                {'domain':'trip','operation':'check_budget','requested_checks':[check]}]]
        for i,ops in enumerate(forms):
            reply=self.query(ops,text,'shape-'+str(i))['content']
            self.assertIn('第2天午餐预算：90以内',reply)
            self.assertIn('旅行行程',reply)

    def test_budget_read_with_active_draft_reports_preview_and_does_not_save(self):
        saved=self.prepared();self.budget('120以内','pending-dinner',[{'day_index':2,'meal':'晚餐'}])
        before=self.candidate();text='查看行程和第二天晚餐预算'
        reply=self.query([{'domain':'trip','operation':'view_current_trip'},
            {'domain':'trip','operation':'check_budget','requested_checks':[{'scope':'food','source':'第二天晚餐预算'}]}],text)['content']
        self.assertIn('第2天晚餐预算：120以内',reply);self.assertIn('尚未保存',reply)
        self.assertEqual(self.candidate(),before)
        self.assertEqual(self.web.store.trips.get(self.web.event(''),saved['trip_id'])['version'],saved['version'])

    def test_historical_budget_uses_the_quoted_snapshot(self):
        self.prepared();self.budget('120以内','historical',[{'day_index':2,'meal':'晚餐'}])
        old=self.context()['confirmations'][0];self.action(old,'discard','drop-old')
        text='只看引用方案的第二天晚餐预算'
        reply=self.query([{'domain':'trip','operation':'check_budget','requested_checks':[{'scope':'food','source':'第二天晚餐预算'}]}],
            text,quote=old['reference_message_id'])['content']
        self.assertIn('第2天晚餐预算：120以内',reply)
        self.assertFalse(self.web.store.trips.current_drafts(self.web.event('')))

    def test_write_disguised_as_read_is_rejected_before_effects(self):
        saved=self.prepared();text='查看行程并取消行程'
        self.compiler.try_compile.return_value=IntentIR(1,'read',(
            IntentOperation('trip','view_current_trip',{}),
            IntentOperation('trip','update_constraints',{'changes':{'budget':{'value':'1','scope':'food','source':text}}})),source_spans=(text,))
        self.web.send(text,'mixed');self.web.run_job()
        self.assertEqual(self.web.store.trips.get(self.web.event(''),saved['trip_id']),saved)
        self.assertFalse(self.web.store.trips.current_drafts(self.web.event('')))
        self.assertIn('查看行程',self.web.messages()[-1]['content'])

    def test_ambiguous_read_keeps_both_views_and_completes_after_target_selection(self):
        from test_trips import SPEC,plan_value,model_response
        saved=self.prepared()
        self.models.completions.responses.extend([model_response({**SPEC,'start_date_text':'2030年10月1日'}),model_response(plan_value())])
        self.create('second-trip')
        text='查看行程，并告诉我第二天晚餐预算是多少'
        operations=[{'domain':'trip','operation':'view_current_trip'},
            {'domain':'trip','operation':'check_budget','requested_checks':[{'scope':'food','source':'第二天晚餐预算是多少'}]}]
        reply=self.query(operations,text,'ambiguous')['content']
        self.assertIn('要查看哪一份',reply)
        before=self.web.store.trips.current_drafts(self.web.event(''))
        selected=text+'，选择'+saved['trip_id']
        reply=self.query(operations,selected,'selected')['content']
        self.assertIn('第2天晚餐预算：70以下',reply);self.assertIn('旅行行程',reply)
        self.assertEqual(self.web.store.trips.current_drafts(self.web.event('')),before)
        with self.web.store._connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM travel_tasks WHERE task_type='semantic' AND status='collecting'").fetchone()[0],0)

    def test_unset_budget_is_not_zero_and_does_not_query_merchants(self):
        saved=self.save(self.create());searches=self.amap.search_places.call_count
        text='第二天晚餐预算是多少'
        reply=self.query([{'domain':'trip','operation':'check_budget',
            'requested_checks':[{'scope':'food','source':text}]}],text)['content']
        self.assertIn('尚未设置',reply)
        self.assertEqual(self.amap.search_places.call_count,searches)
        self.assertEqual(self.web.store.trips.get(self.web.event(''),saved['trip_id']),saved)


class TripReadContractTests(unittest.TestCase):
    def test_budget_selectors_must_be_grounded_and_read_operations_cannot_hide_changes(self):
        text='第二天晚餐预算是多少'
        base={'domain':'trip','operation':'check_budget','requested_checks':[
            {'scope':'food','source':text,'day_index':2,'meal':'晚餐'}]}
        for patch in ({'day_index':3},{'day_index':True},{'meal':'午餐'}):
            op=deepcopy(base);op['requested_checks'][0].update(patch)
            with self.assertRaises(ValueError):parse_intent_ir({'schema_version':1,'action':'read','operations':[op],'source_spans':[text]},text)
        op={**base,'changes':{'budget':{'value':'120'}}}
        with self.assertRaises(ValueError):parse_intent_ir({'schema_version':1,'action':'read','operations':[op],'source_spans':[text]},text)


def load_tests(loader,tests,pattern):
    suite=unittest.TestSuite(loader.loadTestsFromTestCase(TripReadContractTests))
    for name in TripReadCompositionTests.__dict__:
        if name.startswith('test_'):suite.addTest(TripReadCompositionTests(name))
    return suite
