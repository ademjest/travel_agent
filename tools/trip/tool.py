from core.chat_transport import ChatEvent
from tools.trip.constraints import proposal_metadata, plan_durations, CAPTURE_VERSION
from infrastructure.planning_repository import PlanningConflict
from infrastructure.task_repository import TaskConflict
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import json
import hashlib
import re
import os
import logging
from core.intent_ir import IntentIR, IntentOperation, to_jsonable, parse_intent_ir, TRIP_READ_OPERATIONS
from tools.preferences import PreferencesTool, preference_request


def hotel_quote_request(trip,names=()):
    """Project itinerary candidates; the independent price executor never receives a trip."""
    from tools.trip.planner import assess_hotels,HOTEL_PATTERN
    spec,plan,places=trip['spec'],trip['plan'],trip['sources']
    selection={**spec,'budgets':{k:v for k,v in spec.get('budgets',{}).items() if k!='accommodation'}}
    eligible=[r['poi_id'] for r in assess_hotels(selection,{k:v for k,v in plan.items() if k!='hotel_quotes'},places) if r['status']!='conflict']
    selected=[pid for pid in (plan.get('hotel_ids') or plan.get('hotel_quotes',{}).get('requested_poi_ids',[])) if pid in eligible]
    targets={pid:places[pid] for pid in selected or eligible if HOTEL_PATTERN.search(str(places[pid].get('type','')))
             and (not names or places[pid]['name'] in names)}
    return {'city':spec.get('destination',''),'stay':deepcopy(spec.get('hotel_stay',{})),
        'budget':spec.get('budgets',{}).get('accommodation'),'source_trip_id':trip.get('trip_id'),
        'is_trial':bool(spec.get('date_is_tentative') or spec.get('_hotel_quote_trial'))},targets


def lookup_hotel_quotes(service,event,trip,*,refresh=False,names=(),allow_query=True):
    from tools.research.sources import planning_policy
    from tools.trip.constraints import missing_stay
    request,targets=hotel_quote_request(trip,names)
    missing=missing_stay(trip['spec'],service.clock())
    if missing:return {'offers':[],'stay':request['stay'],'note':'请一并补充：'+'、'.join(missing)+'。补充后继续核价，先给预览，不直接保存。','missing_fields':missing}
    result=service.lookup(event,request,targets=targets,policy=planning_policy(trip['spec'],event.content,automatic=True),
        refresh=refresh,allow_query=allow_query)
    if not allow_query and not result.get('request_id') and trip['plan'].get('hotel_quotes'):
        return deepcopy(trip['plan']['hotel_quotes'])
    return result

logger = logging.getLogger(__name__)
from tools.trip.constraints import record_sources, trip_diff, render_diff

from tools.trip.planner import TripPlanError
from core.tasks import TaskUpdate, utc_now
from core.commands import location_answer
from tools.trip.constraints import apply_constraints, transport_only, MODES, PACE, BUDGET_SCOPES, budget_default_note
from tools.reminders.tool import DATE_PATTERN, change_plan
from tools.reminders.policy import BookingPolicyResolver, PolicyUnavailable
from core.time_input import number, parse_reminder_time
from tools.trip.constraints import apply_reference_date, confirm_planning_date, reference_date_note
from tools.trip.constraints import DATE_RANGE
from tools.trip.planner import invalidate_changed_dates
from tools.research.evidence import ResearchSource


TRIP_ID = r'TR-[a-f0-9]{12}'
CONFIRM_TRIP = {'确认行程修改', '确认行程提醒','保存这版行程'}
TRIP_COMMANDS = {'查看行程', '查看我的行程', '我的行程', '取消行程', '重新查询行程预约规则',
                 '重新生成行程预览',
                 '设置行程提醒', '按行程提醒预约', '刷新行程交通', '刷新行程预约规则', *CONFIRM_TRIP}


def trip_request(text):
    if re.search(r'R-\d{8}-\d{3}|A-\d{6}', text):
        return False
    stripped = re.sub(TRIP_ID, '', text).strip()
    return bool(stripped in TRIP_COMMANDS or re.search(
        r'(?:规划|制定|设计).{0,50}(?:行程|旅行|旅游)|安排.{0,40}(?:行程|[一二两三四五六七八九十\d]+[天日](?!后))|'
        r'(?:去|在).{1,20}(?:玩|游玩)[一二两三四五六七八九十\d]+天|'
        r'第[一二两三四五六七八九十\d]+天.{0,20}(?:改|换|不去|删除)|'
        r'(?:行程|旅程).{0,15}(?:改成|改为|调整|改到)|'
        r'(?:放到|挪到|放|挪)第[一二两三四五六七八九十\d]+天', text))


def trip_continuation(text, tasks):
    pending = [task for task in tasks if task.task_type == 'itinerary' and task.status == 'collecting']
    ids = re.findall(TRIP_ID, text)
    if ids:
        if len(set(ids)) != 1:
            return None
        pending = [task for task in pending if task.slots.get('trip_id') == ids[0]]
        text = re.sub(TRIP_ID, '', text).strip()
    if text in CONFIRM_TRIP | {'重新生成行程预览','重新查询行程预约规则', '取消行程', '查看行程', '查看我的行程'}:
        candidates = [task for task in pending if 'confirmation' in task.missing_slots
                      and task.slots.get('candidate_trip')]
        trip_ids = {task.slots.get('trip_id') for task in candidates}
        if candidates and len(trip_ids) == 1:
            return candidates[0]
    if len(pending) != 1:
        return None
    task = pending[0]
    if task.slots.get('planning_failure') and text in {'继续规划','重新生成行程预览'}:return task
    traffic=task.slots.get('candidate_trip',{}).get('plan',{}).get('intercity',{})
    missing={field for direction in ('outbound','inbound') for field in traffic.get(direction,{}).get('missing_fields',[])}
    if missing and set(task.missing_slots) <= {'confirmation'} and (location_answer(text) if 'origin' in missing else reference_date_answer(text)):
        return task
    if text in CONFIRM_TRIP | {'重新生成行程预览','重新查询行程预约规则', '取消行程', '查看行程', '查看我的行程'}:
        return task
    if 'confirmation' in task.missing_slots and re.search(r'https://\S+', text):
        return task
    if ('confirmation' in task.missing_slots and (not task.slots.get('candidate_trip', {}).get('spec', {}).get('start_date') or task.slots.get('candidate_trip', {}).get('spec', {}).get('date_is_tentative'))
            and re.fullmatch(DATE_PATTERN + r'[。 ]*', text)):
        return task
    if 'destination' in task.missing_slots and location_answer(text):
        return task
    if 'duration' in task.missing_slots and re.fullmatch(r'[一二两三四五六七八九十\d]+[天日](?:游)?[。 ]*', text):
        return task
    if 'start_date' in task.missing_slots and (reference_date_answer(text) or DATE_RANGE.fullmatch(text.rstrip('。 ')) or text == '先不定日期'):
        return task
    if 'trip_ref' in task.missing_slots and re.fullmatch(r'(?:第)?[一二两三四五六七八九十\d]+(?:个|条)?', text):
        return task
    if 'document_ref' in task.missing_slots and (re.fullmatch(r'(?:第)?[一二两三四五六七八九十\d]+(?:个|份)?', text)
            or text in task.slots.get('document_names', [])):
        return task
    if 'transport' in task.missing_slots and text in {'公共交通', '公交', '地铁', '步行', '驾车', '自驾', '开车'}:
        return task
    return None


def reference_date_answer(text):
    return bool(re.fullmatch(r'(?:我(?:已经)?(?:决定|确定|定了)?(?:了)?|实际)?\s*(?:在|从|是)?' + DATE_PATTERN
                            + r'\s*(?:开始(?:旅游|旅行|游玩|行程)?|出发|去(?:旅游|旅行|玩))?[。！! ]*', text))


