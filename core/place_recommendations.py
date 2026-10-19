"""Deterministic hotel and dining candidate selection for itinerary plans."""
from __future__ import annotations

from math import asin, cos, radians, sin, sqrt
import re


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
    if kind == 'hotel':
        return {'average_price': _number(place.get('cost')),
                'lowest_price': _number(place.get('lowest_price'))}
    return {'per_capita': _number(place.get('cost'))}


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


def _budget_range(spec, scope):
    budget = spec.get('budgets', {}).get(scope, {})
    text = str(budget.get('value') or '')
    values = [float(item) for item in re.findall(r'\d+(?:\.\d+)?', text)]
    if not values:
        return None
    if len(values) >= 2:
        return min(values[0], values[1]), max(values[0], values[1]), budget
    if re.search(r'以上|起', text):
        return values[0], None, budget
    return None, values[0], budget


def _price_evidence(place, spec, scope, kind):
    target = _budget_range(spec, scope)
    details = price_details(place, kind)
    candidate_price = (details.get('average_price') or details.get('lowest_price')
                       if kind == 'hotel' else details.get('per_capita'))
    if target is None:
        return 'not_requested', details, []
    if candidate_price is None:
        label = '指定日期房价待核验' if kind == 'hotel' else '人均消费待核验'
        return 'unknown', details, [label]
    minimum, maximum, budget = target
    unit = str(budget.get('unit_text') or '')
    if kind == 'hotel':
        return 'reference_only', details, ['仅有非实时参考价，指定日期房价待核验']
    if not ('每人' in unit and ('每餐' in unit or '每顿' in unit)):
        return 'reference_only', details, ['参考消费与餐饮预算单位不能直接比较']
    within = (minimum is None or candidate_price >= minimum) and (maximum is None or candidate_price <= maximum)
    return ('reference_in_budget' if within else 'reference_over_budget'), details, [
        '参考人均符合预算，实际账单待核验' if within else '参考人均超出预算，实际账单待核验']


def hotel_search_terms(spec):
    preferences = spec.get('hotel_preferences', {})
    brands = [str(value).strip() for value in preferences.get('preferred_brands', []) if str(value).strip()]
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


def rank_hotels(spec, plan, places):
    preferences = spec.get('hotel_preferences', {})
    brands = preferences.get('preferred_brands', [])
    minimum_rating = _number(preferences.get('minimum_rating'))
    candidates = []
    for poi_id, place in places.items():
        if not HOTEL_PATTERN.search(str(place.get('type') or '')):
            continue
        name = str(place.get('name') or '')
        if not name or re.search(r'暂停营业|停车场|入口|出口', name):
            continue
        matched_brand = _contains_any(name, brands) if brands else False
        chain = _contains_any(name, CHAIN_BRANDS)
        current_rating = rating(place)
        issues = []
        hard_failure = False
        if preferences.get('brand_mode') == 'required' and brands and not matched_brand:
            issues.append('不属于指定酒店品牌')
            hard_failure = True
        if preferences.get('chain_mode') == 'required' and not chain:
            issues.append('未核实为要求的连锁酒店')
            hard_failure = True
        if minimum_rating is not None and (current_rating is None or current_rating < minimum_rating):
            issues.append('评分缺失' if current_rating is None else f'高德评分低于 {minimum_rating:g}')
            hard_failure = True
        price_status, price_info, price_issues = _price_evidence(place, spec, 'accommodation', 'hotel')
        issues.extend(price_issues)
        distance = _hotel_distance_summary(place, plan, places)
        reasons = []
        if matched_brand:
            reasons.append('匹配偏好品牌')
        elif chain:
            reasons.append('属于已识别连锁品牌')
        if current_rating is not None and (minimum_rating is None or current_rating >= minimum_rating):
            reasons.append(f'高德评分 {current_rating:g}')
        if distance['average_meters'] is not None:
            reasons.append('已比较全部行程日的位置')
        status = 'fallback' if hard_failure else ('partial' if price_issues else 'matched')
        brand_order = next((index for index, brand in enumerate(brands) if brand.casefold() in name.casefold()), len(brands))
        candidates.append({'poi_id': poi_id, 'status': status, 'reasons': reasons, 'issues': issues,
                           'rating': current_rating, 'price_info': price_info,
                           'price_status': price_status, 'distance': distance,
                           '_sort': (2 if status == 'fallback' else 1 if status == 'partial' else 0,
                                     brand_order, 0 if chain else 1,
                                     0 if price_status == 'reference_in_budget' else 1,
                                     distance['worst_day_meters'] if distance['worst_day_meters'] is not None else 10**9,
                                     distance['average_meters'] if distance['average_meters'] is not None else 10**9,
                                     -(current_rating or 0), name)})
    candidates.sort(key=lambda item: item['_sort'])
    for item in candidates:
        item.pop('_sort', None)
    return candidates[:3]


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
            hard_failure = True
        if preferences.get('avoid') or preferences.get('allergies'):
            issues.append('忌口或配料仍需向商家确认')
        price_status, price_info, price_issues = _price_evidence(place, spec, 'food', 'dining')
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


def recommend_dining(spec, plan, places):
    preferences = spec.get('dining_preferences', {})
    scopes = preferences.get('scopes', [])
    if not scopes and preferences.get('near_itinerary'):
        scopes = [{'day_index': day['day_index'], 'meal': '午餐'} for day in plan.get('days', []) if day.get('activities')]
    if not scopes:
        all_anchors = [place for _, _, place in _activity_places(plan, places)] if preferences.get('near_itinerary') else []
        return _rank_restaurants(spec, places, all_anchors)[:3], []
    options = []
    days = {day['day_index']: day for day in plan.get('days', [])}
    for scope in scopes:
        day_index, meal = scope.get('day_index'), scope.get('meal', '餐饮')
        day = days.get(day_index)
        if day is None or meal not in MEALS:
            continue
        ranked = _rank_restaurants(spec, places, _meal_anchors(day, places, meal))[:3]
        if ranked:
            options.append({'day_index': day_index, 'meal': meal, 'candidates': ranked})
    return _rank_restaurants(spec, places, [])[:3] if not options else [], options


def apply_recommendations(spec, plan, places):
    hotel_recommendations = rank_hotels(spec, plan, places) if spec.get('hotel_requested') else []
    restaurant_recommendations, dining_options = (recommend_dining(spec, plan, places)
                                                   if spec.get('restaurant_requested') else ([], []))
    plan['hotel_recommendations'] = hotel_recommendations
    plan['hotel_ids'] = [item['poi_id'] for item in hotel_recommendations]
    plan['restaurant_recommendations'] = restaurant_recommendations
    plan['restaurant_ids'] = [item['poi_id'] for item in restaurant_recommendations]
    plan['dining_options'] = dining_options
    return plan
