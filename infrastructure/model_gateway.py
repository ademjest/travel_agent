from datetime import datetime, timedelta, timezone
import json
import hashlib
import logging
import os
import threading
import time
from types import SimpleNamespace
import uuid

from core.execution_scope import CURRENT_EXECUTION
from core.model_budget import MODEL_CALL, MODEL_EVENT, MODEL_ROUTE, ModelCallContext, ModelBudgetExceeded
from core.model_policy import current_policy, model_for_purpose, output_limit_for_purpose
from core.model_pricing import pricing_config, estimate_cost
from infrastructure.model_accounting import initialize_accounting, period_keys, reserve_ledger, settle_ledger, ledger_report, ledger_occupancy
from infrastructure.model_client import model_failure_metadata
from core.model_config import ModelConfigError


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
    if model is None and 'LLM_CONFIG_JSON' in os.environ:
        from core.model_config import parse_config
        try:
            config=parse_config(os.environ['LLM_CONFIG_JSON'])
            connection,profile=config.select('unspecified',force_default=True)
            price=profile.pricing
            return {'input':price.get('input'),'cached':price.get('cached',price.get('input')),
                'output':price.get('output'),'currency':price.get('currency','USD'),
                'version':price.get('version',''),'provider':connection.id}
        except ModelConfigError:
            return {'input':None,'cached':None,'output':None,'currency':'UNK','version':'','provider':''}
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
                'cache_detail_missing': 0, 'unknown': pricing.get('unmatched_reason','pricing')}
    cached = _row_value(row, 'cached_input_tokens')
    cache_detail_missing = int(type(cached) is not int)
    cached = cached if type(cached) is int else 0
    cost = estimate_cost(prompt, cached, completion,
                         {'input':input_price,'cached':cached_price,'output':output_price})
    if cost is None:
        return {'cost':None,'currency':'','origin':'','currency_inferred':0,
                'cache_detail_missing':cache_detail_missing,'unknown':'usage'}
    supplemented=pricing.get('current_estimate',False) and not snapshot
    currency = pricing['currency'] if supplemented else stored_currency or pricing['currency']
    return {'cost': cost, 'currency': currency, 'origin': 'current_pricing' if supplemented else 'recalculated',
            'pricing_version':pricing.get('version',''), 'currency_inferred': int(not stored_currency and not supplemented),
            'cache_detail_missing': cache_detail_missing, 'unknown': ''}


def _empty_usage():
    return {'calls': 0, 'prompt_tokens': 0, 'cached_input_tokens': 0, 'completion_tokens': 0,
            'reasoning_tokens': 0, 'estimated_cost': 0.0, 'cost_unknown': 0,
            'cost_recalculated': 0, 'cache_detail_missing': 0, 'currency_inferred': 0,
            'unknown_usage': 0, 'unknown_pricing': 0, 'unknown_identity':0,
            'cost_supplemented':0, 'usage_missing': 0, '_costs': {}, '_bases':{}}


def _add_usage(target, row, pricing, result=None):
    target['calls'] += 1
    for source, key in (('prompt_tokens','prompt_tokens'), ('cached_input_tokens','cached_input_tokens'),
                        ('completion_tokens','completion_tokens'), ('reasoning_tokens','reasoning_tokens')):
        value = _row_value(row, source)
        if (type(value) is int and value>=0
                and (source!='cached_input_tokens' or (type(_row_value(row,'prompt_tokens')) is int
                                                       and value<=row['prompt_tokens']))):
            target[key] += value
    result = result or _cost_for_row(row, pricing)
    if result['cost'] is None:
        target['cost_unknown'] += 1
        if result['unknown'] == 'usage':
            target['unknown_usage'] += 1
            target['usage_missing'] += 1
        elif result['unknown']=='identity':
            target['unknown_identity'] += 1
        else:
            target['unknown_pricing'] += 1
        return
    target['_costs'][result['currency']] = target['_costs'].get(result['currency'], 0.0) + result['cost']
    target['cost_recalculated'] += int(result['origin'] == 'recalculated')
    target['cost_supplemented'] += int(result['origin']=='current_pricing')
    basis=(result['origin'],result['currency'],result.get('pricing_version',''))
    projection=target['_bases'].setdefault(basis,{'amount':0.,'calls':0})
    projection['amount']+=result['cost'];projection['calls']+=1
    target['cache_detail_missing'] += result['cache_detail_missing']
    target['currency_inferred'] += result['currency_inferred']