class TripTool:
    def __init__(self, store, planner, *, policy_resolver=None, clock=None, research_reader=None, rail_executor=None,answer_writer=None,hotel_quotes=None):
        self.store = store
        self.planner = planner
        self.policy_resolver = policy_resolver or BookingPolicyResolver()
        self.clock = clock or utc_now
        self.research_service = research_reader
        self.rail_executor=rail_executor
        self.answer_writer=answer_writer
        self.hotel_quotes=hotel_quotes

    def handle_draft_action(self,event,claim):
        """Deterministic UI actions do not pass through intent compilation or recent-task lookup."""
        from infrastructure.trip_repository import DraftActionConflict
        from core.execution_scope import ExecutionRevoked
        data=event.draft_action or {}
        if (event.platform!='web' or set(data)!={'id','version','action','attempt_event'}
                or data['action'] not in {'revalidate','discard'} or type(data['version']) is not int):
            raise DraftActionConflict('草案操作格式无效。')
        outcome=self.store.trips.action_outcome(event,data)
        if outcome and outcome['status'] in {'completed','failed','unknown','cancelled'}:
            self.store.diagnostics.record('draft_action',{'event_key':event.event_key,'status':'reused','name':data['action'],
                'operation_key':data['attempt_event'],'target_id':data['id'],'version':data['version']})
            if outcome['status']=='completed' and data['attempt_event']!=event.event_key and data['action']=='revalidate':
                return '这份草案已重新核对，本次没有重复生成或查询。请查看当前草案卡片。'
            return outcome['reply']
        if data['attempt_event']!=event.event_key:
            self.store.diagnostics.record('draft_action',{'event_key':event.event_key,'status':'reused','phase':'pending','name':data['action'],
                'operation_key':data['attempt_event'],'target_id':data['id'],'version':data['version']})
            return '这份草案的操作已在处理中，本次没有重复核对或产生新的查询。'
        key='draft-action:'+data['attempt_event']
        self.store.delivery.save_event_tool_result(key,event.event_key,'draft_action',{},json.dumps({
            'status':'running','target_id':data['id'],'target_version':data['version'],'action':data['action']}))
        try:
            with self.store._connect() as db:
                state=self.store.trips.draft_state(db,event,data['id'],now=self.clock())
            if state['task'].version!=data['version'] or not state['view']['can_'+data['action']]:
                raise DraftActionConflict('草案已保存、放弃或被更新，请查看最新草案。',state['view']['latest'])
            self.store.diagnostics.record('draft_action',{'event_key':event.event_key,'status':'started',
                'name':data['action'],'operation_key':data['attempt_event'],'target_id':data['id'],'version':data['version']})
            if data['action']=='discard':
                task=state['task'];slots=deepcopy(task.slots);slots['draft_discarded']=True
                update=self._task_update(task,event,slots,'cancelled')
                reply=self.store.trips.commit(event,claim,update,'已放弃这份草案。已保存行程、提醒和长期偏好未改变。',
                    now=self.clock(),draft_action=data,draft_guard=(task.task_id,task.version))
                with self.store._connect() as db:
                    cancelled=db.execute("SELECT event_key,json_extract(payload_json,'$.draft_action.attempt_event') AS attempt_event FROM inbox_jobs WHERE platform=? AND scope_id=? AND owner_id=? AND status='cancelled' AND json_extract(payload_json,'$.draft_action.id')=? AND json_extract(payload_json,'$.draft_action.version')=?",
                        (event.platform,event.scope_id,event.sender_id,data['id'],data['version'])).fetchall()
                for row in cancelled:self.store.diagnostics.record('draft_action',{'event_key':row['event_key'],'status':'cancelled',
                    'name':'revalidate','operation_key':row['attempt_event'],'target_id':data['id'],'version':data['version']})
            else:reply=self._revalidate_draft(event,claim,state,data)
            self.store.diagnostics.record('draft_action',{'event_key':event.event_key,'status':'completed','name':data['action'],
                'operation_key':data['attempt_event'],'target_id':data['id'],'version':data['version']})
            return reply
        except ExecutionRevoked:raise
        except Exception as error:
            phase=getattr(error,'failure_stage','target_check')
            self.store.diagnostics.record('draft_action',{'event_key':event.event_key,'status':'failed','phase':phase,
                'name':data['action'],'operation_key':data['attempt_event'],'target_id':data['id'],'version':data['version'],
                'error_type':type(error).__name__,'error_code':getattr(error,'code','')})
            with self.store._connect() as db:
                fresh=self.store.trips.draft_state(db,event,data['id'],now=self.clock())
                has_ledger=db.execute("SELECT 1 FROM sqlite_master WHERE name='model_usage_ledger'").fetchone()
                uncertain=has_ledger and db.execute('SELECT 1 FROM model_usage_ledger WHERE event_key=? AND result_received=0',(event.event_key,)).fetchone()
            if fresh['view']['state']=='discarded':reply='这份草案已放弃，本次核对结果未发布。已保存行程未改变。'
            else:
                labels={'target_check':'目标版本检查','base_merge':'基础版本对比','candidate_validation':'候选行程校验',
                    'route_review':'路线资料核对','transport_review':'城际交通核对','preview':'新预览提交'}
                detail=str(error) if isinstance(error,(TripPlanError,TaskConflict)) else '当前资料或服务暂时无法完成核对。'
                reply=f"重新核对在{labels.get(phase,'草案处理')}阶段未完成：{detail}\n原草案的酒店、餐饮、预算和其他修改仍保留。可以继续修改、再次核对或放弃这份草案。"
                if uncertain:reply+='\n上次模型提交结果或用量未确认，可能已产生费用；再次点击会作为独立尝试，原占用不会清零。'
            return self.store.trips.finish_draft_action(event,claim,data,'unknown' if uncertain else 'failed',reply)

    def _revalidate_draft(self,event,claim,state,data):
        from tools.trip.constraints import rebase_candidate
        from tools.trip.planner import fresh_route
        from tools.research.sources import planning_policy
        stage='base_merge';task=state['task'];slots=deepcopy(task.slots)
        candidate=deepcopy(slots['candidate_trip']);previous=deepcopy(candidate);current=state['current']
        try:
            if current and current['status']!='active':raise TripPlanError('原正式行程已取消，请明确是否另建行程。')
            if current and current['version']!=slots.get('expected_trip_version'):
                with self.store._connect() as db:
                    row=db.execute('SELECT snapshot_json FROM trip_versions WHERE trip_id=? AND version=?',
                        (current['trip_id'],slots.get('expected_trip_version'))).fetchone()
                if not row:raise TripPlanError('缺少草案的基础版本，不能只更新版本号后覆盖当前正式版。')
                candidate,conflicts=rebase_candidate(json.loads(row[0]),candidate,current)
                if conflicts:raise TripPlanError('草案与当前正式版同时修改了相同项目，请明确最终预算、活动或交通选择；未自动覆盖任何版本。\n'
                    +render_diff(trip_diff(current,slots['candidate_trip'])),code='rebase_conflict',path=conflicts[0])
            spec,plan,places=candidate['spec'],candidate['plan'],candidate['sources']
            if slots.get('preference_update') and slots['preference_update']['version']!=self.store.preferences.snapshot(event)['version']:
                raise TripPlanError('长期偏好已在别处变化，请先明确这份草案中哪些要求仍要长期保存。')
            stage='candidate_validation'
            candidate=self.planner.reconcile_candidate(candidate,previous)
            spec,plan,places=candidate['spec'],candidate['plan'],candidate['sources']
            policy=planning_policy(spec,event.content)
            restricted=not policy['map_allowed']
            reviewed=[];pending=[]
            stage='transport_review'
            traffic=plan.get('intercity',{})
            stale=[]
            for direction in ('outbound','inbound'):
                leg=traffic.get(direction,{})
                if leg.get('selected') and leg.get('queried_at'):
                    age=(self.clock()-datetime.fromisoformat(leg['queried_at'])).total_seconds()
                    if age>300:stale.append(direction)
                elif leg.get('candidates') and leg.get('status')=='completed':
                    stale.append(direction)
            if stale and policy['rail_allowed'] and not spec.get('date_is_tentative') and self.rail_executor:
                keep={k:v for k,v in traffic.items() if k not in stale}
                updated=self._intercity(event,spec,keep,previous_plan=plan)
                if any(updated.get(d,{}).get('status') in {'failed','unavailable','incomplete','unknown'} for d in stale):
                    raise TripPlanError('过期车次资料未能更新，原草案和原车次回执保留。')
                self.planner.connect_transport(plan,spec,places,updated,traffic)
                plan['intercity']=updated;reviewed.append('过期或尚未选定的城际候选')
                if updated.get('completeness') in {'partial','conflict'}:
                    pending.extend(issue['message'] for issue in updated.get('issues',[]))
                    if not updated.get('issues'):pending.append('部分城际车次或接驳尚未完成核验')
            elif stale:pending.append('旧车次报价未重新查询，仍是保存时的参考，不能视为当前余票')
            stage='route_review'
            def routes(value):
                if isinstance(value,dict):
                    if {'from_poi','to_poi','mode'}<=value.keys():yield value
                    else:
                        for item in value.values():yield from routes(item)
                elif isinstance(value,list):
                    for item in value:yield from routes(item)
            entries=[(route,key in {'days','intercity'} or route.get('required') is True)
                     for key,value in plan.items() for route in routes(value)]
            entries.sort(key=lambda entry:not entry[1])
            budget={'remaining':12};cache={};unresolved=[];updated=0
            for route,critical in entries:
                stale=not fresh_route(route,self.clock()) or bool(route.get('error') and not route.get('status'))
                if stale and not restricted and self.planner.amap:
                    key=(route['from_poi'],route['to_poi'],route['mode'])
                    if key not in cache:
                        cache[key]=self.planner.route_evidence(key[0],key[1],places,spec,key[2],budget=budget,now=self.clock())
                    result=cache[key]
                    # A failed refresh must not keep the old successful duration or instructions.
                    for field in ('duration_seconds','distance_meters','instructions','origin','destination','city',
                                  'city_source','place_resolution','error','error_code','status','date_recheck_required','fallback_from'):
                        route.pop(field,None)
                    route.update(deepcopy(result))
                    updated+=not bool(route.get('error'))
                if route.get('error'):
                    segment=places[route['from_poi']]['name']+' → '+places[route['to_poi']]['name']
                    detail=segment+'（'+MODES.get(route['mode'],route['mode'])+'）：'+route['error']
                    if critical and not restricted and self.planner.amap:
                        raise TripPlanError('关键交通段仍无法核验：'+detail+'；原草案保留。',code='required_route_unavailable',path='plan.routes')
                    unresolved.append(detail)
                elif stale and (restricted or not self.planner.amap):
                    unresolved.append('按来源限制沿用已保存路线，未声称重新核验' if restricted else '地图服务未配置，旧路线仅供参考')
            if updated:reviewed.append('市内路段')
            if unresolved:
                unresolved=list(dict.fromkeys(unresolved))
                pending.extend(unresolved[:3])
                if len(unresolved)>3:pending.append(f'另有{len(unresolved)-3}段辅助路线待确认，未将其当作已核验路线')
            self.store.diagnostics.record('planning',{'event_key':event.event_key,'name':'route_review',
                'status':'partial' if unresolved else 'completed','covered_count':updated,
                'remaining_count':len(unresolved),'attempts':12-budget['remaining']})
            candidate=self.planner.reconcile_candidate(candidate,deepcopy(candidate))
            slots.pop('semantic_task',None)
            slots['revalidation']={'source_draft_id':task.task_id,'source_version':task.version,'reviewed':reviewed,'pending':pending,
                                   'status':'partial' if pending else 'completed'}
            if candidate.get('answer'):candidate['answer']['change_summary']='原草案内容已保留，已重新校验并生成新的确认。'
            stage='preview'
            notice=('已生成带待确认项的新预览，未保存的要求与候选内容已继承。' if pending else '已重新核对指定草案，未保存的要求与候选内容已继承。')+('本次更新：'+'、'.join(reviewed)+'。' if reviewed else '本次未触发完整双路重规划。')
            if pending:notice+='仍待确认：'+'；'.join(pending)+'。'
            return self._preview_candidate(event,claim,candidate,current=current,slots=slots,prefix=notice+'\n',
                expected_draft=(candidate['trip_id'],task.task_id,task.version),answer_base=previous,
                write_answer=False,draft_action=data,draft_guard=(task.task_id,task.version,current['version'] if current else 0))
        except Exception as error:
            error.failure_stage=stage
            raise

    def _intercity(self,event,spec,previous=None,refresh=False,allowed=True,previous_plan=None):
        from core.tool_result import AgentToolContext, ToolResult, TRAIN_TICKETS_TOOL, DEFAULT_CATALOG
        from core.execution_scope import ensure_execution_active
        from core.time_input import BEIJING
        from core.rail_contract import seat_matches, select_seat
        if 'intercity_request' not in spec:return None
        spec.pop('intercity_windows',None)
        spec.pop('transport_days',None)
        previous=previous or {}
        declared=spec['intercity_request']
        if (allowed and declared.get('origin_scope')=='address' and declared.get('scope')!='none'
                and any(declared.get(d,{}).get('mode','rail')=='rail' for d in ('outbound','inbound'))):
            address=declared.get('origin','')
            if self.planner.amap and address and declared.get('origin_resolution',{}).get('binding')!=address:
                try:
                    from infrastructure.place_resolution import resolve_route_location
                    location=resolve_route_location(self.planner.amap,address,purpose='transit')
                    if location.city:declared['origin_resolution']={'binding':address,'city':location.city,'location':location.coordinates}
                except Exception as error:
                    from core.execution_scope import ExecutionRevoked
                    if isinstance(error,ExecutionRevoked):raise
        requests=self.planner.intercity_legs(spec,event.occurred_at or self.clock())
        activity_counts=({d['day_index']:len(d['activities']) for d in previous_plan['days']}
                         if previous_plan and self.planner.calendar_matches(spec,previous_plan) else None)
        result={'schema_version':1,'assumptions':{'arrival_buffer_minutes':30,'departure_buffer_minutes':60,
            'unknown_transfer_minutes':60},'limitations':[],'completeness':'partial','station_transfers':{}}
        result['day_count_basis']=spec['intercity_request'].get('day_count_basis','travel_included')
        result['basis_source']='user' if spec['intercity_request'].get('day_count_basis') else 'default'
        if declared.get('origin_scope')=='address':result['origin_address']=declared.get('origin','')
        context=AgentToolContext(event.platform,event.storage_scope_id,event.sender_id,event.event_key,
            allowed_actions=frozenset({TRAIN_TICKETS_TOOL}),catalog_version=self.store.delivery.freeze_tool_catalog(event.event_key,DEFAULT_CATALOG)
                if self.store.delivery.get_event_status(event.event_key) is not None else DEFAULT_CATALOG,event=event,
            execution_data={'reference_time':(event.occurred_at or self.clock()).isoformat()})
        for direction,request in requests.items():
            ensure_execution_active()
            args={key:request[key] for key in ('origin','destination','origin_scope','destination_scope','travel_date','departure_window','train_preference')}
            args.update({key:request[key] for key in ('seat_class','seat_requirement','price_preference') if key in request})
            if request['train_preference_source']=='default':args.pop('train_preference')
            old=previous.get(direction,{})
            part=spec['intercity_request'].get(direction,{})
            preferred=part.get('preferred_train','')
            if not allowed and request['mode']=='rail':
                result[direction]={'request':request,'status':'blocked_source','candidates':[]}
                result['limitations'].append('本次来源限制下不采用铁路查询快照；交通可行性尚未完整核验。')
                continue
            old_request={k:v for k,v in old.get('request',{}).items() if k!='calendar_binding' and (k!='arrival_window' or v)}
            current_request={k:v for k,v in request.items() if k!='calendar_binding' and (k!='arrival_window' or v)}
            if (old_request==current_request and not refresh and
                    not (old.get('candidate_truncated') and not old.get('selected'))):
                result[direction]=deepcopy(old);result[direction]['locked']=True
                self.store.diagnostics.record('planning',{'event_key':event.event_key,'name':'intercity_'+direction,
                    'status':'checkpoint_reused','candidate_count':len(old.get('candidates',[]))})
                continue
            leg={'request':request,'candidates':[],'status':'needs_input','missing_fields':[]}
            result[direction]=leg
            missing=[key for key in ('origin','destination','travel_date') if not args[key]]
            if request.get('time_reference')=='ambiguous':
                missing.append('time_reference')
                result['limitations'].append(('返程' if direction=='inbound' else '去程')+'时间待确认：你说的时段是从目的地出发，还是到达出发地？')
            if request['mode'] in {'driving','flight'}:
                leg.update(status='mode_declared',provider='user_choice');continue
            if missing:
                leg['missing_fields']=missing
                if request.get('date_error'):result['limitations'].append(request['date_error'])
                continue
            moment=(event.occurred_at or self.clock()).astimezone(BEIJING)
            if request['mode']=='rail' and date.fromisoformat(args['travel_date'])<moment.date():
                leg['missing_fields']=['travel_date']
                result['limitations'].append(('去程' if direction=='outbound' else '返程')+'乘车日期已过去，请明确新日期；原草案和其他要求保留。')
                continue
            if request['mode']=='owned_ticket':
                departure=parse_reminder_time(part.get('ticket_departure',''),event.occurred_at or self.clock()).instant()
                arrival=parse_reminder_time(part.get('ticket_arrival',''),event.occurred_at or self.clock()).instant()
                if departure and arrival and arrival>departure:
                    leg.update(status='provided',provider='user_ticket',candidates=[{'train_code':part.get('ticket_code','已有车票'),
                        'origin_station':args['origin'],'destination_station':args['destination'],
                        'departure_at':departure.isoformat(timespec='minutes'),'arrival_at':arrival.isoformat(timespec='minutes'),
                        'duration_minutes':int((arrival-departure).total_seconds()/60),'seats':[]}])
                else:leg['missing_fields']=['ticket_departure','ticket_arrival']
                continue
            if request['mode']!='rail':
                leg.update(status='mode_declared',provider='user_choice');continue
            if not allowed:
                leg['status']='blocked_source'
                result['limitations'].append('本次来源限制下未查询铁路，保留城市草案；交通可行性尚未完整核验。')
                continue
            if not self.rail_executor:
                leg.update(status='unavailable',error_code='not_available');continue
            try:
                from core.time_input import parse_time_window
                arrival_window=parse_time_window(request['arrival_window']) if request.get('arrival_window') else None
            except ValueError:
                leg['missing_fields']=['arrival_window'];continue
            self.store.diagnostics.record('planning',{'event_key':event.event_key,'name':'intercity_'+direction,'status':'started'})
            payload=None
            decision_fields={'preferred_train','seat_class','seat_requirement','price_preference','calendar_binding'}
            if (not refresh and old.get('queried_at') and
                    {k:v for k,v in old_request.items() if k not in decision_fields}==
                    {k:v for k,v in current_request.items() if k not in decision_fields} and
                    0<=(self.clock()-datetime.fromisoformat(old['queried_at'])).total_seconds()<=300):
                snapshots=self.store.trips.rail_snapshots(event,old['queried_at'])
                from tools.travel_query import rail_parameters
                expected,_=rail_parameters(args,event.occurred_at or self.clock())
                for snapshot in snapshots:
                    query=snapshot.get('request',{})
                    if (snapshot.get('all_trains') is not None and not snapshot.get('candidate_truncated') and expected and
                            all(query.get(k)==(list(v) if isinstance(v,tuple) else v) for k,v in expected.items()
                                if k not in {'seat_class','seat_requirement','price_preference','limit'})):
                        payload=deepcopy(snapshot);break
                if payload is None and old.get('candidates_complete'):
                    payload={'status':'ok','all_trains':deepcopy(old['candidates']),'provider':old.get('provider'),
                             'retrieved_at':old['queried_at'],'candidate_truncated':False}
            receipt=(ToolResult('completed',TRAIN_TICKETS_TOOL,payload=payload) if payload is not None
                     else self.rail_executor(TRAIN_TICKETS_TOOL,args,context))
            ensure_execution_active()
            payload=receipt.payload or {}
            deadline=None
            if request['arrival_before']:
                bound=parse_reminder_time(request['arrival_before'],self.clock(),day=request['travel_date'])
                if not bound.clock or bound.needs_period:
                    leg['missing_fields']=['arrival_before'];continue
                deadline=datetime.fromisoformat(bound.day+'T'+bound.clock).replace(tzinfo=BEIJING)
            def eligible(data):
                future=[row for row in data if datetime.fromisoformat(row['departure_at'])>moment]
                rows=[row for row in future if seat_matches(row,request)]
                if arrival_window:
                    rows=[row for row in rows if row['arrival_at'][:10]==request['travel_date'] and
                        arrival_window[0]<=int(row['arrival_at'][11:13])+int(row['arrival_at'][14:16])/60<arrival_window[1]]
                if deadline:rows=[row for row in rows if datetime.fromisoformat(row['arrival_at'])<deadline]
                if preferred:rows=[row for row in rows if row['train_code']==preferred]
                return rows,len(future)
            all_rows=payload.get('all_trains',payload.get('trains',[]))
            full_count=len(all_rows)
            rows,future_count=eligible(all_rows)
            # Shortlist by usable periods, then travel duration; full quote stays in U3 receipts/cards.
            def rank(row):
                trial={**result,direction:{**leg,'selected':row}}
                windows=self.planner.transport_windows(spec,trial)
                seat=select_seat(row,request)
                price=(seat or {}).get('price_cny')
                preferred_seat=bool(request.get('seat_class') and (seat or {}).get('seat_class')!=request['seat_class'])
                price_rank=(price is None,price if price is not None else 0) if request.get('price_preference')=='lower_price' else ()
                loses_activity=any(len(windows.get(str(day),{}).get('allowed_periods',[]))<count for day,count in (activity_counts or {}).items())
                return (seat is None,preferred_seat,loses_activity,*price_rank,-sum(len(w['allowed_periods']) for w in windows.values()),
                        row['duration_minutes'],row['departure_at'],row['train_code'],row['origin_station'],row['destination_station'])
            if direction=='inbound' and payload.get('candidate_truncated') and receipt.status=='completed':
                from tools.trip.planner import ACTIVITY_PERIODS
                from core.time_input import format_time_window
                # One bounded supplement, derived from the destination stay and
                # existing activity periods. Never treat an early-page sample as all trains.
                lower=0
                city=spec.get('city_windows',{}).get('departure')
                if city and (date.fromisoformat(spec['start_date'])+timedelta(days=city['day_index']-1)).isoformat()==request['travel_date']:
                    lower=parse_time_window(city['time_text'])[0]
                for day in (previous_plan or {}).get('days',[]):
                    if day.get('date')!=request['travel_date'] or not day['activities']:continue
                    windows=self.planner.transport_windows(spec,result)
                    earliest=datetime.fromisoformat(windows[str(day['day_index'])]['earliest'])
                    start_minutes=earliest.hour*60+earliest.minute
                    finish=max(max(start_minutes,ACTIVITY_PERIODS[a['period']][0])+90 for a in day['activities'])
                    lower=max(lower,(finish+result['assumptions']['unknown_transfer_minutes']+result['assumptions']['departure_buffer_minutes'])/60)
                window=parse_time_window(args.get('departure_window') or '全天')
                later=bool(rows) and max(int(row['departure_at'][11:13])+int(row['departure_at'][14:16])/60 for row in rows)<lower
                if window[0]<lower<window[1] and (not rows or later or activity_counts and all(rank(row)[2] for row in rows)):
                    query={**args,'departure_window':format_time_window((lower,window[1]))}
                    supplement=self.rail_executor(TRAIN_TICKETS_TOOL,query,context)
                    ensure_execution_active()
                    leg['query_windows']=[args.get('departure_window') or '全天',query['departure_window']]
                    if supplement.status=='completed':
                        added=(supplement.payload or {}).get('all_trains',(supplement.payload or {}).get('trains',[]))
                        quoted=[{**row,'queried_at':stamp} for batch,stamp in (
                            (all_rows,payload.get('retrieved_at')),(added,(supplement.payload or {}).get('retrieved_at'))) for row in batch]
                        merged={(row['train_code'],row['departure_at'],row['origin_station'],row['destination_station']):row for row in quoted}
                        all_rows=list(merged.values());full_count=len(all_rows);rows,future_count=eligible(all_rows)
                    else:
                        result['limitations'].append('返程补充时段查询未完成，保留已取得候选，覆盖范围仍待核实。')
            complete=not payload.get('candidate_truncated') and len(rows)==full_count and full_count<=6
            rows=sorted(rows,key=rank)[:6]
            leg.update(status='completed' if receipt.status=='completed' and rows else payload.get('status') if receipt.status=='completed' else receipt.status,
                candidates=rows,provider=payload.get('provider','12306-mcp'),queried_at=payload.get('retrieved_at'),
                candidate_truncated=payload.get('candidate_truncated',False),error_code=receipt.error_code,
                candidates_complete=complete,missing_fields=list(receipt.missing_fields))
            if receipt.status=='completed' and not rows:leg['status']='empty' if not payload.get('candidate_truncated') else 'incomplete'
            if receipt.status=='completed' and not rows and request.get('seat_class') and full_count:
                leg['status']='incomplete' if payload.get('candidate_truncated') else 'seat_unavailable'
            if receipt.status=='completed' and full_count and not future_count:
                leg.update(status='needs_input',missing_fields=['departure_window'])
                result['limitations'].append('本次返回车次均已出发，请调整乘车日期或时段；原需求未清除。')
            self.store.diagnostics.record('planning',{'event_key':event.event_key,'name':'intercity_'+direction,
                'status':leg['status'],'candidate_count':len(rows),'error_code':receipt.error_code})
        if not requests:
            result.update(completeness='not_required',limitations=['本次未要求铁路往返，按已声明的出行范围安排。'])
        else:
            self.planner.choose_transport(spec,result,activity_counts=activity_counts)
        inbound=requests.get('inbound',{})
        if (inbound.get('departure_window') and not inbound.get('arrival_window') and not inbound.get('arrival_before')
                and not declared.get('inbound',{}).get('departure_window')):
            result['limitations'].append('返程时段按从目的地启程理解，未将其视为回到出发地的时间；若有到达限制请明确说明。')
        for direction in requests:
            result[direction]['request']['calendar_binding']=[spec.get('start_date'),spec.get('day_count')]
        if any(result[d].get('candidate_truncated') for d in requests):
            result['limitations'].append('供应商候选达到上限，结果不完整；仅在已返回候选中选用，不代表全部班次。')
        if any(r['origin_scope']=='city' or r['destination_scope']=='city' for r in requests.values()):
            result['limitations'].append('城市代码查询的覆盖完整性未独立核验；以列出的实际车站为准。')
        costs=[]
        for direction in requests:
            selected=result[direction].get('selected',{})
            seat=result[direction].get('selected_seat')
            if seat and seat.get('price_cny') is not None:costs.append(seat['price_cny'])
        result['ticket_cost']={'basis':'per_person','known_cny':sum(costs),'total_cny':sum(costs) if len(costs)==len(requests) and costs else None,
                               'unknown_segments':len(requests)-len(costs),'currency':'CNY'}
        spec['intercity_context']={key:{k:v for k,v in result[key].items() if k in {'selected','selected_seat','status','missing_fields'}} for key in requests}
        spec['intercity_context']['ticket_cost']=result['ticket_cost']
        return result

    @staticmethod
    def format_intercity(result,direction):
        leg=result.get(direction)
        if not leg:return []
        title='去程' if direction=='outbound' else '返程'
        selected=leg.get('selected')
        if selected:
            text=f"{title}交通：{selected['train_code']}｜{selected['origin_station']} {selected['departure_at'][:16].replace('T',' ')} → {selected['destination_station']} {selected['arrival_at'][:16].replace('T',' ')}"
            lines=[text]
            seat=leg.get('selected_seat')
            if seat and seat.get('price_cny') is not None:
                lines.append(f"参考席别：{seat['name']}，约 {seat['price_cny']:g} CNY／人／单程；余票可能变化。")
            elif seat:lines.append(f"参考席别：{seat['name']}；票价尚未确认。")
            elif 'selected_seat' not in leg and leg.get('provider')!='user_ticket':lines.append('历史快照未保存明确选定席别，不能据此确认席别与报价。')
            elif leg.get('status') in {'sold_out','stock_unknown'}:lines.append('当前没有已确认可直接购买的席位，保留时间参考；请刷新或调整。')
            if leg.get('provider')=='user_ticket':lines.append('按你提供的已有车票安排，未重新查询或核实出票状态。')
            if leg.get('queried_at'):lines.append('车票数据时间：'+leg['queried_at']+'；推荐不代表已购票或锁票。')
            if leg.get('queried_at') and (datetime.now(timezone.utc)-datetime.fromisoformat(leg['queried_at'])).total_seconds()>300:
                lines.append('已保存快照超过5分钟，余票需刷新；确认行程不会自动重查车次。')
        else:
            labels={'needs_input':'待补充','failed':'查询失败','unavailable':'未取得铁路数据','empty':'当前范围未返回匹配车次','incomplete':'结果不完整','seat_unavailable':'本次范围内目标席别暂无已确认可售票','mode_declared':'沿用你指定的方式，未查询铁路'}
            lines=[title+'交通：'+labels.get(leg.get('status'),'待核对')]
        request=leg.get('request',{})
        if request.get('mode_source')=='default':lines.append('未指定城际方式，本次先按高铁优先安排。')
        if request.get('date_source')=='default_end_date':lines.append('返程日期按已确认起始日与总天数推导，可在草稿中调整。')
        transfer=result.get('station_transfers',{}).get(direction)
        if transfer:lines.append('站点接驳：'+(f"约{transfer['duration_minutes']}分钟（查询时路网参考）" if transfer.get('status')=='known'
                    else '尚未核实；当前窗口暂按60分钟规划假设，需确认。'))
        lines.extend(issue['message'] for issue in result.get('issues',[]) if issue.get('direction')==direction)
        return lines

    @staticmethod
    def local_decision(spec,reason):
        prior=spec.get('planning_decision',{})
        spec['planning_decision']={'status':'skipped','reason':reason,
            'reference_event_id':prior.get('source_event_id') or prior.get('reference_event_id','')}
        spec['research_note']=reason

    def _generate(self,event,spec,previous=None,request='',previous_sources=None):
        from tools.research.sources import planning_policy
        from tools.trip.alternatives import Alternatives
        research=self.research_service
        configured=research is not None and research.wendao is not None and research.wendao.configured
        policy=planning_policy(spec,event.content,automatic=configured)
        transport_edit=spec.pop('_transport_edit',False)
        spec.pop('_locked_activity_days',None)
        if previous is None and previous_sources:
            places=deepcopy(previous_sources)
            return places,self.planner.plan(spec,places,edit_request=request)
        if policy['offline']:
            raise TripPlanError('当前要求不联网，未查询外部地点，也未修改行程。')
        if configured and policy['only']:
            self._prepare_research(event,spec)
            raise TripPlanError('已保留问道参考资料；仅用问道时不调用地图，也不保存为已绑定的正式行程。')
        if not policy['map_allowed']:raise TripPlanError('当前来源范围未允许地图核验，需求和已有参考资料保留，尚未生成已绑定草案。',code='source_restricted',path='provider_policy')
        old=(previous or {}).get('intercity',{})
        traffic=self._intercity(event,spec,old,refresh=event.content.strip()=='刷新行程交通',
            allowed=policy['rail_allowed'],
            previous_plan=previous if transport_edit else None)
        if transport_edit and previous is not None and previous_sources is not None and self.planner.calendar_matches(spec,previous):
            places=deepcopy(previous_sources);plan=deepcopy(previous)
            if traffic:
                self.planner.connect_transport(plan,spec,places,traffic,old);plan['intercity']=traffic
            self.local_decision(spec,'本轮仅调整交通，未重新生成两版草案；沿用已保存的景点资料与行程内容。')
            self.store.diagnostics.record('planning',{'event_key':event.event_key,'status':'transport_only'})
            return places,plan
        if transport_edit and previous and len(previous['days'])==spec.get('day_count'):
            old_windows=old.get('availability_windows',{})
            windows=(traffic or {}).get('availability_windows',{})
            spec['_locked_activity_days']={str(day['day_index']):deepcopy(day['activities']) for day in previous['days']
                if old_windows.get(str(day['day_index']))==windows.get(str(day['day_index']))}
        if configured and policy['requested'] and not policy['only'] and not spec.get('source_plan_artifact') and not re.search(r'第[一二两三四五六七八九十\d]+天',request):
            places,plan=Alternatives(self.store,self.planner,research).generate(event,spec,previous,request)
            if traffic:
                self.planner.connect_transport(plan,spec,places,traffic,old);plan['intercity']=traffic
                self.store.diagnostics.record('planning',{'event_key':event.event_key,'name':'transport_validation',
                    'status':'completed' if traffic['completeness'] in {'reference_ready','not_required'} else traffic['completeness'],
                    'phase':traffic.get('repair','initial'),'covered_count':sum(bool(traffic.get(d,{}).get('selected')) for d in ('outbound','inbound'))})
            spec.pop('_locked_activity_days',None)
            return places,plan
        if policy.get('source_restricted'):
            from tools.trip.alternatives import RESEARCH_FIELDS
            for key in RESEARCH_FIELDS-{'source_context'}:spec.pop(key,None)
        places=self.planner.collect_places(spec)
        plan=self.planner.plan(spec,places,previous,request)
        if traffic:
            self.planner.connect_transport(plan,spec,places,traffic,old);plan['intercity']=traffic
            self.store.diagnostics.record('planning',{'event_key':event.event_key,'name':'transport_validation',
                'status':'completed' if traffic['completeness'] in {'reference_ready','not_required'} else traffic['completeness'],
                'phase':traffic.get('repair','initial'),'covered_count':sum(bool(traffic.get(d,{}).get('selected')) for d in ('outbound','inbound'))})
        spec.pop('_locked_activity_days',None)
        return places,plan

    @staticmethod
    def read_draft(store, event, trip):
        rows = store.trips.draft_history(event, trip)
        if not rows or rows[0].status != 'collecting':
            return None
        task = rows[0]
        candidate = deepcopy(task.slots.get('candidate_trip'))
        if not candidate or task.slots.get('trip_already_saved'):
            return None
        original_spec = deepcopy(candidate['spec'])
        # Older constraint updates started over from the saved trip. Recover their
        # validated deltas until a cancelled/completed draft or a different base.
        if not task.slots.get('draft_context_complete') and task.slots.get('expected_trip_version') == trip['version']:
            chain = []
            for row in rows:
                slots = row.slots
                old = slots.get('candidate_trip', {})
                if (row.status not in {'collecting','superseded'} or slots.get('expected_trip_version') != trip['version']
                        or slots.get('trip_already_saved') or not old.get('spec',{}).get('constraint_overrides')):
                    break
                chain.append(old['spec']['constraint_overrides'])
                if slots.get('draft_context_complete'):
                    break
            for changes in reversed(chain):
                # Values and budget units may have been quoted in separate user turns.
                quotes = []
                for change in changes.values():
                    quotes.extend([change['source'],change.get('unit_text','')])
                    value = change.get('value', '')
                    quotes.extend(value if isinstance(value,list) else [value] if isinstance(value,str) else [])
                sources = '\n'.join(quotes)
                candidate['spec'], _ = apply_constraints(candidate['spec'], changes, sources)
        return {'task':task,'candidate':candidate,'recovered':candidate['spec'] != original_spec}

    def draft(self, event, trip):
        return self.read_draft(self.store, event, trip)

    def _prepare_research(self, event, spec):
        if self.research_service is None or spec.get('research_context'):
            return
        from tools.research.sources import planning_policy
        policy=planning_policy(spec,event.content)
        if policy['offline'] or policy['official_only'] or policy['other_source_only']:return
        prepared = self.research_service.prepare_for_planning(event, spec)
        if prepared.note:
            spec['research_note'] = prepared.note
        reference = prepared.source
        if isinstance(reference, ResearchSource):
            spec['source_context'] = reference
            spec['research_context'] = deepcopy(reference.data)

    @staticmethod
    def format_trip(trip, *, preview=False, full=False, sections=None,reply_mode='full'):
        from tools.trip.presentation import presentation_trip, cultural_note, activity_material_state
        trip=presentation_trip(trip)
        if trip.get('answer'):
            from tools.trip.presentation import render_answer
            text,parts=render_answer(trip,TripTool.format_intercity,preview=preview,mode=reply_mode)
            if sections is not None:sections.extend(parts)
            return text
        spec = trip['spec']
        date_note = reference_date_note(spec).replace('日期暂定：', '**日期暂定：** ', 1)
        lines = [f"{'行程修改预览（尚未保存）' if preview else '旅行行程'}：{trip['title']}（{trip['trip_id']}，版本 {trip.get('version', 1)}）"]
        if date_note:
            lines.append(date_note)
        if spec.get('research_note'):
            lines.append(spec['research_note'])
        if spec.get('preferences'):
            lines.append(('**规划约束（含所选资料）：** ' if spec.get('document_ids') else '**你的要求：** ') + '；'.join(spec['preferences']))
        if spec.get('budget_text') and not spec.get('budgets'):
            lines.append('**预算目标：** ' + spec['budget_text'] + '；尚未核验实时房价、门票和交通费用。')
        if spec.get('accommodation_text'):
            lines.append('**住宿要求：** ' + spec['accommodation_text'] + '；舒适度需另行核实。')
        if spec.get('dining_text'):
            lines.append('**餐饮要求：** ' + spec['dining_text'] + '；口味、配料和营业时段需另行核实。')
        for key, label in (('hotel_preferences','住宿筛选'), ('dining_preferences','餐饮筛选')):
            preferences = spec.get(key, {})
            parts = []
            if preferences.get('preferred_brands'):
                parts.append('品牌 ' + '、'.join(preferences['preferred_brands']))
            if preferences.get('cuisines'):
                parts.append('菜系 ' + '、'.join(preferences['cuisines']))
            if preferences.get('minimum_rating') is not None:
                origin = '默认标准' if preferences.get('rating_origin') == 'default_high_quality' else '你的明确要求'
                parts.append(f"高德评分至少 {preferences['minimum_rating']:g}（{origin}）")
            if preferences.get('near_itinerary'):
                parts.append('结合行程位置')
            if parts:
                lines.append('**' + label + '：** ' + '；'.join(parts))
        from tools.trip.constraints import facts
        for scope,budget in spec.get('budgets',{}).items():
            if budget.get('value'):
                lines.append('**'+BUDGET_SCOPES[scope]+'预算目标：** '+budget['value']
                    +('；'+budget['unit_text'] if budget.get('unit_text') else '')+budget_default_note(budget)
                    +'；尚未核验实时价格及库存。')
        for row in facts(spec):
            if row['field'].startswith('city_windows.'):lines.append('**'+row['label']+'：** '+row['value'])
            if '.meals.' in row['field']:lines.append('**'+row['label']+'目标：** '+row['value']+'；尚未核验实时价格及库存。')
        if spec.get('applied_preferences'):
            from infrastructure.preference_repository import PreferenceRepository
            lines.append('**已采用你的偏好：** ' + PreferenceRepository.describe(spec['applied_preferences']) + '（本次明确要求优先）。')
        if spec.get('pace'):
            lines.append('本次节奏：' + PACE[spec['pace']])
        if spec.get('planning_window'):
            limit=spec['planning_window']
            lines.append(f"明确时间范围：{limit['start_text']}至{limit['end_text']}，覆盖{spec['day_count']}个自然日；首末日按可用时段安排。")
            if spec.get('requested_day_count') and not spec.get('duration_adjustment'):
                lines.append(f"原天数要求：{spec['requested_day_count']}天；游览与交通日期分别核验。")
        if spec.get('duration_adjustment'):lines.append(spec['duration_adjustment'])
        transport = spec.get('transport_preferences', {})
        if transport:
            lines.append('本次交通：' + '；'.join(label + '、'.join(MODES[v] for v in transport.get(key, []))
                for key, label in (('preferred','优先'),('discouraged','尽量减少'),('forbidden','不使用')) if transport.get(key)))
        for key, label in (('indoor_days', '室内安排'), ('outdoor_days', '室外安排')):
            if spec.get(key):
                lines.append(label + '：' + '、'.join(f'第{day}天' for day in spec[key]))
        if spec.get('document_ids'):
            lines.append('需求参考文档：' + '、'.join('#' + value for value in spec['document_ids']))
        if spec.get('media_ids'):
            lines.append('需求参考图片：' + '、'.join('#' + value for value in spec['media_ids']))
        traffic=trip['plan'].get('intercity',{})
        if traffic and traffic.get('completeness')!='not_required':
            missing=[]
            for direction,label in (('outbound','去程'),('inbound','返程')):
                for field in traffic.get(direction,{}).get('missing_fields',[]):
                    if not spec.get('intercity_request',{}).get('origin') and field in {'origin','destination'}:name='城际出发地'
                    elif field=='travel_date' and traffic.get('day_count_basis')!='destination_days':name='明确的旅行开始日期'
                    else:name=label+{'travel_date':'乘车日期','departure_window':'明确时段','ticket_departure':'已有票出发时间','ticket_arrival':'已有票到达时间','arrival_before':'最晚到达时间','preferred_train':'指定车次或刷新要求'}.get(field,field)
                    if name not in missing:missing.append(name)
            if missing:lines.append('城际交通待补：'+'、'.join(missing)+'。可继续补充，当前城市草案保留。')
            if spec.get('planning_window'):
                lines.append('交通受明确起止时间约束，具体班次和接驳可用时段仍需核对。')
            elif traffic.get('day_count_basis')=='destination_days':
                lines.append(f"目的地游览天数保留；当前含交通共 {spec['day_count']} 天，新增交通日以本预览确认为准。")
            else:lines.append(f"{'未明确天数口径，本方案先按' if traffic.get('basis_source')=='default' else '按你的要求，'} {spec['day_count']} 日内含往返核对。")
        outbound_start=len(lines)
        lines.extend(TripTool.format_intercity(traffic,'outbound'))
        outbound_end=len(lines)
        for day in trip['plan']['days']:
            if lines and lines[-1] != '':
                lines.append('')
            lines.append(f"第 {day['day_index']} 天" + (f" · {day['date']}" if day['date'] else '（日期待定）'))
            if not day['activities']:
                lines.append('- 交通日：预留给往返交通，不安排景点。' if day.get('kind')=='transport' else '- 自由休整：按你的修改，暂不安排固定景点。')
            for activity in day['activities']:
                place = trip['sources'][activity['poi_id']]
                lines.append(f"- {activity['period']}：{place['name']}｜{place.get('address', '地址待核对')}；{activity['reason']}")
                if activity.get('subactivities'):
                    lines.append('  园内参观：'+'、'.join(trip['sources'][a['poi_id']]['name'] for a in activity['subactivities']))
                description = activity.get('description', {})
                if description.get('intro'):
                    intro = description['intro']
                    lines.append('  ' + intro['text'])
                elif spec.get('activity_materials'):
                    lines.append('  ' + cultural_note(activity_material_state(spec,activity['poi_id'])))
                if description.get('why_visit'):
                    lines.append('  安排理由：' + description['why_visit'])
            for leg in day.get('legs', []):
                origin, destination = trip['sources'][leg['from_poi']]['name'], trip['sources'][leg['to_poi']]['name']
                mode = {'transit': '公共交通', 'walking': '步行', 'driving': '驾车'}[leg['mode']]
                if leg.get('error'):
                    lines.append(f'  {origin} → {destination}：{mode}段暂未核实，{leg["error"]}')
                else:
                    minutes = (leg['duration_seconds'] + 59) // 60
                    estimate = f'约 {minutes} 分钟' if minutes else '耗时尚未返回'
                    lines.append(f'  {origin} → {destination}：{mode}{estimate}（当前路网参考，出发当日需再核对）。')
            if spec.get('gentle'):
                lines.append('  建议在活动间安排休息，按实际体力减少步行。')
        def candidate_lines(recommendations, *, kind):
            rendered = []
            labels = {'matched':'满足已核验条件', 'partial':'部分条件待核实', 'fallback':'需要放宽条件的备选','conflict':'不符合当前筛选要求'}
            for item in recommendations:
                place = trip['sources'][item['poi_id']]
                rendered.append(f"- {place['name']}｜{place.get('address', '')}")
                facts = []
                if item.get('rating') is not None:
                    facts.append(f"高德评分 {item['rating']:g}")
                distance = item.get('distance', {})
                if kind == 'hotel' and distance.get('average_meters') is not None:
                    facts.append(f"距全程景点平均约 {distance['average_meters']/1000:.1f} 公里")
                    facts.append(f"最远约 {distance['maximum_meters']/1000:.1f} 公里")
                elif distance.get('average_meters') is not None:
                    facts.append(f"距本餐前后地点平均约 {distance['average_meters']/1000:.1f} 公里")
                price_info = item.get('price_info', {})
                if not price_info and item.get('reference_price') is not None:
                    price_info = ({'average_price': item['reference_price'], 'lowest_price': None}
                                  if kind == 'hotel' else {'per_capita': item['reference_price']})
                if kind == 'hotel':
                    if price_info.get('basis')=='per_room_night_cny':
                        if price_info.get('average_price') is not None:facts.append(f"参考房价约 {price_info['average_price']:g} 元/间/晚")
                        if price_info.get('lowest_price') is not None:facts.append(f"最低房价参考 {price_info['lowest_price']:g} 元/间/晚")
                    elif item.get('quote_offers'):
                        facts.append('指定日期房型参考报价见上方；实时价格及库存未核实')
                    else:
                        facts.append('房晚价格尚未核实')
                    if price_info.get('reference_cost') is not None:facts.append(f"参考金额 {price_info['reference_cost']:g}（{price_info.get('reference_cost_unit','人均消费')}），未用于房晚预算判断")
                    if price_info.get('unverified_lowest_price') is not None:facts.append('存在最低价线索，但尚不能用于房晚预算判断')
                elif price_info.get('per_capita') is not None:
                    facts.append(f"高德参考人均消费约 {price_info['per_capita']:g} 元/人")
                else:
                    facts.append('高德未返回参考人均消费')
                rendered.append('  ' + labels.get(item.get('status'), '候选') + ('；' + '；'.join(facts) if facts else ''))
                if item.get('reasons'):
                    rendered.append('  推荐依据：' + '；'.join(item['reasons'][:3]))
                if item.get('issues'):
                    rendered.append('  待确认：' + '；'.join(dict.fromkeys(item['issues'])))
                for route in item.get('routes', []) if kind == 'hotel' else []:
                    route_parts = []
                    for direction, label in (('outbound','去首个景点'), ('return','从末个景点返回')):
                        evidence = route.get(direction)
                        if evidence and not evidence.get('error') and evidence.get('duration_seconds'):
                            route_parts.append(f"{label}约 {(evidence['duration_seconds']+59)//60} 分钟")
                    if route_parts:
                        rendered.append(f"  第 {route['day_index']} 天：" + '；'.join(route_parts))
                if kind == 'dining' and isinstance(item.get('routes'), dict):
                    routes = item['routes']
                    if routes.get('extra_duration_seconds') is not None:
                        rendered.append(f"  相比前后地点直达，预计额外绕行约 {(routes['extra_duration_seconds']+59)//60} 分钟")
                    else:
                        route_parts = []
                        for key, label in (('from_previous','从上一地点'), ('to_next','前往下一地点')):
                            evidence = routes.get(key)
                            if evidence and not evidence.get('error') and evidence.get('duration_seconds'):
                                route_parts.append(f"{label}约 {(evidence['duration_seconds']+59)//60} 分钟")
                        if route_parts:
                            rendered.append('  行程位置：' + '；'.join(route_parts))
            return rendered

        if trip['plan'].get('hotel_quotes'):
            from tools.hotel_prices.tool import quote_text
            lines.append('\n'+quote_text(spec,trip['plan']['hotel_quotes'],places=trip['sources']))
        hotel_recommendations = trip['plan'].get('hotel_recommendations', [])
        if hotel_recommendations:
            if lines and lines[-1] != '':
                lines.append('')
            from tools.trip.presentation import hotel_candidate_groups
            if trip['plan'].get('hotel_review_note'):lines.append(trip['plan']['hotel_review_note'])
            for label,rows in hotel_candidate_groups(hotel_recommendations):
                lines.append(label+'：')
                lines.extend(candidate_lines(rows,kind='hotel'))
            lines.append('未核实实时价格和库存；当前地图接口不提供具体房型价格。')
        elif trip['plan'].get('hotel_ids'):
            if lines and lines[-1] != '':
                lines.append('')
            lines.append('住宿候选（地点资料，未核实实时价格和营业/库存）：')
            lines.extend(f"- {trip['sources'][poi_id]['name']}｜{trip['sources'][poi_id].get('address', '')}"
                         for poi_id in trip['plan']['hotel_ids'])
        elif spec.get('hotel_requested'):
            if lines and lines[-1] != '':
                lines.append('')
            lines.append('住宿候选：当前资料不足以确认匹配预算与品牌要求；没有用旧酒店或无关酒店冒充合格结果。')

        dining_options = trip['plan'].get('dining_options', [])
        restaurant_recommendations = trip['plan'].get('restaurant_recommendations', [])
        if dining_options or restaurant_recommendations:
            if lines and lines[-1] != '':
                lines.append('')
            lines.append('餐饮候选（参考消费不是实际账单，营业时段和配料需核实）：')
            if restaurant_recommendations:
                lines.extend(candidate_lines(restaurant_recommendations, kind='dining'))
            for option in dining_options:
                lines.append(f"第 {option['day_index']} 天 · {option['meal']}：")
                lines.extend(candidate_lines(option.get('candidates', []), kind='dining'))
                if option.get('note'):lines.append(option['note'])
        elif trip['plan'].get('restaurant_ids'):
            if lines and lines[-1] != '':
                lines.append('')
            lines.append('餐饮候选（参考消费不是实际账单，营业时段和配料需核实）：')
            lines.extend(f"- {trip['sources'][poi_id]['name']}｜{trip['sources'][poi_id].get('address', '')}"
                         for poi_id in trip['plan']['restaurant_ids'])
        elif spec.get('restaurant_requested'):
            if lines and lines[-1] != '':
                lines.append('')
            lines.append('餐饮候选：本次检索范围内未找到符合要求的餐饮地点；没有为凑数生成商家。')

        if lines and lines[-1] != '':
            lines.append('')
        inbound_start=len(lines)
        lines.extend(TripTool.format_intercity(traffic,'inbound'))
        inbound_end=len(lines)
        if traffic:
            cost=traffic.get('ticket_cost',{})
            if cost.get('total_cny') is not None:lines.append(f"本次推荐车次已知可售席位的单人参考票价合计约 {cost['total_cny']:g} CNY；未核算家庭、儿童或全程总预算。")
            elif cost.get('unknown_segments'):lines.append('部分票价未知，未按零元计入，也未确认总预算达标。')
            if spec.get('hotel_requested') and traffic.get('hotel_nights') is not None:lines.append(f"目的地住宿按到达/离开日期参考 {traffic['hotel_nights']} 晚；入住退房与夜车衔接仍需确认。")
            lines.extend(dict.fromkeys(traffic.get('limitations',[])))
        lines.extend(('地点依据：高德 POI 查询记录；开放时间、预约、价格和跨地点通行时间需另行核对。',
                      '可以说“第二天改为室内”“把行程改到10月2日开始”或“查看行程”。'))
        if spec.get('activity_materials') or spec.get('planning_evidence'):
            lines.append('当前按上午/下午/晚上参考安排；小时级营业窗口、预约场次与交通时间加总尚未校验。')
        if sections is not None:
            sections.extend([{'kind':'text','text':'\n'.join(lines[:outbound_start])},
                *([{'kind':'transport','direction':'outbound'}] if outbound_end>outbound_start else []),
                {'kind':'text','text':'\n'.join(lines[outbound_end:inbound_start])},
                *([{'kind':'transport','direction':'inbound'}] if inbound_end>inbound_start else []),
                {'kind':'text','text':'\n'.join(lines[inbound_end:])}])
        if not full and not preview and sections is None and len('\n'.join(lines)) > 6000:
            compact = [lines[0], date_note, '长行程简报；完整介绍和地址保存在行程详情，可按日查看。']
            if spec.get('preferences'):
                compact.append('**你的要求：** ' + '；'.join(spec['preferences']))
            for day in trip['plan']['days']:
                compact.append(f"\n第 {day['day_index']} 天 · {day.get('date') or '日期待定'}")
                compact.extend(f"- {a['period']}：{trip['sources'][a['poi_id']]['name']}；{a['reason']}" for a in day['activities'])
            compact.append('小时级时间与交通衔接尚未校验。')
            # Whole activity descriptions, fairly distributed across days.
            extras = {a['poi_id']: a.get('description', {}) for day in trip['plan']['days'] for a in day['activities']}
            for pid, description in extras.items():
                if description.get('intro'):
                    text = '\n' + trip['sources'][pid]['name'] + '：' + description['intro']['text']
                    if len('\n'.join(compact)) + len(text) + 1 <= 5900:
                        compact.append(text)
            return '\n'.join(compact)
        return '\n'.join(lines)

    @staticmethod
    def presentation(trip,reply,*,preview=False,reply_mode='full'):
        """Capture structural boundaries at formatting time; never parse Markdown fields."""
        sections=[]
        text=TripTool.format_trip(trip,preview=preview,sections=sections,reply_mode=reply_mode)
        if preview and text not in reply:
            text=text.replace('行程修改预览','行程创建预览',1)
            sections[0]['text']=sections[0]['text'].replace('行程修改预览','行程创建预览',1)
        before,found,after=reply.partition(text)
        if not found:return None
        snapshot={key:deepcopy(value) for key,value in trip.items() if not key.startswith('_')}
        return {'trip':snapshot,'sections':sections,'prefix':before,'suffix':after,'is_preview':preview}

    def _task_update(self, task, event, slots, status, missing=()):
        return TaskUpdate('itinerary', status, task.initial_request if task else event.content,
                          slots, missing, task.task_id if task else '', task.version if task else None)

    def view(self,event,trip,*,prefix=''):
        preview=bool(trip.get('_new_trip') or trip.get('_draft_applied'))
        if trip.get('_draft_applied'):trip={**trip,'version':trip['version']+1}
        reply=prefix+self.format_trip(trip,preview=preview)
        value=self.presentation(trip,reply,preview=preview)
        if value and self.store.delivery.get_event_status(event.event_key) is not None:
            key=trip.get('_draft_key')
            if key and key[0]:value['draft']={'task_id':key[0],'version':key[1]}
            value['content_sha256']=hashlib.sha256(reply.encode()).hexdigest()
            self.store.delivery.save_event_tool_result('trip-presentation:'+event.event_key,event.event_key,
                'trip_presentation',{},json.dumps(value,ensure_ascii=False))
        return reply

    def _question(self, task, event, claim, slots, missing, reply, *, trip=None, expected_trip=None,
                  expected_draft=None, expected_artifact=None, supersede_request='',draft_action=None,draft_guard=None):
        if task and task.task_type=='itinerary' and task.is_expired(self.clock()):
            trip_id=slots.get('trip_id') or task.slots.get('trip_id')
            expected_draft=expected_draft or (trip_id,task.task_id,task.version)
            slots={k:v for k,v in slots.items() if k not in {'budget_run_key','confirmation_preview'}}
            task=None
        if 'confirmation' in missing and slots.get('candidate_trip') and expected_trip:
            saved = self.store.trips.get(event,expected_trip[0])
            if saved:
                slots['change_diff'] = trip_diff(saved,slots['candidate_trip'])
                if not self.answer_writer:reply = render_diff(slots['change_diff'])+'\n\n'+reply
        if 'confirmation' in missing and len(reply) > 7500:
            raise TripPlanError('本次行程与提醒变更太长，无法完整展示供核对。请减少本次修改范围后重试，尚未应用本次变更。')
        return self.store.trips.commit(event, claim, self._task_update(task, event, slots, 'collecting', missing),
                                       reply, trip=trip, expected_trip=expected_trip, expected_draft=expected_draft,
                                       expected_artifact=expected_artifact,supersede_request=supersede_request, now=self.clock(),
                                       draft_action=draft_action,draft_guard=draft_guard,
                                       presentation=self.presentation(slots['candidate_trip'],reply,preview=True,
                                           reply_mode=slots.get('reply_mode','full'))
                                           if 'confirmation' in missing and slots.get('candidate_trip') else None)

    def edit_field(self, event, claim):
        from tools.trip.constraints import edit_text, edit_constraints
        data=event.trip_edit
        if edit_text(data['field'],data['value']) != event.content:
            return '字段内容不一致，请刷新后重试。'
        current=self.store.trips.get(event,data['trip_id'])
        if not current or current['version'] != data['version']:
            return '行程已变化或不属于本会话，请刷新后重新修改。'
        draft=self.draft(event,current)
        if (data.get('draft_id'),data.get('draft_version')) != ((draft['task'].task_id,draft['task'].version) if draft else (None,None)):
            return '草稿已变化，请刷新后重新修改。'
        changes=edit_constraints(data['field'], data['value'])
        return self.update_constraints(event,claim,current['trip_id'],current['version'],changes,
            expected_draft=(draft['task'].task_id if draft else '',draft['task'].version if draft else 0))

    def _edit_candidate(self,event,base,changes,source_text,*,allow_local=True):
        spec, summary = (apply_constraints(base['spec'], changes, source_text or event.content) if changes
                         else (deepcopy(base['spec']), ['根据当前草稿重新生成预览']))
        candidate = deepcopy(base)
        candidate['answer_meal_scopes']=changes.get('dining',{}).get('scopes',[])
        record_sources(spec,changes,event)
        candidate.update(spec=spec)
        from tools.trip.constraints import effective_changes
        if changes:
            changes=effective_changes(base['spec'],changes,source_text or event.content)
            if not changes:
                candidate['planning_mode']='unchanged'
                return candidate,summary
        spec['_transport_edit']=transport_only(changes)
        local = (os.getenv('AI_LOCAL_PLANNING','true').lower() == 'true' and changes
                 and allow_local
                 and set(changes) <= {'accommodation','hotel_stay','dining','budget','budgets','transport'})
        budgets=([changes['budget']] if 'budget' in changes else [])+changes.get('budgets',[])
        dining_scopes=changes.get('dining',{}).get('scopes',[]) or [s for b in budgets if b['scope']=='food' for s in b.get('scopes',[])]
        candidate['answer_meal_scopes']=dining_scopes
        if dining_scopes:
            from tools.trip.planner import meal_scopes
            available={(s['day_index'],s['meal']) for s in meal_scopes(spec,base['plan'],dining_scopes)}
            explicit=any(b.get('scopes') for b in budgets) or bool(re.search(r'第[一二两三四五六七八九十\d]+天',changes.get('dining',{}).get('source','')))
            if explicit and any((s['day_index'],s['meal']) not in available for s in dining_scopes):
                raise TripPlanError('该餐次不在当前到达、离开或可用行程时间内。预算要求已保留，请明确是否改变离开时间，或改为其他餐次。',code='meal_window_conflict')
            if not explicit and 'scopes' in spec.get('dining_preferences',{}):
                spec['dining_preferences']['scopes']=[s for s in dining_scopes if (s['day_index'],s['meal']) in available]
        local = local and all(b['scope'] in {'accommodation','food'} for b in budgets)
        if local:
            categories=set()
            if 'accommodation' in changes or 'hotel_stay' in changes or any(b['scope']=='accommodation' for b in budgets):
                categories.add('hotel')
            if 'dining' in changes or any(b['scope']=='food' for b in budgets):
                categories.add('dining')
            if categories:
                lookup_categories={category for category in categories if spec.get('hotel_requested' if category=='hotel' else 'restaurant_requested')}
                anchors=[base['sources'][a['poi_id']] for day in base['plan']['days'] for a in day['activities']]
                from tools.research.sources import planning_policy
                restricted=not planning_policy(spec,event.content)['map_allowed']
                # Budget edits first reuse the places already collected for this plan.
                lookup_categories={c for c in lookup_categories if ('accommodation' if c=='hotel' else 'dining') in changes
                    or c=='hotel' and 'hotel_stay' in changes and not base['plan'].get('hotel_ids')}
                fresh=self.planner.collect_places(spec,categories=lookup_categories,anchors=anchors) if lookup_categories and not restricted else {}
                candidate['sources'] = {**base['sources'],**fresh}
                candidate['plan'] = self.planner.update_recommendations(spec,base['plan'],candidate['sources'],categories,
                    dining_scopes=dining_scopes)
            candidate['planning_mode']='local_recommendations' if categories else 'routes_only'
            self.local_decision(spec,'本轮仅更新住宿或餐饮候选，沿用已有景点安排。' if categories else '本轮仅更新交通证据，沿用已有景点安排。')
        elif not changes or changes.keys() - {'transport'} or not allow_local:
            request=source_text or event.content
            if set(changes)=={'intercity'} and set(changes['intercity'].get('value',{}))=={'inbound'}:
                request=f"第{spec['day_count']}天调整返程交通"
            candidate['sources'],candidate['plan']=self._generate(event,spec,base['plan'],request,previous_sources=base['sources'])
        if local and categories and 'transport' not in changes and not restricted:
            self.planner._add_recommendation_routes(candidate['plan'],spec,candidate['sources'],categories=categories,dining_scopes=dining_scopes)
        elif not local or not categories:
            self.planner.add_route_evidence(candidate['plan'], spec, candidate['sources'], base['plan'])
        if local and 'transport' in changes and candidate['plan'].get('intercity'):
            self.planner.connect_transport(candidate['plan'],spec,candidate['sources'],candidate['plan']['intercity'],base['plan'].get('intercity'))
        if hasattr(self.store,'diagnostics'):
            self.store.diagnostics.record('planning',{'event_key':event.event_key,'status':candidate.get('planning_mode','full'),
                                                     'version':base.get('version',1)})
        candidate['plan'].pop('_local_patch',None)
        return candidate,summary

    def update_constraints(self, event, claim, trip_id, expected_version, changes, *, source_text=None,
                           preference_update=None, semantic_task=None, expected_draft=None,requested_checks=()):
        current = self.store.trips.get(event, trip_id)
        if not current or current['status'] != 'active' or current['version'] != expected_version:
            raise TripPlanError('当前行程已经变化，请重新查看后再调整。')
        draft = self.draft(event, current)
        draft_key = (draft['task'].task_id,draft['task'].version) if draft else ('',0)
        if expected_draft is not None and draft_key != expected_draft:
            raise TripPlanError('待确认草稿已变化，请重新查看后再修改。')
        stale_draft = bool(draft and draft['task'].slots.get('expected_trip_version') != expected_version)
        base = draft['candidate'] if draft else current
        if stale_draft:
            from tools.trip.constraints import rebase_candidate
            with self.store._connect() as db:
                row=db.execute('SELECT snapshot_json FROM trip_versions WHERE trip_id=? AND version=?',
                    (trip_id,draft['task'].slots.get('expected_trip_version'))).fetchone()
            if not row:raise TripPlanError('缺少草案基础版本，请先核对最新行程。')
            proposed=deepcopy(base);latest=deepcopy(current)
            if changes:
                # Explicit new choices resolve only their fields; unrelated conflicts remain visible.
                proposed['spec'],_=apply_constraints(proposed['spec'],changes,source_text or event.content)
                latest['spec'],_=apply_constraints(latest['spec'],changes,source_text or event.content)
            base,conflicts=rebase_candidate(json.loads(row[0]),proposed,latest)
            if conflicts:raise TripPlanError('草案与当前正式版仍有冲突。请根据以下两版差异明确最终选择；原草案仍保留。\n'
                +render_diff(trip_diff(current,draft['candidate'])))
        rebased=deepcopy(base)
        candidate,summary=self._edit_candidate(event,base,changes,source_text,
            allow_local=not (draft and draft['recovered']))
        if stale_draft:candidate=self.planner.reconcile_candidate(candidate,rebased)
        candidate['version']=expected_version+1
        if stale_draft:summary.insert(0,'已对比基础版、草案与当前正式版，保留无冲突修改；本轮明确选择优先')
        slots = {}
        if requested_checks:slots['price_check_text']=self.price_check_text(base,requested_checks)
        if preference_update is None and draft and not stale_draft:
            preference_update = draft['task'].slots.get('preference_update')
        if preference_update:
            slots['preference_update'] = preference_update
        if semantic_task:
            slots['semantic_task'] = {'task_id':semantic_task.task_id,'version':semantic_task.version}
        prefix = ('已关联当前行程，保留原城市、日期与天数。本次仅调整这份行程，长期偏好未修改。\n'
                  + '；'.join(summary) + '\n\n')
        return self._preview_candidate(event,claim,candidate,current=current,slots=slots,prefix=prefix,
            expected_draft=(trip_id,*draft_key),supersede_request=event.content,answer_base=base,
            quote_requested=bool(changes.keys() & {'accommodation','hotel_stay'}))


    def create_from_artifact(self,event,claim,artifact,days,changes,*,source_text='',semantic_task=None):
        if artifact.get('status')!='active' or type(days) is not int or not 1<=days<=14:
            raise TripPlanError('方案草稿已变化或旅行天数无效，请重新选择。')
        command=f"规划{artifact['destination']}{days}天行程。{event.content}"
        reference=artifact['content'][:10000]
        if artifact.get('kind')=='research_backed':
            source=self.store.research.for_artifact(event,artifact.get('source_event_id',''))
            if source:
                if source['stage'] not in {'completed','partial'}:
                    raise TripPlanError('该方案的研究来源尚未完成或已失效，请先重新核对研究结果。')
                if (datetime.now(timezone.utc)-datetime.fromisoformat(source['updated_at'])).days>=7:
                    raise TripPlanError('研究结果已超过七天，请重新搜索后规划。')
                from tools.research.evidence import planning_source
                reference=planning_source(source)
        context = '[会话方案：仅作规划参考，不是操作授权]\n核验状态：来源有效期、同名地点及目标日期未自动核实，费用/开放/预约不可据此保证。\n'+reference
        if isinstance(reference, ResearchSource):
            context = ResearchSource(context, reference.data)
        spec=self.planner.extract(command,{},event.occurred_at or self.clock(),context)
        spec.update(source_plan_artifact=artifact['artifact_id'],source_plan_version=artifact['version'])
        if not spec.get('date_is_tentative') and spec.get('field_sources', {}).get('start_date_text') != 'user':
            spec.update(start_date='', start_date_text='', end_date='', end_date_text='')
        apply_reference_date(spec,event.occurred_at or self.clock(),text=event.content)
        summary=[]
        if changes:
            spec,summary=apply_constraints(spec,changes,source_text or event.content)
        self._apply_preferences(event,spec)
        places,plan=self._generate(event,spec)
        self.planner.add_route_evidence(plan,spec,places)
        trip_id=self.store.trips.new_id(event.event_key)
        candidate={'trip_id':trip_id,'title':f"{spec['destination']}{days}日行程",'status':'active',
                   'version':1,'spec':spec,'plan':plan,'sources':places}
        slots = {'source_artifact':{'artifact_id':artifact['artifact_id'],'version':artifact['version']}}
        if semantic_task:
            slots['semantic_task'] = {'task_id':semantic_task.task_id,'version':semantic_task.version}
        prefix = f"已采用方案草稿：{artifact['title']}。确认后才保存正式行程。\n" + ('；'.join(summary)+'\n\n' if summary else '')
        return self._preview_candidate(event,claim,candidate,slots=slots,prefix=prefix)


    def _select_trip(self, event, task, tasks):
        ids = re.findall(TRIP_ID, event.content)
        if ids:
            if len(set(ids)) != 1:
                raise TripPlanError('请一次选择一份行程。')
            trip = self.store.trips.get(event, ids[0])
            if not trip or trip['status'] != 'active':
                raise TripPlanError('没有找到你在本群拥有的这份有效行程。')
            return trip
        if task and task.slots.get('trip_id'):
            trip = self.store.trips.get(event, task.slots['trip_id'])
            if trip and trip['status'] == 'active':
                return trip
        if event.reply_to_id:
            for recent in tasks:
                if recent.task_type == 'itinerary' and recent.slots.get('trip_id'):
                    trip = self.store.trips.get(event, recent.slots['trip_id'])
                    if trip and trip['status'] == 'active':
                        return trip
        trips = self.store.trips.list_for_owner(event)
        if len(trips) == 1:
            return trips[0]
        return None

    def _reminder_changes(self, event, trip, *, force_refresh=False):
        from tools.research.sources import planning_policy
        linked = self.store.trips.linked_reminders(event, trip['trip_id'], include_cancelled=True)
        plan = change_plan(event.event_key, trip, linked, now=self.clock(), force_refresh=force_refresh)
        policies, failures = {}, {}
        restricted=not planning_policy(trip['spec'],event.content)['rules_allowed']
        for entity, visit_date, source_url in plan.policy_requests:
            if restricted:
                failures[entity]='按已保存的来源限制未联网刷新规则，原提醒保持待核对。'
                continue
            try:
                policies[entity] = self.policy_resolver.resolve(entity, source_url=source_url,
                                                               visit_date=date.fromisoformat(visit_date))
            except PolicyUnavailable as error:
                failures[entity] = str(error)
        if plan.policy_requests:
            plan = change_plan(event.event_key, trip, linked, now=self.clock(), policies=policies,
                               failures=failures, force_refresh=force_refresh)
        for entity in plan.fingerprints:
            self.store.policies.record(policies[entity])
        for action, reminder in plan.changes:
            if action == 'create':
                reminder['reminder_id'] = self.store.reminders.new_id(reminder['source_operation_key'])
        return plan.changes, plan.notes, plan.errors, plan.fingerprints, plan.review

    def list_trips(self, event):
        trips = self.store.trips.list_for_owner(event)
        return '\n'.join(f"{trip['title']}（{trip['trip_id']}，版本 {trip['version']}）" for trip in trips) or '还没有保存的行程。'

    def confirm_intent(self, event, claim, ir, *, mode='execute'):
        if mode == 'preview':
            return '已识别到待确认的行程变更；当前处于预览模式，尚未执行。'
        if any(item.domain == 'trip' and item.operation == 'confirm_pending_operation' for item in ir.operations):
            return self.handle(event, claim, command='确认行程修改')
        return None

    def _preview_candidate(self, event, claim, candidate, *, current=None, task=None, slots=None,
                           force_refresh=False, expected_draft=None, prefix='', supersede_request='',answer_base=None,
                           write_answer=True,draft_action=None,draft_guard=None,quote_requested=False):
        base=answer_base or (task.slots.get('candidate_trip') if task else None) or current
        if not candidate['spec'].get('hotel_requested'):
            candidate['plan'].pop('hotel_quotes',None)
        elif base and base['spec'].get('destination')==candidate['spec'].get('destination'):
            if not candidate['plan'].get('hotel_quotes') and base['plan'].get('hotel_quotes'):
                candidate['plan']['hotel_quotes']=deepcopy(base['plan']['hotel_quotes'])
        if self.hotel_quotes and candidate['status']=='active' and candidate['spec'].get('hotel_requested'):
            previous=deepcopy(candidate)
            candidate['plan']['hotel_quotes']=lookup_hotel_quotes(self.hotel_quotes,event,candidate,
                allow_query=base is None or quote_requested)
            candidate['plan']['hotel_quotes'].pop('reused',None)
            candidate=self.planner.reconcile_candidate(candidate,previous)
        changes, notes, errors, fingerprints, _ = self._reminder_changes(event, candidate, force_refresh=force_refresh)
        if force_refresh and not changes and not errors:
            return '已核查行程预约规则，没有需要调整的关联提醒。\n' + '\n'.join(notes)
        slots = deepcopy(slots or {})
        price_check_text=slots.pop('price_check_text','')
        previous=answer_base or (task.slots.get('candidate_trip') if task else None) or current
        narrative_changes=trip_diff(previous,candidate)['changes'] if previous else []
        candidate['answer_changes']=[row['field'] for row in narrative_changes]
        if previous and previous['plan'].get('hotel_quotes')!=candidate['plan'].get('hotel_quotes'):
            candidate['answer_changes'].append('hotel_quotes')
        modifying=bool(current or task and task.slots.get('candidate_trip'))
        from tools.trip.presentation import answer_basis
        if candidate.get('answer') and candidate.get('answer_basis')!=answer_basis(candidate):candidate.pop('answer',None)
        if write_answer and self.answer_writer and candidate['status']=='active':
            answer=self.answer_writer.generate(event,candidate,narrative_changes)
            if answer:candidate.update(answer=answer,answer_basis=answer_basis(candidate))
            else:candidate.pop('answer',None)
        slots['reply_mode']='update' if modifying and candidate.get('answer') else 'full'
        slots.update(trip_id=candidate['trip_id'], candidate_trip=candidate,
                     expected_trip_version=current['version'] if current else 0,
                     new_trip_pending=current is None, trip_already_saved=False,
                     draft_context_complete=True, policy_fingerprints=fingerprints, force_refresh=force_refresh,
                     linked_versions={r['reminder_id']:r['version'] for r in self.store.trips.linked_reminders(event,candidate['trip_id'])},
                     reminder_versions={r['reminder_id']:r['version'] for _,r in changes if 'version' in r})
        reply = self.format_trip(candidate, preview=True,reply_mode=slots['reply_mode'])
        if current is None:
            reply = reply.replace('行程修改预览','行程创建预览',1)
        elif candidate['status'] == 'cancelled':
            reply = '取消行程预览（尚未取消）：\n' + reply
        if notes or errors:
            reply += '\n关联提醒尚未变更：\n' + '\n'.join([*notes,*errors])
        if slots.get('preference_update'):
            reply += '\n确认时同时保存长期偏好：' + self.store.preferences.describe(slots['preference_update']['values'])
        reply = price_check_text + (prefix if not write_answer or not candidate.get('answer') else '') + reply + '\n核对后可保存这版行程；确认后才保存行程和关联提醒。'
        artifact = slots.get('source_artifact')
        return self._question(task,event,claim,slots,('confirmation',),reply,
            expected_trip=(current['trip_id'],current['version']) if current else None,
            expected_draft=expected_draft, supersede_request=supersede_request,
            expected_artifact=(artifact['artifact_id'],artifact['version']) if artifact else None,
            draft_action=draft_action,draft_guard=draft_guard)

    def remove_activity(self, event, claim, current, day_index, name):
        if current.get('_new_trip'):
            task=current['_draft_task'];candidate=deepcopy(task.slots['candidate_trip'])
            self._remove_activity(candidate,day_index,name)
            self.planner.add_route_evidence(candidate['plan'],candidate['spec'],candidate['sources'],current['plan'])
            return self._preview_candidate(event,claim,candidate,task=task,slots=task.slots)
        draft = self.draft(event, current)
        if draft and (draft['recovered'] or draft['task'].slots.get('expected_trip_version') != current['version']):
            raise TripPlanError('草稿需要按最新行程重新生成，再删除活动。')
        candidate = deepcopy(draft['candidate'] if draft else current)
        already_removed = (draft and 1 <= day_index <= len(candidate['plan']['days']) and
            not any(candidate['sources'][a['poi_id']]['name'] == name
                    for a in candidate['plan']['days'][day_index-1]['activities']))
        if not already_removed:
            self._remove_activity(candidate, day_index, name)
        candidate['version'] = current['version'] + 1
        self.planner.add_route_evidence(candidate['plan'], candidate['spec'], candidate['sources'], current['plan'])
        return self._preview_candidate(event,claim,candidate,current=current,
            slots={k:v for k,v in draft['task'].slots.items() if k != 'semantic_task'} if draft else None,
            expected_draft=(current['trip_id'],draft['task'].task_id if draft else '',draft['task'].version if draft else 0))

    def _remove_activity(self, candidate, day_index, name):
        spec = candidate['spec']
        if not 1 <= day_index <= spec['day_count']:
            raise TripPlanError('目标天数不在当前行程中。')
        day = candidate['plan']['days'][day_index-1]
        matched = [a for a in day['activities'] if name == candidate['sources'][a['poi_id']]['name']]
        if len(matched) != 1:
            raise TripPlanError('请使用该天行程里的完整地点名称，明确要去掉哪一处。')
        day['activities'].remove(matched[0])
        spec.setdefault('activity_count_overrides', {})[str(day_index)] = len(day['activities'])
        spec['must_visit'] = [value for value in spec.get('must_visit', []) if value != name]
        if not day['activities']:
            spec['rest_days'] = sorted(set(spec.get('rest_days', [])) | {day_index})
        previous=candidate['plan']
        clean=self.planner.validate_plan(previous,spec,candidate['sources'])
        candidate['plan']={**previous,**clean}
        categories={category for category,field in (('hotel','hotel_requested'),('dining','restaurant_requested')) if spec.get(field)}
        if categories:
            candidate['plan']=self.planner.update_recommendations(spec,candidate['plan'],candidate['sources'],categories)
        traffic=candidate['plan'].get('intercity')
        if traffic:
            self.planner.connect_transport(candidate['plan'],spec,candidate['sources'],traffic,deepcopy(previous.get('intercity',{})))
        self.local_decision(spec,'本轮仅调整所选活动，保留其他已确认要求，未重新生成两版草案。')

    def _confirm_preview(self, event, claim, task, current, command, policy_link=False):
        if task.is_expired(self.clock()):
            return '这份确认已过期，草案仍保留。请引用草案继续修改或重新生成预览，再确认新版本；本轮未执行保存。'
        slots = deepcopy(task.slots)
        slots.pop('semantic_task', None)
        new_trip_pending = bool(slots.get('new_trip_pending'))
        text = event.content
        candidate = slots['candidate_trip']
        if command in CONFIRM_TRIP:
            from tools.trip.planner import HOTEL_POLICY_VERSION
            hotel_changes=any(row.get('field','').startswith(('hotel','accommodation','budgets.accommodation')) for row in slots.get('change_diff',{}).get('changes',[]))
            if (candidate['spec'].get('hotel_requested') and candidate['plan'].get('hotel_recommendation_policy')!=HOTEL_POLICY_VERSION
                    and (new_trip_pending or hotel_changes or slots.get('revalidation'))):
                return '这份住宿预览尚未按当前预算和品牌规则核对，请先重新核对并生成预览，再保存；原草案仍保留。'
            if candidate['plan'].get('intercity',{}).get('completeness')=='conflict':
                return '交通窗口与活动仍有冲突，尚未保存。请先调整日期、车次或活动要求。'
            if not new_trip_pending:
                draft = self.draft(event,current)
                if draft and draft['task'].task_id == task.task_id and draft['recovered']:
                    return '这份旧预览遗漏了此前的草稿要求，请发送“重新生成行程预览”，核对完整要求后再确认。'
            if event.platform == 'web' and not self.store.tasks.confirmation_preview(event,task):
                return '完整预览暂时不可用，请发送“重新生成行程预览”，核对后再确认。'
        if policy_link:
            urls = re.findall(r'https://[^\s，。<>]+', text)
            entities = [name for name in candidate['spec'].get('reminder_entities', []) if name in text]
            if not entities and len(candidate['spec'].get('reminder_entities', [])) == 1:
                entities = candidate['spec']['reminder_entities']
            if len(urls) != 1 or len(entities) != 1:
                return '请在链接前写明行程里的完整场馆名称，一次补充一个预约说明链接。'
            candidate['spec'].setdefault('policy_sources', {})[entities[0]] = urls[0]
        if not new_trip_pending and current['version'] != slots['expected_trip_version']:
            raise TripPlanError('行程已变化，旧预览不能确认。请查看最新行程后重新修改。')
        linked_versions = ({row['reminder_id']: row['version'] for row in self.store.trips.linked_reminders(event, current['trip_id'])}
                           if current else {})
        if linked_versions != slots.get('linked_versions', {}):
            raise TripPlanError('关联提醒集合已经变化，请重新查看并修改行程，不能使用旧预览。')
        changes, notes, errors, fingerprints, review = self._reminder_changes(event, candidate, force_refresh=slots.get('force_refresh', False))
        old_versions = slots.get('reminder_versions', {})
        if any(old_versions.get(row['reminder_id']) != row['version'] for _, row in changes if 'version' in row):
            raise TripPlanError('关联提醒已被修改，旧预览不能确认。请查看最新行程后重试。')
        if errors or policy_link or (slots.get('policy_fingerprints') != fingerprints):
            slots.update(policy_fingerprints=fingerprints, reminder_versions={row['reminder_id']: row['version'] for _, row in changes if 'version' in row})
            source_artifact=slots.get('source_artifact')
            return self._question(task, event, claim, slots, ('confirmation',),
                self.format_trip(candidate,preview=True) + '\n规则或适用条件需要重新核对，尚未应用变更。\n'
                + '\n'.join([*notes, *errors]) + '\n核对后回复“确认行程提醒”。',
                expected_trip=(current['trip_id'],current['version']) if current else None,
                expected_artifact=(source_artifact['artifact_id'],source_artifact['version']) if source_artifact else None)
        if command == '重新查询行程预约规则':
            source_artifact=slots.get('source_artifact')
            return self._question(task, event, claim, slots, ('confirmation',),
                self.format_trip(candidate,preview=True) + '\n' + '\n'.join(notes) + '\n核对后回复“确认行程提醒”。',
                expected_trip=(current['trip_id'],current['version']) if current else None,
                expected_artifact=(source_artifact['artifact_id'],source_artifact['version']) if source_artifact else None)
        for _, reminder in changes:
            if reminder.get('source'):
                reminder['source']['applicability_confirmed_by_user'] = True
        reply = ('已创建行程。\n' if new_trip_pending else
                 '已取消行程。\n' if candidate['status'] == 'cancelled' else
                 '已应用行程与提醒变更。\n' if not slots.get('trip_already_saved') else '已设置行程关联提醒。\n') + '\n'.join(notes)
        if slots.get('preference_update'):
            reply += '\n已同时保存长期偏好：' + self.store.preferences.describe(slots['preference_update']['values'])
        if new_trip_pending or not slots.get('trip_already_saved'):
            reply += f"\n已保存这版行程：{candidate['title']}。完整安排可在行程侧栏查看。"
            if candidate['spec'].get('date_is_tentative'):
                reply+=f"\n日期暂定：{candidate['spec'].get('start_date','')} 至 {candidate['spec'].get('end_date','')}，实际出发日尚未确定。"
        artifact=slots.get('source_artifact')
        return self.store.trips.commit(event, claim, self._task_update(task, event, {'trip_id': candidate['trip_id']}, 'completed'),
            reply, trip=candidate if new_trip_pending else None if slots.get('trip_already_saved') else candidate,
            presentation={'trip':{k:deepcopy(v) for k,v in candidate.items() if not k.startswith('_')},
                          'sections':[],'prefix':reply,'suffix':'','is_preview':False},
            expected_version=None if new_trip_pending or slots.get('trip_already_saved') else current['version'],
            expected_trip=None if new_trip_pending else (current['trip_id'], current['version']),
            expected_artifact=(artifact['artifact_id'],artifact['version']) if artifact else None,
            adopt_artifact=bool(new_trip_pending and artifact), reminder_changes=changes,
            preference_update=slots.get('preference_update'), now=self.clock())


    def apply_text_intent(self,event,claim,ir,*,mode='execute',tasks=None):
        if (len(ir.operations)!=1 or ir.operations[0].values.keys()!={'request_text'}
                or ir.operations[0].values['request_text']!=event.content or not ir.source_spans
                or ir.operations[0].operation not in {'create','edit'}):
            return '行程规划必须对应本轮完整请求，请重新说明目的地和要求。'
        if mode!='execute':
            return '已识别行程规划请求；当前仅解析预览，尚未生成草稿。'
        return self.handle(event,claim,tasks=tasks,operation=ir.operations[0].operation)

    def handle(self, event, claim, source_text_override='', *, command=None, tasks=None, operation=None):
        text = command if command is not None else event.content.strip()
        if tasks is None:
            tasks = self.store.tasks.recent(event.platform,event.storage_scope_id,event.sender_id,reply_to_id=event.reply_to_id)
        if not event.confirmation and hasattr(self.store.trips,'current_drafts'):
            known={t.task_id for t in tasks}
            tasks=(*tasks,*(t for t in self.store.trips.current_drafts(event) if t.task_id not in known))
        if event.message_reference and event.message_reference.get('trip') and not re.search(TRIP_ID,text):
            source_id=event.message_reference['trip']['trip_id']
            tasks=tuple(t for t in tasks if t.task_type!='itinerary' or t.slots.get('trip_id')==source_id)
            targets,reference_context=trip_context(self.store,event)
            if text in TRIP_COMMANDS or operation=='edit':
                if reference_context.get('trip_reference_unavailable') or any(t.get('_reference_stale') for t in targets if t['trip_id']==source_id):
                    return '所引用版本已取消或已有新的修改。请引用最新草案继续，或明确基于历史内容重新规划。'
        task = trip_continuation(text, tasks)
        resuming_failure=bool(task and task.slots.get('planning_failure') and text in {'继续规划','重新生成行程预览'})
        if resuming_failure:
            text=task.initial_request
        if task is None and operation=='edit':
            ids=set(re.findall(TRIP_ID,text))
            pending=[t for t in tasks if t.task_type=='itinerary' and t.status=='collecting' and t.slots.get('new_trip_pending')
                     and t.slots.get('candidate_trip') and (not ids or t.slots['candidate_trip']['trip_id'] in ids)]
            if len(pending)==1:task=pending[0]
        trips = self.store.trips.list_for_owner(event)
        reference_answer = len(trips) == 1 and trips[0]['spec'].get('date_is_tentative') and reference_date_answer(text)
        if operation is None and task is None and not trip_request(text) and not reference_answer:
            return None
        command = re.sub(TRIP_ID, '', text).strip()
        if command == '重新生成行程预览':
            if task and task.slots.get('new_trip_pending') and task.slots.get('candidate_trip'):
                candidate=deepcopy(task.slots['candidate_trip'])
                old=candidate['plan'].get('intercity')
                if old:
                    traffic=self._intercity(event,candidate['spec'],old,refresh=True,previous_plan=candidate['plan'])
                    self.planner.connect_transport(candidate['plan'],candidate['spec'],candidate['sources'],traffic,old)
                    candidate['plan']['intercity']=traffic
                return self._preview_candidate(event,claim,candidate,task=task,slots=task.slots)
            current = self._select_trip(event, task, tasks)
            if not current:
                return '请先查看行程，并用行程编号说明要重新生成哪份预览。'
            return self.update_constraints(event,claim,current['trip_id'],current['version'],{})
        if command == '我的行程':
            return self.list_trips(event)
        slots = deepcopy(task.slots) if task else {}
        slots.pop('semantic_task', None)
        if (task and slots.get('candidate_trip') and command not in CONFIRM_TRIP
                and set(task.missing_slots) <= {'confirmation'}
                and any(slots['candidate_trip']['plan'].get('intercity',{}).get(d,{}).get('missing_fields') for d in ('outbound','inbound'))
                and location_answer(text) and not reference_date_answer(text) and not trip_request(text)):
            candidate=deepcopy(slots['candidate_trip'])
            current=self.store.trips.get(event,candidate['trip_id'])
            if current and current['version']!=slots.get('expected_trip_version'):
                return '行程版本已变化，请重新查看后补充城际交通。'
            spec=self.planner.extract(text,candidate['spec'],event.occurred_at or self.clock())
            candidate['sources'],candidate['plan']=self._generate(event,spec,candidate['plan'],text)
            candidate['spec']=spec
            return self._preview_candidate(event,claim,candidate,current=current,task=task,slots=slots)
        if task and 'document_ref' in task.missing_slots:
            selection = re.fullmatch(r'(?:第)?([一二两三四五六七八九十\d]+)(?:个|份)?', text)
            index = number(selection[1]) - 1 if selection else -1
            options = slots['document_candidates']
            matched = [option for option in options if option['filename'] == text]
            if 0 <= index < len(options):
                selected = options[index]
            elif len(matched) == 1:
                selected = matched[0]
            else:
                return '请用列表中的序号选择具体文档版本。'
            slots['document_id'] = selected['id']
            text = slots['original_command']
            command = re.sub(TRIP_ID, '', text).strip()
        if task and 'trip_ref' in task.missing_slots:
            selection = re.fullmatch(r'(?:第)?([一二两三四五六七八九十\d]+)(?:个|条)?', text)
            index = number(selection[1]) - 1 if selection else -1
            ids = slots.get('trip_candidates', [])
            if not 0 <= index < len(ids):
                return '请回复列表中有效的行程序号。'
            slots['trip_id'] = ids[index]
            task = replace(task, slots=slots)
            text = slots['original_command']
            command = re.sub(TRIP_ID, '', text).strip()
        policy_link = bool(task and slots.get('candidate_trip') and re.search(r'https://\S+', text))
        pending_date = bool(task and slots.get('candidate_trip') and (not slots['candidate_trip']['spec'].get('start_date') or slots['candidate_trip']['spec'].get('date_is_tentative'))
                            and re.fullmatch(DATE_PATTERN + r'[。 ]*', text))
        pending_transport = bool(task and 'transport' in task.missing_slots)
        editing = bool(command in {'查看行程', '查看我的行程', '取消行程', *CONFIRM_TRIP, '重新查询行程预约规则',
                                    '设置行程提醒', '按行程提醒预约', '刷新行程交通', '刷新行程预约规则'} or policy_link or pending_date or pending_transport
                       or reference_answer or re.search(r'改|调整|换|挪|放到|放第|不去|删除', text))
        if operation is not None:
            editing = operation == 'edit'
        new_trip_pending=bool(task and slots.get('new_trip_pending') and slots.get('candidate_trip'))
        current = None if new_trip_pending else self._select_trip(event, task, tasks) if editing else None
        if editing and current is None and not new_trip_pending:
            if not trips:
                return '还没有保存的行程。可以说“帮我规划武汉三天行程”。'
            slots.update(trip_candidates=[trip['trip_id'] for trip in trips], original_command=text)
            return self._question(task, event, claim, slots, ('trip_ref',),
                '要操作哪一份行程？回复序号：\n' + '\n'.join(f"{i}. {trip['title']}（{trip['trip_id']}）" for i, trip in enumerate(trips, 1)))
        if command in {'查看行程', '查看我的行程'}:
            candidate=current or slots['candidate_trip']
            if new_trip_pending:candidate={**candidate,'_new_trip':True,'_draft_key':(task.task_id,task.version)}
            reply = self.view(event,candidate)
            if task and 'trip_ref' in task.missing_slots:
                return self.store.trips.commit(event, claim,
                    self._task_update(task, event, {'trip_id': current['trip_id']}, 'completed'), reply,
                    expected_trip=(current['trip_id'], current['version']), now=self.clock())
            return reply
        if command in CONFIRM_TRIP | {'重新查询行程预约规则'} and not (task and slots.get('candidate_trip')):
            return '没有仍然有效的行程变更预览。请先查看或修改行程，再确认具体变更。'
        if task and (command in CONFIRM_TRIP | {'重新查询行程预约规则'} or policy_link) and slots.get('candidate_trip'):
            return self._confirm_preview(event, claim, task, current, command, policy_link)

        draft = self.draft(event,current) if current else None
        if draft and (draft['recovered'] or draft['task'].slots.get('expected_trip_version') != current['version']):
            raise TripPlanError('草稿需要按最新行程重新生成，再继续调整。')
        base = draft['candidate'] if draft else current or slots.get('candidate_trip')
        if draft and draft['task'].slots.get('preference_update'):
            slots['preference_update'] = draft['task'].slots['preference_update']
        spec = deepcopy(base['spec'] if base else slots.get('spec', {}))
        candidate = deepcopy(base) if base else None
        if new_trip_pending and command == '取消行程':
            if task.is_expired(self.clock()):
                return self.store.trips.commit(event,claim,self._task_update(None,event,{'trip_id':slots['trip_id']},'cancelled'),
                    '已取消行程创建草稿，未创建正式行程或关联提醒。',expected_draft=(slots['trip_id'],task.task_id,task.version),now=self.clock())
            return self.store.trips.commit(event,claim,self._task_update(task,event,{},'cancelled'),
                '已取消行程创建草稿，未创建正式行程或关联提醒。',now=self.clock())
        targeted_day = re.search(r'第([一二两三四五六七八九十\d]+)天', text)
        day_draft_key = ((current['trip_id'],draft['task'].task_id if draft else '',draft['task'].version if draft else 0)
                         if current else None)
        transport_change = re.search(r'改(?:成|为).{0,8}(公共交通|公交|地铁|步行|驾车|自驾|开车)', text) if current else None
        if current and targeted_day and re.search(r'室内|室外', text):
            day_index = number(targeted_day[1])
            if not 1 <= day_index <= spec['day_count']:
                raise TripPlanError('这个天数不在当前行程内。')
            indoor = set(spec.get('indoor_days', []))
            outdoor = set(spec.get('outdoor_days', []))
            period_match=re.search(r'第[一二两三四五六七八九十\d]+天(?:的)?(上午|下午|晚上)',text)
            if period_match:
                for key,days in (('indoor_periods',indoor),('outdoor_periods',outdoor)):
                    if day_index in days:
                        spec.setdefault(key,{})[str(day_index)]=['上午','下午','晚上']
                        days.discard(day_index)
                wanted,other=('indoor_periods','outdoor_periods') if '室内' in text else ('outdoor_periods','indoor_periods')
                spec.setdefault(wanted,{})[str(day_index)]=list(dict.fromkeys([*spec.get(wanted,{}).get(str(day_index),[]),period_match[1]]))
                spec.setdefault(other,{})[str(day_index)]=[p for p in spec.get(other,{}).get(str(day_index),[]) if p!=period_match[1]]
            elif '室内' in text:
                indoor.add(day_index)
                outdoor.discard(day_index)
            else:
                indoor.discard(day_index)
                outdoor.add(day_index)
            if not period_match:
                for key in ('indoor_periods','outdoor_periods'):
                    spec.setdefault(key,{}).pop(str(day_index),None)
            spec['indoor_days'] = sorted(indoor)
            spec['outdoor_days'] = sorted(outdoor)
            field='indoor_periods' if period_match and '室内' in text else 'outdoor_periods' if period_match else 'indoor_days' if '室内' in text else 'outdoor_days'
            spec.setdefault('fact_sources',{})[field]={'event_id':event.event_key,'source':event.content,'origin':'user',
                'revision':spec.get('fact_sources',{}).get(field,{}).get('revision',0)+1}
            places = {**base['sources'], **self.planner.collect_places(spec,categories={'general'})}
            generated = self.planner.plan(spec, places, base['plan'], text)
            candidate.update(spec=spec, plan=generated, sources=places)
        elif current and targeted_day and re.search(r'不去|删除', text):
            day_index = number(targeted_day[1])
            names = [p['name'] for p in current['sources'].values() if p['name'] in text]
            if len(set(names)) != 1:
                raise TripPlanError('请使用该天行程里的完整地点名称，明确要去掉哪一处。')
            return self.remove_activity(event, claim, current, day_index, names[0])
        elif current and targeted_day and re.search(r'放到|挪到|放第|挪第', text):
            target_index = number(targeted_day[1]) - 1
            if not 0 <= target_index < spec['day_count']:
                raise TripPlanError('目标天数不在当前行程中。')
            matches = [(day, activity) for day in candidate['plan']['days'] for activity in day['activities']
                       if candidate['sources'][activity['poi_id']]['name'] in text]
            if len(matches) != 1:
                raise TripPlanError('请使用行程里的完整地点名称指定要移动的项目。')
            old_day, activity = matches[0]
            target_day = candidate['plan']['days'][target_index]
            if target_day is not old_day:
                old_day['activities'].remove(activity)
                used_periods = {item['period'] for item in target_day['activities']}
                if not old_day['activities'] or len(target_day['activities']) >= (2 if spec.get('gentle') else 3):
                    swapped = target_day['activities'].pop(0)
                    old_day['activities'].append({**swapped, 'period': activity['period']})
                    used_periods = {item['period'] for item in target_day['activities']}
                period = next(value for value in ('上午', '下午', '晚上') if value not in used_periods)
                target_day['activities'].append({**activity, 'period': period})
                spec.setdefault('activity_count_overrides', {}).update({
                    str(old_day['day_index']): len(old_day['activities']),
                    str(target_day['day_index']): len(target_day['activities'])})
            candidate['plan'] = self.planner.validate_plan(candidate['plan'], spec, candidate['sources'])
        elif base and (pending_date or reference_answer or re.search(r'行程.{0,15}(?:改到|改成|改为)', text)) and re.search(DATE_PATTERN, text):
            raw = re.search(DATE_PATTERN, text)[0]
            parsed = parse_reminder_time(raw, event.occurred_at or self.clock())
            if not parsed.day or parsed.error:
                raise TripPlanError(parsed.error or '请提供明确的行程开始日期。')
            spec.update(start_date=parsed.day, start_date_text=raw)
            spec.pop('tour_start_date',None)
            confirm_planning_date(spec)
            spec.setdefault('field_sources', {})['start_date'] = 'user'
            spec.setdefault('fact_sources', {})['start_date'] = {'event_id':event.event_key,'source':raw,'origin':'user'}
            if spec.get('end_date'):
                spec['end_date'] = (date.fromisoformat(parsed.day) + timedelta(days=spec['day_count']-1)).isoformat()
                spec['end_date_text'] = ''
            for day in candidate['plan']['days']:
                day['date'] = (date.fromisoformat(parsed.day) + timedelta(days=day['day_index']-1)).isoformat()
            if spec.get('intercity_request',{}).get('origin'):
                traffic=self._intercity(event,spec,candidate['plan'].get('intercity'))
                if traffic:
                    self.planner.connect_transport(candidate['plan'],spec,candidate['sources'],traffic)
                    candidate['plan']['intercity']=traffic
            candidate['spec'] = spec
        elif current and command == '取消行程':
            candidate['status'] = 'cancelled'
        elif current and command == '刷新行程预约规则':
            candidate['spec'] = spec
        elif current and command in {'设置行程提醒', '按行程提醒预约'}:
            spec['remind_booking'] = True
            spec['reminder_entities'] = spec.get('must_visit') or list(dict.fromkeys(
                candidate['sources'][activity['poi_id']]['name']
                for day in candidate['plan']['days'] for activity in day['activities']
                if re.search(r'博物馆|美术馆|风景名胜', candidate['sources'][activity['poi_id']].get('type', ''))))
            candidate['spec'] = spec
        elif base and (transport_change or pending_transport or command == '刷新行程交通'):
            if pending_transport:
                spec['transport_text'] = text
            elif transport_change:
                spec['transport_text'] = transport_change[1]
            if (transport_change or pending_transport) and 'transport_preferences' in spec:
                mode = 'transit' if re.search(r'公共交通|公交|地铁', spec['transport_text']) else 'walking' if '步行' in spec['transport_text'] else 'driving'
                spec['transport_preferences']['preferred'] = [mode]
                spec['transport_preferences']['forbidden'] = [v for v in spec['transport_preferences'].get('forbidden',[]) if v != mode]
            if not spec.get('transport_text'):
                slots.update(candidate_trip=candidate,draft_context_complete=True)
                if current:
                    slots.update(trip_id=current['trip_id'],expected_trip_version=current['version'])
                return self._question(task,event,claim,slots,('transport',),
                    '这份行程打算使用公共交通、步行还是驾车？',
                    expected_trip=(current['trip_id'],current['version']) if current else None,expected_draft=day_draft_key)
            candidate['spec'] = spec
            if command == '刷新行程交通':
                traffic=self._intercity(event,spec,candidate['plan'].get('intercity'),refresh=True)
                if traffic:
                    self.planner.connect_transport(candidate['plan'],spec,candidate['sources'],traffic)
                    candidate['plan']['intercity']=traffic
        else:
            if task and text == '先不定日期':
                spec.update(start_date_text=text, start_date='')
                apply_reference_date(spec,event.occurred_at or self.clock(),text=text)
            else:
                source_text = source_text_override
                if slots.get('document_id') or re.search(r'(?:根据|按).{0,50}(?:文档|资料|行程单|\.(?:md|txt|docx|xlsx))', text):
                    if not slots.get('document_id'):
                        options = self.store.trips.document_options(event)
                        matches = [option for option in options if option['filename'] in text]
                        if re.search(r'最新|刚上传', text):
                            matches = list(matches or options)[:1]
                        if not matches and len(options) == 1:
                            matches = list(options)
                        if len(matches) != 1:
                            if not options:
                                return '当前群还没有可用旅行文档，请先上传，或直接告诉我城市和天数。'
                            slots.update(document_candidates=list(options), document_names=[option['filename'] for option in options],
                                         original_command=text)
                            return self._question(task, event, claim, slots, ('document_ref',),
                                '请选择本次规划使用的文档版本：\n' + '\n'.join(
                                    f"{i}. {option['filename']}（文档#{option['id']}，{option['created_at']}）"
                                    for i, option in enumerate(options, 1)))
                        slots['document_id'] = matches[0]['id']
                    source_text = self.store.trips.document_context(event, slots['document_id'])
                if not resuming_failure:
                    try:
                        spec = self.planner.extract(text, spec, event.occurred_at or self.clock(), source_text)
                    except TripPlanError as error:
                        if error.code != 'invalid_dates' or base:
                            raise
                        spec = deepcopy(error.spec)
                        # Rejected endpoints must not override the next date answer.
                        spec.update(start_date_text='', end_date_text='', start_date='', end_date='')
                        slots['spec'] = spec
                        return self._question(task,event,claim,slots,('start_date',),
                            str(error)+'\n已保留其他行程要求，请补充开始日期或起止日期，之后继续生成待确认草案。')
                if not current:
                    self._apply_preferences(event, spec)
            if not current:
                spec['remind_booking'] = bool(spec.get('remind_booking') or re.search(r'提醒.{0,8}预约|预约.{0,8}提醒', text))
            missing = []
            if not spec.get('destination'):
                missing.append('destination')
            if not spec.get('day_count'):
                missing.append('duration')
            if (spec.get('start_date_text') or spec.get('remind_booking')) and not spec.get('start_date') and not spec.get('defer_start_date'):
                missing.append('start_date')
            slots['spec'] = spec
            slots.setdefault('trip_id', current['trip_id'] if current else self.store.trips.new_id(event.event_key))
            if missing:
                questions = {'destination': '想去哪个城市？', 'duration': '计划玩几天？', 'start_date': '具体哪天开始这段行程？请写明月日。'}
                return self._question(task, event, claim, slots, tuple(missing), ' '.join(questions[field] for field in missing))
            spec['reminder_entities'] = spec.get('must_visit', []) if spec.get('remind_booking') else spec.get('reminder_entities', [])
            try:
                places,plan=self._generate(event,spec,current['plan'] if current else None,text if current else '',
                    previous_sources=slots.get('failure_places'))
            except TripPlanError as error:
                if current:raise
                slots.update(spec=getattr(error,'spec',spec),planning_failure={'code':error.code,'path':error.path,
                    'phase':getattr(error,'failure_stage','plan_validation'),'source_event':event.event_key},
                    failure_places=getattr(error,'places',{}))
                self.store.diagnostics.record('planning',{'event_key':event.event_key,'status':'failed','name':'trip_create',
                    'phase':slots['planning_failure']['phase'],'validation':{'code':error.code,'path':error.path}})
                return self._question(task,event,claim,slots,('plan_repair',),
                    str(error)+'\n尚无可确认草案。已保留需求与取得的资料，可补充目标信息或发送“继续规划”；未保存正式行程。')
            if spec.get('remind_booking') and not spec.get('reminder_entities'):
                spec['reminder_entities'] = list(dict.fromkeys(places[activity['poi_id']]['name']
                    for day in plan['days'] for activity in day['activities']
                    if re.search(r'博物馆|美术馆|风景名胜', places[activity['poi_id']].get('type', ''))))
            candidate = {'trip_id': current['trip_id'] if current else slots['trip_id'],
                'title': f"{spec['destination']}{spec['day_count']}日行程", 'status': 'active', 'spec': spec, 'plan': plan, 'sources': places}
        candidate['version'] = current['version'] + 1 if current else 1
        invalidate_changed_dates(candidate['spec'], candidate['plan'])
        if transport_change:
            candidate['spec']['transport_text'] = transport_change[1]
        from tools.research.sources import planning_policy
        restricted=not planning_policy(candidate['spec'],event.content)['map_allowed']
        if candidate['status'] == 'active' and not restricted and not slots.get('failure_places'):
            self.planner.add_route_evidence(candidate['plan'], candidate['spec'], candidate['sources'],
                current['plan'] if current and command != '刷新行程交通' else None)
        force_refresh = command == '刷新行程预约规则'
        candidate['plan'].pop('_local_patch', None)
        slots.pop('planning_failure',None);slots.pop('failure_places',None)
        return self._preview_candidate(event, claim, candidate, current=current, task=task,
                                       slots=slots, force_refresh=force_refresh, expected_draft=day_draft_key)


    def _apply_preferences(self, event, spec):
        if 'preference_version' in spec:
            return
        state = self.store.preferences.defaults(event)
        values, applied = state['values'], {}
        text = event.content + ' ' + ' '.join(spec.get('preferences', []))
        if 'pace' in values and not re.search(r'轻松|适中|紧凑|慢|累|老人|父母|每天', text):
            spec['preferences'].append(values['pace'] + '节奏')
            spec['gentle'] = values['pace'] == '轻松'
            applied['pace'] = values['pace']
        if 'transport' in values and not spec.get('transport_text'):
            spec['transport_text'] = values['transport']
            applied['transport'] = values['transport']
        if 'night' in values and not re.search(r'夜|晚上', text):
            spec['no_night'] = values['night'] == '不安排夜游'
            spec['preferences'].append(values['night'])
            applied['night'] = values['night']
        if 'interests' in values and not spec.get('must_visit'):
            spec['preference_interests'] = values['interests']
            spec['preferences'].append('兴趣：' + '、'.join(values['interests']))
            applied['interests'] = values['interests']
        if 'budget' in values and not spec.get('budget_text'):
            b = values['budget']
            spec['budget_text'] = f"{b['category']} {b['basis']}{b['period']} {b['amount']} {b['currency']}"
            applied['budget'] = b
        spec.update(preference_version=state['version'], applied_preferences=applied)
        spec.setdefault('field_sources', {}).update({key: 'preference' for key in applied})


    def adopt_intent(self,event,claim,ir,artifacts,pending,source_text, *, mode='execute'):
        operations=[op for op in ir.operations if op.domain=='trip' and op.operation=='create_from_plan_artifact']
        if len(operations)!=1 or not ir.source_spans:
            return '请明确要采用哪一份方案。'
        values=operations[0].values
        if values.keys()-{'plan_index','plan_reference','selected_days','changes'}:
            return '采用方案的请求包含不支持的字段，尚未创建行程。'
        artifact=None
        stored=pending.slots.get('plan_artifact') if pending else None
        if stored:
            artifact=next((a for a in artifacts if a['artifact_id']==stored['artifact_id']
                           and a['version']==stored['version']),None)
        index=values.get('plan_index')
        reference=values.get('plan_reference','')
        if artifact is None and type(index) is int:
            if len(artifacts)>1 and (not isinstance(reference,str) or not reference or reference not in event.content):
                return '请明确说出要采用“最开始”还是“刚才搜索”的方案。'
            artifact=next((item for item in artifacts if item['ordinal']==index),None)
        if artifact is None:
            return '没有找到唯一的方案草稿，请先查看方案草稿并选择一份。'
        previous_changes=pending.slots.get('plan_changes',{}) if pending else {}
        changes={**previous_changes,**values.get('changes',{})}
        if changes:
            try:
                apply_constraints({},changes,source_text)
            except ValueError as error:
                return str(error)
        selected=values.get('selected_days')
        if selected is not None and (type(selected) is not int or not 1<=selected<=14
                or (artifact['duration_options'] and selected not in artifact['duration_options'])):
            return '选择的旅行天数不在该方案中，请从已有方案天数中选择。'
        options=artifact['duration_options']
        if selected is None and len(options)==1:
            selected=options[0]
        if selected is None:
            if not options:
                question='这份方案没有确定天数，请告诉我计划玩几天；日期可以继续待定。'
            else:
                question='最开始的方案有'+'和'.join(f'{day}天' for day in options)+'两套，你想采用哪套？酒店推荐等新增要求会一起保留，日期可以先不定。'
            slots={'semantic_ir':to_jsonable(ir),'source_text':source_text,
                   'plan_artifact':{'artifact_id':artifact['artifact_id'],'version':artifact['version']},
                   'plan_changes':changes}
            update=TaskUpdate('semantic','collecting',pending.initial_request if pending else event.content,
                slots,('plan_duration',),pending.task_id if pending else '',pending.version if pending else None)
            self.store.tasks.prepare_result(event,claim,update,question)
            return question
        if mode=='preview':
            return f'已选择“{artifact["title"]}”的 {selected} 天方案；当前仅解析预览，尚未生成行程。'
        return self.create_from_artifact(event,claim,artifact,selected,changes,
            source_text=source_text,semantic_task=pending)

    @staticmethod
    def price_check_text(trip,checks):
        from tools.trip.planner import price_details
        from tools.trip.constraints import facts
        price_checks=[check for check in checks if check.get('kind','price')=='price']
        lines=[]
        if price_checks:
            lines.append('本轮供应商参考报价（不保证实时结算价或房态）：' if any(c.get('mode') in {'lookup','refresh'} for c in price_checks)
                         else '当前候选的价格说明（沿用各分店已保存的参考资料，没有查询实时结算价或房态）：')
            lines.extend('- '+row['label']+'：'+row['value'] for row in facts(trip['spec']) if row['field'].startswith('budgets.'))
        for check in checks:
            scope=check['scope'];plan=trip['plan'];places=trip['sources'];names=check.get('names',[])
            if check.get('kind')=='budget':
                budget=trip['spec'].get('budgets',{}).get(scope,{})
                if scope=='food':
                    from tools.trip.planner import meal_scopes
                    if not (check.get('day_index') or check.get('meal')):
                        if not budget.get('value') and not budget.get('meals'):
                            lines.append('餐饮预算尚未设置。')
                            continue
                        if budget.get('value'):lines.append('餐饮默认预算：'+budget['value']+'；'+budget.get('unit_text','单位未明确'))
                    selected=[s for s in meal_scopes(trip['spec'],plan) if
                        (not check.get('day_index') or s['day_index']==check['day_index']) and
                        (not check.get('meal') or s['meal']==check['meal'])]
                    if not selected:lines.append('指定餐次不在这份行程的可用餐次中，没有安排该餐。')
                    for s in selected:
                        override=budget.get('meals',{}).get(f"{s['day_index']}:{s['meal']}",{})
                        effective={**budget,**override}
                        label=f"第{s['day_index']}天{s['meal']}预算："
                        lines.append(label+(effective['value']+'；'+effective.get('unit_text','单位未明确')
                            +budget_default_note(effective)+('（单餐设置）' if override else '（沿用餐饮默认预算）')
                            if effective.get('value') else '尚未设置，不能按零元计算。'))
                else:
                    selected=[row for row in facts(trip['spec']) if row['field']=='budgets.'+scope or row['field'].startswith('budgets.'+scope+'.')]
                    lines.extend(row['label']+'：'+row['value'] for row in selected)
                    if not selected:lines.append(BUDGET_SCOPES[scope]+'预算尚未设置。')
                continue
            if scope=='accommodation' and plan.get('hotel_quotes'):
                from tools.hotel_prices.tool import quote_text
                quote=plan['hotel_quotes']
                if names:
                    quote={**quote,'offers':[o for o in quote.get('offers',[]) if o['hotel_name'] in names],
                           'requested_hotels':{pid:name for pid,name in quote.get('requested_hotels',{}).items() if name in names}}
                    missing=[name for name in names if name not in {o['hotel_name'] for o in quote['offers']}]
                    if missing:lines.append('未找到指定酒店的可验证报价：'+'、'.join(missing)+'。')
                lines.append(quote_text(trip['spec'],quote,places=places))
                continue
            rows=[]
            if scope=='food':
                for meal in plan.get('dining_options',[]):
                    if check.get('day_index') and meal['day_index']!=check['day_index']:continue
                    if check.get('meal') and meal['meal']!=check['meal']:continue
                    food=trip['spec'].get('budgets',{}).get('food',{})
                    budget={**food,**food.get('meals',{}).get(f"{meal['day_index']}:{meal['meal']}",{})}
                    if budget.get('comparison_unit')=='per_person_meal_cny':
                        lines.append(f"- 第{meal['day_index']}天{meal['meal']}预算：{budget.get('value','')} {budget.get('unit_text','')}。")
                    rows.extend((c['poi_id'],f"第{meal['day_index']}天{meal['meal']}") for c in meal.get('candidates',[]))
                rows.extend((pid,'餐饮') for pid in plan.get('restaurant_ids',[]))
            else:rows.extend((pid,'住宿') for pid in plan.get('hotel_ids',[]))
            seen=set();matched=set()
            for pid,label in rows:
                if pid in seen:continue
                seen.add(pid);place=places.get(pid,{})
                if names and place.get('name') not in names:continue
                matched.add(place.get('name'))
                details=price_details(place,'hotel' if scope=='accommodation' else 'dining')
                amount=details.get('average_price') or details.get('lowest_price') if scope=='accommodation' else details.get('per_capita')
                price=(f"参考{'房价' if scope=='accommodation' else '人均'}约{amount:g}元" if amount else '价格待确认，未按零元计算')
                if scope=='accommodation' and amount:
                    price=('参考房价约' if details.get('average_price') else '最低房价参考约')+f'{amount:g}元/间/晚'
                if scope=='accommodation' and details.get('reference_cost') is not None:
                    price+=f"；另有参考金额{details['reference_cost']:g}（{details.get('reference_cost_unit','人均消费')}），未用于房晚预算判断"
                if scope=='accommodation' and details.get('unverified_lowest_price') is not None:
                    price+=f"；最低价线索{details['unverified_lowest_price']:g}（{details.get('unverified_lowest_price_unit','口径未注明')}），未用于预算判断"
                recorded=str(place.get('queried_at',''))
                stamp='资料记录于'+recorded[:10] if re.match(r'\d{4}-\d{2}-\d{2}',recorded) else '资料时间未提供'
                lines.append(f"- {label} · {place.get('name',pid)}（{place.get('address') or '分店地址未返回'}）：{price}；{stamp}。")
            missing=[name for name in names if name not in matched]
            if missing:
                available=list(dict.fromkeys(places[pid].get('name',pid) for pid,_ in rows if pid in places))
                lines.append('- 未找到指定候选：'+'、'.join(missing)+'；本方案当前候选为：'+('、'.join(available) or '暂无')+'。请明确分店或引用对应消息，未借用同名店价格。')
            if not seen:lines.append('- 当前没有已选中的'+('住宿' if scope=='accommodation' else '餐饮')+'候选，无法据此确认价格。')
        if price_checks:
            if any(check['scope']=='food' for check in price_checks):lines.append('仅人民币每人每餐口径的预算可直接与参考人均比较；总额、每日和外币不自动换算。参考价不保证本次账单。')
            if any(check['scope']=='accommodation' for check in price_checks):lines.append('住宿预算按人民币每间每晚核对；指定日期报价和房态仍待确认。地图人均消费、未明确口径的最低价不用于房价预算判断。')
        else:lines.append('以上为这份行程的预算目标，不是商家的实际报价。')
        return '\n\n'.join(lines)+'\n\n'

    def read_intent(self,event,claim,ir,trips,context,pending,source_text,*,mode='execute'):
        """Compose registered reads against one owned snapshot; never dispatch writes."""
        if mode!='execute':return None if mode=='shadow' else '已识别行程只读查询；当前仅解析预览。'
        if (ir.action not in {'read','clarify'} or not ir.operations or
                any(op.domain!='trip' or op.operation not in TRIP_READ_OPERATIONS for op in ir.operations)):
            return '这组操作不属于可组合的行程只读查询。'
        ir=parse_intent_ir(to_jsonable(ir),source_text)
        values={};checks=[];include_trip=False
        for op in ir.operations:
            include_trip=include_trip or op.operation=='view_current_trip' or op.values.get('include_trip',False)
            for field in ('target_ref','trip_index','trip_reference'):
                if field not in op.values:continue
                if field in values and values[field]!=op.values[field]:return '这些查询指向不同的行程，请分别引用对应方案后查询。'
                values[field]=op.values[field]
            for check in op.values.get('requested_checks',[]):
                if check not in checks:checks.append(check)
        if len(checks)>8:return '一次最多读取8项行程信息，请减少查询事项。'
        values['include_trip']=include_trip
        if checks:values['requested_checks']=checks
        reference=event.message_reference or {}
        if reference.get('event_id') and not include_trip and checks and all(
                c['scope']=='accommodation' and c.get('kind','price')=='price' and c.get('mode','saved')=='saved' for c in checks):
            quote=self.store.research.hotel_quote_for_event(event,reference['event_id'])
            if quote:
                from tools.hotel_prices.tool import quote_text
                names={name for c in checks for name in c.get('names',[])}
                missing=[]
                if names:
                    quote={**quote,'offers':[q for q in quote.get('offers',[]) if q['hotel_name'] in names],
                           'requested_hotels':{pid:name for pid,name in quote.get('requested_hotels',{}).items() if name in names}}
                    missing=sorted(names-{q['hotel_name'] for q in quote['offers']})
                notice=('未找到指定酒店的可验证报价：'+'、'.join(missing)+'。\n\n') if missing else ''
                return '以下只读取所引用的酒店报价资料，按当时查询条件展示；不修改当前行程。\n\n'+notice+quote_text(
                    {'destination':quote.get('city',''),'hotel_stay':quote['stay']},quote)
        folded=replace(ir,action='read',operations=(IntentOperation('trip','check_prices',values),),requires_confirmation=False)
        return self.update_intent(event,claim,folded,trips,context,pending,source_text,mode=mode)

    def _trip_read_reply(self,event,snapshot,checks,include_trip,claim=None,pending=None):
        view_snapshot=deepcopy(snapshot)
        snapshot=deepcopy(snapshot)
        reference=event.message_reference or {}
        if reference.get('event_id') and any(c['scope']=='accommodation' and c.get('kind','price')=='price' and c.get('mode','saved')=='saved' for c in checks):
            quoted=self.store.research.hotel_quote_for_event(event,reference['event_id'])
            if quoted:snapshot['plan']['hotel_quotes']=quoted
        refresh=[c for c in checks if c.get('mode') in {'lookup','refresh'}]
        if refresh:
            if self.hotel_quotes:
                stays=[c['hotel_stay'] for c in refresh if c.get('hotel_stay')]
                if stays:
                    from tools.trip.constraints import stay_change
                    if any(stay!=stays[0] for stay in stays):return '同一批查价请使用一致的住宿条件；不同日期或人数请分开查询。'
                    snapshot['spec']['hotel_stay']=stay_change(snapshot['spec'].get('hotel_stay',{}),stays[0],event.content,spec=snapshot['spec'])
                    snapshot['spec']['_hotel_quote_trial']=True
                names=list(dict.fromkeys(n for c in refresh for n in c.get('names',[])))
                snapshot['plan']['hotel_quotes']=lookup_hotel_quotes(self.hotel_quotes,event,snapshot,refresh=True,names=names)
            else:snapshot['plan']['hotel_quotes']={'offers':[],'note':'酒店报价未启用，原行程未修改。'}
        elif self.hotel_quotes and not event.message_reference and any(
                c['scope']=='accommodation' and c.get('kind','price')=='price' for c in checks):
            saved=self.hotel_quotes.saved(event,{'targets':[{'name':p['name']} for p in hotel_quote_request(snapshot)[1].values()]})
            if saved:snapshot['plan']['hotel_quotes']=saved
        reply=self.price_check_text(snapshot,checks) if checks else ''
        if refresh:reply='本轮只查询参考报价，没有修改酒店选择、正式行程或长期偏好。\n\n'+reply
        if event.message_reference and event.message_reference.get('trip'):
            reply='以下依据所引用消息当时的行程快照。\n\n'+reply
            snapshot={**snapshot,'_new_trip':bool(event.message_reference.get('is_preview'))}
            view_snapshot={**view_snapshot,'_new_trip':bool(event.message_reference.get('is_preview'))}
        elif snapshot.get('_new_trip') or snapshot.get('_draft_applied'):
            reply='以下读取当前草案，尚未保存。\n\n'+reply
        reply=self.view(event,view_snapshot,prefix=reply) if include_trip else reply
        previous=pending.slots.get('semantic_ir',{}) if pending else {}
        if (claim is not None and previous.get('action')=='read' and previous.get('operations') and
                all(op.get('domain')=='trip' and op.get('operation') in TRIP_READ_OPERATIONS for op in previous['operations'])):
            update=TaskUpdate('semantic','completed',pending.initial_request,pending.slots,
                              task_id=pending.task_id,expected_version=pending.version)
            self.store.tasks.prepare_result(event,claim,update,reply)
        return reply

    def adopt_hotel_quote(self,event,claim,ir,tasks=(),*,mode='execute'):
        """Adopt a server-owned displayed choice into the existing preview/confirmation flow."""
        from core.time_input import NUMBER,number
        from tools.hotel_prices.quotes import STAY_FIELDS
        if len(ir.operations)!=1 or ir.action not in {'update','clarify'} or not ir.source_spans:
            return '请明确要采用哪次酒店报价，本次未修改行程。'
        values=ir.operations[0].values;reference=values.get('reference',{})
        ordinal=reference.get('ordinal') if isinstance(reference,dict) else None
        if (values.keys()-{'reference'} or not isinstance(reference,dict) or reference.keys()-{'selection','ordinal'}
                or not isinstance(reference.get('selection'),str) or reference['selection'] not in event.content
                or type(ordinal) is not int or not any(number(n)==ordinal for n in re.findall(r'第('+NUMBER+r')(?:家|个)',event.content))):
            return '请用引用消息及酒店序号明确选择；本次未修改行程。'
        source=(event.message_reference or {}).get('event_id','')
        if not source:
            refs={t.slots['last_result_ref'] for t in tasks if t.task_type=='hotel' and t.status=='completed' and t.slots.get('last_result_ref')}
            if len(refs)!=1:return '请引用要采用的那条酒店报价，避免多份名单串用；本次未修改行程。'
            source=next(iter(refs))
        quote=self.store.research.hotel_quote_for_event(event,source)
        targets=list((quote or {}).get('targets',{}).values())
        if not quote or not 1<=ordinal<=len(targets):return '引用中的酒店名单已不可用或序号超出范围，请重新选择。'
        current=self._select_trip(event,None,tasks)
        if current is None:return '已保留酒店报价；请先明确需要采用它的行程，再生成住宿预览。'
        draft=self.draft(event,current)
        if draft and (draft['recovered'] or draft['task'].slots.get('expected_trip_version')!=current['version']):
            return '当前行程草案已变化，请先重新核对最新预览，再选择酒店。'
        candidate=deepcopy(draft['candidate'] if draft else current);previous=deepcopy(candidate)
        target=targets[ordinal-1];pid=target['id'];stay=quote.get('stay',{});spec=candidate['spec']
        if (spec.get('hotel_stay') and any(spec['hotel_stay'].get(k)!=stay.get(k) for k in STAY_FIELDS)
                or spec.get('destination') and target.get('cityname','').removesuffix('市')!=spec['destination'].removesuffix('市')):
            return '该报价的城市或入住条件与当前行程不同，请明确是否要调整住宿范围；本次未修改。'
        if (spec.get('start_date') and stay.get('check_in') and stay['check_in']<spec['start_date'] or
                spec.get('end_date') and stay.get('check_out') and date.fromisoformat(stay['check_out'])>date.fromisoformat(spec['end_date'])+timedelta(days=1)):
            return '报价住宿日期超出当前行程范围，请先确认日期调整，再采用酒店。'
        if mode!='execute':return '已识别采用酒店报价，当前仅解析预览，尚未修改。'
        candidate['sources'][pid]=deepcopy(target);spec['hotel_requested']=True
        spec['hotel_stay']={k:deepcopy(v) for k,v in stay.items() if k in STAY_FIELDS}
        spec.setdefault('hotel_preferences',{})['selected_poi_id']=pid
        spec.setdefault('field_sources',{})['hotel_stay']='reference:'+source
        spec['hotel_selection']={'poi_id':pid,'source_event_id':source,'ordinal':ordinal,'source':event.content}
        candidate['plan']['hotel_ids']=[pid]
        candidate['plan']['hotel_quotes']={**deepcopy(quote),'targets':{pid:target},'requested_poi_ids':[pid],
            'requested_hotels':{pid:target['name']},'offers':[o for o in quote.get('offers',[]) if o['amap_poi_id']==pid]}
        candidate['version']=current['version']+1
        candidate=self.planner.reconcile_candidate(candidate,previous)
        return self._preview_candidate(event,claim,candidate,current=current,
            expected_draft=(current['trip_id'],draft['task'].task_id if draft else '',draft['task'].version if draft else 0),
            prefix='已将引用中的'+target['name']+'放入住宿预览。沿用该次参考资料，尚未保存或预订。\n')

    def update_intent(self, event, claim, ir, trips, context, pending, source_text, artifacts=(), *, mode='execute'):
        reading=ir.action=='read' and ir.operations and ir.operations[0].operation=='check_prices'
        if len(ir.operations) != 1 or ir.action not in {'read','update','clarify'} or ir.action=='read' and not reading or not ir.source_spans:
            return '行程调整的动作或依据不明确，未修改行程。请分别说明其他操作。'
        values = dict(ir.operations[0].values)
        if pending and not reading:
            from tools.trip.constraints import merge_constraint_changes
            operations=pending.slots.get('semantic_ir',{}).get('operations',[])
            if len(operations)==1 and operations[0].get('operation')=='update_constraints':
                values['changes']=merge_constraint_changes(operations[0].get('changes',{}),values.get('changes',{}),
                    correcting_meal_window='meal_window' in pending.missing_slots)
                ir=replace(ir,operations=(replace(ir.operations[0],values=values),))
        if values.keys() - {'target_ref','trip_index','trip_reference','changes','save_preference_text','question','requested_checks','include_trip'}:
            return '行程调整包含不支持的字段，未修改行程。'
        if 'question' in values and (not isinstance(values['question'],str) or len(values['question'])>400):
            return '模型返回的追问无效，原行程未修改，请重新说明这次调整。'
        if values.get('target_ref', 'current_trip') != 'current_trip':
            return '行程目标引用无效，未修改行程。请说明要调整哪份已保存行程。'
        if not trips:
            trips,context=trip_context(self.store,event,pending)
        if reading and (event.message_reference or {}).get('trip') and (values.get('requested_checks') or values.get('include_trip')) and not values.get('changes') and not values.get('save_preference_text'):
            return self._trip_read_reply(event,event.message_reference['trip'],values.get('requested_checks',[]),values.get('include_trip',False),claim,pending)
        if context.get('trip_reference_unavailable'):
            return '所引用的历史行程已取消、被替代或不可编辑；历史内容仍可查看，请明确是否基于它重新规划。'
        ids = re.findall(r'TR-[a-f0-9]{12}', event.content)
        if ids and (len(set(ids)) != 1 or not any(t['trip_id'] == ids[0] for t in trips)):
            logger.info('Trip constraint rejected: event=%s reason=invalid_target',event.event_key)
            return '没有找到当前会话中属于你的这份有效行程。'
        if not trips:
            if reading:return '当前会话还没有可读取的行程或草案。'
            if artifacts:
                reference = event.content
                index = artifacts[0]['ordinal'] if '最开始' in reference else artifacts[-1]['ordinal'] if re.search(r'刚才|最新|搜索',reference) else 0
                if index:
                    matched_reference=(re.search(r'最开始[^，。；]*',reference) if index==artifacts[0]['ordinal']
                                       else re.search(r'(?:刚才|最新|搜索)[^，。；]*',reference))
                    converted=IntentIR(ir.schema_version,ir.action,
                        (IntentOperation('trip','create_from_plan_artifact',{
                            'plan_index':index,'plan_reference':matched_reference[0] if matched_reference else '',
                            'changes':values['changes']}),),ir.missing_fields,True,ir.source_spans,ir.confidence)
                    return self.adopt_intent(event,claim,converted,artifacts,pending,source_text,mode=mode)
            return '当前会话还没有已保存行程，请先规划行程；本轮未创建交通查询。'
        index = context.get('current_trip_index')
        if not reading and pending and pending.slots.get('constraint_trip'):
            previous = pending.slots['constraint_trip']
            target = next((t for t in trips if t['trip_id'] == previous['trip_id']), None)
            if not target or target['version'] != previous['version']:
                return '之前待补充的行程已经变化，请查看最新行程后重新提出调整；未应用旧要求。'
        requested = values.get('trip_index')
        reference = values.get('trip_reference', '')
        if requested is not None and (index is None or requested != index):
            matches = [i for i,t in enumerate(trips,1) if reference and reference in (
                t['trip_id'], t.get('title',''), t.get('spec',{}).get('destination',''))]
            if (type(requested) is int and isinstance(reference,str) and reference in event.content
                    and matches == [requested]):
                index = requested
            else:
                index = None
        if index is None:
            logger.info('Trip constraint clarification: event=%s reason=ambiguous_target',event.event_key)
            question = ('要查看哪一份行程？' if reading else '要调整哪一份行程？') + '请回复行程编号或唯一的城市/标题：\n' + '\n'.join(
                f"{t.get('title','行程')}（{t['trip_id']}）" for t in trips)
            return self._save_clarification(event, claim, ir, question, task=pending, source_text=source_text)
        trip = trips[index-1]
        if reading:
            if values.get('changes') or values.get('save_preference_text') or not (values.get('requested_checks') or values.get('include_trip')):
                return '只读价格检查包含修改或缺少检查对象，本次未修改行程。'
            snapshot=(event.message_reference or {}).get('trip') or trip
            return self._trip_read_reply(event,snapshot,values.get('requested_checks',[]),values.get('include_trip',False),claim,pending)
        if trip.get('_reference_stale'):
            return '所引用的是历史版本，当前行程或草案已有新修改。请引用最新预览继续，或明确要求基于历史内容另建行程；本轮未覆盖新版本。'
        try:
            candidate_spec, summary = apply_constraints(trip.get('spec',{}), values.get('changes'), source_text)
        except ValueError as exc:
            logger.info('Trip constraint rejected: event=%s reason=invalid_patch validation=%s',event.event_key,str(exc))
            return str(exc)
        budget_changes = ([values['changes']['budget']] if values['changes'].get('budget') else [])
        if isinstance(values['changes'].get('budgets'), list):
            budget_changes.extend(values['changes']['budgets'])
        needs_budget_unit = any(not (candidate_spec['budgets'][budget['scope']].get('meals',{}).get(
            f"{budget['scopes'][0]['day_index']}:{budget['scopes'][0]['meal']}",{}) if budget.get('scopes') else
            candidate_spec['budgets'][budget['scope']]).get('unit_text') for budget in budget_changes)
        if needs_budget_unit:
            ir = replace(ir, missing_fields=tuple(dict.fromkeys([*ir.missing_fields,'budget_unit'])))
        elif budget_changes:
            missing = tuple(f for f in ir.missing_fields if f!='budget_unit')
            ir = replace(ir, missing_fields=missing, action='update' if ir.action=='clarify' and not missing else ir.action)
        if any(f != 'trip_ref' for f in ir.missing_fields) or (ir.action == 'clarify' and not ir.missing_fields):
            question = values.get('question') or ('请补充预算币种与计价单位，例如人民币／间／晚。'
                if any('budget' in f for f in ir.missing_fields) else '请补充这次调整中尚不明确的要求。')
            reply = '已关联原行程，已收集：' + '；'.join(summary) + '\n' + question + '\n已有城市、日期和其他要求保留，补充后再生成修改预览。'
            return self._save_clarification(event,claim,ir,reply,task=pending,source_text=source_text,trip=trip)
        preference_update = None
        save_text = values.get('save_preference_text')
        if save_text is not None:
            if (not isinstance(save_text,str) or not save_text or save_text not in source_text
                    or not preference_request(save_text) or not re.search(r'记住|保存|更新偏好|以后.*(?:默认|优先)',save_text)
                    or re.search(r'不要记住|不要保存|别记住|不用记住|不保存|举例|例如|引用',source_text)):
                return '长期保存缺少明确依据，尚未修改行程或偏好。'
            profile_values = PreferencesTool.extract(save_text)
            if not profile_values:
                return '长期偏好暂不支持这组表达，尚未修改行程或偏好。请将本次行程调整与长期偏好分别说明。'
            preference_update = {'values':profile_values,'version':self.store.preferences.snapshot(event)['version']}
        if mode == 'preview':
            return '已识别对原行程的约束调整；当前仅解析预览，尚未保存行程或偏好。'
        if trip.get('_new_trip'):
            task=trip['_draft_task']
            try:candidate,_=self._edit_candidate(event,task.slots['candidate_trip'],values['changes'],source_text)
            except TripPlanError as error:
                if error.code!='meal_window_conflict':raise
                return self._save_clarification(event,claim,replace(ir,missing_fields=('meal_window',)),str(error),task=pending,source_text=source_text,trip=trip)
            slots=dict(task.slots)
            slots.pop('semantic_task',None)
            if values.get('requested_checks'):slots['price_check_text']=self.price_check_text(task.slots['candidate_trip'],values['requested_checks'])
            if pending:slots['semantic_task']={'task_id':pending.task_id,'version':pending.version}
            return self._preview_candidate(event,claim,candidate,task=task,slots=slots,
                quote_requested=bool(values['changes'].keys() & {'accommodation','hotel_stay'}))
        logger.info('Trip constraint dispatch: event=%s trip=%s version=%s fields=%s',
                    event.event_key,trip['trip_id'],trip['version'],sorted(values['changes']))
        try:
            return self.update_constraints(event, claim, trip['trip_id'], trip['version'], values['changes'],
                source_text=source_text, preference_update=preference_update, semantic_task=pending,
                expected_draft=trip.get('_draft_key'),requested_checks=values.get('requested_checks',()))
        except TripPlanError as error:
            if error.code!='meal_window_conflict':raise
            return self._save_clarification(event,claim,replace(ir,missing_fields=('meal_window',)),str(error),task=pending,source_text=source_text,trip=trip)

    def handle_intent(self, event, claim, ir, *, mode='execute'):
        trips,context=trip_context(self.store,event)
        index=context.get('current_trip_index')
        trip=trips[index-1] if index else None
        if any(item.operation == 'view_current_trip' for item in ir.operations):
            reference=event.message_reference or {}
            if reference.get('trip'):return self.format_trip(reference['trip'],preview=reference.get('is_preview',False))
            return self.view(event,trip) if trip else self.list_trips(event)
        if trip is None:
            if not trips:
                return '还没有找到你在本群创建的行程，请先规划或查看行程。'
            return '你有多份行程，请先使用“查看行程”并说明要修改哪一份。'
        if trip.get('_reference_stale') or context.get('trip_reference_unavailable'):
            return '所引用版本已不是当前可编辑版本，请引用最新预览继续；本轮未覆盖新修改。'
        remove_ops = [item for item in ir.operations if item.domain == 'trip' and item.operation == 'remove_activity']
        if not remove_ops:
            return None
        values = remove_ops[0].values
        activity = values.get('activity') if isinstance(values.get('activity'), dict) else {}
        name = activity.get('name') or values.get('entity') or values.get('activity_name') or values.get('attraction_name') or ''
        date_text = activity.get('date_text') or values.get('date_text') or values.get('date') or values.get('visit_date') or ''
        if not name:
            source_names = [place['name'] for place in trip['sources'].values()
                            if place.get('name') and place['name'] in event.content]
            if len(set(source_names)) == 1:
                name = source_names[0]
        if not name:
            return self._save_clarification(event, claim, ir, '请说明要从行程中删除的具体地点。',
                                            source_text=event.content)
        target_date = ''
        if date_text:
            now = event.occurred_at or (self.clock() if self.clock else datetime.now().astimezone())
            parsed = parse_reminder_time(date_text, now)
            if parsed.error or not parsed.day:
                return parsed.error or '请补充明确的行程日期。'
            target_date = parsed.day
        matches = []
        for day in trip['plan']['days']:
            if target_date and day.get('date') != target_date:
                continue
            for activity_value in day['activities']:
                place = trip['sources'][activity_value['poi_id']]
                if place['name'] == name or name in place['name']:
                    matches.append(day['day_index'])
        if len(matches) != 1:
            return '行程中没有唯一匹配的地点和日期，请补充完整地点名称或日期。'
        if mode == 'preview':
            return '已识别删除行程活动；当前仅解析预览，尚未生成或保存草稿。'
        return self.remove_activity(event, claim, trip, matches[0], name)

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

