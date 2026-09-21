from datetime import datetime, timedelta, timezone
import json
import logging
import threading
import time
from types import SimpleNamespace
import uuid

from core.execution_scope import CURRENT_EXECUTION
from core.model_budget import MODEL_EVENT, ModelBudgetExceeded


logger = logging.getLogger(__name__)
MODEL_SLOTS = threading.BoundedSemaphore(2)
MAX_MODEL_CALLS_PER_EVENT = 12
MAX_INPUT_ESTIMATE = 64_000
MAX_EVENT_ESTIMATE = 256_000
MAX_OUTPUT_TOKENS = 8192


def estimate_input(value):
    """Conservative text byte bound plus an explicit estimate for each image, not a tokenizer."""
    if isinstance(value, str):
        return len(value.encode('utf-8')) + 4
    if isinstance(value, list):
        return sum(estimate_input(item) for item in value) + 2
    if isinstance(value, dict):
        if value.get('type') == 'image_url':
            return 4096
        return sum(estimate_input(key) + estimate_input(item) for key, item in value.items()) + 2
    return len(str(value)) + 2


def usage_summary(store):
    with store._connect() as connection:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_calls'").fetchone():
            return {'calls': 0, 'prompt_tokens': 0, 'completion_tokens': 0, 'usage_missing': 0}
        row = connection.execute('''SELECT count(*) AS calls, coalesce(sum(prompt_tokens),0) AS prompt_tokens,
            coalesce(sum(completion_tokens),0) AS completion_tokens,
            coalesce(sum(CASE WHEN prompt_tokens IS NULL THEN 1 ELSE 0 END),0) AS usage_missing FROM model_calls''').fetchone()
        return dict(row)


class ModelGateway:
    def __init__(self, store, client):
        self.store = store
        self.client = client
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
        with store._connect() as connection:
            connection.execute('''CREATE TABLE IF NOT EXISTS model_calls (
                call_id TEXT PRIMARY KEY, event_key TEXT NOT NULL, job_id INTEGER,
                model_id TEXT NOT NULL, status TEXT NOT NULL, input_estimate INTEGER NOT NULL,
                reserved_tokens INTEGER NOT NULL, prompt_tokens INTEGER, completion_tokens INTEGER,
                started_at TEXT NOT NULL, finished_at TEXT, elapsed_seconds REAL, error_type TEXT NOT NULL DEFAULT ''
            )''')
            connection.execute('CREATE INDEX IF NOT EXISTS idx_model_calls_event ON model_calls(event_key, started_at)')

    def _ensure_active(self):
        scope = CURRENT_EXECUTION.get()
        if scope:
            with self.store._connect() as connection:
                scope.validate(connection)
                row = connection.execute('SELECT deadline_at FROM inbox_jobs WHERE id=?', (scope.job_id,)).fetchone()
                return max(0.01, (datetime.fromisoformat(row['deadline_at']) - datetime.now(timezone.utc)).total_seconds())
        return 90.0

    def _reserve(self, event_key, model, estimate, output):
        now = datetime.now(timezone.utc)
        scope = CURRENT_EXECUTION.get()
        with self.store._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            active = connection.execute("SELECT count(*) FROM model_calls WHERE status='running' AND started_at>?",
                                        ((now-timedelta(seconds=300)).isoformat(),)).fetchone()[0]
            if active >= 2:
                return None
            row = connection.execute('SELECT count(*) AS calls, coalesce(sum(reserved_tokens),0) AS reserved FROM model_calls WHERE event_key=?', (event_key,)).fetchone()
            if row['calls'] >= MAX_MODEL_CALLS_PER_EVENT or row['reserved'] + estimate + output > MAX_EVENT_ESTIMATE:
                raise ModelBudgetExceeded('本次任务的模型调用或总预算已达到上限。请缩小本次任务范围后重试。')
            call_id = uuid.uuid4().hex
            connection.execute('''INSERT INTO model_calls
                (call_id, event_key, job_id, model_id, status, input_estimate, reserved_tokens, started_at)
                VALUES (?, ?, ?, ?, 'running', ?, ?, ?)''',
                (call_id, event_key, scope.job_id if scope else None, model, estimate, estimate+output, now.isoformat()))
        return call_id

    def _finish(self, call_id, started, result, error):
        usage = getattr(result, 'usage', None)
        prompt = getattr(usage, 'prompt_tokens', None)
        completion = getattr(usage, 'completion_tokens', None)
        # Cost telemetry is recorded even when the business job has been revoked.
        token = CURRENT_EXECUTION.set(None)
        try:
            with self.store._connect() as connection:
                connection.execute('''UPDATE model_calls SET status=?, prompt_tokens=?, completion_tokens=?,
                    finished_at=?, elapsed_seconds=?, error_type=? WHERE call_id=?''',
                    ('failed' if error else 'completed', prompt if type(prompt) is int else None,
                     completion if type(completion) is int else None, datetime.now(timezone.utc).isoformat(),
                     time.monotonic()-started, type(error).__name__ if error else '', call_id))
        except Exception:
            logger.warning('Model usage telemetry could not be persisted: call_id=%s', call_id)
        finally:
            CURRENT_EXECUTION.reset(token)

    def create(self, **kwargs):
        estimate = estimate_input(kwargs.get('messages', [])) + estimate_input(kwargs.get('tools', []))
        if estimate > MAX_INPUT_ESTIMATE:
            raise ModelBudgetExceeded('本次模型输入过长，请缩小资料或任务范围后重试。')
        output_key = 'max_tokens' if 'max_tokens' in kwargs else 'max_completion_tokens'
        output = min(int(kwargs.get(output_key, MAX_OUTPUT_TOKENS)), MAX_OUTPUT_TOKENS)
        if output < 1:
            raise ModelBudgetExceeded('模型输出预算必须为正数。')
        kwargs[output_key] = output
        event_key = MODEL_EVENT.get() or 'direct-model:' + uuid.uuid4().hex
        waiting_since = time.monotonic()
        acquired = False
        call_id = None
        try:
            while not acquired:
                self._ensure_active()
                if time.monotonic() - waiting_since > 180:
                    raise ModelBudgetExceeded('模型服务繁忙，等待超过本次上限，请稍后重试。')
                acquired = MODEL_SLOTS.acquire(timeout=0.2)
            while call_id is None:
                self._ensure_active()
                call_id = self._reserve(event_key, str(kwargs.get('model', '')), estimate, output)
                if call_id is None:
                    if time.monotonic() - waiting_since > 180:
                        raise ModelBudgetExceeded('模型服务繁忙，等待超过本次上限，请稍后重试。')
                    time.sleep(0.2)
            started = time.monotonic()
            result, error = None, None
            try:
                kwargs['timeout'] = min(float(kwargs.get('timeout', 90)), 90.0, self._ensure_active())
                result = self.client.chat.completions.create(**kwargs)
                self._ensure_active()
                return result
            except BaseException as exc:
                error = exc
                raise
            finally:
                self._finish(call_id, started, result, error)
        finally:
            if acquired:
                MODEL_SLOTS.release()

    def close(self):
        self.client.close()
