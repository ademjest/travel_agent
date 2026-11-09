"""Typed trip changes, provenance, diffs and proposal parsing; no persistence."""
from infrastructure.planning_repository import CAPTURE_VERSION
from core.intercity_contract import intercity_request
from copy import deepcopy
from datetime import date, timedelta
import re
from core.time_input import NUMBER, number, parse_reminder_time, parse_time_window, ReminderTime, RAIL_DEPARTURE_PERIODS, BEIJING
import json


PACE = {'relaxed': '轻松', 'normal': '适中', 'packed': '紧凑'}


MODES = {'walking': '步行', 'transit': '公共交通', 'driving': '驾车', 'taxi': '打车'}


BUDGET_SCOPES = {'accommodation': '住宿', 'food': '餐饮', 'transport': '交通', 'trip': '全程'}


def travel_preferences(values):
    """Remove whole workflow clauses, preserving travel constraints and their source.

    This compatibility guard is deliberately narrower than semantic extraction:
    不吃辣、不要去寺庙、保存体力 are travel requirements, not workflow commands.
    """
    prefix = r'(?:请)?(?:先|暂时|暂且|本次|这次)?(?:不要|不用|不必|别|不|只|仅)?'
    action = re.compile(prefix + r'(?:直接|自动|再)?(?:保存|确认|执行|修改|调整)'
                        r'(?:这份|当前|这版|该)?(?:草案|草稿|预览|方案|行程)?|'
                        + prefix + r'(?:给我|生成|提供|展示|查看)(?:一份|一个|这份|当前|这版)?(?:草案|草稿|预览)')
    result = []
    for value in values:
        clauses = [part.strip() for part in re.split(r'[，,；;。！!？?\n]', value) if part.strip()]
        kept = [part for part in clauses if not action.fullmatch(part)]
        if len(kept) == len(clauses):result.append(value)
        elif kept:result.append('；'.join(kept))
    return list(dict.fromkeys(result))


from core.budget_contract import BUDGET_UNITS, HOTEL_BUDGET_DEFAULTS, CURRENCY_NAMES, budget_bounds, hotel_comparison_unit

def budget_default_note(budget):
    inherited='（计价单位缺项沿用原餐饮预算）' if budget.get('unit_inherited') else ''
    if budget.get('comparison_unit')=='per_person_meal_cny':return '（按人民币每人每餐比较，参考人均不保证实际账单）'+inherited
    defaults = budget.get('unit_defaults', {})
    notes = ['默认币种：'+defaults['currency']] if defaults.get('currency') else []
    pricing = ''.join(defaults.get(key, '') for key in ('basis','period'))
    if pricing:notes.append('默认计价：'+pricing)
    return ('（'+'；'.join(notes)+'）' if notes else '')+inherited


DINING_DEFAULT_RATING = 4.5


MEALS = {'早餐', '午餐', '晚餐', '餐饮'}


DATE = rf'(?:20\d{{2}}-\d{{1,2}}-\d{{1,2}}|(?:20\d{{2}}年)?{NUMBER}月{NUMBER}[日号]?)'


PERIOD = '|'.join(sorted(RAIL_DEPARTURE_PERIODS,key=len,reverse=True))
CLOCK = rf'(?:\d{{1,2}}[:：]\d{{2}}|{NUMBER}[点时](?:半|{NUMBER}分?)?)'
DATE_TIME = rf'(?:\s*(?:(?:{PERIOD})(?:{CLOCK})?|{CLOCK}))?'
DATE_RANGE = re.compile(rf'(?P<start>{DATE}{DATE_TIME})\s*(?:到|至|—|–|~|～|-)\s*(?P<end>(?:{DATE}|{NUMBER}[日号]){DATE_TIME})')


def date_range(text, now, *, start_text='', end_text='', resolved_start='', resolved_end=''):
    match = DATE_RANGE.search(text)
    if match and not (start_text and end_text):
        start_text,end_text=match['start'],match['end']
        resolved_start=resolved_end=''
    if not start_text or not end_text:
        return None
    start = ReminderTime(resolved_start) if resolved_start else parse_reminder_time(start_text, now)
    original_end=end_text
    if start.day and '月' in end_text and not re.match(r'20\d{2}年', end_text):
        end_text = start.day[:4] + '年' + end_text
    end = ReminderTime(resolved_end) if resolved_end else parse_reminder_time(end_text, now, day=start.day)
    if start.error or end.error:
        raise ValueError(start.error or end.error or '请明确行程起止日期。')
    if not start.day or not end.day:return None
    days = (date.fromisoformat(end.day) - date.fromisoformat(start.day)).days + 1
    if days < 1:
        raise ValueError('结束日期早于开始日期；跨年出行请明确年份。')
    if days > 14:
        raise ValueError('当前支持 1 到 14 天的城市行程，请分段规划。')
    start_window=parse_time_window(start_text,dated=True);end_window=parse_time_window(original_end,dated=True)
    if days==1 and start_window and end_window and start_window[0]>=end_window[1]:
        raise ValueError('结束时间必须晚于开始时间，请核对起止时间。')
    return {'start': start.day, 'end': end.day, 'days': days,
            'start_text': start_text, 'end_text':original_end,'span':match.span() if match else None,
            'start_window':start_window,'end_window':end_window}


def duration_days(text):
    if not text:return 0
    matches=list(re.finditer(rf'(?<![月\d零〇一二两三四五六七八九十.．负\-])({NUMBER})[天日](?!半|后)',text))
    if len(matches)!=1:raise ValueError('已收到天数描述，但尚无唯一明确天数，请核对该描述。')
    return number(matches[0][1])


def apply_date_window(spec, interval):
    interval={**interval,'span':None}
    spec.update(start_date_text=interval['start_text'],end_date_text=interval['end_text'],
                start_date=interval['start'],end_date=interval['end'],day_count=interval['days'],planning_window=interval)
    sources=spec.setdefault('field_sources',{})
    sources.update(start_date='user',end_date='user',day_count='date_range')
    if not spec.get('duration_text') or sources.get('duration_text')=='date_range':
        spec['duration_text']=f"{interval['days']}天";sources['duration_text']='date_range'
    spec.pop('duration_adjustment',None)
    requested=spec.get('requested_day_count',0)
    if requested and (requested>interval['days'] or requested!=interval['days']
            and spec.get('intercity_request',{}).get('day_count_basis')!='destination_days'):
        spec['duration_adjustment']=f"原天数要求{requested}天；按明确起止时间安排{interval['days']}个自然日，首末日按可用时间安排。"
    confirm_planning_date(spec)


