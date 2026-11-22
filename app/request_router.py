"""Resolve validated Intent IR into existing deterministic domain services."""
from tools.trip.tool import (PlanningArtifacts, trip_context as build_trip_context, planning_request,
                             trip_request, trip_continuation, reference_date_answer, TRIP_COMMANDS, TRIP_ID)
from tools.research.tool import research_request, research_continuation
from tools.reminders.tool import (booking_continuation, scheduled_query_continuation, watch_continuation)
from dataclasses import dataclass, replace
from contextvars import ContextVar
import os
from datetime import datetime, timedelta, timezone
import json
import logging
import re
import hashlib

from agents.intent_compiler import IntentCompiler,IntentCompilationError
from core.tasks import TaskUpdate, TaskRecord, READ_TASK_TYPES, ROUTE_TASK_TYPES
from core.hotel_contract import hotel_task_views, selection_number, short_proposal_reply, date_proposal
from core.hotel_contract import merge_hotel_state,NEW_RUN_COMMAND,RUN_LIMIT_CODES
from tools.hotel_prices.quotes import candidate_choices
from core.execution_outcome import TaskContext, TravelReadRequest, TravelDecision, CONVERSATION_POLICY
from core.intent_ir import IntentIR, IntentOperation, to_jsonable, READ_OPERATION_FIELDS, COMMAND_SCHEMA_VERSION, parse_intent_ir, TRIP_READ_OPERATIONS
from core.commands import (parse_command, location_answer, route_correction, is_weather_clarification,
                           missing_weather_location, command_location as location, unsafe_command_tail, Command)
from tools.registry import TOOL_DEFINITIONS
from tools.preferences import PreferencesTool, preference_request, COMMANDS as PREFERENCE_COMMANDS
from core.time_input import is_time_answer, parse_reminder_time, RAIL_DEPARTURE_PERIODS, rail_departure_period
from app.conversation_context import reminder_context

logger = logging.getLogger(__name__)
TASK_COMMANDS = {'查看任务','查看任务进度','我的任务','取消当前任务','退出当前任务'}
REMINDER_VIEW_COMMANDS = {'查看我的提醒','我的提醒','查看提醒','查看定时查询','查看规则监测'}

def route_correction_candidates(tasks):
    return [task for task in tasks if task.status in {'collecting','completed','failed'}
            and (task.task_type in ROUTE_TASK_TYPES|{'rail'} or set(task.slots.get('intents',[]))&ROUTE_TASK_TYPES)
            and task.slots.get('origin') and task.slots.get('destination')]


def weather_continuation(content: str, tasks: tuple[TaskRecord, ...]):
    candidates = [task for task in tasks
                  if task.task_type in {"weather", "forecast"}
                  and task.status == "collecting" and task.missing_slots == ("location",)]
    location = location_answer(content)
    if len(candidates) == 1 and location:
        task = candidates[0]
        return task, f"{task.initial_request}；查询地点：{location}"
    # A completed query may supply location for a short forecast follow-up.
    if re.fullmatch(r"(?:那|那么)?(?:明天|后天)(?:呢|的天气呢)?[？?。 ]*", content.strip()):
        candidates = [task for task in tasks if task.task_type in {"weather", "forecast"}
                      and task.status == "completed" and task.slots.get("location")]
        if len(candidates) == 1 or (candidates and len({task.slots['location'] for task in candidates}) == 1):
            task = candidates[0]
            return task, f"{task.slots['location']} {content} 天气预报"
    return None, content


def read_query_message(task):
    if task.task_type=='hotel':
        from core.context_contract import hotel_request_projection
        return '继续酒店查询，已绑定条件：'+json.dumps(hotel_request_projection(task.slots.get('hotel_request',{})),ensure_ascii=False)
    if task.task_type=='rail':
        return '继续铁路查询，已绑定字段：'+json.dumps({k:v for k,v in task.slots.items()
            if k in READ_OPERATION_FIELDS['rail'] or k=='queries'},ensure_ascii=False)
    if task.task_type not in ROUTE_TASK_TYPES and not set(task.slots.get('intents',[]))&ROUTE_TASK_TYPES:
        return task.initial_request
    labels={'origin':'起点','destination':'终点','city':'城市'}
    values='；'.join(label+'：'+task.slots[field] for field,label in labels.items() if task.slots.get(field))
    return f'继续{task.task_type}查询，以当前地点为准：{values}'


def rail_time_followup(content):
    text=re.sub(r'^(?:改成|改为|换成|那就|那么|那)','',content.strip())
    for name in sorted(RAIL_DEPARTURE_PERIODS,key=len,reverse=True):
        position=text.find(name)
        if position>=0 and rail_departure_period(text[position:]) is not None:
            prefix=text[:position].strip('，,、 ')
            if not prefix or is_time_answer(prefix):
                return True
    return is_time_answer(text) or bool(re.fullmatch(
        r'(?:今天|明天|后天|大后天)?[，,、\s]*(?:早一点|晚一点)(?:出发)?[。！!？?]*',text))


def read_task_continuation(content, tasks):
    # These cues only bind context. The semantic contract still decides whether to execute.
    hotel_followup=bool(re.search(r'入住|退房|房价|报价|第.+家|下一批|原文|\d+间|[一二两三四五六七八九十]+间|成人|儿童|孩子|\d+岁',content)
        or is_time_answer(content) or re.fullmatch(r'(?:是|对|可以)[，,].{1,100}',content))
    hotel_tasks=[task for task in tasks if task.task_type=='hotel' and task.status in {'collecting','completed','failed'}]
    for task in tasks:
        if task.task_type!='read_batch' or task.status!='collecting':continue
        hotel_tasks.extend(hotel_task_views(task))
    named=[]
    for task in hotel_tasks:
        if task.status!='collecting' or 'targets' not in task.missing_slots:continue
        state=merge_hotel_state(task.slots,{})
        if any(candidate_choices(state,content,[{'name':p['name'],'city':p.get('cityname','')}]) for p in state.get('hotel_candidates',[])):
            named.append(task)
    if len(named)==1:return named[0],read_query_message(named[0])+'\n用户本轮选择：'+content
    if len(named)>1:return None,content
    local=[t for t in hotel_tasks if t.status=='collecting' and (
        selection_number(content) is not None and t.slots.get('hotel_candidates') and 'targets' in t.missing_slots or
        short_proposal_reply(content) is not None and date_proposal(t.slots.get('hotel_date_proposal')).get('status')=='proposed')]
    if len(local)==1 and not any(t.status=='collecting' and t.task_id!=local[0].task_id for t in tasks):
        return local[0],read_query_message(local[0])+'\n用户本轮补充：'+content
    if hotel_tasks and is_time_answer(content) and any(t.task_type=='rail' and t.status=='collecting' and 'travel_date' in t.missing_slots for t in tasks):
        return None,content
    if hotel_followup and len(hotel_tasks)==1 and not re.search(r'天气|车票|高铁|确认保存',content):
        task=hotel_tasks[0]
        return task,read_query_message(task)+'\n用户本轮补充：'+content
    changes=route_correction(content)
    if changes:
        candidates=route_correction_candidates(tasks)
        if len(candidates)>1 and any(task.task_type=='rail' for task in candidates):
            return None,content
        pairs={(re.sub(r'[\s的]','',task.slots['origin']),re.sub(r'[\s的]','',task.slots['destination'])) for task in candidates}
        if candidates and len(pairs)==1:
            task=candidates[0]
            slots={**task.slots,**changes}
            if task.task_type in ROUTE_TASK_TYPES:
                slots.pop('city',None)
            if task.task_type=='rail':
                for field in changes:
                    slots.pop(field+'_scope',None)
            amended=replace(task,slots=slots)
            values='；'.join(('起点' if field=='origin' else '终点')+'：'+value for field,value in changes.items())
            return amended,read_query_message(amended)+'\n用户更正'+values
        return None,content
    rail=[task for task in tasks if task.task_type=='rail' and task.status in {'collecting','completed','failed'}]
    if len(rail)==1 and (rail_time_followup(content) or rail[0].status=='collecting'
                        and any(k in {'travel_date','departure_window'} for k in rail[0].missing_slots)
                        and len(content)<80 and (parse_reminder_time(content,datetime.now(timezone.utc)).day
                            or re.search(r'上午|下午|晚上|凌晨|早上|傍晚|晚一点|早一点',content))):
        return rail[0],read_query_message(rail[0])+'\n用户补充或更正：'+content
    task, resolved = weather_continuation(content, tasks)
    if task:
        return task, resolved
    waiting = [task for task in tasks if task.task_type in READ_TASK_TYPES
               and task.status == 'collecting' and task.missing_slots]
    repeated=[task for task in waiting if content.strip().rstrip('。！？?!')==task.initial_request.strip().rstrip('。！？?!')]
    if len(repeated)==1:
        return repeated[0],read_query_message(repeated[0])+'\n用户本轮输入：'+content
    if len(waiting) != 1:
        return None, content
    task = waiting[0]
    location_fields = {'location', 'city', 'origin', 'destination'}
    compatible = bool(location_answer(content)) if set(task.missing_slots) <= location_fields else False
    if set(task.missing_slots) & {'origin', 'destination'} and len(content) < 120:
        compatible = compatible or bool(re.fullmatch(r'[^？?\n]+(?:到|至|->|→)[^？?\n]+', content))
    if compatible:
        return task, f'{read_query_message(task)}\n用户本轮输入：{content}'
    return None, content


