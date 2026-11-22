"""Best-effort, content-free diagnostics tied to existing events and their lifecycle."""
from datetime import datetime, timezone
import json
import logging
import re
from contextvars import ContextVar
from pathlib import Path
import hashlib

SEARCH_TRACE = ContextVar('search_trace', default=None)


class SearchTrace:
    """Explicit local diagnostic capture; absent from ordinary request execution."""
    def __init__(self, root, *, max_requests=50):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=False)
        (self.root / 'responses').mkdir()
        self.max_requests, self.requests = max_requests, 0

    def __enter__(self):
        self.token = SEARCH_TRACE.set(self)
        return self

    def __exit__(self, *error):
        SEARCH_TRACE.reset(self.token)
        self.record('manifest', {'physical_requests':self.requests, 'limit':self.max_requests,
                                'finished':not bool(error[0])})

    def record(self, stage, value):
        import json
        try:
            with (self.root / (stage + '.jsonl')).open('a', encoding='utf-8') as output:
                output.write(json.dumps(value, ensure_ascii=False) + '\n')
        except OSError as error:
            raise RuntimeError('diagnostic_capture_unavailable') from error

    def before_request(self, url, method):
        if self.requests >= self.max_requests:
            raise RuntimeError('diagnostic_http_budget_exhausted')
        self.requests += 1
        self.record('http-attempts', {'attempt':self.requests, 'url':url, 'method':method})

    def response(self, url, body, mime):
        # Never receives request headers, credentials or private conversation context.
        name = f'{self.requests:03d}-' + hashlib.sha256(body).hexdigest()[:16]
        try:
            (self.root / 'responses' / name).write_bytes(body)
        except OSError as error:
            raise RuntimeError('diagnostic_capture_unavailable') from error
        self.record('responses', {'attempt':self.requests, 'url':url, 'mime':mime,
                                 'file':'responses/'+name, 'bytes':len(body), 'complete':True})

from core.execution_scope import CURRENT_EXECUTION
from core.model_budget import MODEL_EVENT, MODEL_RUN

logger = logging.getLogger(__name__)
FIELDS = frozenset('status purpose prompt_version prompt_hash stage_id asset_version asset_bundle_hash system_hash '
    'context_policy_version validator_version model_id call_id '
    'route_owner decision_source answer_status fact_count execution_status facts_hash '
    'version covered_turn_id reason pending_turns pending_chars item_count repair_attempt input_user_chars '
    'previous_reused error_type error_location summary_version summary_prompt_version summary_prompt_hash summary_injected '
    'summary_hash summary_chars summary_truncated recent_turns group_chars document_chars total_chars '
    'name target_host http_status error_code attempts effect_status state_version output_limit input_estimate request_bytes '
    'elapsed_seconds finish_reason attempt_count outbox_id candidate_count operation_key '
    'summary_pending_chars summary_information_incomplete schema_version plan_resolution '
    'scope_kind plan_version matched_count unmatched_count target_count topic_count covered_count remaining_count batch_count day_corrected_count day_unresolved_count '
    'platform_fact_count conflict_count amap_requests read_pages search_requests physical_calls elapsed_ms bytes usage cost ignored_date_count city_source '
    'direction provider_alias connection_id profile_id config_version logical_count mcp_submissions http_requests completeness phase '
    'cause_type root_cause_type timeout_phase timeout_seconds upstream_request_id network_policy trust_env proxy_env_set no_proxy_env_set '
    'fallback_from force_default_model target_id'.split())


def metadata(details):
    clean = {}
    for key in FIELDS:
        value = details.get(key)
        if type(value) in (bool, int, float) or value is None:
            if key in details:
                clean[key] = value
        elif isinstance(value, str) and re.fullmatch(r'[\w./:\[\]-]{0,160}', value, re.ASCII) and '://' not in value:
            clean[key] = value
    validation = details.get('validation')
    parts=details.get('input_parts')
    if isinstance(parts,dict):
        clean['input_parts']={key:value for key,value in parts.items() if isinstance(key,str)
            and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}',key) and type(value) is int and value>=0}
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
            from infrastructure.execution_report import STAGES
            if event_key and stage in STAGES:
                row=self._write(event_key, stage, metadata(details))
                if row:
                    from infrastructure.execution_report import explain
                    summary=explain(row)
                    identifier=str(row['job_id']) if row['job_id'] is not None else hashlib.sha256(event_key.encode()).hexdigest()[:8]
                    workflow=logging.getLogger('travel.workflow')
                    level=logging.WARNING if summary['warning'] else logging.INFO
                    workflow.log(level,'[请求 %s] %s',identifier,summary['description'],
                        extra={'workflow':True,'workflow_details':{'stage':stage,'run_key':MODEL_RUN.get(),**row['details']}})
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
            if ((stage=='request' and details.get('status') in {'received','claimed','output_prepared','cancelled'})
                    or (stage=='delivery' and details.get('status')=='sent')):
                existing=db.execute("SELECT 1 FROM execution_diagnostics WHERE event_key=? AND stage=? AND json_extract(details_json,'$.status')=? AND coalesce(json_extract(details_json,'$.outbox_id'),0)=? LIMIT 1",
                    (event_key,stage,details['status'],details.get('outbox_id',0))).fetchone()
                if existing:return None
            created=datetime.now(timezone.utc).isoformat()
            cursor=db.execute('INSERT INTO execution_diagnostics '
                '(event_key,platform,scope_id,job_id,run_key,stage,details_json,created_at) VALUES (?,?,?,?,?,?,?,?)',
                (event_key, source['platform'], source['scope_id'], job['id'] if job else scope.job_id if scope else None,
                 MODEL_RUN.get(), stage, json.dumps(details, ensure_ascii=False, allow_nan=False),
                 created))
            return {'id':cursor.lastrowid,'stage':stage,'created_at':created,'job_id':job['id'] if job else None,
                    'details':details,'run_key':MODEL_RUN.get()}
