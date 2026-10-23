"""The plan-fact pipeline: platform fields -> reviewed pages -> bounded discovery.

No reminder/trip write tools, independent source availability, per-event checkpoints.
"""
import inspect
import re
from urllib.parse import urlsplit, urldefrag

from core.attraction_facts import VERSION, bind_poi, page_facts, provider_facts, reconcile_facts, validate_api_fact
from core.execution_scope import ensure_execution_active, ExecutionRevoked
from core.research_contract import TOPICS
from core.attraction_projection import attraction_report, poi_detail_view
from services.official_sources import public_policy_url, scoped_url
from core.wendao_answer import provider_policy
from services.wendao_research import attach_answer, answer_report
from infrastructure.wendao_client import WendaoError
from core.rule_semantics import resolve_rules

class AttractionResearch:
    def __init__(self, service, amap, registry):
        self.service, self.amap, self.registry = service, amap, registry

    def _allowed(self, url, scope):
        if scope.get('wendao_only') or scope.get('offline'):
            return False
        allowed = public_policy_url(url) and self.service._domain_allowed(url, scope['requested_domains'] if scope['strict_sources'] else [], scope['excluded_domains'])
        if scope.get('official_only'):
            allowed = allowed and any(scoped_url(url, entry) for entry in self.registry.entries)
        return allowed

    def _save(self, event, data, stage='reading'):
        ensure_execution_active()
        self.service.store.research.save(event, stage, data)

    @staticmethod
    def _source(data, **values):
        source = {'id': max((row['id'] for row in data['sources']), default=0) + 1, **values}
        data['sources'].append(source)
        return source

    def _page(self, event, data, url, target, entry, scope, progress, available):
        if not self._allowed(url, scope) or not available():
            return None
        url = urldefrag(url)[0]
        entity_key = entry['entity_key'] if entry else None
        old = next((row for row in data['sources'] if row.get('type') == 'web' and row.get('entity_key') == entity_key
                    and url in [urldefrag(row.get('requested_url', ''))[0], urldefrag(row.get('url', ''))[0]]), None)
        if old and old.get('duplicate_of'):
            old = next(row for row in data['sources'] if row['id'] == old['duplicate_of'])
        if old and old['id'] not in progress['page_ids']:
            progress['page_ids'].append(old['id'])
        if old and old['status'] != 'loading':
            return old
        if old is None:
            if data['request_counts']['pages'] >= 12 or len(progress['page_ids']) >= 4:
                progress['page_budget_exhausted'] = True
                return None
            old = self._source(data, type='web', provider='public_web', requested_url=url, url=url,
                               title=target['name'], text='', status='loading', entity_key=entity_key)
            progress['page_ids'].append(old['id'])
            data['request_counts']['pages'] += 1
            self._save(event, data)
        policy = lambda candidate: self._allowed(candidate, scope) and (entry is None or scoped_url(candidate, entry))
        try:
            reader = self.service.reader
            parameters = inspect.signature(reader).parameters.values()
            kwargs = {'url_allowed': policy} if any(p.name == 'url_allowed' or p.kind == p.VAR_KEYWORD for p in parameters) else {}
            old.update(reader(url, **kwargs))
            if not policy(old['url']):
                old.update(status='unavailable', text='', access_error='redirect_outside_source_policy')
            old['official_identity'] = self.registry.verify(target['name'], old['url'], scope['city'])
            duplicate = next((row for row in data['sources'] if row['id'] != old['id']
                and row.get('status') == old['status'] == 'read' and row.get('entity_key') == entity_key
                and row.get('official_identity') == old['official_identity']
                and row.get('text') == old.get('text') and row.get('published_at') == old.get('published_at')
                and row.get('publication_evidence') == old.get('publication_evidence')
                and row.get('truncated', False) == old.get('truncated', False)), None)
            if duplicate:
                old.update(status='duplicate', duplicate_of=duplicate['id'], text='')
        except ExecutionRevoked:
            raise
        except (ValueError, OSError) as error:
            old.update(status='unavailable', text='', access_error=getattr(error, 'code', 'page_unavailable'))
        self._save(event, data)
        if old.get('duplicate_of'):
            canonical = next(row for row in data['sources'] if row['id'] == old['duplicate_of'])
            if canonical['id'] not in progress['page_ids']:
                progress['page_ids'].append(canonical['id'])
            return canonical
        return old

    def run(self, event, request, data, available):
        service = self.service
        scope = {**data['target_scope'], 'city': request['plan']['destination']}
        data['target_scope']['city'] = scope['city']
        source_request = '；'.join(filter(None,(request.get('source_request',''),event.content)))
        wendao = getattr(service, 'wendao', None)
        automatic = wendao is not None and wendao.configured and not scope.get('strict_sources')
        policy = provider_policy(source_request, automatic=automatic)
        scope.update(wendao_only=policy['only'], offline=policy['offline'], official_only=policy['official_only'])
        data['provider_policy'] = policy
        if re.search(r'只看|仅看|只用|仅用|只参考', source_request):
            urls = re.findall(r'https://[^\s<>，。]+', source_request)
            if urls:
                scope.update(strict_sources=True, requested_domains=[urlsplit(url).hostname for url in urls])
            elif re.search(r'只(?:看|用|参考)高德|仅(?:看|用)高德', source_request):
                scope.update(strict_sources=True, requested_domains=['amap.com'], amap_only=True)
            elif re.search(r'只看.{0,20}官网|仅看.{0,20}官网|只参考.{0,20}官方', source_request):
                scope['official_only'] = True
        data.setdefault('fact_schema', VERSION)
        data.setdefault('facts', [])
        data.setdefault('place_details', [])
        data.setdefault('target_progress', {})
        data.setdefault('request_counts', {'amap': 0, 'pages': 0, 'search': 0})
        topics = scope['fields']['topics']
        verification_requested = bool(scope.get('official_only') or re.search(r'核实|核验|核对|确认.{0,8}(?:官网|规定|能否入园|抢票)|官方.*(?:确认|规定)', event.content))
        data['research_mode'] = 'verification' if verification_requested else 'information'
        if policy['requested'] and available():
            try:
                if service.wendao is None:
                    raise WendaoError('not_configured')
                dates = {t.get('day_index', 1): service._target_date(t, data['dates']) for t in scope['targets'][:8]}
                timeout = min(75, getattr(available, 'remaining_seconds', lambda: 115)() - getattr(available,'writing_reserve',40))
                attach_answer(service, event, data, targets=scope['targets'][:8], dates=dates, timeout=timeout)
            except WendaoError as error:
                data['wendao_error'] = str(error)
        for index, target in enumerate(scope['targets'][:4]):
            ensure_execution_active()
            if not available():
                break
            target_id = f'target-{index + 1}'
            progress = data['target_progress'].setdefault(target_id, {'page_ids': [], 'searches': []})
            if progress.get('done'):
                continue
            entry = self.registry.match(target['name'], scope['city'])
            visit_date = service._target_date(target, data['dates'])
            from core.attraction_answers import answer_view, has_provider
            initial_view = answer_view(data) if has_provider(data) else None
            selected = next((e for e in initial_view['entries'] if e['target_id']==target_id), None) if initial_view else None
            needs_confirmation = verification_requested or not initial_view
            # Ordinary research uses existing references. Only a real gap or discrepancy
            # starts external verification; unavailable optional sources do not fan out.
            if selected:
                needs_confirmation = needs_confirmation or any(s['answer_state'] in {'conflict','not_provided','raw_pending','limited'} for s in selected['slots'])
            needs_map = verification_requested or not initial_view or bool(selected and any(
                s['topic']=='opening_hours' and s['answer_state']!='reference' for s in selected['slots']))
            if 'amap_status' not in progress:
                candidates, reason = [], 'amap_not_configured'
                permitted = self._allowed('https://restapi.amap.com/v5/place/text', scope)
                permitted = permitted and not re.search(r'不(?:要|用|必).{0,6}高德', event.content)
                lookup_id = getattr(self.amap, 'get_poi_detail', None)
                lookup_text = getattr(self.amap, 'search_poi_details', None)
                supported = callable(lookup_text) or (entry and entry.get('poi_id') and callable(lookup_id))
                if self.amap is not None and permitted and supported and needs_map:
                    data['request_counts']['amap'] += 1
                    try:
                        if entry and entry.get('poi_id') and callable(lookup_id):
                            found = lookup_id(entry['poi_id'])
                            candidates = [found] if found else []
                        else:
                            candidates = list(lookup_text(scope['city'], target['name']))
                        reason = 'entity_ambiguous_or_unmatched'
                    except ExecutionRevoked:
                        raise
                    except (ValueError, OSError, RuntimeError) as error:
                        reason = getattr(error, 'code', '') or 'amap_unavailable'
                elif not permitted:
                    reason = 'user_source_restriction'
                detail = bind_poi(target, scope['city'], candidates, entry)
                progress['amap_status'] = 'matched' if detail else reason
                record = {'target_id': target_id, 'status': progress['amap_status'], 'detail': detail,
                          'entity_poi_id': detail['poi_id'] if detail else None, 'navigation_poi_id': None,
                          'candidates': [{key: row.get(key) for key in ('poi_id', 'name', 'address', 'city', 'parent_id')} for row in candidates]}
                if detail:
                    record['display'] = poi_detail_view(detail)
                data['place_details'].append(record)
                if detail:
                    source = self._source(data, type='api', provider='amap', status='provider_reference',
                        title=detail['name'], interface_path=detail['interface_path'], poi_id=detail['poi_id'],
                        retrieved_at=detail['retrieved_at'], content_hash=detail['content_hash'], snapshot=detail['snapshot'])
                    for fact in provider_facts(detail, source['id'], target_id, visit_date, target['visit_mode']):
                        validate_api_fact(fact, detail)
                        data['facts'].append(fact)
                self._save(event, data)
            urls = ([*entry['policy_urls'], *entry.get('notice_index_urls', [])][:2] if entry and needs_confirmation else [])
            # User-supplied URLs may be read, but never become registered identities.
            urls += re.findall(r'https://[^\s<>，。]+', event.content)[:2]
            if not progress.get('official_done'):
                for url in dict.fromkeys(urls):
                    if not available():
                        break
                    page_entry = entry if entry and scoped_url(url, entry) else None
                    source = self._page(event, data, url, target, page_entry, scope, progress, available)
                    if source and source.get('status') == 'read':
                        for link in source.get('links', []):
                            if (entry and link['url'] != source['url'] and scoped_url(link['url'], entry)
                                    and re.search(r'临时闭馆|开放调整|预约须知|门票调整', link['text'])):
                                self._page(event, data, link['url'], target, entry, scope, progress, available)
                                break
                progress['official_done'] = all(any(urldefrag(url)[0] in [urldefrag(row.get('requested_url', ''))[0],
                    urldefrag(row.get('url', ''))[0]] and row['status'] != 'loading'
                    for row in data['sources']) or not self._allowed(url, scope) for url in dict.fromkeys(urls))
                self._save(event, data)
            pages = [source for source in data['sources'] if source['id'] in progress['page_ids']]
            facts = [fact for source in pages for fact in page_facts(source, target, target_id, topics, visit_date,
                     entry if source.get('official_identity', {}).get('status') == 'verified' else None)]
            missing = [topic for topic in topics if not any(f['topic'] == topic and f['status'] == 'official_verified' for f in facts)]
            if initial_view and not verification_requested and selected:
                missing = [s['topic'] for s in selected['slots'] if s['answer_state'] in {'conflict','not_provided','raw_pending','limited'}]
            if (missing and not scope.get('amap_only') and not scope.get('wendao_only') and not scope.get('offline') and service.search is not None
                    and getattr(service.search, 'configured', True) and available()):
                # Two bounded discovery attempts; model-generated titles are not identity evidence.
                while len(progress['searches']) < (2 if verification_requested or not initial_view else 1) and available():
                    query = ' '.join((scope['city'], target['name'], visit_date,
                                      ' '.join(TOPICS[t] for t in missing), '游客须知' if not progress['searches'] else '开放调整 公告'))
                    kwargs = {}
                    domains = scope['requested_domains'] if scope['strict_sources'] else entry['allowed_hosts'] if entry else []
                    if domains:
                        kwargs['include_domains'] = domains
                    if scope['excluded_domains']:
                        kwargs['exclude_domains'] = scope['excluded_domains']
                    try:
                        rows = service.search.search(service._query(query), **kwargs)
                    except (ValueError, OSError):
                        rows = []
                    progress['searches'].append({'results': rows[:2]})
                    data['request_counts']['search'] += 1
                    self._save(event, data, 'searching')
                for search in progress['searches']:
                    for row in search['results']:
                        page_entry = entry if entry and scoped_url(row['url'], entry) else None
                        source = self._page(event, data, row['url'], target, page_entry, scope, progress, available)
                        if source and source['id'] not in progress['page_ids']:
                            progress['page_ids'].append(source['id'])
            pages = [source for source in data['sources'] if source['id'] in progress['page_ids']]
            data['facts'] = [fact for fact in data['facts'] if fact['target_id'] != target_id or fact['status'] in {'provider_reference', 'provider_answer_reference'}]
            data['facts'].extend(fact for source in pages for fact in page_facts(source, target, target_id, topics, visit_date,
                entry if source.get('official_identity', {}).get('status') == 'verified' else None))
            progress['done'] = available() and progress.get('official_done', False)
            self._save(event, data)
        reconcile_facts(data['facts'])
        resolve_rules(data['facts'])
        for record in data['place_details']:
            for material in data.get('activity_materials', []):
                if record['target_id'] == material['target_id']:
                    material['entity_poi_id'] = record['entity_poi_id']
            for mapping in data.get('provider_mappings', []):
                if record['target_id'] == mapping['target_id'] and record['entity_poi_id']:
                    mapping.update(amap_poi_id=record['entity_poi_id'], status='bound', basis='unique_city_name_amap_match')
        covered = sum(any(f['target_id'] == f'target-{i + 1}' and f['topic'] == topic and f['status'] == 'official_verified'
                      and f.get('rule_scope', {}).get('bucket', 'current') == 'current'
                      and f['scope'].get('visitor_type') in {'general', 'personal'} and f['scope'].get('product', 'general') == 'general'
                      for f in data['facts']) for i in range(len(scope['targets'])) for topic in topics)
        total = len(scope['targets']) * len(topics)
        data['coverage'] = {'target_count': len(scope['targets']), 'topic_count': len(topics), 'covered_count': covered,
                            'remaining_count': total - covered, 'platform_fact_count': sum(f['status'] == 'provider_reference' for f in data['facts']),
                            'conflict_count': sum(f['status'] == 'conflict' for f in data['facts'])}
        data['coverage'].update(provider_candidate_count=sum(f['status'] == 'provider_answer_reference' for f in data['facts']),
            candidate_conflict_count=sum(f.get('rule_resolution') == 'conflict' and f['status'] == 'provider_answer_reference' for f in data['facts']),
            soft_advice_count=sum(f.get('statement_kind') == 'advice' for f in data['facts']))
        data['report'] = self.report(data)
        data['brief_report'] = attraction_report(data)
        if has_provider(data):
            view = answer_view(data)
            data['answer_schema'] = view['schema_version']
            data['answer_metrics'] = view['metrics']
            data['answer_stage'] = view['answer_stage']
        if data.get('wendao_error'):
            data['report'] += '\n' + data['wendao_error']
            data['brief_report'] += '\n' + data['wendao_error']
        self._save(event, data, 'completed' if covered == total else 'partial')
        if hasattr(service.store, 'diagnostics'):
            service.store.diagnostics.record('routing', {'event_key': event.event_key,
                'status': 'research_completed' if covered == total else 'research_partial', **data['coverage'],
                'amap_requests': data['request_counts']['amap'], 'read_pages': data['request_counts']['pages'],
                'search_requests': data['request_counts']['search']})
        if has_provider(data):
            from services.travel_answer_writer import write_answer
            data['stage'] = 'completed' if covered == total else 'partial'
            return write_answer(service, event, data)
        return data['brief_report']

    @staticmethod
    def report(data):
        return attraction_report(data, detailed=True)