def bind_read_context(event, tasks, *, task=None):
    task,message=(read_task_continuation(event.content,tasks) if task is None else
                  (task,read_query_message(task)+'\n用户本轮输入：'+event.content))
    timestamp = (event.occurred_at or datetime.now(timezone.utc)).isoformat()
    reference = task.slots.get('reference_time', timestamp) if task else timestamp
    if task and re.search(r'今天|明天|后天', event.content):
        reference = timestamp
    return TaskContext(task, message, reference, dict(task.slots) if task else {}, event.content)


def read_task_compatible(task,ir,text):
    """A shared domain is insufficient evidence to continue a particular request."""
    if len(ir.operations)!=1:
        return False
    operation=ir.operations[0]
    if operation.domain=='hotel' and task.task_type=='hotel':
        proposed=operation.values.get('targets',[]);known=task.slots.get('hotel_request',{}).get('targets',[])
        if task.status=='collecting' and 'targets' in task.missing_slots and candidate_choices(merge_hotel_state(task.slots,{}),text,proposed):return True
        if proposed and known:return proposed==known
        return bool(operation.values.get('stay') or operation.values.get('reference') or not proposed)
    previous=set(task.slots.get('intents',[task.task_type])); proposed={operation.domain}
    if previous!=proposed and not (previous|proposed)<={'weather','forecast'}:
        return False
    fields=READ_OPERATION_FIELDS.get(operation.domain,set())
    identity=fields&{'origin','destination'} or fields&{'location'} or fields&{'city','keywords'}
    provided={key:value for key,value in operation.values.items() if isinstance(value,str) and value and value in text}
    # Changing weather/forecast before any location is known carries no old place identity.
    if identity=={'location'} and 'location' in task.missing_slots and not task.slots.get('location') and not provided.get('location'):
        return True
    changed={key for key in identity if key in provided and task.slots.get(key)
             and provided[key].strip()!=task.slots[key].strip()}
    if changed-set(task.missing_slots):
        return False
    return bool(set(provided)&set(task.missing_slots) or any(
        key in provided and task.slots.get(key) and provided[key].strip()==task.slots[key].strip() for key in identity))

def resume_weather_request(content: str, recent_dialogue) -> str:
    # Only a short location answer to the latest, unexpired clarification is resumed.
    if not recent_dialogue or not re.fullmatch(r"[\u4e00-\u9fffA-Za-z· ]{1,30}", content):
        return content
    turn = recent_dialogue[-1]
    if not is_weather_clarification(turn.assistant_content):
        return content
    try:
        created = datetime.fromisoformat(turn.created_at)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - created > timedelta(minutes=30):
            return content
    except ValueError:
        return content
    if missing_weather_location(turn.user_content) and location_answer(content):
        return f"{turn.user_content}；查询地点：{content}"
    return content


def response_candidate(text):
    """Eligibility for an unmentioned group message; conveys no tool permissions."""
    text=text.strip()
    if text in TASK_COMMANDS | REMINDER_VIEW_COMMANDS | TRIP_COMMANDS | PREFERENCE_COMMANDS:
        return True
    if parse_command(text).name!='unknown':
        return True
    if re.fullmatch(r'(?:今天|明天)?(?:早饭|午饭|晚饭|早餐|午餐|晚餐)吃什么(?:呀|啊|呢)?[？?。 ]*',text):
        return False
    request=re.search(r'帮我|帮忙|请|查|查看|刷新|确认|取消|修改|补充|新增|制定|列出|根据|按这张|'
                      r'推荐|找一家|找一下|安排|规划|改成|改为|改到|放到|挪到|不去|记住|忘记|删除|'
                      r'暂停|启用|关闭|开启|提醒我|不用提醒|不必提醒|通知我|叫我|监测|怎么|如何|多少|什么|哪里|哪[个天里家]|'
                      r'以后.{0,12}(?:默认|优先)|我一般.{0,12}(?:喜欢|偏爱)|[？?]|吗\s*$',text)
    subject=re.search(r'天气|气温|路况|路线|地铁|公交|步行|驾车|自驾|旅行|旅游|行程|旅程|方案|'
                      r'预约|提醒|放票|开约|定时|任务|偏好|研究|攻略|资料|文档|文件|图片|截图|'
                      r'酒店|餐厅|餐馆|博物馆|景点|吃什么|第[一二两三四五六七八九十\d]+天|'
                      r'R-\d{8}-\d{3}|A-\d{6}|\.xlsx|\.docx|https://',text,re.I)
    return bool(request and subject)


def continuation_candidate(text,tasks):
    waiting={task.task_type for task in tasks if task.status=='collecting'}
    if 'reminder' in waiting:
        return bool(text.strip()) and len(text)<=8000
    if (any(task.task_type=='reminder' for task in tasks) and re.search(r'刚才|上一条|那条',text)
            and re.search(r'改|取消|不用提醒|不必提醒|删掉',text)):
        return True
    if read_task_continuation(text,tasks)[0] is not None:
        return True
    if 'semantic' in waiting:
        return bool(text.strip()) and len(text.strip())<=200
    selectors={'booking_reminder':booking_continuation,'booking_policy_query':booking_continuation,
               'itinerary':trip_continuation,'scheduled_query':scheduled_query_continuation,
               'policy_watch':watch_continuation,'research':research_continuation}
    return any(selectors[kind](text,tasks) is not None for kind in waiting & selectors.keys())


def trip_command(text):
    """Bounded command forms; arbitrary mentions of planning are not commands."""
    text=text.strip()
    if len(set(re.findall(TRIP_ID,text)))>1:
        return None
    if re.sub(TRIP_ID,'',text).strip() in TRIP_COMMANDS:
        return Command('trip_edit')
    if not trip_request(text):
        return None
    if re.fullmatch(r'(?:根据|按照|按)(?:这张|这份|我上传的|我刚上传的|上传的|我的)?(?:图片|文档|资料|行程单)[，, ]*(?:帮我|请)?(?:规划|制定|设计|安排)[^；;\n]{0,400}',text):
        return Command('trip_create')
    if re.fullmatch(r'(?:帮我|请)?(?:规划|制定|设计|安排)[^；;\n]{1,500}',text):
        return Command('trip_create')
    if (re.fullmatch(r'第[一二两三四五六七八九十\d]+天[^；;\n]+',text)
        or re.fullmatch(r'[^；;\n]+(?:放到|挪到|放|挪)第[一二两三四五六七八九十\d]+天',text)
        or re.fullmatch(r'(?:把)?(?:行程|旅程).{0,15}(?:改到|改成|改为|调整)[^；;\n]+',text)):
        return Command('trip_edit')
    return None


def reminder_command(selection,text):
    if selection.task is not None or selection.reply or selection.kind=='watch':
        return True
    if selection.kind=='booking' and selection.operation=='query':
        return True
    if selection.kind=='personal' and re.fullmatch(r'(?:请)?(?:提醒我|叫我|通知我)[。！! ]*',text):
        return True
    if selection.operation in {'list','cancel','update'}:
        return bool(re.match(r'^(?:请|帮我)?(?:查看|看看|列出|我的|有哪些|取消|修改|设置)',text)
            or re.match(r'^(?:把)?(?:刚才|上一条)',text)
            or re.fullmatch(r'[^，,；;\n]+(?:不用|不必)提醒了?[。 ]*',text))
    prefix=re.split(r'提醒我|叫我|通知我|告诉我|发给我',text,maxsplit=1)[0]
    prefix=re.sub(r'^(?:请|帮我)?(?:我想|我要)?(?:在)?','',prefix,count=1)
    return bool(re.match(r'^(?:\d|[一二两三四五六七八九十]+|明天|后天|今天|今晚|大后天|周[一二三四五六日天]|下周)',prefix))


VERSION = COMMAND_SCHEMA_VERSION


def enabled():
    return os.getenv('AI_RULE_ROUTING', 'true').lower() == 'true'


def compound_boundary(text):
    # Punctuation in narrative memory is not an instruction to execute several tasks.
    if not re.match(r'^(?:请|帮我)?(?:查询|查一下|查天气|查路线|搜索|规划|取消|删除|查看|修改行程|酒店预算(?:改为|改成)|不要夜游|不安排夜游)',text.strip()):
        return False
    return bool(re.search(r'[；;\n]|(?:[，,]\s*|同时|顺便|并且|还有)(?:请|再|帮我|顺便|同时)?(?:查|提醒|取消|删除|搜索|推荐|规划|把)',text))


@dataclass(frozen=True)
class RuleResult:
    status: str
    ir: IntentIR | None = None
    reason: str = 'no_match'
    rule_ids: tuple[str, ...] = ()