def _city_arrival_leg(spec):
    """Bind a city stay reference only to the same dated, city-scoped arrival."""
    from infrastructure.place_resolution import city_name
    city=spec.get('city_windows',{}).get('arrival')
    leg=spec.get('intercity_request',{}).get('outbound',{})
    if not city or city.get('independent') or not spec.get('destination') or not leg:return None
    if leg.get('destination_scope','city')!='city':return None
    if city_name(leg.get('destination') or spec.get('destination',''))!=city_name(spec.get('destination','')):return None
    if leg.get('date_text') and not leg.get('resolved_date'):return None
    if leg.get('resolved_date') and spec.get('start_date'):
        expected=(date.fromisoformat(spec['start_date'])+timedelta(days=city['day_index']-1)).isoformat()
        if leg['resolved_date']!=expected:return None
    return leg


def effective_city_windows(spec):
    windows=deepcopy(spec.get('city_windows',{}))
    leg=_city_arrival_leg(spec)
    if leg:
        value=leg.get('arrival_window')
        source=leg.get('field_sources',{}).get('arrival_window','')
        if value and source and value in source:
            windows['arrival'].update(time_text=value,source=source)
    return windows


def apply_city_windows(spec,declaration=None,message=''):
    """City availability remains separate from permission to query intercity transport."""
    if declaration is not None:
        if not isinstance(declaration,dict) or declaration.keys()-{'arrival','departure'}:raise ValueError('城市停留窗口无效。')
        for kind,row in declaration.items():
            if (not isinstance(row,dict) or row.keys()-{'day_index','time_text','source','independent'}
                    or type(row.get('day_index')) is not int or not 1<=row['day_index']<=(spec.get('day_count') or 14)
                    or not isinstance(row.get('source'),str) or not row['source'] or row['source'] not in message
                    or not isinstance(row.get('time_text'),str) or not row['time_text'] or row['time_text'] not in row['source']):
                raise ValueError('城市到达或离开时段缺少本轮原话依据。')
            if 'independent' in row and type(row['independent']) is not bool:raise ValueError('独立城市时段标记无效。')
            if not parse_time_window(row['time_text']):raise ValueError('请明确城市到达或离开时段。')
            indices=[number(v) for v in re.findall(r'第('+NUMBER+r')[天日]',row['source'])]
            if indices and row['day_index'] not in indices:raise ValueError('城市到达或离开日与原话不一致。')
        spec['city_windows']={**spec.get('city_windows',{}),**deepcopy(declaration)}
    if not spec.get('city_windows') or not spec.get('start_date') or not spec.get('day_count'):return
    for row in spec['city_windows'].values():
        if not 1<=row['day_index']<=spec['day_count']:raise ValueError('原城市停留窗口超出新行程，请明确调整后的到达或离开日期。')
    traffic=spec.get('intercity_request',{})
    if traffic and traffic.get('scope')!='none' and (traffic.get('origin') or traffic.get('outbound') or traffic.get('inbound')):
        return  # City stay declarations must not overwrite railway endpoint bounds.
    finish=(date.fromisoformat(spec['start_date'])+timedelta(days=spec['day_count']-1)).isoformat()
    window={**spec.get('planning_window',{}),'start':spec['start_date'],'end':finish,'days':spec['day_count'],
        'start_text':spec.get('start_date_text',spec['start_date']),'end_text':spec.get('end_date_text',finish)}
    for kind,field in (('arrival','start'),('departure','end')):
        row=spec['city_windows'].get(kind)
        if row:
            window[field]=(date.fromisoformat(spec['start_date'])+timedelta(days=row['day_index']-1)).isoformat()
            window[field+'_window']=parse_time_window(row['time_text']);window[field+'_event']='arrival' if kind=='arrival' else 'departure'
    spec['planning_window']=window


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
    elif default_rating is not None and change.get('quality_requested') and 'minimum_rating' not in result:
        result['minimum_rating'] = default_rating
        result['rating_origin'] = 'default_high_quality'
    if 'rating_mode' in change:
        if change['rating_mode'] not in {'required', 'preferred'}:
            raise ValueError('评分要求强度无效。')
        result['rating_mode'] = change['rating_mode']
    elif result.get('minimum_rating') is not None:
        result.setdefault('rating_mode', 'required')
    return result


def checked_meal_scopes(scopes):
    if (not isinstance(scopes,list) or len(scopes)>28 or any(not isinstance(item,dict)
            or item.keys()-{'day_index','meal'} or type(item.get('day_index')) is not int
            or item['day_index']<1 or item.get('meal') not in MEALS for item in scopes)):
        raise ValueError('餐饮日期或餐次范围无效。')
    return list({(item['day_index'],item['meal']):item for item in scopes}.values())




def food_comparison_unit(unit):
    currencies=[CURRENCY_NAMES.get(v.upper(),v) for v in re.findall(BUDGET_UNITS['currency'],unit,re.I)]
    if any(v!='人民币' for v in currencies):return 'foreign'
    if not re.search(r'人均|每人|[/／]人',unit) or re.search(r'总额|总价|全程|每天|每日|[/／][天日]',unit):return 'incomparable'
    rest=re.sub(BUDGET_UNITS['currency'],'',unit,flags=re.I)
    rest=re.sub(r'人均|每人|[/／]人|每餐|每顿|[/／][餐顿]|消费|参考|预算|[\s/／（）()]','',rest)
    return 'incomparable' if rest else 'per_person_meal_cny'


def inherit_food_unit(unit, previous):
    """Merge known unit components, never amounts or opaque group units."""
    patterns={'currency':BUDGET_UNITS['currency'],'basis':r'人均|每人|[/／]人|总额|总价|总预算',
              'period':r'每餐|每顿|[/／][餐顿]|'+BUDGET_UNITS['period']}
    def parts(text):
        result={};rest=text
        for key,pattern in patterns.items():
            values=re.findall(pattern,text,re.I)
            if key=='currency':values=[CURRENCY_NAMES.get(v.upper(),v) for v in values]
            else:values=[re.sub(r'^[/／]','每',v).replace('人均','每人').replace('每顿','每餐').replace('总价','总额').replace('总预算','总额') for v in values]
            values=list(dict.fromkeys(values))
            if len(values)>1:return None
            if values:result[key]=values[0]
            rest=re.sub(pattern,'',rest,flags=re.I)
        return None if re.sub(r'消费|预算|参考|[\s/／，,；;（）()]','',rest) else result
    direct=parts(unit);old=parts(previous)
    if direct is None or old is None:return unit,{}
    inherited={k:v for k,v in old.items() if k not in direct}
    if direct.get('basis')=='总额' or direct.get('period')=='全程':
        inherited={k:v for k,v in inherited.items() if k=='currency'}
    if not inherited:return unit,{}
    merged={**inherited,**direct}
    return ''.join(merged.get(k,'') for k in patterns),inherited


