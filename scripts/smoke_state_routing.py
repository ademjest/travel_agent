"""New 100000-token authorization: isolated model comparisons, fixed external tools."""
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import re
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/'tests'))
from agents.intent_compiler import IntentCompiler
from agents.trip_planner import TripPlanner
from core.chat_transport import ChatEvent
from core.model_budget import MODEL_EVENT,model_call_context
from core.context_contract import response_text
from agents.context_builder import ContextBuilder,render_untrusted_context
from core.tasks import TaskUpdate
from core.tool_result import ToolResult
from infrastructure.memory_store import MemoryStore
from infrastructure.model_gateway import ModelGateway
from services.semantic_task_service import SemanticTaskService
from services.trip_service import TripService
from scripts.smoke_summary_live import Budget,BoundedClient,CountingTransport
from scripts.verify_v3_regression import source_snapshot
from test_trips import PLACES,plan_value


class FixedTravel:
    manages_read_retries=True
    def __init__(self): self.calls=[]
    def execute_result(self,name,args):
        self.calls.append((name,args))
        return ToolResult('completed',name,data='固定测试资料：'+json.dumps(args,ensure_ascii=False)+'；晴，20℃。')


class FixedMap:
    def __init__(self): self.calls=[]
    def search_places(self,*args,**kwargs):
        self.calls.append(('places',args))
        return [*deepcopy(list(PLACES.values())),
                {'id':'hotel-a','name':'测试汉庭酒店','type':'住宿服务','address':'测试路','rating':4.8},
                {'id':'hotel-b','name':'测试如家酒店','type':'住宿服务','address':'测试路二号','rating':4.6}]
    def route_for_pois(self,*args,**kwargs):
        self.calls.append(('route',kwargs))
        return {'duration_seconds':600,'distance_meters':800}


