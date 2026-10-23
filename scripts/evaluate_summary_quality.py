"""Offline summary contracts. Scripted responses are NOT real-model quality scores."""
import argparse
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from agents.context_builder import AgentContext,ContextBuilder,render_untrusted_context
from agents.travel_agent import TravelAgent
from core.chat_transport import ChatEvent
from core.model_budget import MODEL_EVENT
from core.settings import Settings
from infrastructure.memory_store import MemoryStore
from infrastructure.model_gateway import ModelGateway
from services.conversation_summary_service import ConversationSummaryService,validate_items

OFFLINE_ENV={'AI_SUMMARY_ENABLED':'true','AI_BUDGET_TIER':'balanced','AI_VECTOR_ENABLED':'false',
             'AI_DAILY_TOKEN_LIMIT':'0','AI_MONTHLY_TOKEN_LIMIT':'0','AI_DAILY_COST_LIMIT':'0',
             'AI_MONTHLY_COST_LIMIT':'0','LLM_SUMMARY_MODEL_ID':'','LLM_MODEL_PRICES':'[]',
             'LLM_PROVIDER_ALIAS':'offline-summary-eval'}


class ScriptedClient:
    def __init__(self):
        self.calls=[]
        self.items=[]
        self.fault=''
        self.chat=SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self,**kwargs):
        self.calls.append(json.loads(json.dumps(kwargs)))
        if kwargs.get('response_format'):
            if self.fault=='network_failure':
                raise TimeoutError('Offline injected failure')
            payload=json.loads(kwargs['messages'][-1]['content'])
            available={}
            for entry in payload['new_turns']:
                available.setdefault(entry.get('message_id',entry.get('id')),[]).append(entry['user'])
            for item in payload['previous_items']:
                for ref in item['sources']:
                    available.setdefault(ref['message_id'],[]).append(ref['quote'])
            items=[item for item in self.items if all(any(ref['quote'] in text for text in available.get(ref['message_id'],[]))
                                                      for ref in item['sources'])]
            if self.fault=='invalid_reference':
                items=[{'kind':'reference','text':'引用错误','sources':[{'message_id':-1,'quote':'不存在的引用'}]}]
            content='{' if self.fault=='invalid_json' else json.dumps({'items':items},ensure_ascii=False)
        else:
            content='离线脚本回复；未运行真实回答模型，不用于质量评分。'
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content,tool_calls=[]),
                               finish_reason='length' if self.fault=='truncated' else 'stop')],usage=None)


def load_cases(path):
    value=json.loads(Path(path).read_text(encoding='utf-8'))
    if value.get('schema_version')!=2 or value.get('scope')!='synthetic-only':
        raise ValueError('Only versioned synthetic fixtures are allowed')
    return value


