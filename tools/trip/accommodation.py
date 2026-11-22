"""A scoped goal for the existing agent loop; all price and preview facts remain domain-owned."""
from copy import deepcopy
from dataclasses import replace
from datetime import date,timedelta
import json

from core.execution_outcome import TravelDecision
from core.hotel_contract import merge_hotel_state, adult_counts, date_proposal,accommodation_read_only,NEW_RUN_COMMAND,RUN_LIMIT_CODES
from core.model_budget import MODEL_RUN
from core.tasks import TaskUpdate
from core.tool_result import AgentToolContext, ToolResult, DEFAULT_CATALOG
from tools.hotel_prices.quotes import fingerprint, evaluate
from tools.trip.constraints import apply_constraints
from tools.trip.planner import assess_hotels
from tools.hotel_prices.catalog import hotel_brand_targets,match_hotel_brand

GOAL_VERSION='accommodation-goal-v1'
PREVIEW='preview_accommodation_candidates'


def bind_candidate(tool,event,task=None):
    """Reload owned versions rather than copying a trip into the coordination task."""
    from tools.trip.tool import trip_context
    goal=task.slots.get('accommodation_goal',{}) if task else {}
    binding=goal.get('binding',{})
    if task and task.task_type=='itinerary':
        binding={**binding,'draft_id':task.task_id,'draft_version':task.version}
    if binding.get('draft_id'):
        with tool.store._connect() as db:
            state=tool.store.trips.draft_state(db,event,binding['draft_id'],captured_version=binding['draft_version'],now=tool.clock())
        if state['view']['state'] not in {'ready','expired'}:raise ValueError('来源草案已放弃、被替代或保存，不能恢复旧住宿目标。')
        base=deepcopy(state['task'].slots['candidate_trip'])
        return base,state['current'],state['task'],binding
    if binding.get('trip_id'):
        current=tool.store.trips.get(event,binding['trip_id'])
        if not current or current['status']!='active' or current['version']!=binding['trip_version']:
            raise ValueError('行程版本已经变化，请引用最新版本继续；旧报价仍可本地读取。')
        if tool.store.trips.current_drafts(event,current['trip_id']):raise ValueError('已产生新的行程草案，请先引用需要调整的版本。')
        return deepcopy(current),current,None,binding
    reference=event.message_reference or {}
    if reference.get('trip'):
        with tool.store._connect() as db:
            row=db.execute('''SELECT r.result_text,t.task_id,e.task_version FROM event_tool_results r
                JOIN task_event_results e ON e.event_id=r.event_id JOIN travel_tasks t ON t.task_id=e.task_id
                WHERE r.event_id=? AND r.tool_name='trip_presentation' AND t.task_type='itinerary'
                AND t.platform=? AND t.scope_id=? AND t.owner_id=?''',
                (reference.get('event_id'),event.platform,event.storage_scope_id,event.sender_id)).fetchone()
            if not row:raise ValueError('引用的草案不存在或不属于当前会话。')
            state=tool.store.trips.draft_state(db,event,row['task_id'],captured_version=row['task_version'],now=tool.clock())
        if state['view']['state'] not in {'ready','expired','saved'}:raise ValueError('引用的草案已经放弃或被替代。')
        base=json.loads(row['result_text'])['trip'];current=state['current']
        if state['view']['state']=='saved':
            if not current or current['version']!=base['version']:raise ValueError('引用的已保存版本已变化。')
            return deepcopy(base),current,None,{'trip_id':base['trip_id'],'trip_version':current['version']}
        return deepcopy(base),current,state['task'],{'trip_id':base['trip_id'],'trip_version':current['version'] if current else 0,
            'draft_id':row['task_id'],'draft_version':row['task_version']}
    trips,context=trip_context(tool.store,event)
    index=context.get('current_trip_index')
    if not index:raise ValueError('请引用需要调整住宿的行程或草案；目前没有唯一目标。')
    base=deepcopy(trips[index-1]);draft_id,draft_version=base.pop('_draft_key',('',0))
    current=tool.store.trips.get(event,base['trip_id'])
    draft=tool.store.tasks.owned(event,draft_id) if draft_id else None
    return base,current,draft,{'trip_id':base['trip_id'],'trip_version':current['version'] if current else 0,
        **({'draft_id':draft_id,'draft_version':draft_version} if draft_id else {})}


