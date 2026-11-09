"""Bounded narrative generation over an already validated trip; facts stay structural."""
import hashlib
import json
import re
from copy import deepcopy

from core.context_contract import complete_json, response_json, trip_answer_basis as answer_basis
from core.execution_scope import ExecutionRevoked
from core.model_budget import model_call_context
from infrastructure.skill_manager import skill_request, skill_text
from tools.trip.constraints import reference_date_note, facts, travel_preferences, effective_city_windows
from tools.trip.planner import meal_scopes, _meal_anchors, assess_hotels, HOTEL_PATTERN, HOTEL_POLICY_VERSION, activity_material_state
from tools.research.evidence import concrete_claim, cultural_note
from tools.research.sources import INSTRUCTION


class AnswerContractError(ValueError):
    def __init__(self,code,message):
        super().__init__(message)
        self.code=code


def validate_answer(value,trip):
    if not isinstance(value,dict) or value.keys()-{'overview','change_summary','days','stay','meals'}:
        raise AnswerContractError('invalid_shape','行程表达结构无效。')
    if isinstance(value.get('stay'),str) and re.search(r'预算|房价|价格|费用',value['stay']):
        raise AnswerContractError('lodging_price_claim','住宿价格与预算结论由结构化证据呈现。')
    def prose(text,limit=500):
        if (not isinstance(text,str) or len(text)>limit or re.search(
                r'\d|[一二两三四五六七八九十百]+(?:元|点|时|分钟|小时)|https?://|\[来源|开放|营业|闭馆|预约|免费|收费|门票|票价|车次|元[/／]',text)):
            raise AnswerContractError('unverified_practical_fact','自由表达包含未核对的实用事实。')
        if concrete_claim(text) or INSTRUCTION.search(text):raise AnswerContractError('unsupported_claim','自由表达含未经支持的事实。')
        if travel_preferences([text]) != [text]:raise AnswerContractError('workflow_prose','旅行说明不能包含任务操作指令。')
        return text
    result={key:prose(value.get(key,'')) for key in ('overview','change_summary','stay')}
    days=value.get('days',[])
    if not isinstance(days,list) or len(days)>len(trip['plan']['days']):raise AnswerContractError('invalid_days','日期说明无效。')
    result['days']={}
    for row in days:
        if (not isinstance(row,dict) or set(row)!={'day_index','text'} or type(row['day_index']) is not int
                or not 1<=row['day_index']<=len(trip['plan']['days']) or str(row['day_index']) in result['days']):
            raise AnswerContractError('invalid_day_binding','日期说明未绑定本次行程。')
        result['days'][str(row['day_index'])]=prose(row['text'])
    meals=value.get('meals',[])
    if not isinstance(meals,list):raise AnswerContractError('invalid_meals','用餐说明无效。')
    valid={(s['day_index'],s['meal']) for s in meal_scopes(trip['spec'],trip['plan'])}
    if len(meals)>len(valid):raise AnswerContractError('duplicate_meals','用餐说明重复。')
    result['meals']={}
    for row in meals:
        if (not isinstance(row,dict) or set(row)!={'day_index','meal','text'} or type(row['day_index']) is not int
                or (row['day_index'],row['meal']) not in valid):raise AnswerContractError('invalid_meal_binding','餐次说明未绑定可用时间。')
        result['meals'][f"{row['day_index']}:{row['meal']}"]=prose(row['text'],250)
    if not result['overview']:raise AnswerContractError('missing_overview','缺少路线解释。')
    return result


