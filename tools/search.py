"""Standalone website search and reusable bounded navigation execution."""
from datetime import datetime, timezone
from urllib.parse import urlsplit, urldefrag
import re
import time
from copy import deepcopy

from core.chat_transport import ChatEvent
from core.execution_scope import ensure_execution_active, ExecutionRevoked
from core.tool_result import AgentToolContext, ToolResult, OFFICIAL_WEBSITE_TOOL
from infrastructure.execution_diagnostics import SEARCH_TRACE
from infrastructure.web_reader import read_page
from infrastructure.skill_manager import skill_asset
from services.official_sources import OfficialSources, scoped_url, public_policy_url
from tools.research.sources import source_selection, bind_source_selection
from tools.research.official_links import (VERSION, allowed, navigation_key, official_outlinks,
    operator_declaration, priority, verified_link, current_link, markdown_link, identity_checks, selected_link, search_match)


class SearchTool:
    def __init__(self, store, search, *, reader=read_page, registry=None, enabled=True):
        self.store, self.search, self.reader = store, search, reader
        self.registry = registry if registry is not None else OfficialSources()
        self.enabled, self.executor = enabled, None

    def _save(self, event, data, stage=None):
        ensure_execution_active()
        self.store.research.save(event, stage or data.get('stage', 'searching'), data)

    @staticmethod
    def _source(data, **values):
        source = {'id':max((s['id'] for s in data['sources']), default=0)+1,
                  'retrieved_at':datetime.now(timezone.utc).isoformat(), **values}
        data['sources'].append(source)
        return source

    def read_source(self, event, data, url, target, entry, scope, progress, available, *, source_type='web'):
        navigation = source_type == 'navigation'
        permitted = lambda candidate: allowed(candidate, scope) if navigation else self.fact_allowed(candidate, scope)
        if not permitted(url) or scope.get('offline') or not available():
            return None
        url = urldefrag(url)[0]
        policy = lambda candidate: permitted(candidate) and (navigation or entry is None or scoped_url(candidate, entry))
        matches = [s for s in data['sources'] if url in
            (urldefrag(s.get('requested_url', ''))[0], urldefrag(s.get('url', ''))[0])
            and s.get('type')!='navigation_search'
            and policy(s.get('url', url))]
        matches.sort(key=lambda s:s.get('type')!=source_type)
        old = next(iter(matches),None)
        if old and old.get('duplicate_of'):
            old = next(s for s in data['sources'] if s['id'] == old['duplicate_of'])
        if old is not None:
            if old['id'] not in progress['page_ids']:
                progress['page_ids'].append(old['id'])
            if old['status'] == 'loading':
                old.update(status='unavailable', text='', access_error='read_outcome_unknown')
            if not navigation and old.get('type') == 'navigation':
                # Reuse bytes, but compute admission authority for this specific entity.
                old = self._source(data, **{**{k:v for k,v in old.items() if k!='id'},
                    'type':'web', 'entity_key':entry['entity_key'] if entry else None,
                    'official_identity':self.registry.verify(target['name'], old['url'], scope.get('city',''))})
                progress['page_ids'].append(old['id'])
            return old
        if data['request_counts']['pages'] >= 12 or len(progress['page_ids']) >= 4:
            progress['page_budget_exhausted'] = True
            return None
        old = self._source(data, type=source_type, provider='public_web', requested_url=url,
            url=url, title=target['name'], text='', status='loading', entity_key=entry['entity_key'] if entry else None)
        progress['page_ids'].append(old['id'])
        data['request_counts']['pages'] += 1
        self._save(event, data)
        try:
            old.update(self.reader(url, url_allowed=policy))
            if not policy(old['url']):
                old.update(status='unavailable', text='', access_error='redirect_outside_source_policy')
            if old['status'] == 'read':
                old['navigation_checked_at'] = datetime.now(timezone.utc).isoformat()
            old['official_identity'] = ({'status':'unverified', 'reason':'navigation_only'} if navigation else
                self.registry.verify(target['name'], old['url'], scope.get('city','')))
            duplicate=next((s for s in data['sources'] if s['id']!=old['id'] and s.get('type')==source_type
                and s.get('status')==old.get('status')=='read' and s.get('entity_key')==old.get('entity_key')
                and s.get('official_identity')==old.get('official_identity') and s.get('text')==old.get('text')
                and s.get('published_at')==old.get('published_at')
                and s.get('publication_evidence')==old.get('publication_evidence')
                and s.get('truncated',False)==old.get('truncated',False)),None)
            if duplicate:
                old.update(status='duplicate',duplicate_of=duplicate['id'],text='')
        except ExecutionRevoked:
            raise
        except (ValueError, OSError, RuntimeError) as error:
            old.update(status='unavailable', text='', access_error=getattr(error, 'code', 'page_unavailable'))
        self._save(event, data)
        if old.get('duplicate_of'):
            canonical=next(s for s in data['sources'] if s['id']==old['duplicate_of'])
            if canonical['id'] not in progress['page_ids']:
                progress['page_ids'].append(canonical['id'])
            return canonical
        return old

    def fact_allowed(self, url, scope):
        if scope.get('offline') or scope.get('wendao_only'):
            return False
        parsed = urlsplit(url)
        host = parsed.hostname or ''
        match = lambda d: host == d or host.endswith('.'+d)
        return (public_policy_url(url) and bool(host)
            and not any(match(d) for d in scope.get('excluded_domains', []))
            and (not scope.get('official_only') or any(scoped_url(url,e) for e in self.registry.entries))
            and (not scope.get('strict_sources') or any(match(d) for d in scope.get('requested_domains', []))))

    def query_official_website(self, event, target, data, scope, available, *, attempt=0, page_limit=4):
        """One entity/action; repeated round calls consume only saved pending candidates."""
        city = scope.get('city', '')
        entry = self.registry.match(target['name'], city)
        if not city and entry:
            city = scope['city'] = entry['city']
        key = navigation_key(target, city, scope, entry)
        item = data.setdefault('official_link_progress', {}).setdefault(key,
            {'name':target['name'], 'page_ids':[], 'searches':[], 'candidates':[]})
        links = data.setdefault('official_links', {})
        if target['target_id'] in links and (not current_link(links[target['target_id']]) or not allowed(links[target['target_id']]['url'],scope)):
            links.pop(target['target_id'])
        counts = data.setdefault('request_counts', {})
        for kind in ('pages', 'search'):
            counts.setdefault(kind, 0)
        if current_link(item.get('link')) and allowed(item['link']['url'],scope):
            links[target['target_id']] = {**item['link'], 'target_id':target['target_id']}
            return item
        if item.get('status') in {'unavailable', 'budget_exhausted', 'restricted'}:
            return item
        saved = self.store.research.navigation_cache(event).get(key)
        if saved and current_link(saved['link']) and allowed(saved['link']['url'], scope):
            ids = {s['id']:self._source(data, **{k:v for k,v in s.items() if k!='id'})['id'] for s in saved['sources']}
            item['link'] = {**saved['link'], 'target_id':target['target_id'],
                'evidence_source_ids':[ids[s] for s in saved['link']['evidence_source_ids']],
                'evidence_hashes':{str(ids[int(s)]):v for s,v in saved['link']['evidence_hashes'].items()}}
            item.update(status='verified', reused=True)
            links[target['target_id']] = item['link']
            return item
        link = verified_link(target, city, data['sources'], self.registry, scope)
        restricted = scope.get('offline') or scope.get('wendao_only') or scope.get('amap_only')
        if not link and not restricted and available():
            pending = item['candidates']
            if not item.get('prepared'):
                pending.extend({'url':link['url'], 'basis':'official_outlink'} for _,link,_ in
                    official_outlinks(target, city, data['sources'], self.registry, scope))
                if entry:
                    pending.extend({'url':url, 'basis':'registry'} for url in entry['policy_urls'][:1])
                item['prepared'] = True
            if not pending and len(item['searches']) <= attempt and counts['search'] < 8 and counts['pages'] < 12 and getattr(self.search, 'configured', False):
                candidate = bool(item.get('identity_candidate'))
                if not attempt or candidate:
                    query = ' '.join(filter(None, (city, target['name'], '官方网站' if not attempt else '官方网站 文旅局')))
                    search = {'status':'submitted', 'query':query, 'results':[]}
                    item['searches'].append(search)
                    counts['search'] += 1
                    self._save(event, data)
                    try:
                        rows = self.search.search(query,
                            include_domains=scope.get('requested_domains', []) if scope.get('strict_sources') else [],
                            exclude_domains=scope.get('excluded_domains', []))
                        search.update(status='completed', results=rows)
                        for row in rows:
                            if allowed(row['url'],scope) and search_match(target,city,row):
                                import hashlib, json
                                self._source(data, **row, type='navigation_search', status='snippet', text='',
                                    city=city, retrieved_at=datetime.now(timezone.utc).isoformat(),
                                    content_hash=hashlib.sha256(json.dumps(row,sort_keys=True).encode()).hexdigest())
                    except ExecutionRevoked:
                        raise
                    except (ValueError, OSError, RuntimeError) as error:
                        search.update(status='unavailable', error_code=getattr(error,'code',type(error).__name__))
                    self._save(event, data)
            # Reconstruct candidates after interrupted search checkpoint without re-submission.
            seen = {r['url'] for r in item.get('candidate_trace', [])}
            for search in item['searches']:
                for row in search['results']:
                    if row['url'] not in seen and not any(p['url']==row['url'] for p in pending):
                        pending.append(row)
            pending.sort(key=lambda r: 2 if not allowed(r['url'],scope) else
                0 if search_match(target,city,r) or urlsplit(r['url']).hostname.endswith('.gov.cn') else 1)
            while pending and available() and len(item['page_ids']) < page_limit and counts['pages'] < 12:
                row = pending.pop(0)
                trace = {'url':row['url'], 'target':target['name']}
                if not allowed(row['url'], scope):
                    trace['stage'], trace['reason'] = 'filtered', 'url_or_source_policy'
                else:
                    page_entry = entry if entry and scoped_url(row['url'],entry) else None
                    page = self.read_source(event,data,row['url'],target,page_entry,scope,item,available,source_type='navigation')
                    trace.update(stage='read' if page and page['status']=='read' else 'read_failed',
                        reason=page.get('access_error','identity_unconfirmed') if page else 'page_budget_exhausted')
                    if page and not city and page.get('status')=='read':
                        addresses = re.findall(r'地址[：:]\s*(?:[\u4e00-\u9fff]{2,8}省)?([\u4e00-\u9fff]{2,8})市',page.get('text',''))
                        if (urlsplit(page['url']).hostname or '').endswith('.gov.cn'):
                            addresses += re.findall(r'([\u4e00-\u9fff]{2,8})市(?:人民政府|文化(?:和|广电和)?旅游局)',page.get('text',''))
                        if len(set(addresses))==1 and target['name'] in page.get('text',''):
                            city = scope['city'] = addresses[0]
                    follow = official_outlinks(target,city,data['sources'],self.registry,scope)
                    item['identity_candidate'] = bool(follow) or any(operator_declaration(target,city,s) for s in data['sources'])
                    for _,candidate,_ in reversed(follow):
                        if candidate['url'] not in seen and not any(p['url']==candidate['url'] for p in pending):
                            pending.insert(0, candidate)
                    link = verified_link(target,city,data['sources'],self.registry,scope)
                    if page is not None:
                        trace['identity_checks'] = identity_checks(target,city,page,data['sources'],self.registry,scope)
                    if link:
                        trace.update(stage='verified', reason='identity_confirmed')
                item.setdefault('candidate_trace', []).append(trace)
                seen.add(row['url'])
                if SEARCH_TRACE.get() is not None:
                    SEARCH_TRACE.get().record('candidate-trace', trace)
                self._save(event,data)
                if link:
                    break
        if link:
            item.update(status='verified', link=link)
            links[target['target_id']] = link
        else:
            stopped = ('time_budget_exhausted' if not available() else 'page_budget_exhausted' if counts['pages']>=12
                else 'search_budget_exhausted' if counts['search']>=8 else '')
            item['status'] = 'restricted' if restricted else 'budget_exhausted' if stopped else 'pending'
            trace = item.get('candidate_trace', [])
            item['reason'] = ('user_source_restriction' if restricted else
                trace[-1]['reason'] if trace else 'search_unconfigured' if not getattr(self.search,'configured',False)
                else 'not_attempted' if not item['searches'] else 'search_empty')
            item['stop_reason'] = stopped
            item['attempted'] = bool(item['searches'] or item['page_ids'])
        self._save(event,data)
        return item

    def execute_result(self, name, arguments, context):
        ensure_execution_active()
        if context is None or context.event is None:
            return ToolResult('failed',name,error_code='missing_context',data='官网查询缺少当前请求上下文。')
        event = context.event
        if (event.platform,event.scope_id,event.sender_id,event.event_key) != (context.platform,context.group_id,context.creator_id,context.event_id):
            return ToolResult('failed',name,error_code='wrong_resource',data='官网查询不属于当前请求。')
        if not self.enabled:
            return ToolResult('failed',name,error_code='not_available',data='官网查询已停用。')
        if name=='continue_official_websites':
            previous=next((r for r in self.store.research.latest(event) if r.get('official_link_progress')),None)
            if previous is None:
                return ToolResult('needs_input',name,data='当前会话没有可继续的官网查询，请说明景点名称。',missing_fields=('name',))
            data=self.store.research.get(event)
            if data is None:
                data=deepcopy(previous)
                for field in ('research_id','answer_write','report','brief_report','official_link_progress'):
                    data.pop(field,None)
                data.update(query=event.content,request_counts={},source_reused_from=previous.get('research_id'))
                data['sources']=[s for s in data['sources'] if s.get('status') in {'read','provider_unverified'}]
            start=time.monotonic()
            self.discover_links(event,data,context.available or (lambda:time.monotonic()-start<45))
            links=[l for l in data.get('official_links',{}).values() if selected_link(data,{'name':l['name']},l['target_id'])]
            total=len(data.get('target_scope',{}).get('targets',data.get('provider_targets',[])))
            text='官网查询：已确认 '+str(len(links))+'/'+str(total)+' 个入口。\n\n'+'\n'.join(markdown_link(l) for l in links)
            data.update(report=text,brief_report=text)
            self._save(event,data,'completed' if len(links)==total else 'partial')
            return ToolResult('completed' if len(links)==total else 'partial',name,data=text,
                payload={'covered_count':len(links),'target_count':total})
        state = context.execution_data or {}
        if not state and any(not isinstance(v,str) or not v or v not in event.content for v in arguments.values()):
            return ToolResult('failed',name,error_code='invalid_argument',data='官网查询参数无法对应本轮原话。')
        data = state.get('research')
        standalone = data is None
        if standalone:
            data = self.store.research.get(event) or {'query':event.content,'sources':[]}
        target = state.get('target', {'name':arguments['name'],'target_id':'target-1','visit_mode':'entry'})
        city = arguments.get('city','')
        scope = dict(state.get('scope', {'city':city}))
        if not state:
            catalog = skill_asset('research.search_plan').resource('sources')
            _,scope = bind_source_selection(source_selection(event.content,catalog=catalog),scope)
            scope['navigation_blocked_domains'] = sorted({d for g in catalog['groups'].values() for d in g})
        entry = self.registry.match(target['name'],scope.get('city',''))
        if entry and not scope.get('city'):
            scope['city'] = entry['city']
        data.update(official_link_scope=scope,official_link_display_scope=scope,official_link_version=VERSION)
        start = time.monotonic()
        available = context.available or (lambda: time.monotonic()-start < 45)
        item = self.query_official_website(event,target,data,scope,available,attempt=state.get('attempt',0),page_limit=state.get('page_limit',4))
        if standalone and item['status']=='pending':
            item = self.query_official_website(event,target,data,scope,available,attempt=1)
        if item['status']=='pending' and (standalone or state.get('attempt')==1):
            item['status'] = 'unavailable'
        self._save(event,data)
        link = selected_link(data,target,target['target_id'])
        text = markdown_link(link) if link else '本次未能确认该景点的官方网站。'+('请补充所在城市或分馆。' if not scope.get('city') else '可稍后重试或提供候选网址核对。')
        status = 'completed' if link else 'partial' if item.get('attempted') else 'needs_input' if not scope.get('city') else 'skipped'
        if standalone:
            data.update(destination=scope.get('city',''),provider_targets=[target],report=text,brief_report=text)
            self._save(event,data,'completed' if link else 'partial')
        return ToolResult(status,name,data=text,error_code='' if link else item.get('reason','identity_unconfirmed'),
                          payload={'official_link':link,'progress':item},missing_fields=('city',) if status=='needs_input' else ())

    def discover_links(self, event, data, available):
        targets = data.get('target_scope',{}).get('targets',data.get('provider_targets',[]))
        if not targets or not self.enabled:
            return
        scope = {**data.get('target_scope',{}),'city':data.get('target_scope',{}).get('city') or data.get('destination','')}
        catalog = skill_asset('research.search_plan').resource('sources')
        _,scope = bind_source_selection(source_selection(event.content,catalog=catalog),scope)
        scope['wendao_only'] = scope.get('wendao_only') or data.get('provider_policy',{}).get('only',False)
        scope['navigation_blocked_domains'] = sorted({d for g in catalog['groups'].values() for d in g})
        from tools.research.evidence import evidence_view
        entries = {e['target_id']:e for e in evidence_view(data).answer['entries']}
        ordered = [(priority(t,entries.get(f'target-{i+1}',{}),scope.get('fields',{}).get('topics',[]),event.content),i,t) for i,t in enumerate(targets)]
        ordered = sorted((p,i,t) for p,i,t in ordered if p is not None)
        for attempt in range(2):
            for _,index,target in ordered:
                if not available():
                    break
                target = {**target,'target_id':f'target-{index+1}'}
                context = AgentToolContext(event.platform,event.scope_id,event.sender_id,event.event_key,
                    allowed_actions=frozenset({OFFICIAL_WEBSITE_TOOL}),event=event,available=available,
                    execution_data={'research':data,'target':target,'scope':scope,'attempt':attempt,
                                    'page_limit':4 if attempt or len(ordered)==1 else 2})
                (self.executor or self.execute_result)(OFFICIAL_WEBSITE_TOOL,{'name':target['name'],'city':scope['city']},context)
        links = {key:link for key,link in data.get('official_links',{}).items() if selected_link(data,{'name':link['name']},key)}
        self.store.diagnostics.record('tool',{'event_key':event.event_key,'name':'official_link_discovery',
            'status':'completed' if len(links)==len(ordered) else 'partial','target_count':len(ordered),
            'covered_count':len(links),'remaining_count':len(ordered)-len(links),
            'search_requests':data.get('request_counts',{}).get('search',0),'read_pages':data.get('request_counts',{}).get('pages',0)})