def _finalize_usage(value):
    costs = value.pop('_costs')
    value['cost_bases']=[{'basis':basis,'currency':currency,'pricing_version':version,**item}
                         for (basis,currency,version),item in sorted(value.pop('_bases').items())]
    breakdown = [{'currency': currency, 'amount': amount} for currency, amount in sorted(costs.items())]
    value['cost_breakdown'] = breakdown
    value['cost_mixed'] = len(breakdown) > 1
    value['currency'] = breakdown[0]['currency'] if len(breakdown) == 1 else ''
    value['estimated_cost'] = breakdown[0]['amount'] if len(breakdown) == 1 else 0.0
    return value


def _usage_aggregates(connection, rows=None, *, include_rounds=False):
    pricing = _pricing_config()
    from core.model_config import resolve_config
    try:config=resolve_config(os.getenv('LLM_CONFIG_JSON'),os.environ)
    except ModelConfigError:config=None
    totals, groups, labels, rounds = _empty_usage(), {}, {}, {}
    if rows is None:
        rows = connection.execute('SELECT * FROM model_calls ORDER BY started_at').fetchall()
    for row in rows:
        snapshot=json.loads(_row_value(row,'pricing_json','{}') or '{}')
        row_pricing={'input':None,'cached':None,'output':None,'currency':'UNK',**snapshot}
        if (_row_value(row,'estimated_cost') is None and
                not all(_row_value(row,key) is not None for key in ('input_price_per_m','output_price_per_m'))
                and not all(row_pricing.get(key) is not None for key in ('input','output'))):
            matched=config.matching_pricing(_row_value(row,'connection_id'),_row_value(row,'profile_id'),
                row['model_id'],_row_value(row,'route_identity')) if config else None
            if matched is not None:
                row_pricing={**row_pricing,**matched,'cached':matched.get('cached',matched.get('input')),'current_estimate':True}
            else:row_pricing['unmatched_reason']='identity'
        result=_cost_for_row(row,row_pricing)
        _add_usage(totals, row, row_pricing,result)
        key = (_row_value(row,'connection_id','legacy_unknown'),_row_value(row,'profile_id','legacy_unknown'),
               _row_value(row, 'purpose', 'unspecified') or 'unspecified', row['model_id'])
        labels[key]={'connection_name':snapshot.get('connection_name') or _row_value(row,'provider_alias','') or '',
                     'profile_name':snapshot.get('profile_name') or row['model_id']}
        group = groups.setdefault(key, _empty_usage())
        _add_usage(group, row, row_pricing,result)
        if include_rounds:
            turn=rounds.setdefault(row['event_key'],{'event_key':row['event_key'],'started_at':row['started_at'],
                'totals':_empty_usage(),'calls':[]})
            _add_usage(turn['totals'],row,row_pricing,result)
            turn['calls'].append({'call_id':row['call_id'],'purpose':key[2],'model_id':row['model_id'],
                **labels[key],'connection_id':key[0],'profile_id':key[1],
                'prompt_tokens':_row_value(row,'prompt_tokens'),'completion_tokens':_row_value(row,'completion_tokens'),
                'elapsed_seconds':_row_value(row,'elapsed_seconds'),'status':row['status'],**result})
    purposes = []
    for (connection_id,profile_id,purpose, model_id), value in groups.items():
        purposes.append({'purpose': purpose, 'model_id': model_id,'connection_id':connection_id,
                         'profile_id':profile_id,**labels[(connection_id,profile_id,purpose,model_id)], **_finalize_usage(value)})
    purposes.sort(key=lambda row: (-row['calls'], row['purpose'], row['model_id']))
    return _finalize_usage(totals), purposes, pricing,[{**value,'totals':_finalize_usage(value['totals'])} for value in rounds.values()]


def usage_summary(store):
    with store._connect() as connection:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_calls'").fetchone():
            return _finalize_usage(_empty_usage())
        totals, _, _, _ = _usage_aggregates(connection)
        return totals


