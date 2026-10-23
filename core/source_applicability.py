"""Conservative labelled source metadata; no inference from crawl time or titles."""
from datetime import date, datetime
import re
from zoneinfo import ZoneInfo


def rule_applicability(source, quote, visit_date=None):
    """Finite rule windows, independent of source identity and crawl time."""
    result = {'visit_date': visit_date.isoformat() if visit_date else None, 'date_status': 'unknown',
              'effective_from': None, 'effective_until': None, 'basis': 'not_provided'}
    if not visit_date:
        return result
    matches = list(re.finditer(r'(20\d{2})[年/-](\d{1,2})[月/-](\d{1,2})日?', quote))
    days = []
    try:
        days = [date(*map(int, match.groups())) for match in matches]
    except ValueError:
        return result
    if len(days) == 1:
        end_match = re.search(r'(?:至|到)\s*(?:(次年))?(\d{1,2})月(\d{1,2})日', quote[matches[0].end():])
        if end_match:
            try:
                end = date(days[0].year + bool(end_match[1]), int(end_match[2]), int(end_match[3]))
            except ValueError:
                return result
            if end < days[0]:
                return result
            return {**result, 'effective_from': days[0].isoformat(), 'effective_until': end.isoformat(),
                    'basis': 'explicit_date_range', 'date_status': 'applicable' if days[0] <= visit_date <= end else 'outside_declared_window'}
    if len(days) == 2 and re.search(r'至|到|—|~|～', quote[matches[0].end():matches[1].start()]):
        start, end = days
        if start > end:
            return {**result, 'date_status': 'conflicting_range'}
        return {**result, 'effective_from': start.isoformat(), 'effective_until': end.isoformat(),
                'basis': 'explicit_date_range', 'date_status': 'applicable' if start <= visit_date <= end else 'outside_declared_window'}
    if len(days) == 1:
        if re.search(r'临时闭馆|暂停开放', quote):
            return {**result, 'effective_from': days[0].isoformat(), 'effective_until': days[0].isoformat(),
                    'basis': 'explicit_date_range', 'date_status': 'applicable' if visit_date == days[0] else 'outside_declared_window'}
        if re.search(r'(?:自|从).{0,25}(?:起|生效|施行)', quote):
            return {**result, 'effective_from': days[0].isoformat(), 'basis': 'explicit_start',
                    'date_status': 'applicable' if visit_date >= days[0] else 'not_yet_effective'}
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
    if re.search(r'即日起|暑期|寒假|旺季|淡季|节假日|另行通知|\d{1,2}月\d{1,2}日', quote):
        return {**result, 'basis': 'unresolved_date_or_exception'}
    if re.search(r'长期有效|全年|常年|每周[一二三四五六日天]|每日|每天', quote):
        return {**result, 'date_status': 'applicable', 'basis': 'explicit_regular_rule'}
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
    state = ('conflicting_range' if start and end and start > end else
             'not_yet_effective' if start and reference < start else
             'expired' if end and reference > end else
             'within_declared_window' if start and end else 'unknown')
    return {'version':'source-applicability-v1','reference_date':reference.isoformat(),
            'declared_city':labelled('适用城市|所在城市|城市')[:40] or None,
            'effective_from':start.isoformat() if start else None,'effective_until':end.isoformat() if end else None,
            'date_status':state,'entity_match':'not_verified',
            'basis':'explicit_labelled_source_text' if start or end else 'not_provided',
            'note':'来源声明与参考日检查，不证明官方身份、目标出游日有效性或同名实体匹配。'}