def normalize_food_units(spec):
    """Repair partial legacy meal units in a candidate, retaining original source text."""
    food=spec.get('budgets',{}).get('food',{})
    for meal in food.get('meals',{}).values():
        unit,inherited=inherit_food_unit(meal.get('unit_text',''),food.get('unit_text',''))
        if inherited:
            meal.setdefault('unit_literal',meal.get('unit_text',''))
            meal.update(unit_text=unit,unit_inherited=inherited,comparison_unit=food_comparison_unit(unit))


def _apply_budget(spec, change, message, summary):
    if not isinstance(change, dict) or not isinstance(change.get('source'), str):
        raise ValueError('预算变更缺少原话依据。')
    value, scope, source = change.get('value'), change.get('scope'), change['source']
    unit = change.get('unit_text', '')
    if scope=='accommodation' and isinstance(value,str) and value not in message:
        amount = re.sub(r'^(?:人民币|CNY|RMB|[￥¥])\s*|\s*(?:人民币|CNY|RMB|元|[￥¥])$', '', value, flags=re.I)
        if amount and amount in source and amount in message:value=amount
    if (change.keys() - {'value','source','scope','unit_text','scopes','meal_policy','meal_policy_source'} or not isinstance(value,str)
            or not value or len(value)>150 or value not in message or not source or source not in message
            or not isinstance(scope,str) or scope not in BUDGET_SCOPES or not isinstance(unit,str)
            or len(unit)>120):
        raise ValueError('预算或计价单位无法对应你的原话，尚未修改行程。')
    if unit and unit not in message:
        # Only the declared lodging defaults may be inferred by a model; recompute them below.
        compact = re.sub(r'[\s/／每]', '', unit)
        if scope!='accommodation' or not re.fullmatch(r'(?:人民币|CNY|RMB|元|[￥¥])?(?:间房?|房间|房)?晚?',compact,re.I):
            raise ValueError('预算或计价单位无法对应你的原话，尚未修改行程。')
        unit = ''
    scopes=checked_meal_scopes(change.get('scopes',[]))
    if scopes and scope!='food':raise ValueError('单餐预算只能用于餐饮类别。')
    policy=change.get('meal_policy','preserve')
    if policy not in {'preserve','replace_all'}:raise ValueError('餐次预算覆盖方式无效。')
    if policy=='replace_all':
        evidence=change.get('meal_policy_source','')
        if (scope!='food' or scopes or not isinstance(evidence,str) or not evidence or evidence not in message
                or not re.search(r'所有|全部|每餐|各餐',evidence) or not re.search(r'统一|清除|取消|不保留|去掉',evidence)
                or re.search(r'(?:不要|别|不用|不必).{0,6}(?:统一|全部)',evidence)):
            raise ValueError('清除单餐例外需要本轮明确的全餐次统一授权。')
    category=deepcopy(spec.setdefault('budgets', {}).get(scope, {}))
    budget={k:v for k,v in category.items() if k!='meals'}
    budget.pop('meal_policy',None);budget.pop('meal_policy_source',None)
    if scopes and len(scopes)==1:
        budget.update(category.get('meals',{}).get(f"{scopes[0]['day_index']}:{scopes[0]['meal']}",{}))
    old_source=budget.get('source')
    clauses = re.split(r'[，,。；;\n]',message)
    context = '；'.join(part for part in clauses if source in part and value in part)
    if not context:context='；'.join(part for part in clauses if value in part)
    literal_unit = unit or budget.get('unit_text','')
    remaining = re.sub('|'.join(BUDGET_UNITS.values()),'',literal_unit,flags=re.I)
    opaque_unit = bool(re.sub(r'[\s/／，,；;：:（）()]','',remaining))
    if scope=='accommodation' and opaque_unit:
        # Preserve explicit units outside the recognized vocabulary rather than relabel their currency.
        if unit:
            budget['unit_text']=unit
            budget.pop('unit_defaults',None)
    elif scope=='accommodation':
        parts, defaults, inherited_parts, direct_parts = {}, {}, set(), set()
        for key, pattern in BUDGET_UNITS.items():
            texts = [(unit,False),(value,False),(context,False),(budget.get('unit_text',''),True)]
            for text, inherited in texts:
                matches = re.findall(pattern,text,re.I)
                if key=='currency':matches=[CURRENCY_NAMES.get(m.upper(),m) for m in matches]
                else:
                    matches=[('每'+m[1:] if m.startswith(('/','／')) else m).replace('间房','间').replace('房间','间').replace('每房','每间') for m in matches]
                matches = list(dict.fromkeys(matches))
                if len(matches)>1:
                    raise ValueError('住宿预算包含多种币种或计价单位，请明确这笔住宿预算的口径。')
                if matches:
                    parts[key] = matches[0]
                    if unit and text==unit:direct_parts.add(key)
                    if inherited:
                        inherited_parts.add(key)
                        if key in budget.get('unit_defaults',{}):defaults[key]=parts[key]
                    break
        for key, default in HOTEL_BUDGET_DEFAULTS.items():
            if key not in parts and not (key=='period' and parts.get('basis') in {'总额','总价','总预算'}
                                        or key=='basis' and parts.get('period')=='全程'):
                parts[key]=default;defaults[key]=default
        literal = unit if direct_parts==parts.keys() else budget.get('unit_text','') if inherited_parts==parts.keys() else ''
        budget['unit_text'] = literal if literal and not defaults else ''.join(parts.get(key,'') for key in HOTEL_BUDGET_DEFAULTS)
        if defaults:budget['unit_defaults']=defaults
        else:budget.pop('unit_defaults',None)
    elif scope=='food':
        previous_unit=budget.get('unit_text','')
        if scopes:previous_unit=inherit_food_unit(previous_unit,category.get('unit_text',''))[0]
        direct=unit
        if not re.search(BUDGET_UNITS['currency'],direct,re.I):
            currencies=list(dict.fromkeys(CURRENCY_NAMES.get(v.upper(),v) for v in re.findall(BUDGET_UNITS['currency'],value+' '+context,re.I)))
            if len(currencies)>1:raise ValueError('餐饮预算含多个币种，请明确这笔预算的币种。')
            if currencies:direct=currencies[0]+direct
        resolved,inherited=inherit_food_unit(direct,previous_unit)
        if resolved or 'unit_text' in change or 'unit_text' in budget:budget['unit_text']=resolved
        if inherited:budget.update(unit_literal=unit,unit_inherited=inherited)
        else:budget.pop('unit_inherited',None);budget.pop('unit_literal',None)
    elif 'unit_text' in change:
        budget['unit_text'] = unit
    budget.update(value=value, source=source)
    budget['bounds']=budget_bounds(value,source)
    if scope=='food':
        budget['comparison_unit']=food_comparison_unit(budget.get('unit_text',''))
    if scopes:
        for item in scopes:category.setdefault('meals',{})[f"{item['day_index']}:{item['meal']}"]=deepcopy(budget)
        spec['budgets'][scope]=category
    else:
        if policy=='replace_all':
            old_sources={v.get('source') for v in category.get('meals',{}).values()}
            spec['preferences']=[v for v in spec.get('preferences',[]) if v not in old_sources]
            budget.update(meal_policy=policy,meal_policy_source=change['meal_policy_source'])
        elif category.get('meals'):budget['meals']=category['meals']
        spec['budgets'][scope] = budget
    default=spec.get('applied_preferences',{}).get('budget',{})
    default_scope=next((key for key,label in BUDGET_SCOPES.items() if default.get('category') in {key,label}),None)
    if default_scope and default_scope!=scope and default_scope not in spec['budgets']:
        spec['budgets'][default_scope]={'value':str(default['amount']),
            'unit_text':f"{default['currency']} {default['basis']}{default['period']}",'source':'长期偏好'}
    if default.get('category') in {scope,BUDGET_SCOPES[scope]}:
        spec.pop('budget_text',None)
        spec['applied_preferences'].pop('budget',None)
    if old_source:
        spec['preferences']=[text for text in spec.get('preferences',[]) if text!=old_source]
    label='、'.join(f"第{s['day_index']}天{s['meal']}" for s in scopes) or BUDGET_SCOPES[scope]
    summary.append(label + '预算：' + value + ('；'+budget['unit_text'] if budget.get('unit_text') else '') + budget_default_note(budget))
    return source


