"""50 offline summary boundary fixtures, plus cache metric contracts; no quality/cost claims."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from core.chat_transport import ChatEvent
from infrastructure.memory_store import MemoryStore
from services.conversation_summary_service import ConversationSummaryService
from scripts.evaluate_v3_experiments import cache_metrics


def evaluate(kind,variant):
    with tempfile.TemporaryDirectory() as folder:
        store=MemoryStore(Path(folder)/'summary.db')
        repo=store.conversation_summaries
        event=ChatEvent('web','group','current','fixture','owner','继续讨论')
        constraint=('不安排夜游','预算900元全程','2030年10月1日出发')[variant]
        calls=[]
        def generate(**kwargs):
            payload=json.loads(kwargs['messages'][-1]['content'])
            calls.append(payload)
            source=payload['new_turns'][0]
            item={'kind':'decision','text':constraint,'sources':[{'message_id':source['id'],'quote':constraint}]}
            if kind=='forged-source':
                item['sources'][0]['message_id']=999999
            if kind=='changed-fact':
                item['text']=('安排夜游','预算900元每天','2031年10月1日出发')[variant]
            if kind=='delete-during-call':
                with store._connect() as db:
                    db.execute('DELETE FROM conversation_turns')
            if kind=='invalidate-during-call':
                store.preferences.update(event,0,{'pace':'轻松'})
            if kind=='truncated-output':
                return SimpleNamespace(choices=[SimpleNamespace(finish_reason='length',message=SimpleNamespace(content=json.dumps({'items':[item]},ensure_ascii=False)))])
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({'items':[item]},ensure_ascii=False)))])
        client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=generate)))
        service=ConversationSummaryService(repo,client,'offline-quoted-fixture')
        for index in range(8):
            size=32000 if kind=='fragment' and index==0 else 1800
            store.save_turn('web:fixture','owner','turn-'+str(index),constraint+'；讨论材料'+('甲'*size),'助手猜测不能成为事实来源')
        result=service.ensure(event)
        expected_valid=kind in {'valid','fragment','rebuild','owner-isolation','repeat'}
        errors=[]
        if bool(result)!=expected_valid:
            errors.append('publication state differs from expected')
        if len(calls)!=1:
            errors.append('first request did not have exactly one attempt')
        if calls and len(json.dumps(calls[0],ensure_ascii=False))>24000:
            errors.append('batch exceeds intact JSON bound')
        if kind=='fragment' and result and not json.loads(result['progress_json']):
            errors.append('partial giant turn was falsely covered')
        if kind=='rebuild':
            store.preferences.update(event,0,{'pace':'轻松'})
            rebuilt=service.ensure(event)
            if not rebuilt or rebuilt['version']<=result['version']:
                errors.append('stale summary did not rebuild')
        if kind=='owner-isolation':
            other=ChatEvent('web','group','other','fixture','other-owner','继续讨论')
            if repo.latest(other) is not None:
                errors.append('owner isolation failed')
        if kind=='repeat':
            service.ensure(event)
            if len(calls)!=1:
                errors.append('successful covered batch was generated twice')
        return {'id':kind+'-'+str(variant+1),'category':'long','kind':kind,
                'passed':not errors,'errors':errors,'calls':len(calls)}


def short_fixture(index):
    with tempfile.TemporaryDirectory() as folder:
        store=MemoryStore(Path(folder)/'summary.db')
        event=ChatEvent('web','group','short','short','owner','武汉天气')
        calls=[]
        client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw:calls.append(kw))))
        service=ConversationSummaryService(store.conversation_summaries,client,'offline')
        for turn in range(1+index%7):
            store.save_turn('web:short','owner',str(turn),'短讨论'+str(index),'普通回复')
        result=service.ensure(event,needed=index%2==0)
        return {'id':'short-'+str(index+1),'category':'short','passed':result is None and not calls,'calls':len(calls)}


def main(output):
    env={'AI_SUMMARY_ENABLED':'true','AI_VECTOR_ENABLED':'false'}
    kinds=('valid','fragment','rebuild','owner-isolation','repeat','forged-source',
           'changed-fact','delete-during-call','invalidate-during-call','truncated-output')
    with patch.dict(os.environ,env):
        results=[evaluate(kind,variant) for kind in kinds for variant in range(3)]
        results += [short_fixture(index) for index in range(20)]
    missing=cache_metrics([{'prompt_tokens':100,'cached_input_tokens':None}])
    known=cache_metrics([{'prompt_tokens':100,'cached_input_tokens':80},{'prompt_tokens':100,'cached_input_tokens':0}])
    cache_passed=(missing['call_hit_rate'] is None and known['call_hit_rate']==.5
                  and known['token_hit_ratio']==.4 and known['first_token_latency'] is None)
    report={'mode':'offline-summary-and-cache-contracts','long_cases':30,'short_cases':20,
            'passed':sum(row['passed'] for row in results),'total':len(results),'results':results,
            'cache_metrics_passed':cache_passed,'cache_missing':missing,'cache_known':known,
            'real_quality':'unverified','summary_net_savings':'unverified','cache_net_benefit':'unverified',
            'note':'脚本模型仅给固定引用，不证明真实概括质量、下游任务质量、费用下降或供应商缓存支持。'}
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({key:report[key] for key in ('passed','total','long_cases','short_cases','cache_metrics_passed')}))
    if report['passed']!=report['total'] or not cache_passed:
        raise SystemExit(1)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'evals/v3-memory-cache-contracts.json')
    main(parser.parse_args().output)
