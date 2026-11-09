"""Trip candidates, normalized requirements, planning, validation and read-only evidence."""
from __future__ import annotations
from infrastructure.skill_manager import skill_text, skill_request
import re
import json
import time
import os
import unicodedata
from tools.trip.constraints import apply_constraints, date_range, apply_reference_date, confirm_planning_date, duration_days, intercity_request, apply_date_window, travel_preferences, effective_city_windows
from tools.research.evidence import ResearchSource, prose_supported
from tools.research.sources import INSTRUCTION
from math import asin, cos, radians, sin, sqrt, isfinite
from datetime import date, datetime, timedelta, timezone
from copy import deepcopy
from collections import OrderedDict
from infrastructure.amap_client import AmapError
from core.model_budget import model_call_context
from core.context_contract import complete_json, project_places, response_json, response_text
from core.time_input import number, parse_reminder_time, parse_time_window, format_time_window, BEIJING
from infrastructure.place_resolution import bind_visit_entities
from tools.hotel_prices.catalog import hotel_brand_targets, match_hotel_brand

ACTIVITY_PERIODS = {'上午': (540, 720), '下午': (780, 1020), '晚上': (1080, 1260)}
HOTEL_POLICY_VERSION = 'hotel-v2-budget-brand-evidence'


def planning_context(spec, places, previous=None):
    """Structural planning input; full evidence and recommendations stay in stored state."""
    retained={row.get('poi_id') for row in spec.get('required_entities',{}).values() if isinstance(row,dict)}
    if previous:
        retained.update(a['poi_id'] for day in previous['days'] for item in day['activities']
                        for a in [item,*item.get('subactivities',[])])
    candidates={pid:p for pid,p in places.items() if pid in retained or not
                (HOTEL_PATTERN.search(str(p.get('type',''))) or DINING_PATTERN.search(str(p.get('type',''))))}
    requirements={k:v for k,v in spec.items() if k not in {'source_context','research_context','fact_sources',
        'field_sources','constraint_overrides','planning_decision','research_note','research_candidates',
        'document_ids','media_ids','applied_preferences'}}
    if 'city_windows' in requirements:requirements['city_windows']=effective_city_windows(spec)
    if 'hotel_stay' in requirements:
        from tools.hotel_prices.quotes import public_stay
        requirements['hotel_stay']=public_stay(spec['hotel_stay'])
    requirements['planning_evidence']={pid:[{k:v for k,v in fact.items() if k in {
        'topic','value_raw','status','scope','rule_resolution','date_recheck_required','temporal_rule','date_check','price_basis'}}
        for fact in rows] for pid,rows in spec.get('planning_evidence',{}).items() if pid in candidates}
    requirements['planning_answers']=[row for row in spec.get('planning_answers',[]) if row.get('poi_id') in candidates]
    requirements['activity_materials']={pid:{key:[{k:v for k,v in row.items() if k in {'text','source_ids','kind'}}
        for row in material.get(key,[])] for key in ('intro','tips')}
        for pid,material in spec.get('activity_materials',{}).items() if pid in candidates}
    old=None if previous is None else {'days':[{k:v for k,v in day.items() if k in {'day_index','date','kind'}} |
        {'activities':[{k:v for k,v in a.items() if k in {'poi_id','period','reason','subactivities'}} for a in day['activities']]}
        for day in previous['days']]}
    return {'requirements':requirements,'places':project_places(candidates.values()),'previous_plan':old}


def source_context(spec):
    value = spec.get('research_context')
    return value if isinstance(value, dict) else {}


def bind_context(spec, places):
    """Research facts bind only to uniquely resolved POIs in the requested city."""
    data = source_context(spec)
    if not data:
        return
    if data.get('provider_policy', {}).get('only'):
        raise ValueError('资料限定只用问道，尚未授权地图核验，不能生成已绑定的正式行程。')
    bindings = {}
    from infrastructure.place_resolution import city_name
    scoped={pid:p for pid,p in places.items() if city_name(p.get('cityname') or p.get('city'))==city_name(spec['destination'])}
    resolved=bind_visit_entities([t['name'] for t in data.get('targets',[])],scoped,spec['destination'])
    for target in data.get('targets', []):
        match=resolved[target['name']]
        if match['status']=='bound':bindings[target['id']]=match['poi_id']
    spec['planning_evidence'] = {}
    spec['activity_materials'] = {}
    spec['research_candidates'] = [{'poi_id': bindings[t['id']], 'suggested_day_index': t.get('day_index'),
                                    'kind': 'candidate', 'order': i + 1}
                                   for i, t in enumerate(data.get('targets', [])) if t['id'] in bindings]
    spec['planning_answers'] = [{**row,'poi_id':bindings[row['target_id']]} for row in data.get('planning_answers',[])
                                if row['target_id'] in bindings]
    for fact in data.get('facts', []):
        pid = bindings.get(fact['target_id'])
        if pid and fact.get('entity_poi_id') in (None, pid):
            spec['planning_evidence'].setdefault(pid, []).append(deepcopy(fact))
    for row in data.get('activity_materials', []):
        pid = bindings.get(row['target_id'])
        if pid and row.get('entity_poi_id') in (None, pid):
            spec['activity_materials'][pid] = deepcopy(row)


def activity_material_state(spec, pid):
    material = spec.get('activity_materials', {}).get(pid)
    if material is not None:
        return material.get('answer_state', 'reference' if material.get('intro') else 'raw_pending')
    return 'raw_pending' if spec.get('research_context', {}).get('omitted_materials') else 'not_queried'


def validate_activity(item, spec, places, day_index):
    pid = item['poi_id']
    day = (date.fromisoformat(spec['start_date']) + timedelta(days=day_index-1)).isoformat() if spec.get('start_date') else ''
    for fact in spec.get('planning_evidence', {}).get(pid, []):
        if (fact['status'] != 'official_verified' or fact['scope'].get('visit_date') != day
                or fact.get('rule_resolution') in {'superseded', 'outside', 'conflict', 'pending'}
                or fact['scope'].get('product', 'general') != 'general'
                or fact['scope'].get('visitor_type') not in {'general', 'personal', 'adult'}):
            continue
        if re.search(r'临时闭馆|暂停开放|当日闭馆|当日不开放', fact['value_raw']):
            raise ValueError('目标日期存在已核验闭馆规则，不能安排该活动。')
        if fact['topic'] == 'opening_hours' and fact['scope'].get('time_role') == 'opening':
            windows = re.findall(r'(\d{1,2})[:：](\d{2})\s*[-—~～至]\s*(\d{1,2})[:：](\d{2})', fact['value_raw'])
            intervals = [(int(h1)*60+int(m1), int(h2)*60+int(m2)) for h1,m1,h2,m2 in windows
                         if int(h1) < 24 and int(h2) <= 24 and int(m1) < 60 and int(m2) < 60]
            period = {'上午': (360, 720), '下午': (720, 1080), '晚上': (1080, 1440)}[item['period']]
            # Cross-midnight or complex clauses remain pending rather than being guessed.
            if intervals and all(start < finish for start, finish in intervals) and not any(
                    max(start, period[0]) < min(finish, period[1]) for start, finish in intervals):
                raise ValueError('活动时段与目标日期已核验的营业区间不相交。')
    material = spec.get('activity_materials', {}).get(pid, {})
    intros = [{**r,'text':re.sub(r'[<>]','',r['text'])} for r in material.get('intro', [])]
    intro = next((r for r in intros if prose_supported(r['text'],intros)), None)
    description = item.get('description')
    if description is not None:
        try:
            validate_description(description, material, spec)
        except ValueError:
            # Stylistic/semantic description failures do not restart planning.
            description = {}
        description = deepcopy(description)
        if intro and not description.get('intro'):
            description['intro'] = {k: intro[k] for k in ('text', 'source_ids')}
        if not description.get('tips'):
            description['tips'] = deepcopy([r for r in material.get('tips',[]) if prose_supported(r['text'],material.get('tips',[]))][:2])
        return {**description, 'source_status': 'provider_unverified'}
    if not intro:
        return None
    tips = deepcopy([r for r in material.get('tips', []) if prose_supported(r['text'],material.get('tips',[]))][:2])
    result = {'intro': {'text': intro['text'], 'source_ids': intro['source_ids']},
              'tips': tips, 'source_status': 'provider_unverified'}
    validate_description(result, material, spec)
    return result


def validate_description(value, material, spec):
    if not isinstance(value, dict) or value.keys() - {'intro', 'why_visit', 'tips', 'source_status'}:
        raise ValueError('活动说明结构无效。')
    intro = value.get('intro')
    if intro:
        if (not isinstance(intro, dict) or intro.keys() != {'text', 'source_ids'}
                or not isinstance(intro.get('text'), str) or not 1 <= len(intro['text']) <= 1000
                or not isinstance(intro.get('source_ids'), list) or any(type(i) is not int for i in intro['source_ids'])):
            raise ValueError('景点介绍超过长度或结构无效。')
        rows = [r for r in material.get('intro', []) if intro.get('source_ids') == r['source_ids']]
        if not prose_supported(intro['text'], rows):
            raise ValueError('景点介绍缺少对应POI的原文支持。')
    why = value.get('why_visit', '')
    if (not isinstance(why, str) or len(why) > 150 or INSTRUCTION.search(why)
            or travel_preferences([why]) != [why]):
        raise ValueError('安排理由无效。')
    # Free explanations may not introduce dates, prices, hours or venue policies.
    if re.search(r'\d|元|票价|开放时间|预约|始建|建于|商家|演出', why):
        raise ValueError('安排理由含有未核实的具体事实。')
    if why and not prose_supported(why, [{'text':'；'.join(spec.get('preferences',[]))}, *material.get('intro',[]), *material.get('tips',[])]):
        raise ValueError('安排理由包含不适用的实用结论。')
    tips = value.get('tips', [])
    if not isinstance(tips, list) or len(tips) > 2:
        raise ValueError('玩法建议最多两项。')
    for tip in tips:
        if (not isinstance(tip, dict) or not isinstance(tip.get('text'), str) or not 1 <= len(tip['text']) <= 600
                or tip.keys() - {'text', 'source_ids', 'kind', 'start', 'end'}
                or not isinstance(tip.get('source_ids'), list) or any(type(i) is not int for i in tip['source_ids'])
                or tip.get('kind') != 'advice' or INSTRUCTION.search(tip['text'])):
            raise ValueError('玩法建议结构或长度无效。')
        if not prose_supported(tip['text'],[r for r in material.get('tips',[]) if tip.get('source_ids')==r['source_ids']]):
            raise ValueError('玩法建议缺少对应材料支持。')


def invalidate_changed_dates(spec, plan):
    for day in plan['days']:
        for activity in day['activities']:
            for fact in spec.get('planning_evidence', {}).get(activity['poi_id'], []):
                if fact['scope'].get('visit_date') != day.get('date'):
                    fact['date_recheck_required'] = True


HOTEL_PATTERN = re.compile(r'住宿服务|酒店|宾馆|旅馆')


DINING_PATTERN = re.compile(r'餐饮服务|餐厅|餐馆|饭店|小吃|快餐|咖啡|茶馆')


CHAIN_BRANDS = (
    '汉庭', '全季', '桔子', '星程', '海友', '如家', '如家商旅', '如家精选', '和颐',
    '锦江之星', '维也纳', '麗枫', '喆啡', '希岸', '7天', '速8', '格林豪泰', '亚朵',
)


MEALS = ('早餐', '午餐', '晚餐', '餐饮')


def _number(value):
    try:
        number = float(value)
        return number if number >= 0 else None
    except (TypeError, ValueError):
        return None


def rating(place):
    return _number(place.get('rating'))


def price_details(place, kind):
    def price(key):
        if type(place.get(key)) is bool:return None
        value=_number(place.get(key))
        return value if value and isfinite(value) and value>0 else None
    if kind == 'hotel':
        from tools.trip.constraints import hotel_comparison_unit
        unit=str(place.get('price_unit') or '')
        average=price('cost') if hotel_comparison_unit(str(place.get('cost_unit') or unit))=='per_room_night_cny' else None
        lowest=price('lowest_price') if hotel_comparison_unit(str(place.get('lowest_price_unit') or unit))=='per_room_night_cny' else None
        result={'average_price':average,'lowest_price':lowest,'basis':'per_room_night_cny' if average or lowest else 'unconfirmed'}
        if price('cost') is not None and average is None:
            result.update(reference_cost=price('cost'),reference_cost_unit=str(place.get('cost_unit') or unit or '人均消费'))
        if price('lowest_price') is not None and lowest is None:
            result.update(unverified_lowest_price=price('lowest_price'),unverified_lowest_price_unit=str(place.get('lowest_price_unit') or unit or '口径未注明'))
        return result
    unit=str(place.get('price_unit') or place.get('cost_unit') or '')
    return {'per_capita': None if unit and not re.search(r'人均|每人|/人|per.capita',unit,re.I) else price('cost')}


