"""Exact field/paragraph evidence, conservative entity and rule-date decisions."""
from datetime import date, datetime
import re
from zoneinfo import ZoneInfo
from core.attraction_rules import classify_fact

VERSION = 'attraction-facts-v1'
TOPIC_WORDS = {'opening_hours': r'开放|营业|开馆|闭馆|闭园|停止入场|停止入馆',
               'ticket_price': r'门票|票价|入馆免费|免费开放|免费参观|收费',
               'reservation_rules': r'预约|预订|预定'}


def navigation_clause(quote):
    return bool(re.fullmatch(r'(?:立即|点击|提前)(?:预约|预订|购买).{0,30}(?:门票|入口|通道)[。！! ]*', quote)
                or re.fullmatch(r'[^。！!\n]*[？?]', quote)
                or re.search(r'微信搜索关注公众号|预约这些景点.*[：:]$', quote))


def bind_poi(target, city, candidates, registry_entry=None):
    names = {target['name']}
    names.update({city.removesuffix('市') + target['name'], city + target['name']} if city else set())
    if registry_entry:
        names.update([registry_entry['canonical_name'], *registry_entry.get('aliases', [])])
    city = city.removesuffix('市')
    matches = [row for row in candidates if row.get('name') in names
               and city and row.get('city', '').removesuffix('市') == city
               and str(row.get('typecode') or '').startswith(('110', '1401', '1402'))
               and not re.search(r'入口|出口|停车场|售票处|游客中心|地铁站|[东西南北]门$', row['name'])]
    ids = {row['poi_id']: row for row in matches}
    if registry_entry and registry_entry.get('poi_id'):
        ids = {key: row for key, row in ids.items() if key == registry_entry['poi_id']}
    if registry_entry and registry_entry.get('adcode'):
        ids = {key: row for key, row in ids.items() if row.get('adcode') == registry_entry['adcode']}
    return next(iter(ids.values())) if len(ids) == 1 else None


def provider_facts(detail, source_id, target_id, visit_date, visit_mode):
    result = []
    for key in ('opening_today_raw', 'opening_week_raw'):
        value = detail.get(key)
        if not value:
            continue
        query_day = datetime.fromisoformat(detail['retrieved_at']).astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()
        applicability = ('same_query_day' if visit_date == query_day else 'not_target_day') if key == 'opening_today_raw' else 'unknown'
        result.append({'schema_version': VERSION, 'target_id': target_id, 'entity_poi_id': detail['poi_id'],
            'topic': 'opening_hours', 'value_raw': value,
            'scope': {'visit_date': visit_date or None, 'visit_mode': visit_mode, 'visitor_type': 'unknown'},
            'source_id': source_id, 'evidence': [{'kind': 'api_field', 'path': detail['field_paths'][key], 'raw_value': value}],
            'verification': {'identity': 'provider', 'entity_match': 'verified', 'content_support': 'exact_field',
                             'date_applicability': applicability}, 'status': 'provider_reference'})
    return result


def validate_api_fact(fact, detail):
    snapshot = detail['snapshot']
    if fact['entity_poi_id'] != detail['poi_id'] or fact['status'] != 'provider_reference':
        raise ValueError('api_identity_mismatch')
    for evidence in fact['evidence']:
        if evidence['kind'] != 'api_field' or evidence['path'] not in {'business.opentime_today', 'business.opentime_week'}:
            raise ValueError('unsupported_api_rule')
        parent, key = evidence['path'].split('.')
        if evidence['raw_value'] != snapshot[parent][key] or fact['value_raw'] != evidence['raw_value']:
            raise ValueError('api_field_mismatch')


