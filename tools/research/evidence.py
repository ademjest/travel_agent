from infrastructure.skill_manager import skill_text, skill_request
from tools.research.official_links import selected_link, markdown_link

from core.model_policy import current_policy, TIERS
"""Research evidence decisions and shared, read-only consumer projections."""
import hashlib
import json
import re
from dataclasses import dataclass
from tools.research.sources import extract_candidates, INSTRUCTION, clean, field_blocks, practical_detail, EXTRACTOR_REVISION
from core.date_rules import temporal_rule, DateWindow, date_window, policy_date
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from core.research_contract import MAX_ITEMS, MAX_TEXT_CHARS, TOPICS
from copy import deepcopy
from collections import defaultdict


def rule_applicability(source, quote, visit_date=None):
    """Policy windows, independent of source identity and crawl time."""
    result = {'visit_date': visit_date.isoformat() if visit_date else None, 'date_status': 'unknown',
              'effective_from': None, 'effective_until': None, 'basis': 'not_provided'}
    if not visit_date:
        return result
    window = date_window(quote, visit_date)
    state = window.state(visit_date)
    if window.start and not window.recurrence:
        return {**result, 'effective_from': window.start.isoformat(),
                'effective_until': window.end.isoformat() if window.end else None,
                'basis': window.basis,
                'date_status': {'matching': 'applicable', 'outside': 'outside_declared_window'}.get(state, state)}
    labelled = source_applicability(source, visit_date)
    if labelled['date_status'] != 'unknown':
        return {**result, 'effective_from': labelled['effective_from'], 'effective_until': labelled['effective_until'],
                'basis': 'explicit_labelled_source_text',
                'date_status': 'applicable' if labelled['date_status'] == 'within_declared_window' else labelled['date_status']}
    if '即日起' in quote and source.get('publication_evidence'):
        try:
            raw = source['publication_evidence']['raw_value']
            if re.fullmatch(r'\d{4}-\d{2}-\d{2}', raw):
                published = date.fromisoformat(raw)
            else:
                instant = datetime.fromisoformat(raw.replace('Z', '+00:00'))
                if instant.tzinfo is None:
                    return result
                published = instant.astimezone(ZoneInfo('Asia/Shanghai')).date()
        except (ValueError, KeyError, TypeError):
            return result
        return {**result, 'effective_from': published.isoformat(), 'basis': 'relative_to_explicit_publication',
                'publication_evidence': source['publication_evidence'],
                'date_status': 'applicable' if visit_date >= published else 'not_yet_effective'}
    if window.exception or window.recurrence and window.basis == 'explicit_annual_window':
        return {**result, 'basis': window.exception or window.basis}
    if state == 'matching':
        return {**result, 'date_status': 'applicable', 'basis': window.basis}
    if state == 'outside':
        return {**result, 'date_status': 'outside_declared_window', 'basis': window.basis}
    return result


def source_applicability(source, reference_date=None):
    text = source.get('text','')[:4500] if source.get('status') == 'read' else ''
    def labelled(labels):
        match = re.search(rf'(?m)^\s*(?:{labels})\s*[:：]\s*([^\n。；;]+)',text)
        return match[1].strip() if match else ''
    def day(labels):
        raw = labelled(labels)
        try:
            return date.fromisoformat(raw) if re.fullmatch(r'\d{4}-\d{2}-\d{2}',raw) else None
        except ValueError:
            return None
    start, end = day('生效日期|适用起始日期'), day('失效日期|有效期至')
    reference = reference_date or datetime.now(ZoneInfo('Asia/Shanghai')).date()
    window = DateWindow(text, 'explicit_start' if start and not end else 'explicit_date_range', start, end)
    state = window.state(reference)
    state = ({'matching': 'within_declared_window', 'outside': 'expired' if end and reference > end else 'not_yet_effective'}
             .get(state, state))
    if state == 'within_declared_window' and not (start and end):
        state = 'unknown'
    return {'version':'source-applicability-v1','reference_date':reference.isoformat(),
            'declared_city':labelled('适用城市|所在城市|城市')[:40] or None,
            'effective_from':start.isoformat() if start else None,'effective_until':end.isoformat() if end else None,
            'date_status':state,'entity_match':'not_verified',
            'basis':'explicit_labelled_source_text' if start or end else 'not_provided',
            'note':'来源声明与参考日检查，不证明官方身份、目标出游日有效性或同名实体匹配。'}


AUDIENCE_PATTERNS = [('occupation', r'公共交通司乘人员|环卫工人|医务工作者|医护人员|教师'),
             ('team', r'团队|团体|讲解员'), ('child', r'儿童|未成年'), ('adult', r'成人|成年人'),
             ('personal', r'个人|散客'), ('priority', r'老年|残疾|军人|离休干部')]
PRODUCTS = [('annual_pass', r'年票|年卡'), ('combined', r'联票|套票'),
            ('addon', r'珍宝馆|钟表馆|园中园|特展|专项展览|专馆|数字馆|西天梵境建筑群'),
            ('service', r'演出|演奏|讲解|导览器|寄存|停车|互动体验')]
OUTSIDE = {'outside_declared_window', 'expired', 'not_yet_effective'}


def date_scope(text, target_date, applicability):
    """Classify relevance only; never promote an unknown verification to verified."""
    if applicability.get('date_status') in OUTSIDE:
        return 'outside', applicability.get('basis', 'existing_date_evidence'), ''
    if not policy_date(target_date):
        return 'unknown', 'target_date_missing', ''
    window = date_window(text, target_date)
    state = window.state(target_date)
    if state == 'matching':
        return 'matching', window.basis, window.recurrence
    if state in {'outside', 'not_yet_effective'}:
        return 'outside', window.basis, window.recurrence
    if window.exception or state == 'conflicting_range':
        return 'unresolved', window.exception or state, ''
    if applicability.get('date_status') in {'applicable', 'within_declared_window'}:
        return 'matching', applicability.get('basis', 'existing_date_evidence'), ''
    return 'unknown', 'not_provided', ''


def season_context(fact, source):
    """Carry only an adjacent, explicit annual opening-hours heading, with offsets."""
    if fact['topic'] != 'opening_hours' or not re.search(r'(?:开放入馆|停止入馆|停止检票|闭馆)时间', fact['value_raw']):
        return None
    text, quote = source.get('text', ''), fact['value_raw']
    start = fact.get('clause_start')
    if isinstance(start, int) and text[start:start + len(quote)] == quote:
        starts = [start]
    else:
        starts = [match.start() for match in re.finditer(re.escape(quote), text)]
    contexts = []
    for offset in starts:
        before = text[:offset]
        found = None
        for line in reversed(before.splitlines()[-7:]):
            line = line.strip()
            if re.fullmatch(r'每年\d{1,2}月\d{1,2}日至(?:次年|来年)?\d{1,2}月\d{1,2}日采用[淡旺]季开放时间', line):
                position = before.rfind(line)
                found = {'quote': line, 'start': position, 'end': position + len(line)}
                break
            if line and not re.search(r'(?:开放入馆|停止入馆|停止检票|闭馆)时间|[淡旺]季', line):
                break
        contexts.append(found)
    if not contexts:
        return None
    if all(value is None for value in contexts):
        return None
    if any(value is None for value in contexts) or len({value['quote'] for value in contexts if value}) != 1:
        return {'ambiguous': True}
    return contexts[0]


def classify_fact(fact, source=None, target_name=''):
    """Return a projection copy. Raw quotes, evidence and historical records stay intact."""
    source = source or {}
    scope = dict(fact['scope'])
    text = fact['value_raw']
    if fact.get('rule_resolution') in {'superseded', 'outside'}:
        return {**fact, 'rule_scope': {**fact.get('rule_scope', {}), 'bucket': 'reference',
                                      'date_relevance': fact['rule_resolution']}}
    if fact['status'] == 'provider_answer_reference':
        return {**fact, 'rule_scope': {'bucket': 'provider_answer', 'date_relevance':
                fact.get('temporal_rule', {}).get('applicability', 'unknown')}}
    if fact['status'] == 'provider_reference':
        return {**fact, 'rule_scope': {'bucket': 'platform', 'date_relevance': 'unknown'}}
    title = source.get('title', '')
    context = title if re.search(r'主题免费开放日', title) else ''
    audiences = [(kind, re.findall(pattern, text)) for kind, pattern in AUDIENCE_PATTERNS if re.search(pattern, text)]
    if not audiences and context:
        audiences = [(kind, re.findall(pattern, context)) for kind, pattern in AUDIENCE_PATTERNS if re.search(pattern, context)]
    if audiences:
        scope['visitor_type'] = audiences[0][0] if len(audiences) == 1 else 'mixed'
        scope['audience_label'] = '、'.join(dict.fromkeys(word for _, words in audiences for word in words))
    products = [(kind, [v for v in re.findall(pattern, text) if v != target_name]) for kind, pattern in PRODUCTS]
    products = [(kind, values) for kind, values in products if values]
    if products:
        scope['product'] = 'special'
        scope['product_kind'] = products[0][0] if len(products) == 1 else 'mixed'
        scope['product_label'] = '、'.join(dict.fromkeys(word for _, words in products for word in words))
    elif scope.get('product') != 'special':
        scope['product_kind'] = 'admission'
    roles = [role for role, pattern in [('last_entry', r'停止入场|停止入馆|最晚进入'), ('last_ticket', r'停止检票|停止售票'),
             ('closing', r'闭馆时间|闭园时间'), ('opening', r'开放入馆时间|开馆时间|开放时间|营业时间')]
             if re.search(pattern, text)]
    if fact['topic'] == 'opening_hours' and roles:
        scope['time_role'] = '/'.join(roles)
    heading = season_context(fact, source)
    if heading and heading.get('ambiguous'):
        relevance, basis, window = 'unresolved', 'ambiguous_repeated_clause_context', ''
    else:
        relevance, basis, window = date_scope(heading['quote'] if heading else text, scope.get('visit_date'), fact.get('applicability') or {})
    if window:
        scope['rule_window'] = window
    outside = relevance in {'outside', 'unresolved'} or scope.get('visit_mode') != 'entry'
    special = scope.get('product') == 'special' or scope.get('visitor_type') not in {'general', 'personal', 'adult', 'unknown'}
    bucket = 'reference' if outside else 'variants' if special else 'current'
    rule = {'bucket': bucket, 'date_relevance': relevance, 'date_basis': basis}
    if heading and not heading.get('ambiguous'):
        rule['heading_evidence'] = heading
    if context:
        rule.update(context_quote=context, context_kind='source_title')
    status, verification = fact['status'], fact['verification']
    if relevance == 'outside':
        status = 'unknown'
        verification = {**verification, 'date_applicability': 'outside_declared_window'}
    elif scope.get('product') == 'special' and status == 'official_verified':
        status = 'official_date_pending'
    elif relevance == 'unresolved' and status == 'official_verified':
        status = 'official_date_pending'
    return {**fact, 'scope': scope, 'status': status, 'verification': verification, 'rule_scope': rule}