def _change_pace(spec, change, message, summary, key):
    value = change.get('value')
    if not isinstance(value,str) or value not in PACE or change.keys() - {'value','source'}:
        raise ValueError('行程节奏值无效。')
    spec['gentle'] = value == 'relaxed'
    spec['pace'] = value
    summary.append('节奏：' + PACE[value])


def _change_transport(spec, change, message, summary, key):
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


def _change_night(spec, change, message, summary, key):
    if type(change.get('value')) is not bool or change.keys() - {'value','source'}:
        raise ValueError('夜游偏好必须明确是否安排。')
    spec['no_night'] = not change['value']
    summary.append('夜间：' + ('可以安排' if change['value'] else '不安排活动'))


def _change_interests(spec, change, message, summary, key):
    values = change.get('value')
    if (change.keys() - {'value','source'} or not isinstance(values,list) or len(values) > 8
            or any(not isinstance(v,str) or not v or len(v)>80 or v not in message for v in values)):
        raise ValueError('兴趣类别必须来自本轮原话。')
    spec['preference_interests'] = list(dict.fromkeys(values))
    summary.append('兴趣：' + '、'.join(values))


def _change_requirements(spec, change, message, summary, key):
    source = change['source']
    value = change.get('value')
    if key == 'accommodation':
        allowed = {'value','source','requested','replace','preferred_brands','preferred_groups','brand_mode','brand_policy','chain_mode',
                   'minimum_rating','rating_mode','quality_requested','quality_text','lodging_types','lodging_type_mode','near_itinerary','same_hotel_for_trip'}
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
        scoped=key=='dining' and bool(change.get('scopes'))
        current = {} if change.get('replace') or not requested or scoped else spec.get(prefix + '_preferences', {})
        if key=='accommodation':current={k:v for k,v in current.items() if k!='selected_poi_id'}
        if key=='accommodation' and current.get('rating_origin')=='default_high_quality':
            current={k:v for k,v in current.items() if k not in {'minimum_rating','rating_origin','rating_mode'}}
        preferences = _rating_preferences(change, current,
            None if key == 'accommodation' else DINING_DEFAULT_RATING)
        if key=='accommodation' and ('preferred_brands' in change or 'preferred_groups' in change):
            policy=change.get('brand_policy','replace')
            if policy not in {'replace','extend'}:raise ValueError('品牌范围更新方式无效。')
            if policy=='replace':
                preferences.pop('preferred_brands',None);preferences.pop('preferred_groups',None)
        for field in ('preferred_brands', 'preferred_groups', 'lodging_types', 'cuisines', 'keywords', 'avoid', 'allergies'):
            if field in change:
                values = _text_list(change, field, source)
                preferences[field] = list(dict.fromkeys([*preferences.get(field, []), *values])) if values else []
        if key=='accommodation' and change.get('preferred_groups') and 'brand_mode' not in change:
            preferences['brand_mode']='required'
        if key=='accommodation' and not (preferences.get('preferred_brands') or preferences.get('preferred_groups')):
            preferences.pop('brand_mode',None)
        if 'quality_text' in change:
            if not isinstance(change['quality_text'],str) or change['quality_text'] not in source:
                raise ValueError('住宿品质诉求缺少原话依据。')
            preferences['quality_text']=change['quality_text']
        for field, allowed_values in (
            ('brand_mode', {'required','preferred'}), ('chain_mode', {'required','preferred','none'}),
            ('cuisine_mode', {'required','preferred'}),('lodging_type_mode', {'required','preferred'})):
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
            preferences['scopes'] = checked_meal_scopes(scopes)
        if requested:
            if key == 'accommodation':
                preferences.setdefault('near_itinerary', True)
                preferences.setdefault('same_hotel_for_trip', True)
            preferences.setdefault('near_itinerary',True)
            if scoped:
                base=deepcopy(spec.get('dining_preferences',{}))
                for scope in preferences.pop('scopes'):
                    scope_key=f"{scope['day_index']}:{scope['meal']}"
                    base.setdefault('meal_overrides',{})[scope_key]={**base.get('meal_overrides',{}).get(scope_key,{}),**deepcopy(preferences)}
                spec['dining_preferences']=base
            else:spec[prefix + '_preferences'] = preferences
        else:
            spec.pop(prefix + '_preferences', None)
        summary.append(('住宿：' if key == 'accommodation' else '餐饮：') + value)
    else:
        summary.append('补充要求：' + value)


def _change_budget(spec, change, message, summary, key):
    _apply_budget(spec, change, message, summary)


def _change_intercity(spec, change, message, summary, key):
    value=change.get('value')
    spec['intercity_request']=intercity_request(value,spec.get('intercity_request',{}),message)
    summary.append('更新城际交通要求；相应车次及接驳将在新草稿中核对')


def _change_city_windows(spec, change, message, summary, key):
    if set(change)!={'value','source'}:raise ValueError('城市停留时段字段无效。')
    apply_city_windows(spec,change['value'],message)
    summary.append('已保留明确的城市到达与离开时段')


