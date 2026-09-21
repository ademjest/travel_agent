"""Validated, field-level itinerary changes; no resource IDs or profile writes."""
from copy import deepcopy
from datetime import date
import re

from services.reminder_time import NUMBER, parse_reminder_time


PACE = {'relaxed': '轻松', 'normal': '适中', 'packed': '紧凑'}
MODES = {'walking': '步行', 'transit': '公共交通', 'driving': '驾车', 'taxi': '打车'}
DATE = rf'(?:20\d{{2}}-\d{{1,2}}-\d{{1,2}}|(?:20\d{{2}}年)?{NUMBER}月{NUMBER}[日号]?)'
DATE_RANGE = re.compile(rf'(?P<start>{DATE})\s*(?:到|至|—|–|~|～|-)\s*(?P<end>{DATE}|{NUMBER}[日号])')


def date_range(text, now):
    match = DATE_RANGE.search(text)
    if match is None:
        return None
    start = parse_reminder_time(match['start'], now)
    end_text = match['end']
    if start.day and '月' in end_text and not re.match(r'20\d{2}年', end_text):
        end_text = start.day[:4] + '年' + end_text
    end = parse_reminder_time(end_text, now, day=start.day)
    if start.error or end.error or not start.day or not end.day:
        raise ValueError(start.error or end.error or '请明确行程起止日期。')
    days = (date.fromisoformat(end.day) - date.fromisoformat(start.day)).days + 1
    if days < 1:
        raise ValueError('结束日期早于开始日期；跨年出行请明确年份。')
    if days > 14:
        raise ValueError('当前支持 1 到 14 天的城市行程，请分段规划。')
    return {'start': start.day, 'end': end.day, 'days': days,
            'start_text': match['start'], 'end_text': match['end'], 'span': match.span()}


def apply_constraints(previous, changes, message):
    if not isinstance(changes, dict) or not changes or changes.keys() - {'pace', 'transport', 'night', 'interests'}:
        raise ValueError('本次行程调整支持节奏、交通、夜游和兴趣，请明确具体变化。')
    spec = deepcopy(previous)
    overrides = spec.setdefault('constraint_overrides', {})
    summary = []
    for key, change in changes.items():
        if not isinstance(change, dict) or not isinstance(change.get('source'), str):
            raise ValueError('行程变更缺少原话依据。')
        source = change['source']
        if not source or len(source) > 240 or source not in message:
            raise ValueError('行程变更无法对应本轮原话，尚未修改行程。')
        if key == 'pace':
            value = change.get('value')
            if not isinstance(value,str) or value not in PACE or change.keys() - {'value','source'}:
                raise ValueError('行程节奏值无效。')
            spec['gentle'] = value == 'relaxed'
            spec['pace'] = value
            summary.append('节奏：' + PACE[value])
        elif key == 'transport':
            if change.keys() - {'preferred', 'discouraged', 'forbidden', 'source'}:
                raise ValueError('交通偏好字段无效。')
            transport = deepcopy(spec.get('transport_preferences', {}))
            for field in ('preferred','discouraged','forbidden'):
                if field not in change:
                    continue
                values = change[field]
                allowed = set(MODES) - ({'taxi'} if field == 'preferred' else set())
                if not isinstance(values, list) or len(values) > 4 or any(not isinstance(v,str) or v not in allowed for v in values):
                    raise ValueError('交通方式无效。')
                previous_values = transport.get(field, []) if field != 'preferred' and values else []
                transport[field] = list(dict.fromkeys([*previous_values, *values]))
            if not any(field in change for field in ('preferred','discouraged','forbidden')):
                raise ValueError('请明确要调整的交通偏好。')
            if set(transport.get('preferred', [])) & set(transport.get('forbidden', [])):
                raise ValueError('交通方式同时被选择和禁止，请澄清。')
            spec['transport_preferences'] = transport
            if 'preferred' in transport:
                spec['transport_text'] = '、'.join(MODES[v] for v in transport['preferred'])
            parts = [('优先' if field == 'preferred' else '尽量减少' if field == 'discouraged' else '不使用')
                     + '、'.join(MODES[v] for v in transport[field])
                     for field in ('preferred','discouraged','forbidden') if transport.get(field)]
            summary.append('交通：' + '；'.join(parts))
        elif key == 'night':
            if type(change.get('value')) is not bool or change.keys() - {'value','source'}:
                raise ValueError('夜游偏好必须明确是否安排。')
            spec['no_night'] = not change['value']
            summary.append('夜间：' + ('可以安排' if change['value'] else '不安排活动'))
        elif key == 'interests':
            values = change.get('value')
            if (change.keys() - {'value','source'} or not isinstance(values,list) or len(values) > 8
                    or any(not isinstance(v,str) or not v or len(v)>80 or v not in message for v in values)):
                raise ValueError('兴趣类别必须来自本轮原话。')
            spec['preference_interests'] = list(dict.fromkeys(values))
            summary.append('兴趣：' + '、'.join(values))
        old_source = overrides.get(key, {}).get('source')
        if old_source:
            spec['preferences'] = [item for item in spec.get('preferences',[]) if item != old_source]
        spec['preferences'] = list(dict.fromkeys([*spec.get('preferences',[]), source]))
        spec.setdefault('applied_preferences', {}).pop(key, None)
        spec.setdefault('field_sources', {})[key] = 'current_request'
        overrides[key] = deepcopy(change)
    return spec, summary