def parse_rules(text):
    if len(text) > 1000 or re.search(r'[“”"\'（）()\[\]：:]|https?://', text):
        return RuleResult('fallback', reason='unsupported_structure')
    parts = [part.strip() for part in re.split(r'[；;。\n]', text.strip()) if part.strip()]
    if not 1 <= len(parts) <= 3:
        return RuleResult('fallback', reason='operation_limit')
    operations, spans, rules, changes = [], [], [], {}
    for part in parts:
        match = re.fullmatch(r'(?:请)?查询天气\s*(.+?)|(.+?)现在天气怎么样[？?]?', part)
        if match and location((match[1] or match[2]).strip()):
            value = (match[1] or match[2]).strip()
            operations.append(IntentOperation('weather', 'query', {'location':value}))
            spans.append(part); rules.append('current_weather'); continue
        match = re.fullmatch(r'(?:请)?查询(?:驾车)?路线\s*(.+?)\s*(?:→|->)\s*(.+)', part)
        if not match:
            match = re.fullmatch(r'从(.+?)到(.+?)开车怎么走[？?]?',part)
        if match and all(location(value.strip()) for value in match.groups()):
            operations.append(IntentOperation('route','query',{'origin':match[1].strip(),'destination':match[2].strip()}))
            spans.append(part); rules.append('driving_route'); continue
        match = re.fullmatch(r'酒店预算(?:改为|改成)([1-9][0-9]{0,5}(?:-[1-9][0-9]{0,5})?)(元/间/晚|人民币每间每晚)', part)
        if match and 'budget' not in changes:
            amounts=[int(v) for v in match[1].split('-')]
            if len(amounts)==2 and amounts[0]>amounts[1]:
                return RuleResult('fallback',reason='invalid_budget_range')
            changes['budget'] = {'value':match[1],'scope':'accommodation','unit_text':match[2],'source':part}
            spans.append(part); rules.append('hotel_budget'); continue
        if part in {'不要夜游','不安排夜游'} and 'night' not in changes:
            changes['night'] = {'value':False,'source':part}
            spans.append(part); rules.append('no_night'); continue
        return RuleResult('fallback', reason='uncovered_text', rule_ids=tuple(rules))
    if changes and operations:
        return RuleResult('fallback', reason='mixed_read_write', rule_ids=tuple(rules))
    if changes:
        operations = [IntentOperation('trip','update_constraints',{'target_ref':'current_trip','changes':changes})]
    ir = IntentIR(1,'update' if changes else 'read',tuple(operations),requires_confirmation=bool(changes),source_spans=tuple(spans))
    return RuleResult('complete',parse_intent_ir(to_jsonable(ir),text),'complete',tuple(rules))




RULE_VERSION = VERSION
rules_enabled = enabled



def semantic_candidate(text: str) -> bool:
    """Only explicit commands bypass the compiler; natural language has no keyword gate."""
    stripped = text.strip()
    if stripped in TASK_COMMANDS | REMINDER_VIEW_COMMANDS | TRIP_COMMANDS | PREFERENCE_COMMANDS:
        return False
    if unsafe_command_tail(stripped):
        return True
    from core.commands import social_message
    if social_message(stripped):
        return False
    if stripped == '重新生成行程预览' or re.fullmatch(r'重新生成行程预览\s+TR-[a-f0-9]{12}',stripped):
        return False
    if stripped in {'我的偏好','查看我的偏好','查看偏好','保存偏好建议','忘记所有偏好','暂停应用偏好','启用偏好','关闭偏好建议','开启偏好建议'}:
        return False
    if stripped in {'查看方案草稿','我的方案草稿'} or re.fullmatch(r'删除方案草稿\s+P-[a-f0-9]{12}',stripped):
        return False
    if stripped in {'帮助', '菜单', '状态', '查看行程', '查看我的行程', '我的行程', '确认行程修改', '确认行程提醒', '查看我的提醒', '查看任务进度', '查看任务', '查看定时查询', '查看规则监测'}:
        return False
    if re.fullmatch(r'(?:取消提醒|修改提醒)\s+M-[a-f0-9]{12}', stripped):
        return False
    return bool(stripped) and parse_command(stripped).name == 'unknown'



@dataclass
class RequestBinding:
    event: object
    tasks: tuple
    read_context: TaskContext
    budget_run_key: str
    route_owner: str = ""
    decision_source: str = ""

    @property
    def pending_semantic(self):
        waiting = [t for t in self.tasks if t.task_type=='semantic' and t.status=='collecting']
        return waiting[0] if len(waiting)==1 else None

    @property
    def pending_reminders(self):
        return tuple(t for t in self.tasks if t.task_type=='reminder' and t.status=='collecting')


CURRENT_REQUEST = ContextVar('travel_request_binding', default=None)


def bind_request(store,event):
    current = CURRENT_REQUEST.get()
    if current is not None and current.event == event:
        return current
    if event.content.strip()==NEW_RUN_COMMAND:
        tasks=tuple(store.tasks.recent(event.platform,event.storage_scope_id,event.sender_id,reply_to_id=event.reply_to_id))
        if not any(t.status=='collecting' for t in tasks):
            tasks=(*tasks,*store.tasks.recoverable_hotels(event.platform,event.storage_scope_id,event.sender_id))
        eligible=[view for t in tasks for view in hotel_task_views(t) if view.status=='collecting'
            and view.slots.get('model_budget_block',{}).get('code') in RUN_LIMIT_CODES]
        task=eligible[0] if len(eligible)==1 and not any(t.status=='collecting' and t.task_id!=eligible[0].task_id for t in tasks) else None
        key=hashlib.sha256(event.event_key.encode()).hexdigest()
        if task:
            parent={'run_key':task.slots.get('_read_batch',{}).get('budget_run_key') or task.slots.get('budget_run_key'),
                'task_id':task.task_id,'source_event_id':task.last_event_id}
            slots={**task.slots,'budget_run_key':key,'model_budget_block':{},'budget_run_parent':parent,'budget_run_started_by':event.event_key}
            if '_read_batch' in slots:slots['_read_batch']={**slots['_read_batch'],'budget_run_key':key,'budget_run_parent':parent,'budget_run_started_by':event.event_key}
            if task.is_expired(event.occurred_at or datetime.now(timezone.utc)):
                task=replace(task,task_id='',version=0);slots['hotel_expired']=True
            task=replace(task,slots=slots)
        return RequestBinding(event,tasks,bind_read_context(event,(),task=task) if task else bind_read_context(event,()),key)
    if event.hotel_action:
        task=store.tasks.owned(event,event.hotel_action['task_id'])
        view=next((v for v in hotel_task_views(task) if v.slots.get('hotel_query_index')==event.hotel_action.get('query_index')),None) if task else None
        context=bind_read_context(event,(),task=view) if view else bind_read_context(event,())
        return RequestBinding(event,(task,) if task else (),context,
            task.slots.get('budget_run_key') or hashlib.sha256(event.event_key.encode()).hexdigest() if task else hashlib.sha256(event.event_key.encode()).hexdigest())
    if event.confirmation:
        tasks=tuple(t for t in store.tasks.recent(event.platform,event.storage_scope_id,event.sender_id)
            if t.task_id==event.confirmation['id'] and t.version==event.confirmation['version'])
        return RequestBinding(event,tasks,bind_read_context(event,()),hashlib.sha256(event.event_key.encode()).hexdigest())
    if event.platform=='web' and (event.message_reference or event.reply_to_id):
        snapshot=(event.message_reference or {}).get('rail') if event.message_reference else store.tasks.rail_references(
            event.platform,event.storage_scope_id,event.sender_id,[event.reply_to_id]).get(event.reply_to_id)
        timestamp=(event.occurred_at or datetime.now(timezone.utc)).isoformat()
        slots={}
        message=event.content
        if snapshot:
            queries=snapshot['queries']
            slots={**(queries[0] if len(queries)==1 else {'queries':queries}),
                   'intents':['rail'],'reference_event_id':snapshot['reference_event_id'],
                   'missing_fields':snapshot['missing_fields']}
            message='继续铁路查询，引用的历史条件：'+json.dumps(queries,ensure_ascii=False)+'\n用户补充或更正：'+event.content
        # A quote selects its own active clarification, never an unrelated recent task.
        tasks=tuple(task for task in store.tasks.recent(event.platform,event.storage_scope_id,event.sender_id,
            reply_to_id=event.reply_to_id) if task.reference_message_id==event.reply_to_id
            and task.status=='collecting') if event.reply_to_id and hasattr(store,'tasks') else ()
        context=(bind_read_context(event,(),task=tasks[0]) if not snapshot and len(tasks)==1
                 and tasks[0].task_type in READ_TASK_TYPES else TaskContext(None,message,timestamp,slots,event.content))
        run_key=tasks[0].slots.get('budget_run_key','') if len(tasks)==1 else ''
        return RequestBinding(event,tasks,context,run_key or hashlib.sha256(event.event_key.encode()).hexdigest())
    correction = route_correction(event.content)
    tasks = tuple(store.tasks.recent(event.platform,event.storage_scope_id,event.sender_id,
        reply_to_id=event.reply_to_id,**({'include_failed_reads':True} if correction else {}))) if hasattr(store,'tasks') else ()
    context = bind_read_context(event,tasks)
    if event.content.strip() in {'继续','继续核价'}:
        blocked=[view for t in tasks for view in hotel_task_views(t) if view.status=='collecting'
            and (view.slots.get('model_budget_block') or view.slots.get('budget_run_started_by'))]
        if len(blocked)==1 and not any(t.status=='collecting' and t.task_id!=blocked[0].task_id for t in tasks):
            context=bind_read_context(event,(),task=blocked[0])
    if context.task is None and not any(t.task_type=='hotel' for t in tasks) and hasattr(getattr(store,'tasks',None),'recoverable_hotels'):
        interrupted=store.tasks.recoverable_hotels(event.platform,event.storage_scope_id,event.sender_id)
        selected_task,_=read_task_continuation(event.content,interrupted)
        if selected_task:
            expired=selected_task.is_expired(event.occurred_at or datetime.now(timezone.utc))
            recovered=replace(selected_task,task_id='' if expired else selected_task.task_id,version=0 if expired else selected_task.version,
                slots={**selected_task.slots,'hotel_expired':expired,'hotel_previous_status':selected_task.status})
            context=bind_read_context(event,(),task=recovered)
    if len(tasks)==1 and event.reply_to_id and tasks[0].reference_message_id==event.reply_to_id:
        context=bind_read_context(event,(),task=tasks[0])
    selected = context.task
    if selected is None and event.reply_to_id and len(tasks)==1:
        selected = tasks[0]
    waiting = [task for task in tasks if task.status=='collecting']
    if selected is None and len(waiting)==1:
        candidate = waiting[0]
        missing, text = set(candidate.missing_slots), event.content.strip()
        if (is_time_answer(text) or (missing & {'destination','city'} and location_answer(text))
                or (missing & {'duration','day_count','duration_text','selected_days'}
                    and re.fullmatch(r'[一二两三四五六七八九十\d]{1,3}[天日](?:游)?',text))
                or ('budget_unit' in missing and re.fullmatch(
                    r'(?:每人|总额|每间(?:房)?|每天|每晚|全程|总预算)(?:[，,、 ]*(?:每人|总额|每间(?:房)?|每天|每晚|全程|总预算))*',text))):
            selected = candidate
    run_key = (selected.slots.get('budget_run_key') or selected.slots.get('_read_batch',{}).get('budget_run_key')) if selected else None
    if selected and selected.task_type=='hotel' and selected.status=='completed':
        key=hashlib.sha256(event.event_key.encode()).hexdigest()
        context=replace(context,slots={**context.slots,'budget_run_key':key,
            'budget_run_parent':{'run_key':run_key,'task_id':selected.task_id,'source_event_id':selected.last_event_id},'budget_run_started_by':event.event_key})
        run_key=key
    run_key = run_key or hashlib.sha256(event.event_key.encode()).hexdigest()
    return RequestBinding(event,tasks,context,run_key)


