"""Conservative rule scope and finite equivalence, independent of source trust."""
from datetime import date
import re


AUDIENCES = [('occupation', r'公共交通司乘人员|环卫工人|医务工作者|医护人员|教师'),
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
    try:
        target = date.fromisoformat(target_date or '')
    except ValueError:
        return 'unknown', 'target_date_missing', ''
    annuals = list(re.finditer(r'每年(\d{1,2})月(\d{1,2})日至(次年|来年)?(\d{1,2})月(\d{1,2})日', text))
    if annuals and (len(annuals) != 1 or len(re.findall(r'\d{1,2}月\d{1,2}日', text)) != 2
                    or re.search(r'除外|节假日|另行|调整', text)):
        return 'unresolved', 'multiple_windows_or_exception', ''
    if annuals:
        annual = annuals[0]
        try:
            start = date(target.year, int(annual[1]), int(annual[2]))
            end = date(target.year + bool(annual[3]), int(annual[4]), int(annual[5]))
            if end < start:
                return 'unknown', 'ambiguous_annual_window', ''
            if annual[3] and target < start:
                start = start.replace(year=start.year - 1)
                end = end.replace(year=end.year - 1)
        except ValueError:
            return 'unknown', 'invalid_annual_window', ''
        window = annual[0]
        return ('matching' if start <= target <= end else 'outside'), 'explicit_annual_window', window
    # A dated one-day event is not a publication date or an indefinite "since" rule.
    event = re.match(r'\s*(20\d{2})年(\d{1,2})月(\d{1,2})日(?:[（(]周.[）)])?[，,：:\s]+', text)
    if event and re.search(r'免费开放|免费参观|主题免费|活动当日', text):
        if not re.search(r'(?:至|到|—|~|～)\s*(?:20\d{2}年)?\d{1,2}月\d{1,2}日', text):
            try:
                day = date(*map(int, event.groups()))
            except ValueError:
                return 'unknown', 'invalid_event_date', ''
            return ('matching' if target == day else 'outside'), 'explicit_single_day_event', day.isoformat()
    matched = applicability.get('date_status') in {'applicable', 'within_declared_window'}
    if matched and applicability.get('basis') != 'explicit_labelled_source_text':
        return 'matching', applicability.get('basis', 'existing_date_evidence'), ''
    if re.search(r'国庆|暑期|寒假|冰季|\d{1,2}月\d{1,2}日', text):
        return 'unresolved', 'unresolved_event_or_window', ''
    if matched:
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
    audiences = [(kind, re.findall(pattern, text)) for kind, pattern in AUDIENCES if re.search(pattern, text)]
    if not audiences and context:
        audiences = [(kind, re.findall(pattern, context)) for kind, pattern in AUDIENCES if re.search(pattern, context)]
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