def prepare(tool,event,claim,values,*,task=None,mode='execute',initial=None):
    if mode!='execute':return '已识别住宿推荐/调整目标，当前仅解析预览，未查询或修改。'
    if not getattr(tool,'travel_agent',None) or not tool.hotel_quotes:return '住宿编排或报价服务暂不可用，目标尚未完成。'
    if task and task.slots.get('accommodation_goal',{}).get('version')!=GOAL_VERSION:
        return '住宿目标合同版本已变化，历史结果保留，请重新发起完整要求。'
    try:
        if task and task.task_id:
            owned=tool.store.tasks.owned(event,task.task_id)
            if not owned or owned.version!=task.version or owned.status!='collecting':raise ValueError('住宿目标已取消或版本已变化。')
            task=replace(owned,slots=task.slots)
        base,current,draft,binding=initial or bind_candidate(tool,event,task)
        base={k:deepcopy(v) for k,v in base.items() if not k.startswith('_')}
        goal=deepcopy(task.slots.get('accommodation_goal',{})) if task else {}
        for field,value in goal.get('constraints',{}).items():base['spec'][field]=deepcopy(value)
        changes=values.get('changes',{})
        if changes:base['spec'],_=apply_constraints(base['spec'],changes,event.content)
        if not base['spec'].get('hotel_requested'):base['spec']['hotel_requested']=True
        if 'selection' in values:
            if values['selection']['source'] not in event.content:raise ValueError('指定门店缺少本轮用户原话依据。')
            goal['selection']=deepcopy(values['selection'])
            base['spec'].setdefault('hotel_preferences',{}).pop('selected_poi_id',None)
        if changes.get('accommodation',{}).get('preferred_brands') or changes.get('accommodation',{}).get('preferred_groups'):
            goal.pop('selection',None)
        goal.update(version=GOAL_VERSION,binding=binding,
            allow_preview=values.get('allow_preview',goal.get('allow_preview',False)),
            replace_only_if_budget_mismatch=values.get('replace_only_if_budget_mismatch',goal.get('replace_only_if_budget_mismatch',False)))
        if accommodation_read_only(event.content):goal['allow_preview']=False
        goal.setdefault('initial_request',values.get('request_text',event.content))
        goal['requires_preview']=bool(goal.get('requires_preview') or initial or changes or task and task.task_type=='itinerary')
        # Only validated lodging fields, never a second copy of the itinerary or raw evidence.
        goal['constraints']={k:deepcopy(base['spec'][k]) for k in ('hotel_preferences','hotel_stay','budgets','accommodation_text') if k in base['spec']}
        slots=deepcopy(task.slots) if task else {}
        spec=base['spec'];stay=deepcopy(spec.get('hotel_stay',{}))
        counts=adult_counts('；'.join(spec.get('preferences',[])))
        if 'adults' not in stay and len(counts)==1:stay['adults']=next(iter(counts))
        ids=base['plan'].get('hotel_ids') or [r['poi_id'] for r in base['plan'].get('hotel_investigation',[])]
        if not ids:ids=[r['poi_id'] for r in assess_hotels({**spec,'budgets':{}},base['plan'],base['sources']) if r['status']!='conflict']
        targets={pid:deepcopy(base['sources'][pid]) for pid in ids[:3] if pid in base['sources']}
        goal.setdefault('original_candidate_ids',list(targets))
        preferences=spec.get('hotel_preferences',{})
        if preferences.get('brand_mode')=='required':
            brands=hotel_brand_targets(preferences)
            targets={pid:p for pid,p in targets.items() if match_hotel_brand(p['name'],brands)}
        request={'stay':stay,**({'budget':{k:v for k,v in spec.get('budgets',{}).get('accommodation',{}).items() if k in {'value','unit_text','source'}}} if spec.get('budgets',{}).get('accommodation') else {})}
        if targets:request['targets']=[{'name':p['name'],'city':spec['destination'],'address_hint':p['address']} for p in targets.values()]
        else:request['discovery']={'city':spec['destination']}
        if not slots.get('hotel_request'):slots.update(hotel_request=request,hotel_targets=targets)
        if 'budget' in request:slots['hotel_request']['budget']=deepcopy(request['budget'])
        if changes:
            slots['hotel_request']={**slots['hotel_request'],'stay':stay,**{k:v for k,v in request.items() if k=='budget'}}
            if changes.get('accommodation'):
                slots.update(hotel_request=request,hotel_targets=targets,hotel_target_bindings={},hotel_candidates=[])
        if goal.get('selection'):
            selected=goal['selection']['names']
            slots['hotel_request']={**slots['hotel_request'],'targets':[{'name':name,'city':spec['destination']} for name in selected]}
            slots['hotel_request'].pop('discovery',None)
            slots['hotel_targets']={pid:p for pid,p in slots.get('hotel_targets',{}).items() if p['name'] in selected}
            slots.update(hotel_target_bindings={},hotel_candidates=[])
        if not slots.get('hotel_date_proposal') and not stay.get('check_in') and not spec.get('date_is_tentative') and spec.get('start_date') and spec.get('end_date')>spec['start_date']:
            slots['hotel_date_proposal']=date_proposal({'check_in':spec['start_date'],'check_out':spec['end_date'],'presented':False},source_version=binding)
        slots.update(accommodation_goal=goal,budget_run_key=slots.get('budget_run_key') or MODEL_RUN.get() or fingerprint(event.event_key),
            reference_time=(event.occurred_at or tool.clock()).isoformat(),hotel_collecting=bool(task))
        from tools.research.sources import planning_policy
        slots['source_context']={'provider_policy':planning_policy(slots.get('source_context',spec),event.content,automatic=True)}
        # Constraints accumulated over earlier turns must win over the old source spec.
        for field,value in goal['constraints'].items():spec[field]=deepcopy(value)
        runtime={'base':base,'current':current,'draft':draft,'task':task,'claim':claim,
                 'slots':slots,'snapshots':[],'results':[],'preview':None,'halted':False,'initial':bool(initial),
                 'pending_query':values.get('hotel_query',{})}
        for ref in goal.get('receipt_refs',[]):
            quote=tool.store.research.hotel_quote_for_event(event,ref)
            if quote:runtime['snapshots'].append(quote)
        context=AgentToolContext(event.platform,event.scope_id,event.sender_id,event.event_key,event=event,
            catalog_version=DEFAULT_CATALOG,execution_data={**slots,'accommodation_runtime':runtime})
        decision=TravelDecision('accommodation',('accommodation',),True,
            ('query_hotel_prices',PREVIEW) if goal['allow_preview'] else ('query_hotel_prices',),(),False,'normal')
        message=goal['initial_request']+'\n本轮补充：'+event.content+'\n服务端已绑定目标：'+json.dumps({
            'request':slots['hotel_request'],'preferences':spec.get('hotel_preferences',{}),'allow_preview':goal['allow_preview'],
            'replace_only_if_budget_mismatch':goal['replace_only_if_budget_mismatch'],'selection':goal.get('selection'),
            'attempted':goal.get('attempted',[]),'receipt_refs':goal.get('receipt_refs',[])},ensure_ascii=False)
        agent=tool.travel_agent
        result=agent.run(message,tool_context=context,decision_override=decision,
            executor_override=lambda n,a,c:execute(tool,n,a,c,agent.tool_executor))
        if runtime['preview'] is not None:return runtime['preview']
        slots=runtime['slots'];goal=slots['accommodation_goal']
        if result.budget_block:slots['model_budget_block']=result.budget_block
        eligible=eligible_candidates(tool,runtime)
        old=set(goal.get('original_candidate_ids',base['plan'].get('hotel_ids',[])))
        verified=old and old<=set(eligible)
        paused=result.status=='needs_input'
        completed=bool(not goal['requires_preview'] and verified or not goal['allow_preview'] and runtime['results'] and not paused and not runtime['halted'])
        reply=('原候选已按适用参考报价核对，符合预算及硬约束，保留原住宿，无需替换。' if verified and not changes else
               '本轮住宿目标尚未完成；原住宿保留，未保存任何调整。' if goal['allow_preview'] else '本轮只读核对结果：')
        if paused:reply+='\n'+result.reply
        else:reply+='\n'+'\n\n'.join(dict.fromkeys(r.data for r in runtime['results']))
        if result.answer_reasons:reply+='\n'+'；'.join(result.answer_reasons)
        if result.budget_block and result.budget_block['code'] in RUN_LIMIT_CODES:
            reply+='\n可明确回复“'+NEW_RUN_COMMAND+'”，建立有关联的新运行；日/月限额不重置。'
        if not completed and not paused:reply+='\n已查范围及失败原因保留；未找到可核价且符合预算的酒店，不自动放宽条件。'
        if task and task.task_type=='itinerary':slots.update(candidate_trip=base,trip_id=base['trip_id'])
        update=TaskUpdate(task.task_type if task else 'hotel','collecting' if not completed or task and task.task_type=='itinerary' else 'completed',
            task.initial_request if task else goal['initial_request'],slots,
            result.missing_slots if paused else (() if completed else ('accommodation_goal',)),
            task.task_id if task else '',task.version if task else None)
        if initial:
            if current is None:
                base['plan']['hotel_ids']=[];base['plan']['hotel_recommendations']=[]
                base['plan']['hotel_investigation']=[{'poi_id':pid,'price_status':'unknown'} for pid in targets]
            slots.update(candidate_trip=base,trip_id=base['trip_id'],expected_trip_version=current['version'] if current else 0,
                new_trip_pending=current is None,draft_context_complete=True)
            reply=tool.format_trip(base,preview=True)+'\n\n'+reply
            return tool.store.trips.commit(event,claim,replace(update,task_type='itinerary',status='collecting'),reply,
                expected_trip=(current['trip_id'],current['version']) if current else None,
                presentation=tool.presentation(base,reply,preview=True),now=tool.clock())
        tool.store.tasks.prepare_result(event,claim,update,reply)
        return reply
    except (ValueError,LookupError) as error:
        return str(error)+' 本次未保存行程；已取得资料保留。'