def seed_stage(store,event,stage,corpus,mapping):
    prose='\n'.join(corpus['neutral_paragraphs'])
    for turn in stage['turns']:
        if turn['key'] in mapping:
            raise ValueError('Duplicate fixture turn key')
        size=stage.get('assistant_chars',20)
        reply=(stage.get('assistant_prefix','')+'\n'+prose*((size//len(prose))+1))[:size]
        key=f'{event.event_key}:{turn["key"]}'
        store.save_turn(event.storage_scope_id,event.sender_id,key,turn['user'],reply)
        store.save_chat_message(key,event.platform,event.scope_id,event.sender_id,key,'','user',turn['user'])
        with store._connect() as db:
            mapping[turn['key']]=db.execute('SELECT id FROM conversation_turns WHERE user_msg_id=?',(key,)).fetchone()[0]
    return [{'kind':item['kind'],'text':item['text'],
             'sources':[{'message_id':mapping[ref['turn']],'quote':ref['quote']} for ref in item['sources']]}
            for item in stage['items']]


def capture_conditions(store,event,service,sdk,gateway,corpus,record,*,settings=None,real=False):
    if record is None:
        return {'checked':False,'reason':'No published summary'}
    for index,user in enumerate(corpus['aging_messages']):
        key=f'{event.event_key}:aging-{index}'
        store.save_turn(event.storage_scope_id,event.sender_id,key,user,'仅讨论中性的表达方法。')
        store.save_chat_message(key,event.platform,event.scope_id,event.sender_id,key,'','user',user)
    traces=[]
    context=ContextBuilder(store,service,diagnostics=traces.append).build(event)
    raw_context=render_untrusted_context(replace(context,conversation_summary=''))
    probes=['姨妈','15分钟','360','320','19:30','19:00','慢慢看桥']
    leakage=[probe for probe in probes if probe in raw_context]
    with store._connect() as db:
        rows=db.execute('SELECT user_content FROM conversation_turns WHERE group_openid=? AND member_openid=? AND id<=? ORDER BY id',
                        (event.storage_scope_id,event.sender_id,record['covered_turn_id'])).fetchall()
    common={'recent_dialogue':(),'group_context':'','document_context':'','source_note':context.source_note,
            'message_time':context.message_time,'uses_task_state':context.uses_task_state}
    conditions={'full_original':AgentContext(**{**common,'group_context':'\n'.join(row['user_content'] for row in rows)}),
                'summary_only':replace(context,recent_dialogue=(),group_context='',document_context='',tasks=(),preferences=''),
                'no_summary':AgentContext(**common)}
    settings=settings or Settings('','',frozenset(),'','offline','https://offline.invalid','offline-model')
    agent=TravelAgent(settings,lambda *args,**kwargs: (_ for _ in ()).throw(AssertionError('External tool forbidden')),gateway)
    requests={}
    for name,condition in conditions.items():
        before=len(sdk.calls)
        result=agent.run(corpus['recall'],condition)
        if len(sdk.calls)!=before+1:
            raise AssertionError('Offline recall should capture exactly one model request')
        requests[name]={'request':sdk.calls[-1],'reply':result.reply,'quality_score':None}
    return {'checked':True,'leakage_outside_summary':leakage,'trace':traces[-1],
            'requests':requests,'note':('Real model requests captured; semantic grading requires inspection.' if real
                                       else 'Requests captured at scripted SDK boundary; answers are not real-model results.')}


def evaluate_case(case,corpus):
    if 'validation_cases' in case:
        checks=[]
        for entry in case['validation_cases']:
            source_id=-1 if entry.get('invalid_source') else 1
            try:
                validate_items({'items':[{'kind':'reference','text':entry['text'],
                    'sources':[{'message_id':source_id,'quote':entry['quote']}]}]},
                    {'previous_items':[],'new_turns':[{'id':1,'user':entry['quote']}]})
                accepted=True
            except ValueError:
                accepted=False
            checks.append({'id':entry['id'],'expected_accepted':entry['expected_accepted'],
                           'actual_accepted':accepted,'passed':accepted==entry['expected_accepted']})
        return {'id':case['id'],'mechanism_passed':True,'semantic_guard_passed':all(c['passed'] for c in checks),
                'checks':checks,'real_model_quality':None}
    if 'fault_cases' in case:
        checks=[evaluate_fault(name,corpus) for name in case['fault_cases']]
        return {'id':case['id'],'mechanism_passed':all(c['passed'] for c in checks),'checks':checks,'real_model_quality':None}
    with tempfile.TemporaryDirectory(prefix='summary-eval-') as directory:
        store=MemoryStore(Path(directory)/'isolated.db')
        store.save_turn('web:other-scope','other','sentinel','其他会话的消息，不属于当前材料。','离线占位')
        event=ChatEvent('web','group',case['id'],case['id'],'test-owner',corpus['recall'])
        sdk=ScriptedClient()
        gateway=ModelGateway(store,sdk)
        diagnostics=[]
        service=ConversationSummaryService(store.conversation_summaries,gateway,'offline-model',diagnostics.append)
        mapping={}
        stages=[]
        record=None
        for stage in case['stages']:
            sdk.items=seed_stage(store,event,stage,corpus,mapping)
            target=max(mapping.values())
            start=len(sdk.calls)
            for _ in range(10):
                before=record['version'] if record else 0
                record=service.ensure(event)
                if record is None or record['covered_turn_id']>=target or record['version']==before:
                    break
            items=json.loads(record['items_json']) if record else []
            texts={item['text'] for item in items}
            missing=[item['id'] for item in stage['items'] if item['text'] not in texts]
            stages.append({'id':stage['id'],'calls':len(sdk.calls)-start,'published':bool(record),
                           'version':record['version'] if record else None,'target_turn_id':target,
                           'covered_turn_id':record['covered_turn_id'] if record else None,
                           'progress':json.loads(record['progress_json']) if record else None,
                           'user_chars':sum(len(t['user']) for t in stage['turns']),
                           'items':items,'missing_scripted_items':missing})
        passed=all(stage['published'] and not stage['missing_scripted_items'] and stage['covered_turn_id']>=stage['target_turn_id']
                   for stage in stages)
        if case.get('minimum_user_chars'):
            passed=passed and stages[0]['user_chars']>=case['minimum_user_chars']
        if case.get('minimum_batches'):
            passed=passed and stages[0]['calls']>=case['minimum_batches']
        summary_calls=list(sdk.calls)
        source_text=json.dumps([json.loads(call['messages'][-1]['content']) for call in summary_calls],ensure_ascii=False)
        forbidden=[text for text in case.get('forbidden_source',[]) if text in source_text]
        passed=passed and not forbidden
        captured=capture_conditions(store,event,service,sdk,gateway,corpus,record) if case['id']=='Q01' else None
        if captured:
            passed=passed and not captured['leakage_outside_summary'] and captured['trace']['summary_injected']
        return {'id':case['id'],'mechanism_passed':passed,'stages':stages,'source_mapping':mapping,
                'diagnostics':diagnostics,'summary_requests':summary_calls,'forbidden_source_found':forbidden,
                'context_conditions':captured,'real_model_quality':None}


def evaluate_fault(name,corpus):
    with tempfile.TemporaryDirectory(prefix='summary-fault-') as directory:
        store=MemoryStore(Path(directory)/'isolated.db')
        event=ChatEvent('web','group',name,'fault-'+name,'owner','继续普通讨论')
        sdk=ScriptedClient()
        diagnostics=[]
        service=ConversationSummaryService(store.conversation_summaries,sdk,'offline-model',diagnostics.append)
        mapping={}
        sdk.items=seed_stage(store,event,corpus['cases'][0]['stages'][0],corpus,mapping)
        if name in {'invalid_json','truncated','invalid_reference','network_failure'}:
            sdk.fault=name
            record=service.ensure(event)
            expected={'invalid_json':'decode_failed','truncated':'output_truncated',
                      'invalid_reference':'validation_failed','network_failure':'model_failed'}[name]
            passed=record is None and diagnostics[-1]['status']==expected
        elif name=='publish_rejected':
            with patch.object(store.conversation_summaries,'publish',return_value=None):
                record=service.ensure(event)
            passed=record is None and diagnostics[-1]['status']=='publish_rejected'
        else:
            record=service.ensure(event)
            if record is None:
                return {'id':name,'passed':False,'reason':'Initial publication failed'}
            if name=='previous_reused':
                sdk.items=seed_stage(store,event,corpus['cases'][1]['stages'][1],corpus,mapping)
                sdk.fault='invalid_json'
                fallback=service.ensure(event)
                passed=fallback is not None and fallback['version']==record['version'] and diagnostics[-1]['previous_reused']
            elif name=='dependency_changed':
                store.preferences.update(event,0,{'pace':'轻松'})
                passed=store.conversation_summaries.latest(event) is None
            elif name=='source_deleted':
                with store._connect() as db:
                    db.execute('DELETE FROM conversation_turns WHERE id=?',(mapping['t1'],))
                passed=store.conversation_summaries.latest(event) is None
            elif name=='cross_owner':
                passed=store.conversation_summaries.latest(replace(event,sender_id='another-owner')) is None
            else:
                raise ValueError('Unknown fault case')
        return {'id':name,'passed':bool(passed),'diagnostics':diagnostics}


def run(corpus_path,ids=None):
    corpus=load_cases(corpus_path)
    selected=[case for case in corpus['cases'] if not ids or case['id'] in ids]
    if not selected:
        raise ValueError('No selected cases')
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ,OFFLINE_ENV))
        stack.enter_context(patch('requests.sessions.Session.request',side_effect=AssertionError('Offline mode forbids HTTP')))
        stack.enter_context(patch('socket.create_connection',side_effect=AssertionError('Offline mode forbids network')))
        results=[]
        for case in selected:
            token=MODEL_EVENT.set('summary-offline:'+case['id'])
            try:
                results.append(evaluate_case(case,corpus))
            finally:
                MODEL_EVENT.reset(token)
    files=['services/conversation_summary_service.py','infrastructure/conversation_summary_repository.py','core/conversation_summary.py',
           'core/summary_contract.py','core/summary_semantics.py','prompts/conversation_summary.md',
           'agents/context_builder.py','core/context_policy.py','core/model_policy.py','scripts/evaluate_summary_quality.py']
    return {'generated_at':datetime.now(timezone.utc).isoformat(),'mode':'offline-scripted-contracts',
            'corpus_hash':hashlib.sha256(Path(corpus_path).read_bytes()).hexdigest(),
            'source_hashes':{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in files},
            'mechanism_passed':all(case['mechanism_passed'] for case in results),
            'known_guard_failures':[check['id'] for case in results for check in case.get('checks',[])
                                    if 'expected_accepted' in check and not check['passed']],
            'real_model_quality':None,'real_model_calls':0,'external_tool_calls':0,'results':results,
            'note':'Scripted expected responses verify contracts only; no credentials, production DB or model API accessed. Guard failures remain visible.'}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases',type=Path,default=ROOT/'evals/summary_quality_cases_v2.json')
    parser.add_argument('--ids',default='')
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--strict-guards',action='store_true',help='Also fail when semantic guard counterexamples remain')
    args=parser.parse_args()
    report=run(args.cases,set(args.ids.split(',')) if args.ids else None)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({key:report[key] for key in ('mechanism_passed','known_guard_failures','real_model_calls')},ensure_ascii=False))
    raise SystemExit(0 if report['mechanism_passed'] and not (args.strict_guards and report['known_guard_failures']) else 1)
