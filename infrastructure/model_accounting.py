"""Content-free call ledger survives conversation deletion and settles exactly once."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from decimal import Decimal
from zoneinfo import ZoneInfo

from core.model_budget import MODEL_RUN, ModelBudgetExceeded
from core.model_pricing import estimate_cost
from core.model_policy import current_policy
from core.model_config import ModelConfigError


def initialize_periods(db):
    """Migrate exact historic totals; only saved ledger evidence may identify a currency."""
    exists=db.execute("SELECT 1 FROM sqlite_master WHERE name='model_usage_periods'").fetchone()
    old=[]
    if exists and 'connection_id' not in {r['name'] for r in db.execute('PRAGMA table_info(model_usage_periods)')}:
        old=[dict(r) for r in db.execute('SELECT * FROM model_usage_periods')]
        db.execute('ALTER TABLE model_usage_periods RENAME TO model_usage_periods_v1')
    db.execute('''CREATE TABLE IF NOT EXISTS model_usage_periods (
        period_type TEXT NOT NULL,period_start TEXT NOT NULL,purpose TEXT NOT NULL,model_id TEXT NOT NULL,
        connection_id TEXT NOT NULL DEFAULT 'legacy_unknown',profile_id TEXT NOT NULL DEFAULT 'legacy_unknown',
        cost_currency TEXT NOT NULL DEFAULT 'UNK',calls INTEGER NOT NULL DEFAULT 0,
        reserved_tokens INTEGER NOT NULL DEFAULT 0,prompt_tokens INTEGER NOT NULL DEFAULT 0,
        cached_input_tokens INTEGER NOT NULL DEFAULT 0,completion_tokens INTEGER NOT NULL DEFAULT 0,
        reasoning_tokens INTEGER NOT NULL DEFAULT 0,estimated_cost REAL NOT NULL DEFAULT 0,cost_unknown INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(period_type,period_start,purpose,model_id,connection_id,profile_id,cost_currency))''')
    metrics=('calls','reserved_tokens','prompt_tokens','cached_input_tokens','completion_tokens','reasoning_tokens','estimated_cost','cost_unknown')
    for original in old:
        field='day_key' if original['period_type']=='day' else 'month_key'
        ledger=db.execute(f'SELECT * FROM model_usage_ledger WHERE {field}=? AND purpose=? AND model_id=?',
            (original['period_start'],original['purpose'],original['model_id'])).fetchall()
        groups={}
        for row in ledger:
            identity=(row['connection_id'],row['profile_id'],row['currency'] or 'UNK')
            target=groups.setdefault(identity,dict.fromkeys(metrics,0))
            valid=row['status']=='settled'
            values=(1,row['consumed'] if valid else row['reserved'],row['prompt_tokens'] or 0,
                row['cached_input_tokens'] or 0,row['completion_tokens'] or 0,row['reasoning_tokens'] or 0,
                row['estimated_cost'] or 0,int(row['status']=='unknown' or valid and row['estimated_cost'] is None))
            for key,value in zip(metrics,values):target[key]+=value
        remaining={k:original[k]-sum(g[k] for g in groups.values()) for k in metrics}
        if any(value < -1e-9 for value in remaining.values()):
            groups={('legacy_unknown','legacy_unknown',original.get('cost_currency') or 'UNK'):{k:original[k] for k in metrics}}
        elif any(abs(v)>1e-9 for v in remaining.values()):
            key=('legacy_unknown','legacy_unknown',original.get('cost_currency') or 'UNK')
            target=groups.setdefault(key,dict.fromkeys(metrics,0))
            for k,v in remaining.items():target[k]+=max(0,v)
        for (connection,profile,currency),values in groups.items():
            db.execute('INSERT INTO model_usage_periods VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (original['period_type'],original['period_start'],original['purpose'],original['model_id'],connection,profile,currency,
                 *[values[k] for k in metrics]))
    if exists and db.execute("SELECT 1 FROM sqlite_master WHERE name='model_usage_periods_v1'").fetchone():
        db.execute('DROP TABLE model_usage_periods_v1')


def initialize_accounting(db):
    db.execute('''CREATE TABLE IF NOT EXISTS model_usage_ledger (
        call_id TEXT PRIMARY KEY,run_key TEXT NOT NULL,purpose TEXT NOT NULL,model_id TEXT NOT NULL,
        pricing_json TEXT NOT NULL,started_at TEXT NOT NULL,day_key TEXT NOT NULL,month_key TEXT NOT NULL,
        reserved INTEGER NOT NULL,consumed INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL DEFAULT 'running',
        estimated_cost REAL,currency TEXT NOT NULL,prompt_tokens INTEGER,completion_tokens INTEGER,
        cached_input_tokens INTEGER,reasoning_tokens INTEGER)''')
    db.execute('CREATE INDEX IF NOT EXISTS idx_ledger_run ON model_usage_ledger(run_key)')
    columns={row['name'] for row in db.execute('PRAGMA table_info(model_usage_ledger)')}
    if 'money_reserved' not in columns:
        db.execute('ALTER TABLE model_usage_ledger ADD COLUMN money_reserved REAL')
    for name,default in (('connection_id','legacy_unknown'),('profile_id','legacy_unknown'),
                         ('route_identity',''),('config_version',''),('event_key',''),('stage_id','')):
        if name not in columns:db.execute(f"ALTER TABLE model_usage_ledger ADD COLUMN {name} TEXT NOT NULL DEFAULT '{default}'")
    if 'request_fingerprint' not in columns:
        db.execute("ALTER TABLE model_usage_ledger ADD COLUMN request_fingerprint TEXT NOT NULL DEFAULT ''")
    if 'result_received' not in columns:
        db.execute('ALTER TABLE model_usage_ledger ADD COLUMN result_received INTEGER NOT NULL DEFAULT 0')
        db.execute('''UPDATE model_usage_ledger SET result_received=1 WHERE EXISTS
            (SELECT 1 FROM model_calls m WHERE m.call_id=model_usage_ledger.call_id
             AND (m.status='completed' OR m.usage_complete=1))''')
    db.execute('''UPDATE model_usage_ledger SET
        event_key=(SELECT event_key FROM model_calls m WHERE m.call_id=model_usage_ledger.call_id),
        stage_id=(SELECT stage_id FROM model_calls m WHERE m.call_id=model_usage_ledger.call_id)
        WHERE event_key='' AND EXISTS (SELECT 1 FROM model_calls m WHERE m.call_id=model_usage_ledger.call_id)''')
    initialize_periods(db)
    db.execute('''CREATE TABLE IF NOT EXISTS model_accounting_settings (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),day_cutover TEXT NOT NULL,month_cutover TEXT NOT NULL)''')
    now = datetime.now(timezone.utc)
    utc = now.date()
    next_month=(utc.replace(day=28)+timedelta(days=4)).replace(day=1)
    legacy = db.execute('SELECT 1 FROM model_usage_periods LIMIT 1').fetchone()
    db.execute('INSERT OR IGNORE INTO model_accounting_settings VALUES (1,?,?)',
               (datetime.combine(utc+timedelta(days=1),datetime.min.time(),timezone.utc).isoformat() if legacy else now.isoformat(),
                datetime.combine(next_month,datetime.min.time(),timezone.utc).isoformat() if legacy else now.isoformat()))


def period_keys(db, now):
    setting=db.execute('SELECT * FROM model_accounting_settings WHERE singleton=1').fetchone()
    local=now.astimezone(ZoneInfo('Asia/Shanghai')).date()
    utc=now.astimezone(timezone.utc).date()
    def cutover(value):
        parsed=datetime.fromisoformat(value)
        # Early v3 rehearsal date-only values are UTC midnight boundaries too.
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    day=local if now>=cutover(setting['day_cutover']) else utc
    month=local if now>=cutover(setting['month_cutover']) else utc
    return day.isoformat(),month.replace(day=1).isoformat()


def ledger_occupancy(db,key,*,event=False):
    """Only auditable settled usage releases tokens; old ambiguous records stay reserved."""
    occupied="""CASE WHEN status='settled' AND typeof(prompt_tokens)='integer' AND prompt_tokens>=0
        AND typeof(completion_tokens)='integer' AND completion_tokens>=0 AND consumed=prompt_tokens+completion_tokens
        AND (cached_input_tokens IS NULL OR typeof(cached_input_tokens)='integer' AND cached_input_tokens BETWEEN 0 AND prompt_tokens)
        THEN consumed ELSE reserved END"""
    query=f"SELECT {occupied} AS occupied FROM model_usage_ledger WHERE {'event_key' if event else 'run_key'}=?"
    parameters=(key,)
    if event:
        query+=''' UNION ALL SELECT reserved_tokens FROM model_calls m WHERE event_key=?
            AND NOT EXISTS (SELECT 1 FROM model_usage_ledger l WHERE l.call_id=m.call_id)'''
        parameters=(key,key)
    return db.execute('SELECT count(*),coalesce(sum(occupied),0) FROM ('+query+')',parameters).fetchone()


def reserve_ledger(db, call_id, event_key, context, model, estimate, output, pricing, now):
    day,month=period_keys(db,now)
    run=MODEL_RUN.get() or hashlib.sha256(event_key.encode()).hexdigest()
    calls=ledger_occupancy(db,run)
    details={'calls':calls[0],'occupied_tokens':calls[1],'requested_tokens':estimate+output,'call_limit':12,'token_limit':256000}
    if calls[0]>=12:
        raise ModelBudgetExceeded('本轮查询累计模型调用已达12次，未继续提交。可查看已有资料；需要重新核价时请明确开始新一轮查询，日/月限额仍生效。',code='run_call_limit',details=details)
    if calls[1]+estimate+output>256000:
        raise ModelBudgetExceeded('本轮查询累计token占用不足以容纳本次请求，未继续提交。可查看已有资料；需要重新核价时请明确开始新一轮查询，未知用量仍按原预留保留。',code='run_token_limit',details=details)
    if pricing.get('route_identity'):
        route_slot=pricing.get('stage_id',context.stage_id)
        prior=db.execute("SELECT route_identity FROM model_usage_ledger WHERE event_key=? AND purpose=? AND stage_id=? ORDER BY started_at DESC LIMIT 1",
            (event_key,context.purpose,route_slot)).fetchone()
        if prior and not prior['route_identity']:
            raise ModelConfigError('旧请求的模型身份无法确认，未按当前配置重发；请核对记录后发起新请求。')
        if prior and prior['route_identity']!=pricing['route_identity']:
            raise ModelConfigError('此请求已冻结的模型身份发生变化，未换供应商重发；请检查配置后发起新请求。')
        uncertain=db.execute('''SELECT 1 FROM model_usage_ledger WHERE event_key=? AND purpose=? AND stage_id=?
            AND result_received=0 AND (request_fingerprint=? OR request_fingerprint='') LIMIT 1''',
            (event_key,context.purpose,route_slot,pricing.get('request_fingerprint',''))).fetchone()
        if uncertain:
            raise ModelConfigError('此请求上次模型调用的结果尚不确定，未自动重发；请核对记录后发起新请求。')
    worst_pricing = dict(pricing)
    if pricing['input'] is not None and pricing['cached'] is not None:
        worst_pricing['input'] = max(pricing['input'],pricing['cached'])
    quote=estimate_cost(estimate,0,output,worst_pricing)
    policy=current_policy()
    limits=pricing.get('cost_limits')
    currency_limits=limits.get(pricing['currency'],{}) if limits is not None else {}
    daily=currency_limits.get('daily',0) if limits is not None else policy.daily_cost_limit
    monthly=currency_limits.get('monthly',0) if limits is not None else policy.monthly_cost_limit
    for field,key,limit in (('day_key',day,float(daily or 0)),('month_key',month,float(monthly or 0))):
        if not limit:
            continue
        if quote is None or limits is None and pricing['currency'] != policy.cost_currency:
            raise ModelBudgetExceeded('金额限额需要本次模型的完整费率和匹配币种，请补齐费率配置；尚未提交本次模型请求。',code='pricing_incomplete')
        kind = 'day' if field == 'day_key' else 'month'
        occupied = db.execute('SELECT coalesce(sum(reserved_tokens),0) FROM model_usage_periods WHERE period_type=? AND period_start=?',
                              (kind,key)).fetchone()[0]
        tracked = db.execute(f"SELECT coalesce(sum(CASE WHEN status='settled' THEN consumed ELSE reserved END),0) FROM model_usage_ledger WHERE {field}=?",(key,)).fetchone()[0]
        if occupied > tracked:
            raise ModelBudgetExceeded('当前周期有升级前的历史预算占用，金额限额须从无遗留占用的新周期启用。',code='legacy_cost_occupancy')
        rows=db.execute(f'SELECT * FROM model_usage_ledger WHERE {field}=?',(key,)).fetchall()
        if limits is not None:rows=[r for r in rows if r['currency'] in {pricing['currency'],'','UNK'}]
        if any(row['currency'] != pricing['currency'] or (row['estimated_cost'] is None and row['money_reserved'] is None) for row in rows):
            raise ModelBudgetExceeded('当前周期包含未知费用或其他币种，无法保证金额限额；请核对记录及配置，原样重试不能消除此阻断。',code='cost_unknown')
        spent=sum((Decimal(str(row['estimated_cost'] if row['estimated_cost'] is not None else row['money_reserved'])) for row in rows),Decimal(0))
        if spent+Decimal(str(quote))>Decimal(str(limit)):
            raise ModelBudgetExceeded(('今日' if kind=='day' else '本月')+'模型金额预算已达到上限；请等待对应周期恢复或调整配置。',
                code='daily_cost_limit' if kind=='day' else 'monthly_cost_limit',details={'occupied_cost':float(spent),'requested_cost':float(quote),'limit':limit})
    db.execute('''INSERT INTO model_usage_ledger
        (call_id,run_key,purpose,model_id,pricing_json,started_at,day_key,month_key,reserved,currency)
        VALUES (?,?,?,?,?,?,?,?,?,?)''',
        (call_id,run,context.purpose,model,json.dumps(pricing),now.isoformat(),day,month,estimate+output,pricing['currency']))
    db.execute('UPDATE model_usage_ledger SET money_reserved=? WHERE call_id=?',(quote,call_id))
    db.execute('UPDATE model_usage_ledger SET connection_id=?,profile_id=?,route_identity=?,config_version=?,event_key=?,stage_id=? WHERE call_id=?',
        (pricing.get('connection_id','legacy_unknown'),pricing.get('profile_id','legacy_unknown'),pricing.get('route_identity',''),
         pricing.get('config_version',''),event_key,pricing.get('stage_id',context.stage_id),call_id))
    db.execute('UPDATE model_usage_ledger SET request_fingerprint=? WHERE call_id=?',(pricing.get('request_fingerprint',''),call_id))
    return day,month


def settle_ledger(db, call_id, prompt, cached, completion, reasoning):
    if not db.in_transaction:
        db.execute('BEGIN IMMEDIATE')
    row=db.execute('SELECT * FROM model_usage_ledger WHERE call_id=?',(call_id,)).fetchone()
    if row is None or row['status']=='settled':
        return None
    valid=(type(prompt) is int and type(completion) is int and prompt>=0 and completion>=0
           and (cached is None or (type(cached) is int and 0<=cached<=prompt)))
    if not valid and row['status']=='unknown':
        return None
    pricing=json.loads(row['pricing_json'])
    cost=estimate_cost(prompt,cached,completion,pricing) if valid else None
    consumed=prompt+completion if valid else 0
    old_occupied=row['reserved'] if row['status'] in {'running','unknown'} else row['consumed']
    occupied=consumed if valid else row['reserved']
    was_unknown=row['status']=='unknown'
    db.execute('''UPDATE model_usage_ledger SET status=?,consumed=?,estimated_cost=?,
        prompt_tokens=?,cached_input_tokens=?,completion_tokens=?,reasoning_tokens=? WHERE call_id=?''',
        ('settled' if valid else 'unknown',consumed,cost,prompt if valid else None,cached if valid else None,
         completion if valid else None,reasoning if type(reasoning) is int and reasoning>=0 else None,call_id))
    for kind,key in (('day',row['day_key']),('month',row['month_key'])):
        db.execute('''UPDATE model_usage_periods SET reserved_tokens=reserved_tokens+?,
            prompt_tokens=prompt_tokens+?,cached_input_tokens=cached_input_tokens+?,completion_tokens=completion_tokens+?,
            reasoning_tokens=reasoning_tokens+?,estimated_cost=estimated_cost+?,cost_unknown=cost_unknown+?
            WHERE period_type=? AND period_start=? AND purpose=? AND model_id=?
                AND connection_id=? AND profile_id=? AND cost_currency=?''',
            (occupied-old_occupied,prompt if valid else 0,cached or 0 if valid else 0,completion if valid else 0,
             reasoning or 0 if valid else 0,cost or 0,int(cost is None)-int(was_unknown),kind,key,row['purpose'],row['model_id'],
             row['connection_id'],row['profile_id'],row['currency']))
    return {'pricing':pricing,'cost':cost,'valid':valid}


def ledger_report(db):
    result={'accounting_version':3,'estimate_method':'utf8-byte-bound-v1','timezone':'Asia/Shanghai',
            'day_inflight':0,'day_actual_tokens':0,'day_unknown_occupied':0,'day_legacy_occupied':0}
    exists=db.execute("SELECT 1 FROM sqlite_master WHERE name='model_usage_ledger'").fetchone()
    if not exists:
        return result
    day,_=period_keys(db,datetime.now(timezone.utc))
    rows=db.execute('SELECT status,reserved,consumed FROM model_usage_ledger WHERE day_key=?',(day,)).fetchall()
    for row in rows:
        key={'running':'day_inflight','unknown':'day_unknown_occupied','settled':'day_actual_tokens'}[row['status']]
        result[key]+=row['consumed'] if row['status']=='settled' else row['reserved']
    occupied=db.execute("SELECT coalesce(sum(reserved_tokens),0) FROM model_usage_periods WHERE period_type='day' AND period_start=?",(day,)).fetchone()[0]
    result['day_legacy_occupied']=max(0,occupied-sum(result[key] for key in ('day_inflight','day_actual_tokens','day_unknown_occupied')))
    cutover=db.execute('SELECT * FROM model_accounting_settings WHERE singleton=1').fetchone()
    result.update(day_cutover=cutover['day_cutover'],month_cutover=cutover['month_cutover'])
    return result
