"""Conservative labelled source metadata; no inference from crawl time or titles."""
from datetime import date, datetime
import re
from zoneinfo import ZoneInfo


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