def _coordinates(place):
    try:
        longitude, latitude = str(place.get('location') or '').split(',', 1)
        longitude, latitude = float(longitude), float(latitude)
        if -180 <= longitude <= 180 and -90 <= latitude <= 90:
            return longitude, latitude
    except (TypeError, ValueError):
        pass
    return None


def distance_meters(left, right):
    first, second = _coordinates(left), _coordinates(right)
    if first is None or second is None:
        return None
    lon1, lat1, lon2, lat2 = map(radians, (*first, *second))
    dlon, dlat = lon2 - lon1, lat2 - lat1
    value = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return int(round(6371000 * 2 * asin(sqrt(value))))


def place_groups(places):
    """Group visits, preserving every POI and its own admission policy."""
    normalize = lambda text: re.sub(r'[\W_]+','',unicodedata.normalize('NFKC',str(text))).casefold()
    roots = {pid:pid for pid in places}
    def root(pid):
        seen = set()
        while roots[pid]!=pid and pid not in seen:
            seen.add(pid);pid=roots[pid]
        return pid
    for pid,p in places.items():
        parent = p.get('parent_id') or p.get('parent')
        if isinstance(parent,str) and parent in places and root(parent)!=root(pid):
            roots[root(pid)]=root(parent)
        for child in p.get('children',[]) if isinstance(p.get('children'),list) else []:
            cid=child.get('id') if isinstance(child,dict) else child
            if cid in places and root(cid)!=root(pid):roots[root(cid)]=root(pid)
    ordered=sorted(places,key=lambda pid:len(normalize(places[pid]['name'])))
    for i,pid in enumerate(ordered):
        p=places[pid];name=normalize(p['name'])
        for cid in ordered[i+1:]:
            c=places[cid];other=normalize(c['name'])
            if (p.get('cityname') or p.get('city'))!=(c.get('cityname') or c.get('city')):continue
            address=bool(p.get('address')) and normalize(p['address'])==normalize(c.get('address',''))
            distance=distance_meters(p,c)
            duplicate=name==other and (address or distance is not None and distance<=100)
            # This is a visit grouping suggestion, not a claim of shared ticket policy.
            contained=other.startswith(name) and len(other)>len(name) and address and distance is not None and distance<=300
            if (duplicate or contained) and root(pid)!=root(cid):roots[root(cid)]=root(pid)
    return {pid:root(pid) for pid in places}


def _budget_range(spec, scope):
    from tools.trip.constraints import budget_bounds
    budget = spec.get('budgets', {}).get(scope, {})
    text = str(budget.get('value') or '')
    if not re.search(r'\d',text):return None
    try:bounds=budget.get('bounds') or budget_bounds(text,budget.get('source',''))
    except ValueError:return None
    return bounds['lower'],bounds['upper'],{**budget,'bounds':bounds}


def _price_evidence(place, spec, scope, kind):
    from tools.trip.constraints import food_comparison_unit, hotel_comparison_unit
    target = _budget_range(spec, scope)
    details = price_details(place, kind)
    candidate_price = (details.get('average_price') or details.get('lowest_price')
                       if kind == 'hotel' else details.get('per_capita'))
    if target is None:
        return 'not_requested', details, []
    if candidate_price is None:
        if kind=='hotel' and (details.get('reference_cost') is not None or details.get('unverified_lowest_price') is not None):
            return 'reference_only',details,['现有参考金额尚不能与当前房晚预算直接比较；指定日期房价待核验']
        return 'unknown', details, ['房价未知，预算尚未核实；指定日期房价待核验' if kind=='hotel' else '人均消费待核验']
    minimum, maximum, budget = target
    bounds=budget['bounds']
    if bounds['kind']=='approximate':return 'reference_only',details,['预算为近似目标，未据此确认严格达标']
    unit = str(budget.get('unit_text') or '')
    if kind=='hotel' and hotel_comparison_unit(unit)!='per_room_night_cny':
        return 'reference_only',details,['房价参考与住宿预算的币种或计价单位不能直接比较']
    if kind!='hotel' and food_comparison_unit(unit)!='per_person_meal_cny':
        return 'reference_only', details, ['参考消费与餐饮预算单位不能直接比较']
    below=minimum is not None and (candidate_price<minimum or candidate_price==minimum and not bounds['lower_inclusive'])
    above=maximum is not None and (candidate_price>maximum or candidate_price==maximum and not bounds['upper_inclusive'])
    if kind=='hotel' and details.get('average_price') is None and not above:
        return 'reference_only',details,['仅有最低房价参考，不能据此确认所需房型符合预算；指定日期房价待核验']
    status='reference_below_budget' if below else 'reference_over_budget' if above else 'reference_in_budget'
    if kind=='hotel':
        return status,details,[('参考房价低于预算下限' if below else '参考房价超出预算上限' if above else '参考房价落在预算范围')+'；指定日期房价待核验']
    return status,details,[('参考人均低于预算下限' if below else '参考人均超出预算上限' if above else '参考人均符合预算')+'，实际账单待核验']


def hotel_search_terms(spec):
    preferences = spec.get('hotel_preferences', {})
    brands = [row['name'] for row in hotel_brand_targets(preferences)]
    if preferences.get('preferred_groups') and not brands:return []
    terms = [value if re.search(r'酒店|宾馆|旅馆', value) else value + '酒店' for value in brands]
    if preferences.get('chain_mode') == 'required' and not terms:
        terms.extend(value + '酒店' for value in CHAIN_BRANDS[:6])
    if not terms or preferences.get('brand_mode') != 'required':
        terms.append('酒店')
    return list(dict.fromkeys(terms))[:8]


def dining_search_terms(spec):
    preferences = spec.get('dining_preferences', {})
    values = [*preferences.get('preferred_brands', []), *preferences.get('cuisines', []),
              *preferences.get('keywords', [])]
    terms = [str(value).strip() for value in values if str(value).strip()]
    terms = [value if re.search(r'餐厅|餐馆|饭店|小吃|美食', value) else value + '餐厅' for value in terms]
    if not terms or preferences.get('cuisine_mode') != 'required':
        terms.append('餐馆')
    return list(dict.fromkeys(terms))[:8]


def _activity_places(plan, places, day_index=None):
    result = []
    for day in plan.get('days', []):
        if day_index is not None and day.get('day_index') != day_index:
            continue
        for activity in day.get('activities', []):
            place = places.get(activity.get('poi_id'))
            if place:
                result.append((day, activity, place))
    return result


def _hotel_distance_summary(place, plan, places):
    daily = []
    all_distances = []
    for day in plan.get('days', []):
        distances = [distance for _, _, target in _activity_places(plan, places, day.get('day_index'))
                     if (distance := distance_meters(place, target)) is not None]
        if distances:
            daily.append({'day_index': day['day_index'], 'average_meters': round(sum(distances) / len(distances)),
                          'maximum_meters': max(distances)})
            all_distances.extend(distances)
    if not all_distances:
        return {'daily': [], 'average_meters': None, 'maximum_meters': None, 'worst_day_meters': None}
    return {'daily': daily, 'average_meters': round(sum(all_distances) / len(all_distances)),
            'maximum_meters': max(all_distances),
            'worst_day_meters': max(item['maximum_meters'] for item in daily)}


def _contains_any(text, values):
    folded = text.casefold()
    return any(str(value).casefold() in folded for value in values)


