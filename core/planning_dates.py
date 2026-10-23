"""Server-owned reference dates for preliminary trip plans."""
from datetime import date, timedelta
import re

from services.reminder_time import BEIJING


def explicitly_undated(text):
    return bool(re.search(r'(?:不要|不用|不必)(?:指定|安排|填写|填|写|设置|使用)?(?:具体|日历)?日期|'
                          r'只按第几天|不要按明天|别按明天', text))


def confirm_planning_date(spec):
    spec['date_is_tentative'] = False
    spec['defer_start_date'] = False
    for key in ('date_reference_day', 'date_preference_text'):
        spec.pop(key, None)
    for field in ('start_date', 'end_date'):
        if spec.get('field_sources', {}).get(field) == 'system_reference':
            spec['field_sources'][field] = 'user' if field == 'start_date' else 'derived_from_user_start'


def apply_reference_date(spec, now, *, previous=None, text=''):
    """Fill an absent calendar date; preserve an existing reference across follow-ups."""
    previous = previous or {}
    if (spec.get('start_date') and not spec.get('date_is_tentative')
            and spec.get('start_date_text') and spec['start_date_text'] in text):
        return
    if explicitly_undated(text) or (previous.get('defer_start_date') and not spec.get('start_date')):
        spec.update(start_date='', end_date='', start_date_text='', end_date_text='',
                    date_is_tentative=False, defer_start_date=True)
        spec.pop('date_reference_day', None)
        return
    if spec.get('start_date'):
        return
    if previous.get('start_date') and previous.get('date_is_tentative'):
        start, reference_day = previous['start_date'], previous['date_reference_day']
    else:
        instant = now.replace(tzinfo=BEIJING) if now.tzinfo is None else now.astimezone(BEIJING)
        reference_day = instant.date().isoformat()
        start = (instant.date() + timedelta(days=1)).isoformat()
    spec.update(start_date=start, date_is_tentative=True, defer_start_date=False,
                date_reference_day=reference_day, date_preference_text=spec.get('start_date_text', ''))
    if spec.get('day_count'):
        spec['end_date'] = (date.fromisoformat(start) + timedelta(days=spec['day_count'] - 1)).isoformat()
    spec.setdefault('field_sources', {}).update(start_date='system_reference', end_date='system_reference')


def reference_date_note(spec):
    if not spec.get('date_is_tentative'):
        return ''
    note = f"日期暂定：先按提问当天（{spec['date_reference_day']}，北京时间）的次日 {spec['start_date']} 开始安排，供初步参考。"
    preference = spec.get('date_preference_text')
    if preference:
        note += f' 你提到的“{preference}”仍保留，暂定日期不代表已确定出发。'
    return note + ' 确定日期后告诉我，我会以你指定的日期为准。'
