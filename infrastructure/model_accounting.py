"""Content-free call ledger survives conversation deletion and settles exactly once."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from decimal import Decimal
from zoneinfo import ZoneInfo

from core.model_budget import MODEL_RUN, ModelBudgetExceeded
from core.model_pricing import estimate_cost
from core.model_policy import current_policy


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


def reserve_ledger(db, call_id, event_key, context, model, estimate, output, pricing, now):
    day,month=period_keys(db,now)
    run=MODEL_RUN.get() or hashlib.sha256(event_key.encode()).hexdigest()
    calls=db.execute('SELECT count(*),coalesce(sum(reserved),0) FROM model_usage_ledger WHERE run_key=?',(run,)).fetchone()
    if calls[0]>=12 or calls[1]+estimate+output>256000:
        raise ModelBudgetExceeded('本次业务运行的模型调用或预算已达到上限。')
    worst_pricing = dict(pricing)
    if pricing['input'] is not None and pricing['cached'] is not None:
        worst_pricing['input'] = max(pricing['input'],pricing['cached'])
    quote=estimate_cost(estimate,0,output,worst_pricing)
    policy=current_policy()
    for field,key,limit in (('day_key',day,policy.daily_cost_limit),('month_key',month,policy.monthly_cost_limit)):
        if not limit:
            continue
        if quote is None or pricing['currency'] != policy.cost_currency:
            raise ModelBudgetExceeded('金额限额需要本次模型的完整费率和匹配币种，请补齐费率或使用token限额。')
        kind = 'day' if field == 'day_key' else 'month'
        occupied = db.execute('SELECT coalesce(sum(reserved_tokens),0) FROM model_usage_periods WHERE period_type=? AND period_start=?',
                              (kind,key)).fetchone()[0]
        tracked = db.execute(f"SELECT coalesce(sum(CASE WHEN status='settled' THEN consumed ELSE reserved END),0) FROM model_usage_ledger WHERE {field}=?",(key,)).fetchone()[0]
        if occupied > tracked:
            raise ModelBudgetExceeded('当前周期有升级前的历史预算占用，金额限额须从无遗留占用的新周期启用。')
        rows=db.execute(f'SELECT * FROM model_usage_ledger WHERE {field}=?',(key,)).fetchall()
        if any(row['currency'] != policy.cost_currency or (row['estimated_cost'] is None and row['money_reserved'] is None) for row in rows):
            raise ModelBudgetExceeded('当前周期包含未知费用或其他币种，无法保证金额限额。')
        spent=sum((Decimal(str(row['estimated_cost'] if row['estimated_cost'] is not None else row['money_reserved'])) for row in rows),Decimal(0))
        if spent+Decimal(str(quote))>Decimal(str(limit)):
            raise ModelBudgetExceeded('本周期模型金额预算已达到上限。')
    db.execute('''INSERT INTO model_usage_ledger
        (call_id,run_key,purpose,model_id,pricing_json,started_at,day_key,month_key,reserved,currency)
        VALUES (?,?,?,?,?,?,?,?,?,?)''',
        (call_id,run,context.purpose,model,json.dumps(pricing),now.isoformat(),day,month,estimate+output,pricing['currency']))
    db.execute('UPDATE model_usage_ledger SET money_reserved=? WHERE call_id=?',(quote,call_id))
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
            WHERE period_type=? AND period_start=? AND purpose=? AND model_id=?''',
            (occupied-old_occupied,prompt if valid else 0,cached or 0 if valid else 0,completion if valid else 0,
             reasoning or 0 if valid else 0,cost or 0,int(cost is None)-int(was_unknown),kind,key,row['purpose'],row['model_id']))
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