def assess_hotels(spec, plan, places):
    preferences = spec.get('hotel_preferences', {})
    targets=hotel_brand_targets(preferences)
    requested=bool(preferences.get('preferred_brands') or preferences.get('preferred_groups'))
    minimum_rating = _number(preferences.get('minimum_rating'))
    candidates = []
    for poi_id, place in places.items():
        if preferences.get('selected_poi_id') and poi_id!=preferences['selected_poi_id']:continue
        if not HOTEL_PATTERN.search(str(place.get('type') or '')):
            continue
        name = str(place.get('name') or '')
        if not name or re.search(r'暂停营业|停车场|入口|出口', name):
            continue
        brand=match_hotel_brand(name,targets)
        matched_brand=bool(brand)
        chain = matched_brand or _contains_any(name, CHAIN_BRANDS)
        current_rating = rating(place)
        star_category=re.search(r'([一二三四五1-5])星级',str(place.get('type','')))
        category_stars=number(star_category[1]) if star_category else None
        issues = []
        hard_failure = False
        if preferences.get('preferred_groups') and not targets:
            issues.append('当前品牌目录尚不能核实指定集团范围');hard_failure=True
        quality_scope=preferences.get('quality_requested') and preferences.get('lodging_types')!=[]
        lodging_types=preferences.get('lodging_types',['酒店'] if quality_scope else [])
        type_match=_contains_any(str(place.get('type','')),lodging_types) if lodging_types else True
        evidence_types=lodging_types if preferences.get('lodging_type_mode')=='required' else [*lodging_types,'酒店']
        if (quality_scope and not _contains_any(str(place.get('type','')),evidence_types)
                or preferences.get('lodging_type_mode')=='required' and not type_match):
            issues.append('住宿类别不符合要求');hard_failure=True
        if preferences.get('brand_mode') == 'required' and requested and not matched_brand:
            issues.append('尚未核实匹配指定酒店品牌或集团')
            hard_failure = True
        if preferences.get('chain_mode') == 'required' and not chain:
            issues.append('未核实为要求的连锁酒店')
            hard_failure = True
        if minimum_rating is not None and (current_rating is None or current_rating < minimum_rating):
            issues.append('评分缺失' if current_rating is None else f'高德评分低于 {minimum_rating:g}')
            hard_failure = hard_failure or preferences.get('rating_mode','required')=='required'
        price_status, price_info, price_issues = _price_evidence(place, spec, 'accommodation', 'hotel')
        from tools.hotel_prices.quotes import evaluate, fingerprint
        quotes=[q for q in evaluate(spec,plan['hotel_quotes'],places=places) if q['amap_poi_id']==poi_id] if plan.get('hotel_quotes') else []
        if quotes:
            matched=[q for q in quotes if q['status'] in {'reference_match','reference_available'}]
            pending=[q for q in quotes if q['status']!='excluded']
            chosen=(matched or pending or quotes)[0]
            price_status=('reference_in_budget' if matched and matched[0]['status']=='reference_match' else 'not_requested' if matched else 'reference_over_budget' if not pending else 'unknown')
            price_issues=list(chosen['issues'])
            if not pending:price_issues.append('仅本批已列产品不匹配，未证明该酒店其他房型均不匹配')
        elif plan.get('hotel_quotes'):
            price_status='unknown'
            price_issues=['房价待核对，查询状态及缺失条件见住宿报价说明']
        if price_status in {'reference_over_budget','reference_below_budget'}:hard_failure=True
        issues.extend(price_issues)
        if matched_brand:
            issues.append('品牌按官方目录及门店名称对照，具体门店归属待确认' if brand.get('source') else '品牌按名称匹配，具体门店归属待确认')
        elif requested:issues.append('未匹配优先品牌或集团，仅作需要放宽品牌偏好的备选')
        if preferences.get('quality_requested'):
            issues.append('住宿品质与设施档次尚缺独立证据；评分不代表档次')
        distance = _hotel_distance_summary(place, plan, places)
        reasons = []
        lodging_type='、'.join(dict.fromkeys(str(place.get('type','')).split(';')))
        if lodging_type:reasons.append('高德住宿类别：'+lodging_type)
        if matched_brand:
            reasons.append('名称匹配偏好品牌')
        elif chain:
            reasons.append('属于已识别连锁品牌')
        if current_rating is not None and (minimum_rating is None or current_rating >= minimum_rating):
            reasons.append(f'高德评分 {current_rating:g}')
        if distance['average_meters'] is not None:
            reasons.append('已比较全部行程日的位置')
        status = 'conflict' if hard_failure else 'fallback' if requested and not matched_brand else 'partial' if issues else 'matched'
        pending_budget=price_status not in {'not_requested','reference_in_budget'}
        # Compare other evidence within 2 km bands; metre-level differences should not decide alone.
        distance_band=(int(distance['worst_day_meters']//2000) if distance['worst_day_meters'] is not None else 10**6) if preferences.get('near_itinerary',True) else 0
        candidates.append({'poi_id': poi_id, 'status': status, 'reasons': reasons, 'issues': issues,
                           'rating': current_rating, 'price_info': price_info,'lodging_type':lodging_type,
                           'category_stars':category_stars,'category_basis':'amap_classification',
                           'price_status': price_status, 'distance': distance,'brand_match':matched_brand,
                           **({'quote_offers':quotes,'quote_evaluation_fingerprint':fingerprint({
                               'request':plan['hotel_quotes'].get('request_id'),'retrieved_at':plan['hotel_quotes'].get('retrieved_at'),
                               'offers':[(q['offer_id'],q['status'],q['numeric_comparison']) for q in quotes],
                               'budget':spec.get('budgets',{}).get('accommodation'),'preferences':preferences,'distance':distance})} if quotes else {}),
                           'brand_reference':brand,
                           '_sort': (3 if hard_failure else 2 if status=='fallback' else 1 if pending_budget else 0,
                                     0 if matched_brand or not requested else 1,
                                     distance_band,
                                     0 if type_match else 1,
                                     -(category_stars or 0) if preferences.get('quality_requested') else 0,
                                     0 if chain and preferences.get('chain_mode')=='preferred' else 1,
                                     -(current_rating or 0),
                                     distance['worst_day_meters'] if distance['worst_day_meters'] is not None else 10**9,
                                     distance['average_meters'] if distance['average_meters'] is not None else 10**9,name)})
    candidates.sort(key=lambda item: item['_sort'])
    for item in candidates:
        item.pop('_sort', None)
    return candidates


def rank_hotels(spec, plan, places):
    return [row for row in assess_hotels(spec,plan,places) if row['status']!='conflict'][:3]


def _meal_anchors(day, places, meal):
    activities = sorted(day.get('activities', []), key=lambda item: ('上午', '下午', '晚上').index(item['period']))
    resolved = [(item, places.get(item.get('poi_id'))) for item in activities]
    resolved = [(item, place) for item, place in resolved if place]
    if not resolved:
        return []
    if meal == '早餐':
        return [resolved[0][1]]
    if meal == '午餐':
        before = [place for item, place in resolved if item['period'] == '上午']
        after = [place for item, place in resolved if item['period'] in {'下午', '晚上'}]
        return ([before[-1]] if before else []) + ([after[0]] if after else [])
    return [resolved[-1][1]]


def _restaurant_distance(place, anchors):
    distances = [distance for anchor in anchors if (distance := distance_meters(place, anchor)) is not None]
    return {'average_meters': round(sum(distances) / len(distances)) if distances else None,
            'maximum_meters': max(distances) if distances else None}


def _rank_restaurants(spec, places, anchors):
    preferences = spec.get('dining_preferences', {})
    brands = preferences.get('preferred_brands', [])
    cuisines = [*preferences.get('cuisines', []), *preferences.get('keywords', [])]
    minimum_rating = _number(preferences.get('minimum_rating'))
    avoid = preferences.get('avoid', [])
    candidates = []
    for poi_id, place in places.items():
        if not DINING_PATTERN.search(str(place.get('type') or '')):
            continue
        name = str(place.get('name') or '')
        if not name or re.search(r'暂停营业|停车场|入口|出口', name):
            continue
        evidence_text = ' '.join((name, str(place.get('type') or ''), str(place.get('queried_keyword') or '')))
        brand_match = _contains_any(name, brands) if brands else False
        cuisine_match = _contains_any(evidence_text, cuisines) if cuisines else False
        current_rating = rating(place)
        issues = []
        hard_failure = False
        if preferences.get('brand_mode') == 'required' and brands and not brand_match:
            issues.append('不属于指定餐饮品牌')
            hard_failure = True
        if preferences.get('cuisine_mode') == 'required' and cuisines and not cuisine_match:
            issues.append('没有所需菜系或特色的检索证据')
            hard_failure = True
        conflicts = [value for value in avoid if value and str(value) in evidence_text]
        if conflicts:
            issues.append('名称或分类与忌口冲突：' + '、'.join(conflicts))
            hard_failure = True
        if minimum_rating is not None and (current_rating is None or current_rating < minimum_rating):
            issues.append('评分缺失' if current_rating is None else f'高德评分低于 {minimum_rating:g}')
            hard_failure = hard_failure or preferences.get('rating_mode','required')=='required'
        if preferences.get('avoid') or preferences.get('allergies'):
            issues.append('忌口或配料仍需向商家确认')
        if hard_failure:continue
        price_status, price_info, price_issues = _price_evidence(place, spec, 'food', 'dining')
        if _budget_range(spec,'food') and price_status!='reference_in_budget':continue
        issues.extend(price_issues)
        distance = _restaurant_distance(place, anchors)
        reasons = []
        if brand_match:
            reasons.append('匹配指定餐饮品牌')
        if cuisine_match:
            reasons.append('匹配菜系或特色关键词')
        if current_rating is not None and (minimum_rating is None or current_rating >= minimum_rating):
            reasons.append(f'高德评分 {current_rating:g}')
        if distance['average_meters'] is not None:
            reasons.append('靠近本餐前后行程地点')
        status = 'fallback' if hard_failure else ('partial' if issues else 'matched')
        candidates.append({'poi_id': poi_id, 'status': status, 'reasons': reasons, 'issues': issues,
                           'rating': current_rating, 'price_info': price_info,
                           'price_status': price_status, 'distance': distance,
                           '_sort': (2 if status == 'fallback' else 1 if status == 'partial' else 0,
                                     0 if brand_match or cuisine_match else 1,
                                     0 if price_status == 'reference_in_budget' else 1,
                                     distance['average_meters'] if distance['average_meters'] is not None else 10**9,
                                     -(current_rating or 0), name)})
    candidates.sort(key=lambda item: item['_sort'])
    for item in candidates:
        item.pop('_sort', None)
    return candidates


def meal_scopes(spec,plan,scopes=()):
    """City meals need usable time, including first/last days and rest days."""
    if not scopes:
        scopes=spec.get('dining_preferences',{}).get('scopes',[]) or [
            {'day_index':item['day_index'],'meal':item['meal']} for item in plan.get('dining_options',[])]
    if not scopes:
        scopes=[{'day_index':day['day_index'],'meal':meal} for day in plan.get('days',[])
                for meal in ('早餐','午餐','晚餐') if day.get('kind')!='transport' or str(day['day_index']) in spec.get('intercity_windows',{})]
    result=[]
    days={day['day_index']:day for day in plan.get('days',[])}
    for scope in scopes:
        day_index, meal = scope.get('day_index'), scope.get('meal', '餐饮')
        day = days.get(day_index)
        if day is None or meal not in MEALS:
            continue
        window=spec.get('intercity_windows',{}).get(str(day_index),{})
        start,end={'早餐':(7,10),'午餐':(11,14),'晚餐':(17,21),'餐饮':(0,24)}[meal]
        earliest=window.get('earliest');latest=window.get('latest')
        bound=spec.get('planning_window',{})
        if not earliest and day.get('date')==bound.get('start') and bound.get('start_window'):
            earliest=(datetime.fromisoformat(day['date']).replace(tzinfo=BEIJING)+timedelta(hours=bound['start_window'][1])).isoformat()
        if not latest and day.get('date')==bound.get('end') and bound.get('end_window'):
            latest=(datetime.fromisoformat(day['date']).replace(tzinfo=BEIJING)+timedelta(hours=bound['end_window'][0])).isoformat()
        if day.get('date'):
            earliest=earliest or day['date']+'T00:00:00+08:00'
            latest=latest or day['date']+'T23:59:59+08:00'
        if day.get('date') and earliest and latest:
            midnight=datetime.fromisoformat(day['date']).replace(tzinfo=BEIJING)
            if min(datetime.fromisoformat(latest),midnight+timedelta(hours=end))-max(datetime.fromisoformat(earliest),midnight+timedelta(hours=start))<timedelta(minutes=45):continue
        result.append({'day_index':day_index,'meal':meal,'date':day.get('date','')})
    return result


def recommend_dining(spec, plan, places):
    from tools.trip.constraints import food_comparison_unit
    preferences=spec.get('dining_preferences',{})
    scopes=meal_scopes(spec,plan,preferences.get('scopes',[]))
    options=[]
    days={day['day_index']:day for day in plan.get('days',[])}
    for scope in scopes:
        day_index,meal=scope['day_index'],scope['meal']
        day=days[day_index]
        scoped={**preferences,**preferences.get('meal_overrides',{}).get(f'{day_index}:{meal}',{})}
        food=spec.get('budgets',{}).get('food',{})
        budget={**food,**food.get('meals',{}).get(f'{day_index}:{meal}',{})}
        ranked = _rank_restaurants({**spec,'dining_preferences':scoped,'budgets':{**spec.get('budgets',{}),'food':budget}}, places, _meal_anchors(day, places, meal))[:3]
        note='该餐暂无有足够价格与条件证据的匹配餐馆，保留用餐区域与预算方向；可选择是否放宽。'
        status='no_verified_match'
        if _budget_range({'budgets':{'food':budget}},'food') and food_comparison_unit(budget.get('unit_text',''))!='per_person_meal_cny':
            status='incomparable_budget';note='预算口径与参考人均不能直接比较，未自动换算，也未认定超预算；保留用餐方向与待核实要求。'
        elif budget.get('bounds',{}).get('kind')=='approximate':
            status='approximate_budget';note='预算为近似目标，未擅自设定浮动范围或认定严格达标；保留用餐方向。'
        options.append({'day_index': day_index, 'date':day.get('date',''),'meal': meal, 'candidates': ranked,
            **({'note':note,'price_check_status':status} if not ranked else {})})
    return [], options


def apply_recommendations(spec, plan, places):
    hotel_recommendations = rank_hotels(spec, plan, places) if spec.get('hotel_requested') else []
    restaurant_recommendations, dining_options = (recommend_dining(spec, plan, places)
                                                   if spec.get('restaurant_requested') else ([], []))
    plan['hotel_recommendations'] = hotel_recommendations
    plan['hotel_ids'] = [item['poi_id'] for item in hotel_recommendations]
    plan['hotel_recommendation_policy']=HOTEL_POLICY_VERSION
    plan.pop('hotel_review_note',None)
    plan['restaurant_recommendations'] = restaurant_recommendations
    plan['restaurant_ids'] = [item['poi_id'] for item in restaurant_recommendations]
    plan['dining_options'] = dining_options
    return plan


REASONS = {'兴趣匹配', '室内备选', '文化参观', '户外休闲', '用餐休整', '节奏舒缓'}


class TripPlanError(ValueError):
    def __init__(self,message,*,code='invalid_plan',path=''):
        self.code=code;self.path=path
        super().__init__(message)


def indoor_place(place):
    return bool(re.search(r'博物馆|美术馆|图书馆|科技馆|展览馆|商场|文化宫', place.get('type', '')))


def outdoor_place(place):
    return not indoor_place(place) and bool(re.search(r'公园|广场|自然景观', place.get('type', '')))


def fresh_route(leg,now=None):
    if leg.get('date_recheck_required'):return False
    try:
        stamp=datetime.fromisoformat(leg.get('queried_at',''))
        return stamp.tzinfo is not None and 0 <= ((now or datetime.now(timezone.utc))-stamp).total_seconds() <= 300
    except (ValueError,TypeError):
        return False


class TripPlanner:
    @staticmethod
    def intercity_legs(spec, now):
        request=spec.get('intercity_request',{})
        if not request or request.get('scope')=='none':
            return {}
        origin=request.get('origin','')
        address=request.get('origin_scope')=='address'
        if address:origin=request.get('origin_resolution',{}).get('city','')
        destination=spec.get('destination','')
        if origin.removesuffix('市')==destination.removesuffix('市') and origin:
            return {}
        scope=request.get('scope','round_trip')
        result={}
        for direction in ('outbound','inbound') if scope=='round_trip' else ('outbound',):
            part=request.get(direction,{})
            explicit=part.get('date_text','')
            date_error=''
            day=part.get('resolved_date','') if explicit else ''
            if explicit and not day:
                parsed=parse_reminder_time(explicit,now)
                day=parsed.day if not parsed.error else ''
                date_error=parsed.error or ''
                if day:part['resolved_date']=day;part['date_reference_time']=now.isoformat()
            bound=spec.get('planning_window',{})
            if not day and not explicit and bound:
                day=bound['start' if direction=='outbound' else 'end']
            if not day and not explicit and not spec.get('date_is_tentative') and request.get('day_count_basis')!='destination_days':
                start=spec.get('start_date')
                day=(date.fromisoformat(start)+timedelta(days=spec['day_count']-1)).isoformat() if start and direction=='inbound' else start
            window=part.get('departure_window','')
            arrival_window=part.get('arrival_window','')
            time_reference=part.get('time_reference','')
            if time_reference=='ambiguous':window=arrival_window=''
            if not window and not arrival_window and explicit and time_reference!='ambiguous':
                try:
                    derived=parse_time_window(explicit,dated=True)
                    if derived:
                        if derived[0]==derived[1]:derived=(derived[0],min(24,derived[1]+1/60))
                        if time_reference=='arrival' or direction=='inbound' and bound.get('end_event')=='arrival':arrival_window=format_time_window(derived)
                        else:window=format_time_window(derived)
                except ValueError:
                    window=explicit
            if not window and not arrival_window and not explicit and bound and time_reference!='ambiguous':
                derived=bound.get('start_window' if direction=='outbound' else 'end_window')
                if derived:
                    if direction=='inbound' and bound.get('end_event')=='arrival':
                        arrival_window=format_time_window((0,derived[1]) if derived[0]==derived[1] else derived)
                    else:
                        window=format_time_window((derived[0],24) if derived[0]==derived[1] else derived)
            result[direction]={'origin':origin if direction=='outbound' else destination,
                'destination':destination if direction=='outbound' else part.get('destination',origin),
                'origin_scope':('city' if address else request.get('origin_scope','station' if origin.endswith('站') else 'city')) if direction=='outbound' else 'city',
                'destination_scope':'city' if direction=='outbound' else part.get('destination_scope','city' if address else request.get('origin_scope','station' if origin.endswith('站') else 'city')),
                'travel_date':day or '', 'mode':part.get('mode','rail'),
                'departure_window':window,'arrival_window':arrival_window,
                'train_preference':part.get('train_preference','high_speed'),
                'train_preference_source':'user' if part.get('train_preference') else 'default',
                'date_source':'user' if explicit else 'default_end_date' if direction=='inbound' else 'confirmed_trip_date',
                'mode_source':'user' if part.get('mode') else 'default',
                'arrival_before':part.get('arrival_before','')}
            if time_reference:result[direction]['time_reference']=time_reference
            result[direction]['preferred_train']=part.get('preferred_train','')
            if date_error:result[direction]['date_error']=date_error
            result[direction].update({key:part[key] for key in ('seat_class','seat_requirement','price_preference') if key in part})
            result[direction]['calendar_binding']=[spec.get('start_date'),spec.get('day_count')]
        bound=spec.get('planning_window',{})
        if bound and result.get('outbound',{}).get('travel_date') and result.get('inbound',{}).get('travel_date'):
            start_text=request.get('outbound',{}).get('date_text') or bound['start_text']
            end_text=request.get('inbound',{}).get('date_text') or bound['end_text']
            interval=date_range('',now,start_text=start_text,end_text=end_text,
                resolved_start=result['outbound']['travel_date'],resolved_end=result['inbound']['travel_date'])
            for direction,key in (('outbound','start_window'),('inbound','end_window')):
                leg=result[direction]
                raw=leg.get('arrival_window') or leg.get('arrival_before') or leg.get('departure_window')
                event='arrival' if leg.get('arrival_window') or leg.get('arrival_before') else 'departure'
                interval['start_event' if direction=='outbound' else 'end_event']=event
                if raw:
                    try:window=parse_time_window(raw)
                    except ValueError:continue
                    if window!=interval[key]:
                        interval[key]=window
                        interval['start_text' if direction=='outbound' else 'end_text']=result[direction]['travel_date']+' '+raw
            apply_date_window(spec,interval)
        if not spec.get('planning_window') and request.get('day_count_basis')=='destination_days' and all(r['travel_date'] for r in result.values()) and spec.get('start_date'):
            tour_start=date.fromisoformat(spec.get('tour_start_date',spec['start_date']))
            tour_days=spec.get('destination_day_count',spec['day_count'])
            tour_end=tour_start+timedelta(days=tour_days-1)
            begin=min(tour_start,date.fromisoformat(result['outbound']['travel_date']))
            end=max(tour_end,date.fromisoformat(result.get('inbound',result['outbound'])['travel_date']))
            total=(end-begin).days+1
            if total>14:raise TripPlanError('含交通日后超过14天，请分段安排。')
            spec.update(tour_start_date=tour_start.isoformat(),destination_day_count=tour_days,
                        start_date=begin.isoformat(),end_date=end.isoformat(),day_count=total)
            spec['transport_days']=[i+1 for i in range(total) if not tour_start<=begin+timedelta(days=i)<=tour_end]
        if 'city_windows' in spec:spec['city_windows']=effective_city_windows(spec)
        return result

    @staticmethod
    def calendar_matches(spec, plan):
        days=plan.get('days',[])
        if not spec.get('start_date') or len(days)!=spec.get('day_count'):return False
        start=date.fromisoformat(spec['start_date'])
        return all(day.get('day_index')==index and day.get('date')==(start+timedelta(days=index-1)).isoformat()
                   for index,day in enumerate(days,1))

    @staticmethod
    def transport_windows(spec, result):
        if not spec.get('start_date'):return {}
        city_windows=effective_city_windows(spec)
        assumptions=result.get('assumptions',{'arrival_buffer_minutes':30,'departure_buffer_minutes':60,'unknown_transfer_minutes':60})
        windows={}
        for index in range(1,spec['day_count']+1):
            day=date.fromisoformat(spec['start_date'])+timedelta(days=index-1)
            start=datetime.combine(day,datetime.min.time(),BEIJING)
            earliest,latest=start,start+timedelta(days=1)
            limit=spec.get('planning_window',{})
            if limit:
                earliest=max(earliest,datetime.fromisoformat(limit['start']).replace(tzinfo=BEIJING)+timedelta(hours=(limit.get('start_window') or (0,24))[0]))
                latest=min(latest,datetime.fromisoformat(limit['end']).replace(tzinfo=BEIJING)+timedelta(hours=(limit.get('end_window') or (0,24))[1]))
            for kind,direction in (('arrival','outbound'),('departure','inbound')):
                city=city_windows.get(kind)
                if not city:continue
                city_day=datetime.combine(date.fromisoformat(spec['start_date'])+timedelta(days=city['day_index']-1),datetime.min.time(),BEIJING)
                period=parse_time_window(city['time_text'])
                selected=bool(result.get(direction,{}).get('selected'))
                if kind=='arrival':earliest=max(earliest,city_day+timedelta(hours=period[0 if selected else 1]))
                else:latest=min(latest,city_day+timedelta(hours=period[1 if selected else 0]))
            for direction in ('outbound','inbound'):
                leg=result.get(direction,{})
                selected=leg.get('selected')
                if not selected:continue
                transfer=result.get('station_transfers',{}).get(direction,{})
                duration=transfer.get('duration_minutes',assumptions['unknown_transfer_minutes'])
                if direction=='outbound':
                    earliest=max(earliest,datetime.fromisoformat(selected['arrival_at'])+timedelta(minutes=duration+assumptions['arrival_buffer_minutes']))
                else:
                    latest=min(latest,datetime.fromisoformat(selected['departure_at'])-timedelta(minutes=duration+assumptions['departure_buffer_minutes']))
            allowed=[name for name,(a,b) in ACTIVITY_PERIODS.items() if (name!='晚上' or not spec.get('no_night'))
                     and (min(latest,start+timedelta(minutes=b))-max(earliest,start+timedelta(minutes=a))).total_seconds()>=90*60]
            windows[str(index)]={'date':day.isoformat(),'earliest':earliest.isoformat(timespec='minutes'),
                'latest':latest.isoformat(timespec='minutes'),'allowed_periods':allowed,'precision':'period_reference'}
        return windows

    @classmethod
    def choose_transport(cls,spec,result,*,activity_counts=None):
        from itertools import product
        from core.rail_contract import select_seat
        directions=[key for key in ('outbound','inbound') if result.get(key,{}).get('candidates')]
        if not spec.get('start_date'):
            result.setdefault('limitations',[]).append('游览日期尚未确认；车次仅作候选，未确认活动衔接。')
            return
        if not directions:return
        end=date.fromisoformat(spec['start_date'])+timedelta(days=spec['day_count']-1)
        choices=[];unavailable_days=set()
        for combination in product(*([result[d]['selected']] if result[d].get('locked') and result[d].get('selected')
                                     else result[d]['candidates'][:6] for d in directions)):
            trial=deepcopy(result)
            for direction,row in zip(directions,combination):trial[direction]['selected']=row
            inbound=trial.get('inbound',{}).get('selected')
            if inbound and spec.get('intercity_request',{}).get('day_count_basis','travel_included')=='travel_included' and datetime.fromisoformat(inbound['arrival_at']).date()>end:
                continue
            outbound=trial.get('outbound',{}).get('selected')
            bound=spec.get('planning_window',{})
            if bound:
                earliest=datetime.fromisoformat(bound['start']).replace(tzinfo=BEIJING)+timedelta(hours=(bound.get('start_window') or (0,24))[0])
                latest=datetime.fromisoformat(bound['end']).replace(tzinfo=BEIJING)+timedelta(hours=(bound.get('end_window') or (0,24))[1])
                start_field='arrival_at' if bound.get('start_event')=='arrival' else 'departure_at'
                if outbound and datetime.fromisoformat(outbound[start_field])<earliest:continue
                end_field='departure_at' if bound.get('end_event')=='departure' else 'arrival_at'
                if inbound and datetime.fromisoformat(inbound[end_field])>=latest:continue
            if outbound and spec.get('intercity_request',{}).get('day_count_basis','travel_included')=='travel_included' and datetime.fromisoformat(outbound['departure_at']).date()<date.fromisoformat(spec['start_date']):continue
            if inbound and outbound and datetime.fromisoformat(inbound['departure_at'])<=datetime.fromisoformat(outbound['arrival_at']):continue
            windows=cls.transport_windows(spec,trial)
            blocked={day for day,count in (activity_counts or {}).items() if len(windows.get(str(day),{}).get('allowed_periods',[]))<count}
            if blocked:
                unavailable_days.update(blocked);continue
            usable=sum(len(w['allowed_periods']) for w in windows.values())
            duration=sum(row['duration_minutes'] for row in combination)
            # Prefer useful touring periods, then short journeys; do not hardcode earliest/latest trains.
            seats=[select_seat(row,trial[direction].get('request',{})) for direction,row in zip(directions,combination)]
            stock_penalty=sum(seat is None for direction,seat in zip(directions,seats) if trial[direction].get('status')!='provided')
            seat_penalty=sum(bool(trial[d].get('request',{}).get('seat_class') and
                (seat or {}).get('seat_class')!=trial[d]['request']['seat_class']) for d,seat in zip(directions,seats))
            prices=[(seat or {}).get('price_cny') for d,seat in zip(directions,seats)
                    if trial[d].get('request',{}).get('price_preference')=='lower_price']
            price_rank=(sum(p is None for p in prices),sum(p for p in prices if p is not None)) if prices else ()
            stable=tuple((row['departure_at'],row['train_code'],row['origin_station'],row['destination_station']) for row in combination)
            choices.append(((stock_penalty,seat_penalty,*price_rank,-usable,duration,stable),combination,windows))
        if not choices:
            incomplete=any(result[d].get('candidate_truncated') for d in directions)
            result['completeness']='partial' if incomplete else 'conflict'
            message=('当前候选覆盖不完整，尚未找到能衔接活动的组合，不能据此判断整趟行程不可行。' if incomplete
                     else '当前候选车次无法同时满足旅行日期、时间约束和已有活动，请调整交通或活动安排。')
            result['issues']=[{'code':'candidate_coverage_incomplete' if incomplete else 'transport_conflict',
                'direction':'outbound' if day==1 else 'inbound' if day==spec['day_count'] else 'journey',
                'day_index':day,'path':f'days[{day-1}].activities' if day else 'intercity',
                'message':(f'第{day}天：' if day else '')+message} for day in sorted(unavailable_days) or [0]]
            result.setdefault('limitations',[]).extend(issue['message'] for issue in result['issues'])
            return
        result['completeness']='partial';result.pop('issues',None)
        _,chosen,windows=min(choices,key=lambda item:item[0])
        for direction,row in zip(directions,chosen):
            result[direction]['selected']=deepcopy(row)
            result[direction]['selected_seat']=select_seat(row,result[direction].get('request',{}))
            if row.get('queried_at'):result[direction]['queried_at']=row['queried_at']
        for direction in directions:
            leg=result[direction];seats=leg['selected'].get('seats',[])
            if leg.get('status')=='completed' and not any(s.get('availability')=='available' for s in seats):
                leg['status']='sold_out' if seats and all(s.get('availability') in {'sold_out','waitlist'} for s in seats) else 'stock_unknown'
        outbound=result.get('outbound',{}).get('selected')
        inbound=result.get('inbound',{}).get('selected')
        if outbound and inbound:
            result['hotel_nights']=max(0,(date.fromisoformat(inbound['departure_at'][:10])-date.fromisoformat(outbound['arrival_at'][:10])).days)
        inbound=result.get('inbound',{}).get('selected')
        if inbound and not spec.get('planning_window') and spec.get('intercity_request',{}).get('day_count_basis')=='destination_days':
            arrival=date.fromisoformat(inbound['arrival_at'][:10])
            if arrival>end:
                count=(arrival-date.fromisoformat(spec['start_date'])).days+1
                if count>14:raise TripPlanError('返程跨日后超过14天，请分段规划。')
                spec['day_count']=count;spec['end_date']=arrival.isoformat()
                windows=cls.transport_windows(spec,result)
        result['availability_windows']=windows
        spec['intercity_windows']=windows
        spec['transport_days']=sorted(set(spec.get('transport_days',[]))|{int(i) for i,w in windows.items() if not w['allowed_periods']})

    def connect_transport(self,plan,spec,places,result,previous=None):
        """Refine station access with the existing map client; unknown routes remain assumptions."""
        if result.get('completeness')=='not_required':return
        if not spec.get('start_date'):
            result.setdefault('limitations',[]).append('日期未确认，未查询实时车票，城市安排仅供参考。')
            return
        if not self.calendar_matches(spec,plan):
            raise TripPlanError('活动日期与当前旅行范围不一致，请按新日期重新生成行程预览。',code='calendar_conflict',path='days')
        transfers=result.setdefault('station_transfers',{})
        address=result.get('origin_address')
        if address and self.amap:
            for direction,source,target in (('origin_access',address,result.get('outbound',{}).get('selected',{}).get('origin_station')),
                                             ('home_return',result.get('inbound',{}).get('selected',{}).get('destination_station'),address)):
                if not source or not target:continue
                if direction=='home_return' and result.get('inbound',{}).get('request',{}).get('destination')!=spec['intercity_request'].get('origin_resolution',{}).get('city'):continue
                city=spec['intercity_request'].get('origin_resolution',{}).get('city','')
                binding=[source,target,city]
                saved=(previous or {}).get('station_transfers',{}).get(direction,{})
                if saved.get('binding')==binding and saved.get('status')=='known':transfers[direction]=deepcopy(saved);continue
                try:
                    route=self.amap.non_driving_route(source,target,mode='transit',city=city)
                    seconds=route.get('duration_seconds')
                    if type(seconds) in (int,float) and seconds>=0:
                        transfers[direction]={'status':'known','binding':binding,'duration_minutes':int((seconds+59)//60),'provider':'amap'}
                except Exception as error:
                    from core.execution_scope import ExecutionRevoked
                    if isinstance(error,ExecutionRevoked):raise
                    transfers[direction]={'status':'unknown','binding':binding}
            out=result.get('outbound',{}).get('selected');back=result.get('inbound',{}).get('selected')
            access=transfers.get('origin_access',{});home=transfers.get('home_return',{})
            begin=date.fromisoformat(spec['start_date']);finish=begin+timedelta(days=spec['day_count']-1)
            if (out and access.get('status')=='known' and (datetime.fromisoformat(out['departure_at'])-timedelta(minutes=access['duration_minutes']+result['assumptions']['departure_buffer_minutes'])).date()<begin
                    or back and home.get('status')=='known' and (datetime.fromisoformat(back['arrival_at'])+timedelta(minutes=home['duration_minutes']+result['assumptions']['arrival_buffer_minutes'])).date()>finish):
                result['completeness']='conflict';result.setdefault('limitations',[]).append('已查门到站接驳跨出当前总行程日期，请调整交通或延长行程。')
        activities=[(day,a) for day in plan['days'] for a in day['activities']]
        if not activities:return
        mode='walking' if spec.get('transport_preferences',{}).get('preferred')==['walking'] else 'transit'
        for direction in ('outbound','inbound'):
            leg=result.get(direction,{})
            train=leg.get('selected')
            if not train:continue
            _,activity=activities[0] if direction=='outbound' else activities[-1]
            place=places.get(activity['poi_id'],{})
            station=train['destination_station'] if direction=='outbound' else train['origin_station']
            endpoints=(station,place.get('name','')) if direction=='outbound' else (place.get('name',''),station)
            identity=[*endpoints,place.get('location'),mode,spec.get('destination')]
            old=(previous or {}).get('station_transfers',{}).get(direction,{})
            if old.get('binding')==identity and old.get('status')=='known':
                transfers[direction]=deepcopy(old);continue
            row={'status':'unknown','binding':identity,'assumed_minutes':result['assumptions']['unknown_transfer_minutes']}
            if self.amap and all(endpoints):
                try:
                    route=self.amap.non_driving_route(*endpoints,mode=mode,city=spec['destination'])
                    seconds=route.get('duration_seconds')
                    if type(seconds) in (int,float) and seconds>=0:
                        row.update(status='known',duration_minutes=int((seconds+59)//60),provider='amap',
                                   queried_at=datetime.now(timezone.utc).isoformat())
                except Exception as error:
                    from core.execution_scope import ExecutionRevoked
                    if isinstance(error,ExecutionRevoked):raise
                    row['error_type']=type(error).__name__
            transfers[direction]=row
        windows=self.transport_windows(spec,result)
        result['availability_windows']=windows;spec['intercity_windows']=windows
        # One bounded local repair: move only unbound periods within the same day.
        candidate=deepcopy(plan);changed=False
        for day in candidate['days']:
            allowed=windows[str(day['day_index'])]['allowed_periods']
            used={a['period'] for a in day['activities'] if a['period'] in allowed}
            for activity in day['activities']:
                if activity['period'] not in allowed:
                    free=next((period for period in allowed if period not in used),None)
                    if free:activity['period']=free;used.add(free);changed=True
        try:
            clean=self.validate_plan(candidate,spec,places)
            if changed:
                plan['days']=clean['days'];result['repair']='local_periods'
                anchors=[a['poi_id'] for day in plan['days'] for a in day['activities']]
                for direction,poi in (('outbound',anchors[0]),('inbound',anchors[-1])) if anchors else ():
                    transfer=transfers.get(direction,{})
                    endpoint=places.get(poi,{}).get('name')
                    if transfer.get('binding') and endpoint not in transfer['binding']:
                        transfer['status']='unknown';transfer.pop('duration_minutes',None)
        except TripPlanError as error:
            result['completeness']='conflict'
            match=re.search(r'days\[(\d+)\]',error.path or '')
            day_index=int(match[1])+1 if match else None
            direction=('outbound' if day_index==1 else 'inbound' if day_index==spec['day_count'] else 'journey')
            message=(f'第{day_index}天：' if day_index else '')+str(error)+' 未自动删减必去地点。'
            result.setdefault('issues',[]).append({'code':error.code,'path':error.path,'day_index':day_index,
                'direction':direction,'message':message})
            result.setdefault('limitations',[]).append(message)
        if result.get('completeness')!='conflict':
            result['completeness']='partial' if any(leg.get('status') not in {'completed','provided','not_required'} or
                leg.get('status') in {'completed','provided'} and not leg.get('selected') for key,leg in result.items()
                if key in {'outbound','inbound'}) or any(row.get('status')!='known' for row in transfers.values()) else 'reference_ready'
        result.setdefault('limitations',[]).append('活动按上午/下午/晚上参考；接驳为查询时路网，缓冲为规划假设，未核验全部营业和预约场次。')

    def __init__(self, client, model, amap):
        self.client = client
        self.model = model
        self.amap = amap
        self._place_cache = OrderedDict()


    def route_evidence(self, origin_id, destination_id, places, spec, mode, *, budget, now=None):
        """Bounded route attempt with explicit no-route results and permitted alternatives."""
        stamp=(now or datetime.now(timezone.utc)).isoformat()
        base={'from_poi':origin_id,'to_poi':destination_id,'mode':mode,'queried_at':stamp}
        if origin_id==destination_id:
            return {**base,'status':'same_place','duration_seconds':0,'distance_meters':0,
                    'instructions':('同一POI，无跨地点交通段；内部步行及出入口仍需核对。',)}
        preference=spec.get('transport_preferences',{});forbidden=preference.get('forbidden',[])
        if mode in forbidden:return {**base,'status':'unavailable','error_code':'mode_forbidden','error':'当前偏好禁止该交通方式。'}
        walking=('walking' in preference.get('preferred',[]) or any('步行' in clause and not
            re.search(r'不|别|避免|禁止',clause) for clause in re.split(r'[，,。；;]',spec.get('transport_text',''))))
        modes=[mode]+(['walking'] if mode=='transit' and walking and 'walking' not in forbidden else [])
        for selected_mode in modes:
            if budget['remaining']<=0:
                return {**base,'status':'unavailable','error_code':'query_limit','error':'本轮路线查询额度已用完，仍待确认。'}
            budget['remaining']-=1
            try:
                result=self.amap.route_for_pois(places[origin_id],places[destination_id],mode=selected_mode,city=spec['destination'])
            except AmapError as error:
                if error.code=='route_not_found' and selected_mode!=modes[-1]:continue
                return {**base,'status':'no_route' if error.code=='route_not_found' else 'unavailable',
                        'error_code':error.code or 'route_unavailable','error':str(error)}
            if selected_mode!=mode and result.get('duration_seconds',0)>(1200 if spec.get('gentle') else 1800):
                return {**base,'status':'unavailable','error_code':'walking_too_long',
                        'error':'替代步行路线超过当前节奏的参考上限，未自动采用。'}
            return {**base,**result,'mode':selected_mode,'status':'known',
                    **({'fallback_from':mode} if selected_mode!=mode else {})}

    @skill_request
    def extract(self, text, previous, now, source_text=''):
        if self.client is None:
            raise TripPlanError('行程规划需要已配置的模型服务；天气与明确时间的提醒仍可使用。')
        with model_call_context('trip_requirement_extract', prompt_version='trip-requirements-v11-hotel-stay', stage_id='trip.requirements'):
            response = self.client.chat.completions.create(model=self.model,
                response_format={'type': 'json_object'}, messages=[
                {'role': 'system', 'content': (
                    skill_text('trip.requirements', 'system'))},
                {'role': 'user', 'content': complete_json({'previous':previous,'message':text,'documents':source_text},45000)},
                ])
        value = response_json(response)
        if not isinstance(value, dict):
            raise TripPlanError('未能解析行程需求，请说明城市和旅行天数。')
        result = dict(previous)
        corpus = text + '\n' + source_text
        clear = value.get('clear_fields', {})
        if (not isinstance(clear, dict) or clear.keys() - {'budget_text','transport_text','preferences','must_visit'}
                or any(not isinstance(v,str) or not v or v not in text for v in clear.values())):
            raise TripPlanError('清除字段缺少本轮明确依据。')
        field_sources = dict(previous.get('field_sources', {}))
        for key in ('destination', 'duration_text', 'start_date_text', 'end_date_text', 'budget_text', 'transport_text'):
            item = value.get(key, '')
            if item == '' and key not in clear:
                item = previous.get(key, '')
            if key in clear:
                item = ''
            if not isinstance(item, str) or len(item) > 150 or (item and item not in corpus and item != previous.get(key)):
                raise TripPlanError('行程字段无法对应你的原话，请明确城市、日期和天数。')
            result[key] = item
            if item:
                field_sources[key] = ('user' if item in text else ('image' if source_text.startswith('[图片#') else 'document')
                                     if item in source_text else field_sources.get(key, 'previous'))
        for key in ('preferences', 'must_visit'):
            items = value.get(key, [] if key in clear else previous.get(key, []))
            if (not isinstance(items, list) or len(items) > 8 or
                    any(not isinstance(item, str) or not item or len(item) > 150 or
                        (item not in corpus and item not in previous.get(key, [])) for item in items)):
                raise TripPlanError('行程约束无法对应你的原话，请分开说明。')
            result[key] = list(dict.fromkeys([*([] if key in clear else previous.get(key, [])), *items]))
        result['preferences'] = travel_preferences(result['preferences'])
        result['field_sources'] = field_sources
        if source_text:
            result['source_context'] = source_text
            if isinstance(source_text, ResearchSource):
                result['research_context'] = deepcopy(source_text.data)
            result['document_ids'] = list(dict.fromkeys(re.findall(r'文档#(\d+)', source_text)))
            result['media_ids'] = list(dict.fromkeys(re.findall(r'图片#(\d+)', source_text)))
        try:
            transport=intercity_request(value.get('intercity'),previous.get('intercity_request',{}),corpus)
            requested=(0 if field_sources.get('duration_text')=='date_range' and result['duration_text'] not in text
                       else duration_days(result['duration_text']))
            if transport or not previous:
                transport.setdefault('schema_version',1)
                result['intercity_request']=transport
            changes=value.get('intercity') or {}
            for direction,field in (('outbound','start_date_text'),('inbound','end_date_text')):
                if changes.get(direction,{}).get('date_text') and not value.get(field):
                    result[field]=transport[direction]['date_text']
            start_text=result['start_date_text'] or transport.get('outbound',{}).get('date_text','')
            end_text=result['end_date_text'] or transport.get('inbound',{}).get('date_text','')
            start_changed=bool(value.get('start_date_text') or changes.get('outbound',{}).get('date_text'))
        except ValueError as exc:
            raise TripPlanError(str(exc)) from exc
        date_error = ''
        try:
            direct_interval=date_range(text,now)
            if direct_interval:
                interval=direct_interval
            elif (previous.get('planning_window') and not start_changed and start_text==previous.get('start_date_text')
                    and end_text==previous.get('end_date_text')):
                interval=deepcopy(previous['planning_window'])
            else:
                interval=date_range(text,now,start_text=start_text,end_text=end_text,
                    resolved_start=previous.get('start_date','') if not start_changed and start_text==previous.get('start_date_text') else '')
        except ValueError as exc:
            date_error = str(exc)
            interval = None
        result['day_count']=requested or previous.get('day_count',0)
        if requested:result['requested_day_count']=requested
        if transport.get('day_count_basis')=='destination_days' and requested:
            result['destination_day_count']=requested
        result.pop('duration_adjustment',None)
        if interval:
            interval['span']=None
            # Separate leg dates describe return departure unless arrival was explicit.
            interval['end_event']=('arrival' if transport.get('inbound',{}).get('arrival_window')
                or transport.get('inbound',{}).get('arrival_before') or interval.get('end_event')=='arrival'
                or direct_interval else 'departure')
            apply_date_window(result,interval)
        if result['day_count'] and not 1 <= result['day_count'] <= 14:
            raise TripPlanError('请先规划 1 到 14 天的一段城市行程，较长旅行可按城市分段。')
        raw_date = result['start_date_text']
        supplied_date = bool(interval)
        if interval:
            result['start_date'] = interval['start']
            confirm_planning_date(result)
        elif raw_date and not date_error:
            if raw_date == previous.get('start_date_text') and previous.get('start_date'):
                result['start_date'] = previous['start_date']
            else:
                parsed = parse_reminder_time(raw_date, now)
                if parsed.error:
                    date_error = parsed.error
                result['start_date'] = parsed.day
                if parsed.day:
                    confirm_planning_date(result)
                    supplied_date = raw_date in text
        else:
            result['start_date'] = ''
        if date_error:
            result.update(start_date='', end_date='')
            result.pop('planning_window', None)
        if not supplied_date and not date_error:
            apply_reference_date(result, now, previous=previous, text=text)
        if result.get('start_date') and result.get('day_count') and (interval or previous.get('end_date') or result.get('date_is_tentative')):
            result['end_date'] = (date.fromisoformat(result['start_date']) + timedelta(days=result['day_count']-1)).isoformat()
        constraints = ' '.join(result['preferences'])
        result['gentle'] = bool(re.search(r'轻松|不太累|别太累|少走路|慢节奏', constraints) or
            (re.search(r'老人|父母', constraints) and not re.search(r'不带老人|老人不去|父母不去|不带父母', constraints)))
        result['no_night'] = bool(re.search(r'不走夜路|不夜游|不安排晚上|不玩太晚', constraints))
        overrides = previous.get('constraint_overrides', {})
        if 'pace' in overrides:
            result['gentle'] = overrides['pace']['value'] == 'relaxed'
        if 'night' in overrides:
            result['no_night'] = not overrides['night']['value']
        count_match = re.search(r'每天(?:安排|改成|改为)?([一二两三四五六七八九十\d]+)(?:处|个)(?:景点|活动)?', text)
        if not count_match and not previous.get('daily_activity_count'):
            count_match = re.search(r'每天(?:安排)?([一二两三四五六七八九十\d]+)(?:处|个)(?:景点|活动)?', source_text)
        if count_match:
            count = number(count_match[1])
            if not 1 <= count <= 3:
                raise TripPlanError('当前日程按每天 1 到 3 处主要活动规划，请先明确主要活动数量。')
            result['daily_activity_count'] = count
        result['hotel_requested'] = previous['hotel_requested'] if 'hotel_requested' in previous else bool(re.search(r'酒店|住宿|住哪', corpus))
        result['restaurant_requested'] = previous['restaurant_requested'] if 'restaurant_requested' in previous else bool(re.search(r'餐厅|餐馆|美食', corpus))
        if re.search(r'(?:不用|不要|不需要|无需|取消|不住).{0,8}(?:酒店|住宿)', text):
            result['hotel_requested'] = False
        if re.search(r'(?:不用|不要|不需要|无需|取消).{0,8}(?:餐厅|餐馆|美食|餐饮)', text):
            result['restaurant_requested'] = False
        structured = {}
        if isinstance(value.get('hotel'), dict):
            structured['accommodation'] = value['hotel']
        if isinstance(value.get('dining'), dict):
            structured['dining'] = value['dining']
        if isinstance(value.get('budgets'), list) and value['budgets']:
            structured['budgets'] = value['budgets']
        if isinstance(value.get('hotel_stay'),dict):structured['hotel_stay']=value['hotel_stay']
        if structured:
            try:
                result, _ = apply_constraints(result, structured, corpus)
            except ValueError as exc:
                raise TripPlanError(str(exc)) from exc
        result.setdefault('indoor_days', [])
        result.setdefault('outdoor_days', [])
        from tools.trip.constraints import apply_city_windows
        try:apply_city_windows(result,value.get('city_windows'),text)
        except ValueError as error:raise TripPlanError(str(error),code='city_window',path='city_windows') from error
        if date_error:
            error = TripPlanError(date_error.replace('提醒日期', '行程日期').replace('提醒时间', '行程时间'),
                                  code='invalid_dates', path='start_date')
            error.spec = result
            raise error
        return result

    def collect_places(self, spec, categories=None, *, anchors=()):
        research = source_context(spec)
        if research.get('provider_policy', {}).get('only'):
            raise TripPlanError('资料限定只用问道，地点和路线尚未授权核验；请先查看参考方案。')
        if self.amap is None:
            raise TripPlanError('地点数据源未配置，不能编造行程地点。')
        candidates = [t['name'] for t in research.get('targets', [])][:4]
        general = list(dict.fromkeys([*spec.get('must_visit', []), *candidates, *spec.get('preference_interests', []),
                                      '博物馆', '风景名胜', '公园']))[:10]
        queries = [(keyword, 'general', 1, 5) for keyword in general]
        if spec.get('hotel_requested'):
            queries.extend((keyword, 'hotel', page, 20) for keyword in hotel_search_terms(spec) for page in (1, 2))
        if spec.get('restaurant_requested'):
            queries.extend((keyword, 'dining', page, 20) for keyword in dining_search_terms(spec) for page in (1, 2))
        places = {}
        for keyword, category, page, offset in queries:
            if categories is not None and category not in categories:
                continue
            around=''
            if category!='general':
                points=[p for p in anchors if _coordinates(p)] or [p for p in places.values() if p['name'] in spec.get('must_visit',[]) and _coordinates(p)]
                if points:
                    central=min(points,key=lambda p:sum(distance_meters(p,other) or 0 for other in points))
                    around=central['location']
            key = (spec['destination'], keyword, category, page, offset, 'all' if category != 'general' else 'base',around)
            cached = self._place_cache.get(key)
            if cached and time.monotonic() - cached[0] < 300:
                query_results = cached[1]
            else:
                try:
                    query_results = tuple({**place, 'queried_at': datetime.now(timezone.utc).isoformat()}
                                          for place in self.amap.search_places(spec['destination'], keyword,
                                              offset=offset, page=page,
                                              extensions='all' if category != 'general' else 'base',
                                              **({'location':around} if around else {})))
                except AmapError as exc:
                    raise TripPlanError(f'地点查询暂时不可用：{exc}。没有保存行程变更，请稍后重试。') from exc
                self._place_cache[key] = (time.monotonic(), query_results)
                self._place_cache.move_to_end(key)
                while len(self._place_cache) > 128:
                    self._place_cache.popitem(last=False)
            for place in query_results:
                if (not place.get('id') or not place.get('name') or
                        re.search(r'停车场|卫生间|检票口|售票处|入口|出口|[东西南北]门$', place['name'])):
                    continue
                existing = places.get(place['id'], {})
                places[place['id']] = {**existing, **place, 'queried_keyword': keyword,
                                       'query_categories': list(dict.fromkeys([
                                           *existing.get('query_categories', []), category]))}
        if categories is not None:
            return places
        if not places:
            raise TripPlanError('没有查到可用于规划的地点资料，请核对城市或缩小范围。')
        for pid,group in place_groups(places).items():
            places[pid]['visit_group_id']=group
        for name in spec.get('must_visit', []):
            binding=bind_visit_entities([name],places,spec['destination'])[name]
            if binding['status']!='bound':
                error=TripPlanError(f'必去地点“{name}”没有唯一匹配，请补充完整名称后重试。',
                    code='required_entity_'+binding['status'],path='must_visit')
                error.places=places;raise error
        return places

    def update_recommendations(self, spec, previous_plan, places, categories, candidate_ids=None, *, dining_scopes=()):
        plan = deepcopy(previous_plan)
        candidates=places
        if candidate_ids is not None:
            candidates={key:place for key,place in places.items() if key in candidate_ids or not
                (('hotel' in categories and re.search(r'酒店|住宿服务',place.get('type',''))) or
                 ('dining' in categories and re.search(r'餐饮|餐馆|餐厅',place.get('type',''))))}
        recommendation_spec=spec
        if 'dining' in categories and previous_plan.get('dining_options'):
            scopes=[{'day_index':item['day_index'],'meal':item['meal']} for item in previous_plan['dining_options']]
            recommendation_spec={**spec,'dining_preferences':{**spec.get('dining_preferences',{}),'scopes':scopes}}
        updated = apply_recommendations(recommendation_spec,deepcopy(previous_plan),candidates)
        if dining_scopes:
            affected={(scope['day_index'],scope['meal']) for scope in dining_scopes}
            meals={(item['day_index'],item['meal']):deepcopy(item) for item in previous_plan.get('dining_options',[])}
            for key in affected:meals.pop(key,None)
            meals.update({(item['day_index'],item['meal']):item for item in updated['dining_options']
                          if (item['day_index'],item['meal']) in affected})
            updated['dining_options']=sorted(meals.values(),key=lambda m:(m['day_index'],MEALS.index(m['meal'])))
        keys = []
        if 'hotel' in categories:
            keys += ['hotel_ids','hotel_recommendations','hotel_recommendation_policy']
        if 'dining' in categories:
            keys += ['restaurant_ids','restaurant_recommendations','dining_options']
        for key in keys:
            plan[key] = updated[key]
        # No current search candidates means explicitly empty recommendations, not stale successes.
        validation_spec={**spec}
        if 'hotel' in categories and not updated['hotel_ids']:
            validation_spec['hotel_requested']=False
        if 'dining' in categories and not updated['restaurant_ids']:
            validation_spec['restaurant_requested']=False
        self.validate_plan(plan,validation_spec,places)
        return plan

    def reconcile_candidate(self,candidate,previous):
        """Adopt normalized core data and rebuild only invalidated derived state, without I/O."""
        spec,plan,places=candidate['spec'],candidate['plan'],candidate['sources']
        from tools.trip.constraints import normalize_food_units
        normalize_food_units(spec)
        if 'city_windows' in spec:spec['city_windows']=effective_city_windows(spec)
        old_days=deepcopy(plan['days'])
        if spec.get('start_date') and (spec.get('planning_window') or spec.get('city_windows')):
            spec['intercity_windows']=self.transport_windows(spec,plan.get('intercity',{}))
        core=self.validate_plan(plan,spec,places)
        plan.update({key:value for key,value in core.items() if key!='days'})
        plan['days']=[{**old,**clean} for old,clean in zip(old_days,core['days'])]
        identity=lambda rows:[(d.get('date'),[(a['poi_id'],a['period'],a.get('subactivities',[])) for a in d['activities']]) for d in rows]
        changed_days=identity(plan['days'])!=identity(previous['plan']['days'])
        moved_dates=any(a.get('date')!=b.get('date') for a,b in zip(old_days,plan['days']))
        geometry=lambda sources:{pid:{k:p.get(k) for k in ('location','address','cityname')} for pid,p in sources.items()}
        new_geometry,old_geometry=geometry(places),geometry(previous['sources'])
        moved={pid for pid in places if new_geometry.get(pid)!=old_geometry.get(pid)}
        anchors={a['poi_id'] for d in plan['days'] for a in d['activities']}
        anchor_moved=bool(moved & anchors)
        if spec.get('start_date'):
            finish=(date.fromisoformat(spec['start_date'])+timedelta(days=spec['day_count']-1)).isoformat()
            if spec.get('end_date') and spec['end_date']!=finish:
                raise TripPlanError('行程起止日期与天数不一致，请明确要采用的日期范围。',code='calendar_conflict',path='spec.end_date')
            window=spec.get('planning_window',{})
            if window and not spec.get('city_windows') and (window.get('start')!=spec['start_date'] or window.get('end')!=finish):
                raise TripPlanError('城市可用窗口与当前行程日期不一致，请先明确起止日期。',code='calendar_conflict',path='spec.planning_window')
            if spec.get('intercity_request',{}).get('day_count_basis','travel_included')=='travel_included':
                for direction in ('outbound','inbound'):
                    selected=plan.get('intercity',{}).get(direction,{}).get('selected',{})
                    if any(value and not spec['start_date']<=value[:10]<=finish for value in (selected.get('departure_at'),selected.get('arrival_at'))):
                        raise TripPlanError('已选交通日期超出当前含交通行程，请明确改期或重新选择交通。',code='calendar_conflict',path='plan.intercity.'+direction)
        if spec.get('city_windows'):
            from tools.trip.constraints import apply_city_windows
            apply_city_windows(spec)
        if spec.get('planning_window') or any(plan.get('intercity',{}).get(d,{}).get('selected') for d in ('outbound','inbound')):
            traffic=plan.get('intercity',{})
            if changed_days or anchor_moved:
                for transfer in traffic.get('station_transfers',{}).values():
                    transfer.pop('duration_minutes',None);transfer['status']='unknown'
            spec['intercity_windows']=self.transport_windows(spec,traffic)
            if traffic:traffic['availability_windows']=deepcopy(spec['intercity_windows'])
        for old,day in zip(old_days,plan['days']):
            ids=[a['poi_id'] for a in day['activities']];pairs=set(zip(ids,ids[1:]))
            if 'legs' in day:day['legs']=[r for r in day['legs'] if (r.get('from_poi'),r.get('to_poi')) in pairs]
            for route in day.get('legs',[]):
                if old.get('date')!=day.get('date') or route.get('from_poi') in moved or route.get('to_poi') in moved:
                    route['date_recheck_required']=True
        invalidate_changed_dates(spec,plan)
        categories=set()
        for kind,field,requested,budget in (('hotel','hotel_preferences','hotel_requested','accommodation'),
                ('dining','dining_preferences','restaurant_requested','food')):
            pattern=HOTEL_PATTERN if kind=='hotel' else DINING_PATTERN
            relevant=lambda sources:{pid:{k:p.get(k) for k in ('name','type','location','address','cost','cost_unit','price_unit','lowest_price','lowest_price_unit','rating')} for pid,p in sources.items() if pattern.search(str(p.get('type','')))}
            if (changed_days or moved_dates or anchor_moved or relevant(places)!=relevant(previous['sources']) or spec.get(field)!=previous['spec'].get(field)
                    or spec.get('budgets',{}).get(budget)!=previous['spec'].get('budgets',{}).get(budget)
                    or kind=='hotel' and (plan.get('hotel_recommendation_policy')!=HOTEL_POLICY_VERSION or plan.get('hotel_quotes') or spec.get('hotel_stay')!=previous['spec'].get('hotel_stay'))):
                if spec.get(requested):categories.add(kind)
        if categories:plan=self.update_recommendations(spec,plan,places,categories)
        for option in plan.get('dining_options',[]):
            for row in option.get('candidates',[]):
                routes=row.get('routes',{})
                parts=[routes.get(k) for k in ('from_previous','to_next','direct')]
                if all(part and not part.get('error') and part.get('duration_seconds') is not None for part in parts):
                    routes['extra_duration_seconds']=max(0,parts[0]['duration_seconds']+parts[1]['duration_seconds']-parts[2]['duration_seconds'])
                else:routes.pop('extra_duration_seconds',None)
        candidate['plan']=plan
        from tools.trip.presentation import answer_basis
        if candidate.get('answer') and candidate.get('answer_basis')!=answer_basis(candidate):
            candidate.pop('answer',None);candidate.pop('answer_basis',None)
        # Derived fields survive, but every normalized date/period is adopted.
        final=self.validate_plan(plan,spec,places)
        plan['days']=[{**old,**clean} for old,clean in zip(plan['days'],final['days'])]
        return candidate

    @staticmethod
    def validate_plan(value, spec, places):
        if not isinstance(value, dict) or not isinstance(value.get('days'), list):
            raise TripPlanError('行程结构无效。')
        days = value['days']
        windows=spec.get('intercity_windows',{})
        if spec.get('planning_window') and spec.get('start_date') and (not windows or any(
                row.get('date')!=(date.fromisoformat(spec['start_date'])+timedelta(days=int(index)-1)).isoformat()
                for index,row in windows.items())):
            windows=TripPlanner.transport_windows(spec,value.get('intercity',{}))
        if len(days) != spec['day_count']:
            raise TripPlanError('行程天数与用户要求不一致。',code='day_count',path='days')
        required=bind_visit_entities(spec.get('must_visit',[]),places,spec.get('destination',''))
        required_ids={r['poi_id'] for r in required.values() if r['status']=='bound'}
        spec['required_entities']=required
        used = set()
        groups=place_groups(places)
        used_groups={}
        result = []
        for index, day in enumerate(days, 1):
            if not isinstance(day, dict) or type(day.get('day_index')) is not int or day['day_index'] != index:
                raise TripPlanError('行程日期序号必须连续。')
            activities = day.get('activities')
            traffic=index in spec.get('transport_days',[])
            minimum = 0 if index in spec.get('rest_days', []) or traffic else 1
            expected = spec.get('activity_count_overrides', {}).get(str(index), spec.get('daily_activity_count'))
            if traffic and spec.get('intercity_request',{}).get('day_count_basis')=='destination_days':expected=None
            maximum = max(2 if spec.get('gentle') else 3, expected or 0)
            if not isinstance(activities, list) or not minimum <= len(activities) <= maximum:
                raise TripPlanError('每日安排过多或缺失，不满足节奏约束。')
            if expected is not None and len(activities) != expected:
                raise TripPlanError('未满足用户明确指定的每日主要活动数量。')
            periods = set()
            clean = []
            for item in activities:
                allowed=windows.get(str(index),{}).get('allowed_periods')
                if allowed is not None and item.get('period') not in allowed:
                    raise TripPlanError('活动与城市到达/离开可用窗口冲突，请调整首尾日活动。' if spec.get('city_windows') else
                        '活动与城际到达/返程窗口冲突，请调整首尾日活动。',code='transport_conflict',path=f'days[{index-1}].activities')
                if not isinstance(item, dict) or item.get('poi_id') not in places:
                    raise TripPlanError('安排了没有查询证据的地点。',code='unexpected_poi',path=f'days[{index-1}].activities')
                poi_id = item['poi_id']
                if re.search(r'酒店|住宿服务', places[poi_id].get('type', '')) and poi_id not in required_ids:
                    raise TripPlanError('住宿候选不能冒充景点活动。')
                period = item.get('period')
                if period not in {'上午', '下午', '晚上'} or period in periods:
                    raise TripPlanError('一天内的时间段不能重复或含糊。',code='invalid_period',path=f'days[{index-1}].activities')
                if spec.get('no_night') and period == '晚上':
                    raise TripPlanError('用户要求不安排夜间活动。')
                if (index in spec.get('indoor_days', []) or period in spec.get('indoor_periods',{}).get(str(index),[])) and not indoor_place(places[poi_id]):
                    raise TripPlanError('室内日包含未经确认是室内的地点。')
                if (index in spec.get('outdoor_days', []) or period in spec.get('outdoor_periods',{}).get(str(index),[])) and not outdoor_place(places[poi_id]):
                    raise TripPlanError('室外日只能选择分类明确的公园、广场或自然景观。')
                if poi_id in used:
                    raise TripPlanError('同一个地点不能重复占用多个行程时段。')
                if groups[poi_id] in used_groups and not {poi_id,used_groups[groups[poi_id]]} <= required_ids:
                    raise TripPlanError('同一游览目的地或园内场馆不能重复占用主要活动，请合并为subactivities或选择其他地点。',code='duplicate_visit_group',path=f'days[{index-1}].activities')
                reason = item.get('reason', '兴趣匹配')
                if reason not in REASONS:
                    raise TripPlanError('行程理由含有未经核实的自由描述。')
                used.add(poi_id)
                used_groups[groups[poi_id]]=poi_id
                periods.add(period)
                activity = {'poi_id': poi_id, 'period': period, 'reason': reason}
                children=item.get('subactivities',[])
                if not isinstance(children,list):raise TripPlanError('子场馆结构无效。')
                for child in children:
                    cid=child.get('poi_id') if isinstance(child,dict) else None
                    if cid not in places or cid in used or groups[cid]!=groups[poi_id]:
                        raise TripPlanError('子场馆必须属于同一游览组，且不可重复。')
                    validate_activity({'poi_id':cid,'period':period,'reason':reason},spec,places,index)
                    activity.setdefault('subactivities',[]).append({'poi_id':cid})
                    used.add(cid)
                try:
                    description = validate_activity(item, spec, places, index)
                except ValueError as error:
                    raise TripPlanError(str(error)) from error
                if description:
                    activity['description'] = description
                clean.append(activity)
            clean.sort(key=lambda item: ('上午', '下午', '晚上').index(item['period']))
            locked=spec.get('_locked_activity_days',{}).get(str(index))
            identity=lambda items:[(a['poi_id'],a['period'],[c['poi_id'] for c in a.get('subactivities',[])]) for a in items]
            if locked is not None and identity(locked)!=identity(clean):
                raise TripPlanError('城际调整不能更改未受影响日期的活动。')
            actual_date = (date.fromisoformat(spec['start_date']) + timedelta(days=index-1)).isoformat() if spec.get('start_date') else ''
            result.append({'day_index': index, 'date': actual_date, 'activities': clean,
                           **({'kind':'transport'} if traffic else {})})
        for index,(name,binding) in enumerate(required.items()):
            if binding['status']!='bound':
                raise TripPlanError(f'尚未唯一确认必去地点：{name}，请核对城市、具体园区或地址。',
                    code='required_entity_'+binding['status'],path=f'must_visit[{index}]')
            if binding['poi_id'] not in used:
                raise TripPlanError(f'遗漏了用户要求的地点：{name}',code='missing_required_entity',path=f'must_visit[{index}]')
        plan = {'days': result}
        for key, requested, pattern in (('hotel_ids', 'hotel_requested', r'酒店|住宿服务'),
                                        ('restaurant_ids', 'restaurant_requested', r'餐饮|餐馆|餐厅')):
            values = value.get(key, [])
            if not isinstance(values, list) or len(values) > 3 or any(
                    not isinstance(poi_id, str) or poi_id not in places or not re.search(pattern, places[poi_id].get('type', '')) for poi_id in values):
                raise TripPlanError('住宿或餐饮候选必须来自对应类别的查询结果，且最多三处。')
            if not spec.get(requested) and values:
                raise TripPlanError('不要增加用户未请求的住宿或餐饮候选。')
            plan[key] = list(dict.fromkeys(values))
        return plan

    def add_route_evidence(self, plan, spec, places, previous_plan=None):
        if spec.get('transport_preferences'):
            self._add_preferred_routes(plan, spec, places)
            self._add_recommendation_routes(plan, spec, places)
            return
        transport = spec.get('transport_text', '')
        mode = ('transit' if re.search(r'公交|地铁|公共交通', transport) else
                'walking' if '步行' in transport else 'driving' if re.search(r'自驾|驾车|开车', transport) else '')
        if not mode or re.search(r'不|别', transport):
            return
        previous_days = {day['day_index']: day for day in (previous_plan or {}).get('days', [])}
        for day in plan['days']:
            old = previous_days.get(day['day_index'])
            if old and old['activities'] == day['activities'] and all(leg.get('mode') == mode and not leg.get('error') and fresh_route(leg) for leg in old.get('legs', [])) and old.get('legs'):
                day['legs'] = old['legs']
                continue
            day['legs'] = []
            activities = day['activities']
            for origin, destination in zip(activities, activities[1:]):
                evidence = {'from_poi': origin['poi_id'], 'to_poi': destination['poi_id'], 'mode': mode,
                            'queried_at': datetime.now(timezone.utc).isoformat()}
                try:
                    evidence.update(self.amap.route_for_pois(places[origin['poi_id']], places[destination['poi_id']],
                                                           mode=mode, city=spec['destination']))
                except AmapError as exc:
                    evidence['error'] = str(exc)
                day['legs'].append(evidence)
        self._add_recommendation_routes(plan, spec, places)

    def _add_preferred_routes(self, plan, spec, places):
        preference = spec['transport_preferences']
        modes = [mode for mode in preference.get('preferred', []) if mode not in preference.get('forbidden', [])]
        modes.sort(key=lambda mode: mode in preference.get('discouraged', []))
        if not modes:
            for day in plan['days']:
                day['legs'] = []
            return
        short_walk = 20*60 if spec.get('gentle') else 30*60
        for day in plan['days']:
            day['legs'] = []
            for origin, destination in zip(day['activities'],day['activities'][1:]):
                order = (['walking','transit'] if set(modes) == {'walking','transit'}
                         and 'walking' not in preference.get('discouraged',[]) else modes)
                choices = []
                for mode in order:
                    evidence = {'from_poi':origin['poi_id'],'to_poi':destination['poi_id'],'mode':mode,
                                'queried_at':datetime.now(timezone.utc).isoformat()}
                    try:
                        evidence.update(self.amap.route_for_pois(places[origin['poi_id']],places[destination['poi_id']],
                                                               mode=mode,city=spec['destination']))
                    except AmapError as exc:
                        evidence['error'] = str(exc)
                    choices.append(evidence)
                    if not evidence.get('error'):
                        if mode != 'walking' or 'transit' not in modes or 0 < evidence.get('duration_seconds',0) <= short_walk:
                            break
                selected = choices[-1]
                if selected.get('error') and len(choices)>1:
                    selected['error'] += '；未自动改为打车或推荐长距离步行。'
                day['legs'].append(selected)

    @staticmethod
    def _recommendation_mode(spec):
        preferred = spec.get('transport_preferences', {}).get('preferred', [])
        for mode in ('transit', 'walking', 'driving'):
            if mode in preferred:
                return mode
        text = spec.get('transport_text', '')
        return ('transit' if re.search(r'公交|地铁|公共交通', text) else
                'walking' if '步行' in text else 'driving' if re.search(r'自驾|驾车|开车', text) else '')

    def _add_recommendation_routes(self, plan, spec, places, limit=12, *, categories=None,dining_scopes=()):
        mode = self._recommendation_mode(spec)
        if not mode:
            return
        budget={'remaining':limit}

        def query(origin_id, destination_id):
            if budget['remaining']<=0 or origin_id not in places or destination_id not in places:
                return None
            return self.route_evidence(origin_id,destination_id,places,spec,mode,budget=budget)

        hotel_recommendations = plan.get('hotel_recommendations', []) if categories is None or 'hotel' in categories else []
        if spec.get('hotel_preferences', {}).get('near_itinerary') and hotel_recommendations:
            selected = hotel_recommendations[0]
            selected['routes'] = []
            hotel_id = selected['poi_id']
            for day in plan.get('days', []):
                activities = day.get('activities', [])
                if not activities:
                    continue
                outbound = query(hotel_id, activities[0]['poi_id'])
                inbound = query(activities[-1]['poi_id'], hotel_id)
                selected['routes'].append({'day_index': day['day_index'], 'outbound': outbound, 'return': inbound})
                if budget['remaining']<=0:
                    break

        for option in plan.get('dining_options', []):
            if categories is not None and 'dining' not in categories:break
            if dining_scopes and not any(s['day_index']==option['day_index'] and s['meal']==option['meal'] for s in dining_scopes):continue
            if budget['remaining']<=0:break
            if not option.get('candidates'):continue
            selected = option['candidates'][0]
            day = next((item for item in plan.get('days', []) if item['day_index'] == option['day_index']), None)
            if not day or not day.get('activities'):
                continue
            activities = day['activities']
            meal = option.get('meal')
            if meal == '午餐':
                before = [item for item in activities if item['period'] == '上午']
                after = [item for item in activities if item['period'] in {'下午','晚上'}]
                origin = before[-1]['poi_id'] if before else activities[0]['poi_id']
                destination = after[0]['poi_id'] if after else ''
            elif meal == '早餐':
                origin, destination = '', activities[0]['poi_id']
            else:
                origin, destination = activities[-1]['poi_id'], ''
            routes = {'from_previous': query(origin, selected['poi_id']) if origin else None,
                      'to_next': query(selected['poi_id'], destination) if destination else None,
                      'direct': query(origin, destination) if origin and destination else None}
            parts = (routes['from_previous'], routes['to_next'], routes['direct'])
            if all(item and not item.get('error') and item.get('duration_seconds') is not None for item in parts):
                routes['extra_duration_seconds'] = max(0, parts[0]['duration_seconds'] + parts[1]['duration_seconds']
                                                       - parts[2]['duration_seconds'])
            selected['routes'] = routes

    @skill_request
    def plan(self, spec, places, previous_plan=None, edit_request='', *, attempts=2,repair_feedback=None):
        try:
            bind_context(spec, places)
        except ValueError as error:
            raise TripPlanError(str(error)) from error
        spec['required_entities']=bind_visit_entities(spec.get('must_visit',[]),places,spec.get('destination',''))
        if spec.get('city_windows') and spec.get('start_date'):
            spec['intercity_windows']=self.transport_windows(spec,spec.get('intercity_context',{}))
        target_days={number(value) for value in re.findall(r'第([一二两三四五六七八九十\d]+)天',edit_request)}
        if previous_plan and len(target_days)==1 and os.getenv('AI_LOCAL_PLANNING','true').lower() == 'true':
            return self._plan_day(spec,places,previous_plan,edit_request,next(iter(target_days)))
        messages = [
            {'role': 'system', 'content': (
                skill_text('trip.plan', 'system'))},
            {'role': 'user', 'content': complete_json({**planning_context(spec,places,previous_plan),
                'edit_request':edit_request},45000,purpose='trip_plan')},
        ]
        if repair_feedback:
            messages.append({'role':'user','content':complete_json({'validation_feedback':repair_feedback,
                'instruction':'只修复校验指出的问题，保持全部用户硬约束，不新增未查询地点。'},26000)})
        for attempt in range(attempts):
            purpose = 'trip_plan' if attempt == 0 and not repair_feedback else 'trip_plan_repair'
            with model_call_context(purpose, prompt_version='trip-plan-v8-structural-context', repair_attempt=attempt+int(bool(repair_feedback)),
                                    output_multiplier=2 if spec['day_count']>7 else 1, stage_id='trip.plan'):
                response = self.client.chat.completions.create(model=self.model, response_format={'type': 'json_object'}, messages=messages)
            raw = response.choices[0].message.content or '{}'
            try:
                response_text(response)
                plan = self.validate_plan(json.loads(raw), spec, places)
                if previous_plan and target_days:
                    for old, new in zip(previous_plan['days'], plan['days']):
                        if old['day_index'] not in target_days and old['activities'] != new['activities']:
                            raise TripPlanError('修改了用户未要求调整的其他日期，请保持其他日期原样。')
                return apply_recommendations(spec, plan, places)
            except (ValueError, TypeError) as exc:
                if attempt==attempts-1:
                    if isinstance(exc,TripPlanError):
                        exc.candidate=json.loads(raw);exc.failure_stage='plan_validation'
                        if exc.code!='transport_conflict':raise
                    if isinstance(exc,TripPlanError) and exc.code=='transport_conflict':
                        error=TripPlanError('交通窗口与明确活动要求仍有冲突，请调整车次、减少可选活动或延长行程；尚未保存不合格结果。',code=exc.code,path=exc.path)
                        error.candidate=exc.candidate;raise error from exc
                    raise TripPlanError('模型未能生成符合约束的行程，没有保存不合格结果。') from exc
                messages.extend(({'role': 'assistant', 'content': raw},
                                 {'role': 'user', 'content': (skill_text('trip.plan', 'feedback') + f'{exc}' + skill_text('trip.plan', 'feedback_2'))}))

    @skill_request
    def _plan_day(self, spec, places, previous, request, day_index):
        day = next((d for d in previous['days'] if d['day_index']==day_index),None)
        if day is None:
            raise TripPlanError('指定日期不在当前行程中。')
        period_match=re.search(r'第[一二两三四五六七八九十\d]+天(?:的)?(上午|下午|晚上)',request)
        period=period_match[1] if period_match else None
        locked=[a for d in previous['days'] for a in d['activities']
                if d['day_index']!=day_index or (period and a['period']!=period)]
        excluded={a['poi_id'] for a in locked}
        payload=planning_context(spec,places,previous)
        projected_day=next(d for d in payload.pop('previous_plan')['days'] if d['day_index']==day_index)
        payload.update(day=projected_day,period=period,locked_poi_ids=list(excluded),request=request)
        payload['places']=[p for p in payload['places'] if p.get('id') not in excluded]
        messages=[{'role':'system','content':
            skill_text('trip.local_day', 'system')},
            {'role':'user','content':complete_json(payload,45000,purpose='trip_local_day')}]
        for attempt in range(2):
            with model_call_context('trip_plan' if not attempt else 'trip_plan_repair',prompt_version='trip-local-day-v5-structural-context',repair_attempt=attempt, stage_id='trip.local_day'):
                response=self.client.chat.completions.create(model=self.model,response_format={'type':'json_object'},messages=messages)
            raw=response_text(response)
            try:
                value=json.loads(raw)
                if set(value) != {'activities'} or not isinstance(value['activities'],list):
                    raise TripPlanError('局部修改只能包含activities。')
                if any(not isinstance(a,dict) for a in value['activities']):
                    raise TripPlanError('活动结构无效。')
                if period and any(a.get('period')!=period for a in value['activities']):
                    raise TripPlanError('修改了指定时段以外的活动。')
                candidate=deepcopy(previous)
                selected=next(d for d in candidate['days'] if d['day_index']==day_index)
                selected['activities'] = ([a for a in day['activities'] if a['period']!=period] if period else [])+value['activities']
                clean=self.validate_plan(candidate,spec,places)
                selected['activities']=next(d for d in clean['days'] if d['day_index']==day_index)['activities']
                selected.pop('legs',None)
                candidate['_local_patch']=True
                return candidate
            except (ValueError,TypeError,KeyError) as exc:
                if attempt:
                    raise TripPlanError('局部修改未通过完整行程校验，原行程未改变；请调整要求或明确扩大修改范围。') from exc
                messages.extend([{'role':'assistant','content':raw},{'role':'user','content':(skill_text('trip.local_day', 'feedback') + f'{exc}')}])