def _change_hotel_stay(spec,change,message,summary,key):
    from tools.trip.constraints import stay_change
    spec['hotel_stay']=stay_change(spec.get('hotel_stay',{}),change,message,spec=spec)
    spec['hotel_requested']=True
    summary.append('已更新有原话依据的住宿入住条件')


CONSTRAINT_HANDLERS = {
    'hotel_stay':_change_hotel_stay,
    'pace': _change_pace,
    'transport': _change_transport,
    'night': _change_night,
    'interests': _change_interests,
    'accommodation': _change_requirements,
    'dining': _change_requirements,
    'requirements': _change_requirements,
    'budget': _change_budget,
    'intercity': _change_intercity,
    'city_windows': _change_city_windows,
}


def transport_only(changes):
    if not changes or 'intercity' not in changes or changes.keys()-{'intercity','requirements'}:return False
    if 'requirements' not in changes:return True
    # A repeated free-text field is redundant only when one structured field carries that exact evidence.
    quotes=[field.get('source') for part in changes['intercity'].get('value',{}).values() if isinstance(part,dict)
            for field in part.values() if isinstance(field,dict)]
    return changes['requirements'].get('value') in quotes


def apply_constraints(previous, changes, message):
    if not isinstance(changes, dict) or not changes:
        raise ValueError('模型尚未解析出有效的行程变化，原行程未修改，请重新说明希望调整的内容。')
    if changes.keys() - (CONSTRAINT_HANDLERS.keys() | {'budgets'}):
        raise ValueError('模型返回了无法执行的修改字段，原行程未修改，请重新描述这次调整。')
    spec = deepcopy(previous)
    overrides = spec.setdefault('constraint_overrides', {})
    summary = []
    for key, change in changes.items():
        if key=='requirements' and transport_only(changes):continue
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
        CONSTRAINT_HANDLERS[key](spec, change, message, summary, key)
        old_source = overrides.get(key, {}).get('source')
        if old_source and key != 'requirements' and not (key in {'accommodation','dining'} and change.get('requested',True) and not change.get('replace')):
            spec['preferences'] = [item for item in spec.get('preferences',[]) if item != old_source]
        if key not in {'intercity','city_windows'}:spec['preferences'] = list(dict.fromkeys([*spec.get('preferences',[]), source]))
        if key!='budget':spec.setdefault('applied_preferences', {}).pop(key, None)
        spec.setdefault('field_sources', {})[key] = 'current_request'
        overrides[key] = deepcopy(change)
    city=changes.get('city_windows',{}).get('value',{}).get('arrival')
    leg=_city_arrival_leg(spec)
    if city and leg:
        incoming=changes.get('intercity',{}).get('value',{}).get('outbound',{}).get('arrival_window')
        if incoming and parse_time_window(incoming['value'])!=parse_time_window(city['time_text']):
            raise ValueError('两处到达时段不一致，请明确是否分别指车站到达和市区到达；本轮未修改行程。')
        leg['arrival_window']=city['time_text'];leg['time_reference']='arrival'
        leg.setdefault('field_sources',{}).update(arrival_window=city['source'],time_reference=city['source'])
    if 'city_windows' in spec:spec['city_windows']=effective_city_windows(spec)
    spec['preferences'] = travel_preferences(spec.get('preferences', []))
    return spec, summary


def effective_changes(previous, changes, message):
    """Reaffirmed structured values do not expand the work needed for an edit."""
    provenance={'source','field_sources','fact_sources','constraint_overrides','applied_preferences'}
    def canonical(value, free_text=False):
        if isinstance(value,dict):
            return {k:canonical(v,free_text) for k,v in value.items()
                    if k not in provenance and not k.startswith('_') and (free_text or k!='preferences')}
        if isinstance(value,(list,tuple)):return [canonical(v,free_text) for v in value]
        return value
    result={}
    for key,change in changes.items():
        proposed,_=apply_constraints(previous,{key:change},message)
        if canonical(proposed,key=='requirements')!=canonical(previous,key=='requirements'):
            result[key]=change
    return result


def merge_constraint_changes(previous, delta, *, correcting_meal_window=False):
    """Validated pending changes survive a partial answer; explicit replacement resets a category."""
    result=deepcopy(previous)
    if correcting_meal_window:
        incoming=([delta['budget']] if delta.get('budget') else [])+delta.get('budgets',[])
        corrected={item.get('scope') for item in incoming if item.get('scopes')}
        if result.get('budget',{}).get('scope') in corrected and result['budget'].get('scopes'):
            result.pop('budget')
        if 'budgets' in result:
            result['budgets']=[item for item in result['budgets'] if not (item.get('scope') in corrected and item.get('scopes'))]
    for key,value in delta.items():
        if key=='budgets':
            identity=lambda item:(item['scope'],tuple(sorted((s['day_index'],s['meal']) for s in item.get('scopes',[]))))
            budgets={identity(item):item for item in result.get('budgets',[])}
            for item in value:
                budgets[identity(item)]={**budgets.get(identity(item),{}),**item}
            result[key]=list(budgets.values())
        elif isinstance(value,dict) and not value.get('replace') and value.get('requested') is not False:
            result[key]={**result.get(key,{}),**deepcopy(value)}
        else:
            result[key]=deepcopy(value)
    return result


def rebase_candidate(base,candidate,current):
    """Three-way business merge; conflicting edits are returned, never silently selected."""
    missing=object();conflicts=[]
    metadata={'constraint_overrides','fact_sources','field_sources','activity_materials','planning_evidence',
              'planning_answers','planning_decision','research_candidates','source_context','research_note'}
    def clone(value):return missing if value is missing else deepcopy(value)
    def merge(old,local,latest,path):
        if local==old:return clone(latest)
        if latest==old or local==latest:return clone(local)
        if path.endswith('.source') or '.field_sources.' in path:return clone(latest if latest is not missing else local)
        if path in {'plan.days','plan.dining_options'} and all(isinstance(v,list) for v in (old,local,latest)):
            fields=('day_index',) if path=='plan.days' else ('day_index','meal')
            if all(isinstance(r,dict) and all(k in r for k in fields) for values in (old,local,latest) for r in values):
                keyed=[{tuple(r[k] for k in fields):r for r in values} for values in (old,local,latest)]
                if all(len(k)==len(v) for k,v in zip(keyed,(old,local,latest))):
                    value=merge(*keyed,path)
                    return [value[key] for key in sorted(value)]
        if isinstance(local,dict) and isinstance(latest,dict) and (isinstance(old,dict) or old is missing):
            old={} if old is missing else old;result={}
            for key in old.keys()|local.keys()|latest.keys():
                previous,proposed,live=old.get(key,missing),local.get(key,missing),latest.get(key,missing)
                if path=='plan' and key in {'hotel_quotes','hotel_ids','restaurant_ids','hotel_recommendations','hotel_recommendation_policy','hotel_review_note','restaurant_recommendations','dining_options'} or key=='legs' and path.startswith('plan.days'):
                    value=clone(live if live is not missing else proposed)
                elif path=='spec' and key in metadata:
                    # Provenance remains in both immutable snapshots. Do not mistake metadata for user conflicts.
                    if isinstance(proposed,dict) and isinstance(live,dict):
                        value={**deepcopy(proposed),**deepcopy(live)}
                        for field in proposed:
                            if proposed[field]!=(previous.get(field,missing) if isinstance(previous,dict) else missing):value[field]=deepcopy(proposed[field])
                    else:value=clone(live if live is not missing else proposed)
                else:value=merge(previous,proposed,live,path+'.'+str(key))
                if value is not missing:result[key]=value
            return result
        conflicts.append(path)
        return clone(latest)
    result=deepcopy(current)
    for key in ('spec','plan','title','status'):
        result[key]=merge(base.get(key,missing),candidate.get(key,missing),current.get(key,missing),key)
    result['sources']={**deepcopy(candidate['sources']),**deepcopy(current['sources'])}
    from tools.hotel_prices.quotes import preferred_snapshot
    quote=preferred_snapshot(result['spec'],[current['plan'].get('hotel_quotes'),candidate['plan'].get('hotel_quotes')])
    if quote:result['plan']['hotel_quotes']=quote
    result['version']=current['version']+1
    return result,conflicts


