"""Validated, field-level itinerary changes; no resource IDs or profile writes."""
from copy import deepcopy
from datetime import date
import re

from services.reminder_time import NUMBER, parse_reminder_time


PACE = {'relaxed': '轻松', 'normal': '适中', 'packed': '紧凑'}
MODES = {'walking': '步行', 'transit': '公共交通', 'driving': '驾车', 'taxi': '打车'}
BUDGET_SCOPES = {'accommodation': '住宿', 'food': '餐饮', 'transport': '交通', 'trip': '全程'}
HOTEL_DEFAULT_RATING = 4.7
DINING_DEFAULT_RATING = 4.5
MEALS = {'早餐', '午餐', '晚餐', '餐饮'}
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


def _text_list(change, field, source, *, limit=12):
    values = change.get(field, [])
    if (not isinstance(values, list) or len(values) > limit or
            any(not isinstance(value, str) or not value or len(value) > 80 or value not in source for value in values)):
        raise ValueError('住宿或餐饮偏好缺少可核对的原话依据。')
    return list(dict.fromkeys(values))


def _rating_preferences(change, current, default_rating):
    result = deepcopy(current)
    if 'quality_requested' in change:
        if type(change['quality_requested']) is not bool:
            raise ValueError('高评分要求格式无效。')
        result['quality_requested'] = change['quality_requested']
    if 'minimum_rating' in change:
        value = change['minimum_rating']
        if type(value) not in {int, float} or not 0 <= value <= 5:
            raise ValueError('评分要求必须在 0 到 5 分之间。')
        result['minimum_rating'] = float(value)
        result['rating_origin'] = 'user'
    elif change.get('quality_requested') and 'minimum_rating' not in result:
        result['minimum_rating'] = default_rating
        result['rating_origin'] = 'default_high_quality'
    if 'rating_mode' in change:
        if change['rating_mode'] not in {'required', 'preferred'}:
            raise ValueError('评分要求强度无效。')
        result['rating_mode'] = change['rating_mode']
    elif result.get('minimum_rating') is not None:
        result.setdefault('rating_mode', 'required')
    return result


def _apply_budget(spec, change, message, summary):
    if not isinstance(change, dict) or not isinstance(change.get('source'), str):
        raise ValueError('预算变更缺少原话依据。')
    value, scope, source = change.get('value'), change.get('scope'), change['source']
    unit = change.get('unit_text', '')
    if (change.keys() - {'value','source','scope','unit_text'} or not isinstance(value,str)
            or not value or len(value)>150 or value not in message or not source or source not in message
            or not isinstance(scope,str) or scope not in BUDGET_SCOPES or not isinstance(unit,str)
            or len(unit)>120 or (unit and unit not in message)):
        raise ValueError('预算或计价单位无法对应你的原话，尚未修改行程。')
    budget = deepcopy(spec.setdefault('budgets', {}).get(scope, {}))
    budget.update(value=value, source=source)
    if 'unit_text' in change:
        budget['unit_text'] = unit
    spec['budgets'][scope] = budget
    summary.append(BUDGET_SCOPES[scope] + '预算：' + value + ('；'+budget['unit_text'] if budget.get('unit_text') else ''))
    return source