class RequestRouter:
    def __init__(self, store, compiler: IntentCompiler, trip_service=None,
                 reminder_service=None, research_service=None, travel_service=None,
                 mode='execute', clock=None, tool_executor=None, read_only_service=None, preferences_tool=None, planning_tool=None, reservation_tool=None):
        self.store, self.compiler = store, compiler
        self.trip_service, self.reminder_service = trip_service, reminder_service
        self.research_service = research_service
        from tools.travel_query import TravelQueryTool
        self.read_only_service = read_only_service or TravelQueryTool(store=store, backend=travel_service, tool_executor=tool_executor)
        self.mode = mode if mode in {'shadow', 'preview', 'execute'} else 'shadow'
        self.clock = clock
        self.preferences_tool = preferences_tool
        self.planning_tool = planning_tool
        self.reservation_tool = reservation_tool
        self.tool_executor = tool_executor

    def _dispatch(self, owner, function, *args, **kwargs):
        binding = CURRENT_REQUEST.get()
        if binding is not None:
            binding.route_owner = owner
        return function(*args, **kwargs)

    def _booking_policy(self,event,ir):
        if len(ir.operations)!=1 or not ir.source_spans:
            return '请分别说明每项预约操作，本次未修改预约。'
        op=ir.operations[0]
        definition=TOOL_DEFINITIONS.get(op.operation)
        if not definition or definition.owner!='reservation' or op.operation=='create_reservation_draft_from_image':
            return '请使用明确的预约操作；创建图片草稿请发送“制定预约”和当前图片。'
        values=op.values
        if values.keys()-{'reference','authorization'}:
            return '预约操作包含未支持字段，本次未修改。'
        authorization,reference=values.get('authorization',''),values.get('reference','')
        if authorization!=event.content or not isinstance(reference,str):
            return '预约操作需要对应本轮完整请求，未执行。'
        if definition.resource_field:
            pattern=r'R-\d{8}-\d{3}' if definition.resource_field=='plan_code' else r'A-\d{6}'
            if not re.fullmatch(pattern,reference) or reference not in event.content:
                return '请明确本次操作的计划或项目编号，未执行。'
        elif reference:
            return '此操作不接受资源编号。'
        expected_actions = ({'read'} if definition.mode=='read' else
            {'confirm'} if op.operation=='confirm_reservation_plan' else
            {'cancel','delete'} if op.operation.startswith('cancel_') else {'update','create'})
        if ir.action not in expected_actions or ir.missing_fields or ir.requires_confirmation or self.mode!='execute':
            return '已识别预约操作，但动作或确认信息尚不完整，本次未修改。'
        guarded={'confirm_reservation_plan':'reservation_confirm','cancel_reservation_plan':'reservation_cancel_plan',
                 'cancel_reservation_item':'reservation_cancel_item','modify_reservation_item_date':'reservation_modify_date',
                 'modify_reservation_item_times':'reservation_modify_times'}
        if op.operation in guarded:
            command=parse_command(re.sub(r'^(?:请|帮我)','',event.content.strip()))
            if command.name!=guarded[op.operation] or command.error or command.args[0]!=reference:
                return '确认、取消或修改已保存预约需要明确命令和编号，本次未修改。'
        elif definition.mode=='write' and not re.match(r'^(?:请|帮我)?(?:把|将|修改|更新|补充|新增|设置|刷新)',event.content.strip()):
            return '预约草稿缺少明确修改要求，本次未修改。'
        tools=(op.operation,) if op.operation=='update_reservation_draft_items' else tuple(dict.fromkeys(('list_reservation_plans',op.operation)))
        return TravelDecision('reservation',('reservation',),False,tools,((op.operation,),),False,'normal',
                              ((op.operation,reference),) if reference else ())

    def _task_command(self, event, claim):
        text = event.content.strip()
        if text not in {'查看任务', '查看任务进度', '我的任务', '取消当前任务', '退出当前任务'}:
            return None
        tasks = self.store.tasks.recent(event.platform, event.storage_scope_id, event.sender_id, reply_to_id=event.reply_to_id)
        waiting = [task for task in tasks if task.status in {'collecting', 'waiting_external'}]
        if text in {'查看任务', '查看任务进度', '我的任务'}:
            if not tasks:
                return '当前没有近期任务。已设置的提醒请说“查看我的提醒”。'
            labels = {'weather': '天气查询', 'forecast': '天气预报', 'reminder': '提醒设置', 'booking_reminder': '开约提醒',
                      'booking_policy_query': '预约规则查询', 'places': '地点查询', 'transit': '公共交通查询', 'walking': '步行路线',
                      'route': '驾车路线', 'traffic': '路况查询', 'itinerary': '行程规划', 'scheduled_query': '定时查询', 'policy_watch': '规则监测'}
            states = {'collecting': '等待补充信息', 'waiting_external': '等待外部信息', 'completed': '已完成'}
            return '\n'.join(f"{labels.get(task.task_type, task.task_type)}：{states.get(task.status, task.status)}；{task.initial_request[:120]}" for task in tasks)
        if len(waiting) != 1:
            return '请回复要结束的那条任务消息后发送“取消当前任务”。已创建的提醒请使用“取消提醒”。'
        task = waiting[0]
        update = TaskUpdate(task.task_type, 'cancelled', task.initial_request, task.slots,
                            task_id=task.task_id, expected_version=task.version)
        reply = '已结束当前待补充任务。已经创建的提醒仍保留，需要时请单独取消提醒。'
        self.store.tasks.prepare_result(event, claim, update, reply)
        return reply

    def route(self,event,claim):
        if claim is not None:
            from core.tool_result import DEFAULT_CATALOG
            self.store.delivery.freeze_tool_catalog(event.event_key,DEFAULT_CATALOG)
        binding = bind_request(self.store,event)
        token = CURRENT_REQUEST.set(binding)
        policy = CONVERSATION_POLICY
        try:
            result = self._route_bound(event,claim,binding)
            if isinstance(result,TravelDecision):
                policy, result = result, None
            binding.route_owner = binding.route_owner or ('clarification' if result is not None else 'conversation')
            return result,policy
        finally:
            if binding.route_owner and getattr(self.store,'diagnostics',None):
                failed=binding.route_owner=='intent_failure'
                self.store.diagnostics.record('routing',{'event_key':event.event_key,'status':'intent_failed' if failed else 'selected',
                    'route_owner':binding.route_owner,'decision_source':binding.decision_source or 'semantic',
                    'candidate_count':0 if failed else 1,**({'error_code':binding.decision_source} if failed else {})})
            CURRENT_REQUEST.reset(token)

    def _legacy_read(self,event,binding):
        if (binding.tasks or not semantic_candidate(event.content) or not location_answer(event.content)
                or self.store.tasks.has_history(event.platform,event.storage_scope_id,event.sender_id)):
            return None
        history=self.store.conversations.get_recent_turns(event.storage_scope_id,event.sender_id)
        message=resume_weather_request(event.content,history)
        if message==event.content:
            return None
        created=datetime.fromisoformat(history[-1].created_at)
        if created.tzinfo is None:
            created=created.replace(tzinfo=timezone.utc)
        domain='forecast' if re.search(r'明天|后天|预报|未来',history[-1].user_content) else 'weather'
        context=TaskContext(None,message,created.isoformat(),{},event.content)
        return TravelReadRequest((IntentOperation(domain,'query',{'location':location_answer(event.content)}),),
                                 context,source='legacy_history')

    def _route_bound(self,event,claim,binding):
        text = event.content.strip()
        hotel=getattr(self.read_only_service,'hotel_prices',None)
        if text in {'继续','继续核价'} and binding.read_context.task and binding.read_context.task.slots.get('model_budget_block'):
            return '原模型预算阻断仍在生效，请按受限回复中的原因处理；可先查看已保存资料，普通继续不会重建运行。'
        if text==NEW_RUN_COMMAND:
            task=binding.read_context.task
            if hotel and task and task.slots.get('budget_run_started_by')==event.event_key:
                pending=task.slots.get('budget_pending_request')
                if pending:
                    resumed=replace(event,content=pending['text'],occurred_at=datetime.fromisoformat(pending['occurred_at']))
                    previous=binding.event,binding.read_context
                    binding.event=resumed;binding.read_context=bind_read_context(resumed,(),task=task)
                    try:return self.handle(resumed,claim)
                    finally:binding.event,binding.read_context=previous
                return self._hotel_action(event,claim,task,{'mode':'refresh'})
            return '没有唯一绑定且达到单轮模型上限的酒店查询。请引用对应的受限回复；本次没有建立新运行或提交查询。'
        if hotel and event.hotel_action:
            try:task,values=hotel.validate_action(event,event.hotel_action)
            except ValueError as error:return str(error)
            return self._hotel_action(event,claim,task,values)
        if hotel:
            local=hotel.local_read(event)
            if local is not None:return self._dispatch('hotel_local_read',lambda:local)
            tasks=binding.tasks
            if binding.read_context.task and not any(t.task_id==binding.read_context.task.task_id for t in tasks):
                tasks=(*tasks,binding.read_context.task)
            selection=hotel.local_selection(event,tasks)
            if isinstance(selection,str):return selection
            if selection is not None:return self._hotel_action(event,claim,*selection)
        if event.confirmation:
            if len(binding.tasks)!=1:return '这项确认已过期或修订发生变化，本次未保存。'
            task=binding.tasks[0]
            if task.task_type=='itinerary':
                return self._dispatch('trip',self.trip_service.handle,event,claim,tasks=binding.tasks,command='确认行程修改')
            selection=self.reminder_service.prepare_request(event,tasks=binding.tasks) if self.reminder_service else None
            return self._dispatch('reminders',self.reminder_service.execute,selection,event,claim) if selection else '这项确认当前不可执行。'
        if event.draft_action is not None:
            binding.decision_source='draft_action'
            return self._dispatch('trip',self.trip_service.handle_draft_action,event,claim)
        failed_trip=trip_continuation(text,binding.tasks)
        if (self.trip_service and failed_trip and failed_trip.slots.get('planning_failure')
                and text in {'继续规划','重新生成行程预览'}):
            binding.decision_source='bound_trip'
            return self._dispatch('trip',self.trip_service.handle,event,claim,tasks=(failed_trip,))
        if event.rail_query is not None:
            if event.platform!='web':
                return '此入口不支持车次卡片查询。'
            values=event.rail_query['request']
            ir=IntentIR(1,'read',(IntentOperation('rail','query',values),),source_spans=(event.content,))
            context=TaskContext(None,event.content,(event.occurred_at or datetime.now(timezone.utc)).isoformat(),values,event.content)
            return self._dispatch('travel_query',self.read_only_service.prepare,event,claim,ir,context,source='rail_card',mode=self.mode)
        if event.platform=='web' and event.reply_to_id and not event.message_reference and not binding.read_context.slots.get('reference_event_id'):
            return '这条消息的引用已不可用，请重新选择引用或说明当前目标。'
        if event.trip_edit is not None:
            reply = self._dispatch('trip',self.handle,event,claim)
            return reply if reply is not None else '当前处于观察模式，本次未生成草稿或修改行程。'
        legacy = self._legacy_read(event,binding)
        if legacy is not None:
            binding.decision_source='legacy_history'
            return self._dispatch('travel_query',self.read_only_service.execute,event,claim,legacy)
        complete = not compound_boundary(text)
        planning = self.planning_tool or PlanningArtifacts(self.store.planning)
        if complete and planning_request(text):
            binding.decision_source = 'command'
            return self._dispatch('planning_artifact',planning.handle,event,claim)
        research_command = (text in {'查看研究结果','查看研究进度'}
            or re.fullmatch(r'(?:帮我|请)?(?:搜索|搜一下|搜一搜|联网搜索|读取公开页面|重新整理|润色)[^；;\n]{1,800}',text)
            or re.fullmatch(r'根据研究结果(?:帮我)?(?:规划|制定|安排)[^；;\n]+',text)
            or re.fullmatch(r'查看[^；;\n]{0,30}(?:研究原文|资料原文|景点依据|来源依据)',text))
        if complete and self.research_service and not re.search(r'官网|官方网站',text) and (research_command or research_continuation(text,binding.tasks)):
            binding.decision_source = 'research_request'
            combined = self.trip_service is not None and trip_request(text) and bool(re.search(r'规划|制定|安排',text))
            answer = self._dispatch('research_plan' if combined else 'research',self.research_service.handle,
                                    event,claim,force=True,tasks=binding.tasks)
            return self._finish_research_plan(event,claim,answer) if combined else answer
        if text in TASK_COMMANDS:
            binding.decision_source = 'command'
            return self._dispatch('tasks',self._task_command,event,claim)
        command = parse_command(text)
        command_ir = self.read_only_service.command_ir(text,command)
        if command_ir is not None:
            binding.decision_source = 'command'
            return self._dispatch('travel_query',self.read_only_service.prepare,event,claim,command_ir,
                                  binding.read_context,source='command')
        if command.name.startswith('reservation_') and self.reservation_tool is not None:
            binding.decision_source = 'command'
            return self._dispatch('reservation',self.reservation_tool.handle_command,command,event)
        if self.compiler is not None:
            result = self.handle(event,claim)
            if result is not None:
                return result
        if not complete:
            return '这组请求尚未完整识别，本次未执行。请分别说明各项需求。'
        preferences = self.preferences_tool or PreferencesTool(self.store.preferences)
        if preference_request(text):
            binding.decision_source = 'command'
            return self._dispatch('preferences',preferences.handle,event,claim)
        selection = self.reminder_service.prepare_request(event,tasks=binding.tasks) if self.reminder_service else None
        if selection is not None and selection.kind == 'watch' and reminder_command(selection,text):
            binding.decision_source = 'bound_reminder'
            return self._dispatch('reminders',self.reminder_service.execute,selection,event,claim)
        if self.trip_service is not None:
            trip_input = trip_command(text)
            trip_task = trip_continuation(text,binding.tasks)
            trips = self.store.trips.list_for_owner(event)
            date_answer = len(trips)==1 and trips[0]['spec'].get('date_is_tentative') and reference_date_answer(text)
            if trip_input or trip_task or date_answer:
                binding.decision_source = 'bound_trip'
                return self._dispatch('trip',self.trip_service.handle,event,claim,tasks=binding.tasks,
                    operation=('create' if trip_input.name=='trip_create' else 'edit') if trip_input else None)
        if selection is not None and reminder_command(selection,text):
            binding.decision_source = 'bound_reminder'
            return self._dispatch('reminders',self.reminder_service.execute,selection,event,claim)
        request = self.read_only_service.from_context(binding.read_context)
        if request is not None:
            binding.route_owner, binding.decision_source = 'travel_query','continuation'
            return request
        if re.search(r'R-\d{8}-\d{3}|A-\d{6}',text):
            binding.route_owner = 'reservation'
            return '尚未明确本次预约操作，未修改任何预约。请分别说明操作和编号，确认请用“确认预约 R-编号”。'
        return None

    def _hotel_action(self,event,claim,task,values):
        context=bind_read_context(event,(),task=task)
        ir=IntentIR(1,'read',(IntentOperation('hotel','query',values),),source_spans=(event.content,))
        return self._dispatch('hotel_action',self.read_only_service.prepare,event,claim,ir,context,source='hotel_action')

    def _finish_research_plan(self,event,claim,answer):
        if not event.content.startswith('根据研究结果'):
            current=self.store.research.get(event)
            if not current or current.get('stage') not in {'completed','partial'} or not current.get('report'):
                return answer+'\n尚未取得本次研究结果，未生成行程。'
        material = self.research_service.read_for_planning(event)
        if material.source is None:
            return answer if not material.note or material.note in answer else answer+'\n'+material.note
        reply = self.trip_service.handle(event,claim,source_text_override=material.source,
                                         tasks=bind_request(self.store,event).tasks,operation='create')
        return (reply if event.content.startswith('根据研究结果') or material.provider_answer else answer+'\n\n'+reply) if reply else answer

    def _research_plan_intent(self,event,claim,ir):
        if self.research_service is None or self.trip_service is None:
            return '研究或行程服务暂不可用，本次未执行。'
        planning=next(op for op in ir.operations if op.domain=='trip')
        if (ir.action!='create' or planning.values.keys()!={'request_text'}
                or planning.values['request_text']!=event.content):
            return '研究后规划需要本轮明确的完整要求，本次未执行。'
        if self.mode!='execute':
            return '已识别先研究再规划的请求；当前仅解析预览，尚未执行。'
        research=next(op for op in ir.operations if op.domain=='research')
        answer=self._research(event,claim,replace(ir,action='read',operations=(research,)))
        return self._finish_research_plan(event,claim,answer)

    def shadow(self, event):
        if not semantic_candidate(event.content):
            return None
        try:ir = self.compiler.try_compile(event.content)
        except IntentCompilationError:return None
        if ir is not None:
            logger.info('Semantic shadow IR: event=%s action=%s operations=%s', event.event_key, ir.action,
                        [(item.domain, item.operation) for item in ir.operations])
        return ir

    def handle(self, event, claim):
        try:return self._handle(event,claim)
        except IntentCompilationError as error:
            binding=CURRENT_REQUEST.get()
            if binding is not None:
                binding.route_owner='intent_failure';binding.decision_source=error.code
            elif getattr(self.store,'diagnostics',None):
                self.store.diagnostics.record('routing',{'event_key':event.event_key,'status':'intent_failed',
                    'route_owner':'intent_failure','error_code':error.code,'candidate_count':0})
            if self.mode=='shadow':return None
            if error.budget_code and binding and binding.read_context.task and binding.read_context.task.task_type=='hotel' and claim is not None:
                task=binding.read_context.task
                reply=error.detail+(' 酒店条件已保留；可明确回复“'+NEW_RUN_COMMAND+'”，建立关联的新一轮核价。' if error.budget_code in RUN_LIMIT_CODES else '')
                slots={**binding.read_context.slots,'model_budget_block':{'code':error.budget_code,'details':error.budget_details},
                    'budget_pending_request':{'text':event.content,'event_key':event.event_key,'occurred_at':(event.occurred_at or datetime.now(timezone.utc)).isoformat()}}
                if '_read_batch' in task.slots:
                    parent=self.store.tasks.owned(event,task.task_id)
                    index=str(task.slots['hotel_query_index'])
                    run_fields={k:slots[k] for k in ('budget_run_key','budget_run_parent','budget_run_started_by') if k in slots}
                    slots={**parent.slots,**run_fields,'hotel_states':{**parent.slots.get('hotel_states',{}),index:{k:v for k,v in slots.items() if k!='_read_batch'}}}
                    task=parent
                self.store.tasks.prepare_result(event,claim,TaskUpdate(task.task_type,'collecting',task.initial_request,
                    slots,task.missing_slots,
                    task_id=task.task_id,expected_version=task.version),reply)
                return reply
            if error.detail:return error.detail
            reason={'timeout':'模型服务响应超时','connection':'模型服务连接未成功',
                'authentication':'模型服务鉴权未通过','rate_limit':'模型服务当前限流',
                'upstream_error':'模型服务暂时不可用','invalid_request':'模型服务请求未被接受，请核对模型及接口配置',
                'budget_exceeded':'模型调用预算不足','invalid_response':'模型返回的需求解析结果未通过校验',
                'internal_error':'意图解析出现内部错误','model_configuration':'模型配置或所需能力不匹配，未换供应商重发'}.get(error.code,'模型解析服务未完成')
            return reason+'，本次未执行。完整请求已保留，可在服务恢复或配置调整后重试原请求，无需改写已提供的需求。'

    def _handle(self, event, claim):
        if claim is not None and getattr(claim, 'prepared_reply', None) is not None:
            return claim.prepared_reply
        if event.trip_edit is not None:
            if self.mode == 'shadow':
                return None
            if self.mode == 'preview':
                return '已识别字段调整；当前仅解析预览，尚未生成或保存草稿。'
            if event.platform != 'web' or self.trip_service is None:
                return '此入口不支持行程字段操作。'
            return self._dispatch('trip',self.trip_service.edit_field,event,claim)
        if self.mode=='execute':
            resumed=self.read_only_service.resume(event,claim)
            if resumed is not None:
                return resumed
        binding = bind_request(self.store,event)
        pending = binding.pending_semantic
        reminders = binding.pending_reminders
        if self.mode == 'execute' and not pending and not reminders and self.trip_service is not None:
            trip_task = trip_continuation(event.content.strip(), binding.tasks)
            if (trip_task and trip_task.missing_slots == ('start_date',)
                    and not event.message_reference
                    and not any(t.status == 'collecting' and t.task_id != trip_task.task_id for t in binding.tasks)):
                binding.decision_source = 'bound_trip'
                return self._dispatch('trip',self.trip_service.handle,event,claim,tasks=(trip_task,))
            from tools.trip.tool import reference_date_answer
            if reference_date_answer(event.content.strip()):
                dated_trips = self.store.trips.list_for_owner(event)
                if len(dated_trips) == 1 and dated_trips[0]['spec'].get('date_is_tentative'):
                    return self.trip_service.handle(event, claim)
        rule = parse_rules(event.content) if rules_enabled() and not pending and not reminders and not event.attachments else None
        if hasattr(self.store.delivery,'get_event_tool_result'):
            frozen=self.store.delivery.get_event_tool_result('rule-plan:'+hashlib.sha256(event.event_key.encode()).hexdigest())
            if frozen:
                from core.intent_ir import parse_intent_ir
                snapshot=json.loads(frozen)
                if snapshot.get('version')!=RULE_VERSION:
                    return '本次查询使用较早的规则版本，已完成结果保留；请重新发起完整查询。'
                rule=RuleResult('complete',parse_intent_ir(snapshot['ir'],event.content),'resumed')
        if rule and hasattr(self.store,'diagnostics'):
            self.store.diagnostics.record('routing',{'event_key':event.event_key,'status':rule.status,
                'reason':rule.reason,'prompt_version':RULE_VERSION,'candidate_count':len(rule.ir.operations) if rule.ir else 0})
        if not semantic_candidate(event.content) and not (rule and rule.ir):
            return None
        compile_text = event.content
        correction=route_correction(event.content)
        read_tasks = binding.tasks
        read_context = binding.read_context
        if correction and not re.search(r'[；;]|顺便|同时|并且|还有|查询天气', event.content) and not (read_context.task and read_context.task.task_type=='rail') and not read_context.slots.get('reference_event_id'):
            if read_context.task is None:
                return '请回复需要更正的那条路线消息，或重新提供另一端地点；不会猜测要修改哪条路线。'
            if self.mode=='shadow':
                return None
            domains=read_context.slots.get('intents',[read_context.task.task_type])
            operations=tuple(IntentOperation(domain,'search' if domain=='places' else 'query',
                {field:read_context.slots[field] for field in READ_OPERATION_FIELDS[domain] if read_context.slots.get(field)})
                for domain in domains if domain in READ_OPERATION_FIELDS)
            corrected=IntentIR(1,'read',operations,source_spans=tuple(correction.values()))
            return self._dispatch('travel_query',self.read_only_service.prepare,event,claim,corrected,read_context,source='correction',mode=self.mode)
        waiting_reads = [value for value in read_tasks if value.task_type in READ_TASK_TYPES
                         and value.status == 'collecting']
        if read_context.task is None and is_time_answer(event.content) and len([
                t for t in waiting_reads if set(t.missing_slots)&{'check_in','check_out','travel_date'}])>1:
            return '有多个查询等待日期，请引用对应的酒店或铁路消息，或说明要补充哪一项；本次没有查询。'
        rail_candidates=[task for task in read_tasks if task.task_type=='rail' and task.status in {'collecting','completed','failed'}]
        if len(rail_candidates)>1 and read_context.task is None and rail_time_followup(event.content):
            return '有多个铁路查询可供修改，请使用“引用查询”选择对应回复，或重新说明起终点和日期。'
        if (len(waiting_reads)>1 and location_answer(event.content) and read_context.task is None
                and not re.search(r'到|至|→|->',event.content)):
            return '有多个查询正在等待补充，请明确要继续的查询，或重新提供完整路线/地点。'
        if read_context.task is not None or read_context.slots.get('reference_event_id'):
            compile_text = read_context.message
        if pending is not None:
            compile_text = pending.slots.get('source_text', pending.initial_request) + '\n用户补充：' + event.content
        if rule and rule.ir and rule.ir.action == 'read' and read_context.task is None:
            if self.mode == 'shadow':
                return None
            return self._dispatch('travel_query',self.read_only_service.prepare,event,claim,rule.ir,read_context,source='rule',mode=self.mode)
        if hasattr(self.store,'planning'):
            PlanningArtifacts(self.store.planning).ensure_history(event)
        trips, trip_context = build_trip_context(self.store, event, pending)
        artifacts = tuple(self.store.planning.list_for_owner(event)) if hasattr(self.store,'planning') else ()
        if artifacts:
            trip_context['planning_artifacts']=[{'index':a['ordinal'],'kind':a['kind'],'title':a['title'],
                'destination':a['destination'],'duration_options':a['duration_options'],'version':a['version']}
                for a in artifacts]
        research_context = self.store.research.context(event) if hasattr(self.store,'research') else None
        if research_context:
            trip_context['research_context']={key:research_context[key] for key in
                ('destination','preferred_domains','excluded_domains','strict_sources')}
        context = json.loads(self._reminder_context(reminders, event)) if reminders else {}
        context.update(trip_context)
        from app.conversation_context import lodging_context
        from core.context_contract import task_projection
        context['lodging_context']=lodging_context(self.store,event) if not (read_context.task and read_context.task.task_type=='hotel') else {}
        context['hotel_tasks']=[{'index':i,**task_projection(t)} for i,t in enumerate(read_tasks,1)
            if t.task_type=='hotel' and (not read_context.task or t.task_id!=read_context.task.task_id)]
        if event.message_reference:
            from core.context_contract import reference_projection
            context['message_reference']=reference_projection(event.message_reference,
                text_limit=800 if pending or event.message_reference.get('trip') else 4000)
        if read_context.task is not None or read_context.slots.get('reference_event_id'):
            from core.context_contract import hotel_state_projection
            context['read_task'] = {'type': read_context.task.task_type if read_context.task else 'rail', 'slots': hotel_state_projection(read_context.slots) if read_context.task and read_context.task.task_type=='hotel' else read_context.slots,
                                    'reference_time': read_context.reference_time}
        if rule and rule.ir and read_context.task is None:
            ir = rule.ir
        elif context:
            continuing_trip = pending and pending.slots.get('constraint_trip')
            ir = self.compiler.try_compile(event.content if reminders and not continuing_trip else compile_text,
                                           context=json.dumps(context, ensure_ascii=False))
        else:
            ir = self.compiler.try_compile(compile_text)
        if compound_boundary(event.content) and not (rule and rule.ir):
            def incomplete(value):
                if (value is not None and len(value.operations)==1 and value.operations[0].domain=='trip'
                        and value.operations[0].operation=='create' and value.action in {'create','clarify'}
                        and value.operations[0].values.get('request_text')==compile_text==event.content):
                    # A complete creation request is one coordinating workflow, regardless of punctuation.
                    return False
                if value is None or not value.operations:return True
                if len(value.operations)>1:return False
                operation=value.operations[0]
                if operation.domain!='trip':return True
                if operation.operation in TRIP_READ_OPERATIONS:
                    checks=operation.values.get('requested_checks',[])
                    return not (checks and (operation.operation=='view_current_trip'
                        or operation.values.get('include_trip') or len(checks)>1))
                return not (operation.operation=='update_constraints' and
                    (len(operation.values.get('changes',{}))>1 or operation.values.get('save_preference_text')
                     or operation.values.get('requested_checks')))
            if incomplete(ir):
                ir=self.compiler.try_compile(compile_text,context=json.dumps(context,ensure_ascii=False)+
                    '\n请检查完整请求中的全部事项，不要只返回第一项；不能合并时保留所有operations。')
                if incomplete(ir):
                    return None if self.mode=='shadow' else '这条消息包含多个部分，尚未可靠识别全部事项，本次没有执行。请分别写明每项需求后重试。'
        if ir is None and unsafe_command_tail(event.content):
            return '未能完整识别这条查询及附加要求，本次尚未执行，请重新说明全部需求。'
        if ir is None:
            if self.mode=='shadow':
                return None
            return ('这次未能可靠解析你的补充，提醒尚未完成设置。请再说明要补充的时间、事项，或明确结束设置。'
                    if reminders else '未能可靠识别完整请求，本次未执行。请补充或重新说明需求。')
        if (ir.action in {'read','clarify'} and ir.operations and
                all(op.domain=='trip' and op.operation in TRIP_READ_OPERATIONS for op in ir.operations)):
            if self.trip_service is None:return '行程服务暂不可用。'
            return self._dispatch('trip',self.trip_service.read_intent,event,claim,ir,trips,trip_context,pending,compile_text,mode=self.mode)
        if ir is not None and len(ir.operations) > 1:
            pairs = [(o.domain,o.operation) for o in ir.operations]
            if len(pairs)==2 and set(pairs)=={('research','search_travel_guides'),('trip','update_constraints')}:
                # Repair only this ambiguous protocol split; never infer that unrelated research is a price check.
                repaired=self.compiler.try_compile(compile_text,context=json.dumps(context,ensure_ascii=False)+
                    '\n若查询仅为当前同一餐饮/住宿候选的价格解释，并用于该对象预算修改，请合并为一个trip/update_constraints，带requested_checks及原话。'
                    '若是景点研究、其他对象或独立事项，必须保留所有operations，不得丢弃。')
                if (repaired and len(repaired.operations)==1 and repaired.operations[0].domain=='trip'
                        and repaired.operations[0].operation=='update_constraints'
                        and repaired.operations[0].values.get('requested_checks')):
                    ir=repaired;pairs=[('trip','update_constraints')]
            if len(pairs)==2 and set(pairs)=={('research','search_travel_guides'),('trip','create')}:
                return self._dispatch('research_plan',self._research_plan_intent,event,claim,ir)
            reads = all(d in READ_OPERATION_FIELDS and op == ('search' if d == 'places' else 'query') for d,op in pairs)
            linked = (len(pairs) == 2 and set(pairs) == {('trip','remove_activity'),('reminder','cancel_linked_reminder')})
            if linked:
                removal=next(op.values for op in ir.operations if op.domain=='trip')
                activity=removal.get('activity')
                name=activity.get('name') if isinstance(activity,dict) else removal.get('entity')
                entity=next(op.values.get('entity') for op in ir.operations if op.domain=='reminder')
                linked=bool(name) and name==entity
            if len(pairs)>1 and not reads and not linked:
                if self.mode == 'shadow':
                    return None
                labels = {'trip':'行程调整','reminder':'提醒','research':'攻略研究','booking':'预约',
                          'weather':'天气','forecast':'天气预报','route':'路线','transit':'公共交通'}
                items = '、'.join(f'{i}. {TRIP_READ_OPERATIONS.get(o.operation,labels.get(o.domain,o.domain)) if o.domain=="trip" else labels.get(o.domain,o.domain)}' for i,o in enumerate(ir.operations,1))
                return self._save_clarification(event,claim,ir,
                    '已识别这些事项：'+items+'。这组操作暂不能一起执行，本次尚未执行任何一项。请明确先处理的事项及要求。',
                    source_text=compile_text,task=pending)
        if self.mode == 'shadow' and reminders:
            return None
        if ir is not None and ir.action == 'clarify' and not ir.operations and reminders:
            return '提醒设置仍在等待补充。请说明具体时间或要继续的事项，不明确的时间不会自动创建提醒。'
        recovered = self._recover_ir(event, pending, binding.tasks)
        if (recovered is not None and ir is not None
                and any(item.domain in {'weather','forecast','route','traffic','places','walking','transit'}
                        for item in ir.operations)
                and any(item.domain == 'trip' for item in recovered.operations)):
            ir = recovered
        if ir.action=='none' and recovered is not None:
            ir = recovered
        if self.mode=='shadow':
            return None
        if ir.action=='none':
            if (event.platform=='web' and not pending and not reminders and not waiting_reads and rail_time_followup(event.content)
                    and self.store.tasks.has_history(event.platform,event.storage_scope_id,event.sender_id,task_type='rail')):
                return '未找到可自动接续的查询，请使用历史铁路回复旁的“引用查询”选择对象，或重新提供起终点、日期和时段。'
            command_candidate = rule or parse_rules(event.content)
            if command_candidate.ir is not None and command_candidate.ir.action=='read':
                binding.decision_source = 'complete_command'
                return self._dispatch('travel_query',self.read_only_service.prepare,event,claim,
                    command_candidate.ir,read_context,source='rule',mode=self.mode)
            return None
        if not ir.operations:
            return self._save_clarification(event,claim,ir,'请补充这次操作涉及的地点、日期或事项。',
                                            task=pending,source_text=compile_text)
        if (len(ir.operations)==1 and ir.operations[0].domain=='system'
                and ir.operations[0].operation=='unsupported_transport'
                and ir.operations[0].values.get('request_text')==event.content):
            return '目前尚未接入实时航班数据，无法查询机票价格与余票。'
        if all(op.domain in READ_OPERATION_FIELDS for op in ir.operations):
            return self._dispatch('travel_query',self._read_intent,event,claim,ir,read_context,waiting_reads,compile_text,context)
        handlers = {
            ('search','query_official_website'): ('search',{'read','clarify'},lambda: self._website(event,ir)),
            ('search','continue_official_websites'): ('search',{'read'},lambda: self._website(event,ir)),
            ('trip','create'): ('trip',{'create','clarify'},
                lambda: self.trip_service.apply_text_intent(event,claim,ir,mode=self.mode,tasks=binding.tasks)),
            ('trip','edit'): ('trip',{'update','cancel','clarify'},
                lambda: self.trip_service.apply_text_intent(event,claim,ir,mode=self.mode,tasks=binding.tasks)),
            ('trip','create_from_plan_artifact'): ('trip',{'create','update','clarify'},
                lambda: self.trip_service.adopt_intent(event,claim,ir,artifacts,pending,compile_text,mode=self.mode)),
            ('trip','update_constraints'): ('trip',{'update','clarify'},
                lambda: self.trip_service.update_intent(event,claim,ir,trips,trip_context,pending,compile_text,artifacts,mode=self.mode)),
            ('trip','adopt_hotel_quote'): ('trip',{'update','clarify'},
                lambda: self.trip_service.adopt_hotel_quote(event,claim,ir,binding.tasks,mode=self.mode)),
            ('trip','check_prices'): ('trip',{'read','clarify'},
                lambda: self.trip_service.update_intent(event,claim,ir,trips,trip_context,pending,compile_text,artifacts,mode=self.mode)),
            ('trip','remove_activity'): ('trip',{'update','delete','cancel','clarify'},
                lambda: self.trip_service.handle_intent(event,claim,ir,mode=self.mode)),
            ('trip','view_current_trip'): ('trip',{'read'},
                lambda: self.trip_service.handle_intent(event,claim,ir,mode=self.mode)),
            ('trip','confirm_pending_operation'): ('trip',{'confirm'},
                lambda: self.trip_service.confirm_intent(event,claim,ir,mode=self.mode)),
            ('reminder','continue_pending'): ('reminders',{'create','update','clarify','cancel'},
                lambda: self._continue_reminder(event,claim,ir,reminders)),
            ('reminder','create'): ('reminders',{'create','clarify'},lambda: self._reminder(event,claim,ir)),
            ('reminder','cancel_linked_reminder'): ('reminders',{'cancel','delete'},lambda: self._reminder(event,claim,ir)),
            ('research','search_travel_guides'): ('research',{'read','create','clarify'},lambda: self._research(event,claim,ir)),
        }
        for operation in ('update','suggest','forget'):
            handlers[('preferences',operation)] = ('preferences',{'read','create','update','delete','clarify'},
                lambda: (self.preferences_tool or PreferencesTool(self.store.preferences)).handle_intent(event,claim,ir,mode=self.mode))
        for operation in ('create_booking_reminder','query_booking_policy','create_scheduled_query','create_policy_watch'):
            handlers[('reminder',operation)] = ('reminders',{'create','read','clarify'},
                lambda: self.reminder_service.apply_domain_intent(event,claim,ir,mode=self.mode,tasks=binding.tasks))
        for name,definition in TOOL_DEFINITIONS.items():
            if definition.owner=='reservation':
                handlers[('booking',name)] = ('reservation',{'read','create','update','confirm','cancel','delete','clarify'},
                    lambda: self._booking_policy(event,ir))
        operation = next((op for op in ir.operations if op.domain=='trip'),ir.operations[0])
        selected = handlers.get((operation.domain,operation.operation))
        if selected is None:
            return '这项操作尚未支持，本次没有执行。请说明需要查询、修改或确认的具体事项。'
        owner,actions,execute = selected
        if ir.action not in actions:
            return '解析到的动作与操作类型不一致，本次没有执行。'
        if owner=='trip' and self.trip_service is None:
            return '行程服务暂不可用，本次未修改行程。'
        if owner=='reminders' and self.reminder_service is None:
            return '提醒服务暂不可用，本次未执行。'
        if ir is recovered:
            binding.decision_source = 'bound_command'
        return self._dispatch(owner,execute)

    def _read_intent(self,event,claim,ir,read_context,waiting_reads,compile_text,context):
        from tools.travel_query import ReadCoverageError
        if read_context.task and read_context.task.task_type=='hotel' and (
                not any(op.domain=='hotel' for op in ir.operations) or
                any(op.domain=='hotel' and op.values.get('targets') and
                    not read_task_compatible(read_context.task,ir,event.content) for op in ir.operations)):
            read_context=TaskContext(None,event.content,(event.occurred_at or datetime.now(timezone.utc)).isoformat(),{},event.content)
        if read_context.slots.get('reference_event_id') and any(op.domain!='rail' for op in ir.operations):
            read_context=TaskContext(None,event.content,(event.occurred_at or datetime.now(timezone.utc)).isoformat(),{},event.content)
        referenced=bool(event.reply_to_id and read_context.task and read_context.task.reference_message_id==event.reply_to_id)
        if (read_context.task and read_context.task.task_type in ROUTE_TASK_TYPES and not referenced
                and not route_correction(event.content) and not read_task_compatible(read_context.task,ir,event.content)):
            read_context=TaskContext(None,event.content,(event.occurred_at or datetime.now(timezone.utc)).isoformat(),{},event.content)
        if not event.reply_to_id and not event.message_reference and len(ir.operations)==1 and ir.operations[0].domain=='hotel' and ir.operations[0].values.get('targets'):
            specific=[task for task in waiting_reads if task.task_type=='hotel' and candidate_choices(
                merge_hotel_state(task.slots,{}),event.content,ir.operations[0].values['targets'])]
            if len(specific)>1:return '有多个候选查询符合这个门店，请引用对应列表，避免混用入住日期；本次没有查询。'
            if len(specific)==1:read_context=bind_read_context(event,(),task=specific[0])
        if read_context.task is None:
            compatible=[task for task in waiting_reads if read_task_compatible(task,ir,event.content)]
            if len(compatible)>1 and any(op.domain=='hotel' for op in ir.operations):
                return '有多个酒店查询等待补充，请引用对应消息或说明具体门店；本次没有查询。'
            if len(compatible)==1:
                read_context=bind_read_context(event,(),task=compatible[0])
        binding=CURRENT_REQUEST.get()
        if read_context.task is None and binding and any(op.domain=='hotel' for op in ir.operations):
            candidates=[t for t in binding.tasks if t.task_type=='hotel' and t.status=='completed' and read_task_compatible(t,ir,event.content)]
            if len(candidates)==1:read_context=bind_read_context(event,(),task=candidates[0])
            elif len(candidates)>1 and any(op.values.get('reference') for op in ir.operations):
                return '有多份可引用的酒店查询，请引用对应报价消息后继续；本次没有查询。'
        if any(op.domain=='hotel' for op in ir.operations) and read_context.task is None and not event.message_reference:
            lodging=context.get('lodging_context',{})
            known={k:lodging[k] for k in ('targets','stay') if lodging.get(k)}
            if not known.get('targets') and lodging.get('discovery',{}).get('city'):known['discovery']=lodging['discovery']
            read_context=replace(read_context,slots={**read_context.slots,'hotel_request':known,
                'hotel_date_proposal':lodging.get('proposed_stay',{}),'source_context':lodging.get('source_context',{})})
        if binding and binding.pending_semantic and any(op.domain=='hotel' for op in ir.operations):
            read_context=replace(read_context,task=read_context.task or binding.pending_semantic,
                message=compile_text,slots={**read_context.slots,'hotel_evidence':compile_text})
        try:
            return self.read_only_service.prepare(event,claim,ir,read_context,mode=self.mode)
        except ReadCoverageError:
            corrected=self.compiler.try_compile(compile_text,context=json.dumps(context,ensure_ascii=False)+
                '\n只读事项覆盖校验失败。请检查原话的全部查询，返回完整IntentIR；不要新增写操作。')
            if corrected is not None and corrected.action in {'read','clarify'}:
                try:
                    return self.read_only_service.prepare(event,claim,corrected,read_context,mode=self.mode)
                except ReadCoverageError:
                    pass
            return '尚未完整识别这条消息中的全部查询，本次未调用数据源；请分别说明需要查询的事项。'

    def _website(self,event,ir):
        from core.tool_result import AgentToolContext, OFFICIAL_WEBSITE_TOOL
        if len(ir.operations)!=1 or not ir.source_spans:
            return '请明确要查询官网的景点名称。'
        values = ir.operations[0].values
        action = ir.operations[0].operation
        if action=='query_official_website' and not values.get('name'):
            return '请补充要查询官网的景点名称。'
        if self.mode != 'execute':
            return '已识别官网查询，当前仅预览，尚未联网。'
        if self.tool_executor is None:
            return '官网搜索工具暂不可用。'
        context = AgentToolContext(event.platform,event.scope_id,event.sender_id,event.event_key,
            allowed_actions=frozenset({action}),event=event)
        return self.tool_executor(action,values,context).data

    def _research(self,event,claim,ir):
        if self.research_service is None:
            return '联网研究服务暂不可用。'
        binding = bind_request(self.store,event)
        return self.research_service.apply_intent(event,claim,ir,mode=self.mode,tasks=binding.tasks)



    def _pending_reminders(self, event):
        return bind_request(self.store,event).pending_reminders

    def _reminder_context(self,tasks,event):
        return reminder_context(self.store,tasks,event,self.clock)

    def _continue_reminder(self,event,claim,ir,tasks):
        if self.reminder_service is None:
            return '没有可接续的提醒设置，可能已经完成或过期。请重新说明事项和时间。'
        return self.reminder_service.continue_intent(event,claim,ir,tasks,mode=self.mode)

    def _recover_ir(self, event, pending, tasks):
        text = event.content
        if not re.search(r'不去|删除|取消.{0,12}(?:行程|日程)|不安排', text):
            return None
        trips = self.store.trips.list_for_owner(event)
        if len(trips) != 1:
            return None
        names = list(dict.fromkeys(place['name'] for place in trips[0]['sources'].values()
                                   if place.get('name') and place['name'] in text))
        if len(names) != 1:
            return None
        date_pattern=r'(?:(?:20\d{2}年)?\d{1,2}月\d{1,2}[日号]?|20\d{2}-\d{1,2}-\d{1,2}|大后天|后天|明天|今天)'
        if not re.fullmatch(rf'(?:{date_pattern})?\s*(?:不去|删除|不安排){re.escape(names[0])}(?:了)?[。！! ]*',text):
            return None
        date_match = re.search(r'(?:(?:20\d{2}年)?\d{1,2}月\d{1,2}[日号]?|20\d{2}-\d{1,2}-\d{1,2}|大后天|后天|明天|今天)', text)
        activity = {'name': names[0]}
        if date_match:
            activity['date_text'] = date_match[0]
        operations = [IntentOperation('trip', 'remove_activity', {'activity': activity})]
        if re.search(r'预约提醒|关联提醒', text):
            operations.append(IntentOperation('reminder', 'cancel_linked_reminder', {'entity': names[0]}))
        spans = tuple(item for item in (date_match[0] if date_match else '', names[0]) if item)
        return IntentIR(1, 'update', tuple(operations), requires_confirmation=True, source_spans=spans)


    def _save_clarification(self, event, claim, ir, question, *, source_text=None, task=None, trip=None):
        if not hasattr(self.store, 'tasks'):
            return question
        slots = {'semantic_ir': to_jsonable(ir), 'source_text': source_text or event.content}
        if trip:
            slots['constraint_trip'] = {'trip_id':trip['trip_id'],'version':trip['version']}
        update = TaskUpdate('semantic', 'collecting', task.initial_request if task else event.content,
                            slots, tuple(ir.missing_fields or ('semantic_input',)),
                            task.task_id if task else '', task.version if task else None)
        self.store.tasks.prepare_result(event, claim, update, question)
        return question


    def _reminder(self,event,claim,ir):
        return (self.reminder_service.apply_intent(event,claim,ir,mode=self.mode) if self.reminder_service
                else '提醒服务暂不可用，本次未执行。')
