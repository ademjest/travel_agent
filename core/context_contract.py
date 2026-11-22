"""Complete structured projections shared by all model entry points."""
import json
import hashlib

from core.model_budget import ModelBudgetExceeded


POLICY_VERSION = 'context-v3'
READ_SLOT_FIELDS = {'location','city','origin','destination','keywords','reference_time','intents',
                    'travel_date','origin_scope','destination_scope','train_preference','train_preference_text','departure_window','queries',
                    'trip_id','expected_trip_version','plan_artifact','budget_unit','period','title','trigger'}
READ_SLOT_FIELDS.update({'seat_class','seat_requirement','price_preference'})
READ_SLOT_FIELDS.update({'hotel_request','hotel_targets','hotel_candidates','hotel_date_proposal','hotel_remaining_targets','last_result_ref','operations'})
POI_FIELDS = {'id','name','address','type','location','adname','cityname','rating','cost','lowest_price','queried_at',
              'parent','parent_id','children','visit_group_id'}


def hotel_request_projection(request):
    value={k:request[k] for k in ('mode','targets','discovery','reference','stay','budget') if k in request}
    if 'stay' in value:
        from core.hotel_contract import STAY_FIELDS
        value['stay']={k:v for k,v in value['stay'].items() if k in STAY_FIELDS}
    if 'budget' in value:value['budget']={k:v for k,v in value['budget'].items() if k in {'value','unit_text'}}
    return value


def hotel_state_projection(slots):
    from core.hotel_contract import date_proposal
    return {'hotel_request':hotel_request_projection(slots.get('hotel_request',{})),
        'candidates':[{k:p.get(k) for k in ('name','address','cityname')} for p in slots.get('hotel_candidates',[])],
        'selection_status':slots.get('selection_status'),
        'date_proposal':{k:v for k,v in date_proposal(slots.get('hotel_date_proposal',{})).items() if k in {'check_in','check_out','status','version','presented','source_kind'}},
        'has_saved_result':bool(slots.get('last_result_ref')),
        'source_policy':{k:bool(v) for k,v in slots.get('source_context',{}).get('provider_policy',{}).items() if isinstance(v,bool)}}


def task_projection(task):
    return {'type':task.task_type,'status':task.status,'missing':list(task.missing_slots),
            'request':task.initial_request[:240] if task.task_type=='hotel' else task.initial_request,
            'slots':hotel_state_projection(task.slots) if task.task_type=='hotel' else {key:value for key,value in task.slots.items() if key in READ_SLOT_FIELDS}}


def complete_json(value, max_chars, *, purpose=''):
    raw = json.dumps(value,ensure_ascii=False,separators=(',',':'))
    if len(raw) > max_chars:
        error=ModelBudgetExceeded('本次意图上下文超过长度上限，未发送模型请求；可先使用本地资料入口，或明确要处理的对象。' if purpose=='intent' else
                                 '系统组装的行程资料超过本步骤输入上限，尚未生成本轮预览；原行程和已取得资料保留。'
                                 if purpose else '当前必要信息超过本次输入预算，请分段说明或缩小任务范围。')
        if purpose:
            error.details={'code':'input_projection_limit','path':purpose,'actual':len(raw),'max':max_chars}
            error.input_parts={key:len(json.dumps(item,ensure_ascii=False,separators=(',',':')))
                               for key,item in value.items()} if isinstance(value,dict) else {}
        raise error
    return raw


def project_places(places):
    return [{key:value for key,value in place.items() if key in POI_FIELDS} for place in places]


def trip_answer_basis(trip):
    """Shared persistence/writer contract; identical to the original narrative fingerprint."""
    spec={k:v for k,v in trip['spec'].items() if not k.startswith('_') and k not in {
        'fact_sources','field_sources','constraint_overrides','planning_decision','planning_evidence','required_entities'}}
    data=[spec,trip['plan'],trip['sources']]
    return hashlib.sha256(json.dumps(data,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def reference_projection(reference, text_limit=4000):
    """Bounded model context; the full owned snapshot remains available to the business handler."""
    if not reference:return {}
    content=reference.get('content','')
    value={k:reference[k] for k in ('message_id','role','kind','created_at','availability','attachments','association_note','related_result_created_at') if k in reference}
    value.update(content=content[:text_limit],omitted_content_chars=max(0,len(content)-text_limit))
    if reference.get('rail'):value['rail']=reference['rail']
    results=[]
    for row in reference.get('tool_results',[]):
        data=str(row.get('data',''))
        item={k:row[k] for k in ('action','status','error_code') if k in row}
        item.update(data=data[:1200],omitted_data_chars=max(0,len(data)-1200))
        if len(json.dumps(results+[item],ensure_ascii=False))>min(2200,max(600,text_limit)):break
        results.append(item)
    if results:value['tool_results']=results
    if reference.get('tool_results'):value['omitted_tool_results']=len(reference['tool_results'])-len(results)
    trip=reference.get('trip')
    if trip:
        value['trip']={'version':trip.get('version'),'is_preview':reference.get('is_preview'),
            'requirements':{k:v for k,v in trip['spec'].items() if k in {'destination','start_date','end_date','day_count',
                'preferences','must_visit','budgets','hotel_stay','intercity_request'}},
            'activities':[{'day':d['day_index'],'places':[trip['sources'].get(a['poi_id'],{}).get('name','')
                for a in d['activities']]} for d in trip['plan']['days']]}
    return value


def response_text(response):
    choice = response.choices[0]
    if getattr(choice, 'finish_reason', None) in {'length', 'content_filter'}:
        raise ValueError('模型输出被截断或过滤，不能作为完整业务结果。')
    return choice.message.content or '{}'


def response_json(response):
    return json.loads(response_text(response))