def apply_constraints(previous, changes, message):
    if not isinstance(changes, dict) or not changes:
        raise ValueError('模型尚未解析出有效的行程变化，原行程未修改，请重新说明希望调整的内容。')
    if changes.keys() - {'pace', 'transport', 'night', 'interests', 'accommodation', 'dining',
                         'budget', 'budgets', 'requirements'}:
        raise ValueError('模型返回了无法执行的修改字段，原行程未修改，请重新描述这次调整。')
    spec = deepcopy(previous)
    overrides = spec.setdefault('constraint_overrides', {})
    summary = []
    for key, change in changes.items():
        if key == 'budgets':
            if not isinstance(change, list) or not 1 <= len(change) <= 4:
                raise ValueError('多项预算格式无效。')
            sources = [_apply_budget(spec, item, message, summary) for item in change]
            spec['preferences'] = list(dict.fromkeys([*spec.get('preferences', []), *sources]))
            spec.setdefault('field_sources', {})['budgets'] = 'current_request'
            overrides['budgets'] = deepcopy(change)
            continue
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
        elif key in {'accommodation', 'dining', 'requirements'}:
            value = change.get('value')
            if key == 'accommodation':
                allowed = {'value','source','requested','replace','preferred_brands','brand_mode','chain_mode',
                           'minimum_rating','rating_mode','quality_requested','near_itinerary','same_hotel_for_trip'}
            elif key == 'dining':
                allowed = {'value','source','requested','replace','preferred_brands','brand_mode','cuisines',
                           'keywords','cuisine_mode','minimum_rating','rating_mode','quality_requested',
                           'near_itinerary','scopes','avoid','allergies'}
            else:
                allowed = {'value','source'}
            if (change.keys() - allowed or not isinstance(value,str) or not value or len(value)>240
                    or value not in message):
                raise ValueError('补充要求需要对应你的原话，尚未修改行程。')
            if key in {'accommodation', 'dining'}:
                requested = change.get('requested', True)
                if type(requested) is not bool or type(change.get('replace',False)) is not bool:
                    raise ValueError('住宿或餐饮需求尚未明确，未修改行程。')
                prefix = 'hotel' if key == 'accommodation' else 'dining'
                requested_key = 'hotel_requested' if key == 'accommodation' else 'restaurant_requested'
                text_key = 'accommodation_text' if key == 'accommodation' else 'dining_text'
                spec[requested_key] = requested
                old = spec.get(text_key,'') if not change.get('replace') else ''
                spec[text_key] = ('；'.join(dict.fromkeys([part for part in (old,value) if part])) if requested else '')
                current = {} if change.get('replace') or not requested else spec.get(prefix + '_preferences', {})
                preferences = _rating_preferences(change, current,
                    HOTEL_DEFAULT_RATING if key == 'accommodation' else DINING_DEFAULT_RATING)
                for field in ('preferred_brands', 'cuisines', 'keywords', 'avoid', 'allergies'):
                    if field in change:
                        values = _text_list(change, field, source)
                        preferences[field] = list(dict.fromkeys([*preferences.get(field, []), *values]))
                for field, allowed_values in (
                    ('brand_mode', {'required','preferred'}), ('chain_mode', {'required','preferred','none'}),
                    ('cuisine_mode', {'required','preferred'})):
                    if field in change:
                        if change[field] not in allowed_values:
                            raise ValueError('住宿或餐饮约束强度无效。')
                        preferences[field] = change[field]
                for field in ('near_itinerary','same_hotel_for_trip'):
                    if field in change:
                        if type(change[field]) is not bool:
                            raise ValueError('住宿或餐饮位置要求格式无效。')
                        preferences[field] = change[field]
                if 'scopes' in change:
                    scopes = change['scopes']
                    if (not isinstance(scopes,list) or len(scopes)>14 or any(not isinstance(item,dict)
                            or item.keys()-{'day_index','meal'} or type(item.get('day_index')) is not int
                            or item['day_index']<1 or item.get('meal') not in MEALS for item in scopes)):
                        raise ValueError('餐饮日期或餐次范围无效。')
                    preferences['scopes'] = list({(item['day_index'],item['meal']):item for item in scopes}.values())
                if requested:
                    if key == 'accommodation':
                        preferences.setdefault('near_itinerary', True)
                        preferences.setdefault('same_hotel_for_trip', True)
                    spec[prefix + '_preferences'] = preferences
                else:
                    spec.pop(prefix + '_preferences', None)
                summary.append(('住宿：' if key == 'accommodation' else '餐饮：') + value)
            else:
                summary.append('补充要求：' + value)
        elif key == 'budget':
            _apply_budget(spec, change, message, summary)
        old_source = overrides.get(key, {}).get('source')
        if old_source and key != 'requirements' and not (key in {'accommodation','dining'} and change.get('requested',True) and not change.get('replace')):
            spec['preferences'] = [item for item in spec.get('preferences',[]) if item != old_source]
        spec['preferences'] = list(dict.fromkeys([*spec.get('preferences',[]), source]))
        spec.setdefault('applied_preferences', {}).pop(key, None)
        spec.setdefault('field_sources', {})[key] = 'current_request'
        overrides[key] = deepcopy(change)
    return spec, summary
