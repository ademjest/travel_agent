"""Hotel evidence failures must remain recoverable at the public tool boundary."""
from copy import deepcopy
import unittest
from unittest.mock import Mock

import test_universal_hotel_prices as fixtures
from core.hotel_contract import evidenced_request
from core.execution_outcome import TaskContext,TravelReadRequest
from core.intent_ir import IntentIR,IntentOperation


class HotelRequestValidationTests(unittest.TestCase):
    def setUp(self):
        fixtures.UniversalHotelTests.setUp(self)
        self.store.diagnostics.record=Mock(wraps=self.store.diagnostics.record)
    event=fixtures.UniversalHotelTests.event
    call=fixtures.UniversalHotelTests.call
    turn=fixtures.UniversalHotelTests.turn

    def test_person_classifiers_with_explicit_no_children(self):
        for text,rooms,adults in [('一间房，一个人无儿童',1,1),('两间房，两个人不带孩子',2,2),
                                  ('1间房，1个人，无儿童',1,1),('一间房，一位成年人，无儿童',1,1)]:
            with self.subTest(text=text):
                values={'stay':{'rooms':rooms,'adults':adults,'children_ages':[]}}
                self.assertEqual(evidenced_request(values,text)['stay'],values['stay'])

    def test_normal_input_continues_stored_dates_and_targets(self):
        request=deepcopy(fixtures.REQUEST);request['stay']={k:v for k,v in request['stay'].items() if k in {'check_in','check_out'}}
        _,task,_=self.turn(fixtures.FULL,request)
        self.assertEqual(task.status,'collecting')
        reply,task,_=self.turn('一间房，一个人无儿童',{'stay':{'rooms':1,'adults':1,'children_ages':[]}})
        self.assertEqual(self.transport.call_count,1,reply)
        self.assertEqual(task.slots['hotel_request']['targets'],request['targets'])
        self.assertEqual(task.slots['hotel_request']['stay']['check_out'],'2030-01-09')
        self.assertFalse(self.store.trips.list_for_owner(self.event()))

    def test_advisory_missing_names_do_not_prevent_hotel_tool_clarification(self):
        text='那确认一下行程里推荐的酒店的房价'
        ctx=TaskContext(None,text,fixtures.NOW.isoformat(),{'hotel_request':{'targets':fixtures.REQUEST['targets']}},text)
        ir=IntentIR(1,'clarify',(IntentOperation('hotel','query',{'reference':{'selection':'行程里推荐的酒店'}}),),
            missing_fields=('hotel_stay','入住日期','stay.adults'),source_spans=(text,))
        event=self.event(text);claim=self.store.delivery.begin_event(event.event_key)
        reply=self.owner.prepare(event,claim,ir,ctx)
        self.assertNotIn('字段或范围未通过校验',reply)
        self.assertIn('入住日期',reply)
        self.transport.assert_not_called()

    def test_invalid_evidence_retains_valid_fields_and_creates_recoverable_task(self):
        initial=deepcopy(fixtures.REQUEST);initial.pop('stay')
        _,_,_=self.turn(fixtures.FULL,initial)
        request={'stay':{**fixtures.STAY,'adults':3}}
        reply,task,event=self.turn(fixtures.FULL,request)
        self.assertIsNotNone(task,reply)
        self.assertEqual(task.status,'collecting')
        self.assertEqual(task.slots['hotel_request']['stay']['rooms'],1)
        self.assertNotIn('adults',task.slots['hotel_request']['stay'])
        self.assertIn('成人数',reply)
        self.assertNotIn('字段或范围未通过校验',reply)
        self.transport.assert_not_called()
        details=[call.args[1] for call in self.store.diagnostics.record.call_args_list]
        self.assertTrue(any(r.get('validation',{}).get('path')=='stay.adults' for r in details))
        reply,task,_=self.turn('两个人，无儿童',{'stay':{'adults':2,'children_ages':[]}})
        self.assertEqual(self.transport.call_count,1,reply)
        self.assertEqual(task.status,'completed',reply)

    def test_bound_target_subset_is_not_tied_to_list_position(self):
        previous={'targets':[{'name':'甲酒店','city':'甲城'},{'name':'乙酒店','city':'乙城'}]}
        result=evidenced_request({'targets':[previous['targets'][1]]},'看看刚才的乙酒店',previous=previous)
        self.assertEqual(result['targets'],[previous['targets'][1]])
        with self.assertRaises(ValueError):
            evidenced_request({'targets':[{'name':'乙酒店','city':'甲城'}]},'看看刚才的乙酒店',previous=previous)

    def test_no_children_must_not_be_invented(self):
        for text in ['一间房，一个人','一间房，一个人带孩子']:
            with self.subTest(text=text),self.assertRaises(ValueError):
                evidenced_request({'stay':{'rooms':1,'adults':1,'children_ages':[]}},text)

    def test_unverified_replacement_cannot_fall_back_to_an_old_valid_value(self):
        result=self.call({'stay':{'adults':3}},self.event('更改入住人数'),slots={'hotel_request':fixtures.REQUEST})
        self.assertEqual(result.status,'needs_input')
        self.assertNotIn('adults',result.payload['request']['stay'])
        result=self.call({'stay':{'rooms':1}},self.event('一间房'),slots={'hotel_request':result.payload['request']})
        self.assertEqual(result.status,'needs_input')
        self.assertIn('adults',result.missing_fields)
        self.transport.assert_not_called()