def main(round2=False):
    from dotenv import load_dotenv
    import httpx
    from openai import OpenAI
    load_dotenv(ROOT/'.env',override=False)
    load_dotenv(ROOT.parent/'.env',override=False)
    key,base=os.getenv('LLM_API_KEY',''),os.getenv('LLM_BASE_URL','')
    if not key or not base:
        raise ValueError('Configured model connection required')
    suffix='-round2' if round2 else ''
    audit=ROOT/f'evals/state-routing-live{suffix}-2026-10-18.json'
    root=ROOT/f'.upgrade-testing/state-routing-live{suffix}-20261018'
    limit=100000
    parent=None
    if round2:
        parent_path=ROOT/'evals/state-routing-live-2026-10-18.json'
        parent=json.loads(parent_path.read_text(encoding='utf-8'))
        state=parent['budget']
        if state['pending_reservation'] or any(not r.get('usage_complete') for r in parent['physical_requests']):
            raise ValueError('Previous usage must be settled before continuation')
        if sum(r['accounted_tokens'] for r in parent['physical_requests']) != state['accounted_tokens']:
            raise ValueError('Inconsistent previous audit')
        limit-=state['accounted_tokens']
    if root.exists(): raise FileExistsError('Never overwrite an existing test run')
    root.mkdir(parents=True)
    budget=Budget(audit,12,limit)
    budget.report.update(model='gpt-5.6-sol',authorization='NEW 100000-token state/routing grant; previous grant untouched',
        source_files_before=source_snapshot(),database_root=str(root),production_accessed=False,
        external_tools='fixed synthetic map/weather snapshots; no live map/search acceptance',
        summary_prompt_sha256=hashlib.sha256((ROOT/'docs/prompt_design/abstract_design_prompts.md').read_bytes()).hexdigest())
    if parent:
        budget.report['parent']={'path':str(parent_path),'sha256':hashlib.sha256(parent_path.read_bytes()).hexdigest(),
                                 'accounted_tokens':parent['budget']['accounted_tokens'],'total_authorization':100000}
    budget.report['script_sha256']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    budget.save()
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ,{'LLM_MODEL_ID':'gpt-5.6-sol','LLM_INTENT_MODEL_ID':'gpt-5.6-sol',
            'AI_SUMMARY_ENABLED':'false','AI_VECTOR_ENABLED':'false','AI_DAILY_COST_LIMIT':'0',
            'AI_MONTHLY_COST_LIMIT':'0','AI_DAILY_TOKEN_LIMIT':'0','AI_MONTHLY_TOKEN_LIMIT':'0'}))
        stack.enter_context(patch('requests.sessions.Session.request',side_effect=AssertionError('External tools disabled')))
        stack.enter_context(patch('infrastructure.public_http.request_public',side_effect=AssertionError('External tools disabled')))
        http=stack.enter_context(httpx.Client(transport=CountingTransport(budget),follow_redirects=False,timeout=90))
        sdk=stack.enter_context(OpenAI(api_key=key,base_url=base,max_retries=0,http_client=http,timeout=90))
        bounded=BoundedClient(sdk,budget)
        def run(name,mode,kind,text):
            if budget.stop_reason:return
            row={'id':name,'mode':mode,'kind':kind,'input':text,'started_tokens':budget.tokens,
                 'started_calls':budget.physical_calls}
            budget.report['results'].append(row);budget.save()
            token=MODEL_EVENT.set('state-routing:'+name)
            started=time.monotonic()
            try:
                with patch.dict(os.environ,{'AI_RULE_ROUTING':'true' if mode=='optimized' else 'false',
                                           'AI_LOCAL_PLANNING':'true' if mode=='optimized' else 'false'}):
                    store=MemoryStore(root/(name+'.db'))
                    client=ModelGateway(store,bounded,default_model='gpt-5.6-sol')
                    amap=FixedMap();travel=FixedTravel()
                    planner=TripPlanner(client,'gpt-5.6-sol',amap)
                    service=TripService(store,planner)
                    semantic=SemanticTaskService(store,IntentCompiler(client,'gpt-5.6-sol'),service,travel_service=travel)
                    seed=ChatEvent('web','group','seed',name,'test-owner','创建测试行程')
                    spec={'destination':'武汉','start_date':'2030-10-01','day_count':3,'gentle':False,'no_night':False,
                          'must_visit':['湖北省博物馆'],'preferences':[],'hotel_requested':True,
                          'budgets':{'accommodation':{'value':'420','unit_text':'元/间/晚','source':'420'}}}
                    places={p['id']:p for p in amap.search_places('武汉','测试')}
                    plan=planner.validate_plan({**plan_value(),'hotel_ids':['hotel-a']},spec,places)
                    saved={'trip_id':store.trips.new_id(seed.event_key),'title':'武汉三日行程','status':'active',
                           'spec':spec,'plan':plan,'sources':places}
                    store.trips.commit(seed,store.begin_event(seed.event_key),TaskUpdate('itinerary','completed','测试',{}),'seed',trip=saved)
                    event=ChatEvent('web','group','request',name,'test-owner',text,occurred_at=datetime.now(timezone.utc))
                    claim=store.begin_event(event.event_key)
                    if kind=='state_qa':
                        for i,amount in enumerate((320,350)):
                            change=ChatEvent('web','group',f'change-{i}',name,'test-owner',f'酒店预算改为{amount}元/间/晚')
                            semantic.handle(change,store.begin_event(change.event_key))
                        context=ContextBuilder(store).build(event)
                        row['context_chars']=context.total_chars
                        with model_call_context('tool_answer',prompt_version='state-qa-acceptance-v1'):
                            response=client.chat.completions.create(model='gpt-5.6-sol',max_completion_tokens=1024,
                                response_format={'type':'json_object'},messages=[
                                    {'role':'system','content':'依据提供的业务状态回答用户问题。只返回JSON，saved_budget和pending_budget为数值字符串，unit为计价单位。不将待确认当作已保存。'},
                                    {'role':'user','content':render_untrusted_context(context)},
                                    {'role':'user','content':text}])
                        answer=json.loads(response_text(response))
                        row['answer']=answer
                        row['quality_pass']=answer.get('saved_budget')=='420' and answer.get('pending_budget')=='350'
                        row['saved_version']=store.trips.get(event,saved['trip_id'])['version']
                        row['quality_pass']=row['quality_pass'] and row['saved_version']==1
                        return
                    elif kind=='day':
                        reply=service.handle(event,claim)
                    else:
                        reply=semantic.handle(event,claim)
                    row['reply']=reply if isinstance(reply,str) else type(reply).__name__
                    current=store.trips.get(event,saved['trip_id'])
                    draft=store.trips.draft(event,current)
                    row['external_fixture_calls']=len(amap.calls)-1+len(travel.calls)
                    if kind in {'budget','day'}:
                        if not draft: raise AssertionError('Expected a versioned preview')
                        candidate=draft['candidate']
                        row['before_confirmation_version']=current['version']
                        row['diff']=draft['task'].slots.get('change_diff')
                        if kind=='budget':
                            budget_value=candidate['spec']['budgets']['accommodation']
                            row['quality_pass']=bool(re.fullmatch(r'320(?:元/间/晚)?',budget_value['value'])) and budget_value.get('unit_text')=='元/间/晚' and candidate['plan']['days']==plan['days']
                        else:
                            row['quality_pass']=candidate['plan']['days'][0]==plan['days'][0] and candidate['plan']['days'][2]==plan['days'][2]
                        confirm=ChatEvent('web','group','confirm',name,'test-owner','确认行程修改')
                        row['confirmation_reply']=service.handle(confirm,store.begin_event(confirm.event_key))
                        row['saved_version']=store.trips.get(confirm,saved['trip_id'])['version']
                        row['quality_pass']=row['quality_pass'] and row['saved_version']==2 and row['before_confirmation_version']==1
                    elif kind=='query':
                        row['quality_pass']=len(travel.calls)==1 and travel.calls[0][1].get('location')=='武汉'
                    elif kind=='compound':
                        row['quality_pass']=[args.get('location') for _,args in travel.calls]==['武汉','长沙']
                    else:
                        row['quality_pass']=not travel.calls and '尚未执行' in str(reply)
            except Exception as exc:
                row['failure_type']=type(exc).__name__
                row['quality_pass']=False
            finally:
                row.update(tokens=budget.tokens-row['started_tokens'],physical_requests=budget.physical_calls-row['started_calls'],
                           elapsed_seconds=round(time.monotonic()-started,3))
                MODEL_EVENT.reset(token);budget.save()
                print(json.dumps({k:row.get(k) for k in ('id','quality_pass','tokens','physical_requests','failure_type')},ensure_ascii=False),flush=True)
        if round2:
            run('state-qa','optimized','state_qa','我的行程已经保存的酒店预算，以及最新待确认的预算分别是多少？')
            run('two-city','optimized','compound','查询天气武汉；查询天气长沙')
        run('query-baseline','baseline','query','武汉现在天气怎么样？')
        run('query-optimized','optimized','query','武汉现在天气怎么样？')
        run('budget-optimized','optimized','budget','酒店预算改为320元/间/晚')
        run('budget-baseline','baseline','budget','酒店预算改为320元/间/晚')
        run('day-baseline','baseline','day','第二天改为室内')
        run('day-optimized','optimized','day','第二天改为室内')
        run('mixed-guard','optimized','guard','查询天气武汉；取消我的预约提醒')
    budget.report['source_files_after']=source_snapshot()
    budget.report['source_unchanged']=budget.report['source_files_before']==budget.report['source_files_after']
    budget.save()


if __name__=='__main__':main('--round2' in sys.argv)
