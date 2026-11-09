"""Typed user requests for read-only hotel prices; identity and ownership stay server-side."""
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta
import re

from core.time_input import NUMBER, number, parse_reminder_time, BEIJING
from core.event_idempotency import event_operation_key

FIELDS = {'mode','targets','discovery','reference','stay','budget','source_spans','proposal_response'}
STAY_FIELDS = {'check_in','check_out','rooms','adults','children_ages','room_requirement',
               'breakfast_requirement','cancellation_requirement'}
LABELS = {'targets':'具体酒店及城市，或希望寻找的城市和区域','check_in':'入住日期',
          'check_out':'退房日期','rooms':'房间数','adults':'成人数','children_ages':'儿童年龄（无儿童请明确）'}
NEW_RUN_COMMAND='开始新一轮酒店核价'
RUN_LIMIT_CODES={'run_call_limit','run_token_limit','event_call_limit','event_token_limit'}
NIGHT_COUNT='('+NUMBER+r')(?:个)?(?:晚上?|夜)'

def obj(properties):
    return {'type':'object','properties':properties,'additionalProperties':False}

TEXT={'type':'string'}
TEXTS={'type':'array','items':TEXT,'maxItems':12}
SCHEMA=obj({
    'mode':{'type':'string','enum':['lookup','refresh','saved']},
    'targets':{'type':'array','maxItems':12,'items':obj({k:TEXT for k in ('name','city','address_hint')})},
    'discovery':obj({'city':TEXT,'area':TEXT,'preferred_brands':TEXTS,'preferred_groups':TEXTS}),
    'reference':obj({'selection':TEXT,'ordinal':{'type':'integer','minimum':1,'maximum':12}}),
    'proposal_response':{**obj({'kind':{'type':'string','enum':['stay_dates']},
        'decision':{'type':'string','enum':['accept','reject']},'source':TEXT}), 'required':['kind','decision','source']},
    'stay':obj({**{k:TEXT for k in STAY_FIELDS-{'rooms','adults','children_ages'}},
        'rooms':{'type':'integer','minimum':1,'maximum':20},'adults':{'type':'integer','minimum':1,'maximum':20},
        'children_ages':{'type':'array','maxItems':8,'items':{'type':'integer','minimum':0,'maximum':17}}}),
    'budget':obj({k:TEXT for k in ('value','unit_text','source')}), 'source_spans':TEXTS})

def merge_request(previous, changes):
    result=deepcopy(previous)
    for key,value in changes.items():
        if isinstance(value,dict):result[key]={**result.get(key,{}),**deepcopy(value)}
        else:result[key]=deepcopy(value)
    return result

def leaves(value, path=''):
    if isinstance(value,dict):
        for key,item in value.items():yield from leaves(item,path+'.'+key if path else key)
    elif isinstance(value,list) and value:
        for index,item in enumerate(value):yield from leaves(item,path+'.'+str(index))
    else:yield path,value

def validate_request(value):
    # Imported lazily to keep the schema usable by the common tool catalog.
    from core.tool_result import validate_arguments
    error=validate_arguments(value,SCHEMA)
    if error:raise ValueError(error)
    if any(isinstance(v,str) and (not v or len(v)>240) for _,v in leaves(value)):
        raise ValueError('酒店查询字段须为1至240字。')
    return deepcopy(value)

class HotelEvidenceError(ValueError):
    """Unproven fields are excluded while verified conditions remain resumable."""
    def __init__(self,issues,request):
        self.issues,self.request=issues,request
        self.code,self.path='unverified_hotel_field',issues[0]['path']
        super().__init__('；'.join(dict.fromkeys(row['reason'] for row in issues)))


