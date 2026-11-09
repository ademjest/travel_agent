"""Shared accounting admission must retain uncertainty and release settled reservations."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest.mock import patch,Mock
import os

from core.model_budget import MODEL_RUN,MODEL_EVENT,ModelBudgetExceeded
from infrastructure.memory_store import MemoryStore
from infrastructure.model_gateway import ModelGateway
from core.hotel_contract import NEW_RUN_COMMAND


class RunBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=MemoryStore(Path(self.temp.name)/'accounting.db')
        self.gateway=ModelGateway(self.store,SimpleNamespace())
        token=MODEL_RUN.set('one-hotel-run');self.addCleanup(MODEL_RUN.reset,token)
        self.result=SimpleNamespace(usage=SimpleNamespace(prompt_tokens=20,completion_tokens=10))
        self.env=patch.dict(os.environ,{'AI_DAILY_TOKEN_LIMIT':'0','AI_MONTHLY_TOKEN_LIMIT':'0','AI_DAILY_COST_LIMIT':'0','AI_MONTHLY_COST_LIMIT':'0'})
        self.env.start();self.addCleanup(self.env.stop)

    def finish(self,event,estimate,*,known=True):
        call=self.gateway._reserve(event,'fake',estimate,10)
        self.gateway._finish(call,time.monotonic(),self.result if known else None,None if known else TimeoutError())
        return call

    def test_settled_actual_usage_releases_run_reservation_without_changing_audit(self):
        for i in range(6):self.finish('turn-'+str(i),60000)
        with self.store._connect() as db:
            self.assertEqual(db.execute('SELECT sum(reserved),sum(consumed) FROM model_usage_ledger').fetchone()[:],(360060,180))

    def test_settled_actual_usage_also_releases_same_event_reservation(self):
        for _ in range(6):self.finish('same-event',60000)

    def test_mixed_settled_running_unknown_keeps_the_unknown_reservation(self):
        self.finish('settled',180000);self.finish('unknown',50000,known=False)
        self.gateway._reserve('running','fake',19000,10)
        call=self.gateway._reserve('allowed','fake',10000,10)
        self.assertIsNotNone(call)
        with self.store._connect() as db:
            self.assertEqual(db.execute("SELECT reserved FROM model_usage_ledger WHERE status='unknown'").fetchone()[0],50010)

    def test_ambiguous_legacy_settlement_stays_reserved(self):
        for i in range(2):
            call=self.finish(str(i),120000,known=False)
            with self.store._connect() as db:db.execute("UPDATE model_usage_ledger SET status='settled',consumed=0 WHERE call_id=?",(call,))
        with self.assertRaises(ModelBudgetExceeded):self.gateway._reserve('blocked','fake',20000,10)

    def test_unknown_usage_cannot_release_tokens(self):
        self.finish('one',125000,known=False);self.finish('two',125000,known=False)
        with self.assertRaises(ModelBudgetExceeded) as caught:self.gateway._reserve('blocked','fake',10000,10)
        self.assertEqual(caught.exception.code,'run_token_limit')
        self.assertGreaterEqual(caught.exception.details['occupied_tokens'],250000)

    def test_run_call_limit_survives_settlement_and_detail_deletion(self):
        for i in range(12):self.finish(str(i),100)
        with self.store._connect() as db:db.execute('DELETE FROM model_calls')
        with self.assertRaises(ModelBudgetExceeded) as caught:self.gateway._reserve('new-event','fake',10,10)
        self.assertEqual(caught.exception.code,'run_call_limit')

    def test_concurrent_reservations_cannot_both_take_the_last_tokens(self):
        self.finish('prior',240000,known=False)
        def reserve(i):
            token=MODEL_RUN.set('one-hotel-run')
            try:
                try:return bool(self.gateway._reserve('parallel-'+str(i),'fake',10000,10))
                except ModelBudgetExceeded:return False
            finally:MODEL_RUN.reset(token)
        with ThreadPoolExecutor(max_workers=2) as pool:self.assertEqual(sum(pool.map(reserve,range(2))),1)

    def test_daily_limit_still_applies_after_an_explicit_new_run(self):
        self.finish('unknown',100000,known=False)
        token=MODEL_RUN.set('new-hotel-run')
        try:
            with patch.dict(os.environ,{'AI_DAILY_TOKEN_LIMIT':'105000'}),self.assertRaises(ModelBudgetExceeded) as caught:
                self.gateway._reserve('blocked','fake',10000,10)
            self.assertEqual(caught.exception.code,'daily_token_limit')
        finally:MODEL_RUN.reset(token)


class HotelRunRecoveryTests(unittest.TestCase):
    def setUp(self):
        import test_hotel_flow_state as flow
        self.flow=flow.HotelFlowStateTests();self.flow.setUp();self.addCleanup(self.flow.doCleanups)

    def block(self,code='run_call_limit',text='请继续核对房价'):
        from agents.intent_compiler import IntentCompilationError
        from app.request_router import RequestRouter
        _,task,_,_=self.flow.menu();event=self.flow.event(text)
        compiler=Mock();compiler.try_compile.side_effect=IntentCompilationError('budget_exceeded','本轮调用已达到上限。',budget_code=code,budget_details={'calls':12})
        reply,_=RequestRouter(self.flow.store,compiler,read_only_service=self.flow.owner).route(event,self.flow.store.delivery.begin_event(event.event_key))
        return reply,self.flow.store.tasks.for_event(event,event.event_key),task

    def test_block_is_explained_and_only_explicit_restart_uses_a_linked_new_run(self):
        from app.request_router import bind_request,RequestRouter
        reply,blocked,before=self.block()
        self.assertIn(NEW_RUN_COMMAND,reply);self.assertEqual(blocked.task_id,before.task_id)
        self.assertEqual(blocked.slots['hotel_request'],before.slots['hotel_request'])
        self.assertEqual(bind_request(self.flow.store,self.flow.event('继续核价')).budget_run_key,before.slots['budget_run_key'])
        event=self.flow.event(NEW_RUN_COMMAND);binding=bind_request(self.flow.store,event)
        self.assertNotEqual(binding.budget_run_key,before.slots['budget_run_key'])
        self.assertEqual(binding.read_context.slots['budget_run_parent']['run_key'],before.slots['budget_run_key'])
        from core.intent_ir import IntentIR,IntentOperation
        compiler=Mock();compiler.try_compile.return_value=IntentIR(1,'read',(IntentOperation('hotel','query',{}),),source_spans=('请继续核对房价',))
        claim=self.flow.store.delivery.begin_event(event.event_key)
        RequestRouter(self.flow.store,compiler,read_only_service=self.flow.owner).route(event,claim)
        after=self.flow.store.tasks.for_event(event,event.event_key)
        self.assertEqual(after.slots['budget_run_key'],binding.budget_run_key)
        self.assertEqual(after.slots['hotel_request']['stay'],before.slots['hotel_request']['stay'])
        compiler.try_compile.assert_called_once();self.flow.transport.assert_not_called()

    def test_global_limit_does_not_offer_a_run_restart(self):
        from app.request_router import bind_request,RequestRouter
        reply,_,_=self.block('daily_token_limit')
        self.assertNotIn(NEW_RUN_COMMAND,reply)
        event=self.flow.event(NEW_RUN_COMMAND)
        self.assertIsNone(bind_request(self.flow.store,event).read_context.task)
        compiler=Mock();reply,_=RequestRouter(self.flow.store,compiler,read_only_service=self.flow.owner).route(event,self.flow.store.delivery.begin_event(event.event_key))
        self.assertIn('没有唯一绑定',reply);compiler.try_compile.assert_not_called();self.flow.transport.assert_not_called()

    def test_new_run_reprocesses_the_blocked_date_update_instead_of_querying_old_dates(self):
        from app.request_router import RequestRouter
        from core.intent_ir import IntentIR,IntentOperation
        text='改为2030-01-10入住，2030-01-11退房，其余不变，继续核对房价'
        self.block(text=text)
        compiler=Mock();compiler.try_compile.return_value=IntentIR(1,'read',(IntentOperation('hotel','query',{
            'stay':{'check_in':'2030-01-10','check_out':'2030-01-11'}}),),source_spans=(text,))
        event=self.flow.event(NEW_RUN_COMMAND)
        RequestRouter(self.flow.store,compiler,read_only_service=self.flow.owner).route(event,self.flow.store.delivery.begin_event(event.event_key))
        task=self.flow.store.tasks.for_event(event,event.event_key)
        self.assertEqual(task.slots['hotel_request']['stay']['check_in'],'2030-01-10')
        self.assertEqual(task.slots['hotel_request']['stay']['check_out'],'2030-01-11')
        self.assertTrue(compiler.try_compile.call_args.args[0].endswith(text))
        self.assertNotIn(NEW_RUN_COMMAND,compiler.try_compile.call_args.args[0])
        self.flow.transport.assert_not_called()

    def test_budget_exception_detail_survives_intent_parsing_without_model_fallback(self):
        from agents.intent_compiler import IntentCompiler,IntentCompilationError
        error=ModelBudgetExceeded('本月token限额不足，原样重试无效。',code='monthly_token_limit',details={'limit':100})
        create=Mock(side_effect=error)
        compiler=IntentCompiler(SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),'fake')
        with self.assertRaises(IntentCompilationError) as caught:compiler.try_compile('查武汉酒店房价')
        self.assertEqual(caught.exception.detail,str(error));self.assertEqual(caught.exception.budget_code,error.code)
        self.assertEqual(create.call_count,1)

    def test_fallback_budget_error_keeps_its_period_and_never_tries_a_third_call(self):
        from agents.intent_compiler import IntentCompiler,IntentCompilationError
        invalid=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='invalid JSON'))])
        error=ModelBudgetExceeded('本月token限额不足。',code='monthly_token_limit')
        create=Mock(side_effect=[invalid,error])
        compiler=IntentCompiler(SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),'default')
        with patch.dict(os.environ,{'LLM_INTENT_MODEL_ID':'other'}),self.assertRaises(IntentCompilationError) as caught:
            compiler.try_compile('查询武汉酒店房价')
        self.assertEqual(caught.exception.detail,str(error));self.assertEqual(caught.exception.budget_code,error.code)
        self.assertEqual(create.call_count,2)

    def test_completed_query_new_user_request_has_parent_but_no_shared_run(self):
        from app.request_router import bind_request
        import test_universal_hotel_prices as fixtures
        _,before,_,_=self.flow.turn(fixtures.FULL,fixtures.REQUEST)
        self.assertEqual(before.status,'completed')
        event=self.flow.event('改为2030年1月10日入住，11日退房，再查房价')
        binding=bind_request(self.flow.store,event)
        self.assertNotEqual(binding.budget_run_key,before.slots['budget_run_key'])
        self.assertEqual(binding.read_context.slots['budget_run_parent']['run_key'],before.slots['budget_run_key'])
        self.assertEqual(binding.budget_run_key,bind_request(self.flow.store,event).budget_run_key)

    def test_extraction_budget_block_retains_source_and_local_read_does_not_call_models(self):
        import test_universal_hotel_prices as fixtures
        self.flow.hotel.client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=Mock(
            side_effect=ModelBudgetExceeded('本轮调用上限。',code='run_call_limit')))))
        result=self.flow.call()
        self.assertEqual(result.status,'needs_input');self.assertEqual(result.payload['budget_block']['code'],'run_call_limit')
        self.assertIn('原文',result.data)
        self.assertEqual(result.data.count('本轮调用上限。'),1)
        event=self.flow.event('读取已有资料')
        snapshot=result.payload['snapshot']
        self.flow.hotel.original(event,snapshot['source_event_id'])
        self.assertEqual(self.flow.transport.call_count,1)
        self.assertEqual(self.flow.hotel.client.chat.completions.create.call_count,1)

    def test_mixed_task_budget_recovery_preserves_other_results_and_parent_run(self):
        from agents.intent_compiler import IntentCompilationError
        from app.request_router import RequestRouter,bind_request
        from core.tasks import TaskUpdate
        from core.tool_result import ToolResult
        _,before,_,_=self.flow.menu()
        with self.flow.store._connect() as db:db.execute("UPDATE travel_tasks SET status='cancelled'")
        import json
        weather=json.loads(ToolResult('completed','get_weather',data='已保存天气资料',payload={'weather':'晴'}).to_json())
        hotel=json.loads(ToolResult('needs_input','query_hotel_prices',data='请选择',missing_fields=('targets',),payload={}).to_json())
        event=self.flow.event('酒店和天气');claim=self.flow.store.delivery.begin_event(event.event_key)
        slots={'budget_run_key':'mixed-run','hotel_states':{'0':before.slots},'operations':[
            {'domain':'hotel','operation_key':'h','arguments':{},'status':'needs_input'},
            {'domain':'weather','operation_key':'w','arguments':{'location':'测试城'},'status':'completed'}],'receipts':[hotel,weather]}
        self.flow.store.tasks.prepare_result(event,claim,TaskUpdate('read_batch','collecting',event.content,slots,('targets',)),'请选择酒店')
        compiler=Mock();compiler.try_compile.side_effect=IntentCompilationError('budget_exceeded','本轮已受限。',budget_code='run_call_limit')
        event=self.flow.event('继续查酒店房价');router=RequestRouter(self.flow.store,compiler,read_only_service=self.flow.owner)
        router.route(event,self.flow.store.delivery.begin_event(event.event_key))
        parent=self.flow.store.tasks.for_event(event,event.event_key)
        self.assertEqual(parent.task_type,'read_batch');self.assertEqual(parent.slots['receipts'][1],weather)
        event=self.flow.event(NEW_RUN_COMMAND);binding=bind_request(self.flow.store,event)
        self.assertIsNotNone(binding.read_context.task)
        self.assertEqual(binding.read_context.slots['_read_batch']['budget_run_key'],binding.budget_run_key)
        self.assertEqual(binding.read_context.slots['budget_run_parent']['run_key'],'mixed-run')
        router.route(event,self.flow.store.delivery.begin_event(event.event_key))
        updated=self.flow.store.tasks.for_event(event,event.event_key)
        self.assertEqual(updated.slots['budget_run_key'],binding.budget_run_key)
        self.assertEqual(updated.slots['receipts'][1],weather)

    def test_expired_blocked_task_can_start_an_owned_linked_run_without_reusing_old_task_id(self):
        from app.request_router import bind_request
        _,blocked,_=self.block()
        with self.flow.store._connect() as db:db.execute("UPDATE travel_tasks SET expires_at='2000-01-01T00:00:00+00:00'")
        event=self.flow.event(NEW_RUN_COMMAND);binding=bind_request(self.flow.store,event)
        self.assertIsNotNone(binding.read_context.task)
        self.assertEqual(binding.read_context.task.task_id,'')
        self.assertTrue(binding.read_context.slots['hotel_expired'])
        self.assertEqual(binding.read_context.slots['budget_run_parent']['task_id'],blocked.task_id)
        self.flow.transport.assert_not_called()


if __name__=='__main__':unittest.main()
