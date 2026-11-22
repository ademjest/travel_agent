"""Bounded HTTPS to a resolved public IP, preserving TLS hostname verification."""
import ipaddress
import json
import time
import random
import math
import hashlib
import base64
import sqlite3
from contextlib import closing
from contextvars import ContextVar
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit, urljoin

import urllib3

from core.execution_scope import ensure_execution_active, CURRENT_EXECUTION
from infrastructure.secure_download import resolve_host


LAST_FETCH_AT = ContextVar('public_fetch_time', default=None)
LAST_FETCH_CHAIN = ContextVar('public_fetch_chain', default=())


class PublicHTTPError(ValueError):
    def __init__(self, code='unavailable', *, retryable=False, retry_after_seconds=None, attempts=0, http_status=None, error_type=''):
        self.code = code
        self.http_status,self.error_type=http_status,error_type
        self.retryable, self.retry_after_seconds, self.attempts = retryable, retry_after_seconds, attempts
        super().__init__('外部服务请求未完成（' + code + '）。')


def retry_after(value):
    if not isinstance(value, str):
        return None
    try:
        return max(0, int(value)) if value.strip().isdigit() else max(0, math.ceil(
            (parsedate_to_datetime(value).astimezone(timezone.utc)-datetime.now(timezone.utc)).total_seconds()))
    except (ValueError, TypeError, OverflowError):
        return None


class PublicReadBudget:
    def __init__(self, url, payload, headers, policy):
        self.scope = CURRENT_EXECUTION.get()
        self.event = self.key = None
        self.attempts = 0
        if self.scope:
            with closing(sqlite3.connect(self.scope.database_path)) as db:
                row = db.execute('SELECT event_key FROM inbox_jobs WHERE id=?', (self.scope.job_id,)).fetchone()
                self.event = row[0] if row else None
                if self.event and not db.execute('SELECT 1 FROM processed_events WHERE event_id=?', (self.event,)).fetchone():
                    self.event = None
            if self.event:
                self.key = hashlib.sha256(json.dumps([self.event,'public-http-v1',url,payload,headers,policy],sort_keys=True).encode()).hexdigest()

    def cached(self):
        if not self.key:
            return None
        with closing(sqlite3.connect(self.scope.database_path)) as db:
            row = db.execute('SELECT result_text FROM event_tool_results WHERE operation_key=?', (self.key,)).fetchone()
        if row:
            value = json.loads(row[0])
            LAST_FETCH_AT.set(value['fetched_at'])
            LAST_FETCH_CHAIN.set(tuple(value.get('redirect_chain', ())))
            return base64.b64decode(value['body']), value['url'], value['mime']

    def reserve(self):
        if self.key:
            with closing(sqlite3.connect(self.scope.database_path)) as db:
                db.execute('BEGIN IMMEDIATE')
                self.scope.validate(db)
                row = db.execute('SELECT attempts FROM http_read_attempts WHERE operation_key=?', (self.key,)).fetchone()
                self.attempts = row[0] if row else 0
                if self.attempts >= 3:
                    raise PublicHTTPError('attempt_limit', attempts=self.attempts)
                db.execute('INSERT INTO http_read_attempts VALUES (?,?,1,?) ON CONFLICT(operation_key) DO UPDATE SET attempts=attempts+1',
                           (self.key,self.event,datetime.now(timezone.utc).isoformat()))
                self.scope.validate(db)
                db.commit()
        if self.attempts >= 3:
            raise PublicHTTPError('attempt_limit', attempts=self.attempts)
        self.attempts += 1

    def save(self, result):
        if self.key:
            data,url,mime=result
            value=json.dumps({'body':base64.b64encode(data).decode(),'url':url,'mime':mime,'fetched_at':LAST_FETCH_AT.get(),
                              'redirect_chain':LAST_FETCH_CHAIN.get()})
            with closing(sqlite3.connect(self.scope.database_path)) as db:
                db.execute('BEGIN IMMEDIATE')
                self.scope.validate(db)
                db.execute('INSERT OR IGNORE INTO event_tool_results (operation_key,event_id,tool_name,arguments_json,result_text,created_at) VALUES (?,?,?,?,?,?)',
                           (self.key,self.event,'public_http','{}',value,datetime.now(timezone.utc).isoformat()))
                self.scope.validate(db)
                db.commit()


def public_address(url, *, allow_http=False):
    try:
        parsed = urlsplit(url)
        if (parsed.scheme not in ({'http','https'} if allow_http else {'https'}) or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.port not in (None, 80 if parsed.scheme=='http' else 443)
                or any(ord(c) < 32 for c in url)):
            raise ValueError()
        addresses = resolve_host(parsed.hostname)
        if not addresses or any(not ipaddress.ip_address(value).is_global for value in addresses):
            raise ValueError()
        return parsed, addresses[0]
    except (ValueError, OSError):
        raise PublicHTTPError('invalid_public_url') from None


