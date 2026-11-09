"""Pure navigation identity and projection; never promotes admission facts or performs I/O."""
from datetime import datetime, timedelta, timezone
import hashlib
import ipaddress
import json
import re
import unicodedata
from urllib.parse import urlsplit, parse_qsl

from services.official_sources import scoped_url

VERSION = 'official-navigation-v3'
CACHE_AGE = timedelta(days=7)


def normalized(value):
    return re.sub(r'[\W_]+', '', unicodedata.normalize('NFKC', value)).casefold()


def entity_mention(name, text):
    # The object must end before the site/owner label; a main venue name must
    # not match a longer branch name simply because it is a prefix.
    return bool(re.search(re.escape(normalized(name)) + r'(?:官方网站|官网|版权所有|allrightsreserved|$)', normalized(text)))


def navigation_url(url):
    if not isinstance(url, str) or len(url) > 2000 or re.search(r'[\s\\<>\[\]"]', url):
        return False
    try:
        parsed = urlsplit(url)
        if (parsed.scheme not in {'http','https'} or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.port not in (None, 80 if parsed.scheme=='http' else 443)
                or any(ord(c)<32 for c in url)
                or any(re.search(r'key|token|auth|secret|password|signature|^sig$|^code$', k, re.I) for k,_ in parse_qsl(parsed.query))):
            return False
        host = parsed.hostname.rstrip('.').lower()
    except ValueError:
        return False
    if '.' not in host or host.endswith(('.local', '.localhost', '.internal')):
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return True


def allowed(url, scope):
    if not navigation_url(url) or scope.get('wendao_only') or scope.get('amap_only'):
        return False
    host = urlsplit(url).hostname.lower().rstrip('.')
    matches = lambda domain: host == domain or host.endswith('.' + domain)
    return (not any(matches(d) for d in [*scope.get('excluded_domains', []), *scope.get('navigation_blocked_domains', [])])
            and (not scope.get('strict_sources') or any(matches(d) for d in scope.get('requested_domains', []))))


