"""The confirmed token/query protocol; one physical submission per owned event."""
from datetime import datetime, timezone
import hashlib
import json
import re
import time
from urllib.parse import quote

from core.execution_scope import CURRENT_EXECUTION, ensure_execution_active
from infrastructure.public_http import PublicHTTPError, request_public

ENDPOINT = 'https://wendao-skill-prod.ctrip.com/skill/query'
MAX_BYTES = 200_000


class WendaoError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__('携程问道未完成（' + code + '）；本事件不会自动重新提交。')


def extract_answer(body, mime):
    try:
        text = body.decode('utf-8-sig')
    except UnicodeError:
        raise WendaoError('invalid_encoding') from None
    if not text.strip():
        raise WendaoError('empty_response')
    if re.search(r'^\s*(?:<!doctype\s+html|<html)', text, re.I):
        raise WendaoError('html_response')
    if mime == 'application/json' or text.lstrip().startswith(('{', '[')):
        try:
            value = json.loads(text)
        except ValueError:
            raise WendaoError('invalid_json') from None
        if not isinstance(value, dict):
            raise WendaoError('invalid_result')
        if value.get('error'):
            raise WendaoError('business_error')
        result = value.get('result')
        text = result.get('content') if isinstance(result, dict) else result
        if not isinstance(text, str) or not text.strip():
            raise WendaoError('invalid_result')
    return text


class WendaoClient:
    def __init__(self, token='', enabled=False, *, transport=request_public, diagnostics=None):
        self.token, self.enabled = token, enabled
        self.transport, self.diagnostics = transport, diagnostics

    @property
    def configured(self):
        return self.enabled and bool(self.token)

    def redact(self, value):
        for secret in {self.token, quote(self.token, safe=''), json.dumps(self.token)[1:-1]} - {''}:
            value = value.replace(secret, '[Token 已隐藏]')
        return value

    def query(self, event, repository, query, *, timeout=90):
        ensure_execution_active()
        previous, _ = repository.wendao_call(event)
        if previous:
            return {**previous, 'reused': True,
                    'status': 'result_unknown' if previous['status'] == 'submitted' else previous['status']}
        if not self.configured:
            raise WendaoError('not_configured')
        if not self.token or any(ord(c) < 33 or ord(c) > 126 for c in self.token):
            raise WendaoError('invalid_token')
        if not isinstance(query, str) or not query.strip() or len(query) > 8000:
            raise WendaoError('invalid_query')
        scope = CURRENT_EXECUTION.get()
        if scope:
            with repository.store._connect() as db:
                job = db.execute('SELECT deadline_at FROM inbox_jobs WHERE id=?', (scope.job_id,)).fetchone()
            timeout = min(timeout, (datetime.fromisoformat(job['deadline_at']) - datetime.now(timezone.utc)).total_seconds() - 50)
        if timeout < 5:
            raise WendaoError('insufficient_budget')
        result = {'status': 'submitted', 'submission_reservations': 1, 'physical_calls': None, 'usage': None, 'cost': None,
                  'query_summary': self.redact(query[:400]), 'query_hash': hashlib.sha256(query.encode()).hexdigest(),
                  'submitted_at': datetime.now(timezone.utc).isoformat(), 'bytes': None}
        previous, reserved = repository.wendao_call(event, reserve=result)
        if not reserved:
            return {**previous, 'reused': True,
                    'status': 'result_unknown' if previous['status'] == 'submitted' else previous['status']}
        started = time.monotonic()
        try:
            body, _, mime = self.transport(ENDPOINT, payload={'token': self.token, 'query': query},
                max_bytes=MAX_BYTES, redirects=0, max_attempts=1, persist=False, timeout=timeout,
                content_types=('text/markdown', 'text/plain', 'application/json'))
            result['physical_calls'] = 1
            if len(body) > MAX_BYTES:
                raise WendaoError('response_limit')
            result.update(bytes=len(body), mime=mime)
            text = self.redact(extract_answer(body, mime))
            result.update(status='completed', text=text, bytes=len(body), mime=mime,
                          content_hash=hashlib.sha256(text.encode()).hexdigest(),
                          retrieved_at=datetime.now(timezone.utc).isoformat())
        except (PublicHTTPError, WendaoError) as error:
            code = error.code
            if isinstance(error, PublicHTTPError):
                result['physical_calls'] = error.attempts
            result.update(status='result_unknown' if code in {'network_error', 'deadline_exceeded', 'response_limit'} else 'failed', error_code=code)
        except (OSError, TimeoutError):
            result.update(status='result_unknown', error_code='network_error')
        result['elapsed_ms'] = round((time.monotonic() - started) * 1000)
        ensure_execution_active()
        repository.finish_wendao(event, result)
        if self.diagnostics:
            self.diagnostics('tool', {'name': 'ctrip_wendao', **{k: result[k] for k in
                ('status', 'physical_calls', 'elapsed_ms', 'bytes', 'usage', 'cost')}})
        return result
