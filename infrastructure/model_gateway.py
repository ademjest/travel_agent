from datetime import datetime, timedelta, timezone
import json
import logging
import os
import threading
import time
from types import SimpleNamespace
import uuid

from core.execution_scope import CURRENT_EXECUTION
from core.model_budget import MODEL_CALL, MODEL_EVENT, ModelCallContext, ModelBudgetExceeded
from core.model_policy import current_policy, model_for_purpose, output_limit_for_purpose
from core.model_pricing import pricing_config, estimate_cost
from infrastructure.model_accounting import initialize_accounting, period_keys, reserve_ledger, settle_ledger, ledger_report


logger = logging.getLogger(__name__)
MODEL_SLOTS = threading.BoundedSemaphore(2)
MAX_MODEL_CALLS_PER_EVENT = 12
MAX_INPUT_ESTIMATE = 64_000
MAX_EVENT_ESTIMATE = 256_000
MAX_OUTPUT_TOKENS = 8192
MAX_REQUEST_BYTES = 12 * 1024 * 1024
DEFAULT_PRICE_CURRENCY = 'USD'


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


def _pricing_config(model=None, default_model=None):
    return pricing_config(model, default_model)


def _row_value(row, name, default=None):
    return row[name] if name in row.keys() else default


def _cost_for_row(row, pricing):
    persisted = _row_value(row, 'estimated_cost')
    stored_currency = (_row_value(row, 'cost_currency', '') or '').strip().upper()
    if persisted is not None:
        return {'cost': float(persisted), 'currency': stored_currency or pricing['currency'],
                'origin': 'persisted', 'currency_inferred': int(not stored_currency),
                'cache_detail_missing': int(_row_value(row,'cached_input_tokens') is None
                                            and type(_row_value(row,'prompt_tokens')) is int), 'unknown': ''}
    prompt, completion = _row_value(row, 'prompt_tokens'), _row_value(row, 'completion_tokens')
    raw_cached = _row_value(row,'cached_input_tokens')
    if (type(prompt) is not int or type(completion) is not int or prompt<0 or completion<0
            or (raw_cached is not None and (type(raw_cached) is not int or not 0<=raw_cached<=prompt))):
        return {'cost': None, 'currency': '', 'origin': '', 'currency_inferred': 0,
                'cache_detail_missing': 0, 'unknown': 'usage'}
    input_price = _row_value(row, 'input_price_per_m')
    output_price = _row_value(row, 'output_price_per_m')
    cached_price = _row_value(row, 'cached_input_price_per_m')
    snapshot = input_price is not None and output_price is not None
    input_price = float(input_price) if snapshot else pricing['input']
    output_price = float(output_price) if snapshot else pricing['output']
    cached_price = float(cached_price) if snapshot and cached_price is not None else (
        input_price if snapshot else pricing['cached'])
    if input_price is None or output_price is None:
        return {'cost': None, 'currency': '', 'origin': '', 'currency_inferred': 0,
                'cache_detail_missing': 0, 'unknown': 'pricing'}
    cached = _row_value(row, 'cached_input_tokens')
    cache_detail_missing = int(type(cached) is not int)
    cached = cached if type(cached) is int else 0
    cost = estimate_cost(prompt, cached, completion,
                         {'input':input_price,'cached':cached_price,'output':output_price})
    if cost is None:
        return {'cost':None,'currency':'','origin':'','currency_inferred':0,
                'cache_detail_missing':cache_detail_missing,'unknown':'usage'}
    currency = stored_currency or pricing['currency']
    return {'cost': cost, 'currency': currency, 'origin': 'recalculated',
            'currency_inferred': int(not stored_currency),
            'cache_detail_missing': cache_detail_missing, 'unknown': ''}


def _empty_usage():
    return {'calls': 0, 'prompt_tokens': 0, 'cached_input_tokens': 0, 'completion_tokens': 0,
            'reasoning_tokens': 0, 'estimated_cost': 0.0, 'cost_unknown': 0,
            'cost_recalculated': 0, 'cache_detail_missing': 0, 'currency_inferred': 0,
            'unknown_usage': 0, 'unknown_pricing': 0, 'usage_missing': 0, '_costs': {}}