def navigation_key(target, city, scope, entry=None):
    entity = ([entry['entity_key'], entry['registry_version'], entry['allowed_hosts'], entry['path_prefixes'],
               entry.get('page_entity_markers', [])] if entry else (target.get('poi_id'), normalized(target['name'])))
    policy = {key: scope.get(key, False if key.endswith('only') or key == 'strict_sources' else [])
              for key in ('requested_domains', 'excluded_domains', 'navigation_blocked_domains', 'strict_sources', 'wendao_only', 'amap_only', 'official_only')}
    for key in ('requested_domains', 'excluded_domains', 'navigation_blocked_domains'):
        policy[key] = sorted(policy[key])
    return hashlib.sha256(json.dumps([VERSION, city.removesuffix('市'), entity, policy],
                                     ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def priority(target, entry, topics, request):
    """An admission decision, never an inference that average spend means paid entry."""
    explicit = bool(re.search(r'官网|官方网站|预约', request))
    if len(topics) == 1 and topics[0] != 'reservation_rules' and not explicit:
        return None
    if target.get('visit_mode') == 'exterior' and not explicit:
        return None
    slots = entry.get('slots', [])
    reservation = next((s['text'] for s in slots if s['topic'] == 'reservation_rules'), '')
    # Explicitly unreserved public outdoor areas are lower priority, not assumed paid.
    unreserved = re.search(r'无需预约|无须预约|免预约|不需要预约', reservation)
    return 1 if unreserved and re.search(r'公园|街道|街区|步行街|广场', target['name']) and not re.search(r'官网|官方网站', request) else 0


def official_outlinks(target, city, sources, registry, scope):
    """One definition of an explicit institutional outlink, used for discovery and proof."""
    result = []
    for source in sources:
        if source.get('status') != 'read' or not allowed(source.get('url'), scope):
            continue
        publisher = registry.navigation_authority(source, city)
        if not publisher:
            continue
        for link in source.get('links', []):
            label = link.get('context', '') + link.get('text', '')
            if (allowed(link.get('url'), scope) and entity_mention(target['name'], label)
                    and re.search(r'官方网站|官网|official\s+(?:web)?site', label, re.I)):
                result.append((source, link, publisher))
    return result


def operator_declaration(target, city, page):
    text = page.get('text', '')
    if (page.get('status') != 'read' or not city or normalized(target['name']) not in normalized(text)
            or normalized(city.removesuffix('市')) not in normalized(text)):
        return ''
    return next((line for line in text.splitlines()
                 if re.search(r'主办(?:单位)?|运营(?:单位|方)?|版权所有|copyright', line, re.I)
                 and entity_mention(target['name'], line)), '')


def identity_checks(target, city, page, sources, registry, scope):
    """Diagnostic projection of the same evidence predicates used by verification."""
    return {'readable':page.get('status')=='read', 'source_allowed':allowed(page.get('url'),scope),
        'entity_mentioned':normalized(target['name']) in normalized(page.get('text','')),
        'city_mentioned':bool(city) and normalized(city.removesuffix('市')) in normalized(page.get('text','')),
        'reviewed_registry':bool(registry.navigation(target['name'],city,page)),
        'operator_declaration':bool(operator_declaration(target,city,page)),
        'official_outlink':any(link['url'] in (page.get('url'),page.get('requested_url'))
            for _,link,_ in official_outlinks(target,city,sources,registry,scope))}


def verified_link(target, city, sources, registry, scope, *, blocked_domains=()):
    """Navigation identity and page accessibility are independent."""
    entry = registry.match(target['name'], city)
    pages = [s for s in sources if s.get('status') in {'read','unavailable','snippet'} and allowed(s.get('url'), scope)]
    pages.sort(key=lambda s: not bool(re.search(r'须知|预约|参观|visit|visitor', s.get('title', '') + s['url'], re.I)))
    outlinks = official_outlinks(target, city, pages, registry, scope)
    verified = []
    for page in pages:
        host = urlsplit(page['url']).hostname
        original = urlsplit(page.get('requested_url',page['url'])).hostname or ''
        if original.removeprefix('www.') != host.removeprefix('www.'):
            continue
        if page.get('status')=='read':
            text = page.get('text','')
            addresses = re.findall(r'(?m)^(?:地址[：:]\s*)?([\u4e00-\u9fff]{2,8})市',text)
            heading = normalized((text.strip().splitlines() or [''])[0])
            if (city and addresses and not any(normalized(city.removesuffix('市')) in normalized(a) for a in addresses)
                    or heading.startswith(normalized(target['name'])) and not entity_mention(target['name'],heading)):
                continue
        if any(host == domain or host.endswith('.' + domain) for domain in blocked_domains):
            continue
        basis = registry.navigation(target['name'], city, page)
        if basis is None and entry and page.get('status')!='read' and scoped_url(page['url'],entry):
            basis = {'kind':'reviewed_registry','entity_key':entry['entity_key']}
        evidence = [page]
        if basis is None:
            operator = operator_declaration(target, city, page)
            original_host = urlsplit(page.get('requested_url',page['url'])).hostname or ''
            if operator and original_host.removeprefix('www.') == host.removeprefix('www.'):
                basis = {'kind':'operator_declaration','operator_declaration':operator[:300]}
            for authority, link, publisher in outlinks:
                if authority['id'] != page['id'] and link['url'] == page.get('requested_url', page['url']):
                    basis = {'kind':'official_outlink', 'publisher':publisher, 'link_text':link['text'],
                             'link_context':link.get('context', ''), 'operator_declaration':operator[:300]}
                    evidence.append(authority)
                    break
        if basis is None and page.get('type')=='navigation_search' and search_match(target,city,page):
            basis = {'kind':'search_match'}
        if basis:
            try:
                checked = min(datetime.fromisoformat(s.get('navigation_checked_at') or s['retrieved_at']) for s in evidence)
                if not timedelta(0) <= datetime.now(timezone.utc)-checked <= CACHE_AGE:
                    continue
            except (KeyError, TypeError, ValueError):
                continue
            verified.append({'schema_version':VERSION, 'target_id':target['target_id'], 'entity_key':entry['entity_key'] if entry else normalized(target['name']),
                    'name':target['name'], 'city':city, 'url':page['url'], 'final_url':page['url'],
                    'label':target['name']+('官网候选（搜索匹配）' if basis['kind']=='search_match' else '官方网站'),
                    'status':'candidate_navigation' if basis['kind']=='search_match' else 'verified_navigation', 'identity_basis':basis,
                    'scope_key':navigation_key(target, city, scope, entry), 'checked_at':checked.isoformat(),
                    'evidence_source_ids':[s['id'] for s in evidence],
                    'evidence_hashes':{str(s['id']):s.get('content_hash', '') for s in evidence}})
    reviewed = [link for link in verified if link['identity_basis']['kind']=='reviewed_registry']
    if reviewed:
        return reviewed[0]
    confirmed = [link for link in verified if link['status']=='verified_navigation']
    choices = confirmed or verified
    return choices[0] if choices and len({urlsplit(link['url']).hostname for link in choices})==1 else None


def search_match(target, city, row):
    title, snippet = row.get('title',''), row.get('snippet','')
    addresses=re.findall(r'(?:地址[：:]|位于)\s*([\u4e00-\u9fff]{2,8})市',snippet)
    if city and addresses and not any(normalized(city.removesuffix('市')) in normalized(a) for a in addresses):
        return False
    homepage = urlsplit(row['url']).path.rstrip('/') in {'','/index.php','/index.html','/index.htm'}
    return (homepage and (entity_mention(target['name'],title) or normalized(target['name']) in normalized(snippet))
            or entity_mention(target['name'],title) and bool(re.search(r'官网|官方网站|official\s+(?:web)?site',title,re.I))) and (
        not row.get('city') or not city or normalized(row['city'])==normalized(city))


def current_link(record, *, now=None):
    if not isinstance(record, dict):
        return False
    try:
        age = (now or datetime.now(timezone.utc)) - datetime.fromisoformat(record['checked_at'])
        return (record['schema_version'] in {VERSION, 'official-navigation-v2', 'official-navigation-v1'}
                and record['status'] in {'verified_navigation','candidate_navigation'}
                and record['url'] == record['final_url'] and navigation_url(record['url'])
                and record['identity_basis']['kind'] in {'reviewed_registry', 'official_outlink','operator_declaration','search_match'}
                and all(isinstance(record[key], str) and record[key] for key in ('name', 'label', 'scope_key'))
                and isinstance(record['evidence_hashes'], dict)
                and bool(record['evidence_source_ids']) and timedelta(0) <= age <= CACHE_AGE)
    except (KeyError, TypeError, ValueError):
        return False


def selected_link(data, target, target_id):
    record = data.get('official_links', {}).get(target_id)
    scope = {**data.get('official_link_scope', {}), **data.get('target_scope', {}), **data.get('official_link_display_scope', {})}
    scope['wendao_only'] = data.get('provider_policy', {}).get('only', False) or scope.get('wendao_only', False)
    if not current_link(record) or not allowed(record['url'], scope) or normalized(record['name']) != normalized(target['name']):
        return None
    city = data.get('target_scope', {}).get('city') or data.get('destination') or data.get('official_link_scope', {}).get('city')
    if city and normalized(city.removesuffix('市')) != normalized(record['city'].removesuffix('市')):
        return None
    sources = {s['id']:s for s in data.get('sources', [])}
    if not all(sid in sources and sources[sid].get('status') in {'read','unavailable','snippet'}
               and allowed(sources[sid].get('url'), scope)
               and sources[sid].get('content_hash', '') == record['evidence_hashes'].get(str(sid))
               for sid in record['evidence_source_ids']):
        return None
    return record


def markdown_link(record):
    if not current_link(record):
        return ''
    label = re.sub(r'([\\\[\]*_`])', r'\\\1', record['label'])
    return '详细信息见景点官网：[' + label + '](<' + record['url'] + '>)'