def evidenced_request(changes, text, *, previous=None, reference_time=None, selection=None):
    """Accept current explicit fields or fields already bound to an owned task/result."""
    changes=validate_request(changes);previous=previous or {}
    prior=dict(leaves(previous));reference=reference_time or datetime.now(BEIJING)
    if isinstance(reference,str):reference=datetime.fromisoformat(reference)
    no_children=bool(re.search(r'无儿童|没有儿童|不带(?:儿童|孩子)|仅(?:成人|成年人)|(?:都是|均为)(?:成人|成年人)',text))
    bound_targets={str(i) for i,target in enumerate(changes.get('targets',[])) if target and any(
        all(old.get(k)==v for k,v in target.items()) for old in previous.get('targets',[]))}
    issues=[]
    for path,value in leaves(changes):
        target_field=path.startswith('targets.')
        if (path=='mode' or path.startswith('source_spans.') or path in {'proposal_response.kind','proposal_response.decision'}
                or target_field and path.split('.')[1] in bound_targets
                or not target_field and prior.get(path,object())==value):continue
        field=path.split('.')[-1];reason=''
        if path=='reference.ordinal':
            if selection!=value and not any(number(n)==value for n in re.findall(r'第('+NUMBER+r')(?:家|个)',text)):
                reason='酒店序号缺少本轮引用依据。'
        elif path.startswith('stay.') and field in {'rooms','adults'}:
            unit=r'间(?:房)?' if field=='rooms' else r'(?:位|名|个)?(?:成人|成年人)'
            proved=any(number(n)==value for n in re.findall('('+NUMBER+')'+unit,text))
            if field=='adults' and no_children:
                proved=proved or any(number(n)==value for n in re.findall('('+NUMBER+r')(?:个?人|位)',text))
            if not proved:reason=LABELS[field]+'尚不能对应本轮原话，请确认。'
        elif path=='stay.children_ages':
            if value!=[] or not no_children:reason='请明确是否有儿童及其年龄。'
        elif path.startswith('stay.children_ages.'):
            if not any(number(n)==value for n in re.findall('('+NUMBER+r')\s*岁',text)):reason='儿童年龄缺少依据。'
        elif path in {'stay.check_in','stay.check_out'}:
            if value not in text:
                parsed=parse_reminder_time(text,reference)
                if not parsed.day or parsed.day!=value:
                    reason=LABELS[field]+'缺少当前原话或绑定条件依据，请保留日期原文。'
        elif not isinstance(value,str) or value not in text:
            reason='酒店查询字段缺少本轮原话或已绑定资料依据：'+path
        if reason:issues.append({'path':path,'reason':reason})
    accepted=deepcopy(changes);retained=deepcopy(previous)
    for issue in issues:
        parts=issue['path'].split('.')
        for values in (accepted,retained):
            if parts[0]=='stay':values.get('stay',{}).pop(parts[1],None)
            else:values.pop(parts[0],None)
    result=merge_request(retained,accepted)
    result.setdefault('mode','lookup')
    result.setdefault('source_spans',[text[:200]])
    if issues:raise HotelEvidenceError(issues,result)
    return result


def resolve_stay(stay, reference_time, text='',previous=None):
    result=deepcopy(stay)
    reference=datetime.fromisoformat(reference_time) if isinstance(reference_time,str) else reference_time
    for key in ('check_in','check_out'):
        if not result.get(key):continue
        try:result[key]=date.fromisoformat(result[key]).isoformat()
        except ValueError:
            if result[key] in {'周末','这个周末','下个周末','当天','最后一天'}:
                result.pop(key);continue
            anchor=(previous or {}).get(key) or result.get('check_in')
            day_only=re.fullmatch('('+NUMBER+r')[日号]',result[key])
            if day_only and anchor and re.fullmatch(r'\d{4}-\d{2}-\d{2}',anchor):
                try:result[key]=date.fromisoformat(anchor).replace(day=number(day_only[1])).isoformat()
                except ValueError:result.pop(key)
                continue
            # Date normalization is independent of whether a query is historical or current.
            # Reminders reject past days; hotel execution applies its own future-date rule later.
            explicit=re.fullmatch(r'(?:(20\d{2})年)?('+NUMBER+r')月('+NUMBER+r')[日号]?',result[key])
            if explicit:
                try:result[key]=date(int(explicit[1] or reference.astimezone(BEIJING).year),number(explicit[2]),number(explicit[3])).isoformat()
                except ValueError:result.pop(key)
                continue
            parsed=parse_reminder_time(result[key],reference)
            if parsed.day:result[key]=parsed.day
            else:result.pop(key)
    nights=re.findall(r'住'+NIGHT_COUNT,text)
    extra=re.findall(r'(?:再|多)住'+NIGHT_COUNT,text)
    if extra:
        checkout=(previous or {}).get('check_out')
        if checkout and len(extra)==1:
            result['check_out']=(date.fromisoformat(checkout)+timedelta(days=number(extra[0]))).isoformat()
        else:result.pop('check_out',None)
    elif result.get('check_in') and not result.get('check_out') and len(nights)==1:
        result['check_out']=(date.fromisoformat(result['check_in'])+timedelta(days=number(nights[0]))).isoformat()
    return result

def missing_request(request, now):
    if request.get('mode')=='saved':return []
    missing=[];stay=request.get('stay',{})
    if not request.get('targets') and not request.get('discovery',{}).get('city'):missing.append('targets')
    if any(not t.get('name') or not t.get('city') for t in request.get('targets',[])):missing.append('targets')
    missing.extend(k for k in LABELS if k!='targets' and stay.get(k) is None)
    if stay.get('check_in') and stay.get('check_out'):
        nights=(date.fromisoformat(stay['check_out'])-date.fromisoformat(stay['check_in'])).days
        if not 1<=nights<=14:missing.append('有效的连续1至14晚住宿日期')
        if date.fromisoformat(stay['check_in'])<now.astimezone(BEIJING).date():missing.append('未来入住日期')
    return missing


