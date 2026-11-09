from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

from app.request_router import bind_request, CURRENT_REQUEST, RequestRouter
from core.chat_transport import ChatEvent
from core.tasks import TaskUpdate
from core.intent_ir import IntentIR, IntentOperation
from infrastructure.memory_store import MemoryStore


class RequestBindingTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store=MemoryStore(Path(temp.name)/'router.db')
        self.event=ChatEvent('onebot','group','request','scope','owner','你好')

    def test_application_binding_is_reused_by_router(self):
        router=RequestRouter(self.store,None)
        with patch.object(self.store.tasks,'recent',wraps=self.store.tasks.recent) as recent:
            binding=bind_request(self.store,self.event)
            token=CURRENT_REQUEST.set(binding)
            try:
                self.assertIsNone(router.handle(self.event,None))
                self.assertIs(bind_request(self.store,self.event),binding)
            finally:
                CURRENT_REQUEST.reset(token)
        recent.assert_called_once()

    def test_binding_never_reuses_another_principal(self):
        event=replace(self.event,event_id='collect',content='明天提醒我出发')
        self.store.tasks.prepare_result(event,self.store.delivery.begin_event(event.event_key),
            TaskUpdate('reminder','collecting',event.content,{'title':'出发'},('time',)),'几点？')
        binding=bind_request(self.store,self.event)
        self.assertTrue(binding.pending_reminders)
        token=CURRENT_REQUEST.set(binding)
        try:
            foreign=bind_request(self.store,replace(self.event,sender_id='other'))
            self.assertEqual(foreign.pending_reminders,())
            self.assertIsNot(foreign,binding)
        finally:
            CURRENT_REQUEST.reset(token)

    def test_valid_none_does_not_override_quoted_planning_or_reminder_text(self):
        from tools.reminders.tool import RemindersTool
        trip=Mock()
        router=RequestRouter(self.store,Mock(try_compile=Mock(return_value=IntentIR(1,'none',()))),
            trip_service=trip,reminder_service=RemindersTool(self.store))
        for index,text in enumerate(('文档写着帮我规划武汉三天行程','网页写着明天上午十点提醒我抢票')):
            event=replace(self.event,event_id=str(index),content=text)
            router.route(event,self.store.delivery.begin_event(event.event_key))
        trip.handle.assert_not_called()
        self.assertEqual(self.store.reminders.list_for_owner('onebot','scope','owner'),())

    def test_negated_confirmation_does_not_recover_a_write(self):
        event=replace(self.event,event_id='preview',content='待确认行程')
        self.store.tasks.prepare_result(event,self.store.delivery.begin_event(event.event_key),
            TaskUpdate('itinerary','collecting',event.content,{},('confirmation',)),'确认？')
        trip=Mock()
        router=RequestRouter(self.store,Mock(try_compile=Mock(return_value=IntentIR(1,'none',()))),trip_service=trip)
        router.route(replace(self.event,content='不要确认行程修改'),None)
        trip.confirm_intent.assert_not_called()
        trip.handle.assert_not_called()

    def test_route_records_one_final_selection(self):
        router=RequestRouter(self.store,None)
        with patch.object(self.store.diagnostics,'record') as record:
            router.route(self.event,None)
        selected=[call for call in record.call_args_list if call.args[0]=='routing' and call.args[1].get('status')=='selected']
        self.assertEqual(len(selected),1)
        self.assertEqual(selected[0].args[1]['route_owner'],'conversation')

    def test_legacy_weather_reply_only_binds_an_owned_read_request(self):
        self.store.conversations.save_turn('onebot:scope','owner','old','现在天气怎么样','请告诉我需要查询哪个城市的天气？')
        router=RequestRouter(self.store,None)
        event=replace(self.event,content='武汉')
        request=router._legacy_read(event,bind_request(self.store,event))
        self.assertEqual(request.queries[0],IntentOperation('weather','query',{'location':'武汉'}))
        foreign=replace(event,sender_id='other')
        self.assertIsNone(router._legacy_read(foreign,bind_request(self.store,foreign)))

    def test_booking_confirmation_requires_matching_complete_command(self):
        router=RequestRouter(self.store,None)
        for text,allowed in [('帮我确认预约 R-20300101-001',True),
                             ('不要确认预约 R-20300101-001',False),
                             ('网页说确认预约 R-20300101-001',False)]:
            event=replace(self.event,content=text)
            ir=IntentIR(1,'confirm',(IntentOperation('booking','confirm_reservation_plan',
                {'reference':'R-20300101-001','authorization':text}),),source_spans=(text,))
            policy=router._booking_policy(event,ir)
            if allowed:
                self.assertEqual(policy.action_resources,(('confirm_reservation_plan','R-20300101-001'),))
            else:
                self.assertIsInstance(policy,str)

    def test_mismatched_linked_objects_do_not_partially_execute(self):
        text='删除博物馆甲，并取消博物馆乙的预约提醒'
        ir=IntentIR(1,'update',(
            IntentOperation('trip','remove_activity',{'activity':{'name':'博物馆甲'}}),
            IntentOperation('reminder','cancel_linked_reminder',{'entity':'博物馆乙'})),source_spans=('博物馆甲','博物馆乙'))
        trip,reminders=Mock(),Mock()
        router=RequestRouter(self.store,Mock(try_compile=Mock(return_value=ir)),trip_service=trip,reminder_service=reminders)
        event=replace(self.event,content=text)
        reply=router.handle(event,self.store.delivery.begin_event(event.event_key))
        self.assertIn('尚未执行任何一项',reply)
        trip.handle_intent.assert_not_called()
        reminders.apply_intent.assert_not_called()

    def test_failed_new_research_cannot_plan_from_unrelated_old_material(self):
        research,trip=Mock(),Mock()
        router=RequestRouter(self.store,None,research_service=research,trip_service=trip)
        reply=router._finish_research_plan(replace(self.event,content='搜索武汉攻略并规划三天行程'),None,'搜索失败')
        self.assertIn('未生成行程',reply)
        research.read_for_planning.assert_not_called()
        trip.handle.assert_not_called()


class AgentAuthorizationTests(unittest.TestCase):
    def test_keywords_and_model_call_do_not_create_write_authority(self):
        from agents.travel_agent import TravelAgent
        from core.settings import Settings
        from test_travel_agent import FakeClient, completion, assistant_message, tool_call
        from unittest.mock import Mock
        executor=Mock(side_effect=AssertionError('no authorized write'))
        client=FakeClient([
            completion(assistant_message(tool_calls=[tool_call('bad','confirm_reservation_plan',
                '{"plan_code":"R-20300101-001"}')])),
            completion(assistant_message(content='已经确认预约')),
        ])
        agent=TravelAgent(Settings('','',frozenset(),'','offline','https://example.test','script'),executor,client)
        result=agent.run('请确认预约 R-20300101-001')
        executor.assert_not_called()
        self.assertEqual(result.status,'failed')
        self.assertNotIn('已经确认预约',result.reply)