def presentation_trip(trip):
    """Read-only projection for legacy preferences and generated boilerplate."""
    value=deepcopy(trip)
    previous=value['spec'].get('city_windows',{})
    current=effective_city_windows(value['spec'])
    if previous!=current:
        value['spec']['city_windows']=current
        if any(previous.get(kind,{}).get(field)!=row.get(field)
               for kind,row in current.items() for field in ('day_index','time_text','independent')):
            value.pop('answer',None);value.pop('answer_basis',None)
    preferences=value['spec'].get('hotel_preferences',{})
    old_hotels=value['plan'].get('hotel_recommendations',[])
    if (old_hotels and (value['spec'].get('budgets',{}).get('accommodation') or preferences.get('preferred_brands') or preferences.get('preferred_groups'))
            and any(HOTEL_PATTERN.search(str(p.get('type',''))) for p in value['sources'].values())):
        assessed={row['poi_id']:row for row in assess_hotels(value['spec'],value['plan'],value['sources'])}
        old_by_id={row['poi_id']:row for row in old_hotels}
        hotels=[assessed.get(row['poi_id'],deepcopy(row)) for row in old_hotels]
        for row in hotels:
            if old_by_id.get(row['poi_id'],{}).get('routes'):row['routes']=deepcopy(old_by_id[row['poi_id']]['routes'])
        if hotels!=old_hotels:
            value['plan']['hotel_recommendations']=hotels
            value.pop('answer',None);value.pop('answer_basis',None)
        if value['plan'].get('hotel_recommendation_policy')!=HOTEL_POLICY_VERSION:
            value['plan']['hotel_review_note']='原住宿候选需按当前预算与品牌要求重新核对；此处未自动替换或保存酒店。'
    if value['plan'].get('hotel_quotes'):
        from tools.hotel_prices.quotes import evaluate,project_quote
        from tools.hotel_prices.tool import reference_text
        value['plan']['hotel_quotes']=project_quote(value['plan']['hotel_quotes'])
        value['plan']['hotel_quotes']['offers']=evaluate(value['spec'],value['plan']['hotel_quotes'],places=value['sources'])
        value['plan']['hotel_quotes']['reference_text']=reference_text(value['plan']['hotel_quotes']) if value['plan']['hotel_quotes'].get('reference_clues') or value['plan']['hotel_quotes'].get('unavailable_prices') else ''
        if value['plan']['hotel_quotes'].get('coverage'):
            value['plan']['hotel_quotes']['coverage']['comparable']=sum(o['status']=='reference_match' for o in value['plan']['hotel_quotes']['offers'])
    if 'preferences' in value['spec']:
        value['spec']['preferences']=travel_preferences(value['spec']['preferences'])
    for day in value['plan']['days']:
        for activity in day['activities']:
            description=activity.get('description',{})
            why=description.get('why_visit','')
            if (re.fullmatch(r'结合你提出的“[^”]*”，将这里作为[^。]*的一站，停留可按体力调整。',why)
                    or travel_preferences([why]) != [why]):
                description.pop('why_visit',None)
    return value


def hotel_candidate_groups(rows):
    groups=[('住宿候选',[]),('待核价候选（未确认符合预算）',[]),('其他品牌备选（未满足品牌偏好）',[]),('未通过当前筛选的原候选（需重新核对）',[])]
    for row in rows:
        index=3 if row.get('status')=='conflict' else 2 if row.get('status')=='fallback' else 0 if row.get('price_status') in {'not_requested','reference_in_budget'} else 1
        groups[index][1].append(row)
    if groups[0][1] and all(r.get('price_status')=='reference_in_budget' for r in groups[0][1]):
        groups[0]=('参考房价在预算范围的候选',groups[0][1])
    return [(label,items) for label,items in groups if items]