def usage_report(store):
    policy = current_policy()
    with store._connect() as connection:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_calls'").fetchone():
            totals, purposes, pricing = _finalize_usage(_empty_usage()), [], _pricing_config()
            day_reserved = month_reserved = 0
        else:
            totals, purposes, pricing, _ = _usage_aggregates(connection)
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
    from core.model_config import resolve_config
    try:
        config=resolve_config(os.getenv('LLM_CONFIG_JSON'),os.environ)
        limits=config.cost_limits
        uncovered=sorted({p.pricing.get('currency','USD') for p in config.profiles
            if not any(float(v or 0)>0 for v in limits.get(p.pricing.get('currency','USD'),{}).values())})
    except ModelConfigError:
        limits={};uncovered=[]
    return {'totals': totals, 'purposes': purposes,
            'currency': totals['currency'] or pricing['currency'],
            'pricing_active': pricing['input'] is not None and pricing['output'] is not None,
            'budget': {'tier': policy.tier, 'daily_limit': policy.daily_token_limit,
                       'monthly_limit': policy.monthly_token_limit,
                       'day_reserved': day_reserved, 'month_reserved': month_reserved, **accounting,
                       'daily_cost_limit':policy.daily_cost_limit,'monthly_cost_limit':policy.monthly_cost_limit,
                       'cost_currency':policy.cost_currency,'cost_limits':limits,'unlimited_currencies':uncovered},
            'cost_note': _usage_note(totals, pricing), 'historical_corrections':corrections}


