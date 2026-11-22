"""Conversation/draft acceptance through production Web assembly, fixed providers by default."""
import argparse,json,sys,time,unittest
from pathlib import Path
from evaluation.support import ROOT,EVALUATION_BUDGET

# Several assertions may support one journey; each test executes once.
MATRIX={
 'J01':['test_J01_referenced_','test_J01_J02_'], 'J02':['test_J01_J02_'], 'J03':['test_J03_'],
 'J04':['test_J04_'],'J05':['test_J05_'],'J06':['test_J06_J07_'],'J07':['test_J06_J07_'],
 'J08':['test_J08_J09_'],'J09':['test_J01_J02_','test_J08_J09_'],
 'J10':['test_duplicate_ids_'],'J11':['test_expired_create_'],'J12':['test_J12_'],
 'J13':['test_expired_discard_','test_J14_'],'J14':['test_J14_','test_bound_confirmation_'],'J15':['test_J15_'],
 'J16':['test_duplicate_ids_'],'J17':['test_J17_','test_commit_guard_','test_explicit_new_budget_'],
 'J18':['test_J18_'],'J19':['test_J19_'],'J20':['test_J20_'],'J21':['test_J20_'],
 'J22':['test_J22_'],'J23':['test_date_shift_replans_','test_booking_reminder_is_','test_manual_reminder_time_'],
 'J24':['test_J24_','test_tentative_dates_missing_','test_source_restriction_does_']}

AUDIT_MATRIX={
 'A01':['test_A01_A04_'],'A02':['test_A02_A06_'],'A03':['test_A03_'],'A04':['test_A01_A04_'],
 'A05':['test_A05_','test_F03_'],'A06':['test_A02_A06_','test_A20_'],'A07':['test_F05_','test_A07_'],
 'A08':['test_A08_F06_'],'A09':['test_A09_'],'A10':['test_A10_','test_required_subactivities_'],'A11':['test_F08_','test_A11_'],
 'A12':['test_F09_','test_A12_'],'A13':['test_F10_','test_A13_'],'A14':['test_A20_','test_A08_F06_'],
 'A15':['test_A15_','test_V03_'],'A16':['test_F07_','test_A16_','test_J19_','test_J18_'],
 'A17':['test_A17_V04_','test_J20_'],'A18':['test_A18_V01_','test_V02_'],
 'A19':['test_J22_','test_J15_','test_J18_','test_duplicate_ids_'],'A20':['test_A20_','test_complete_creation_with_semicolons_']}

def offline():
    sys.path.insert(0,str(ROOT/'tests'))
    suite=unittest.TestSuite()
    def flatten(s):
        for t in s:
            if isinstance(t,unittest.TestSuite):yield from flatten(t)
            else:yield t
    tests=list(flatten(unittest.defaultTestLoader.loadTestsFromNames([
        'test_draft_actions','test_post_delivery_repairs','test_intercity_planning','test_trips'])))
    selected=[t for t in tests if any(t._testMethodName.startswith(p) for prefixes in {**MATRIX,**AUDIT_MATRIX}.values() for p in prefixes)]
    result=unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(selected))
    failures={t.id():trace for t,trace in result.failures+result.errors}
    rows=[]
    for identity,prefixes in {**MATRIX,**AUDIT_MATRIX}.items():
        ids=[t.id() for t in selected if any(t._testMethodName.startswith(p) for p in prefixes)]
        rows.append({'id':identity,'mode':'offline_production_assembly','test_ids':ids,
            'passed':bool(ids) and all(i not in failures for i in ids),
            'browser_required':identity=='J21'})
    return {'schema_version':'conversation-workflow-v1','fixture_version':'workflow-2030-09-30-v1',
        'reference_time':'2030-09-30T08:00:00+00:00','results':rows,
        'failures':failures,'physical_model_calls':0,'provider_calls':0,
        'summary':{'total':44,'passed':sum(r['passed'] for r in rows),'legacy_total':24,'audit_total':20,'unique_tests':result.testsRun,
            'browser_status':'separate_evidence_required'}}