class TripAnswerWriter:
    def __init__(self,store,client,model):
        self.store,self.client,self.model=store,client,model

    @skill_request
    def generate(self,event,trip,changes):
        key='trip-answer:'+hashlib.sha256(event.event_key.encode()).hexdigest()
        saved=self.store.delivery.get_event_tool_result(key)
        if saved:return json.loads(saved).get('answer')
        trip=presentation_trip(trip)
        spec=trip['spec']
        payload={'requirements':{k:v for k,v in spec.items() if k in {'destination','day_count','preferences','budgets',
            'hotel_preferences','hotel_stay','dining_preferences','accommodation_text','dining_text','city_windows','planning_window'}},'changes':changes,
            'days':[{'day_index':day['day_index'],'activities':[{
                'poi_id':a['poi_id'],'name':trip['sources'][a['poi_id']]['name'],'period':a['period'],
                'subactivities':[{'poi_id':child['poi_id'],'name':trip['sources'][child['poi_id']]['name']} for child in a.get('subactivities',[])],
                'cultural_state':activity_material_state(spec,a['poi_id']),
                'intro':a.get('description',{}).get('intro',{}).get('text','')} for a in day['activities']]}
                for day in trip['plan']['days']], 'meals':meal_scopes(spec,trip['plan'])}
        traffic=trip['plan'].get('intercity',{})
        payload['transport']={key:traffic.get(key) for key in ('completeness','issues','limitations')}
        payload['transport'].update({direction:{'query_status':traffic[direction].get('status'),
            'selected':bool(traffic[direction].get('selected'))} for direction in ('outbound','inbound') if direction in traffic})
        payload['lodging']={'candidates':[{'name':trip['sources'][row['poi_id']]['name'],
            'status':row.get('status'),'price_status':row.get('price_status','unknown'),
            'brand_match':row.get('brand_match'),'issues':row.get('issues',[])} for row in trip['plan'].get('hotel_recommendations',[])]}
        if 'hotel_stay' in payload['requirements']:
            from tools.hotel_prices.quotes import public_stay
            payload['requirements']['hotel_stay']=public_stay(spec['hotel_stay'])
        if trip['plan'].get('hotel_quotes'):
            from tools.hotel_prices.quotes import evaluate
            payload['lodging']['quote_states']=[{k:q.get(k) for k in ('hotel_name','room_name','status','issues')}
                for q in evaluate(spec,trip['plan']['hotel_quotes'],places=trip['sources'])]
        try:
            with model_call_context('trip_answer_write',stage_id='trip.answer',prompt_version='trip-answer-v7-cultural-materials'):
                response=self.client.chat.completions.create(model=self.model,response_format={'type':'json_object'},
                    max_completion_tokens=2048,messages=[{'role':'system','content':skill_text('trip.answer','system')},
                        {'role':'user','content':complete_json(payload,20000)}])
            answer=validate_answer(response_json(response),trip)
        except ExecutionRevoked:raise
        except Exception as error:
            self.store.diagnostics.record('planning',{'event_key':event.event_key,'name':'trip_answer','status':'fallback',
                                                     'error_type':type(error).__name__,
                                                     'validation':{'code':getattr(error,'code','invalid_response'),'path':'trip.answer'}})
            answer=None
        self.store.delivery.save_event_tool_result(key,event.event_key,'trip_answer',{},json.dumps({'answer':answer},ensure_ascii=False))
        return answer


