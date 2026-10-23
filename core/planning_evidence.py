"""Bounded research -> POI binding -> validated, persistent activity descriptions."""
from copy import deepcopy
from datetime import date, timedelta
import json
import re

from core.wendao_answer import INSTRUCTION


class ResearchSource(str):
    """Server-created handoff. A document cannot acquire this type via its text."""
    def __new__(cls, text, data=None):
        value = super().__new__(cls, text)
        value.data = data
        return value


def source_context(spec):
    value = spec.get('research_context')
    return value if isinstance(value, dict) else {}


def research_context(data):
    from core.attraction_projection import planning_facts
    from core.attraction_answers import has_provider, answer_view, working_copy, fact_key
    view = answer_view(data) if has_provider(data) else None
    from core.travel_answer import limits
    from infrastructure.model_gateway import estimate_input
    budget = limits()[0] - 10000
    if view:
        data = working_copy(data)
        base = {'schema_version':data.get('fact_schema'), 'facts':[], 'sources':[], 'targets':[]}
    elif data.get('target_scope'):
        base = json.loads(planning_facts(data).split('\n', 1)[1])
    else:
        base = {'schema_version': data.get('fact_schema'), 'facts': [], 'sources': [], 'targets': []}
    targets = data.get('target_scope', {}).get('targets', data.get('provider_targets', []))
    facts = base.get('facts', []) if data.get('target_scope') else data.get('facts', [])
    if view:
        selected = {fact_key(f):f for entry in view['entries'] for slot in entry['slots'] for f in slot['facts']
                    if f['status']!='provider_reference' and f.get('date_check',{}).get('state')!='outside'}
        facts = sorted(selected.values(),key=lambda f:(f['status']!='official_verified',
                       {'opening_hours':0,'reservation_rules':1,'ticket_price':2}.get(f['topic'],3)))
    payload = {**base, 'facts': [], 'sources': [{k: s[k] for k in ('id', 'type', 'provider', 'retrieved_at') if k in s}
                for s in data.get('sources', [])],
               'city': data.get('target_scope', {}).get('city') or data.get('destination', ''),
               'targets': [{'id': f'target-{i + 1}', 'name': t['name'], 'day_index': t.get('day_index')} for i, t in enumerate(targets)],
               'activity_materials': [], 'provider_policy': data.get('provider_policy', {}),
               'omitted_materials': len(data.get('activity_materials', [])), 'omitted_facts': len(data.get('facts', []))}
    if view:
        payload.update(answer_schema=view['schema_version'],planning_answers=[],omitted_answer_slots=view['metrics']['slot_count'])
        # Whole selected slots, rotating over topics and objects. No one-sided conflicts.
        for n in range(max((len(e['slots']) for e in view['entries']),default=0)):
            for entry in view['entries']:
                if n>=len(entry['slots']):
                    continue
                slot = entry['slots'][n]
                row = {k:slot[k] for k in ('target_id','topic','text','notes','answer_state','verification_state','source_ids','fact_ids')}
                row['visit_date'] = entry.get('visit_date')
                trial = {**payload,'planning_answers':[*payload['planning_answers'],row],
                         'omitted_answer_slots':payload['omitted_answer_slots']-1}
                if estimate_input(trial)<=budget*0.65:
                    payload=trial
    # Reserve room for per-POI material; select complete records, never truncate prose.
    materials = data.get('activity_materials', [])
    for row in materials:
        row = {**row, 'intro': row.get('intro', [])[:1], 'tips': row.get('tips', [])[:1]}
        trial = {**payload, 'activity_materials': [*payload['activity_materials'], row],
                 'omitted_materials': payload['omitted_materials'] - 1}
        if estimate_input(trial) <= budget*0.85:
            payload = trial
    queues = {t['id']: [f for f in facts if f['target_id'] == t['id']] for t in payload['targets']}
    for round_index in range(max((len(q) for q in queues.values()), default=0)):
        for rows in queues.values():
            if round_index >= len(rows):
                continue
            trial = {**payload, 'facts': [*payload['facts'], rows[round_index]],
                     'omitted_facts': payload['omitted_facts'] - 1}
            if estimate_input(trial) <= budget:
                payload = trial
    return payload


def bind_context(spec, places):
    """Only unique exact names in the requested city may bind."""
    data = source_context(spec)
    if not data:
        return
    if data.get('provider_policy', {}).get('only'):
        raise ValueError('资料限定只用问道，尚未授权地图核验，不能生成已绑定的正式行程。')
    bindings = {}
    city = spec['destination'].removesuffix('市')
    for target in data.get('targets', []):
        matches = [pid for pid, p in places.items() if p['name'] == target['name']
                   and str(p.get('cityname') or p.get('city') or '').removesuffix('市') == city]
        if len(matches) == 1:
            bindings[target['id']] = matches[0]
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
    from core.travel_answer import prose_supported
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
        return {**description, 'why_visit': description.get('why_visit') or arrangement_reason(spec, item['reason']),
                'source_status': 'provider_unverified'}
    if not intro:
        return None
    tips = deepcopy([r for r in material.get('tips', []) if prose_supported(r['text'],material.get('tips',[]))][:2])
    result = {'intro': {'text': intro['text'], 'source_ids': intro['source_ids']},
              'why_visit': arrangement_reason(spec, item['reason']), 'tips': tips, 'source_status': 'provider_unverified'}
    validate_description(result, material, spec)
    return result


def arrangement_reason(spec, reason):
    preferences = []
    for text in spec.get('preferences', []):
        if (len(text) <= 35 and not INSTRUCTION.search(text)
                and not re.search(r'\d|元|票价|开放时间|预约|始建|建于|商家|演出', text)
                and len('；'.join([*preferences, text])) <= 70):
            preferences.append(text)
    prefix = ('结合你提出的“' + '；'.join(preferences) + '”，') if preferences else ''
    return prefix + '将这里作为' + reason + '的一站，停留可按体力调整。'


def validate_description(value, material, spec):
    from core.travel_answer import prose_supported
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
    if not isinstance(why, str) or len(why) > 150 or INSTRUCTION.search(why):
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