def live(config,*,providers=False,output=None,resume=None,provider_receipt=None):
    """Real models start with empty conversation state; external replay is labelled separately."""
    import asyncio,copy,tempfile,threading,sqlite3
    from contextlib import closing
    from datetime import datetime,timezone
    from types import SimpleNamespace
    from unittest.mock import patch
    from dotenv import load_dotenv
    load_dotenv(config)
    from adapters.web_app import create_web_app
    from core.web_settings import WebSettings
    from core.chat_transport import ChatEvent
    from infrastructure.web_repository import OWNER
    from fastapi.testclient import TestClient
    from infrastructure.amap_client import AmapError
    from infrastructure.model_gateway import usage_report
    import urllib3.connectionpool
    budget=EVALUATION_BUDGET.get();started=time.monotonic();lock=threading.Lock()
    records=[];counts={'amap':0,'wendao':0};caps={'amap':24,'wendao':1};rows=[];not_run=[];snapshots={}
    if providers:
        fixture={'reference_time':'2026-11-02T08:00:00+00:00','places':{},
            'request':'请规划2026年11月7日至9日的杭州三日游，两位成年人，节奏轻松。第一天早上到杭州市区，第三天下午离开；先不用查询跨城交通。必去中国丝绸博物馆、六和塔和西湖风景名胜区，市内优先步行或公交地铁，少打车。先生成草案，不要直接保存。',
            'requirements':{'must_visit':['中国丝绸博物馆','六和塔','西湖风景名胜区']},'data_origin':'synthetic_public'}
    else:
        fixture=json.loads((ROOT/'tests/fixtures/request_208_public_2026-11-02.json').read_text(encoding='utf-8'))

    provider_output=output.with_suffix('.providers.json')
    if provider_receipt:
        previous=json.loads(provider_receipt.read_text(encoding='utf-8'));counts.update(previous['counts']);records.extend(previous['requests'])
    if resume:
        with closing(sqlite3.connect(resume.as_uri()+'?mode=ro',uri=True)) as db:
            data=json.loads(db.execute("SELECT slots_json FROM travel_tasks WHERE status='collecting' AND json_extract(slots_json,'$.candidate_trip') IS NOT NULL ORDER BY rowid DESC LIMIT 1").fetchone()[0])
            snapshots.update(data['candidate_trip']['sources'])
    original_pool=urllib3.connectionpool.HTTPConnectionPool.urlopen
    def counted(pool,method,url,*args,**kwargs):
        host=getattr(pool,'assert_hostname',None) or pool.host
        provider='amap' if host=='restapi.amap.com' else 'wendao' if host=='wendao-skill-prod.ctrip.com' else ''
        if not provider:raise RuntimeError('This acceptance forbids other provider, webpage and QQ HTTP')
        with lock:
            if counts[provider]>=caps[provider] or time.monotonic()-started>1800:raise RuntimeError('Provider acceptance budget exhausted')
            counts[provider]+=1;row={'provider':provider,'physical_attempt':counts[provider],'status':'submitted'};records.append(row)
            provider_output.write_text(json.dumps({'limits':caps,'counts':counts,'requests':records},indent=2),encoding='utf-8')
        try:
            response=original_pool(pool,method,url,*args,**kwargs);row.update(status='returned',http_status=response.status);return response
        except BaseException as error:row.update(status='unknown',error_type=type(error).__name__);raise
        finally:
            with lock:provider_output.write_text(json.dumps({'limits':caps,'counts':counts,'requests':records},indent=2),encoding='utf-8')
    class SavedMap:
        def __init__(self,places):self.places=places
        def search_places(self,*args,**kwargs):return tuple(copy.deepcopy(list(self.places.values())))
        def route_for_pois(self,*args,**kwargs):raise AmapError('固定资料未包含当前路线，移动耗时保持待核对。')
        def non_driving_route(self,*args,**kwargs):raise AmapError('固定资料未包含接驳资料。')
    settings=WebSettings.from_env().travel_settings()
    if not settings.llm_configured:raise ValueError('The existing project model configuration is required')
    with patch.object(urllib3.connectionpool.HTTPConnectionPool,'urlopen',counted):
        for enabled in (True,False):
            if budget.stop_reason:
                not_run.append({'id':'cold-disabled' if not enabled else 'cold-enabled','reason':budget.stop_reason});break
            with tempfile.TemporaryDirectory(prefix='audit-cold-',dir=output.parent) as folder:
                resuming=bool(enabled and resume)
                if resuming:
                    with closing(sqlite3.connect(resume.as_uri()+'?mode=ro',uri=True)) as source:
                        with closing(sqlite3.connect(Path(folder)/'travel_bot.db')) as target:source.backup(target)
                app=create_web_app(WebSettings(data_dir=Path(folder),open_browser=False),settings,start_workers=False)
                runtime=app.state.runtime;trip=runtime.application.trip_service;research=trip.research_service;store=app.state.repository.store
                assert store.database_path.resolve().is_relative_to(Path(folder).resolve())
                actual=enabled and providers and not resuming
                if actual:
                    original_search=trip.planner.amap.search_places
                    def capture_search(*args,**kwargs):
                        places=original_search(*args,**kwargs)
                        for p in places:snapshots[p['id']]=copy.deepcopy(p)
                        return places
                    trip.planner.amap.search_places=capture_search
                    if not research.wendao.configured:raise ValueError('The existing Wendao service is not configured')
                else:
                    saved=SavedMap(snapshots or fixture['places']);trip.planner.amap=saved;runtime.travel_service.amap=saved
                    if enabled and not resuming:
                        research.wendao=SimpleNamespace(configured=True,query=lambda *a,**kw:{'status':'completed','physical_calls':0,
                            'retrieved_at':fixture['reference_time'],'content_hash':'request208-fixed','text':fixture['wendao_text']})
                    else:research.wendao=None
                label='wendao-enabled' if enabled else 'wendao-disabled';calls_before=budget.physical_calls
                with TestClient(app,base_url='http://127.0.0.1:8080') as client:
                    boot=client.get('/api/bootstrap').json();headers={'x-csrf-token':boot['csrf_token']}
                    if resuming:
                        with store._connect() as db:cid=db.execute('SELECT id FROM web_conversations ORDER BY rowid LIMIT 1').fetchone()[0]
                    else:cid=client.post('/api/conversations',headers=headers).json()['id']
                    event=ChatEvent('web','group','read',cid,OWNER,'')
                    if not resuming:assert not store.trips.current_drafts(event) and not store.trips.list_for_owner(event)
                    def context():return client.get(f'/api/conversations/{cid}/context').json()
                    def candidate():
                        tasks=store.trips.current_drafts(event)
                        return copy.deepcopy(tasks[0].slots['candidate_trip']) if len(tasks)==1 else None
                    last_job=[None]
                    def send(text,key):
                        response=client.post(f'/api/conversations/{cid}/messages',headers=headers,json={'content':text,'client_request_id':key})
                        response.raise_for_status();last_job[0]=response.json()['job_id'];asyncio.run(app.state.worker.run_once())
                        return client.get(f'/api/conversations/{cid}/messages').json()['items'][-1]['content']
                    def action(draft,kind,key):
                        response=client.post(f'/api/conversations/{cid}/drafts/{draft["id"]}/actions',headers=headers,
                            json={'action':kind,'version':draft['version'],'client_request_id':key});response.raise_for_status()
                        asyncio.run(app.state.worker.run_once())
                    reply=send(fixture['request'],label+'-create') if not resuming else '复核本轮先前已从空白创建的草案，未重发模型和供应商。';current=candidate()
                    if current:
                        spec=current['spec'];bindings=spec.get('required_entities',{})
                        required={b['poi_id'] for b in bindings.values() if b['status']=='bound'}
                        used={p['poi_id'] for d in current['plan']['days'] for a in d['activities'] for p in [a,*a.get('subactivities',[])]}
                        passed=(set(spec['must_visit'])==set(fixture['requirements']['must_visit']) and len(required)==3 and required<=used
                            and spec.get('intercity_request',{}).get('scope')=='none'
                            and spec.get('city_windows',{}).get('arrival',{}).get('day_index')==1
                            and spec.get('city_windows',{}).get('departure',{}).get('day_index')==3
                            and spec['start_date']=='2026-11-07' and spec['end_date']=='2026-11-09'
                            and bool(context()['confirmations']) and not store.trips.list_for_owner(event))
                    else:passed=False
                    rows.append({'id':label+('-prior-created-draft-recheck' if resuming else '-cold-create'),'passed':passed,'started_from_blank':not resuming,'seeded_draft':False,
                        'models':'real','data_origin':fixture.get('data_origin','recorded_public_fixture'),'providers':'live' if actual else 'recorded_data','reply':reply,
                        'candidate':current,'model_calls':budget.physical_calls-calls_before,
                        'diagnostics':client.get(f'/api/conversations/{cid}/jobs/{last_job[0]}/diagnostics').json() if last_job[0] else {'source_state':str(resume)}})
                    if not passed:
                        with closing(sqlite3.connect(output.parent/(label+'-failed-state.db'))) as target:
                            with store._connect() as source:source.backup(target)
                        break
                    if not enabled and not budget.stop_reason:
                        before=copy.deepcopy(current);reply=send('餐饮预算默认人均不低于70元，只调整本次预算，其他要求保持。',label+'-lower')
                        current=candidate();b=current['spec'].get('budgets',{}).get('food',{}).get('bounds',{}) if current else {}
                        passed=bool(current and b.get('lower')==70 and b.get('upper') is None and b.get('lower_inclusive')
                            and current['plan']['days']==before['plan']['days'])
                        rows.append({'id':'real-lower-budget','passed':passed,'before':before['spec'].get('budgets',{}),'after':current['spec'].get('budgets',{}) if current else {},'reply':reply})
                        if not passed:break
                        if not budget.stop_reason:
                            before=copy.deepcopy(current);task_keys=[(t.task_id,t.version) for t in store.trips.current_drafts(event)]
                            reply=send('只解释当前餐饮预算，不要修改方案。',label+'-read')
                            passed=before==candidate() and task_keys==[(t.task_id,t.version) for t in store.trips.current_drafts(event)] and '当前候选的价格说明' in reply
                            rows.append({'id':'real-readonly-budget','passed':passed,'reply':reply})
                            if not passed:break
                        else:not_run.append({'id':'real-readonly-budget','reason':budget.stop_reason})
                    prior=copy.deepcopy(candidate());calls=budget.physical_calls;provider_counts=counts.copy()
                    action(context()['confirmations'][0],'revalidate',label+'-revalidate');fresh=context()['confirmations'][0]
                    renewed=candidate();preserved=renewed['spec']['must_visit']==prior['spec']['must_visit'] and renewed['spec'].get('budgets')==prior['spec'].get('budgets') and renewed['plan']['days']==prior['plan']['days']
                    if enabled:action(fresh,'discard',label+'-discard');effect=not store.trips.current_drafts(event) and not store.trips.list_for_owner(event)
                    else:
                        response=client.post(f'/api/conversations/{cid}/confirmations/{fresh["id"]}',headers=headers,
                            json={'version':fresh['version'],'client_request_id':label+'-save'});response.raise_for_status();asyncio.run(app.state.worker.run_once())
                        saved_trip=store.trips.list_for_owner(event);effect=len(saved_trip)==1 and saved_trip[0]['version']==1 and saved_trip[0]['spec'].get('budgets')==renewed['spec'].get('budgets')
                    rows.append({'id':label+'-revalidate-'+('discard' if enabled else 'save'),'passed':bool(preserved and effect and budget.physical_calls==calls and counts==provider_counts),
                        'models':'none_for_control','provider_calls_added':{k:counts[k]-provider_counts[k] for k in counts},'persisted_effect':effect})
                    rows[-1]['usage']=usage_report(store)
                    with closing(sqlite3.connect(output.parent/(label+'-state.db'))) as target:
                        with store._connect() as source:source.backup(target)
    return {'schema_version':'conversation-workflow-v2-audit','results':rows,'not_run':not_run,'provider_counts':counts,
        'provider_fees':'unknown_when_live','physical_model_calls':budget.physical_calls,
        'summary':{'total':len(rows),'passed':sum(bool(r['passed']) for r in rows),'unexecuted':len(not_run),
            'cold_create_cases':sum(r['id'].endswith('cold-create') and r['passed'] for r in rows)}}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--config',type=Path);parser.add_argument('--live-providers',action='store_true');parser.add_argument('--carry-model-receipt',type=Path);parser.add_argument('--resume-live-state',type=Path);parser.add_argument('--carry-provider-receipt',type=Path);args=parser.parse_args()
    if args.output.exists():raise FileExistsError('Do not overwrite acceptance evidence')
    try:report=live(args.config,providers=args.live_providers,output=args.output,resume=args.resume_live_state.resolve() if args.resume_live_state else None,provider_receipt=args.carry_provider_receipt) if EVALUATION_BUDGET.get() else offline()
    except Exception as error:
        import traceback
        frames=traceback.extract_tb(error.__traceback__)
        report={'results':[{'id':'execution','passed':False,'failure_type':type(error).__name__,'failed_at':[{'file':Path(f.filename).name,'line':f.lineno,'function':f.name} for f in frames]}],
            'summary':{'total':1,'passed':0},'physical_model_calls':EVALUATION_BUDGET.get().physical_calls if EVALUATION_BUDGET.get() else 0}
    args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report['summary'],ensure_ascii=False))
    if report['summary']['passed']!=report['summary']['total']:raise SystemExit(1)

if __name__=='__main__':main()