def eligible_candidates(tool,runtime):
    base=runtime['base'];places=deepcopy(base['sources']);result={}
    for quote in runtime['snapshots']:
        places.update(quote.get('targets',{}))
        passed={o['amap_poi_id'] for o in evaluate(base['spec'],quote,tool.clock(),places=places)
            if o['status']=='reference_match' or not base['spec'].get('budgets',{}).get('accommodation') and o['status']=='reference_available'}
        rows=assess_hotels(base['spec'],{**base['plan'],'hotel_quotes':quote},places)
        for row in rows:
            if row['poi_id'] in passed and row['status']!='conflict':result[row['poi_id']]=row
    return result


def execute(tool,name,arguments,context,executor):
    data=context.execution_data or {};runtime=data.get('accommodation_runtime')
    if not runtime:return executor(name,arguments,context)
    if name!= 'query_hotel_prices':return executor(name,arguments,context)
    if runtime['halted']:return ToolResult('failed',name,error_code='goal_paused',data='来源故障或额度阻断已暂停，不能换店绕过。')
    slots=runtime['slots'];goal=slots['accommodation_goal'];spec=runtime['base']['spec'];arguments=deepcopy(arguments)
    if runtime['pending_query']:
        arguments={**arguments,**runtime.pop('pending_query')};runtime['pending_query']={}
    if 'budget' in arguments:
        fixed=slots['hotel_request'].get('budget',{})
        if any(k in arguments['budget'] and arguments['budget'][k]!=fixed.get(k) for k in ('value','unit_text')):
            return ToolResult('failed',name,error_code='fixed_goal_budget',data='查询工具不能修改已绑定预算；请省略预算字段，真实预算变更须先通过住宿目标的原话校验。')
        arguments.pop('budget')  # A model restatement is not a new source or a budget change.
    if arguments.get('targets') and goal.get('selection') and any(t['name'] not in goal['selection']['names'] for t in arguments['targets']):
        return ToolResult('failed',name,error_code='pinned_hotel',data='本轮必须保留用户明确指定的酒店，不得换成其他门店。')
    discovery=arguments.get('discovery') or (slots['hotel_request'].get('discovery') if not slots['hotel_request'].get('targets') else None)
    if discovery:
        if not goal['allow_preview']:return ToolResult('failed',name,error_code='operation_denied',data='本轮仅核对指定名单，未授权替代搜索。')
        if goal.get('selection'):return ToolResult('failed',name,error_code='pinned_hotel',data='用户指定门店尚需核实，不能擅自替换。')
        if discovery.get('city','').removesuffix('市')!=spec['destination'].removesuffix('市'):
            return ToolResult('failed',name,error_code='wrong_scope',data='只能在已绑定的行程城市寻找住宿。')
        if not runtime['snapshots'] and slots['hotel_request'].get('targets'):
            return ToolResult('failed',name,error_code='query_original_first',data='先核对原候选，预算内原对象不能无故替换。')
        old=set(goal.get('original_candidate_ids',runtime['base']['plan'].get('hotel_ids',[])))
        if old and old<=set(eligible_candidates(tool,runtime)):
            return ToolResult('failed',name,error_code='already_verified',data='原候选已通过预算及硬约束，没有替代搜索的必要。')
        prefs=spec.get('hotel_preferences',{})
        if prefs.get('brand_mode')=='required':
            discovery={**{k:v for k,v in discovery.items() if k in {'city','area'}},
                **{k:prefs[k] for k in ('preferred_brands','preferred_groups') if k in prefs}}
        arguments['discovery']=discovery
        request={k:v for k,v in slots['hotel_request'].items() if k not in {'targets','reference'}}
        request['discovery']=discovery
        slots.update(hotel_request=request,hotel_targets={},hotel_target_bindings={},hotel_candidates=[])
    prefs=spec.get('hotel_preferences',{})
    if prefs.get('brand_mode')=='required' and arguments.get('targets') and any(
            not match_hotel_brand(t['name'],hotel_brand_targets(prefs)) for t in arguments['targets']):
        return ToolResult('failed',name,error_code='wrong_brand_scope',data='本轮查询须保留用户指定的品牌/集团范围。')
    query_context=replace(context,execution_data={**{k:v for k,v in data.items() if k!='accommodation_runtime'},**slots})
    result=executor(name,arguments,query_context)
    runtime['slots']=merge_hotel_state(slots,result.payload or {})
    runtime['base']['spec']['hotel_stay']=deepcopy(runtime['slots'].get('hotel_request',{}).get('stay',{}))
    runtime['slots']['accommodation_goal']['constraints']['hotel_stay']=deepcopy(runtime['base']['spec']['hotel_stay'])
    data.update(runtime['slots'])
    quote=(result.payload or {}).get('snapshot',{})
    if quote and quote.get('source_event_id'):
        runtime['snapshots'].append(quote)
        goal=runtime['slots']['accommodation_goal'];refs=goal.setdefault('receipt_refs',[])
        if quote['source_event_id'] not in refs:refs.append(quote['source_event_id'])
        outcomes={o['amap_poi_id']:o for o in (result.payload or {}).get('offers',[])}
        attempts=goal.setdefault('attempted',[])
        for pid in quote.get('requested_poi_ids',[]):
            row=outcomes.get(pid,{})
            item={'poi_id':pid,'stay':fingerprint(quote.get('stay',{})),'reason':row.get('numeric_comparison') if row.get('status')=='excluded' else 'verified' if row.get('status')=='reference_match' else 'unpriceable',
                'source_ref':quote['source_event_id']}
            if item not in attempts:attempts.append(item)
    runtime['results'].append(result)
    if tool.store.delivery.get_event_status(context.event_id) is not None:
        tool.store.delivery.save_event_tool_result('accommodation-query:'+fingerprint([context.event_id,result.payload.get('request') if result.payload else arguments,goal['binding']]),
            context.event_id,name,arguments,result.to_json())
    if (result.status=='failed' or (result.payload or {}).get('budget_block') or quote.get('submission_status') not in {None,'completed'}
            or quote.get('extraction_status') in {'failed','unknown','not_run','budget_blocked'}):
        runtime['halted']=True
    admitted=eligible_candidates(tool,runtime);old=set(goal.get('original_candidate_ids',runtime['base']['plan'].get('hotel_ids',[])))
    runtime['goal_verified']=bool(not goal['requires_preview'] and old and old<=set(admitted))
    if goal.get('selection') and result.status=='completed' and not runtime['goal_verified']:
        runtime['halted']=True
    return replace(result,payload={'admitted_ids':list(admitted),'goal_complete':runtime['goal_verified'],
        'targets':[{k:p.get(k) for k in ('id','name','address')} for p in quote.get('targets',{}).values()],
        'price_outcomes':[{k:o.get(k) for k in ('amap_poi_id','hotel_name','status','numeric_comparison','issues')} for o in (result.payload or {}).get('offers',[])],
        'source_ref':quote.get('source_event_id'),'continue_with_new_candidates':bool(goal['allow_preview'] and not goal.get('selection') and not runtime['halted'] and not runtime['goal_verified'])})