def edit_text(field, value):
    if field == 'hotel_budget' and re.fullmatch(r'[1-9][0-9]{0,5}(?:-[1-9][0-9]{0,5})?',value):
        amounts=[int(v) for v in value.split('-')]
        if len(amounts)==2 and amounts[0]>amounts[1]:
            raise ValueError('预算范围下限不能高于上限。')
        return f'酒店预算改为{value}元/间/晚'
    if field == 'night' and value == 'false':
        return '不要夜游'
    raise ValueError('请填写明确的住宿预算（人民币每间每晚）或选择不夜游。')


LABELS = {'hotel_stay':'住宿入住条件','city_windows':'城市停留时段','budgets':'预算','hotel_preferences':'住宿偏好','accommodation_text':'住宿要求',
          'transport_preferences':'交通偏好','transport_text':'交通方式','gentle':'轻松节奏',
          'no_night':'不安排夜游','dining_preferences':'餐饮偏好','dining_text':'餐饮要求',
          'start_date':'开始日期','end_date':'结束日期','day_count':'旅行天数'}


LABELS.update({'hotel_requested':'需要住宿候选','restaurant_requested':'需要餐饮候选',
               'preferences':'旅行要求','preference_interests':'兴趣','indoor_days':'室内活动日期',
               'outdoor_days':'室外活动日期','indoor_periods':'室内活动时段','outdoor_periods':'室外活动时段'})


CHANGE_FIELDS = {'hotel_stay':'hotel_stay','budget':'budgets','budgets':'budgets','accommodation':'hotel_preferences',
                 'dining':'dining_preferences','transport':'transport_preferences','pace':'gentle','night':'no_night',
    'requirements':'preferences','interests':'preference_interests','intercity':'intercity_request','city_windows':'city_windows'}


DETAIL_LABELS = {'preferred':'优先','discouraged':'尽量避免','forbidden':'不使用',
    'preferred_brands':'品牌','preferred_groups':'酒店集团/品牌体系','brand_mode':'品牌要求','chain_mode':'连锁要求','minimum_rating':'最低评分',
    'rating_mode':'评分要求','rating_origin':'评分来源','quality_requested':'品质偏好','quality_text':'品质诉求',
    'lodging_types':'住宿类型','lodging_type_mode':'住宿类型要求','meal_overrides':'各餐安排',
    'near_itinerary':'靠近行程地点','same_hotel_for_trip':'全程同一家酒店','cuisines':'菜系',
    'keywords':'特色','cuisine_mode':'菜系要求','avoid':'忌口','allergies':'过敏限制',
    'scopes':'适用餐次','day_index':'第几天','meal':'餐别'}


def record_sources(spec, changes, event):
    metadata = deepcopy(spec.get('fact_sources',{}))
    for key, change in changes.items():
        field = CHANGE_FIELDS[key]
        entries = change if isinstance(change,list) else [change]
        for item in entries:
            path = field + '.' + item['scope'] if key in {'budget','budgets'} else field
            source = item.get('source','')
            if source and source in event.content:
                paths=[path+'.meals.'+f"{s['day_index']}:{s['meal']}" for s in item.get('scopes',[])] or [path]
                for path in paths:
                    metadata[path] = {'event_id':event.event_key,'source':source,'origin':'user',
                                      'revision':metadata.get(path,{}).get('revision',0)+1}
    spec['fact_sources'] = metadata


def display(value, field=''):
    if value is None:
        return '未明确'
    if isinstance(value,bool):
        return '是' if value else '否'
    if isinstance(value,str):
        if field in {'preferred','discouraged','forbidden'}:
            return MODES.get(value,value)
        if field in {'brand_mode','chain_mode','rating_mode','cuisine_mode','rating_origin'}:
            return {'required':'必须满足','preferred':'优先考虑','none':'无要求','user':'用户指定',
                    'default_high_quality':'默认高评分标准'}.get(value,value)
        return value or '已清除'
    if isinstance(value,list):
        return '、'.join(display(item,field) for item in value) or '未指定'
    if isinstance(value,dict):
        return '；'.join((f'第{key}天' if str(key).isdigit() else DETAIL_LABELS.get(key,'补充条件'))+
                        '：'+display(item,key) for key,item in sorted(value.items())) or '未指定'
    return str(value)