def _add_usage(target, row, pricing):
    target['calls'] += 1
    for source, key in (('prompt_tokens','prompt_tokens'), ('cached_input_tokens','cached_input_tokens'),
                        ('completion_tokens','completion_tokens'), ('reasoning_tokens','reasoning_tokens')):
        value = _row_value(row, source)
        if (type(value) is int and value>=0
                and (source!='cached_input_tokens' or (type(_row_value(row,'prompt_tokens')) is int
                                                       and value<=row['prompt_tokens']))):
            target[key] += value
    result = _cost_for_row(row, pricing)
    if result['cost'] is None:
        target['cost_unknown'] += 1
        if result['unknown'] == 'usage':
            target['unknown_usage'] += 1
            target['usage_missing'] += 1
        else:
            target['unknown_pricing'] += 1
        return
    target['_costs'][result['currency']] = target['_costs'].get(result['currency'], 0.0) + result['cost']
    target['cost_recalculated'] += int(result['origin'] == 'recalculated')
    target['cache_detail_missing'] += result['cache_detail_missing']
    target['currency_inferred'] += result['currency_inferred']


def _finalize_usage(value):
    costs = value.pop('_costs')
    breakdown = [{'currency': currency, 'amount': amount} for currency, amount in sorted(costs.items())]
    value['cost_breakdown'] = breakdown
    value['cost_mixed'] = len(breakdown) > 1
    value['currency'] = breakdown[0]['currency'] if len(breakdown) == 1 else ''
    value['estimated_cost'] = breakdown[0]['amount'] if len(breakdown) == 1 else 0.0
    return value


def _usage_aggregates(connection, rows=None):
    pricing = _pricing_config()
    totals, groups = _empty_usage(), {}
    if rows is None:
        rows = connection.execute('SELECT * FROM model_calls ORDER BY started_at').fetchall()
    for row in rows:
        row_pricing = _pricing_config(row['model_id'])
        if _row_value(row,'provider_alias','') != row_pricing['provider']:
            row_pricing = {**row_pricing,'input':None,'cached':None,'output':None}
        _add_usage(totals, row, row_pricing)
        key = (_row_value(row, 'purpose', 'unspecified') or 'unspecified', row['model_id'])
        group = groups.setdefault(key, _empty_usage())
        _add_usage(group, row, row_pricing)
    purposes = []
    for (purpose, model_id), value in groups.items():
        purposes.append({'purpose': purpose, 'model_id': model_id, **_finalize_usage(value)})
    purposes.sort(key=lambda row: (-row['calls'], row['purpose'], row['model_id']))
    return _finalize_usage(totals), purposes, pricing


def usage_summary(store):
    with store._connect() as connection:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_calls'").fetchone():
            return _finalize_usage(_empty_usage())
        totals, _, _ = _usage_aggregates(connection)
        return totals


def usage_report(store):
    policy = current_policy()
    with store._connect() as connection:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_calls'").fetchone():
            totals, purposes, pricing = _finalize_usage(_empty_usage()), [], _pricing_config()
            day_reserved = month_reserved = 0
        else:
            totals, purposes, pricing = _usage_aggregates(connection)
            today = datetime.now(timezone.utc).date()
            has_ledger=connection.execute("SELECT 1 FROM sqlite_master WHERE name='model_accounting_settings'").fetchone()
            day_key,month_key=period_keys(connection,datetime.now(timezone.utc)) if has_ledger else (today.isoformat(),today.replace(day=1).isoformat())
            if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_usage_periods'").fetchone():
                day_reserved = connection.execute("SELECT coalesce(sum(reserved_tokens),0) FROM model_usage_periods WHERE period_type='day' AND period_start=?",
                                                  (day_key,)).fetchone()[0]
                month_reserved = connection.execute("SELECT coalesce(sum(reserved_tokens),0) FROM model_usage_periods WHERE period_type='month' AND period_start=?",
                                                    (month_key,)).fetchone()[0]
            else:
                day_reserved = month_reserved = 0
        accounting=ledger_report(connection)
        corrections = historical_cost_corrections(connection)
    return {'totals': totals, 'purposes': purposes,
            'currency': totals['currency'] or pricing['currency'],
            'pricing_active': pricing['input'] is not None and pricing['output'] is not None,
            'budget': {'tier': policy.tier, 'daily_limit': policy.daily_token_limit,
                       'monthly_limit': policy.monthly_token_limit,
                       'day_reserved': day_reserved, 'month_reserved': month_reserved, **accounting,
                       'daily_cost_limit':policy.daily_cost_limit,'monthly_cost_limit':policy.monthly_cost_limit,
                       'cost_currency':policy.cost_currency},
            'cost_note': _usage_note(totals, pricing), 'historical_corrections':corrections}