def trip_context(store, event, pending=None):
    repo = getattr(store, 'trips', None)
    ids = re.findall(TRIP_ID,event.content)
    target=''
    if len(set(ids))==1:target=ids[0]
    elif not ids:target=(event.message_reference or {}).get('trip',{}).get('trip_id','')
    trips = tuple(repo.list_for_owner(event)) if repo else ()
    if target and repo and not any(t['trip_id']==target for t in trips):
        saved=repo.get(event,target)
        if saved and saved['status']=='active':trips=(*trips,saved)
    if target:trips=tuple(t for t in trips if t['trip_id']==target)
    if hasattr(repo, 'draft_history'):
        enriched = []
        for trip in trips:
            draft = TripTool.read_draft(store, event, trip)
            trip = {**trip, '_draft_key': (draft['task'].task_id,draft['task'].version) if draft else ('',0)}
            if draft and draft['task'].slots.get('expected_trip_version') == trip['version']:
                trip.update({key:draft['candidate'][key] for key in ('spec','plan','sources')})
                trip['_draft_applied']=True
            enriched.append(trip)
        trips = tuple(enriched)
    drafts=repo.current_drafts(event,trip_id=target) if repo and hasattr(repo,'current_drafts') else ()
    known={trip['trip_id'] for trip in trips}
    for task in drafts:
        candidate=task.slots['candidate_trip']
        if candidate['trip_id'] not in known:
            trips=(*trips,{**deepcopy(candidate),'_new_trip':True,'_draft_task':task,
                          '_draft_key':(task.task_id,task.version)})
            known.add(candidate['trip_id'])
    candidates = []
    for index, trip in enumerate(trips, 1):
        spec = trip.get('spec', {})
        candidates.append({'index': index, 'title': trip.get('title', ''),
            'city': spec.get('destination', ''), 'start_date': spec.get('start_date', ''),
            'end_date': spec.get('end_date', ''), 'date_is_tentative': spec.get('date_is_tentative', False),
            'days': spec.get('day_count'), 'version': trip.get('version',1)+int(bool(trip.get('_draft_applied'))),'is_preview':bool(trip.get('_new_trip') or trip.get('_draft_applied'))})
    selected = trips[0] if len(trips) == 1 else None
    ids = re.findall(r'TR-[a-f0-9]{12}', event.content)
    if ids:
        selected = next((t for t in trips if len(set(ids)) == 1 and t['trip_id'] == ids[0]), None)
    elif event.message_reference:
        reference=event.message_reference;source=reference.get('trip')
        if source:selected=next((t for t in trips if t['trip_id']==source['trip_id']),None)
        elif reference.get('trip_snapshot_missing'):selected=None
        if selected and source:
            key=selected.get('_draft_key',('',0));quoted=reference.get('draft',{})
            selected['_reference_stale']=(any(selected.get(k)!=source.get(k) for k in ('spec','plan')) or
                selected.get('version',1)+int(bool(selected.get('_draft_applied')))!=source.get('version',1) or
                bool(key[0] and key!=(quoted.get('task_id'),quoted.get('version'))))
    elif event.reply_to_id and hasattr(repo, 'referenced'):
        referenced = repo.referenced(event)
        selected = next((t for t in trips if referenced and t['trip_id'] == referenced['trip_id']), selected)
    elif pending and pending.slots.get('constraint_trip'):
        selected = next((t for t in trips if t['trip_id'] == pending.slots['constraint_trip']['trip_id']), None)
    context = {'trips': candidates}
    if event.message_reference and (event.message_reference.get('trip') or event.message_reference.get('trip_snapshot_missing')) and not selected and not ids:
        context['trip_reference_unavailable']=True
    if selected:
        context['current_trip_index'] = next(i for i,t in enumerate(trips,1) if t['trip_id'] == selected['trip_id'])
        spec = selected.get('spec', {})
        context['current_trip'] = {k: spec[k] for k in ('destination','start_date','end_date','day_count','date_is_tentative',
            'gentle','pace','transport_text','transport_preferences','must_visit','preferences',
            'hotel_requested','accommodation_text','hotel_preferences','hotel_stay','restaurant_requested','dining_text',
            'dining_preferences','budget_text','budgets') if k in spec}
        context['current_trip'].update({k:deepcopy(spec[k]) for k in ('city_windows','planning_window') if k in spec})
        context['current_trip']['activities'] = [
            {'day': d.get('day_index'), 'places': [selected.get('sources',{}).get(a['poi_id'],{}).get('name','')
                for a in d.get('activities',[])]} for d in selected.get('plan',{}).get('days',[])]
        context['current_trip']['research_available'] = bool(spec.get('source_context') or spec.get('research_context'))
        if spec.get('budgets'):context['current_trip'].pop('budget_text',None)
        context['current_trip']['is_preview']=bool(selected.get('_new_trip') or selected.get('_draft_applied'))
        if spec.get('intercity_request'):
            context['current_trip']['intercity_request']=spec['intercity_request']
            context['current_trip']['intercity_summary']={key:{'status':leg.get('status'),'selected_seat':leg.get('selected_seat'),
                'selected':{k:v for k,v in leg.get('selected',{}).items() if k in
                    {'train_code','origin_station','destination_station','departure_at','arrival_at'}}}
                for key,leg in selected['plan'].get('intercity',{}).items() if key in {'outbound','inbound'}}
    if pending and pending.slots.get('new_trip_pending') and pending.slots.get('candidate_trip'):
        draft=pending.slots['candidate_trip']
        context['pending_trip']={'destination':draft['spec'].get('destination'),'start_date':draft['spec'].get('start_date'),
            'day_count':draft['spec'].get('day_count'),'intercity_request':draft['spec'].get('intercity_request',{}),'is_preview':True}
    if pending and pending.slots.get('semantic_ir'):
        context['pending_changes']={'operations':pending.slots['semantic_ir'].get('operations',[]),
                                    'missing_fields':list(pending.missing_slots)}
    preferences = getattr(store, 'preferences', None)
    if preferences:
        context['long_term_defaults'] = preferences.defaults(event)['values']
    return trips, context


