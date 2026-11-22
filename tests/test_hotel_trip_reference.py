"""Itinerary candidates are valid hotel query inputs, including expired drafts."""
from copy import deepcopy
from dataclasses import replace
import unittest

import test_universal_hotel_prices as fixtures
from test_hotel_quotes import trip, STAY, TARGET
from core.tasks import TaskUpdate
from core.intent_ir import IntentIR, IntentOperation
from app.request_router import bind_read_context


class HotelTripReferenceTests(unittest.TestCase):
    setUp=fixtures.UniversalHotelTests.setUp
    event=fixtures.UniversalHotelTests.event
    call=fixtures.UniversalHotelTests.call

    def seed(self, *, expired=False, complete=False, candidate=None):
        event=self.event('规划测试城，两位成年人')
        value=deepcopy(candidate or trip());value['spec'].update(start_date=STAY['check_in'],end_date=STAY['check_out'])
        value['spec'].setdefault('preferences',['两位成年人'])
        if not complete:value['spec'].pop('hotel_stay')
        value['spec']['budgets']['accommodation']={'value':'300-400','unit_text':'人民币每间每晚','source':'300-400'}
        slots={'trip_id':value['trip_id'],'candidate_trip':value,'expected_trip_version':0,'new_trip_pending':True}
        self.store.trips.commit(event,self.store.delivery.begin_event(event.event_key),
            TaskUpdate('itinerary','collecting',event.content,slots,('confirmation',)),
            '待核价候选：'+TARGET['h']['name'],presentation={'trip':value,'is_preview':True,'sections':[]})
        task=self.store.tasks.for_event(event,event.event_key)
        if expired:
            with self.store._connect() as db:db.execute("UPDATE travel_tasks SET expires_at='2000-01-01' WHERE task_id=?",(task.task_id,))
        reference={'event_id':event.event_key,'trip':deepcopy(value),'is_preview':True,
                   'draft':{'task_id':task.task_id,'version':task.version}}
        return reference,task

    def turn(self,text,values,reference=None):
        event=self.event(text,message_reference=reference);claim=self.store.delivery.begin_event(event.event_key)
        context=bind_read_context(event,self.store.tasks.recent(event.platform,event.storage_scope_id,event.sender_id))
        ir=IntentIR(1,'read',(IntentOperation('hotel','query',values),),source_spans=(text,))
        reply=self.owner.prepare(event,claim,ir,context)
        return reply,self.store.tasks.for_event(event,event.event_key)

    def test_expired_candidate_collects_only_missing_stay_and_keeps_budget(self):
        for expired in (False,True):
            with self.subTest(expired=expired):
                reference,_=self.seed(expired=expired)
                result=self.call({'mode':'refresh','reference':{'selection':'这些酒店'}},self.event('复查这些酒店的房价',message_reference=reference))
                self.assertEqual(result.status,'needs_input',result.data)
                self.assertNotIn('targets',result.missing_fields)
                self.assertNotIn('adults',result.missing_fields)
                self.assertEqual(result.payload['request']['stay'],{'adults':2})
                self.assertEqual(result.payload['request']['budget']['value'],'300-400')
                self.assertEqual(result.payload['request']['targets'][0]['name'],TARGET['h']['name'])
                self.assertIn('2030-01-07',result.data)
                self.assertIn(TARGET['h']['name'],result.data)
        self.transport.assert_not_called();self.map.search_places.assert_not_called()

    def test_date_confirmation_continues_same_targets_and_does_not_save_trip(self):
        reference,original=self.seed(expired=True)
        reply,task=self.turn('核对这些酒店房价',{'reference':{'selection':'这些酒店'}},reference)
        self.assertNotIn('targets',task.missing_slots,reply)
        self.assertEqual(task.slots['hotel_request']['stay']['adults'],2)
        reply,task=self.turn('是，一间房，无儿童',{'proposal_response':{'kind':'stay_dates','decision':'accept','source':'是'},
                                               'stay':{'rooms':1,'children_ages':[]}})
        self.assertEqual(task.status,'completed',reply)
        self.assertEqual(task.slots['hotel_request']['stay'],STAY)
        self.assertEqual(task.slots['hotel_request']['budget']['value'],'300-400')
        self.assertEqual(task.slots['hotel_trip_reference'],{})
        self.assertEqual(self.transport.call_count,1)
        self.assertEqual(self.map.search_places.call_count,0)
        self.assertFalse(self.store.trips.list_for_owner(self.event()))
        unchanged=self.store.tasks.owned(self.event(),original.task_id)
        self.assertEqual(unchanged.slots,original.slots)
        self.assertEqual(unchanged.version,original.version)

    def test_full_stay_uses_frozen_snapshot_not_current_or_forged_payload(self):
        reference,_=self.seed(complete=True)
        reference['trip']['sources']['h']['name']='伪造酒店'
        result=self.call({'reference':{'selection':'这些酒店'}},self.event('查询这些酒店',message_reference=reference))
        self.assertEqual(result.status,'completed',result.data)
        self.assertEqual(result.payload['request']['targets'][0]['name'],TARGET['h']['name'])
        self.assertNotIn('伪造酒店',str(result.payload))

    def test_foreign_source_and_ended_draft_never_query(self):
        reference,task=self.seed(complete=True)
        for event in (self.event('核对这些酒店',message_reference=reference,sender_id='other'),
                      self.event('核对这些酒店',message_reference=reference,scope_id='other')):
            self.assertEqual(self.call({'reference':{'selection':'这些酒店'}},event).status,'needs_input')
        for status in ('cancelled','superseded'):
            with self.store._connect() as db:db.execute('UPDATE travel_tasks SET status=? WHERE task_id=?',(status,task.task_id))
            result=self.call({'reference':{'selection':'这些酒店'}},self.event('核对这些酒店',message_reference=reference))
            self.assertEqual(result.status,'needs_input',result.data)
        self.transport.assert_not_called();self.map.search_places.assert_not_called()

    def test_pending_query_rechecks_draft_lifecycle_before_external_call(self):
        reference,original=self.seed(expired=True)
        _,task=self.turn('核对这些酒店房价',{'reference':{'selection':'这些酒店'}},reference)
        with self.store._connect() as db:db.execute("UPDATE travel_tasks SET status='cancelled' WHERE task_id=?",(original.task_id,))
        reply,_=self.turn('是，一间房，无儿童',{'proposal_response':{'kind':'stay_dates','decision':'accept','source':'是'},'stay':{'rooms':1,'children_ages':[]}})
        self.assertIn('引用',reply)
        self.transport.assert_not_called()

    def test_only_displayed_candidates_and_display_order_are_inherited(self):
        value=trip()
        value['sources']['other']={**TARGET['h'],'id':'other','name':'另一候选酒店','address':'测试路9号'}
        value['sources']['hidden']={**TARGET['h'],'id':'hidden','name':'未推荐酒店'}
        value['plan']['hotel_ids']=['h','other']
        value['plan']['hotel_recommendations']=[{'poi_id':'h','status':'fallback'},
                                               {'poi_id':'other','status':'partial','price_status':'unknown'}]
        reference,_=self.seed(candidate=value)
        result=self.call({'reference':{'selection':'第二家','ordinal':2}},self.event('查询第二家',message_reference=reference))
        self.assertEqual([t['name'] for t in result.payload['request']['targets']],[TARGET['h']['name']])
        self.assertNotIn('hidden',result.payload['resolved_targets'])
        self.transport.assert_not_called()

    def test_changed_draft_version_requires_new_reference(self):
        reference,task=self.seed(complete=True)
        with self.store._connect() as db:db.execute('UPDATE travel_tasks SET version=version+1 WHERE task_id=?',(task.task_id,))
        result=self.call({'reference':{'selection':'这些酒店'}},self.event('查询这些酒店',message_reference=reference))
        self.assertEqual(result.status,'needs_input')
        self.assertIn('版本',result.data)
        self.transport.assert_not_called()

    def test_explicit_stay_and_current_correction_override_the_snapshot(self):
        reference,_=self.seed(complete=True)
        result=self.call({'reference':{'selection':'这些酒店'},'stay':{'adults':1}},
                         self.event('查询这些酒店，改为1名成人',message_reference=reference))
        self.assertEqual(result.payload['request']['stay']['adults'],1)
        self.assertEqual(result.payload['request']['stay']['children_ages'],[])

    def test_conflicting_or_negated_party_is_not_inferred_and_no_children_is_not_defaulted(self):
        for preferences in (['两位成年人','三位成年人'],['不是两位成年人']):
            value=trip();value['spec']['preferences']=preferences
            reference,_=self.seed(candidate=value)
            result=self.call({'reference':{'selection':'这些酒店'}},self.event('查询这些酒店',message_reference=reference))
            self.assertIn('adults',result.missing_fields)
            self.assertIn('children_ages',result.missing_fields)
        self.transport.assert_not_called()

    def test_offline_source_does_not_query_or_borrow_unrelated_price(self):
        value=trip();value['spec']['provider_policy']={'offline':True}
        reference,_=self.seed(candidate=value,complete=True)
        result=self.call({'mode':'refresh','reference':{'selection':'这些酒店'}},self.event('复查这些酒店',message_reference=reference))
        self.assertEqual(result.error_code,'source_restricted')
        self.transport.assert_not_called();self.map.search_places.assert_not_called()

    def test_multiple_map_categories_must_all_be_accommodation(self):
        from tools.hotel_prices.quotes import lodging_target
        self.assertTrue(lodging_target({'typecode':'100102|100200',
            'type':'住宿服务;宾馆酒店;五星级宾馆|住宿服务;旅馆招待所;旅馆招待所'}))
        for code,kind in [('100102|150904','住宿服务;宾馆酒店'),
                          ('100102|100200','住宿服务;宾馆酒店|交通设施服务;停车场'),
                          ('100102||100200','住宿服务;宾馆酒店')]:
            self.assertFalse(lodging_target({'typecode':code,'type':kind}))

    def test_completed_hotel_receipt_remains_readable_after_draft_is_discarded(self):
        reference,task=self.seed(complete=True)
        query_event=self.event('查询这些酒店',message_reference=reference)
        result=self.call({'reference':{'selection':'这些酒店'}},query_event)
        self.assertEqual(result.status,'completed',result.data)
        with self.store._connect() as db:db.execute("UPDATE travel_tasks SET status='cancelled' WHERE task_id=?",(task.task_id,))
        quote={'event_id':query_event.event_key,'tool_results':[{'action':'query_hotel_prices','payload':result.payload}]}
        saved=self.call({'mode':'saved','reference':{'selection':'这份报价'}},self.event('查看这份报价',message_reference=quote))
        self.assertEqual(saved.status,'completed',saved.data)
        self.assertEqual(self.transport.call_count,1)


if __name__=='__main__':unittest.main()
