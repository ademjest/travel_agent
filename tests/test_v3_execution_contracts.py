from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from core.tool_execution import retry_read
from core.tool_result import ToolResult, validate_arguments
from core.context_contract import complete_json, task_projection, response_json
from core.model_budget import ModelBudgetExceeded
from infrastructure.memory_store import MemoryStore
from infrastructure.model_gateway import ModelGateway
from scripts.evaluate_token_memory import build_report, ReadOnlyStore


class ExecutionContractsTests(unittest.TestCase):
    def test_transient_read_retries_only_with_bounded_backoff(self):
        results=[ToolResult('failed','t',error_code='timeout',retryable=True,retry_after_seconds=1),ToolResult('completed','t',data='ok')]
        waits=[]
        result=retry_read(lambda:results.pop(0),sleeper=waits.append)
        self.assertEqual((result.status,result.attempts,waits),('completed',2,[1]))

    def test_permission_error_has_no_retry(self):
        self.assertEqual(retry_read(lambda:ToolResult('failed','t',error_code='permission_denied'),sleeper=lambda _:self.fail()).attempts,1)

    def test_complete_json_rejects_overflow_instead_of_truncating(self):
        with self.assertRaises(ModelBudgetExceeded):
            complete_json({'important':'不允许'+ '甲'*1000},100)

    def test_task_projection_keeps_reference_and_excludes_nested_plan(self):
        task=SimpleNamespace(task_type='weather',status='collecting',missing_slots=['location'],initial_request='天气',
                             slots={'reference_time':'2030-01-01','candidate_trip':{'long':'甲'*10000}})
        projected=task_projection(task)
        self.assertIn('reference_time',projected['slots'])
        self.assertNotIn('candidate_trip',projected['slots'])

    def test_schema_maximum_is_enforced(self):
        self.assertTrue(validate_arguments(100,{'type':'integer','maximum':10}))

    def test_truncated_json_is_not_accepted_even_if_syntactically_valid(self):
        result=SimpleNamespace(choices=[SimpleNamespace(finish_reason='length',message=SimpleNamespace(content='{}'))])
        with self.assertRaises(ValueError):
            response_json(result)

    def test_committed_write_survives_later_model_failure(self):
        from agents.travel_agent import TravelAgent
        from core.settings import Settings
        calls = []
        response=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='',tool_calls=[
            SimpleNamespace(id='write',function=SimpleNamespace(name='cancel_reservation_plan',arguments='{"plan_code":"R-20300101-001"}'))]))])
        def create(**kwargs):
            if calls:
                raise TimeoutError()
            return response
        def execute(name,args):
            calls.append(name)
            return ToolResult('completed',name,resource_id=args['plan_code'],effect_status='committed',data='已取消第一个预约')
        client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        agent=TravelAgent(Settings('','',frozenset(),'','fake','https://example.test','fake'),execute,client)
        result=agent.run('取消预约 R-20300101-001 和 R-20300101-002')
        self.assertEqual(result.status,'partial')
        self.assertIn('已取消第一个预约',result.reply)
        self.assertEqual(len(result.receipts),1)
        self.assertEqual(calls,['cancel_reservation_plan'])

    def test_history_watermark_processes_only_new_turns_without_renumbering(self):
        from core.chat_transport import ChatEvent
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as folder:
            store=MemoryStore(Path(folder)/'history.db')
            event=ChatEvent('web','group','event','c','u','引用最开始的方案')
            store.save_turn('web:c','u','web:group:c:old','规划武汉2天','方案：第1天公园，第2天博物馆')
            repo=store.planning
            with patch.object(repo,'capture',wraps=repo.capture) as capture:
                repo.ensure_history(event)
                repo.ensure_history(event)
                self.assertEqual(capture.call_count,1)
                first=repo.list_for_owner(event)[0]
                store.save_turn('web:c','u','web:group:c:new','规划武汉3天','方案：第1天公园，第2天博物馆，第3天街区')
                repo.ensure_history(event)
                self.assertEqual(capture.call_count,2)
                self.assertEqual(repo.list_for_owner(event)[0]['ordinal'],first['ordinal'])

    def test_report_cannot_write_or_create_tables(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'r.db'
            store=MemoryStore(path)
            before=set()
            with store._connect() as db:
                before={row[0] for row in db.execute('SELECT name FROM sqlite_master')}
            report=build_report(path)
            self.assertFalse(report['features']['model_calls'])
            with ReadOnlyStore(path)._connect() as db:
                self.assertEqual({row[0] for row in db.execute('SELECT name FROM sqlite_master')},before)
                with self.assertRaises(Exception):
                    db.execute('CREATE TABLE forbidden(x)')

    def test_missing_report_database_is_not_created(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'absent.db'
            with self.assertRaises(FileNotFoundError):
                build_report(path)
            self.assertFalse(path.exists())

    def test_injected_runtime_enables_production_semantic_path(self):
        from app.runtime_factory import build_runtime
        from core.settings import Settings
        with tempfile.TemporaryDirectory() as folder:
            client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw:None)))
            runtime=build_runtime(Settings('','',frozenset(),'','fake','https://example.test','fake'),
                platform='web',store=MemoryStore(Path(folder)/'r.db'),client=client,
                transport=SimpleNamespace(),reply_renderer=SimpleNamespace(),group_allowed=lambda _:True)
            self.assertIsNotNone(runtime.application.semantic_task_service)
            self.assertIs(runtime.application.semantic_task_service.compiler.client.client,client)
