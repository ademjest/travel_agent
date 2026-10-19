"""Source metadata and deterministic projections of existing trip/draft state."""
from copy import deepcopy
import json
import re


def edit_text(field, value):
    if field == 'hotel_budget' and re.fullmatch(r'[1-9][0-9]{0,5}(?:-[1-9][0-9]{0,5})?',value):
        amounts=[int(v) for v in value.split('-')]
        if len(amounts)==2 and amounts[0]>amounts[1]:
            raise ValueError('预算范围下限不能高于上限。')
        return f'酒店预算改为{value}元/间/晚'
    if field == 'night' and value == 'false':
        return '不要夜游'
    raise ValueError('请填写明确的住宿预算（人民币每间每晚）或选择不夜游。')

LABELS = {'budgets':'预算','hotel_preferences':'住宿偏好','accommodation_text':'住宿要求',
          'transport_preferences':'交通偏好','transport_text':'交通方式','gentle':'轻松节奏',
          'no_night':'不安排夜游','dining_preferences':'餐饮偏好','dining_text':'餐饮要求',
          'start_date':'开始日期','end_date':'结束日期','day_count':'旅行天数'}
LABELS.update({'hotel_requested':'需要住宿候选','restaurant_requested':'需要餐饮候选',
               'preferences':'旅行要求','preference_interests':'兴趣','indoor_days':'室内活动日期',
               'outdoor_days':'室外活动日期','indoor_periods':'室内活动时段','outdoor_periods':'室外活动时段'})
CHANGE_FIELDS = {'budget':'budgets','budgets':'budgets','accommodation':'hotel_preferences',
                 'dining':'dining_preferences','transport':'transport_preferences','pace':'gentle','night':'no_night',
                 'requirements':'preferences','interests':'preference_interests'}
DETAIL_LABELS = {'preferred':'优先','discouraged':'尽量避免','forbidden':'不使用',
    'preferred_brands':'品牌','brand_mode':'品牌要求','chain_mode':'连锁要求','minimum_rating':'最低评分',
    'rating_mode':'评分要求','rating_origin':'评分来源','quality_requested':'高评分',
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
            from core.trip_updates import MODES
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
    rows=[]
    for key,label in LABELS.items():
        if key not in spec:
            continue
        if key == 'budgets':
            for scope,budget in spec[key].items():
                field='budgets.'+scope
                value=budget.get('value','')
                unit=budget.get('unit_text','')
                rows.append({'field':field,'label':{'accommodation':'住宿预算','food':'餐饮预算','transport':'交通预算','trip':'全程预算'}.get(scope,scope),
                    'value':value if unit and unit in value else value+' '+(unit or '（单位未明确）'),
                    'source':spec.get('fact_sources',{}).get(field,{}).get('source','历史来源未完整记录')})
        else:
            rows.append({'field':key,'label':label,'value':display(spec[key],key),
                         'source':spec.get('fact_sources',{}).get(key,{}).get('source','历史来源未完整记录')})
    return rows


def state_context(store,event):
    if not hasattr(store,'trips') or not re.search(r'行程|预算|住宿|酒店|交通|夜游|节奏|餐饮|要求|偏好',event.content):
        return ''
    trips=store.trips.list_for_owner(event)
    referenced=store.trips.referenced(event) if event.reply_to_id else None
    if referenced:
        trips=(referenced,)
    if len(trips)!=1:
        return '当前有多份行程，需明确目标后才能确认其需求。' if trips else ''
    trip=trips[0]
    draft=store.trips.draft(event,trip)
    valid_draft=bool(draft and not draft['recovered'] and draft['task'].slots.get('expected_trip_version')==trip['version'])
    pending={r['field']:r['value'] for r in facts(draft['candidate']['spec'])} if valid_draft else {}
    saved={r['field']:r for r in facts(trip['spec'])}
    rows=[];omitted=[]
    for field in sorted(saved.keys() | pending.keys()):
        if field == 'preferences':
            continue  # Legacy free text is not an authoritative current-value field.
        row={'field':field,'saved':saved.get(field,{}).get('value','未明确')}
        if field in pending and pending[field] != row['saved']:
            row['pending_not_saved']=pending[field]
        if len(json.dumps(rows+[row],ensure_ascii=False))<=1300:
            rows.append(row)
        else:
            omitted.append(field)
    return json.dumps({'trip_id':trip['trip_id'],'saved_version':trip['version'],
        'fields':rows,'omitted_fields':omitted,'note':'saved为已保存；pending_not_saved为最新要求但尚未确认；未提供字段不可根据旧摘要补猜。'},ensure_ascii=False)


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
