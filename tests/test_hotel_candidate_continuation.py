"""Hotel identity refinement must continue an owned candidate task, not erase its stay."""
from dataclasses import replace
import unittest

import test_hotel_flow_state as flow
import test_universal_hotel_prices as fixtures
from core.tasks import TaskUpdate
from app.request_router import bind_request


class CandidateContinuationTests(unittest.TestCase):
    setUp=flow.HotelFlowStateTests.setUp
    event=flow.HotelFlowStateTests.event
    call=flow.HotelFlowStateTests.call
    turn=flow.HotelFlowStateTests.turn
    menu=flow.HotelFlowStateTests.menu

    def test_copied_candidate_label_preserves_stay_and_uses_existing_task(self):
        _,before,_,_=self.menu();candidate=before.slots['hotel_candidates'][0]
        text=candidate['name']+' · '+candidate['address']
        values={'targets':[{'name':candidate['name'],'city':'测试城','address_hint':candidate['address']}]}
        reply,after,_,compiler=self.turn(text,values)
        self.assertEqual(after.task_id,before.task_id,reply)
        self.assertEqual(after.slots['hotel_request']['stay'],before.slots['hotel_request']['stay'])
        self.assertEqual(after.status,'completed',reply);self.assertEqual(self.transport.call_count,1)
        self.assertEqual(self.map.search_places.call_count,1)
        compiler.try_compile.assert_not_called()

    def test_semantic_full_name_refinement_uses_owned_candidate_identity(self):
        _,before,_,_=self.menu();candidate=before.slots['hotel_candidates'][0]
        text='请核实'+candidate['name']+'的价格，其余入住条件不变'
        values={'targets':[{'name':candidate['name'],'city':'测试城'}]}
        self.assertEqual(bind_request(self.store,self.event(text)).budget_run_key,before.slots['budget_run_key'])
        reply,after,_,compiler=self.turn(text,values)
        self.assertEqual(after.task_id,before.task_id,reply)
        self.assertEqual(after.slots['hotel_request']['stay'],before.slots['hotel_request']['stay'])
        self.assertEqual(self.transport.call_count,1);self.assertEqual(self.map.search_places.call_count,1)
        compiler.try_compile.assert_called_once()

    def test_unique_candidate_binding_beats_an_empty_duplicate_request(self):
        _,original,_,_=self.menu();candidate=original.slots['hotel_candidates'][0]
        text=candidate['name']+' · '+candidate['address']
        values={'targets':[{'name':candidate['name'],'city':'测试城','address_hint':candidate['address']}]}
        # Reproduce the already-created duplicate left by the production defect.
        event=self.event(text);claim=self.store.delivery.begin_event(event.event_key)
        self.store.tasks.prepare_result(event,claim,TaskUpdate('hotel','collecting',text,
            {'hotel_request':{**values,'stay':{}},'intents':['hotel']},('check_in','check_out','rooms','adults','children_ages')),'请补充入住条件')
        bound=bind_request(self.store,self.event(text))
        self.assertEqual(bound.read_context.task.task_id,original.task_id)
        self.assertEqual(bound.budget_run_key,original.slots['budget_run_key'])
        reply,after,_,_=self.turn(text,values)
        self.assertEqual(after.task_id,original.task_id,reply);self.assertEqual(self.transport.call_count,1)

    def test_matching_two_candidate_lists_does_not_guess_dates(self):
        _,task,_,_=self.menu();candidate=task.slots['hotel_candidates'][0]
        other=replace(task,task_id='different-task',slots={**task.slots,'hotel_request':{
            **task.slots['hotel_request'],'stay':{**fixtures.STAY,'check_in':'2030-02-01','check_out':'2030-02-03'}}})
        text=candidate['name']+' · '+candidate['address']
        result=self.hotel.local_selection(self.event(text),(task,other))
        self.assertIsInstance(result,str);self.transport.assert_not_called()

    def test_other_hotel_starts_new_request_and_does_not_borrow_conditions(self):
        _,before,_,_=self.menu()
        reply,after,_,_=self.turn('另外城另一酒店的房价',{'targets':[{'name':'另一酒店','city':'另外城'}]})
        self.assertNotEqual(before.task_id,after.task_id)
        self.assertEqual(after.slots['hotel_request']['stay'],{})
        self.transport.assert_not_called()

    def test_history_keeps_the_explicit_candidate_filter(self):
        self.call();_,task,_,_=self.menu();candidate=task.slots['hotel_candidates'][1]
        values={'mode':'saved','targets':[{'name':candidate['name'],'city':'测试城'}]}
        result=self.call(values,self.event('查看测试城'+candidate['name']+'之前的酒店报价原文，不重新查询'),task.slots)
        self.assertIn('未命中',result.data);self.assertNotIn('已保存的问道原文',result.data)
        self.assertEqual(self.transport.call_count,1)


    def test_changed_city_or_conflicting_address_is_not_a_candidate_selection(self):
        _,before,_,_=self.menu();candidate=before.slots['hotel_candidates'][0]
        for city,address in (('另外城',candidate['address']),('测试城','完全不同的路99号')):
            values={'targets':[{'name':candidate['name'],'city':city,'address_hint':address}]}
            result=self.call(values,self.event(candidate['name']+' '+city+' '+address),before.slots)
            self.assertEqual(result.status,'needs_input',result.data)
        self.transport.assert_not_called()

    def test_chatter_does_not_select_a_candidate(self):
        _,task,_,_=self.menu()
        self.assertIsNone(self.hotel.local_selection(self.event('谢谢'),(task,)))
        self.transport.assert_not_called()

    def test_expired_candidate_text_retains_conditions_before_explicit_resume(self):
        _,before,_,_=self.menu();candidate=before.slots['hotel_candidates'][0]
        with self.store._connect() as db:db.execute("UPDATE travel_tasks SET expires_at='2000-01-01T00:00:00+00:00'")
        text=candidate['name']+' · '+candidate['address']
        reply,recovered,_,compiler=self.turn(text,{'targets':[{'name':candidate['name'],'city':'测试城'}]})
        self.assertIn('过期',reply);self.assertEqual(recovered.slots['hotel_request']['stay'],before.slots['hotel_request']['stay'])
        self.transport.assert_not_called();compiler.try_compile.assert_not_called()
        reply,completed,_,_=self.turn('继续核价',{})
        self.assertEqual(completed.status,'completed',reply);self.assertFalse(completed.slots['hotel_expired'])
        self.assertEqual(self.transport.call_count,1)


class CandidateWebContinuationTests(unittest.TestCase):
    setUp=flow.HotelWebActionsTests.setUp
    seed=flow.HotelWebActionsTests.seed

    def test_normal_message_with_candidate_label_uses_the_existing_task(self):
        self.seed();event=self.web.event('')
        before=self.web.store.tasks.recent('web',event.storage_scope_id,event.sender_id)[0]
        candidate=before.slots['hotel_candidates'][0];text=candidate['name']+' · '+candidate['address']
        self.compiler.reset_mock()
        self.web.send(text,key='candidate-label');self.assertTrue(self.web.run_job())
        after=self.web.store.tasks.recent('web',event.storage_scope_id,event.sender_id)
        self.assertEqual(len(after),1);self.assertEqual(after[0].task_id,before.task_id)
        self.assertEqual(after[0].slots['hotel_request']['stay'],before.slots['hotel_request']['stay'])
        self.assertEqual(after[0].status,'completed');self.assertEqual(self.vendor.call_count,1)
        self.compiler.try_compile.assert_not_called();self.assertIn('399',self.web.messages()[-1]['content'])
        self.assertFalse(self.web.store.trips.list_for_owner(event))



if __name__=='__main__':unittest.main()
