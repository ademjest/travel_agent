from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
from threading import Event, Lock
from types import SimpleNamespace
import unittest

from core.execution_scope import CURRENT_EXECUTION, ExecutionScope, ExecutionRevoked
from core.model_budget import MODEL_EVENT, ModelBudgetExceeded
from infrastructure.memory_store import MemoryStore
from infrastructure.model_gateway import ModelGateway, estimate_input


class ModelGatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = MemoryStore(Path(self.temp.name) / 'models.db')
        self.requests = []
        self.result = SimpleNamespace(choices=[], usage=SimpleNamespace(prompt_tokens=20, completion_tokens=10))
        def create(**kwargs):
            self.requests.append(kwargs)
            return self.result
        self.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        self.gateway = ModelGateway(self.store, self.client)

    def test_records_actual_usage_and_limits_output(self):
        token = MODEL_EVENT.set('test-event')
        try:
            self.gateway.create(model='fake', messages=[{'role': 'user', 'content': 'hello'}], max_completion_tokens=20000)
        finally:
            MODEL_EVENT.reset(token)
        self.assertEqual(self.requests[0]['max_completion_tokens'], 8192)
        with self.store._connect() as connection:
            row = connection.execute('SELECT * FROM model_calls').fetchone()
        self.assertEqual((row['prompt_tokens'], row['completion_tokens']), (20, 10))
        self.assertEqual(row['event_key'], 'test-event')
        self.assertNotIn('hello', str(dict(row)))

    def test_image_payload_is_estimated_as_image_not_base64_text_tokens(self):
        value = [{'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + 'A'*100000}}]
        self.assertLess(estimate_input(value), 5000)

    def test_budget_stops_before_extra_model_call(self):
        token = MODEL_EVENT.set('bounded-event')
        try:
            for _ in range(12):
                self.gateway.create(model='fake', messages=[{'role': 'user', 'content': 'hello'}])
            with self.assertRaises(ModelBudgetExceeded):
                self.gateway.create(model='fake', messages=[{'role': 'user', 'content': 'hello'}])
        finally:
            MODEL_EVENT.reset(token)
        self.assertEqual(len(self.requests), 12)
        with self.assertRaises(ModelBudgetExceeded):
            self.gateway.create(model='fake', messages=[{'role': 'user', 'content': '很长'*40000}])

    def test_physical_model_concurrency_is_at_most_two(self):
        release = Event()
        two_started = Event()
        lock = Lock()
        running, peak = 0, 0
        def create(**kwargs):
            nonlocal running, peak
            with lock:
                running += 1
                peak = max(peak, running)
                if running == 2:
                    two_started.set()
            release.wait(3)
            with lock:
                running -= 1
            return self.result
        self.client.chat.completions.create = create
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(self.gateway.create, model='fake', messages=[{'role': 'user', 'content': 'hello'}]) for _ in range(4)]
            self.assertTrue(two_started.wait(2))
            release.set()
            for future in futures:
                future.result(timeout=3)
        self.assertEqual(peak, 2)

    def test_revoked_job_cannot_start_another_model_call(self):
        self.store.inbox.submit('onebot:group:g:e', 'onebot', 'g', 'u', {})
        job = self.store.inbox.claim()
        self.store.inbox.cancel(job['event_key'], 'onebot', 'g', 'u')
        token = CURRENT_EXECUTION.set(ExecutionScope(str(self.store.database_path), job['id'], job['claim_token']))
        try:
            with self.assertRaises(ExecutionRevoked):
                self.gateway.create(model='fake', messages=[])
        finally:
            CURRENT_EXECUTION.reset(token)
        self.assertEqual(self.requests, [])