def request_public(url, *, payload=None, headers=None, max_bytes=2_000_000, redirects=0,
                   content_types=('application/json',), timeout=20, sleeper=None, diagnostics=None, url_allowed=None,
                   max_attempts=3, persist=True, allow_http=False):
    if allow_http and (payload is not None or headers):
        raise PublicHTTPError('invalid_public_url')
    started = time.monotonic()
    chain = [url]
    LAST_FETCH_CHAIN.set(tuple(chain))
    policy=(max_bytes,redirects,content_types)
    budget = PublicReadBudget(url,payload,headers,(*policy,'public-http-get-v1') if allow_http else policy)
    if not persist:
        budget.key = None
    def emit(status,error=None):
        if diagnostics is not None:
            try:
                diagnostics('tool',{'name':'public_http','status':status,'attempts':budget.attempts,
                    'error_code':error.code if error else '', 'operation_key':budget.key or '',
                    'http_status':error.http_status if error else None,'error_type':error.error_type if error else '',
                    'target_host':urlsplit(url).hostname or '', 'elapsed_ms':round((time.monotonic()-started)*1000)})
            except Exception:
                pass
    ensure_execution_active()
    if url_allowed is not None and not url_allowed(url):
        raise PublicHTTPError('source_policy_rejected')
    cached = budget.cached()
    if cached:
        cached_chain=LAST_FETCH_CHAIN.get() or tuple(dict.fromkeys([url,cached[1]]))
        if url_allowed is not None and any(not url_allowed(hop) for hop in cached_chain):
            raise PublicHTTPError('source_policy_rejected')
        emit('checkpoint_reused')
        LAST_FETCH_CHAIN.set(tuple(cached_chain))
        return cached
    LAST_FETCH_AT.set(None)
    redirected = 0
    last_error = None
    for attempt in range(max_attempts):
        ensure_execution_active()
        if url_allowed is not None and not url_allowed(url):
            raise PublicHTTPError('source_policy_rejected')
        parsed, address = public_address(url,allow_http=True) if allow_http else public_address(url)
        remaining = timeout - (time.monotonic()-started)
        scope = CURRENT_EXECUTION.get()
        if scope:
            with closing(sqlite3.connect(scope.database_path)) as db:
                row = db.execute('SELECT deadline_at FROM inbox_jobs WHERE id=?',(scope.job_id,)).fetchone()
            if row:
                remaining = min(remaining,(datetime.fromisoformat(row[0])-datetime.now(timezone.utc)).total_seconds())
        if remaining <= 0:
            raise PublicHTTPError('deadline_exceeded',attempts=budget.attempts)
        # Resolve once, connect to the validated IP, verify certificate against original hostname.
        pool_options = {'retries':False,'timeout':urllib3.Timeout(connect=min(5, remaining), read=remaining, total=remaining)}
        pool = (urllib3.HTTPConnectionPool(address, port=80, **pool_options) if parsed.scheme=='http' else
            urllib3.HTTPSConnectionPool(address, port=443, server_hostname=parsed.hostname,
                assert_hostname=parsed.hostname, cert_reqs='CERT_REQUIRED', **pool_options))
        response = None
        try:
            body = json.dumps(payload).encode() if payload is not None else None
            request_headers = {'Host': parsed.hostname, 'Accept-Encoding': 'identity',
                               'User-Agent': 'BeyondTravel/1.0', **(headers or {})}
            if body is not None:
                request_headers['Content-Type'] = 'application/json'
            budget.reserve()
            from infrastructure.execution_diagnostics import SEARCH_TRACE
            trace = SEARCH_TRACE.get()
            if trace is not None:
                trace.before_request(url, 'POST' if body is not None else 'GET')
            response = pool.urlopen('POST' if body is not None else 'GET',
                (parsed.path or '/') + ('?' + parsed.query if parsed.query else ''),
                body=body, headers=request_headers, preload_content=False, redirect=False, retries=False)
            if response.status in (301, 302, 303, 307, 308):
                if headers or payload is not None or redirected >= redirects:
                    raise PublicHTTPError('redirect_rejected')
                url = urljoin(url, response.headers.get('Location', ''))
                chain.append(url)
                LAST_FETCH_CHAIN.set(tuple(chain))
                redirected += 1
                continue
            if not 200 <= response.status < 300:
                raise PublicHTTPError({401: 'unauthorized', 403: 'forbidden', 404:'not_found',429: 'rate_limited'}.get(response.status, 'http_error'),
                    retryable=response.status==429 or 500<=response.status<600,
                    retry_after_seconds=retry_after(response.headers.get('Retry-After')),http_status=response.status)
            mime = response.headers.get('Content-Type', '').split(';')[0].strip().lower()
            if mime not in content_types:
                raise PublicHTTPError('unsupported_content')
            chunks, length = [], 0
            for chunk in response.stream(16384, decode_content=True):
                ensure_execution_active()
                length += len(chunk)
                if time.monotonic() - started > timeout:
                    raise PublicHTTPError('read_timeout')
                if length > max_bytes:
                    raise PublicHTTPError('response_limit')
                chunks.append(chunk)
            result = b''.join(chunks), url, mime
            if trace is not None:
                trace.response(url, result[0], mime)
            ensure_execution_active()
            LAST_FETCH_AT.set(datetime.now(timezone.utc).isoformat())
            budget.save(result)
            emit('completed')
            return result
        except PublicHTTPError as error:
            last_error = error
        except urllib3.exceptions.SSLError as error:
            last_error = PublicHTTPError('tls_error',error_type=type(error).__name__)
        except urllib3.exceptions.ConnectTimeoutError as error:
            last_error = PublicHTTPError('connect_error',retryable=True,error_type=type(error).__name__)
        except urllib3.exceptions.TimeoutError as error:
            last_error = PublicHTTPError('read_timeout',retryable=True,error_type=type(error).__name__)
        except (urllib3.exceptions.HTTPError, OSError, ValueError) as error:
            last_error = PublicHTTPError('network_error',retryable=True,error_type=type(error).__name__)
        finally:
            if response is not None:
                response.close()
            pool.close()
        last_error.attempts = budget.attempts
        emit('failed',last_error)
        if not last_error.retryable or budget.attempts >= max_attempts:
            raise last_error
        delay = last_error.retry_after_seconds
        delay = random.uniform(0,min(4,2**attempt)) if delay is None else delay
        if delay > 30 or delay >= remaining - 1:
            raise last_error
        (sleeper or time.sleep)(delay)
        ensure_execution_active()
    raise last_error or PublicHTTPError('attempt_limit',attempts=budget.attempts)
