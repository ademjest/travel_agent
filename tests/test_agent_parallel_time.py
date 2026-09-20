from contextvars import ContextVar
from threading import Barrier, Lock
import unittest

from agents.context_builder import AgentContext
from agents.travel_agent import TravelAgent
from core.settings import Settings
from core.tasks import TaskRecord
from test_travel_agent import FakeClient, tool_call, assistant_message, completion


class ParallelAndTimeTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings('', '', frozenset(), '', 'fake', 'https://example.test', 'fake')

    def test_read_queries_overlap_and_copy_context_without_reordering_receipts(self):
        barrier = Barrier(2)
        marker = ContextVar('marker', default='missing')
        seen = []
        lock = Lock()
        def execute(name, arguments):
            barrier.wait(timeout=2)
            with lock:
                seen.append(marker.get())
            return name + ':success'
        client = FakeClient([
            completion(assistant_message(tool_calls=[
                tool_call('weather', 'get_weather_forecast', '{"location":"武汉"}'),
                tool_call('traffic', 'get_route_traffic', '{"origin":"武汉站","destination":"湖北省博物馆"}'),
            ])), completion(assistant_message(content='查询完成')),
        ])
        token = marker.set('job-context')
        try:
            result = TravelAgent(self.settings, execute, client=client).run('明天武汉天气和武汉站到湖北省博物馆路况怎么样')
        finally:
            marker.reset(token)
        self.assertEqual(seen, ['job-context', 'job-context'])
        self.assertEqual([trace.call_id for trace in result.traces], ['weather', 'traffic'])

    def test_write_tools_are_not_dispatched_by_parallel_helper(self):
        called = []
        agent = TravelAgent(self.settings, lambda name, args: called.append(name), client=FakeClient([]))
        result = agent._parallel_reads([
            tool_call('a', 'confirm_reservation_plan', '{"plan_code":"R-20300101-001"}'),
            tool_call('b', 'cancel_reservation_plan', '{"plan_code":"R-20300101-002"}'),
        ], {'confirm_reservation_plan', 'cancel_reservation_plan'}, None, {}, 0)
        self.assertEqual(result, {})
        self.assertEqual(called, [])

    def test_city_reply_after_midnight_keeps_original_question_time(self):
        task = TaskRecord('t', 'forecast', 'collecting', '明天天气怎么样',
            {'reference_time': '2030-09-09T23:55:00+08:00', 'intents': ['forecast']}, ('location',), 1,
            '2030-09-10T00:25:00+08:00', 'previous')
        context = AgentContext((), '', '', '', tasks=(task,), uses_task_state=True,
                               message_time='2030-09-10T00:05:00+08:00')
        client = FakeClient([
            completion(assistant_message(tool_calls=[tool_call('w', 'get_weather_forecast', '{"location":"武汉"}')])),
            completion(assistant_message(content='天气预报')),
        ])
        result = TravelAgent(self.settings, lambda name, args: 'data', client=client).run('武汉', context)
        self.assertIn('2030-09-09T23:55:00+08:00', client.completions.requests[0]['messages'][0]['content'])
        self.assertEqual(result.task_update.slots['reference_time'], task.slots['reference_time'])
