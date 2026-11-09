"""Pure, shared provenance contract for incremental intercity travel requirements."""
from copy import deepcopy

from core.rail_contract import SEAT_CLASSES, SEAT_REQUIREMENTS, PRICE_PREFERENCES
from core.time_input import parse_time_window


class IntercityContractError(ValueError):
    def __init__(self, code, path, allowed=None):
        self.code, self.path = code, path
        labels = {'date_text': '乘车日期', 'departure_window': '出发时段', 'arrival_window': '到达时段',
                  'arrival_before': '最晚到达时间', 'origin': '出发地', 'destination': '目的地',
                  'seat_class': '席别', 'seat_requirement': '席别要求', 'train_preference': '车种', 'mode': '出行方式'}
        direction = '去程' if '.outbound' in path else '返程' if '.inbound' in path else '城际交通'
        self.detail = direction + '的' + labels.get(path.rsplit('.', 1)[-1], '要求') + '未能正确对应你的原话'
        options = ('允许值：' + ', '.join(sorted(allowed)) + '。') if allowed is not None else ''
        super().__init__(f'{path}: {code}。{options}日期、地点、时段须保留连续原文；枚举须使用合同允许值。')


def _field(entry, source, path, allowed=None):
    if not isinstance(entry, dict) or set(entry) != {'value', 'source'}:
        raise IntercityContractError('invalid_evidence', path)
    raw, quote = entry['value'], entry['source']
    if not isinstance(raw, str) or len(raw) > 150:
        raise IntercityContractError('invalid_value', path)
    if not isinstance(quote, str) or not quote or quote not in source:
        raise IntercityContractError('source_not_in_message', path)
    if allowed is not None:
        if raw not in allowed:raise IntercityContractError('invalid_enum', path, allowed)
    elif raw not in quote:
        raise IntercityContractError('value_not_in_source', path)
    return raw, quote


def intercity_request(value, previous, source):
    """Merge evidenced changes; preserve literal time text for server normalization."""
    if value is None:return deepcopy(previous)
    fields = {'origin', 'origin_scope', 'scope', 'day_count_basis', 'outbound', 'inbound'}
    enums = {'origin_scope': {'city', 'station', 'address'}, 'scope': {'round_trip', 'one_way', 'none'},
             'day_count_basis': {'travel_included', 'destination_days'},
             'mode': {'rail', 'driving', 'flight', 'owned_ticket'},
             'train_preference': {'high_speed', 'high_speed_only', 'd_train', 'conventional', 'all'},
             'seat_class': set(SEAT_CLASSES), 'seat_requirement': SEAT_REQUIREMENTS,
             'price_preference': PRICE_PREFERENCES, 'time_reference': {'departure', 'arrival', 'ambiguous'},
             'destination_scope': {'city', 'station'}}
    if not isinstance(value, dict) or value.keys() - fields:
        raise IntercityContractError('invalid_fields', 'intercity')
    result = deepcopy(previous or {})
    for key, item in value.items():
        path = 'intercity.' + key
        if key in {'outbound', 'inbound'}:
            allowed = {'date_text', 'departure_window', 'arrival_before', 'arrival_window', 'destination',
                       'destination_scope', 'mode', 'train_preference', 'preferred_train', 'ticket_departure',
                       'ticket_arrival', 'ticket_code', 'seat_class', 'seat_requirement', 'price_preference', 'time_reference'}
            if not isinstance(item, dict) or item.keys() - allowed:
                raise IntercityContractError('invalid_fields', path)
            part = result.setdefault(key, {})
            if part.get('time_reference') == 'ambiguous':
                for name in ('departure_window', 'arrival_window', 'arrival_before'):
                    if name not in item and name not in part.get('field_sources', {}):part.pop(name, None)
            for name, entry in item.items():
                raw, quote = _field(entry, source, path + '.' + name, enums.get(name))
                if name in {'departure_window', 'arrival_window', 'arrival_before'} and raw == '':
                    part.pop(name, None)
                    part.setdefault('field_sources', {}).pop(name, None)
                    continue
                part[name] = raw; part.setdefault('field_sources', {})[name] = quote
                if name == 'date_text':
                    part.pop('resolved_date', None); part.pop('date_reference_time', None)
                    try:window = parse_time_window(raw, dated=True)
                    except ValueError:window = raw
                    if window is not None and 'departure_window' not in item:
                        part.pop('departure_window', None)
                        if isinstance(window, str):part['departure_window'] = raw
            reference = item.get('time_reference', {}).get('value')
            if not reference:
                if set(item) & {'arrival_window', 'arrival_before'}:reference = 'arrival'
                elif 'departure_window' in item:reference = 'departure'
                if reference:
                    name = next(name for name in ('arrival_window', 'arrival_before', 'departure_window') if name in item)
                    part.setdefault('field_sources', {})['time_reference'] = item[name]['source']
            if reference:part['time_reference'] = reference
            # The reference interprets otherwise unlabelled time text. Explicit
            # departure and arrival constraints are independent, including on edits.
        else:
            raw, quote = _field(item, source, path, enums.get(key))
            result[key] = raw; result.setdefault('field_sources', {})[key] = quote
            if key == 'origin':result.pop('origin_resolution', None)
    result['schema_version'] = 1
    return result
