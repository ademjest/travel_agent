"""Applicability and explicit exceptions are separate from source authority."""
from datetime import date
import re


def statement_kind(text):
    if re.search(r'必须|不得|不能|停止入|须.{0,6}预约|需.{0,6}预约|闭馆|开放时间|营业时间|票价', text):
        return 'rule'
    if re.search(r'建议|最好|适合|推荐|可以|可选', text):
        return 'advice'
    if re.match(r'\s*(?:\*\*)?\d{1,2}[:：]\d{2}\s*(?:出发|午餐|晚餐)', text):
        return 'selected_action'
    return 'reference'


def split_statements(text):
    """Keep the suggestion and a genuine late-entry restriction independently."""
    return [{'text': m[0], 'kind': statement_kind(m[0]), 'start': m.start(), 'end': m.end()}
            for m in re.finditer(r'[^，,；;。]+[，,；;。]?', text)]


def temporal_rule(text, visit_date):
    rule = {'kind': 'temporary' if re.search(r'临时|调整|提前执行|特殊开放|特开|特设', text) else 'regular',
            'effective_from': None, 'effective_to': None, 'applicability': 'unknown'}
    dated = re.search(r'(20\d{2})年(\d{1,2})月(\d{1,2})日', text)
    try:
        target = date.fromisoformat(visit_date or '')
    except (ValueError, TypeError):
        target = None
    try:
        if dated:
            start = date(*map(int, dated.groups()))
            rule['effective_from'] = start.isoformat()
            rest = text[dated.end():]
            end = re.match(r'\s*(?:至|到|—|-)\s*(?:(20\d{2})年)?(\d{1,2})月(\d{1,2})日', rest)
            if end:
                finish = date(int(end[1] or start.year), int(end[2]), int(end[3]))
                rule['effective_to'] = finish.isoformat()
                if target:
                    rule['applicability'] = 'matching' if start <= target <= finish else 'outside'
            elif re.match(r'\s*(?:起|开始|生效)', rest):
                if target:
                    rule['applicability'] = 'matching' if target >= start else 'outside'
            elif rule['kind'] == 'temporary':
                rule['effective_to'] = start.isoformat()
                if target:
                    rule['applicability'] = 'matching' if target == start else 'outside'
        elif target and re.search(r'每年\d{1,2}月\d{1,2}日至', text):
            from core.attraction_rules import date_scope
            relevance, _, recurrence = date_scope(text, visit_date, {})
            rule.update(applicability=relevance, recurrence=recurrence)
        elif target and re.search(r'每周[一二三四五六日天].{0,5}闭馆', text) and not re.search(r'节假日|除外', text):
            weekday = re.search(r'每周([一二三四五六日天])', text)[1]
            rule['applicability'] = 'matching' if target.weekday() == '一二三四五六日'.index('日' if weekday == '天' else weekday) else 'outside'
        elif target and re.search(r'全年|每天|每日|常规', text) and not re.search(r'\d{1,2}月|节假日|除外', text):
            rule['applicability'] = 'matching'
        # Unknown years and unbounded seasonal windows are intentionally unresolved.
    except (ValueError, TypeError):
        pass
    rule['explicit_override'] = rule['kind'] == 'temporary' and not re.search(r'可能|或许|拟|待定|预计|建议', text)
    return rule


def resolve_rules(facts):
    """Do not let provider prose supersede official/user rules, or advice compete."""
    from core.attraction_facts import fact_signature
    groups = {}
    for fact in facts:
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