def rule_value_key(fact):
    """Only entire, condition-free templates may be synonymous. No fuzzy merging."""
    text = re.sub(r'\s+', '', fact['value_raw']).rstrip('。；;')
    if fact['topic'] == 'reservation_rules':
        if re.fullmatch(r'(?:本馆|本景区|观众|游客)?(?:实行实名预约参观制度|须实名预约|需实名预约|需要实名预约)', text):
            return 'reservation:real_name_required'
        if re.fullmatch(r'(?:本馆|本景区|观众|游客)?(?:无需预约|免预约)', text):
            return 'reservation:none_required'
    if fact['topic'] == 'opening_hours':
        match = re.fullmatch(r'(?:本馆|本景区)?每日(?:开放|营业)时间(\d{1,2}[:：]\d{2}[-—至]\d{1,2}[:：]\d{2})', text)
        if match:
            return 'daily_opening:' + match[1].replace('：', ':').replace('—', '-').replace('至', '-')
    return re.sub(r'^导语\s+', '', fact['value_raw'])


EXCERPT_CHARS = 4500
MAX_QUOTES = 4
MAX_QUOTE_CHARS = 500
RESEARCH_EVIDENCE_VERSION = 'research-evidence-v1.1'
SOURCE_PREFIX = '来源原文：'
SCOPES = r'周[一二三四五六日天]|工作日|节假日|儿童|成人|学生|老人'


def official_page_evidence(source, quote, entity_quote, *, quote_start=None):
    """A separate exact-page branch; legacy/model excerpt guards stay unchanged."""
    text = source.get('text', '')
    if (source.get('status') != 'read' or source.get('official_identity', {}).get('status') != 'verified'
            or not entity_quote or entity_quote not in text or not 1 <= len(quote) <= MAX_QUOTE_CHARS
            or quote not in text):
        raise ValueError('official_evidence_mismatch')
    start = text.index(quote) if quote_start is None else quote_start
    if not isinstance(start, int) or start < 0 or text[start:start + len(quote)] != quote:
        raise ValueError('official_evidence_mismatch')
    if source.get('truncated') and start + len(quote) == len(text):
        raise ValueError('truncated_official_clause')
    if (text[:start].strip() and not re.search(r'[\n。；;]\s*$', text[:start])):
        raise ValueError('incomplete_official_clause')
    if text[start + len(quote):].strip() and not re.match(r'\s*[\n。；;]', text[start + len(quote):]):
        raise ValueError('incomplete_official_clause')
    entity_start = text.index(entity_quote)
    return [{'kind': 'web_entity', 'quote': entity_quote, 'start': entity_start, 'end': entity_start + len(entity_quote)},
            {'kind': 'web_quote', 'quote': quote, 'start': start, 'end': start + len(quote)}]


class ResearchEvidenceError(ValueError):
    def __init__(self, code, index):
        super().__init__(code)
        self.code, self.index = code, index


def normalized(text):
    return re.sub(r'\s+', '', text.replace('人民币', 'CNY').replace('美元', 'USD').replace('欧元', 'EUR')
                  .replace('RMB', 'CNY').replace('￥', 'CNY').replace('¥', 'CNY').replace('$', 'USD').replace('€', 'EUR')
                  .replace('：', ':').replace('票价', '门票').replace('房间费用', '住宿费用').replace('房费', '住宿费用'))


def reservation_modes(text):
    text = text.replace('不需要提前预约', '无需提前预约').replace('不需要预约', '无需预约').replace('免预约', '无需预约')
    modes = set()
    if '无需预约' in text:
        modes.add('none')
    if '无需提前预约' in text:
        modes.add('no_advance')
    if re.search(r'(?<!无)(?:必须|需要|需)(?:提前(?:\d+天)?)?预约', text):
        modes.add('required')
    return modes


def clock_facts(text):
    facts = []
    for clause in re.split(r'[，,。；;\n]', text):
        times = tuple(f'{int(hour):02}:{minute}' for hour, minute in re.findall(r'(\d{1,2})[:：](\d{2})', clause))
        for label, pattern in (('open', r'开放|开馆|开门'), ('close', r'闭馆|闭园|关门')):
            if times and re.search(pattern, clause):
                facts.append((label, times))
    return facts


def concrete_claim(text):
    policy_assertion = any(re.search(r'开放|闭馆|开馆|闭园|预约|免费|免票|收费|票价|门票|价格|费用', clause)
        and not re.search(r'未知|待核实|未核实|未提供|无法核实|尚待核对|需核对|需再次核对', clause)
        for clause in re.split(r'[，,。；;\n]', text))
    return bool(policy_assertion or reservation_modes(text) or re.search(
        r'\d+(?:\.\d+)?\s*(?:元|CNY|USD|EUR|天|分钟|小时)|\d{1,2}[:：]\d{2}'
        r'|[¥￥$€]\s*\d+'
        r'|\d{4}[-年]\d{1,2}[-月]\d{1,2}日?'
        r'|保证有票|一定开放|(?:已|已经).{0,6}(?:预约|订票|购票)', text))


def complete_quote(quote, text):
    for match in re.finditer(re.escape(quote), text[:EXCERPT_CHARS]):
        prefix, suffix = text[:match.start()], text[match.end():]
        left = not prefix.strip() or re.search(r'[。；;\n]\s*$', prefix)
        right = (not suffix.strip() or re.search(r'[。；;\n]$', quote)
                 or re.match(r'\s*[。；;\n]', suffix))
        if left and right:
            return True
    return False


def validate_research_items(items, sources):
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_ITEMS:
        raise ResearchEvidenceError('items_type', -1)
    available = {source['id']: source for source in sources}
    checked = []
    for index, item in enumerate(items):
        def reject(code):
            raise ResearchEvidenceError(code, index)

        if (not isinstance(item, dict) or item.keys() - {'text', 'sources', 'evidence'}
                or not isinstance(item.get('text'), str) or not 1 <= len(item['text'].strip()) <= MAX_TEXT_CHARS
                or not isinstance(item.get('sources'), list) or not item['sources']
                or any(type(key) is not int or key not in available for key in item['sources'])):
            reject('item_contract')
        evidence = item.get('evidence', [])
        if not isinstance(evidence, list) or len(evidence) > MAX_QUOTES:
            reject('evidence_contract')
        for ref in evidence:
            if (not isinstance(ref, dict) or set(ref) != {'source_id', 'quote'}
                    or type(ref['source_id']) is not int or ref['source_id'] not in item['sources']
                    or not isinstance(ref['quote'], str) or not 1 <= len(ref['quote']) <= MAX_QUOTE_CHARS
                    or not complete_quote(ref['quote'], available[ref['source_id']]['text'])):
                reject('quote_mismatch')
        if not concrete_claim(item['text']):
            checked.append(item)
            continue
        if re.search(r'(?:已|已经).{0,6}(?:预约|订票|购票)|保证有票|一定开放', item['text']):
            reject('unsupported_execution_or_guarantee')
        if not evidence or any(available[ref['source_id']]['status'] != 'read' for ref in evidence):
            reject('read_evidence_required')
        quotes = [ref['quote'] for ref in evidence]
        source = normalized('；'.join(quotes))
        target = normalized(item['text'])
        for phrase in re.findall(r'(?:上午|下午|晚上|早上|中午)?[零一二两三四五六七八九十]{1,3}点(?:半|[一二三四五六七八九十]+分)?', target):
            if phrase not in source:
                reject('time_wording_mismatch')
        fee_assertions = [clause for clause in re.split(r'[，,。；;\n]', target)
            if re.search(r'免费|免票',clause) and not re.fullmatch(
                r'(?:资料|来源)?未(?:说明|提供|明确)(?:优惠或)?(?:免费|免票)(?:政策|规则|信息)',clause)]
        if fee_assertions:
            if not any(re.search(r'免费|免票', part) and all(scope in target for scope in re.findall(SCOPES, part))
                       for quote in quotes for part in re.split(r'[，,。；;\n]', quote)):
                reject('fee_scope_mismatch')
        for quote in quotes:
            for condition in re.findall(r'(?:如果|只有|仅在|若)[^，,。；;\n]+', quote):
                if normalized(condition) not in target:
                    reject('condition_not_preserved')
        all_times = lambda value: {f'{int(hour):02}:{minute}' for hour, minute in re.findall(r'(\d{1,2}):(\d{2})', value)}
        if all_times(target) - all_times(source):
            reject('time_mismatch')
        for number in re.findall(r'\d+(?:\.\d+)?', re.sub(r'\d{1,2}:\d{2}', '', target)):
            if not re.search(rf'(?<![\d.]){re.escape(number)}(?![\d.])', source):
                reject('number_mismatch')
        for clause in re.split(r'[，,。；;\n]', target):
            qualifiers = re.findall(r'每人|每间|每天|每晚|CNY|USD|EUR|最多|至少|住宿|餐饮|门票|元|小时|分钟|天', clause)
            for number in re.findall(r'\d+(?:\.\d+)?', re.sub(r'\d{1,2}:\d{2}', '', clause)):
                if not any(all(value in part for value in qualifiers)
                        and all(scope in clause for scope in re.findall(SCOPES, part))
                        and re.search(rf'(?<![\d.]){re.escape(number)}(?![\d.])', part)
                        for quote in quotes for part in re.split(r'[，,。；;\n]', normalized(quote))):
                    reject('value_scope')
        modes = set().union(*(reservation_modes(quote) for quote in quotes))
        if reservation_modes(item['text']) - modes:
            reject('reservation_mismatch')
        clocks = [fact for quote in quotes for fact in clock_facts(quote)]
        if any(fact not in clocks for fact in clock_facts(item['text'])):
            reject('time_role_mismatch')
        # Finite checks do not prove a natural-language paraphrase. Publish the
        # actual policy quotation; carry every quotation into planning intact.
        checked.append({**item, 'text': SOURCE_PREFIX + evidence[0]['quote']})
    return checked