def preview(tool,arguments,context):
    runtime=(context.execution_data or {}).get('accommodation_runtime')
    def reject(code,text):return ToolResult('failed',PREVIEW,error_code=code,data=text)
    if not runtime or not runtime['slots']['accommodation_goal'].get('allow_preview'):return reject('operation_denied','本轮未授权住宿预览。')
    if runtime['halted']:return reject('goal_paused','来源或额度已暂停，不能生成完成预览。')
    ids=arguments['candidate_ids'];eligible=eligible_candidates(tool,runtime);base=runtime['base'];goal=runtime['slots']['accommodation_goal']
    if len(ids)!=len(set(ids)) or any(pid not in eligible for pid in ids):return reject('missing_evidence','候选尚未通过适用参考报价、预算或硬约束核验；请先核价，不以未知价凑数。')
    old=set(goal.get('original_candidate_ids',base['plan'].get('hotel_ids',[])));keep=old&set(eligible)
    if not keep<=set(ids):return reject('keep_verified','已核对且符合要求的原候选必须保留。')
    owned_places={**base['sources'],**{pid:p for q in runtime['snapshots'] for pid,p in q.get('targets',{}).items()}}
    if goal.get('selection') and any(owned_places.get(pid,{}).get('name') not in goal['selection']['names'] for pid in ids):
        return reject('pinned_hotel','不能替换用户明确指定的门店。')
    try:
        if not runtime.get('initial'):
            bind_candidate(tool,context.event,runtime['task']) if runtime['task'] else bind_candidate(tool,context.event)
        draft=runtime['draft'];current=runtime['current']
        if draft and draft.is_expired(tool.clock()):return reject('expired_draft','来源草案已过期；只读核价结果保留，请先重新核对最新草案再生成预览。')
        candidate=deepcopy(base);candidate['version']=(current['version']+1) if current else 1
        stay=candidate['spec'].get('hotel_stay',{});spec=candidate['spec']
        if (spec.get('start_date') and stay.get('check_in','')<spec['start_date'] or spec.get('end_date')
                and stay.get('check_out','')>(date.fromisoformat(spec['end_date'])+timedelta(days=1)).isoformat()):
            return reject('stay_outside_trip','住宿日期超出当前行程；未更改旅行日期，请先明确住宿范围。')
        quotes=runtime['snapshots'];places=candidate['sources']
        for quote in quotes:places.update(deepcopy(quote.get('targets',{})))
        # Keep each evidence span/source identity attached to its own validated product.
        merged=deepcopy(quotes[-1]);merged.pop('extracted_facts',None)
        merged.update(offers=[{**deepcopy(o),'source_ref':q['source_event_id']} for q in quotes
            for o in evaluate(spec,q,tool.clock(),places=places) if o['amap_poi_id'] in ids and o['status'] in {'reference_match','reference_available'}],
            targets={pid:places[pid] for pid in ids},requested_poi_ids=ids,requested_hotels={pid:places[pid]['name'] for pid in ids},
            source_refs=[q['source_event_id'] for q in quotes],raw_text='',reference_clues=[],unavailable_prices=[])
        candidate['plan'].update(hotel_ids=ids,hotel_recommendations=[eligible[pid] for pid in ids],hotel_quotes=merged,hotel_investigation=[])
        from tools.trip.planner import HOTEL_POLICY_VERSION
        candidate['plan']['hotel_recommendation_policy']=HOTEL_POLICY_VERSION
        if runtime['task'] and runtime['task'].task_type=='hotel':
            completed={'task_id':runtime['task'].task_id,'version':runtime['task'].version}
        else:completed=None
        reply=tool._preview_candidate(context.event,runtime['claim'],candidate,current=current,
            task=draft,expected_draft=(base['trip_id'],draft.task_id if draft else '',draft.version if draft else 0),
            slots={'completed_accommodation_task':completed} if completed else {},write_answer=False,
            accommodation_ready=True,prefix='仅调整住宿候选；按已取得的适用参考报价符合预算，不保证库存或成交价。\n')
        runtime['preview']=reply
        return ToolResult('completed',PREVIEW,data=reply,effect_status='committed',operation_key='accommodation-preview:'+context.event_id)
    except (ValueError,LookupError) as error:return reject('stale_reference',str(error))