def render_answer(trip,format_transport,*,preview=False,mode='full'):
    """Narrative paragraphs plus canonical fields; train/time/price never come from prose."""
    spec,plan,places,answer=trip['spec'],trip['plan'],trip['sources'],trip['answer']
    lines=[f"{'行程修改预览（尚未保存）' if preview else '旅行行程'}：{trip['title']}（{trip['trip_id']}，版本 {trip.get('version',1)}）"]
    sections=[]
    def transport(direction):
        sections.append({'kind':'text','text':'\n'.join(lines)})
        lines.clear()
        text=format_transport(plan.get('intercity',{}),direction)
        if text:sections.append({'kind':'transport','direction':direction,'text':'\n'.join(text)})
    if reference_date_note(spec):lines.append(reference_date_note(spec))
    traffic=plan.get('intercity',{})
    unresolved=traffic.get('completeness') in {'partial','conflict'}
    lines.append('交通核对尚未完成，已有城市安排保留；请核对下方未解决事项。' if mode=='update' and unresolved
                 else answer.get('change_summary') if mode=='update' else answer['overview'])
    for row in facts(spec):
        if row['field'].startswith('city_windows.'):lines.append(row['label']+'：'+row['value'])
        if row['field'].startswith('budgets.'):lines.append(row['label']+'目标：'+row['value']+'；实际价格待核实。')
    if mode!='update':
        transport('outbound')
        for day in plan['days']:
            lines.append(f"\n### 第{day['day_index']}天 · {day.get('date') or '日期待定'}")
            if answer['days'].get(str(day['day_index'])):lines.append(answer['days'][str(day['day_index'])])
            for activity in day['activities']:
                lines.append(f"- {activity['period']}：{places[activity['poi_id']]['name']}")
                if activity.get('subactivities'):
                    lines.append('  同组地点：'+'、'.join(places[c['poi_id']]['name'] for c in activity['subactivities'])+'（接驳、游览时长与票务需分别核对）。')
                intro=activity.get('description',{}).get('intro',{}).get('text')
                lines.append('  '+intro if intro else '  '+cultural_note(activity_material_state(spec,activity['poi_id'])))
            for leg in day.get('legs',[]):
                seconds=leg.get('duration_seconds')
                duration=f"约{(seconds+59)//60:g}分钟（查询时路网参考）" if isinstance(seconds,(int,float)) and not leg.get('error') else '移动耗时待核实'
                lines.append(f"{places[leg['from_poi']]['name']} → {places[leg['to_poi']]['name']}：{duration}")
            if not day.get('legs') and len(day['activities'])>1:
                lines.append('活动间移动：'+' → '.join(places[a['poi_id']]['name'] for a in day['activities'])+'（方式与耗时待核对）。')
            window=spec.get('intercity_windows',{}).get(str(day['day_index']),{})
            if window.get('latest') and day['day_index']==spec['day_count']:
                lines.append(f"离开游览区域的参考上限：{window['latest'][11:16]}（已预留返站与进站缓冲，接驳未核实时仍为假设）。")
            for scope in meal_scopes(spec,plan):
                if scope['day_index']!=day['day_index']:continue
                text=answer['meals'].get(f"{scope['day_index']}:{scope['meal']}")
                anchors=_meal_anchors(day,places,scope['meal'])
                area='、'.join(dict.fromkeys(p['name'] for p in anchors)) or '住宿地点'
                lines.append(f"- {scope['meal']}：{text or '就近用餐，具体菜品按口味选择。'}（结合{area}附近安排，营业和配料需向商家确认）")
        transport('inbound')
    else:
        lines.append('完整当前草案可在行程侧栏查看。')
        for day in plan['days']:
            if f"day.{day['day_index']}" in trip.get('answer_changes',[]):
                lines.append(f"第{day['day_index']}天调整为："+'；'.join(f"{a['period']} {places[a['poi_id']]['name']}" for a in day['activities']))
        for direction in ('outbound','inbound'):
            if 'intercity.'+direction in trip.get('answer_changes',[]):transport(direction)
    if mode!='update' or any(key.startswith(('hotel','accommodation','budgets.accommodation')) for key in trip.get('answer_changes',[])):
        lines.append('\n### 住宿建议\n')
        lines.append((answer['stay'] or '围绕主要游览地点选择交通方便的住宿区域。')+'\n')
        if plan.get('hotel_quotes'):
            from tools.hotel_prices.tool import quote_text
            lines.append(quote_text(spec,plan['hotel_quotes'],places=places))
        hotels=plan.get('hotel_recommendations',[])
        if plan.get('hotel_review_note'):lines.append(plan['hotel_review_note'])
        for label,rows in hotel_candidate_groups(hotels):
            lines.append('\n'+label+'：')
            for row in rows:lines.append(f"- {places[row['poi_id']]['name']}：{'；'.join([*row['reasons'],*row['issues']])}")
        if spec.get('hotel_requested') and not hotels:
            lines.append('当前资料中没有可确认匹配的住宿候选；预算与品牌要求已保留，未用旧酒店作为已满足要求的替代。')
        lines.append('\n入住前确认房间和行李寄存；退房日先处理行李，再衔接返站。入住、退房时刻以酒店确认为准。\n')
    if mode!='update' or any(key.startswith(('dining','budgets.food')) for key in trip.get('answer_changes',[])):
        scopes=trip.get('answer_meal_scopes',[])
        options=[option for option in plan.get('dining_options',[]) if not scopes or any(
            s['day_index']==option['day_index'] and s['meal']==option['meal'] for s in scopes)]
        if options:lines.append('\n### 餐饮建议\n')
        for option in options:
            lines.append(f"- **第{option['day_index']}天{option['meal']}**："+('、'.join(places[r['poi_id']]['name'] for r in option['candidates']) or option.get('note','暂无有证据的匹配候选。')))
    notes=[]
    if mode!='update':
        notes.append('出发前按实际游览日复核所选景点的开放和预约要求；尚缺适用证据的事项保持待确认。')
    notes.extend(plan.get('intercity',{}).get('limitations',[]))
    if notes:
        lines.append('\n### 出行提醒\n')
        lines.extend('- '+note for note in dict.fromkeys(notes))
    sections.append({'kind':'text','text':'\n'.join(filter(None,lines))})
    text='\n'.join(section['text'] for section in sections)
    return text,[{k:v for k,v in section.items() if k!='text' or section['kind']=='text'} for section in sections]