def page_facts(source, target, target_id, topics, visit_date, entry=None):
    """Publish the complete policy clause, not a model's paraphrase."""
    from core.research_evidence import official_page_evidence, complete_quote
    from core.source_applicability import rule_applicability
    text = source.get('text', '')
    identity = source.get('official_identity', {})
    verified = (identity.get('status') == 'verified' and entry is not None
                and identity.get('entity_key') == entry['entity_key'])
    result = []
    # Paragraphs beyond a model's excerpt remain usable by this exact-text branch.
    for match in re.finditer(r'[^\n]+', text):
        quote = match[0].strip()
        quote_start = match.start() + len(match[0]) - len(match[0].lstrip())
        if not 1 <= len(quote) <= 500 or public_rule_issue(quote):
            continue
        if re.search(r'公告$|参观须知$|预约须知$|参观指南$', quote):
            continue  # navigation/headlines are not the actual policy clauses
        if navigation_clause(quote):
            continue  # booking links are not a policy requiring advance booking
        markers = (entry or {}).get('page_entity_markers', [])
        if verified and any(marker not in text for marker in markers):
            continue
        named_parts = re.findall(r'(?:对|位于|进入|前往)([\u4e00-\u9fff]{2,18}?(?:宫|馆|园|厅|楼|区))', quote)
        if any(part not in [target['name'], (entry or {}).get('canonical_name'), *(entry or {}).get('aliases', [])]
               for part in named_parts):
            continue
        entity_quote = next((name for name in [target['name'], *(entry or {}).get('aliases', []),
                             (entry or {}).get('canonical_name', ''), *markers] if name and name in text), '')
        if verified:
            try:
                evidence = official_page_evidence(source, quote, entity_quote, quote_start=quote_start)
            except ValueError:
                continue
        else:
            if target['name'] not in quote or not complete_quote(quote, text):
                continue
            evidence = [{'kind': 'web_quote', 'quote': quote, 'start': quote_start, 'end': quote_start + len(quote)}]
        applicability = rule_applicability(source, quote, date.fromisoformat(visit_date) if visit_date else None)
        for topic in topics:
            if not re.search(TOPIC_WORDS[topic], quote):
                continue
            meaningful = {'opening_hours': r'\d{1,2}\s*[:：]\s*\d{2}|每周|全年|常年|临时闭馆|暂停开放',
                          'ticket_price': r'\d+(?:\.\d+)?元|免费|收费',
                          'reservation_rules': r'(?:需|须)(?:要|提前)?.{0,8}预约|预约参观制度|实名预约|免预约|'
                                               r'(?:可|只能|仅可).{0,30}预约|提前预订|预约入场|'
                                               r'预约.{0,20}(?:提前|成功|开放|取消|次数|限制|时段|实名)'}
            if len(quote) < 5 or not re.search(meaningful[topic], quote):
                continue
            audiences = {kind for kind, pattern in [('team', r'团队|团体|讲解员'), ('child', r'儿童|未成年'),
                         ('adult', r'成人|成年人'), ('personal', r'个人|散客'), ('priority', r'老年|残疾|军人')]
                         if re.search(pattern, quote)}
            visitor = next(iter(audiences)) if len(audiences) == 1 else 'mixed' if audiences else 'general'
            # Raw policy scope stays explicit; never apply a performance or inner
            # hall's ticket to general museum admission.
            product = 'special' if re.search(r'特展|演出|演奏|讲解|导览器|寄存|停车|互动体验|专项|专馆', quote) else 'general'
            state = applicability['date_status']
            status = ('unknown' if state in {'expired', 'not_yet_effective', 'outside_declared_window'} else
                      'official_verified' if verified and state == 'applicable' and product == 'general' else
                      'official_date_pending' if verified else 'unverified_source')
            visit_mode = target['visit_mode']
            if visit_mode == 'exterior':
                status, visit_mode = 'unknown', 'entry_not_applicable_to_exterior'
            result.append(classify_fact({'schema_version': VERSION, 'target_id': target_id, 'entity_poi_id': None,
                'clause_start': quote_start,
                'topic': topic, 'value_raw': quote, 'scope': {'visit_date': visit_date or None,
                    'visit_mode': visit_mode, 'visitor_type': visitor, 'product': product},
                'source_id': source['id'], 'evidence': evidence, 'applicability': applicability,
                'verification': {'identity': 'official' if verified else 'unverified', 'entity_match': 'verified' if entity_quote else 'pending',
                    'content_support': 'exact_quote', 'date_applicability': state}, 'status': status}, source, target['name']))
    unique = {}
    for fact in result:
        unique.setdefault((fact['topic'], fact['value_raw'], tuple(sorted(fact['scope'].items()))), fact)
    result = list(unique.values())
    source['omitted_fact_count'] = max(0, len(result) - 24)
    return result[:24]


def public_rule_issue(quote):
    return bool(re.search(r'未知|未公布|待定|尚未明确|仅供参考', quote))


def fact_signature(fact):
    text = fact['value_raw']
    if fact['topic'] == 'opening_hours':
        if re.search(r'临时闭馆|暂停开放', text):
            return ('closure', 'closed')
        if re.search(r'停止入场|停止入馆|售票', text):
            return None  # distinct time roles, not opening-hour contradictions
        ranges = re.findall(r'\d{1,2}[:：]\d{2}\s*[-—~～至]\s*\d{1,2}[:：]\d{2}', text)
        return ('hours', tuple(ranges)) if ranges else None
    if fact['topic'] == 'ticket_price':
        if re.search(r'门票免费|免费开放|免费参观', text):
            return ('price', 'free')
        amounts = re.findall(r'\d+(?:\.\d+)?元', text)
        return ('price', tuple(amounts)) if amounts else None
    if '无需提前预约' in text:
        return ('reservation', 'no_advance')
    if re.search(r'无需预约|免预约', text):
        return ('reservation', 'none')
    if re.search(r'须预约|需.{0,5}预约|预约参观制度|实名预约', text):
        return ('reservation', 'required')
    return None


def reconcile_facts(facts):
    groups = {}
    for fact in facts:
        if fact['verification']['identity'] == 'official' and fact.get('rule_scope', {}).get('bucket') != 'reference':
            key = (fact['target_id'], fact['topic'], tuple(sorted((k, v) for k, v in fact['scope'].items()
                   if k not in {'time_role', 'rule_window'})))
            groups.setdefault(key, []).append(fact)
    for group in groups.values():
        closures = [fact for fact in group if fact_signature(fact) == ('closure', 'closed')
                    and fact['status'] == 'official_verified' and fact['applicability'].get('basis') == 'explicit_date_range']
        if closures:
            for fact in group:
                if fact not in closures and fact['topic'] == 'opening_hours':
                    fact.update(status='unknown', superseded_by=closures[0]['source_id'])
        signatures = {}
        for fact in group:
            value = fact_signature(fact)
            if value and fact['status'] in {'official_verified', 'conflict'}:
                signatures.setdefault((value[0], fact['scope'].get('time_role'), fact['scope'].get('rule_window')), []).append((value[1], fact))
        for rows in signatures.values():
            if len({value for value, _ in rows}) > 1:
                for _, fact in rows:
                    fact['status'] = 'conflict'
    return facts