def planning_request(text):
    return text.strip() in {'查看方案草稿','我的方案草稿'} or bool(re.fullmatch(r'删除方案草稿\s+P-[a-f0-9]{12}',text.strip()))


class PlanningArtifacts:
    def __init__(self,repository):
        self.repository=repository


    def handle(self,event,claim=None):
        text=event.content.strip()
        if not planning_request(text):
            return None
        self.ensure_history(event)
        if text in {'查看方案草稿','我的方案草稿'}:
            plans=self.repository.list_for_owner(event)
            if not plans:
                return '当前会话还没有方案草稿。探索性规划和联网研究方案会出现在这里。'
            return '\n'.join(f"方案 {p['ordinal']}：{p['title']}（{p['artifact_id']}，版本 {p['version']}）"
                for p in plans)
        artifact_id=re.search(r'P-[a-f0-9]{12}',text)[0]
        artifact=self.repository.get(event,artifact_id)
        if not artifact or artifact['status']!='active':
            return '没有找到当前会话中可删除的这份方案草稿。'
        try:
            self.repository.delete(event,artifact_id,artifact['version'])
        except PlanningConflict as error:
            return str(error)
        return '已删除方案草稿；已保存行程、研究报告和其他方案不受影响。'


    def capture(self, event, assistant_content, *, kind='exploratory', destination='', duration_options=None):
        if self.repository.store.delivery.get_event_tool_result('trip-presentation:'+event.event_key):return None
        metadata = proposal_metadata(event.content, assistant_content, kind=kind,
                                     destination=destination, duration_options=duration_options)
        return self.repository.capture(event, assistant_content, **metadata) if metadata else None

    def ensure_history(self, event):
        self.revalidate(event)
        rows = self.repository.history_rows(event)
        for row in rows:
            historical = ChatEvent(event.platform, event.channel, row['user_msg_id'].split(':')[-1],
                                   event.scope_id, event.sender_id, row['user_content'])
            self.capture(historical, row['assistant_content'], kind=row['kind'],
                duration_options=plan_durations(row['user_content'], '') if row['kind']=='research_backed' else None)
        if rows:
            self.repository.advance_history(event, rows[-1]['id'])

    def revalidate(self, event, limit=50):
        repairs = []
        for row in self.repository.stale_metadata(event, CAPTURE_VERSION, limit):
            metadata = proposal_metadata(row['user_content'] or '', row['content'], kind=row['kind'])
            destination = metadata['destination'] if metadata else row['destination']
            options = metadata['duration_options'] if metadata else json.loads(row['duration_options_json'])
            title = metadata['title'] if metadata else row['title']
            changed = not metadata or (destination,options,title) != (row['destination'],json.loads(row['duration_options_json']),row['title'])
            repairs.append({'artifact_id':row['artifact_id'],'version':row['version'],
                'status':'active' if metadata else 'invalid','destination':destination,'duration_options':options,
                'title':title,'changed':changed,'reason':'metadata_repaired' if metadata else 'insufficient_plan_evidence'})
        self.repository.repair_metadata(event, repairs, CAPTURE_VERSION)

