"""Bounded intent-model probe. Offline validates the plan; --live requires complete limits."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from agents.intent_compiler import IntentCompiler
from core.model_pricing import decimal_price, parse_profiles
from core.model_budget import MODEL_EVENT
from infrastructure.memory_store import MemoryStore
from infrastructure.model_gateway import ModelGateway


def preflight(manifest):
    errors=[]
    for key in ('provider','baseline_model','candidate_model','currency'):
        if not isinstance(manifest.get(key),str) or not manifest[key].strip():
            errors.append('missing '+key)
    for key in ('max_calls','max_cost'):
        if key=='max_calls':
            if type(manifest.get(key)) is not int or not 1<=manifest[key]<=1000:
                errors.append('max_calls must be 1..1000')
        else:
            try:
                if not decimal_price(manifest.get(key)):
                    errors.append('max_cost must be positive')
            except ValueError:
                errors.append('invalid max_cost')
    profiles=manifest.get('prices',[])
    try:
        parse_profiles(json.dumps(profiles))
    except (ValueError,TypeError):
        errors.append('invalid model price profiles')
        return errors
    for model in (manifest.get('baseline_model'),manifest.get('candidate_model')):
        found=[p for p in profiles if p.get('model')==model and p.get('provider','')==manifest.get('provider')]
        if not found or any(found[0].get(key) is None for key in ('input','output')) or found[0].get('currency')!=manifest.get('currency'):
            errors.append('missing matching price for '+str(model))
    return errors


def percentile(values,fraction):
    return sorted(values)[min(len(values)-1,int((len(values)-1)*fraction))] if values else None


def compare(baseline,candidate):
    if not baseline or not candidate or baseline.get('cost') is None or candidate.get('cost') is None:
        return {'decision':'unverified','reason':'缺少真实费用或质量对照'}
    if not baseline.get('passed') or not candidate.get('passed'):
        return {'decision':'no-go','reason':'存在关键任务失败'}
    saved=1-candidate['cost']/baseline['cost'] if baseline['cost'] else 0
    delay=candidate['p90']/baseline['p90'] if baseline['p90'] else 1
    return {'decision':'go' if saved>=.20 and delay<=1.10 else 'no-go',
            'cost_reduction':saved,'p90_ratio':delay}


def cache_metrics(rows):
    provided=[r for r in rows if r.get('cached_input_tokens') is not None]
    prompt=sum(r.get('prompt_tokens') or 0 for r in provided)
    return {'usage_coverage':len(provided)/len(rows) if rows else None,
            'call_hit_rate':sum((r['cached_input_tokens'] or 0)>0 for r in provided)/len(provided) if provided else None,
            'token_hit_ratio':sum(r['cached_input_tokens'] for r in provided)/prompt if prompt else None,
            'first_token_latency':None,'latency_note':'非流式只测总耗时，不提供首token延迟'}


def live_probe(manifest,cases):
    from openai import OpenAI
    key=os.getenv('LLM_API_KEY','').strip()
    url=os.getenv('LLM_BASE_URL','').strip()
    if not key or not url:
        raise ValueError('真实探针需要启动环境中的模型Key和地址；不自动读取正式会话。')
    env={'LLM_PROVIDER_ALIAS':manifest['provider'],'LLM_MODEL_PRICES':json.dumps(manifest['prices']),
         'AI_DAILY_COST_LIMIT':str(manifest['max_cost']),'AI_MONTHLY_COST_LIMIT':str(manifest['max_cost']),
         'AI_COST_CURRENCY':manifest['currency'],'AI_SUMMARY_ENABLED':'false','AI_VECTOR_ENABLED':'false',
         'AI_DAILY_TOKEN_LIMIT':'0','AI_MONTHLY_TOKEN_LIMIT':'0'}
    groups={}
    physical_bound=0
    with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,env):
        store=MemoryStore(Path(directory)/'probe.db')
        sdk=OpenAI(api_key=key,base_url=url,max_retries=0)
        gateway=ModelGateway(store,sdk)
        try:
            for name in ('baseline','candidate'):
                selected=manifest[name+'_model']
                values=[]
                for case in cases:
                    # Each compile may use candidate plus one default-model fallback.
                    allowance=1 if name=='baseline' else 2
                    if physical_bound+allowance>manifest['max_calls']:
                        raise ValueError('探针调用上限不足，停止运行。')
                    physical_bound+=allowance
                    started=time.monotonic()
                    event=MODEL_EVENT.set('probe:'+name+':'+str(case['id']))
                    try:
                        with patch.dict(os.environ,{'LLM_INTENT_MODEL_ID':selected if name=='candidate' else '',
                                                    'LLM_MODEL_ID':manifest['baseline_model']}):
                            ir=IntentCompiler(gateway,manifest['baseline_model']).try_compile(case['text'])
                        success=bool(ir and ir.action==case['expected_action'] and
                                     [(op.domain,op.operation) for op in ir.operations]==[tuple(x) for x in case['expected_operations']])
                    finally:
                        MODEL_EVENT.reset(event)
                    values.append({'id':case['id'],'passed':success,'elapsed':time.monotonic()-started})
                with store._connect() as db:
                    usage=[dict(row) for row in db.execute('SELECT * FROM model_calls WHERE event_key LIKE ?',(f'probe:{name}:%',))]
                groups[name]={'passed':all(v['passed'] for v in values),'results':values,
                              'cost':sum(row['estimated_cost'] for row in usage) if all(row['estimated_cost'] is not None for row in usage) else None,
                              'p90':percentile([v['elapsed'] for v in values],.9),'cache':cache_metrics(usage)}
        finally:
            sdk.close()
    return {'mode':'live-intent-probe','groups':groups,'comparison':compare(groups['baseline'],groups['candidate']),
            'notes':['只验证意图探针；正式启用还须端到端留出集。','调用额度包括候选失败升级；没有自动扩大额度。']}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--manifest',type=Path,default=ROOT/'evals/v3-live-probe-plan.json')
    parser.add_argument('--cases',type=Path,default=ROOT/'evals/v3-intent-probes.json')
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--live',action='store_true')
    args=parser.parse_args()
    manifest=json.loads(args.manifest.read_text(encoding='utf-8'))
    cases=json.loads(args.cases.read_text(encoding='utf-8'))
    errors=preflight(manifest)
    if type(manifest.get('max_calls')) is int and manifest['max_calls'] < 3*len(cases):
        errors.append('max_calls cannot cover baseline plus candidate/fallback for all cases')
    if not cases or any(case.get('label_status') != 'reviewed' for case in cases):
        errors.append('probe labels require review before live evaluation')
    report={'mode':'offline-preflight','decision':'pending-config' if errors else 'ready-for-approved-probe',
            'errors':errors,'cases':len(cases),'data_sha256':hashlib.sha256(args.cases.read_bytes()).hexdigest(),
            'real_model_quality':'unverified','summary_net_savings':'unverified','cache_benefit':'unverified'}
    if args.live:
        if errors:
            raise ValueError('真实评测参数未补齐：'+', '.join(errors))
        report=live_probe(manifest,cases)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({key:report.get(key) for key in ('mode','decision','errors')},ensure_ascii=False))


if __name__=='__main__':
    main()