def _usage_note(totals, pricing):
    if not totals['calls']:
        return '尚无模型调用记录。'
    notes = ['费用是本地估算，不等同于供应商账单。']
    if totals['cost_recalculated']:
        notes.append(f"{totals['cost_recalculated']} 次历史调用按当前生效费率回算。")
    if totals['cache_detail_missing']:
        notes.append(f"{totals['cache_detail_missing']} 次调用缺少缓存拆分，全部输入按普通输入费率保守估算；不能据此判断缓存未命中。")
    if totals['unknown_usage']:
        notes.append(f"{totals['unknown_usage']} 次缺少供应商 usage，仍无法估算。")
    if totals['unknown_pricing']:
        notes.append(f"{totals['unknown_pricing']} 次因当前输入或输出费率不完整而无法估算。")
    if totals['currency_inferred']:
        notes.append(f"{totals['currency_inferred']} 次历史记录未保存币种，按当前币种 {pricing['currency']} 展示。")
    if totals['cost_mixed']:
        notes.append('存在多种币种，费用已分币种显示，未合并为单一总额。')
    return ''.join(notes)


def conversation_usage_report(store, identity, owner):
    """Use exact Web scope boundaries, including legacy calls without inbox job IDs."""
    prefix = f'web:group:{identity}:'
    with store._connect() as connection:
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_calls'").fetchone():
            rows = connection.execute('''SELECT * FROM model_calls WHERE
                substr(event_key,1,?)=? OR job_id IN
                (SELECT id FROM inbox_jobs WHERE platform='web' AND scope_id=? AND owner_id=?)
                ORDER BY started_at''', (len(prefix), prefix, identity, owner)).fetchall()
            totals, purposes, pricing = _usage_aggregates(connection, rows)
        else:
            rows = []
            totals, purposes, pricing = _finalize_usage(_empty_usage()), [], _pricing_config()
    totals['total_tokens'] = totals['prompt_tokens'] + totals['completion_tokens']
    totals['running_calls'] = sum(row['status'] == 'running' for row in rows)
    return {'conversation_id': identity, 'totals': totals, 'purposes': purposes,
            'currency': totals['currency'] or pricing['currency'],
            'cost_note': _usage_note(totals, pricing),
            'scope_note': '仅统计当前会话保留的模型调用；输入包含缓存输入，输出包含推理 token，均不重复计入总量。'}


def historical_cost_corrections(connection):
    """Read-only proposed corrections; never rewrite snapshots or quota counters."""
    columns = {row['name'] for row in connection.execute('PRAGMA table_info(model_calls)')}
    required = {'estimated_cost','cost_currency','input_price_per_m','output_price_per_m',
                'cached_input_price_per_m','cached_input_tokens','cost_estimation_version'}
    if not required <= columns:
        return {'rule_version':3,'items':[],'truncated':False,'applied':False}
    rows = connection.execute('''SELECT * FROM model_calls WHERE estimated_cost IS NOT NULL
        AND cost_estimation_version<3 AND cached_input_price_per_m=0 AND cached_input_tokens>0
        AND input_price_per_m IS NOT NULL AND output_price_per_m IS NOT NULL
        AND cost_currency<>'' ORDER BY started_at DESC LIMIT 101''').fetchall()
    items = []
    for row in rows[:100]:
        corrected = estimate_cost(row['prompt_tokens'],row['cached_input_tokens'],row['completion_tokens'],
                                  {'input':row['input_price_per_m'],'cached':0,'output':row['output_price_per_m']})
        if corrected is not None and abs(corrected-row['estimated_cost']) > 1e-12:
            items.append({'call_id':row['call_id'],'original':row['estimated_cost'],'corrected':corrected,
                          'delta':corrected-row['estimated_cost'],'currency':row['cost_currency']})
    return {'rule_version':3,'items':items,'truncated':len(rows)>100,'applied':False}


