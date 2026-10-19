"""Best-effort, content-free diagnostics tied to existing events and their lifecycle."""
from datetime import datetime, timezone
import json
import logging
import re

from core.execution_scope import CURRENT_EXECUTION
from core.model_budget import MODEL_EVENT, MODEL_RUN

logger = logging.getLogger(__name__)
FIELDS = frozenset('status purpose prompt_version prompt_hash context_policy_version validator_version model_id call_id '
    'version covered_turn_id reason pending_turns pending_chars item_count repair_attempt input_user_chars '
    'previous_reused error_type summary_version summary_prompt_version summary_prompt_hash summary_injected '
    'summary_hash summary_chars summary_truncated recent_turns group_chars document_chars total_chars '
    'name error_code attempts effect_status state_version output_limit input_estimate request_bytes '
    'elapsed_seconds finish_reason attempt_count outbox_id candidate_count operation_key '
    'summary_pending_chars summary_information_incomplete schema_version plan_resolution '
    'target_count topic_count covered_count remaining_count batch_count day_corrected_count day_unresolved_count'.split())


def metadata(details):
    clean = {}
    for key in FIELDS:
        value = details.get(key)
        if type(value) in (bool, int, float) or value is None:
            if key in details:
                clean[key] = value
        elif isinstance(value, str) and re.fullmatch(r'[\w./:\[\]-]{0,160}', value, re.ASCII):
            clean[key] = value
    validation = details.get('validation')
    for key in ('adopted_fields', 'ignored_fields'):
        names = details.get(key)
        if isinstance(names, list):
            clean[key] = [n for n in names[:30] if isinstance(n, str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}', n)]
    if isinstance(validation, dict):
        clean['validation'] = {key: value for key, value in validation.items()
            if (key in {'actual', 'max'} and type(value) is int)
            or (key in {'code', 'path'} and isinstance(value, str)
                and re.fullmatch(r'[\w.\[\]-]{1,160}', value, re.ASCII))}
    return clean


class ExecutionDiagnostics:
    def __init__(self, store):
        self.store = store
        with store._connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS execution_diagnostics (
                id INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT NOT NULL,
                platform TEXT NOT NULL, scope_id TEXT NOT NULL, job_id INTEGER,
                run_key TEXT NOT NULL, stage TEXT NOT NULL, details_json TEXT NOT NULL,
                created_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS idx_diagnostics_event ON execution_diagnostics(event_key,id);
                CREATE INDEX IF NOT EXISTS idx_diagnostics_scope ON execution_diagnostics(platform,scope_id);
            ''')

    def record(self, stage, details):
        try:
            event_key = details.get('event_key') or MODEL_EVENT.get()
            scope = CURRENT_EXECUTION.get()
            if not event_key and scope is not None:
                with self.store._connect() as db:
                    job = db.execute('SELECT event_key FROM inbox_jobs WHERE id=?', (scope.job_id,)).fetchone()
                    event_key = job['event_key'] if job else ''
            if event_key and stage in {'request', 'summary', 'context', 'tool', 'model', 'delivery', 'retrieval', 'routing', 'planning'}:
                self._write(event_key, stage, metadata(details))
        except Exception as error:
            logger.warning('Execution diagnostic unavailable: error_type=%s', type(error).__name__)

    def summary(self, details):
        self.record('summary', details)

    def context(self, details):
        self.record('context', details)

    def _write(self, event_key, stage, details):
        with self.store._connect(timeout=0.1) as db:
            db.execute('PRAGMA busy_timeout=100')
            db.execute('BEGIN IMMEDIATE')
            job = db.execute('SELECT id,platform,scope_id FROM inbox_jobs WHERE event_key=?', (event_key,)).fetchone()
            source = job or db.execute('SELECT platform,group_id AS scope_id FROM chat_messages WHERE message_key=?',
                                      (event_key,)).fetchone()
            if source is None:
                return
            lifecycle = getattr(self.store, 'web_lifecycle', None)
            if source['platform'] == 'web' and lifecycle:
                lifecycle.check(source['scope_id'], db)
            scope = CURRENT_EXECUTION.get()
            db.execute('INSERT INTO execution_diagnostics '
                '(event_key,platform,scope_id,job_id,run_key,stage,details_json,created_at) VALUES (?,?,?,?,?,?,?,?)',
                (event_key, source['platform'], source['scope_id'], job['id'] if job else scope.job_id if scope else None,
                 MODEL_RUN.get(), stage, json.dumps(details, ensure_ascii=False, allow_nan=False),
                 datetime.now(timezone.utc).isoformat()))