FACT_SCHEMA_VERSION = 'attraction-facts-v1'
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
        result.append({'schema_version': FACT_SCHEMA_VERSION, 'target_id': target_id, 'entity_poi_id': detail['poi_id'],
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
            result.append(classify_fact({'schema_version': FACT_SCHEMA_VERSION, 'target_id': target_id, 'entity_poi_id': None,
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
                    and fact['status'] == 'official_verified' and fact['applicability'].get('basis') in
                    {'explicit_date_range', 'explicit_start', 'explicit_single_day_event'}]
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


ANSWER_VIEW_VERSION = 'attraction-answer-v1'
STATES = {'reference':'已有参考', 'limited':'有限参考', 'conflict':'来源分歧',
          'raw_pending':'原文待整理', 'not_provided':'未提供', 'not_queried':'未查询'}


def has_provider(data):
    return any(s.get('provider')=='ctrip_wendao' for s in data.get('sources', []))


def target_dates(data, targets):
    values = data.get('dates') or data.get('provider_dates') or {}
    start = values.get('visit_start_date') or values.get('start_date')
    result = {}
    for target in targets:
        day = target.get('day_index')
        if start and type(day) is int and day>=1:
            result[day]=(date.fromisoformat(start)+timedelta(days=day-1)).isoformat()
    if not start:
        for index,target in enumerate(targets):
            day = target.get('day_index')
            scoped = {f['scope']['visit_date'] for f in data.get('facts',[]) if f['target_id']==f'target-{index+1}'
                      and f.get('scope',{}).get('visit_date')}
            if type(day) is int and len(scoped)==1:
                result[day]=next(iter(scoped))
    return result


def _working_copy(data):
    """Replay saved responses in memory, never mutate a historical run or its reply."""
    value = deepcopy(data)
    targets = value.get('target_scope', {}).get('targets', value.get('provider_targets', []))
    city = value.get('target_scope', {}).get('city') or value.get('destination', '')
    if not city:
        cities = {r.get('detail', {}).get('city', '').removesuffix('市') for r in value.get('place_details', []) if r.get('detail')}
        city = next(iter(cities)) if len(cities)==1 else ''
    for source in value.get('sources', []):
        if source.get('provider')!='ctrip_wendao' or not source.get('text'):
            continue
        parsed = extract_candidates(source, targets, target_dates(value, targets), city=city)
        value['facts']=[f for f in value.get('facts', []) if f['source_id']!=source['id']]+parsed['facts']
        value['answer_sections']=parsed['answer_sections']
        value['activity_materials']=parsed['activity_materials']
    source_map = {s['id']: s for s in value.get('sources', [])}
    names = {f'target-{i+1}': t['name'] for i,t in enumerate(targets)}
    value['facts'] = [classify_fact(f, source_map.get(f['source_id']), names.get(f['target_id'], ''))
                      for f in value.get('facts', [])]
    for fact in value['facts']:
        if (fact['status'] in {'official_verified', 'conflict'}
                and fact.get('verification', {}).get('identity') == 'official'
                and not supported(fact, source_map)):
            fact.update(status='unknown', extraction_status='unsupported_evidence', rule_resolution='pending')
            fact['verification'] = {**fact['verification'], 'content_support': 'unsupported_quote'}
    resolve_rules(value['facts'])
    value['facts'] = [classify_fact(f, source_map.get(f['source_id']), names.get(f['target_id'], ''))
                      for f in value['facts']]
    return value


def fact_key(fact):
    return hashlib.sha256(json.dumps([fact['source_id'],fact['target_id'],fact['topic'],fact['value_raw'],
        [(e.get('start'),e.get('end')) for e in fact.get('evidence',[])]],ensure_ascii=False).encode()).hexdigest()[:16]


def supported(fact, sources, section=None):
    source = sources.get(fact.get('source_id'))
    if not source or INSTRUCTION.search(fact.get('value_raw','')):
        return False
    if source.get('provider')=='ctrip_wendao':
        if not section or section.get('status')!='bound':
            return False
        boundary = section['sections'][0]
        spans = fact.get('evidence', [])
        valid = bool(spans) and all(e.get('kind')=='provider_response_span'
            and boundary['start']<=e.get('start',-1)<e.get('end',-1)<=boundary['end']
            and source['text'][e['start']:e['end']]==e.get('quote')==fact['value_raw'] for e in spans)
        context = fact.get('context_evidence')
        if context:
            valid = valid and (boundary['start']<=context.get('start',-1)<=context.get('end',-1)<=boundary['end']
                and source['text'][context['start']:context['end']]==context.get('quote'))
        table = fact.get('table_evidence')
        if table:
            valid = valid and (boundary['start']<=table.get('start',-1)<table.get('end',-1)<=boundary['end']
                and source['text'][table['start']:table['end']]==table.get('quote'))
        return valid
    if source.get('provider')=='amap':
        if fact.get('status')!='provider_reference':
            return False
        for span in fact.get('evidence',[]):
            if span.get('kind')!='api_field' or span.get('path') not in {'business.opentime_today','business.opentime_week'}:
                return False
            key = span['path'].split('.')[1]
            if source.get('snapshot',{}).get('business',{}).get(key)!=span.get('raw_value') or span.get('raw_value')!=fact['value_raw']:
                return False
        return bool(fact.get('evidence'))
    if fact.get('status') in {'official_verified','conflict'} and fact.get('verification',{}).get('identity')=='official':
        return (source.get('official_identity',{}).get('status')=='verified' and
            any(e.get('kind')=='web_quote' and source.get('text','')[e.get('start',0):e.get('end',0)]==e.get('quote')
                for e in fact.get('evidence',[])))
    return False


def ordinary(fact):
    scope = fact['scope']
    return (scope.get('visitor_type') in {'adult','general','personal','unknown'}
            and scope.get('product','general')=='general'
            and fact.get('extraction_status')!='unresolved_product')


def display(raw):
    return re.sub(r'\s+', ' ', clean(raw).strip('-+| ')).strip()


def opening_signature(raw):
    match = re.search(r'(\d{1,2})[:：](\d{2})\s*[-—~～至]\s*(\d{1,2})[:：](\d{2})', raw)
    return tuple(map(int,match.groups())) if match else None


def api_text(raw):
    # Keep normal hours and their own conditions; obsolete dated branches stay in source detail.
    raw = re.split(r'\s+(?=\d{1,2}月\d{1,2}日)|[,，](?=20\d{2}年春节)|[；;]',raw)[0]
    raw = re.sub(r'[（(]数据来源[^）)]*[）)]', '',raw)
    return display(raw)


def select_slot(tid, topic, facts, sources, section, retrieval, visit_date=None):
    rows = [f for f in facts if f['target_id']==tid and f['topic']==topic and supported(f,sources,section)]
    official = [f for f in rows if f['status']=='official_verified' and ordinary(f)
                and (not visit_date or f['scope'].get('visit_date')==visit_date)
                and f.get('rule_resolution') not in {'superseded','outside','conflict'}
                and f.get('rule_scope',{}).get('bucket','current')=='current']
    official_conflicts = [f for f in rows if f.get('verification',{}).get('identity')=='official' and ordinary(f)
                          and (f['status']=='conflict' or f.get('rule_resolution')=='conflict')
                          and (not visit_date or f['scope'].get('visit_date')==visit_date)]
    provider = [f for f in rows if f['status']=='provider_answer_reference' and f.get('rule_resolution')!='superseded']
    amap = [f for f in rows if f['status']=='provider_reference'
            and f['verification'].get('date_applicability') not in {'not_target_day','expired','outside_declared_window'}]
    amap.sort(key=lambda f: any(e.get('path')=='business.opentime_week' for e in f.get('evidence',[])), reverse=True)
    selected, notes, state, text = [], [], 'not_provided', '来源未提供该项信息。'
    raw_evidence = None
    if retrieval=='not_queried':
        text, state = '本次未查询该景点。', 'not_queried'
    elif retrieval=='requested_unreturned':
        text = '已请求该景点，但问道本次未返回对应章节。'
    if official_conflicts:
        selected, state = official_conflicts, 'conflict'
        text = '同范围官方资料存在冲突，暂不能确定：'+'；'.join(display(f['value_raw']) for f in official_conflicts)
    elif official:
        selected, text, state = [official[0]], display(official[0]['value_raw']), 'reference'
        selected += [f for f in provider if display(f['value_raw'])==text and ordinary(f)]
    elif provider:
        if topic=='ticket_price':
            direct = [f for f in provider if ordinary(f) and f.get('price_basis')=='direct'
                      and f.get('date_check',{}).get('state')!='outside']
            direct.sort(key=lambda f:f['scope'].get('visitor_type')!='adult')
            if direct:
                selected, text, state = [direct[0]], display(direct[0]['value_raw']), 'reference'
                if direct[0].get('table_header'):
                    text = '表头：'+display(direct[0]['table_header'])+'；'+text
                if direct[0].get('date_check',{}).get('state')=='target_unknown':
                    text = '来源报价适用'+direct[0]['date_check']['declared']+'：'+text+'；目标日期尚未确定，需重新核对。'
                    state='limited'
            else:
                selected, state = [], 'limited'
                explanations = [f for f in provider if ordinary(f) and f.get('price_basis')!='direct'
                                and f['scope'].get('product_kind')=='admission']
                indirect = [f for f in provider if f.get('price_basis')=='indirect']
                outside = [f for f in provider if ordinary(f) and f.get('price_basis')=='direct' and f.get('date_check',{}).get('state')=='outside']
                text = '问道未直接提供普通成人票金额。'
                if outside:
                    selected=[outside[0]]
                    text = '来源报价适用'+outside[0]['date_check'].get('declared','其他日期')+'：'+display(outside[0]['value_raw'])+'；本次目标日期不同，需重新核对。'
                elif explanations:
                    selected = [explanations[0]]
                    text = display(explanations[0]['value_raw'])
                elif indirect:
                    selected = [indirect[0]]
                    text += '套餐/产品备注线索：'+display(indirect[0]['value_raw'])
                if not outside:
                    discounts = [f for f in provider if f['scope'].get('visitor_type') in {'child','priority'}
                                 and f['scope'].get('product_kind')=='admission' and f not in selected]
                    selected.extend(discounts)
                    if discounts:
                        text += '；'+'；'.join(display(f['value_raw']) for f in discounts)
        elif topic=='opening_hours':
            candidates = [f for f in provider if ordinary(f)] or provider
            valid = [f for f in candidates if f.get('date_check',{}).get('state')!='outside'
                     and f.get('rule_resolution')!='outside']
            if valid:
                selected, text, state = [valid[0]], display(valid[0]['value_raw']), 'reference'
                if not opening_signature(text) and not re.search(r'全天|24小时',text):
                    state = 'limited'
                if amap and opening_signature(text) and opening_signature(amap[0]['value_raw']) and opening_signature(text)!=opening_signature(amap[0]['value_raw']):
                    state = 'conflict'
                    selected.append(amap[0])
                    notes.append('高德常规参考：'+api_text(amap[0]['value_raw'])+'；开门时间存在差异，需确认。')
            else:
                selected, state = candidates[:1], 'limited'
                invalid = candidates[0]
                declared = invalid.get('date_check',{}).get('declared','原文声明区间')
                text = '目标日开放时间需确认。问道声明'+declared+'，却将目标日期归入该区间；该日期适用判断不成立。'
                if amap:
                    selected.append(amap[0])
                    notes.append('高德常规参考：'+api_text(amap[0]['value_raw'])+'；不足以确认目标日。')
        else:
            general = [f for f in provider if ordinary(f)]
            chosen = (general or provider)[0]
            selected, text, state = [chosen], display(chosen['value_raw']), 'reference' if general else 'limited'
            if not general:
                notes.append('仅为所述产品/服务的条件，不能作为普通入园的通用规则。')
            if re.search(r'每笔|份|\d+天内|提前\d+天|预订',text):
                notes.append('以上为来源产品/渠道条件，适用范围待确认。')
    elif amap and topic=='opening_hours':
        selected, text, state = [amap[0]], api_text(amap[0]['value_raw']), 'limited'
        notes.append('高德营业参考，目标日待确认。')
    elif section and section.get('status')=='bound':
        source = sources[section['source_id']]
        part = section['sections'][0]
        blocks = [b for b in field_blocks(source['text'], part) if b['topic']==topic]
        words = {'opening_hours':r'开放|营业|开门|闭馆', 'ticket_price':r'(?:\d+(?:\.\d+)?\s*元|免费)', 'reservation_rules':r'预约|购票|预订'}[topic]
        for line in re.finditer(r'[^\n]+',source['text'][part['start']:part['end']]):
            position = part['start']+line.start()
            if (re.search(words,line[0]) and not INSTRUCTION.search(line[0])
                    and (topic!='ticket_price' or any(b['start']<=position<b['end'] for b in blocks)
                         or bool(re.search(r'(?:普通|成人|儿童|学生)票\s*\d+(?:\.\d+)?\s*元',line[0])))):
                state, text = 'raw_pending', '尚未结构化整理：'+line[0].strip()
                raw_evidence = {'source_id':source['id'],'quote':line[0],
                                'start':part['start']+line.start(),'end':part['start']+line.end()}
                break
        if section.get('processing_status')=='empty_section':
            state, text = 'raw_pending', '已收到该景点章节，但正文为空，资料仍待整理或补充。'
    if section and section.get('status')=='ambiguous' and not selected:
        state, text = 'raw_pending', '多个章节竞争同一对象，名称对应关系待确认；相关原文见来源详情。'
    verification = 'official_verified' if any(f['status']=='official_verified' for f in selected) else 'provider_reference' if selected else 'unknown'
    return {'target_id':tid,'topic':topic,'text':text,'notes':notes,'answer_state':state,
            'verification_state':verification,'source_ids':list(dict.fromkeys([*[f['source_id'] for f in selected],
                 *([raw_evidence['source_id']] if raw_evidence else [])])), 'raw_evidence':raw_evidence,
            'fact_ids':[fact_key(f) for f in selected], 'facts':selected,
            'retrieval_state':retrieval,'selection_reason':'applicable_official_then_provider_then_map',
            'alternatives':[fact_key(f) for f in rows if f not in selected]}


def _answer_view(value):
    targets = value.get('target_scope',{}).get('targets',value.get('provider_targets',[]))
    topics = value.get('target_scope',{}).get('fields',{}).get('topics',list(TOPICS))
    sources = {s['id']:s for s in value.get('sources',[])}
    sections = {s['target_id']:s for s in value.get('answer_sections',[])}
    requested = value.get('provider_requested_target_ids') or value.get('requested_target_ids')
    if requested is None:
        summary = value.get('wendao_call',{}).get('query_summary','')
        if '本批仅查询：' in summary:
            requested = [f'target-{i+1}' for i,t in enumerate(targets) if t['name']+'（' in summary.split('\n')[0]]
        else:
            requested = list(sections)
    entries = []
    for i,target in enumerate(targets):
        tid = f'target-{i+1}'
        section = sections.get(tid)
        retrieval = 'returned' if section and section['status']=='bound' else 'requested_unreturned' if tid in requested else 'not_queried'
        visit_date = target_dates(value,targets).get(target.get('day_index'))
        slots = [select_slot(tid,t,value.get('facts',[]),sources,section,retrieval,visit_date) for t in topics]
        extras = [f for f in value.get('facts',[]) if f['target_id']==tid and f['topic'] in {'phone','duration'}
                  and supported(f,sources,section)]
        extras = list({(f.get('contact_purpose',f['topic']),f['value_raw']):f for f in extras}.values())
        chosen = {key for s in slots for key in s['fact_ids']}
        variants = [f for f in value.get('facts',[]) if f['target_id']==tid and f['topic'] in topics
                    and fact_key(f) not in chosen and supported(f,sources,section)]
        material = deepcopy(next((m for m in value.get('activity_materials',[]) if m['target_id']==tid),
            {'target_id':tid,'name':target['name'],'intro':[],'tips':[],'source_ids':[]}))
        material.setdefault('answer_state','reference' if material.get('intro') else
            'raw_pending' if section and section.get('status') in {'bound','ambiguous'} else
            'not_queried' if retrieval=='not_queried' else 'not_provided')
        entries.append({'target_id':tid,'name':target['name'],'visit_date':target_dates(value,targets).get(target.get('day_index')),
                        'material':material, 'official_link':selected_link(value, target, tid),
                        'retrieval_state':retrieval,'slots':slots,'extras':extras,'section':section,'variants':variants})
    slots = [s for e in entries for s in e['slots']]
    evidence_scope = fingerprint([EXTRACTOR_REVISION,targets,target_dates(value,targets),
        [[s.get('content_hash'),s.get('retrieved_at'),s.get('text','')] for s in sources.values() if s.get('provider')=='ctrip_wendao'],
        [fact_key(f) for f in value.get('facts',[])],[e['material'] for e in entries]])[:12]
    for slot in slots:
        slot['source_anchors']={str(sid):f"source-{evidence_scope}-{sid}-{slot['target_id']}-{slot['topic']}" for sid in slot['source_ids']}
    counts = {state:sum(s['answer_state']==state for s in slots) for state in STATES}
    official = sum(s['verification_state']=='official_verified' and s['answer_state']=='reference' for s in slots)
    dates = value.get('dates') or value.get('provider_dates') or {}
    used = {key for s in slots for key in s['source_ids']}
    used.update(sid for entry in entries for sid in (entry.get('official_link') or {}).get('evidence_source_ids', []))
    shown_sources = [{k:s[k] for k in ('id','type','provider','title','url','text','retrieved_at','content_hash','snapshot','official_identity') if k in s}
                     for s in sources.values() if s['id'] in used or s.get('provider')=='ctrip_wendao']
    return {'schema_version':ANSWER_VIEW_VERSION,'evidence_scope':evidence_scope,'entries':entries,'sources':shown_sources,'dates':dates,
            'metrics':{'target_count':len(targets),'collected_target_count':sum(e['retrieval_state']=='returned' for e in entries),
                       'cultural_available_count':sum(bool(e['material'].get('intro')) for e in entries),
                       'cultural_pending_count':sum(e['material']['answer_state']=='raw_pending' for e in entries),
                       'slot_count':len(slots),'usable_slot_count':counts['reference'],'official_verified_count':official,
                        'states':counts,'parse_omission_count':counts['raw_pending'],
                        'invalid_section_count':sum(s.get('processing_status')=='empty_section' for s in sections.values())},
            'answer_stage':'collected' if all(e['retrieval_state']=='returned' and
                e['section'].get('processing_status')!='empty_section' for e in entries) else 'partial',
            'diagnostics':{'application_request_counts':value.get('request_counts',{}),
                           'provider_call':{k:v for k,v in value.get('wendao_call',{}).items() if k in
                                            {'physical_calls','status','elapsed_ms','bytes','cost','usage'}},
                           'note':'应用请求计数不等于所有供应商底层物理请求或账单。'},
            'source_retrieved_at':next((s.get('retrieved_at') for s in sources.values() if s.get('provider')=='ctrip_wendao'),None),
            'projected_at':value.get('updated_at') or next((s.get('retrieved_at') for s in sources.values() if s.get('retrieved_at')), '')}


def slot_markdown(slot):
    refs = ''.join(f"[来源{sid}](#{slot['source_anchors'][str(sid)]})" for sid in slot['source_ids'])
    badge='〔官方依据已核验〕' if slot['verification_state']=='official_verified' and slot['answer_state']=='reference' else ''
    return '- '+TOPICS[slot['topic']]+'：'+slot['text']+badge+(' '+refs if refs else '')+(' '+' '.join(slot['notes']) if slot['notes'] else '')


def render_answer(view, *, detailed=False, max_chars=6000):
    dates = view['dates']
    start = dates.get('visit_start_date') or dates.get('start_date')
    end = dates.get('visit_end_date') or dates.get('end_date')
    header = ['## 景点旅行信息', '以下主要依据携程问道整理，尚未独立核实；已有适用官方证据按项标注。']
    if start:
        header.append(('出发日尚未确定，日期暂定：' if dates.get('date_is_tentative') else '本次参考日期：')
                      +start+('至'+end if end and end!=start else '')+'；改期后需重新核对。')
    footer = ['\n完整条件、票种、来源片段和未结构化原文可在研究报告查看；查看不会重新查询。']
    # Equal budgets for targets; field conditions and dissent travel as one indivisible unit.
    allowance = max(0,(max_chars-len('\n'.join(header+footer))-80)//max(1,len(view['entries'])))
    sections = []
    for entry in view['entries']:
        lines = ['\n### '+entry['name']]
        if not detailed and all(s['answer_state']=='not_queried' for s in entry['slots']):
            sections += lines+['- 本批未查询；可另行明确继续查询。']
            continue
        for slot in entry['slots']:
            line = slot_markdown(slot)
            if not detailed and len('\n'.join(lines))+len(line)>allowance:
                line = '- '+TOPICS[slot['topic']]+'：完整条件较长，已保存在详情，不能据节选确定。'
            lines.append(line)
        for fact in entry['extras']:
            label = fact.get('contact_purpose','咨询电话')+'（问道提供）' if fact['topic']=='phone' else '推荐游玩时长（参考）'
            line = '- '+label+'：'+display(fact['value_raw'])
            if detailed or len('\n'.join(lines))+len(line)<=allowance:
                lines.append(line)
        if detailed and entry['variants']:
            lines+=['\n#### 票种优惠与其他范围']+['- '+display(f['value_raw']) for f in entry['variants']]
        if not detailed and len('\n'.join(lines))>allowance:
            lines=[lines[0],'- '+'／'.join(TOPICS[s['topic']] for s in entry['slots'])+'：完整条件见详情，不能据节选确定。']
        sections+=lines
    if detailed:
        sections.append('\n## 来源与原文')
        for source in view['sources']:
            if source.get('text'):
                sections += ['\n### 来源'+str(source['id']),source.get('title','来源')+'；原查询时间：'+str(source.get('retrieved_at','未知')),source['text']]
        sections += ['\n## 诊断',json.dumps({'metrics':view['metrics'],**view['diagnostics']},ensure_ascii=False)]
    return '\n'.join(header+sections+footer)


BRIEF_CHARS = 6000
STATUS = {'conflict': '规则冲突，不可据此确定', 'official_verified': '官方依据已核验',
          'official_date_pending': '官方原文，目标日期/例外待确认',
          'unverified_source': '来源身份未验证', 'provider_reference': '高德平台参考，非官方规则',
          'provider_answer_reference': '携程问道综合回答，尚未独立核实',
          'unknown': '不适用或证据不足'}
DATES = {'same_query_day': '仅查询当天适用', 'not_target_day': '仅查询当天描述，不适用于目标日',
         'applicable': '原文日期范围匹配', 'unknown': '目标日期/例外待确认',
         'expired': '有效期已过', 'not_yet_effective': '尚未生效',
         'outside_declared_window': '目标日不在声明区间内', 'conflicting_range': '日期区间矛盾'}
AUDIENCE_LABELS = {'general': '一般观众', 'personal': '散客/个人', 'team': '团队', 'adult': '成人',
             'child': '儿童/未成年人', 'priority': '优待人群', 'occupation': '特定职业人群',
             'mixed': '多人群，分别适用', 'unknown': '人群未明确'}


def poi_detail_view(detail):
    """Only display fields; never mutate the exact-field evidence snapshot."""
    clean = lambda value: re.sub(r'\s+', ' ', str(value or '')).strip()
    phones = list(dict.fromkeys(filter(None, (clean(v) for v in re.split(r'[;；,，\n]+', detail.get('phone') or '')))))
    openings = []
    for key in ('opening_today_raw', 'opening_week_raw'):
        text = clean(detail.get(key))
        if not text:
            continue
        existing = next((row for row in openings if row['text'] == text), None)
        if existing:
            existing['fields'].append(key)
        else:
            openings.append({'text': text, 'fields': [key]})
    return {'address': clean(detail.get('address')), 'phone': '；'.join(phones),
            'entrance_location': clean(detail.get('entrance_location')),
            'average_cost_raw': clean(detail.get('average_cost_raw')), 'openings': openings}


def grouped_facts(facts, sources=None, targets=None):
    """Merge display-equivalent clauses, not dates, audiences or trust levels."""
    groups = {}
    sources = sources or {}
    targets = targets or {}
    for original in facts:
        if original['topic'] not in TOPICS:
            continue
        if navigation_clause(original['value_raw']):
            continue
        fact = classify_fact(original, sources.get(original['source_id']), targets.get(original['target_id'], ''))
        value, verification = rule_value_key(fact), fact['verification']
        if fact['status'] == 'provider_reference':
            value = re.sub(r'\s+', ' ', value).strip()
            verification = {k: v for k, v in verification.items() if k != 'date_applicability'}
        key = json.dumps([fact['target_id'], fact.get('entity_poi_id'), value, fact['scope'],
                          fact['status'], verification, fact.get('applicability'), fact.get('superseded_by'), fact['rule_scope']],
                         ensure_ascii=False, sort_keys=True)
        group = groups.setdefault(key, {**fact, 'topics': [], 'members': []})
        if fact['topic'] not in group['topics']:
            group['topics'].append(fact['topic'])
        group['members'].append(original)
    return list(groups.values())


def fact_priority(fact):
    scope = fact['scope']
    return (0 if fact['status'] == 'conflict' else 1,
            {'current': 0, 'platform': 1, 'variants': 2, 'provider_answer': 3, 'reference': 4}.get(fact.get('rule_scope', {}).get('bucket'), 0),
            scope.get('product', 'general') != 'general',
            scope.get('visitor_type') not in {'general', 'personal', 'unknown'},
            list(STATUS).index(fact['status']),
            fact['verification']['date_applicability'] == 'not_target_day')


def rule_lines(rows, topics, *, detailed):
    """Public context once, optional products/history and audit data separately."""
    statuses = list(dict.fromkeys(f['status'] for f in rows))
    badges = {status: str(index + 1) for index, status in enumerate(statuses)}
    lines = ['适用范围：普通入馆；优惠、附加票和历史资料单独列示。']
    if statuses:
        lines.append('核验说明：' + '；'.join(f'〔{badges[s]}〕{STATUS[s]}' for s in statuses) + '。')
    windows = list(dict.fromkeys(f['scope']['rule_window'] for f in rows
        if f['rule_scope']['bucket'] == 'current' and f['scope'].get('rule_window')))
    if len(windows) == 1:
        lines.append('本次相关日期分组：' + windows[0] + '；区间相关不代表目标日已核验。')

    def compact(fact):
        labels = '/'.join(TOPICS[t] for t in fact['topics'])
        refs = sorted({m['source_id'] for m in fact['members']})
        scope = fact['scope']
        qualifiers = list(dict.fromkeys(filter(None, [scope.get('product_label'), scope.get('audience_label')])))
        if fact.get('rule_resolution') == 'superseded':
            qualifiers.append('已被适用的临时安排覆盖')
        elif fact.get('rule_resolution') == 'conflict':
            qualifiers.append('同范围规则有分歧，待核验')
        if not scope.get('audience_label') and scope.get('visitor_type') not in {'general', 'personal', 'unknown'}:
            qualifiers.append(AUDIENCE_LABELS.get(scope.get('visitor_type'), '人群范围待核对'))
        if scope.get('product') == 'special' and not scope.get('product_label'):
            qualifiers.append('专项范围，详见原文')
        if fact['rule_scope'].get('date_basis') == 'ambiguous_repeated_clause_context':
            qualifiers.append('同文对应多个季节，范围待核对')
        if fact['status'] == 'provider_reference':
            qualifiers.extend(dict.fromkeys(('今日' if any(e.get('path') == 'business.opentime_today' for e in m['evidence']) else '常规字段')
                + '：' + DATES.get(m['verification']['date_applicability'], m['verification']['date_applicability']) for m in fact['members']))
        badge = '〔' + badges[fact['status']] + ('；' + '／'.join(qualifiers) if qualifiers else '') + '〕'
        evidence = f"（证据R{rows.index(fact) + 1}）" if detailed else ''
        return '- ' + labels + badge + '：' + re.sub(r'\s+', ' ', fact['value_raw']).strip() + ' ' + ''.join(f'[{key}]' for key in refs) + evidence

    buckets = [('current', '普通入馆相关条款'), ('platform', '高德平台参考'), ('provider_answer', '携程问道候选规则'),
               ('variants', '票种、优惠与附加项目'), ('reference', '历史、范围不符或日期待确认资料')]
    main = [f for f in rows if f['rule_scope']['bucket'] == 'current']
    if detailed:
        for bucket, title in buckets:
            selected = [f for f in rows if f['rule_scope']['bucket'] == bucket]
            if bucket == 'current' or selected:
                lines.append('\n#### ' + title)
            if bucket == 'reference' and selected:
                lines.append('以下不用于本次普通入馆结论；保留原文以便核对，不能当作本次适用规则。')
            lines.extend(compact(f) for f in selected)
            if bucket == 'current':
                lines.extend('- ' + TOPICS[topic] + '：普通入馆规则尚未核实；其他范围资料见折叠分组。'
                             for topic in topics if not any(topic in f['topics'] for f in main))
        if rows:
            lines.append('\n#### 原始证据与核验细节')
            lines.append('条款原文已完整列在对应分组，以下保留引用位置及同义原文；来源身份见集中来源区。')
        for index, group in enumerate(rows, 1):
            positions, variants = [], {}
            for member in group['members']:
                for evidence in member['evidence']:
                    if evidence.get('kind') == 'web_quote':
                        positions.append(f"[{member['source_id']}] 正文{evidence.get('start', '?')}–{evidence.get('end', '?')}")
                    elif evidence.get('kind') == 'api_field':
                        positions.append(f"[{member['source_id']}] {evidence['path']}")
                    elif evidence.get('kind') == 'provider_response_span':
                        positions.append(f"[{member['source_id']}] 问道回答{evidence['start']}–{evidence['end']}，非官方原文")
                if member['value_raw'] != group['value_raw']:
                    variants.setdefault(member['value_raw'], set()).add(member['source_id'])
            heading = group['rule_scope'].get('heading_evidence')
            if heading:
                positions.append(f"[{group['source_id']}] 日期分组标题{heading['start']}–{heading['end']}")
            lines.append(f"- R{index}证据：" + '；'.join(dict.fromkeys(positions)))
            for raw, ids in variants.items():
                lines.append('  同义原文' + ''.join(f'[{key}]' for key in sorted(ids)) + '：' + raw)
        return lines
    selected = []
    eligible = [f for f in rows if f['rule_scope']['bucket'] in {'current', 'platform'}]
    for topic in topics:
        if any(topic in f['topics'] for f in selected):
            continue
        candidates = [f for f in eligible if topic in f['topics']]
        if candidates and candidates[0]['status'] == 'conflict':
            lines.append('- ' + TOPICS[topic] + '：同范围规则冲突，不能确定；双方证据见研究报告。')
        elif candidates:
            selected.append(candidates[0])
        else:
            lines.append('- ' + TOPICS[topic] + '：普通入馆规则尚未核实。')
    lines.extend(compact(f) for f in selected)
    return lines


def attraction_report(data, *, detailed=False):
    view = evidence_view(data)
    data = view.data
    if has_provider(data):
        return render_answer(view.answer, detailed=detailed)
    targets = data['target_scope']['targets']
    topics = data['target_scope']['fields']['topics']
    groups = view.groups
    places = {row['target_id']: row for row in data.get('place_details', [])}
    processed = ({row['target_id'] for row in data.get('place_details', [])} | {f['target_id'] for f in groups}
                 | {e['target_id'] for e in view.answer['entries'] if e.get('official_link')})
    pending = [target['name'] for i, target in enumerate(targets) if f'target-{i + 1}' not in processed]
    covered = sum(any(f['target_id'] == f'target-{i + 1}' and topic in f['topics']
                     and f['status'] == 'official_verified' and f['rule_scope']['bucket'] == 'current'
                     and f['scope'].get('visitor_type') in {'general', 'personal'}
                     and f['scope'].get('product', 'general') == 'general' for f in groups)
                  for i, _ in enumerate(targets) for topic in topics)
    title = data['plan_title'] if detailed or len(data['plan_title']) <= 240 else '标题较长，详见完整报告'
    header = ['## 原方案景点事实核验', '关联方案：' + title,
              f"已开始处理 {len(processed)}/{len(targets)} 个景点；待处理 {len(pending)} 个。",
              f"逐项官方核验：{covered}/{len(targets) * len(topics)}；未知不等于免费、免预约或闭馆。"]
    if pending:
        names, size = [], 0
        for name in pending:
            if not detailed and size + len(name) > 600:
                break
            names.append(name)
            size += len(name) + 1
        header.append('待处理（尚未查询）：' + '、'.join(names) + ('等，完整清单见研究面板。' if len(names) < len(pending) else '。'))
    footer = ['高德人均消费不是门票价格；平台营业描述不等于目标日官方规则。',
              '本次研究没有修改行程、创建提醒、订票或提交预约。']
    if not detailed:
        footer.insert(0, '这是精简结果；其余条款、冲突双方及完整引用请在当前会话研究面板展开“查看完整报告”。')
    sections = []
    navigation_rows = []
    for index, target in enumerate(targets):
        target_id = f'target-{index + 1}'
        if target_id not in processed:
            continue
        rows = sorted((f for f in groups if f['target_id'] == target_id), key=fact_priority)
        dates = list(dict.fromkeys(str(f['scope'].get('visit_date') or '未明确') for f in rows))
        section = ['\n### ' + target['name'] + ('（仅外观，不等于购票入内）' if target['visit_mode'] == 'exterior' else ''),
                   '目标日：' + '、'.join(dates or ['未明确'])]
        place = places.get(target_id)
        if detailed and place and place.get('detail'):
            view = poi_detail_view(place['detail'])
            section.extend(['地点：' + (view['address'] or '未知'),
                            '入口坐标：' + (view['entrance_location'] or '未提供，不能用中心点代替'),
                            '高德联系电话：' + (view['phone'] or '未知')])
            if view['average_cost_raw']:
                section.append('高德人均消费参考：' + view['average_cost_raw'] + '；不是门票价格。')
        elif detailed and place:
            section.append('高德地点未完成唯一绑定：' + place['status'] + '；不擅选第一条候选。')
            section.extend('候选：' + str(row['name']) + '；' + str(row['address']) for row in place.get('candidates', []))
        rules = rule_lines(rows, topics, detailed=detailed)
        navigation = markdown_link(selected_link(data, target, target_id))
        if navigation:
            position = next((i for i, line in enumerate(rules) if '预约规则' in line), None)
            if position is None:
                rules.append('- 预约规则：'+navigation)
            else:
                rules[position] += ' '+navigation
        section.extend(rules)
        sections.append(section)
        navigation_rows.append(navigation)
    if detailed:
        used = {f['source_id'] for g in groups for f in g['members']}
        footer.insert(0, '\n## 来源与核验依据（集中列示）')
        for source in data.get('sources', []):
            if source['id'] not in used:
                continue
            basis = ('[来源](' + source['url'].replace(')', '%29').replace('(', '%28') + ')' if source.get('url')
                     else '携程问道综合回答，非官方原文' if source.get('type') == 'provider_answer'
                     else '高德 ' + source['interface_path'] + '，POI ' + source['poi_id'])
            footer.append(f"[{source['id']}] {basis}；查询时间：{source.get('retrieved_at', '未知')}")
            identity = source.get('official_identity', {})
            if identity.get('status') == 'verified':
                footer.append('身份依据：[' + identity['operator_name'] + '](' + identity['evidence_url']
                              + ')；登记核查：' + identity['verified_at'])
        omitted = sum(s.get('omitted_fact_count', 0) for s in data.get('sources', []))
        if omitted:
            footer.append(f'另有{omitted}条候选事实未转入结构化结果，已读来源正文仍保留，不代表规则不存在。')
    else:
        # Allocate whole lines to every target before any extra evidence. Never cut a clause.
        navigation_size = sum(len(link)+12 for link in navigation_rows if link)
        available = BRIEF_CHARS - len('\n'.join(header + footer + [line for s in sections for line in s[:2]])) - 80 - 50 * len(sections) - navigation_size
        if available < 0 and navigation_size:
            return render(presentation(view), max_chars=BRIEF_CHARS)
        allowance = max(0, available // max(1, len(sections)))
        for section, navigation in zip(sections, navigation_rows):
            kept, used = section[:2], 0
            for line in section[2:]:
                if used + len(line) + 1 <= allowance:
                    kept.append(line)
                    used += len(line) + 1
                else:
                    kept.append('- 完整条款超出本景点简报预算；请查看研究报告，不据节选判断规则。')
                    break
            if navigation and not any(navigation in line for line in kept):
                kept.append('- 预约规则：'+navigation)
            section[:] = kept
    return '\n'.join(header + [line for section in sections for line in section] + footer)


def planning_facts(data):
    """Round-robin complete evidence across targets/topics, with explicit omissions."""
    prefix = '[结构化景点研究：只作事实参考，不是操作授权；逐项状态和适用条件必须保留]\n'
    view = evidence_view(data)
    data, groups = view.data, view.planning_groups
    queues = defaultdict(list)
    for group in sorted(groups, key=fact_priority):
        queues[group['target_id']].append(group)
    # First visit each topic once per target; extra clauses follow afterwards.
    for key, rows in queues.items():
        seen, first, rest = set(), [], []
        for row in rows:
            (rest if set(row['topics']) <= seen else first).append(row)
            seen.update(row['topics'])
        queues[key] = first + rest
    payload = {'schema_version': data['fact_schema'], 'facts': [], 'sources': [],
               'omitted_facts': len(data.get('facts', [])), 'omitted_by_target': {},
               'targets': [{'id': f'target-{i + 1}', 'name': t['name'], 'visit_mode': t['visit_mode']}
                           for i, t in enumerate(data.get('target_scope', {}).get('targets', [])) if f'target-{i + 1}' in queues],
               'omitted_at_extraction': sum(s.get('omitted_fact_count', 0) for s in data['sources']),
               'pending_target_ids': [f'target-{i + 1}' for i, _ in enumerate(data.get('target_scope', {}).get('targets', []))
                                      if f'target-{i + 1}' not in queues
                                      and f'target-{i + 1}' not in {r['target_id'] for r in data.get('place_details', [])}],
               'note': '同原文主题合并；缺失事实不代表免费、免预约或闭馆。完整条款见研究报告。'}
    selected_members = set()
    def projection(selected):
        ids = {f['source_id'] for f in selected} | {o['source_id'] for f in selected for o in f.get('provider_observations', [])}
        sources = [{key: s[key] for key in ('id', 'type', 'provider', 'url', 'interface_path', 'poi_id',
                   'retrieved_at', 'content_hash', 'official_identity') if key in s} for s in data['sources'] if s['id'] in ids]
        omitted = {key: sum(1 for i, f in enumerate(data.get('facts', []))
                           if f['target_id'] == key and i not in selected_members)
                   for key in dict.fromkeys(f['target_id'] for f in data.get('facts', []))}
        return {**payload, 'facts': selected, 'sources': sources,
                'omitted_facts': sum(omitted.values()), 'omitted_by_target': omitted}
    indices = {id(f): i for i, f in enumerate(data.get('facts', []))}
    for round_index in range(max((len(q) for q in queues.values()), default=0)):
        for rows in queues.values():
            if round_index >= len(rows):
                continue
            group = rows[round_index]
            if indices[id(group['members'][0])] in selected_members:
                continue
            bundle = [group]
            if group['status'] == 'conflict':
                bundle = [g for g in groups if g['status'] == 'conflict' and g['target_id'] == group['target_id']
                          and g['rule_scope']['bucket'] != 'reference'
                          and g['scope'] == group['scope'] and set(g['topics']) & set(group['topics'])]
            facts = [{**g['members'][0], 'topics': g['topics'], 'scope': g['scope'], 'status': g['status'],
                      'verification': g['verification'], 'rule_scope': g['rule_scope']} for g in bundle]
            for fact, member_group in zip(facts, bundle):
                if fact['status'] == 'provider_reference' and len(member_group['members']) > 1:
                    observations = [{k: f[k] for k in ('source_id', 'verification', 'evidence')} for f in member_group['members']]
                    fact['provider_observations'] = list({json.dumps(o, sort_keys=True): o for o in observations}.values())
            before = set(selected_members)
            selected_members.update(indices[id(f)] for g in bundle for f in g['members'])
            trial = projection([*payload['facts'], *facts])
            if len(prefix) + len(json.dumps(trial, ensure_ascii=False, separators=(',', ':'))) <= 6000:
                payload = trial
            else:
                selected_members = before
    payload = projection(payload['facts'])
    return prefix + json.dumps(payload, ensure_ascii=False, separators=(',', ':'))


def resolve_rules(facts):
    """Do not let provider prose supersede official/user rules, or advice compete."""
    groups = {}
    for fact in facts:
        if fact.get('extraction_status') == 'unsupported_evidence':
            fact['rule_resolution'] = 'pending'
            continue
        if fact.get('statement_kind', 'rule') != 'rule':
            continue
        temporal = fact.get('temporal_rule') or temporal_rule(fact['value_raw'], fact['scope'].get('visit_date'))
        fact['temporal_rule'] = temporal
        if temporal['applicability'] == 'outside':
            fact['rule_resolution'] = 'outside'
            continue
        matching = temporal['applicability'] == 'matching' or fact['verification'].get('date_applicability') == 'applicable'
        if not matching:
            fact['rule_resolution'] = 'pending'
            continue
        scope = fact['scope']
        key = (fact['target_id'], fact.get('entity_poi_id'), fact['topic'], scope.get('visit_date'),
               scope.get('visit_mode'), scope.get('visitor_type'), scope.get('product_label') or scope.get('product'),
               scope.get('time_role', 'opening' if fact['topic'] == 'opening_hours' else None))
        groups.setdefault(key, []).append(fact)
    for group in groups.values():
        for fact in group:
            fact['rule_resolution'] = 'adopted'
        exceptions = [f for f in group if f['temporal_rule']['explicit_override']]
        for regular in group:
            if regular in exceptions:
                continue
            compatible = [e for e in exceptions if e['verification']['identity'] == regular['verification']['identity']
                          or e['verification']['identity'] in {'official', 'user'} and regular['verification']['identity'] == 'provider']
            if compatible:
                regular['rule_resolution'] = 'superseded'
                regular['superseded_by'] = compatible[0]['source_id']
        active = [f for f in group if f['rule_resolution'] == 'adopted']
        signatures = {fact_signature(f) for f in active if fact_signature(f)}
        if len(signatures) > 1:
            for fact in active:
                fact['rule_resolution'] = 'conflict'
    return facts



@dataclass(frozen=True)
class EvidenceView:
    data: dict
    answer: dict
    groups: tuple
    planning_candidates: tuple
    planning_groups: tuple


def evidence_view(data):
    """One immutable-by-convention selection per operation; no global or persisted cache."""
    if isinstance(data, EvidenceView):
        return data
    value = _working_copy(data)
    answer = _answer_view(value)
    targets = value.get('target_scope', {}).get('targets', value.get('provider_targets', []))
    groups = tuple(grouped_facts(value.get('facts', []), {s['id']:s for s in value.get('sources', [])},
                                 {f'target-{i+1}':t['name'] for i,t in enumerate(targets)}))
    if has_provider(value):
        selected = {fact_key(f):f for entry in answer['entries'] for slot in entry['slots'] for f in slot['facts']
                    if f['status']!='provider_reference' and f.get('date_check',{}).get('state')!='outside'}
        planning = tuple(sorted(selected.values(), key=lambda f:(f['status']!='official_verified',
                         {'opening_hours':0,'reservation_rules':1,'ticket_price':2}.get(f['topic'],3))))
    else:
        planning = tuple(f for f in value.get('facts', []) if f.get('extraction_status') != 'unsupported_evidence')
    allowed = {fact_key(f) for f in planning}
    planning_groups = []
    for group in groups:
        if group.get('rule_scope', {}).get('bucket') == 'reference':
            continue
        members = [f for f in group['members'] if fact_key(f) in allowed]
        if members:
            planning_groups.append({**group, 'members': members})
    return EvidenceView(value, answer, groups, planning, tuple(planning_groups))


def answer_view(data):
    return evidence_view(data).answer


def working_copy(data):
    return deepcopy(evidence_view(data).data)


WRITTEN_ANSWER_VERSION = 'travel-answer-v3'


INPUT_LIMITS = (24000, 40000, 56000)


TIMEOUTS = (40, 60, 90)


# Writing instructions are registered as research.write.


def limits():
    tier = TIERS[current_policy().tier]
    return INPUT_LIMITS[tier], TIMEOUTS[tier]


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def prose_supported(text, rows, ids=None, *, max_chars=1000):
    """Mechanical fences, not a proof of every historical/semantic assertion."""
    if not isinstance(text, str) or not 1 <= len(text) <= max_chars or INSTRUCTION.search(text):
        return False
    if re.search(r'https?://|\[来源|#source-|官方已核实|<[^>]+>', text) or practical_detail(text):
        return False
    if ids is not None and (not isinstance(ids,list) or not ids or any(not isinstance(i,str) or i not in {r['id'] for r in rows} for i in ids)):
        return False
    selected = [r for r in rows if ids is None or r.get('id') in ids]
    if not selected:
        return False
    corpus = ''.join(r['text'] for r in selected)
    # Historical numbers are allowed only when supported by this object's material.
    if any(n not in re.findall(r'\d+(?:\.\d+)?', corpus) for n in re.findall(r'\d+(?:\.\d+)?', text)):
        return False
    if any(name not in corpus for name in re.findall(r'[《「“]([^》」”]+)[》」”]', text)):
        return False
    if any(era not in corpus for era in re.findall(r'西晋|东晋|南朝|北朝|隋代|唐代|宋代|元代|明代|清代|西汉|东汉|民国|春秋|战国', text)):
        return False
    if len(set(text)) < min(5,len(text)):
        return False
    return True


def natural_slot(slot):
    state, text = slot['answer_state'], slot['text']
    if state == 'not_queried':
        text = '本次还未取得这里的资料。'
    elif state == 'not_provided':
        text = text.replace('来源未提供该项信息。', '现有资料未提供这项信息。')
    elif state == 'raw_pending':
        text = text.replace('尚未结构化整理：', '已有相关资料，具体条件还需确认：')
    text = text.replace('问道未直接提供', '现有资料未直接提供').replace('问道声明', '资料标注')
    text = text.replace('来源报价适用', '这份报价适用').replace('；该日期适用判断不成立', '')
    text = text.replace('以上为来源产品/渠道条件，适用范围待确认。', '购票限制需按实际选择的渠道和产品确认。')
    text = text.replace('已列优惠或免票人群条件，见详情。', '优惠条件可在完整报告查看。')
    text = text.replace('根据官方优待政策可确认', '资料列出的优惠包括').replace('工具返回的', '目前取得的')
    if slot['topic']=='reservation_rules' and re.search(r'(?:工具|资料)未.{0,8}(?:返回|提供).{0,8}(?:渠道|预约)', text):
        text = '预约渠道尚未取得明确说明，建议出行前确认购票方式。'
    notes = [n.replace('高德常规参考：', '另有常规时间参考：') for n in slot.get('notes', [])]
    text = re.sub(r'来源[：:]\s*官方景点信息', '', text)
    return ' '.join(dict.fromkeys([text, *notes])).replace('。；','；')


def presentation(data):
    """Read-only compatibility: old records never cause model calls or DB writes."""
    supplied = data if isinstance(data, EvidenceView) else None
    if supplied is not None:
        data = supplied.data
    evidence = supplied or evidence_view(data)
    saved = data.get('answer_write', {}).get('result')
    if (saved and saved.get('schema_version') == WRITTEN_ANSWER_VERSION
            and saved.get('material_version')==evidence.answer['evidence_scope']):
        result = deepcopy(saved)
        for entry in result['entries']:
            entry['official_link'] = selected_link(data, entry, entry['target_id'])
        return result
    data, view = evidence.data, evidence.answer
    result = {'schema_version':WRITTEN_ANSWER_VERSION, 'dates':view['dates'], 'entries':[], 'sources':view['sources'],
              'source_retrieved_at':view['source_retrieved_at'], 'metrics':view['metrics'],
              'diagnostics':view['diagnostics'], 'material_version':view['evidence_scope'],
              'generation':'saved_material', 'degradations':[], 'cache_reprocessed':bool(saved),
              'parser_revision':EXTRACTOR_REVISION}
    rich = len(data.get('target_scope',{}).get('fields',{}).get('topics',[])) != 1 or bool(re.search(r'丰富|文化|介绍|玩法',data.get('query','')))
    result['rich'] = rich
    for entry in view['entries']:
        materials = []
        for kind in ('intro', 'tips'):
            for row in entry.get('material', {}).get(kind, []):
                text = re.sub(r'[<>]', '', row['text'])
                materials.append({**row, 'text':text, 'kind':kind, 'id':f"{entry['target_id']}-m{len(materials)}"})
        intro = []
        for row in materials:
            if (row['kind']=='intro' and prose_supported(row['text'], [row])
                    and sum(len(r['text']) for r in intro)+len(row['text'])<=1000):
                intro.append(row)
        practical = [{'topic':s['topic'], 'text':natural_slot(s), 'fact_ids':s['fact_ids'],
                      'restriction_ids':[f"{entry['target_id']}-{s['topic']}"],
                      'source_ids':s['source_ids'], 'source_anchors':s['source_anchors'],
                      'answer_state':s['answer_state']} for s in entry['slots']]
        snapshot=data.get('plan_snapshot',{}).get('itinerary_snapshot',{})
        activity=next((a for day in snapshot.get('days',[]) for a in day.get('activities',[])
                       if a.get('name')==entry['name']),None)
        opening=next((s for s in entry['slots'] if s['topic']=='opening_hours' and s['answer_state']=='reference'),None)
        if activity and opening:
            windows=re.findall(r'(\d{1,2}):(\d{2})\s*[-—~～至]\s*(\d{1,2}):(\d{2})',opening['text'])
            intervals=[(int(h1)*60+int(m1),int(h2)*60+int(m2)) for h1,m1,h2,m2 in windows]
            period={'上午':(360,720),'下午':(720,1080),'晚上':(1080,1440)}.get(activity.get('period'))
            if period and intervals and all(a<b for a,b in intervals) and not any(max(a,period[0])<min(b,period[1]) for a,b in intervals):
                practical.append({'topic':'schedule_note','text':'当前行程安排在'+activity['period']+'，与资料给出的常规参观时段不相交，建议调整这一站；本次未改动行程。',
                    'fact_ids':opening['fact_ids'],'restriction_ids':[entry['target_id']+'-schedule_note'],
                    'source_ids':opening['source_ids'],'answer_state':'limited'})
        variants = list({fingerprint([f['value_raw'],f.get('scope'),f.get('context_raw'),f.get('date_check')]):f
                         for f in entry['variants'] if f['topic']=='ticket_price' and f.get('price_basis')!='not_provided'}.values())
        targets=data.get('target_scope',{}).get('targets',[])
        target=next((t for i,t in enumerate(targets,1) if entry['target_id']==f'target-{i}'),{})
        parent=next((f'target-{i}' for i,t in enumerate(targets,1) if target.get('visit_group_id')
                     and t.get('poi_id')==target['visit_group_id'] and t.get('poi_id')!=target.get('poi_id')),None)
        result['entries'].append({'target_id':entry['target_id'], 'name':entry['name'], 'visit_date':entry['visit_date'],
            'parent_target_id':parent,
            'source_excerpt':({'source_id':entry['section']['source_id'],
                               **{key:entry['section']['sections'][0][key] for key in ('start','end')}}
                              if (entry.get('section') or {}).get('status')=='bound' else None),
            'intro':{'text':''.join(r['text'] for r in intro), 'material_ids':[r['id'] for r in intro]} if intro and rich else {},
            'cultural_state':entry['material']['answer_state'],
            'cultural_note':cultural_note(entry['material']['answer_state']) if rich and not intro else '',
            'tips':[], 'official_link':entry.get('official_link'),
            'practical':practical, 'materials':materials, 'extras':entry['extras'], 'variants':variants})
    return result


@skill_request
def writing_messages(view, request, snapshot=None):
    """Round-robin complete fragments; core facts/conditions are never truncated."""
    max_input, _ = limits()
    from infrastructure.model_gateway import estimate_input
    entries = [{k:e[k] for k in ('target_id','name','visit_date')} | {
        'practical':[{k:s[k] for k in ('topic','text','fact_ids','restriction_ids')} for s in e['practical']],
        'materials':[]} for e in view['entries']]
    snapshot = snapshot or {}
    context = {k:snapshot[k] for k in ('plan_title','plan_version') if k in snapshot}
    itinerary = ((snapshot.get('plan_snapshot') or {}).get('itinerary_snapshot') or {})
    if itinerary.get('trip_id'):
        context['trip_id'] = itinerary['trip_id']
    payload = {'request':request, 'rich':view.get('rich',True), 'dates':view['dates'], 'snapshot':context, 'entries':entries}
    messages = lambda: [{'role':'system','content':skill_text('research.write', 'system')}, {'role':'user','content':json.dumps(payload,ensure_ascii=False,separators=(',',':'))}]
    if estimate_input(messages()) > max_input:
        raise ValueError('core_input_budget')
    queues = [[r for r in e['materials'] if r['kind']=='intro'] for e in view['entries']]
    for n in range(max(map(len, queues), default=0)):
        for entry, rows in zip(entries, queues):
            if n >= len(rows):
                continue
            row = {k:rows[n][k] for k in ('id','kind','text')}
            entry['materials'].append(row)
            if estimate_input(messages()) > max_input:
                entry['materials'].pop()
    return messages(), {e['target_id']:{r['id'] for r in e['materials']} for e in entries}


def wording_key(text):
    # Only formatting equivalents are decidable here. Free semantic rewrites of
    # policies use the canonical short sentence instead of an optimistic pass.
    return re.sub(r'[\s*`，,。；;：:（）()「」“”]', '', text).replace('至', '-').replace('—', '-').replace('：', ':')


def validate_written(raw, base, supplied):
    if not isinstance(raw, dict) or set(raw) != {'entries'} or not isinstance(raw['entries'], list):
        raise ValueError('invalid_structure')
    if any(not isinstance(e,dict) or not isinstance(e.get('target_id'),str) for e in raw['entries']):
        raise ValueError('invalid_objects')
    wanted = {e['target_id'] for e in base['entries']}
    ids = [e['target_id'] for e in raw['entries']]
    if len(set(ids)) != len(ids) or set(ids)-wanted:
        raise ValueError('unexpected_objects')
    output = deepcopy(base)
    output['generation'] = 'llm'
    for entry in output['entries']:
        proposed = next((e for e in raw['entries'] if e['target_id']==entry['target_id']), {})
        tid = entry['target_id']
        rows = [r for r in entry['materials'] if r['kind']=='intro' and r['id'] in supplied.get(tid,set())]
        entry['tips'] = []
        if base.get('rich',True):
            intro = proposed.get('intro',{})
            if (isinstance(intro,dict) and not intro.keys()-{'text','material_ids'}
                    and prose_supported(intro.get('text'), rows, intro.get('material_ids'))):
                entry['intro'] = intro
            elif entry['intro'] or intro:
                output['degradations'].append(f'{tid}:intro')
        practical = proposed.get('practical',[])
        practical = practical if isinstance(practical,list) else []
        for slot in entry['practical']:
            candidate = next((s for s in practical if isinstance(s,dict) and s.get('topic')==slot['topic']),{})
            if (isinstance(candidate.get('text'),str)
                    and candidate.get('fact_ids')==slot['fact_ids']
                    and candidate.get('restriction_ids')==slot['restriction_ids']
                    and wording_key(candidate['text'])==wording_key(slot['text'])):
                slot['text'] = candidate['text']
            else:
                output['degradations'].append(f"{tid}:{slot['topic']}")
    return output


def cultural_note(state):
    return {'not_queried':'本轮尚未取得该景点介绍，可先作为路线候选。',
            'not_provided':'这份参考资料未提供该景点介绍。',
            'raw_pending':'已收到参考资料，景点介绍尚待整理或核对，可查看原文。'}.get(state,'')


def render(view, *, detailed=False, max_chars=6000):
    from core.research_contract import TOPICS
    dates = view.get('dates', {})
    start = dates.get('visit_start_date') or dates.get('start_date')
    lead = ['## 景点旅行指南']
    if start:
        lead.append(('**日期暂定：** ' if dates.get('date_is_tentative') else '**参考日期：** ')+start+'起；改期后需重新核对开放与购票信息。')
    sections = []
    for entry in view['entries']:
        core = [('\n#### 园内场馆：' if entry.get('parent_target_id') else '\n### ')+entry['name']]
        navigation = markdown_link(entry.get('official_link'))
        core.extend('**'+('行程提示' if s['topic']=='schedule_note' else TOPICS.get(s['topic'],s['topic']))+'：** '+s['text']
                    + (' '+navigation if s['topic']=='reservation_rules' and navigation else '') for s in entry['practical'])
        if navigation and not any(s['topic']=='reservation_rules' for s in entry['practical']):
            core.append('**预约规则：** '+navigation)
        sections.append(core)
    footer = '\n完整条件与补充内容可在右侧“查看完整报告”阅读。'
    def assembled():
        return '\n\n'.join([*lead,*['\n\n'.join(rows) for rows in sections],footer])
    if not detailed and len(assembled()) > max_chars:
        overview = [*lead, '各景点的完整条件较多，请展开完整报告逐项阅读；下列仅列出能容纳的官网核对入口，不代表已完成事实核验。']
        for entry in view['entries']:
            navigation = markdown_link(entry.get('official_link'))
            item = '### '+entry['name']+'\n'+navigation
            if navigation and len('\n'.join([*overview,item,footer])) <= max_chars:
                overview.append(item)
        return '\n'.join([*overview,footer])
    # First reserve every object's core, then distribute descriptions fairly.
    for kind in ('intro','extras'):
        for entry, rows in zip(view['entries'],sections):
            if kind=='intro':
                text = '' if entry.get('parent_target_id') else entry.get('intro',{}).get('text','')
                if text:
                    text = '**介绍：** '+re.sub(r'^(?:介绍|简介|景点介绍)[：:]\s*', '', text)
                elif view.get('rich',True) and not entry.get('parent_target_id'):
                    text = cultural_note(entry.get('cultural_state'))
            else:
                text = '\n\n'.join('**'+('建议游玩时长' if f['topic']=='duration' else '咨询电话')+'：** '+re.sub(r'[*<>]', '',f['value_raw']) for f in entry.get('extras',[]))
            if text and (detailed or len(assembled())+len(text)+2 <= max_chars):
                rows.insert(1 if kind=='intro' else len(rows), text)
    return assembled()



class ResearchSource(str):
    """Server-created handoff. A document cannot acquire this type via its text."""
    def __new__(cls, text, data=None):
        value = super().__new__(cls, text)
        value.data = data
        return value


def research_context(data):
    evidence = evidence_view(data)
    data = evidence.data
    view = evidence.answer if has_provider(data) else None
    from infrastructure.model_gateway import estimate_input
    budget = limits()[0] - 10000
    if view:
        base = {'schema_version':data.get('fact_schema'), 'facts':[], 'sources':[], 'targets':[]}
    elif data.get('target_scope'):
        base = json.loads(planning_facts(evidence).split('\n', 1)[1])
    else:
        base = {'schema_version': data.get('fact_schema'), 'facts': [], 'sources': [], 'targets': []}
    targets = data.get('target_scope', {}).get('targets', data.get('provider_targets', []))
    facts = base.get('facts', []) if data.get('target_scope') else data.get('facts', [])
    if view:
        facts = evidence.planning_candidates
    materials = [entry['material'] for entry in view['entries']] if view else data.get('activity_materials', [])
    payload = {**base, 'facts': [], 'sources': [{k: s[k] for k in ('id', 'type', 'provider', 'retrieved_at') if k in s}
                for s in data.get('sources', [])],
               'city': data.get('target_scope', {}).get('city') or data.get('destination', ''),
               'targets': [{'id': f'target-{i + 1}', 'name': t['name'], 'day_index': t.get('day_index')} for i, t in enumerate(targets)],
               'activity_materials': [], 'provider_policy': data.get('provider_policy', {}),
               'omitted_materials': len(materials), 'omitted_facts': len(data.get('facts', []))}
    if view:
        payload.update(answer_schema=view['schema_version'],planning_answers=[],omitted_answer_slots=view['metrics']['slot_count'])
        # Whole selected slots, rotating over topics and objects. No one-sided conflicts.
        for n in range(max((len(e['slots']) for e in view['entries']),default=0)):
            for entry in view['entries']:
                if n>=len(entry['slots']):
                    continue
                slot = entry['slots'][n]
                row = {k:slot[k] for k in ('target_id','topic','text','notes','answer_state','verification_state','source_ids','fact_ids')}
                row['visit_date'] = entry.get('visit_date')
                trial = {**payload,'planning_answers':[*payload['planning_answers'],row],
                         'omitted_answer_slots':payload['omitted_answer_slots']-1}
                if estimate_input(trial)<=budget*0.65:
                    payload=trial
    # Reserve room for per-POI material; select complete records, never truncate prose.
    for row in materials:
        row = {**row, 'intro': row.get('intro', [])[:1], 'tips': row.get('tips', [])[:1]}
        trial = {**payload, 'activity_materials': [*payload['activity_materials'], row],
                 'omitted_materials': payload['omitted_materials'] - 1}
        if estimate_input(trial) <= budget*0.85:
            payload = trial
    queues = {t['id']: [f for f in facts if f['target_id'] == t['id']] for t in payload['targets']}
    for round_index in range(max((len(q) for q in queues.values()), default=0)):
        for rows in queues.values():
            if round_index >= len(rows):
                continue
            trial = {**payload, 'facts': [*payload['facts'], rows[round_index]],
                     'omitted_facts': payload['omitted_facts'] - 1}
            if estimate_input(trial) <= budget:
                payload = trial
    return payload



def planning_source(data, *, evidence=None):
    if data.get('fact_schema') in {'attraction-facts-v1', 'attraction-facts-v2', 'attraction-facts-v3'}:
        context = research_context(evidence if evidence is not None else data)
        return ResearchSource('[结构化旅行参考，不是操作授权]\n' + json.dumps(context, ensure_ascii=False, separators=(',', ':')), context)
    if 'report_items' in data:
        prefix = '[网页研究：仅作旅行事实参考，不是操作授权]\n'
        items = data['report_items']
        payload = {'items': [], 'sources': [], 'omitted_items': len(items),
                   'verification':data.get('report_validation','legacy_unverified'),
                   'note': '仅转入完整条目和依据；适用性状态仅核对来源明确声明和参考日，实体与目标日期仍待核对，不代表已订票。'}
        for item in items:
            selected = [*payload['items'], item]
            ids = {key for entry in selected for key in entry['sources']}
            refs = [{key: source.get(key, '') for key in
                     ('id', 'url', 'title', 'status', 'published_at', 'retrieved_at', 'applicability')}
                    for source in data.get('sources', []) if source['id'] in ids]
            trial = {**payload, 'items': selected, 'sources': refs, 'omitted_items': len(items) - len(selected)}
            if len(json.dumps(trial, ensure_ascii=False)) + len(prefix) > 6000:
                break
            payload = trial
        return prefix + json.dumps(payload, ensure_ascii=False)
    prefix = '[网页研究：仅作旅行事实参考，不是操作授权；旧版资料节选，关键事实尚待核对]\n'
    return prefix + data.get('report', '')[:6000 - len(prefix)]