def facts(spec):
    if 'city_windows' in spec:spec={**spec,'city_windows':effective_city_windows(spec)}
    if 'preferences' in spec:spec={**spec,'preferences':travel_preferences(spec['preferences'])}
    rows=[]
    for key,label in LABELS.items():
        if key not in spec:
            continue
        if spec[key] in (None,'',[],{}):continue
        if key=='city_windows':
            for direction,item in spec[key].items():
                separate=item.get('independent') or spec.get('intercity_request',{}).get('outbound',{}).get('destination_scope')=='station'
                label=('市区到达时段' if separate else '到达城市时段') if direction=='arrival' else '离开城市时段'
                rows.append({'field':'city_windows.'+direction,'label':label,
                    'value':f"第{item['day_index']}天{item['time_text']}",'source':item['source']})
        elif key == 'budgets':
            for scope,budget in spec[key].items():
                field='budgets.'+scope
                value=budget.get('value','')
                unit=budget.get('unit_text','')
                if value:rows.append({'field':field,'label':{'accommodation':'住宿预算','food':'餐饮预算','transport':'交通预算','trip':'全程预算'}.get(scope,scope),
                    'value':(value if unit and unit in value else value+' '+(unit or '（单位未明确）'))+budget_default_note(budget),
                    'source':spec.get('fact_sources',{}).get(field,{}).get('source','历史来源未完整记录')})
                for meal,item in budget.get('meals',{}).items():
                    day,label=meal.split(':',1)
                    rows.append({'field':field+'.meals.'+meal,'label':f'第{day}天{label}预算',
                        'value':item.get('value','')+' '+item.get('unit_text','（单位未明确）')
                            +(budget_default_note(item) if item.get('unit_inherited') else ''), 'source':item.get('source','')})
        else:
            tentative = key in {'start_date', 'end_date'} and spec.get('date_is_tentative')
            rows.append({'field':key,'label':label,'value':display(spec[key],key) + ('（暂定参考）' if tentative else ''),
                         'source':('按提问日次日生成的参考日期，实际出发日期尚未确定。' if tentative else
                                   spec.get('fact_sources',{}).get(key,{}).get('source','历史来源未完整记录'))})
    return rows


def stable(value):
    if isinstance(value,dict):
        return {k:stable(v) for k,v in value.items() if k not in {'queried_at','checked_at'}}
    if isinstance(value,list):
        return [stable(v) for v in value]
    return value


def trip_diff(current, candidate):
    before={r['field']:r for r in facts(current['spec'])}
    after={r['field']:r for r in facts(candidate['spec'])}
    changes=[]
    for direction,label in (('outbound','去程'),('inbound','返程')):
        old=current['plan'].get('intercity',{}).get(direction,{})
        new=candidate['plan'].get('intercity',{}).get(direction,{})
        if stable(old)!=stable(new):
            describe=lambda leg: (leg.get('selected',{}).get('train_code','未选择')+' '+leg.get('selected',{}).get('departure_at','')
                                  if leg.get('selected') else leg.get('status','未规划'))
            changes.append({'field':'intercity.'+direction,'label':label+'交通','before':describe(old),'after':describe(new)})
    for field in sorted(before.keys() | after.keys()):
        old,new=before.get(field),after.get(field)
        if (old or {}).get('value') != (new or {}).get('value'):
            changes.append({'field':field,'label':(new or old)['label'],
                            'before':old['value'] if old else '未明确','after':new['value'] if new else '已清除'})
    old_days={d['day_index']:d for d in current['plan']['days']}
    changed_days=[]
    for day in candidate['plan']['days']:
        old=old_days.get(day['day_index'],{})
        if (old.get('activities'),old.get('date')) != (day.get('activities'),day.get('date')):
            changed_days.append(day['day_index'])
            def describe(value,trip):
                return '；'.join(a['period']+' '+trip['sources'].get(a['poi_id'],{}).get('name',a['poi_id']) for a in value.get('activities',[])) or '无活动'
            changes.append({'field':f"day.{day['day_index']}",'label':f"第{day['day_index']}天活动",
                            'before':describe(old,current),'after':describe(day,candidate)})
    changed_days.extend(i for i in old_days if i not in {d['day_index'] for d in candidate['plan']['days']})
    if current['plan'].get('hotel_ids',[]) != candidate['plan'].get('hotel_ids',[]):
        def hotel_names(trip):
            return '、'.join(trip['sources'].get(k,{}).get('name',k) for k in trip['plan'].get('hotel_ids',[])) or '无候选'
        changes.append({'field':'hotel_ids','label':'酒店候选','before':hotel_names(current),'after':hotel_names(candidate)})
    return {'base_version':current['version'],'changes':changes,'changed_days':changed_days,
            'hotel_changed':current['plan'].get('hotel_ids',[]) != candidate['plan'].get('hotel_ids',[]),
            'routes_changed':stable([d.get('legs',[]) for d in current['plan']['days']]) != stable([d.get('legs',[]) for d in candidate['plan']['days']]),
            'price_note':'预算为目标；没有实时价格依据时不能确认候选达标。'}


def render_diff(diff):
    rows=['本次累计变更（相对已保存版本）：']
    rows.extend(f"• {r['label']}：{r['before']} → {r['after']}" for r in diff['changes'])
    rows.append('• 活动安排：'+('第'+ '、'.join(map(str,diff['changed_days']))+'天变化' if diff['changed_days'] else '保持不变'))
    rows.append('• 酒店候选：'+('已变化' if diff['hotel_changed'] else '保持不变'))
    rows.append(diff['price_note'])
    return '\n'.join(rows)


def edit_constraints(field, value):
    """UI values become the same constraint contract as grounded language, without routing."""
    text = edit_text(field, value)
    if field == 'hotel_budget':
        return {'budget': {'value':value, 'scope':'accommodation', 'unit_text':'元/间/晚', 'source':text}}
    if field == 'night':
        return {'night': {'value':False, 'source':text}}
    raise ValueError('不支持的行程字段。')


def plan_destination(user, content):
    patterns = (
        rf'(?:想去|去)([\u4e00-\u9fff]{{2,10}}?)(?:玩|旅游|旅行|看看)',
        rf'(?:搜索|查(?:一下)?|了解|规划|安排)([\u4e00-\u9fff]{{2,10}}?)(?=(?:{NUMBER})[天日](?:游)?|攻略|旅行|旅游|行程)',
    )
    for pattern in patterns:
        match = re.search(pattern, user)
        if match:
            return match[1]
    match = re.search(r'(?:去|到)([\u4e00-\u9fff]{2,10})(?=[，。！；,\s]|$)', user)
    if match:
        return match[1]
    # Only an itinerary title can supply assistant-owned destination evidence.
    for line in plan_body(content).splitlines()[:6]:
        title = re.sub(r'^[\s#*]+|[*\s]+$', '', line)
        match = re.fullmatch(rf'([\u4e00-\u9fff]{{2,10}}?)(?:(?:{NUMBER})[天日](?:游)?|参考|研究|旅行|旅游)+(?:参考|研究|旅行|旅游|游玩)?(?:规划|方案|行程|攻略).*', title)
        if match:
            return match[1]
    return ''


def plan_body(content):
    body = re.sub(r'```.*?```|~~~.*?~~~', '', content, flags=re.S)
    body = re.sub(r'^\s*>.*$', '', body, flags=re.M)
    body = re.sub(r'“[^”]*”|「[^」]*」|"[^"\n]*"', '', body)
    # A demonstrative itinerary is not the user's travel proposal.
    body = re.split(r'(?:例如|举例|示例)(?:如下)?[：:]', body, maxsplit=1)[0]
    return body


