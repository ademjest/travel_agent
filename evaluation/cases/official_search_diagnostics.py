"""Bounded provider capture and zero-network identity replay for U1 debugging."""
import argparse
from collections import deque
from datetime import datetime, timezone
import json
from pathlib import Path

from infrastructure.execution_diagnostics import SearchTrace
from infrastructure.search_client import SearchClient
from infrastructure.web_reader import read_page
from services.official_sources import OfficialSources
from tools.research.official_links import allowed, official_outlinks, verified_link, identity_checks

TARGETS = (('湖北省博物馆','武汉'),('六朝博物馆','南京'),('总统府','南京'),
           ('南京博物院','南京'),('南京城墙景区(神策门至太平门)','南京'))


def replay(snapshot):
    registry = OfficialSources()
    results = []
    for i,record in enumerate(snapshot['targets']):
        target = {'name':record['name'],'target_id':f'target-{i+1}'}
        scope = {'city':record['city']}
        sources = [dict(s) for s in snapshot['pages'] if s.get('status')=='read']
        link = verified_link(target,record['city'],sources,registry,scope)
        candidates=[]
        for query in record['queries']:
            for rank,row in enumerate(query['results'],1):
                page=next((s for s in snapshot['pages'] if s.get('requested_url')==row['url']),None)
                stage=('filtered' if not allowed(row['url'],scope) else 'not_read' if page is None else
                       'read_failed' if page.get('status')!='read' else 'verified' if link and page['url']==link['url'] else 'identity_unconfirmed')
                candidates.append({'query':query['query'],'rank':rank,'url':row['url'],'stage':stage,
                    'read_error':page.get('access_error') if page else None,
                    'checks':identity_checks(target,record['city'],page,sources,registry,scope) if page else None})
        results.append({'name':target['name'],'queries':record['queries'],'official_link':link,
            'candidate_trace':candidates,
            'readable_candidates':sum(s.get('status')=='read' for s in snapshot['pages'] if
                any(s.get('requested_url')==r['url'] for q in record['queries'] for r in q.get('results',[])))})
    return {'summary':{'total':len(results),'verified':sum(bool(r['official_link']) for r in results)},
            'results':results,'real_model_calls':0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--capture',action='store_true')
    parser.add_argument('--continue-pages',action='store_true')
    parser.add_argument('--snapshot',type=Path,required=True)
    parser.add_argument('--trace-dir',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError('Do not overwrite previous evidence')
    if args.capture:
        import os
        from dotenv import load_dotenv
        load_dotenv()
        search = SearchClient(os.getenv('SEARCH_API_KEY',''))
        data = {'targets':[],'pages':[],'captured_at':datetime.now(timezone.utc).isoformat()}
        registry = OfficialSources()
        with SearchTrace(args.trace_dir,max_requests=50) as trace:
            queue = deque()
            for name,city in TARGETS:
                record = {'name':name,'city':city,'queries':[]}
                data['targets'].append(record)
                for query in (name+' 官方网站',city+' '+name+' 官方网站 主办单位'):
                    rows = search.search(query)
                    record['queries'].append({'query':query,'results':rows})
                queue.extend((name,city,r['url']) for q in record['queries'] for r in q['results'][:2])
                args.snapshot.parent.mkdir(parents=True,exist_ok=True)
                args.snapshot.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
            seen = set()
            while queue and len(seen)<20:
                name,city,url = queue.popleft()
                if url in seen or not allowed(url,{}):
                    continue
                seen.add(url)
                page = {'id':len(data['pages'])+1,'requested_url':url,'url':url,'status':'unavailable','text':''}
                try:
                    page.update(read_page(url,url_allowed=lambda candidate:allowed(candidate,{})))
                except (ValueError,OSError) as error:
                    page['access_error'] = getattr(error,'code',type(error).__name__)
                data['pages'].append(page)
                trace.record('pages',page)
                for _,link,_ in official_outlinks({'name':name},city,data['pages'],registry,{}):
                    if link['url'] not in seen:
                        queue.appendleft((name,city,link['url']))
                args.snapshot.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
            data['physical_requests'] = trace.requests
            args.snapshot.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    elif args.continue_pages:
        data = json.loads(args.snapshot.read_text(encoding='utf-8'))
        prior = data.get('physical_requests',0)
        seen = {p['requested_url'] for p in data['pages']}
        registry = OfficialSources()
        pending = []
        for record in data['targets']:
            entry = registry.match(record['name'],record['city'])
            if entry:
                pending.extend(entry['policy_urls'][:1])
            for q in record['queries']:
                pending.extend(r['url'] for r in q['results'] if record['name'] in r.get('title','')
                    and not any(host in r['url'] for host in ('wikipedia.org','baidu.com','eztravel','weixin.qq.com')))
                pending.extend(r['url'].replace('http://','https://',1) for r in q['results'] if
                    r['url'].startswith('http://') and '.gov.cn/' in r['url'])
        with SearchTrace(args.trace_dir,max_requests=50-prior) as trace:
            for url in dict.fromkeys(pending):
                if url in seen or len(seen)>=20 or not allowed(url,{}):
                    continue
                seen.add(url)
                page={'id':len(data['pages'])+1,'requested_url':url,'url':url,'status':'unavailable','text':''}
                try:
                    page.update(read_page(url,url_allowed=lambda candidate:allowed(candidate,{})))
                except (ValueError,OSError) as error:
                    page['access_error']=getattr(error,'code',type(error).__name__)
                data['pages'].append(page)
                trace.record('pages',page)
                data['physical_requests']=prior+trace.requests
                args.output.with_suffix('.snapshot.json').write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    else:
        data = json.loads(args.snapshot.read_text(encoding='utf-8'))
    result = replay(data)
    result['mode'] = 'live_provider' if args.capture else 'offline'
    result['physical_requests'] = data.get('physical_requests',0) if args.capture else 0
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result['summary']))


if __name__=='__main__':
    main()
