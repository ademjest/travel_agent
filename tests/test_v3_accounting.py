from pathlib import Path
import os
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
import threading
import asyncio
from datetime import datetime, timezone

from core.model_budget import MODEL_RUN, ModelBudgetExceeded
from infrastructure.memory_store import MemoryStore
from infrastructure.model_gateway import ModelGateway, usage_report


class AccountingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store=MemoryStore(Path(self.temp.name)/'ledger.db')
        self.result=SimpleNamespace(usage=SimpleNamespace(prompt_tokens=20,completion_tokens=10))
        self.gateway=ModelGateway(self.store,SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw:self.result))))

    def test_actual_usage_releases_unused_reservation(self):
        self.gateway.create(model='fake',messages=[])
        report=usage_report(self.store)
        self.assertEqual(report['budget']['day_reserved'],30)
        self.assertEqual(report['budget']['day_actual_tokens'],30)

    def test_deleted_detail_still_settles_without_resurrecting_it(self):
        call=self.gateway._reserve('event','fake',100,100)
        with self.store._connect() as db:
            db.execute('DELETE FROM model_calls')
        self.gateway._finish(call,time.monotonic(),self.result,None)
        with self.store._connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM model_calls').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT consumed FROM model_usage_ledger').fetchone()[0],30)

    def test_duplicate_finish_cannot_double_count(self):
        call=self.gateway._reserve('event','fake',100,100)
        for _ in range(2):
            self.gateway._finish(call,time.monotonic(),self.result,None)
        self.assertEqual(usage_report(self.store)['budget']['day_reserved'],30)

    def test_concurrent_duplicate_finish_settles_once(self):
        from infrastructure.model_accounting import settle_ledger
        barrier=threading.Barrier(2)
        def concurrent(*args,**kwargs):
            barrier.wait(timeout=10)
            return settle_ledger(*args,**kwargs)
        call=self.gateway._reserve('concurrent','fake',100,100)
        with patch('infrastructure.model_gateway.settle_ledger',side_effect=concurrent):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures=[pool.submit(self.gateway._finish,call,time.monotonic(),self.result,None) for _ in range(2)]
                for future in futures:
                    future.result()
        self.assertEqual(usage_report(self.store)['budget']['day_reserved'],30)
        with self.store._connect() as db:
            self.assertEqual(db.execute('SELECT consumed FROM model_usage_ledger').fetchone()[0],30)

    def test_settlement_takes_write_transaction_before_reading_old_status(self):
        from infrastructure.model_accounting import settle_ledger
        call=self.gateway._reserve('trace','fake',100,100)
        statements=[]
        with self.store._connect() as db:
            db.set_trace_callback(statements.append)
            settle_ledger(db,call,20,None,10,None)
        self.assertEqual(statements[0],'BEGIN IMMEDIATE')

    def test_invalid_usage_is_not_added_to_known_totals(self):
        self.result.usage=SimpleNamespace(prompt_tokens=-100,completion_tokens=-10,
                                          prompt_tokens_details=SimpleNamespace(cached_tokens=-1))
        self.gateway.create(model='fake',messages=[])
        report=usage_report(self.store)
        self.assertEqual((report['totals']['prompt_tokens'],report['totals']['cached_input_tokens'],
                          report['totals']['completion_tokens']),(0,0,0))
        self.assertEqual(report['totals']['unknown_usage'],1)
        self.assertGreater(report['budget']['day_unknown_occupied'],0)

    def test_bare_itinerary_slot_answers_keep_original_business_run(self):
        from app.runtime_factory import build_runtime
        from core.chat_transport import ChatEvent
        from core.settings import Settings
        from core.tasks import TaskUpdate
        for index,(field,answer) in enumerate((('destination','武汉'),('duration','三天'),('budget_unit','每人、全程'))):
            with self.subTest(field=field):
                scope='c'+str(index)
                start=ChatEvent('web','group','first',scope,'owner','继续补充行程')
                self.store.tasks.prepare_result(start,self.store.begin_event(start.event_key),
                    TaskUpdate('itinerary','collecting',start.content,{'budget_run_key':'shared-'+scope},(field,)),'请补充')
                runtime=build_runtime(Settings('','',frozenset(),'','','',''),platform='web',store=self.store,
                    transport=SimpleNamespace(),reply_renderer=SimpleNamespace(),group_allowed=lambda _:True)
                keys=[]
                async def handle(event):
                    keys.append(MODEL_RUN.get())
                runtime.application._handle=handle
                asyncio.run(runtime.application.handle(ChatEvent('web','group','answer',scope,'owner',answer)))
                asyncio.run(runtime.application.handle(ChatEvent('web','group','new',scope,'owner','查西宁天气')))
                self.assertEqual(keys[0],'shared-'+scope)
                self.assertNotEqual(keys[1],'shared-'+scope)

    def test_legacy_utc_periods_are_not_released_at_earlier_shanghai_midnight(self):
        from infrastructure.model_accounting import initialize_accounting, period_keys
        now=datetime(2026,10,9,14,tzinfo=timezone.utc)
        with self.store._connect() as db:
            db.execute('DELETE FROM model_accounting_settings')
            db.execute("INSERT INTO model_usage_periods(period_type,period_start,purpose,model_id,reserved_tokens) VALUES ('day','2026-10-09','legacy','fake',1500)")
            with patch('infrastructure.model_accounting.datetime') as clock:
                clock.now.return_value=now
                clock.min=datetime.min
                clock.combine=datetime.combine
                initialize_accounting(db)
            self.assertEqual(period_keys(db,datetime(2026,10,9,16,1,tzinfo=timezone.utc))[0],'2026-10-09')
            self.assertEqual(period_keys(db,datetime(2026,10,10,0,1,tzinfo=timezone.utc))[0],'2026-10-10')
            self.assertEqual(period_keys(db,datetime(2026,10,31,16,1,tzinfo=timezone.utc))[1],'2026-10-01')
            self.assertEqual(period_keys(db,datetime(2026,11,1,0,1,tzinfo=timezone.utc))[1],'2026-11-01')

    def test_unknown_usage_keeps_reservation_and_can_be_reconciled(self):
        call=self.gateway._reserve('event','fake',100,100)
        self.gateway._finish(call,time.monotonic(),None,TimeoutError())
        self.assertEqual(usage_report(self.store)['budget']['day_unknown_occupied'],200)
        self.gateway._finish(call,time.monotonic(),self.result,None)
        self.assertEqual(usage_report(self.store)['budget']['day_reserved'],30)

    def test_shared_run_does_not_reset_budget_for_followup_messages(self):
        token=MODEL_RUN.set('one-logical-run')
        try:
            for _ in range(12):
                self.gateway.create(model='fake',messages=[])
            with self.assertRaises(ModelBudgetExceeded):
                self.gateway.create(model='fake',messages=[])
        finally:
            MODEL_RUN.reset(token)

    def test_hard_amount_limit_requires_known_prices(self):
        with patch.dict(os.environ,{'AI_DAILY_COST_LIMIT':'1','LLM_INPUT_PRICE_PER_M':'','LLM_OUTPUT_PRICE_PER_M':''}):
            with self.assertRaises(ModelBudgetExceeded):
                self.gateway.create(model='fake',messages=[])

    def test_zero_rate_does_not_consume_money_limit(self):
        with patch.dict(os.environ,{'AI_DAILY_COST_LIMIT':'.001','LLM_INPUT_PRICE_PER_M':'0','LLM_OUTPUT_PRICE_PER_M':'0'}):
            self.gateway.create(model='fake',messages=[])
        with self.store._connect() as db:
            self.assertEqual(db.execute('SELECT estimated_cost FROM model_usage_ledger').fetchone()[0],0)
