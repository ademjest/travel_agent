from app.request_router import bind_read_context
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from datetime import datetime, timezone

from core.chat_transport import ChatEvent
from core.intent_ir import IntentIR, IntentOperation
from infrastructure.memory_store import MemoryStore
from app.request_router import RequestRouter
from core.execution_outcome import TaskContext
from core.tasks import TaskRecord
from core.tool_result import ToolResult


class QueryFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = MemoryStore(Path(self.temp.name) / 'tasks.db')
        self.calls = []
        self.travel = SimpleNamespace(execute_result=self.execute, manages_read_retries=False)

    def execute(self, name, arguments):
        self.calls.append((name, arguments))
        return ToolResult('completed', name, data='构造天气快照：晴，温度20℃')

    def send(self, key, text, ir):
        event = ChatEvent('web', 'group', key, 'c', 'u', text,
                          occurred_at=datetime.now(timezone.utc))
        compiler = SimpleNamespace(try_compile=lambda *args, **kwargs: ir)
        service = RequestRouter(self.store, compiler, travel_service=self.travel)
        return service.handle(event, self.store.delivery.begin_event(event.event_key))

    def test_missing_location_then_city_then_forecast_updates_one_task(self):
        self.send('1', '查天气', IntentIR(1, 'clarify', (IntentOperation('weather', 'query', {}),),
                  ('location',), False, ('天气',)))
        self.send('2', '武汉', IntentIR(1, 'read', (IntentOperation('weather', 'query', {'location': '武汉'}),),
                  (), False, ('武汉',)))
        self.send('3', '明天呢', IntentIR(1, 'read', (IntentOperation('forecast', 'query', {}),),
                  (), False, ('明天',)))
        self.assertEqual(self.calls, [('get_current_weather', {'location': '武汉'}),
                                     ('get_weather_forecast', {'location': '武汉'})])
        with self.store._connect() as db:
            rows = db.execute('SELECT * FROM travel_tasks').fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['version'], rows[0]['status']), (3, 'completed'))

    def test_tool_failure_is_not_saved_as_completed(self):
        self.travel.execute_result = lambda name, args: ToolResult('failed', name, data='网络超时', error_code='timeout')
        self.send('fail', '武汉天气', IntentIR(1, 'read', (IntentOperation('weather', 'query', {'location': '武汉'}),),
                  (), False, ('武汉',)))
        with self.store._connect() as db:
            row = db.execute('SELECT status FROM travel_tasks').fetchone()
        self.assertEqual(row['status'], 'failed')

    def test_cancelled_query_does_not_receive_short_answer(self):
        self.send('1', '查天气', IntentIR(1, 'clarify', (IntentOperation('weather', 'query', {}),),
                  ('location',), False, ('天气',)))
        with self.store._connect() as db:
            db.execute("UPDATE travel_tasks SET status='cancelled'")
        self.send('2', '武汉', IntentIR(1, 'none', ()))
        self.assertEqual(self.calls, [])

    def test_midnight_location_answer_retains_original_time_but_new_relative_date_resets_it(self):
        original='2030-09-19T15:59:00+00:00'
        task=TaskRecord('t','forecast','collecting','明天天气',{'reference_time':original},
                        ('location',),1,'2030-09-20T16:30:00+00:00','first')
        next_time=datetime(2030,9,19,16,1,tzinfo=timezone.utc)
        context=bind_read_context(ChatEvent('web','group','city','c','u','武汉',occurred_at=next_time),(task,))
        self.assertEqual(context.reference_time,original)
        completed=TaskRecord('t','forecast','completed','明天天气',{'reference_time':original,'location':'武汉'},
                             (),2,'2030-09-20T16:30:00+00:00','city')
        new=bind_read_context(ChatEvent('web','group','next','c','u','那明天呢',occurred_at=next_time),(completed,))
        self.assertEqual(new.reference_time,next_time.isoformat())

    def test_different_completed_locations_are_not_silently_selected(self):
        tasks=tuple(TaskRecord(str(i),'weather','completed','天气',{'location':city},(),1,
                               '2030-12-31T00:00:00+00:00','old') for i,city in enumerate(('武汉','西宁')))
        event=ChatEvent('web','group','new','c','u','那明天呢')
        self.assertIsNone(bind_read_context(event,tasks).task)