class ModelGateway:
    def __init__(self, store, client, *, default_model=None):
        self.store = store
        self.client = client
        self.default_model = default_model
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
        self.embeddings = SimpleNamespace(create=self.create_embedding)
        with store._connect() as connection:
            connection.execute('''CREATE TABLE IF NOT EXISTS model_calls (
                call_id TEXT PRIMARY KEY, event_key TEXT NOT NULL, job_id INTEGER,
                model_id TEXT NOT NULL, status TEXT NOT NULL, input_estimate INTEGER NOT NULL,
                reserved_tokens INTEGER NOT NULL, prompt_tokens INTEGER, completion_tokens INTEGER,
                started_at TEXT NOT NULL, finished_at TEXT, elapsed_seconds REAL, error_type TEXT NOT NULL DEFAULT '',
                purpose TEXT NOT NULL DEFAULT 'unspecified', provider_alias TEXT NOT NULL DEFAULT '',
                config_version TEXT NOT NULL DEFAULT '', prompt_version TEXT NOT NULL DEFAULT '',
                context_policy_version TEXT NOT NULL DEFAULT '', output_limit INTEGER NOT NULL DEFAULT 0,
                cached_input_tokens INTEGER, reasoning_tokens INTEGER, repair_attempt INTEGER NOT NULL DEFAULT 0,
                fallback_from TEXT NOT NULL DEFAULT '', usage_complete INTEGER NOT NULL DEFAULT 0,
                estimated_cost REAL, cost_currency TEXT NOT NULL DEFAULT '',
                input_price_per_m REAL, cached_input_price_per_m REAL, output_price_per_m REAL,
                pricing_version TEXT NOT NULL DEFAULT '', cost_estimation_version INTEGER NOT NULL DEFAULT 1,
                cost_origin TEXT NOT NULL DEFAULT '',
                usage_accounted INTEGER NOT NULL DEFAULT 0, pricing_json TEXT NOT NULL DEFAULT '{}'
            )''')
            connection.execute('''CREATE TABLE IF NOT EXISTS model_usage_periods (
                period_type TEXT NOT NULL, period_start TEXT NOT NULL,
                purpose TEXT NOT NULL, model_id TEXT NOT NULL,
                calls INTEGER NOT NULL DEFAULT 0, reserved_tokens INTEGER NOT NULL DEFAULT 0,
                prompt_tokens INTEGER NOT NULL DEFAULT 0, cached_input_tokens INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0, reasoning_tokens INTEGER NOT NULL DEFAULT 0,
                estimated_cost REAL NOT NULL DEFAULT 0, cost_unknown INTEGER NOT NULL DEFAULT 0,
                cost_currency TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(period_type, period_start, purpose, model_id)
            )''')
            columns = {row['name'] for row in connection.execute('PRAGMA table_info(model_calls)').fetchall()}
            additions = {
                'purpose': "TEXT NOT NULL DEFAULT 'unspecified'",
                'provider_alias': "TEXT NOT NULL DEFAULT ''",
                'config_version': "TEXT NOT NULL DEFAULT ''",
                'prompt_version': "TEXT NOT NULL DEFAULT ''",
                'context_policy_version': "TEXT NOT NULL DEFAULT ''",
                'output_limit': 'INTEGER NOT NULL DEFAULT 0',
                'cached_input_tokens': 'INTEGER',
                'reasoning_tokens': 'INTEGER',
                'repair_attempt': 'INTEGER NOT NULL DEFAULT 0',
                'fallback_from': "TEXT NOT NULL DEFAULT ''",
                'usage_complete': 'INTEGER NOT NULL DEFAULT 0',
                'estimated_cost': 'REAL',
                'cost_currency': "TEXT NOT NULL DEFAULT ''",
                'input_price_per_m': 'REAL',
                'cached_input_price_per_m': 'REAL',
                'output_price_per_m': 'REAL',
                'pricing_version': "TEXT NOT NULL DEFAULT ''",
                'cost_estimation_version': 'INTEGER NOT NULL DEFAULT 1',
                'cost_origin': "TEXT NOT NULL DEFAULT ''",
                'usage_accounted': 'INTEGER NOT NULL DEFAULT 0',
                'pricing_json': "TEXT NOT NULL DEFAULT '{}'",
                'estimate_method': "TEXT NOT NULL DEFAULT 'utf8-byte-bound-v1'",
                'request_bytes': 'INTEGER NOT NULL DEFAULT 0',
                'input_parts_json': "TEXT NOT NULL DEFAULT '{}'",
                'finish_reason': "TEXT NOT NULL DEFAULT ''",
            }
            for name, definition in additions.items():
                if name not in columns:
                    connection.execute(f'ALTER TABLE model_calls ADD COLUMN {name} {definition}')
            connection.execute('CREATE INDEX IF NOT EXISTS idx_model_calls_event ON model_calls(event_key, started_at)')
            connection.execute('CREATE INDEX IF NOT EXISTS idx_model_calls_purpose ON model_calls(purpose, started_at)')
            initialize_accounting(connection)

    def _ensure_active(self):
        scope = CURRENT_EXECUTION.get()
        if scope:
            with self.store._connect() as connection:
                scope.validate(connection)
                row = connection.execute('SELECT deadline_at FROM inbox_jobs WHERE id=?', (scope.job_id,)).fetchone()
                return max(0.01, (datetime.fromisoformat(row['deadline_at']) - datetime.now(timezone.utc)).total_seconds())
        return 90.0

    def _reserve(self, event_key, model, estimate, output, call_context=None, pricing=None, metadata=None):
        call_context = call_context or ModelCallContext()
        pricing = pricing if pricing is not None else _pricing_config(model)
        now = datetime.now(timezone.utc)
        scope = CURRENT_EXECUTION.get()
        policy = current_policy()
        with self.store._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            active = connection.execute("SELECT count(*) FROM model_calls WHERE status='running' AND started_at>?",
                                        ((now-timedelta(seconds=300)).isoformat(),)).fetchone()[0]
            if active >= 2:
                return None
            row = connection.execute('SELECT count(*) AS calls, coalesce(sum(reserved_tokens),0) AS reserved FROM model_calls WHERE event_key=?', (event_key,)).fetchone()
            if row['calls'] >= MAX_MODEL_CALLS_PER_EVENT or row['reserved'] + estimate + output > MAX_EVENT_ESTIMATE:
                raise ModelBudgetExceeded('本次任务的模型调用或总预算已达到上限。请缩小本次任务范围后重试。')
            day_start,month_start = period_keys(connection,now)
            if policy.daily_token_limit:
                used = connection.execute("SELECT coalesce(sum(reserved_tokens),0) FROM model_usage_periods WHERE period_type='day' AND period_start=?",
                                          (day_start,)).fetchone()[0]
                if used + estimate + output > policy.daily_token_limit:
                    raise ModelBudgetExceeded('今日模型 token 预算已达到上限，请稍后再试或调整预算设置。')
            if policy.monthly_token_limit:
                used = connection.execute("SELECT coalesce(sum(reserved_tokens),0) FROM model_usage_periods WHERE period_type='month' AND period_start=?",
                                          (month_start,)).fetchone()[0]
                if used + estimate + output > policy.monthly_token_limit:
                    raise ModelBudgetExceeded('本月模型 token 预算已达到上限，请调整预算设置。')
            call_id = uuid.uuid4().hex
            reserve_ledger(connection,call_id,event_key,call_context,model,estimate,output,pricing,now)
            connection.execute('''INSERT INTO model_calls
                (call_id, event_key, job_id, model_id, status, input_estimate, reserved_tokens, started_at,
                 purpose, provider_alias, config_version, prompt_version, context_policy_version, output_limit,
                 repair_attempt, fallback_from)
                VALUES (?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (call_id, event_key, scope.job_id if scope else None, model, estimate, estimate+output, now.isoformat(),
                 call_context.purpose, os.getenv('LLM_PROVIDER_ALIAS', '').strip(),
                 os.getenv('LLM_CONFIG_VERSION', '').strip(), call_context.prompt_version,
                 call_context.context_policy_version, output, call_context.repair_attempt,
                 call_context.fallback_from))
            connection.execute('UPDATE model_calls SET pricing_json=? WHERE call_id=?',
                               (json.dumps(pricing),call_id))
            if metadata:
                connection.execute('UPDATE model_calls SET request_bytes=?,input_parts_json=? WHERE call_id=?',
                                   (metadata['request_bytes'],json.dumps(metadata['parts']),call_id))
            for period_type, period_start in (('day', day_start), ('month', month_start)):
                connection.execute('''INSERT INTO model_usage_periods
                    (period_type,period_start,purpose,model_id,calls,reserved_tokens)
                    VALUES (?,?,?,?,1,?) ON CONFLICT(period_type,period_start,purpose,model_id)
                    DO UPDATE SET calls=calls+1,reserved_tokens=reserved_tokens+excluded.reserved_tokens''',
                    (period_type, period_start, call_context.purpose, model, estimate + output))
        return call_id

    @staticmethod
    def _detail(value, key):
        if value is None:
            return None
        if isinstance(value, dict):
            result = value.get(key)
        else:
            result = getattr(value, key, None)
        return result if type(result) is int else None

    def _finish(self, call_id, started, result, error):
        usage = getattr(result, 'usage', None)
        prompt = getattr(usage, 'prompt_tokens', None)
        completion = getattr(usage, 'completion_tokens', None)
        cached = self._detail(getattr(usage, 'prompt_tokens_details', None), 'cached_tokens')
        reasoning = self._detail(getattr(usage, 'completion_tokens_details', None), 'reasoning_tokens')
        choices = getattr(result,'choices',None)
        finish_reason = getattr(choices[0],'finish_reason','') if isinstance(choices,(list,tuple)) and choices else ''
        finish_reason = finish_reason if isinstance(finish_reason,str) else ''
        # Cost telemetry is recorded even when the business job has been revoked.
        token = CURRENT_EXECUTION.set(None)
        try:
            with self.store._connect() as connection:
                settled=settle_ledger(connection,call_id,prompt,cached,completion,reasoning)
                if settled is None:
                    return
                pricing=settled['pricing']
                input_price,cached_price,output_price = pricing['input'],pricing['cached'],pricing['output']
                estimated_cost = settled['cost']
                currency = pricing['currency'] if estimated_cost is not None else ''
                connection.execute('''UPDATE model_calls SET status=?, prompt_tokens=?, completion_tokens=?,
                    cached_input_tokens=?, reasoning_tokens=?, usage_complete=?, estimated_cost=?, cost_currency=?,
                    input_price_per_m=?,cached_input_price_per_m=?,output_price_per_m=?,pricing_version=?,
                    cost_estimation_version=3,cost_origin=?,
                    usage_accounted=1, finished_at=?, elapsed_seconds=?, error_type=?,finish_reason=? WHERE call_id=?''',
                    ('failed' if error else 'completed', prompt if type(prompt) is int else None,
                     completion if type(completion) is int else None, cached, reasoning, int(settled['valid']),
                     estimated_cost, currency, input_price, cached_price, output_price, pricing['version'],
                     'live' if estimated_cost is not None else '',
                     datetime.now(timezone.utc).isoformat(),
                     time.monotonic()-started, type(error).__name__ if error else '', finish_reason, call_id))
        except Exception:
            logger.warning('Model usage telemetry could not be persisted: call_id=%s', call_id)
        finally:
            CURRENT_EXECUTION.reset(token)

    def create(self, **kwargs):
        try:
            return self._create(**kwargs)
        except ModelBudgetExceeded:
            self.store.diagnostics.record('model', {'status': 'budget_blocked',
                'purpose': (MODEL_CALL.get() or ModelCallContext()).purpose})
            raise

    def _create(self, **kwargs):
        call_context = MODEL_CALL.get() or ModelCallContext()
        default_model = self.default_model or str(kwargs.get('model',''))
        if not call_context.force_default_model:
            kwargs['model'] = model_for_purpose(str(kwargs.get('model', '')), call_context.purpose)
        estimate = estimate_input(kwargs.get('messages', [])) + estimate_input(kwargs.get('tools', []))
        request_bytes = len(json.dumps(kwargs,ensure_ascii=False,default=str).encode('utf-8'))
        if request_bytes > MAX_REQUEST_BYTES:
            raise ModelBudgetExceeded('完整模型请求超过硬字节上限，请减少图片或资料。')
        parts = {'tools':estimate_input(kwargs.get('tools',[]))}
        for message in kwargs.get('messages',[]):
            role = message.get('role','unknown')
            if role not in {'system','user','assistant','tool'}:
                role = 'unknown'
            parts[role] = parts.get(role,0) + estimate_input(message.get('content',''))
        if estimate > MAX_INPUT_ESTIMATE:
            raise ModelBudgetExceeded('本次模型输入过长，请缩小资料或任务范围后重试。')
        output_key = 'max_tokens' if 'max_tokens' in kwargs else 'max_completion_tokens'
        output = min(int(kwargs.get(output_key, MAX_OUTPUT_TOKENS)), MAX_OUTPUT_TOKENS)
        output = min(output, output_limit_for_purpose(call_context.purpose,MAX_OUTPUT_TOKENS) * call_context.output_multiplier)
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
                call_id = self._reserve(event_key, str(kwargs.get('model', '')), estimate, output, call_context,
                                        _pricing_config(str(kwargs.get('model','')),default_model),
                                        {'request_bytes':request_bytes,'parts':parts})
                if call_id is None:
                    if time.monotonic() - waiting_since > 180:
                        raise ModelBudgetExceeded('模型服务繁忙，等待超过本次上限，请稍后重试。')
                    time.sleep(0.2)
            started = time.monotonic()
            self.store.diagnostics.record('model', {'event_key': event_key, 'call_id': call_id,
                'status': 'started', 'purpose': call_context.purpose, 'prompt_version': call_context.prompt_version,
                'context_policy_version': call_context.context_policy_version, 'model_id': kwargs['model'],
                'input_estimate': estimate, 'output_limit': output, 'request_bytes': request_bytes})
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
                self.store.diagnostics.record('model', {'event_key': event_key, 'call_id': call_id,
                    'status': 'failed' if error else 'completed', 'purpose': call_context.purpose,
                    'error_type': type(error).__name__ if error else '',
                    'elapsed_seconds': round(time.monotonic() - started, 3)})
        finally:
            if acquired:
                MODEL_SLOTS.release()

    def create_embedding(self, *, model, input, **kwargs):
        """Embedding experiments inherit the same execution fence, price snapshot and ledger."""
        from core.model_budget import model_call_context
        estimate=estimate_input(input)
        if estimate>MAX_INPUT_ESTIMATE:
            raise ModelBudgetExceeded('检索索引批次过大，请分批处理。')
        with model_call_context('retrieval_embedding',prompt_version='embedding-v3'):
            self._ensure_active()
            if not MODEL_SLOTS.acquire(timeout=min(10,self._ensure_active())):
                raise ModelBudgetExceeded('模型并发繁忙，请稍后检索。')
            call_id=None
            started=time.monotonic()
            result=None
            error=None
            try:
                call_id=self._reserve(MODEL_EVENT.get() or 'embedding:'+uuid.uuid4().hex,model,estimate,0,
                                      MODEL_CALL.get(),_pricing_config(model,os.getenv('LLM_MODEL_ID','')))
                if call_id is None:
                    raise ModelBudgetExceeded('模型并发繁忙，请稍后检索。')
                result=self.client.embeddings.create(model=model,input=input,timeout=min(90,self._ensure_active()),**kwargs)
                self._ensure_active()
                return result
            except BaseException as exc:
                error=exc
                raise
            finally:
                if call_id:
                    usage=getattr(result,'usage',None)
                    wrapper=SimpleNamespace(usage=SimpleNamespace(prompt_tokens=getattr(usage,'prompt_tokens',None),completion_tokens=0))
                    self._finish(call_id,started,wrapper,error)
                MODEL_SLOTS.release()

    def close(self):
        self.client.close()