def _usage_note(totals, pricing):
    if not totals['calls']:
        return '尚无模型调用记录。'
    notes = ['费用是本地估算，不等同于供应商账单。']
    if totals['cost_recalculated']:
        notes.append(f"{totals['cost_recalculated']} 次历史调用按已保存费率快照回算。")
    if totals['cost_supplemented']:
        notes.append(f"{totals['cost_supplemented']} 次历史缺价按身份匹配的当前费率补估；不改写原始记录或预算账本。")
    if totals['cache_detail_missing']:
        notes.append(f"{totals['cache_detail_missing']} 次调用缺少缓存拆分，全部输入按普通输入费率保守估算；不能据此判断缓存未命中。")
    if totals['unknown_usage']:
        notes.append(f"{totals['unknown_usage']} 次缺少供应商 usage，仍无法估算。")
    if totals['unknown_pricing']:
        notes.append(f"{totals['unknown_pricing']} 次缺少完整费率，无法估算。")
    if totals['unknown_identity']:
        notes.append(f"{totals['unknown_identity']} 次历史身份无法匹配，未按当前默认模型猜填费用。")
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
            totals, purposes, pricing, rounds = _usage_aggregates(connection, rows,include_rounds=True)
        else:
            rows = []
            totals, purposes, pricing = _finalize_usage(_empty_usage()), [], _pricing_config()
            rounds=[]
        has_wendao=connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='wendao_calls'").fetchone()
        supplier_usage={'wendao_submissions':connection.execute(
            "SELECT count(*) FROM wendao_calls WHERE platform='web' AND scope=? AND owner=?",(identity,owner)).fetchone()[0]
            if has_wendao else None,'cost':None,'usage':None,
            'note':'问道仅列已记录的提交数，用量和费用未知；地图、铁路等工具费用未接入模型统计。'}
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='wendao_requests'").fetchone():
            supplier_usage['wendao_submissions']=(supplier_usage['wendao_submissions'] or 0)+connection.execute(
                "SELECT count(*) FROM wendao_requests WHERE platform='web' AND scope=? AND owner=?",(identity,owner)).fetchone()[0]
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='web_requests'").fetchone():
            grouped={turn['event_key']:turn for turn in rounds}
            rounds=[]
            for request in connection.execute('SELECT event_key,content,created_at FROM web_requests WHERE conversation_id=? ORDER BY created_at,rowid',(identity,)):
                turn=grouped.pop(request['event_key'],None) or {'event_key':request['event_key'],'started_at':request['created_at'],
                    'totals':_finalize_usage(_empty_usage()),'calls':[]}
                rounds.append({**turn,'user_message':request['content'][:120]})
            rounds.extend(grouped.values())
    totals['total_tokens'] = totals['prompt_tokens'] + totals['completion_tokens']
    totals['running_calls'] = sum(row['status'] == 'running' for row in rows)
    return {'conversation_id': identity, 'totals': totals, 'purposes': purposes,'rounds':rounds,'supplier_usage':supplier_usage,
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
    def __init__(self, store, client=None, *, default_model=None, skill_manager=None,
                 configuration=None, client_factory=None):
        self.store = store
        self.client = client
        self.default_model = default_model
        self.skill_manager = skill_manager
        self.configuration = configuration
        self.client_factory = client_factory
        self._clients = {}
        self._client_lock = threading.Lock()
        if configuration and client is not None:
            connection,_=configuration.select('unspecified',force_default=True)
            self._clients[connection.id]=client
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
        self.embeddings = SimpleNamespace(create=self.create_embedding)
        with store._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
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
                'stage_id': "TEXT NOT NULL DEFAULT ''",
                'asset_version': "TEXT NOT NULL DEFAULT ''",
                'asset_bundle_hash': "TEXT NOT NULL DEFAULT ''",
                'system_hash': "TEXT NOT NULL DEFAULT ''",
                'connection_id': "TEXT NOT NULL DEFAULT 'legacy_unknown'",
                'profile_id': "TEXT NOT NULL DEFAULT 'legacy_unknown'",
                'route_identity': "TEXT NOT NULL DEFAULT ''",
            }
            for name, definition in additions.items():
                if name not in columns:
                    connection.execute(f'ALTER TABLE model_calls ADD COLUMN {name} {definition}')
            connection.execute('CREATE INDEX IF NOT EXISTS idx_model_calls_event ON model_calls(event_key, started_at)')
            connection.execute('CREATE INDEX IF NOT EXISTS idx_model_calls_purpose ON model_calls(purpose, started_at)')
            initialize_accounting(connection)

    def _route(self, context, kwargs, *, embedding=False):
        if self.configuration is None:
            if not context.force_default_model and not embedding:
                kwargs['model']=model_for_purpose(str(kwargs.get('model','')),context.purpose)
            pricing=_pricing_config(kwargs.get('model'),self.default_model)
            return self.client,pricing
        if embedding and not context.profile_id:
            raise ModelConfigError('云向量实验必须显式指定 embedding 类型模型档案；本地索引不受影响。')
        connection,profile=self.configuration.select(context.purpose,profile_id=context.profile_id,
            force_default=context.force_default_model)
        if (profile.kind=='embedding')!=embedding:
            raise ModelConfigError('聊天和 embedding 模型档案不能混用。')
        required={'embedding'} if embedding else {'text'}
        if kwargs.get('tools'):required.add('tools')
        if (kwargs.get('response_format') or {}).get('type') in {'json_object','json_schema'}:required.add('json')
        if any(isinstance(m.get('content'),list) and any(isinstance(p,dict) and p.get('type')=='image_url'
                for p in m['content']) for m in kwargs.get('messages',[])):required.add('vision')
        if required-set(profile.capabilities):
            raise ModelConfigError('所选模型未声明本次所需的结构化、工具或图片能力，尚未发送请求。')
        kwargs['model']=profile.model_id
        if not embedding:
            other='max_tokens' if profile.token_parameter=='max_completion_tokens' else 'max_completion_tokens'
            if other in kwargs and profile.token_parameter in kwargs:
                raise ModelConfigError('不能同时提供两种模型输出上限参数。')
            if other in kwargs:
                kwargs[profile.token_parameter]=kwargs.pop(other)
            kwargs.setdefault(profile.token_parameter,MAX_OUTPUT_TOKENS)
            if profile.temperature=='omit':kwargs.pop('temperature',None)
        with self._client_lock:
            if connection.id not in self._clients:
                from infrastructure.model_client import create_model_client
                factory=self.client_factory or create_model_client
                self._clients[connection.id]=factory(api_key=connection.api_key,base_url=connection.base_url,timeout=90)
            client=self._clients[connection.id]
        rates=profile.pricing
        pricing={'input':rates.get('input'),'cached':rates.get('cached',rates.get('input')),
            'output':rates.get('output'),'currency':rates.get('currency','USD'),
            'version':rates.get('version') or self.configuration.version,'provider':connection.id,
            'connection_id':connection.id,'profile_id':profile.id,'config_version':self.configuration.version,
            'connection_name':connection.name,'profile_name':profile.name,
            'route_identity':self.configuration.route_identity(connection,profile),
            'stage_id':context.stage_id or ('explicit:'+context.profile_id if context.profile_id else ''),
            'cost_limits':self.configuration.cost_limits}
        return client,pricing

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
            calls,occupied=ledger_occupancy(connection,event_key,event=True)
            details={'calls':calls,'occupied_tokens':occupied,'requested_tokens':estimate+output,'call_limit':MAX_MODEL_CALLS_PER_EVENT,'token_limit':MAX_EVENT_ESTIMATE}
            if calls>=MAX_MODEL_CALLS_PER_EVENT:
                raise ModelBudgetExceeded('本条请求的模型调用次数已达到上限；本次没有自动重发，可查看已有结果或明确发起新查询。',code='event_call_limit',details=details)
            if occupied+estimate+output>MAX_EVENT_ESTIMATE:
                raise ModelBudgetExceeded('本条请求的累计token占用不足以容纳本次调用；请缩小处理范围后发起新查询，原样重试不能释放占用。',code='event_token_limit',details=details)
            day_start,month_start = period_keys(connection,now)
            if policy.daily_token_limit:
                used = connection.execute("SELECT coalesce(sum(reserved_tokens),0) FROM model_usage_periods WHERE period_type='day' AND period_start=?",
                                          (day_start,)).fetchone()[0]
                if used + estimate + output > policy.daily_token_limit:
                    raise ModelBudgetExceeded('今日模型token预算不足以容纳本次调用；请等待日周期恢复或调整预算配置，开始新一轮查询也不会清除此占用。',
                        code='daily_token_limit',details={'occupied_tokens':used,'requested_tokens':estimate+output,'limit':policy.daily_token_limit})
            if policy.monthly_token_limit:
                used = connection.execute("SELECT coalesce(sum(reserved_tokens),0) FROM model_usage_periods WHERE period_type='month' AND period_start=?",
                                          (month_start,)).fetchone()[0]
                if used + estimate + output > policy.monthly_token_limit:
                    raise ModelBudgetExceeded('本月模型token预算不足以容纳本次调用；请等待月周期恢复或调整预算配置。',
                        code='monthly_token_limit',details={'occupied_tokens':used,'requested_tokens':estimate+output,'limit':policy.monthly_token_limit})
            call_id = uuid.uuid4().hex
            reserve_ledger(connection,call_id,event_key,call_context,model,estimate,output,pricing,now)
            connection.execute('''INSERT INTO model_calls
                (call_id, event_key, job_id, model_id, status, input_estimate, reserved_tokens, started_at,
                 purpose, provider_alias, config_version, prompt_version, context_policy_version, output_limit,
                 repair_attempt, fallback_from)
                VALUES (?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (call_id, event_key, scope.job_id if scope else None, model, estimate, estimate+output, now.isoformat(),
                 call_context.purpose, pricing.get('provider',''),
                 pricing.get('config_version',os.getenv('LLM_CONFIG_VERSION', '').strip()), call_context.prompt_version,
                 call_context.context_policy_version, output, call_context.repair_attempt,
                 call_context.fallback_from))
            connection.execute('UPDATE model_calls SET pricing_json=? WHERE call_id=?',
                               (json.dumps(pricing),call_id))
            connection.execute('UPDATE model_calls SET connection_id=?,profile_id=?,route_identity=? WHERE call_id=?',
                (pricing.get('connection_id','legacy_unknown'),pricing.get('profile_id','legacy_unknown'),pricing.get('route_identity',''),call_id))
            connection.execute('UPDATE model_calls SET stage_id=?,asset_version=?,asset_bundle_hash=?,system_hash=? WHERE call_id=?',
                (call_context.stage_id, call_context.asset_version, call_context.asset_bundle_hash,
                 (metadata or {}).get('system_hash', ''), call_id))
            if metadata:
                connection.execute('UPDATE model_calls SET request_bytes=?,input_parts_json=? WHERE call_id=?',
                                   (metadata['request_bytes'],json.dumps(metadata['parts']),call_id))
            for period_type, period_start in (('day', day_start), ('month', month_start)):
                connection.execute('''INSERT INTO model_usage_periods
                    (period_type,period_start,purpose,model_id,connection_id,profile_id,cost_currency,calls,reserved_tokens)
                    VALUES (?,?,?,?,?,?,?,1,?) ON CONFLICT(period_type,period_start,purpose,model_id,connection_id,profile_id,cost_currency)
                    DO UPDATE SET calls=calls+1,reserved_tokens=reserved_tokens+excluded.reserved_tokens''',
                    (period_type, period_start, call_context.purpose, model,pricing.get('connection_id','legacy_unknown'),
                     pricing.get('profile_id','legacy_unknown'),pricing['currency'],estimate + output))
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
                connection.execute('UPDATE model_usage_ledger SET result_received=max(result_received,?) WHERE call_id=?',
                                   (int(result is not None),call_id))
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
        except ModelConfigError:
            self.store.diagnostics.record('model',{'status':'configuration_blocked','error_code':'model_configuration',
                'error_type':'ModelConfigError','purpose':(MODEL_CALL.get() or ModelCallContext()).purpose})
            raise
        except ModelBudgetExceeded as error:
            self.store.diagnostics.record('model', {'status': 'budget_blocked',
                'purpose': (MODEL_CALL.get() or ModelCallContext()).purpose,'error_code':error.code,'validation':error.details})
            raise

    def _create(self, **kwargs):
        call_context = MODEL_CALL.get() or ModelCallContext()
        system_hash = hashlib.sha256(json.dumps([message.get('content') for message in kwargs.get('messages', [])
            if message.get('role') == 'system'], ensure_ascii=False, sort_keys=True,
            separators=(',', ':')).encode()).hexdigest()
        client,pricing=self._route(call_context,kwargs)
        estimate = estimate_input(kwargs.get('messages', [])) + estimate_input(kwargs.get('tools', []))
        request_bytes = len(json.dumps(kwargs,ensure_ascii=False,default=str).encode('utf-8'))
        if request_bytes > MAX_REQUEST_BYTES:
            raise ModelBudgetExceeded('完整模型请求超过硬字节上限，未发送；请减少图片或资料，原样重试不会缩小请求。',
                code='request_bytes_limit',details={'actual':request_bytes,'limit':MAX_REQUEST_BYTES})
        parts = {'tools':estimate_input(kwargs.get('tools',[]))}
        for message in kwargs.get('messages',[]):
            role = message.get('role','unknown')
            if role not in {'system','user','assistant','tool'}:
                role = 'unknown'
            parts[role] = parts.get(role,0) + estimate_input(message.get('content',''))
        if estimate > MAX_INPUT_ESTIMATE:
            raise ModelBudgetExceeded('本次模型输入估算超过上限，未发送；请缩小资料或对象范围，原样重试不会缩小输入。',
                code='model_input_limit',details={'actual':estimate,'limit':MAX_INPUT_ESTIMATE,'parts':parts})
        output_key = 'max_tokens' if 'max_tokens' in kwargs else 'max_completion_tokens'
        output = min(int(kwargs.get(output_key, MAX_OUTPUT_TOKENS)), MAX_OUTPUT_TOKENS)
        output = min(output, output_limit_for_purpose(call_context.purpose,MAX_OUTPUT_TOKENS) * call_context.output_multiplier)
        if output < 1:
            raise ModelBudgetExceeded('模型输出预算必须为正数。')
        kwargs[output_key] = output
        pricing={**pricing,'request_fingerprint':hashlib.sha256(json.dumps(
            {key:value for key,value in kwargs.items() if key!='timeout'},ensure_ascii=False,sort_keys=True,
            separators=(',',':'),default=str).encode()).hexdigest()}
        event_key = MODEL_EVENT.get() or 'direct-model:' + uuid.uuid4().hex
        if call_context.stage_id:
            from infrastructure.skill_manager import CURRENT_SKILLS, SkillSnapshot, skill_asset
            snapshot = CURRENT_SKILLS.get() or SkillSnapshot({call_context.stage_id: skill_asset(call_context.stage_id)})
            self.store.delivery.freeze_skill_assets(event_key, snapshot.metadata())
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
                                        pricing,
                                        {'request_bytes':request_bytes,'parts':parts,'system_hash':system_hash})
                if call_id is None:
                    if time.monotonic() - waiting_since > 180:
                        raise ModelBudgetExceeded('模型服务繁忙，等待超过本次上限，请稍后重试。')
                    time.sleep(0.2)
            started = time.monotonic()
            result, error = None, None
            try:
                kwargs['timeout'] = min(float(kwargs.get('timeout', 90)), 90.0, self._ensure_active())
                network=getattr(client,'network_metadata',None)
                network=network if isinstance(network,dict) else {'network_policy':'injected_or_legacy'}
                self.store.diagnostics.record('model', {'event_key': event_key, 'call_id': call_id,
                    'provider_alias':pricing.get('provider','main'),'connection_id':pricing.get('connection_id','legacy_unknown'),
                    'profile_id':pricing.get('profile_id','legacy_unknown'),'config_version':pricing.get('config_version',''),
                    'status': 'started', 'purpose': call_context.purpose, 'prompt_version': call_context.prompt_version,
                    'stage_id': call_context.stage_id, 'asset_version': call_context.asset_version,
                    'asset_bundle_hash': call_context.asset_bundle_hash, 'system_hash': system_hash,
                    'context_policy_version': call_context.context_policy_version, 'model_id': kwargs['model'],
                    'input_estimate': estimate, 'output_limit': output, 'request_bytes': request_bytes,
                    'timeout_seconds':kwargs['timeout'],'fallback_from':call_context.fallback_from,
                    'repair_attempt':call_context.repair_attempt,'force_default_model':call_context.force_default_model,**network})
                route_token=MODEL_ROUTE.set(pricing)
                try:result = client.chat.completions.create(**kwargs)
                finally:MODEL_ROUTE.reset(route_token)
                self._ensure_active()
                return result
            except BaseException as exc:
                error = exc
                raise
            finally:
                self._finish(call_id, started, result, error)
                self.store.diagnostics.record('model', {'event_key': event_key, 'call_id': call_id,
                    'provider_alias':pricing.get('provider','main'),'connection_id':pricing.get('connection_id','legacy_unknown'),
                    'profile_id':pricing.get('profile_id','legacy_unknown'),
                    'status': 'failed' if error else 'completed', 'purpose': call_context.purpose,
                    'error_type': type(error).__name__ if error else '',
                    'elapsed_seconds': round(time.monotonic() - started, 3),
                    **(model_failure_metadata(error) if error else {})})
        finally:
            if acquired:
                MODEL_SLOTS.release()

    def create_embedding(self, *, model, input, **kwargs):
        """Embedding experiments inherit the same execution fence, price snapshot and ledger."""
        from core.model_budget import model_call_context
        estimate=estimate_input(input)
        if estimate>MAX_INPUT_ESTIMATE:
            raise ModelBudgetExceeded('检索索引批次过大，请分批处理。')
        profile_id=(MODEL_CALL.get() or ModelCallContext()).profile_id
        with model_call_context('retrieval_embedding',prompt_version='embedding-v3',profile_id=profile_id):
            arguments={'model':model}
            client,pricing=self._route(MODEL_CALL.get(),arguments,embedding=True)
            model=arguments['model']
            self._ensure_active()
            if not MODEL_SLOTS.acquire(timeout=min(10,self._ensure_active())):
                raise ModelBudgetExceeded('模型并发繁忙，请稍后检索。')
            call_id=None
            started=time.monotonic()
            result=None
            error=None
            try:
                call_id=self._reserve(MODEL_EVENT.get() or 'embedding:'+uuid.uuid4().hex,model,estimate,0,
                                      MODEL_CALL.get(),pricing)
                if call_id is None:
                    raise ModelBudgetExceeded('模型并发繁忙，请稍后检索。')
                result=client.embeddings.create(model=model,input=input,timeout=min(90,self._ensure_active()),**kwargs)
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
        clients=[*self._clients.values(),*([self.client] if self.client is not None else [])]
        for client in {id(c):c for c in clients}.values():client.close()