def plan_day_numbers(content):
    body = plan_body(content)
    return [number(m[1] or m[2]) for m in re.finditer(
        rf'(?:第\s*({NUMBER})\s*天|\bDay\s*(\d+))(?=[\s*：:、，,（(\-]|[^\x00-\x7f])', body, re.I)]


def structured_plan(content):
    days = plan_day_numbers(content)
    if not days or days[0] != 1:
        return False
    # Each variant must have a complete sequential day structure.
    previous = 0
    for day in days:
        if day != 1 and day != previous + 1:
            return False
        previous = day
    variants = list(re.finditer(rf'(?:方案\s*[A-Fa-f甲乙丙一二三\d]+|(?:完整|总)行程)[：:\s*]*({NUMBER})\s*[天日]',content))
    for i, match in enumerate(variants):
        section = content[match.end():variants[i+1].start() if i+1<len(variants) else len(content)]
        variant_days = plan_day_numbers(section)
        if not variant_days or max(variant_days) != number(match[1]):
            return False
    return bool(plan_body(content).strip())


def plan_durations(user, content):
    values = []
    body = plan_body(content)
    variants = re.findall(rf'(?:方案\s*[A-Fa-f甲乙丙一二三\d]+|(?:完整|总)行程)[：:\s*]*({NUMBER})\s*[天日]', body)
    days = plan_day_numbers(body)
    totals = variants or ([str(max(days))] if days and structured_plan(body) else [])
    if not totals:
        totals = re.findall(rf'(?<![第每同月\d一二两三四五六七八九十])({NUMBER})\s*(?:天|日游)', user)
    for text in totals:
        value = number(text)
        if 1 <= value <= 14 and value not in values:
            values.append(value)
    return values[:6]




def proposal_metadata(user, content, *, kind='exploratory', destination='', duration_options=None):
    if kind not in {'exploratory', 'research_backed'} or not isinstance(content, str):
        raise ValueError('规划方案类型或正文无效。')
    destination = destination or plan_destination(user, content)
    options = list(duration_options or plan_durations(user, content))
    structural_days = plan_durations('', content)
    if duration_options and structural_days and not set(duration_options) & set(structural_days):
        return None
    requested = plan_durations(user, '')
    if requested and options and not set(requested) & set(options):
        return None
    if not destination or not structured_plan(content):
        return None
    title = destination + ('研究方案' if kind=='research_backed' else '参考方案')
    if options:
        title += '（'+'／'.join(str(v)+'天' for v in options)+'）'
    return {'kind':kind, 'destination':destination, 'duration_options':options, 'title':title,
            'summary':re.sub(r'\s+', ' ', content).strip()[:360], 'capture_version':CAPTURE_VERSION}


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



def itinerary_calendar(spec):
    return {key:spec.get(key) for key in ('start_date','end_date','day_count','date_is_tentative')}


def stay_change(previous,change,text,*,spec=None):
    from tools.hotel_prices.quotes import STAY_FIELDS, _date_present
    """Only explicit, individually evidenced fields; never import test occupancy defaults."""
    if not isinstance(change,dict) or change.keys()-{'value','source','field_sources'}:
        raise ValueError('住宿条件字段无效。')
    values=change.get('value');sources=change.get('field_sources',{})
    if not isinstance(values,dict) or values.keys()-STAY_FIELDS or not isinstance(sources,dict):
        raise ValueError('住宿条件结构无效。')
    result=deepcopy(previous or {});result.setdefault('field_sources',{})
    for key,value in values.items():
        source=sources.get(key)
        if not isinstance(source,str) or not source or len(source)>240 or source not in text:
            raise ValueError('住宿条件必须逐项保留用户原话。')
        if key in {'check_in','check_out'}:
            if not isinstance(value,str):raise ValueError('入住退房日期无效。')
            parsed=date.fromisoformat(value)
            anchor=(spec or {}).get('start_date' if key=='check_in' else 'end_date') or (spec or {}).get('start_date')
            context_year=int(anchor[:4]) if anchor and not (spec or {}).get('date_is_tentative') else None
            if not _date_present(value,source+'\n'+change.get('source',''),context_year=context_year):raise ValueError('入住退房日期缺少明确年月日或已确认的年份依据。')
        elif key in {'rooms','adults'}:
            from core.time_input import NUMBER,number
            unit=r'间(?:房)?' if key=='rooms' else r'(?:位|名|个)?(?:成年(?:人)?|成人)'
            if type(value) is not int or not 1<=value<=20 or not any(number(n)==value for n in re.findall('('+NUMBER+')'+unit,source)):
                raise ValueError('房数或成人数无法对应原话。')
        elif key=='children_ages':
            if (not isinstance(value,list) or len(value)>8 or any(type(n) is not int or not 0<=n<=17 for n in value)
                    or not value and not re.search(r'无儿童|没有儿童|不带儿童|仅成人|都是成年人',source)
                    or value and any(not re.search(str(n)+r'\s*岁',source) for n in value)):
                raise ValueError('请明确儿童年龄，或明确没有儿童。')
        elif not isinstance(value,str) or not value or len(value)>100 or value not in source:
            raise ValueError('房型、早餐或退改要求缺少原话依据。')
        result[key]=value;result['field_sources'][key]={'origin':'user','text':source}
    if spec is not None and values.keys()&{'check_in','check_out'}:
        result['itinerary_calendar']=itinerary_calendar(spec)
    if result.get('check_in') and result.get('check_out'):
        nights=(date.fromisoformat(result['check_out'])-date.fromisoformat(result['check_in'])).days
        if not 1<=nights<=14:raise ValueError('住宿退房须晚于入住，首期支持连续1至14晚。')
    return result


def missing_stay(spec,now=None):
    from tools.hotel_prices.quotes import LABELS
    from zoneinfo import ZoneInfo
    stay=spec.get('hotel_stay',{});missing=[label for key,label in LABELS.items() if stay.get(key) is None]
    if not missing:
        try:
            nights=(date.fromisoformat(stay['check_out'])-date.fromisoformat(stay['check_in'])).days
            if not 1<=nights<=14:missing.append('有效的连续住宿日期')
            if now and date.fromisoformat(stay['check_in'])<now.astimezone(ZoneInfo('Asia/Shanghai')).date():missing.append('未来入住日期')
        except (TypeError,ValueError):missing.append('有效入住退房日期')
    if stay.get('rooms') not in {None,1}:missing.append('首期仅支持单间连续住宿，多房分配暂不能形成可比较报价')
    if stay.get('itinerary_calendar') is not None and stay['itinerary_calendar']!=itinerary_calendar(spec):
        missing.append('行程日期或天数已变更，请确认入住退房范围是否仍然适用')
    if spec.get('hotel_preferences',{}).get('same_hotel_for_trip') is False:missing.append('单酒店连续住宿范围')
    return missing

