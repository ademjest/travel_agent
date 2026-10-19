from dataclasses import replace
from contextlib import closing
import time
import random

from core.execution_scope import ensure_execution_active, CURRENT_EXECUTION
from core.tool_result import ToolResult


def retry_read(operation, max_attempts=3, sleeper=time.sleep, jitter=None):
    """Only typed transient failures retry; execution fences apply before/after each call."""
    for attempt in range(1,max_attempts+1):
        ensure_execution_active()
        result = operation()
        ensure_execution_active()
        result = replace(result,attempts=attempt)
        if result.status != 'failed' or not result.retryable or attempt == max_attempts:
            return result
        if result.retry_after_seconds is not None:
            delay = max(0, result.retry_after_seconds)
            if delay > 30:
                return result
        else:
            limit = min(30, 2**attempt)
            delay = (jitter or (lambda cap: random.uniform(0, cap)))(limit)
        scope = CURRENT_EXECUTION.get()
        if scope:
            import sqlite3
            from datetime import datetime, timezone
            with closing(sqlite3.connect(scope.database_path)) as db:
                row = db.execute('SELECT deadline_at FROM inbox_jobs WHERE id=?',(scope.job_id,)).fetchone()
            if not row or (datetime.fromisoformat(row[0])-datetime.now(timezone.utc)).total_seconds() <= delay+10:
                return result
        sleeper(delay)
    return result