HOTEL_STATE_FIELDS={'request':'hotel_request','resolved_targets':'hotel_targets','target_candidates':'hotel_candidates',
    'request_items':'hotel_request_items',
    'date_proposal':'hotel_date_proposal','remaining_targets':'hotel_remaining_targets','field_provenance':'field_provenance',
    'expired':'hotel_expired','target_bindings':'hotel_target_bindings','candidate_set_id':'candidate_set_id',
    'candidate_set_version':'candidate_set_version','candidate_request_fingerprint':'candidate_request_fingerprint',
    'selection_status':'selection_status','selected_candidate_ids':'selected_candidate_ids','budget_block':'model_budget_block'}


def merge_hotel_state(previous,payload):
    """Absent receipt fields preserve state; only explicit server transitions replace it."""
    state=deepcopy(previous)
    for field,slot in HOTEL_STATE_FIELDS.items():
        if field in payload:state[slot]=deepcopy(payload[field])
    if 'source_policy' in payload:state['source_context']={'provider_policy':payload['source_policy']}
    if payload.get('snapshot',{}).get('source_event_id'):state['last_result_ref']=payload['snapshot']['source_event_id']
    if state.get('hotel_candidates') and not state.get('candidate_set_id'):
        state.update(candidate_transition(state,state['hotel_candidates'],state.get('hotel_request',{}),'legacy'))
    return state


def hotel_task_views(task):
    if task.task_type=='hotel':return [task]
    if task.task_type!='read_batch':return []
    result=[]
    for i,row in enumerate(task.slots.get('operations',[])):
        if row['domain']!='hotel' or row['status'] not in {'needs_input','failed'}:continue
        receipt=task.slots['receipts'][i];payload=receipt.get('payload') or {}
        state=task.slots.get('hotel_states',{}).get(str(i),merge_hotel_state({},payload))
        state.setdefault('hotel_request',row['arguments'])
        result.append(replace(task,task_type='hotel',missing_slots=tuple(receipt.get('missing_fields',())),slots={**state,
            **{k:task.slots[k] for k in ('budget_run_key','budget_run_parent','budget_run_started_by') if k in task.slots},
            'hotel_query_index':i,'hotel_batch_continuation':True,'_read_batch':{'index':i,**task.slots}}))
    return result


def candidate_transition(previous,candidates,request,event_key):
    version=previous.get('candidate_set_version',0)+1
    scope={k:request[k] for k in ('targets','discovery') if k in request}
    return {'candidate_set_id':event_operation_key(event_key,'hotel-candidates',{'scope':scope,'ids':[p.get('id') for p in candidates],'version':version}),
        'candidate_set_version':version,'candidate_request_fingerprint':event_operation_key('','hotel-search',scope),
        'selection_status':'awaiting' if candidates else 'empty','selected_candidate_ids':[]}


def selection_number(text):
    value=text.strip().rstrip('。!！')
    return number(value) if re.fullmatch(NUMBER,value) else None


def date_proposal(value,*,source_version=None,trip_id=''):
    if not value or not all(value.get(k) for k in ('check_in','check_out')):return {}
    proposal=deepcopy(value)
    proposal.setdefault('version',1);proposal.setdefault('source_kind','itinerary' if trip_id else 'prior_query')
    proposal.setdefault('source_version',source_version);proposal.setdefault('status','proposed')
    proposal.setdefault('presented',True)  # Legacy task proposals were already displayed by the old question flow.
    if trip_id:proposal['trip_id']=trip_id
    proposal.setdefault('proposal_id',event_operation_key('','hotel-stay-proposal',proposal))
    return proposal


def short_proposal_reply(text):
    """Local shortcut for a bound question; general language uses proposal_response."""
    first=re.split(r'[，,。；;\n]',text.strip(),maxsplit=1)[0]
    if first in {'不是','不','否','不同意','拒绝'}:return 'reject'
    if first in {'是','对','没错','可以','好','好的','同意'} and not re.search(r'不(?:是|接受|同意|按|用)|日期.{0,3}不|保存|预订|付款',text):return 'accept'
    return None


def stay_text(stay):
    labels={**LABELS,'children_ages':'儿童情况','room_requirement':'房型','breakfast_requirement':'早餐','cancellation_requirement':'退改'}
    rows=[]
    for key,value in stay.items():
        if key not in labels:continue
        if key=='children_ages':value='未知' if value is None else '无儿童' if value==[] else str(len(value))+'名儿童（'+'、'.join(str(n)+'岁' for n in value)+'）'
        elif value is None:value='未知'
        rows.append(labels[key]+'：'+str(value))
    return '；'.join(rows)


def history_field_matches(key,observed,expected):
    if key in {'check_in','check_out'} and isinstance(observed,str) and isinstance(expected,str):
        actual=date.fromisoformat(observed)
        day=re.fullmatch('('+NUMBER+r')[日号]',expected)
        month_day=re.fullmatch('('+NUMBER+r')月('+NUMBER+r')[日号]?',expected)
        if day:return actual.day==number(day[1])
        if month_day:return (actual.month,actual.day)==(number(month_day[1]),number(month_day[2]))
    return observed==expected
