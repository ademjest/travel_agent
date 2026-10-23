"""Resolve validated Intent IR into existing deterministic domain services."""
from dataclasses import replace
from datetime import datetime
import json
import logging
import re
import hashlib

from agents.intent_compiler import IntentCompiler
from core.tasks import TaskUpdate, READ_TASK_TYPES, location_answer, route_correction
from core.execution_outcome import TaskContext, ExecutionOutcome, TravelReadRequest
from core.tool_result import ToolResult
from core.tool_execution import retry_read
from tools.agent_tools import AgentToolContext
from core.intent_ir import IntentIR, IntentOperation, to_jsonable, READ_OPERATION_FIELDS
from core.commands import parse_command
from core.rule_intent import RuleResult, parse_rules, enabled as rules_enabled, unsafe_command_tail, compound_boundary, VERSION as RULE_VERSION
from core.execution_scope import ensure_execution_active
from core.trip_updates import apply_constraints
from services.preference_service import PreferenceService, preference_request
from services.reminder_time import is_time_answer, parse_reminder_time
from core.research_contract import project_request, ResearchRequestError, SCHEMA_VERSION
from services.research_context import resolve_plan, research_dates

logger = logging.getLogger(__name__)


def semantic_candidate(text: str) -> bool:
    """Only explicit commands bypass the compiler; natural language has no keyword gate."""
    stripped = text.strip()
    if unsafe_command_tail(stripped):
        return True
    if re.fullmatch(r'(?:你好|您好|嗨|hello|hi|谢谢|多谢|好的|好|收到)[！!。 ]*', stripped, re.I):
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


class SemanticTaskService:
    def __init__(self, store, compiler: IntentCompiler, trip_service=None,
                 reminder_service=None, research_service=None, travel_service=None,
                 mode='execute', clock=None, tool_executor=None):
        self.store, self.compiler = store, compiler
        self.trip_service, self.reminder_service = trip_service, reminder_service
        self.research_service = research_service
        self.travel_service = travel_service
        self.tool_executor = tool_executor
        self.mode = mode if mode in {'shadow', 'preview', 'execute'} else 'shadow'
        self.clock = clock

    def shadow(self, event):
        if not semantic_candidate(event.content):
            return None
        ir = self.compiler.try_compile(event.content)
        if ir is not None:
            logger.info('Semantic shadow IR: event=%s action=%s operations=%s', event.event_key, ir.action,
                        [(item.domain, item.operation) for item in ir.operations])
        return ir

    def handle(self, event, claim):
        if claim is not None and getattr(claim, 'prepared_reply', None) is not None:
            return claim.prepared_reply
        if event.trip_edit is not None:
            if self.mode == 'shadow':
                return None
            if self.mode == 'preview':
                return '已识别字段调整；当前仅解析预览，尚未生成或保存草稿。'
            if event.platform != 'web' or self.trip_service is None:
                return '此入口不支持行程字段操作。'
            from core.trip_diff import edit_text
            data=event.trip_edit
            if edit_text(data['field'],data['value']) != event.content:
                return '字段内容不一致，请刷新后重试。'
            current=self.store.trips.get(event,data['trip_id'])
            if not current or current['version'] != data['version']:
                return '行程已变化或不属于本会话，请刷新后重新修改。'
            draft=self.store.trips.draft(event,current)
            if (data.get('draft_id'),data.get('draft_version')) != ((draft['task'].task_id,draft['task'].version) if draft else (None,None)):
                return '草稿已变化，请刷新后重新修改。'
            ir=parse_rules(event.content).ir
            return self.trip_service.update_constraints(event,claim,current['trip_id'],current['version'],ir.operations[0].values['changes'],
                expected_draft=(draft['task'].task_id if draft else '',draft['task'].version if draft else 0))
        pending = self._pending(event)
        reminders = self._pending_reminders(event)
        if self.mode == 'execute' and not pending and not reminders and self.trip_service is not None:
            from services.trip_service import reference_date_answer
            if reference_date_answer(event.content.strip()):
                dated_trips = self.store.trips.list_for_owner(event)
                if len(dated_trips) == 1 and dated_trips[0]['spec'].get('date_is_tentative'):
                    return self.trip_service.handle(event, claim)
        rule = parse_rules(event.content) if rules_enabled() and not pending and not reminders and not event.attachments else None
        if hasattr(self.store,'get_event_tool_result'):
            frozen=self.store.get_event_tool_result('rule-plan:'+hashlib.sha256(event.event_key.encode()).hexdigest())
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
        read_tasks = (self.store.tasks.recent(event.platform, event.storage_scope_id, event.sender_id,
                      reply_to_id=event.reply_to_id,**({'include_failed_reads':True} if correction else {})) if hasattr(self.store, 'tasks') else ())
        read_context = TaskContext.for_read(event, read_tasks)
        if correction and not re.search(r'[；;]|顺便|同时|并且|还有|查询天气', event.content):
            if read_context.task is None:
                return '请回复需要更正的那条路线消息，或重新提供另一端地点；不会猜测要修改哪条路线。'
            if self.mode=='shadow':
                return None
            domains=read_context.slots.get('intents',[read_context.task.task_type])
            operations=tuple(IntentOperation(domain,'search' if domain=='places' else 'query',
                {field:read_context.slots[field] for field in READ_OPERATION_FIELDS[domain] if read_context.slots.get(field)})
                for domain in domains if domain in READ_OPERATION_FIELDS)
            corrected=IntentIR(1,'read',operations,source_spans=tuple(correction.values()))
            if len(operations)==1:
                return self._travel_lookup(event,corrected,claim,read_context)
            request=TravelReadRequest.from_ir(corrected,read_context)
            return request if self.mode=='execute' else '已识别路线地点更正；当前仅预览，尚未重新查询。'
        waiting_reads = [value for value in read_tasks if value.task_type in READ_TASK_TYPES
                         and value.status == 'collecting']
        if len(waiting_reads) > 1 and location_answer(event.content) and read_context.task is None:
            return '有多个查询正在等待补充，请回复对应的问题后再提供地点。'
        if read_context.task is not None:
            compile_text = read_context.message
        if pending is not None:
            compile_text = pending.slots.get('source_text', pending.initial_request) + '\n用户补充：' + event.content
        if rule and rule.ir and rule.ir.action == 'read' and read_context.task is None:
            if self.mode == 'shadow':
                return None
            if len(rule.ir.operations)==1:
                return self._travel_lookup(event,rule.ir,claim,read_context)
            return self._rule_reads(event, claim, rule.ir)
        if hasattr(self.store,'planning'):
            self.store.planning.ensure_history(event)
        trips, trip_context = self._trip_context(event, pending)
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
        if read_context.task is not None:
            context['read_task'] = {'type': read_context.task.task_type, 'slots': read_context.slots,
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
                return value is None or (len(value.operations)<2 and not
                    (value.operations and value.operations[0].domain=='trip'
                     and value.operations[0].operation=='update_constraints'
                     and (len(value.operations[0].values.get('changes',{}))>1
                          or value.operations[0].values.get('save_preference_text'))))
            if incomplete(ir):
                ir=self.compiler.try_compile(compile_text,context=json.dumps(context,ensure_ascii=False)+
                    '\n请检查完整请求中的全部事项，不要只返回第一项；不能合并时保留所有operations。')
                if incomplete(ir):
                    return None if self.mode=='shadow' else '这条消息包含多个部分，尚未可靠识别全部事项，本次没有执行。请分别写明每项需求后重试。'
        if ir is None and unsafe_command_tail(event.content):
            return '未能完整识别这条查询及附加要求，本次尚未执行，请重新说明全部需求。'
        if ir is not None and len(ir.operations) > 1:
            pairs = [(o.domain,o.operation) for o in ir.operations]
            reads = all(d in READ_OPERATION_FIELDS and op == ('search' if d == 'places' else 'query') for d,op in pairs)
            linked = (len(pairs) == 2 and set(pairs) == {('trip','remove_activity'),('reminder','cancel_linked_reminder')})
            if not reads and not linked:
                if self.mode == 'shadow':
                    return None
                labels = {'trip':'行程调整','reminder':'提醒','research':'攻略研究','booking':'预约',
                          'weather':'天气','forecast':'天气预报','route':'路线','transit':'公共交通'}
                items = '、'.join(f'{i}. {labels.get(o.domain,o.domain)}' for i,o in enumerate(ir.operations,1))
                return self._save_clarification(event,claim,ir,
                    '已识别这些事项：'+items+'。这组操作暂不能一起执行，本次尚未执行任何一项。请明确先处理的事项及要求。',
                    source_text=compile_text,task=pending)
        if self.mode == 'shadow' and reminders:
            return None
        if ir is None and reminders and not is_time_answer(event.content):
            return '这次未能可靠解析你的补充，提醒尚未完成设置。请再说明要补充的时间、事项，或明确结束设置。'
        if ir is not None and ir.action == 'clarify' and not ir.operations and reminders:
            return '提醒设置仍在等待补充。请说明具体时间或要继续的事项，不明确的时间不会自动创建提醒。'
        recovered = self._recover_ir(event, pending)
        if (recovered is not None and ir is not None
                and any(item.domain in {'weather','forecast','route','traffic','places','walking','transit'}
                        for item in ir.operations)
                and any(item.domain == 'trip' for item in recovered.operations)):
            ir = recovered
        if ir is not None and any(item.domain == 'reminder' and item.operation == 'continue_pending' for item in ir.operations):
            if self.mode == 'shadow':
                return None
            return self._continue_reminder(event, claim, ir, reminders)
        if ir is not None and any(item.domain == 'research' and item.operation == 'search_travel_guides' for item in ir.operations):
            if self.mode == 'shadow':
                return None
            return self._research(event,claim,ir)
        if ir is not None and any(item.domain in {'weather','forecast','route','traffic','places','walking','transit'}
                                  for item in ir.operations):
            if self.mode == 'shadow':
                return None
            if (len(ir.operations)>1 or re.search(r'带伞|撑伞|淋雨|风险|安全|比较|分析',event.content)) and not re.search(r'不要|取消|确认|预约|提醒',event.content):
                request=TravelReadRequest.from_ir(ir,read_context)
                if request is not None:
                    if self.mode=='preview':
                        return '已识别组合旅行查询；当前仅解析预览，尚未调用数据源。'
                    return request
            return self._travel_lookup(event, ir, claim, read_context)
        if ir is not None and any(item.domain == 'trip' and item.operation == 'create_from_plan_artifact' for item in ir.operations):
            if self.mode == 'shadow':
                return None
            return self._create_from_plan(event,claim,ir,artifacts,pending,compile_text)
        if ir is not None and any(item.domain == 'trip' and item.operation == 'update_constraints' for item in ir.operations):
            if self.mode == 'shadow':
                return None
            return self._update_trip(event, claim, ir, trips, trip_context, pending, compile_text, artifacts)
        if ir is None and trips and self.mode != 'shadow':
            return '这次未能可靠识别你的意图，原行程和偏好未改变。请再说明要调整行程还是查询独立路线。'
        if ir is None or not self._supported(ir):
            ir = recovered
        if ir is None:
            return None
        logger.info('Semantic IR accepted: event=%s mode=%s source=%s action=%s operations=%s',
                    event.event_key, self.mode, 'recovery' if ir is recovered else 'model', ir.action,
                    [(item.domain, item.operation) for item in ir.operations])
        if self.mode == 'shadow':
            logger.info('Semantic shadow mode ignored actionable IR: event=%s action=%s operations=%s',
                        event.event_key, ir.action, [(item.domain, item.operation) for item in ir.operations])
            return None
        if ir.action == 'clarify':
            if len(ir.operations) == 1 and ir.operations[0].domain == 'reminder' and ir.operations[0].operation == 'create':
                return self._reminder(event, claim, ir)
            question = '请补充这次操作涉及的地点、日期或事项。'
            if ir.missing_fields and any('地点' in field or 'name' in field for field in ir.missing_fields):
                question = '请说明要从行程中删除的具体地点。'
            return self._save_clarification(event, claim, ir, question, task=pending,
                                            source_text=compile_text)
        if ir.action == 'none':
            return None
        if ir.action == 'confirm':
            return self._confirm(event, claim, ir)
        if any(item.domain == 'trip' for item in ir.operations):
            return self._trip(event, claim, ir)
        if any(item.domain == 'reminder' for item in ir.operations):
            return self._reminder(event, claim, ir)
        return None

    def _rule_reads(self, event, claim, ir):
        """One root commit; operation checkpoints retain every read, including same-domain reads."""
        if self.mode == 'preview':
            return f'已识别 {len(ir.operations)} 项查询；当前仅预览，尚未调用数据源。'
        if self.travel_service is None:
            return '查询服务暂不可用，本次未执行查询。'
        tools = {'weather':'get_current_weather','route':'get_driving_route'}
        key = 'rule-plan:' + hashlib.sha256(event.event_key.encode()).hexdigest()
        ensure_execution_active()
        frozen = self.store.get_event_tool_result(key)
        if frozen:
            from core.intent_ir import parse_intent_ir
            ir = parse_intent_ir(json.loads(frozen)['ir'],event.content)
        elif claim is not None:
            self.store.save_event_tool_result(key,event.event_key,'rule_plan',{},
                json.dumps({'version':RULE_VERSION,'ir':to_jsonable(ir)},ensure_ascii=False))
        rows, receipts = [], []
        for index, operation in enumerate(ir.operations):
            ensure_execution_active()
            op_key = key + ':' + str(index)
            cached = self.store.get_event_tool_result(op_key)
            if cached:
                result = ToolResult(**json.loads(cached))
            elif self.tool_executor:
                result = self.tool_executor(tools[operation.domain],operation.values,
                    AgentToolContext(event.platform,event.scope_id,event.sender_id,event.event_key))
            else:
                result = self.travel_service.execute_result(tools[operation.domain],operation.values)
            ensure_execution_active()
            if not cached and claim is not None:
                self.store.save_event_tool_result(op_key,event.event_key,tools[operation.domain],operation.values,result.to_json())
            receipts.append(result)
            rows.append({'operation_key':op_key,'domain':operation.domain,'arguments':operation.values,'status':result.status})
        labels = {'completed':'已完成','needs_input':'待补充','failed':'失败'}
        reply = '\n\n'.join(f'{i}. {" / ".join(row["arguments"].values())}：{labels.get(result.status,"未完成")}\n{result.data}'
            for i,(row,result) in enumerate(zip(rows,receipts),1))
        status = 'completed' if all(r.status == 'completed' for r in receipts) else 'failed'
        update = TaskUpdate('read_batch',status,event.content,{'operations':rows,'rule_version':RULE_VERSION})
        return ExecutionOutcome(status,reply,update,tuple(receipts)).persist(self.store,event,claim)

    def _research(self,event,claim,ir):
        def diagnostic(status, **details):
            if hasattr(self.store, 'diagnostics'):
                self.store.diagnostics.record('routing', {'event_key':event.event_key,
                    'status':status,'schema_version':SCHEMA_VERSION, **details})
        if self.research_service is None:
            diagnostic('research_rejected',reason='research_unavailable')
            return '联网研究服务暂不可用。'
        operations=[op for op in ir.operations if op.domain=='research' and op.operation=='search_travel_guides']
        if len(operations)!=1 or len(ir.operations)!=1 or ir.action not in {'read','create','clarify'} or not ir.source_spans:
            diagnostic('research_rejected', reason='invalid_research_action')
            return '这次攻略研究的目标还不明确，请说明目的地和想参考的来源。'
        try:
            values, ignored = project_request(operations[0].values, event.content)
        except ResearchRequestError as error:
            diagnostic('research_rejected', reason=error.code)
            return '研究请求的核心字段无法对应你的原话，尚未搜索；请确认查询目标、方案引用和日期。'
        request = {**values, 'original_request':event.content}
        if values.get('plan_reference') or values.get('plan_index'):
            plan, resolution = resolve_plan(self.store, event, values)
            if plan is None:
                diagnostic('research_needs_input', plan_resolution=resolution)
                return '请确认要核验哪份方案（可引用方案序号或标题）；尚未搜索，也未修改原行程。'
            request['plan'] = plan
            request['plan_resolution'] = resolution
        try:
            request['dates'] = research_dates(request, event, request.get('plan',{}).get('duration_options',[]),
                                             planning_spec=request.get('plan',{}).get('planning_spec'))
        except ValueError as error:
            diagnostic('research_needs_input', reason='invalid_research_dates')
            return str(error)
        if self.mode=='preview':
            return '已识别联网攻略研究请求；当前仅预览，尚未搜索。'
        diagnostic('research_accepted', adopted_fields=sorted(values), ignored_fields=ignored,
                   plan_resolution=request.get('plan_resolution','not_referenced'))
        return self.research_service.handle_intent(event,claim,request=request)

    def _travel_lookup(self, event, ir, claim=None, read_context=None):
        if self.travel_service is None or len(ir.operations) != 1 or ir.action not in {'read', 'clarify'}:
            return None
        operation = ir.operations[0]
        if operation.operation not in {'query', 'search'} or not ir.source_spans:
            return None
        if ir.requires_confirmation or re.search(r'风险|安全|比较|分析|不要|取消', event.content):
            return None
        read_context = read_context or TaskContext.for_read(event, ())
        schemas = {
            'weather': ('get_current_weather', ('location',)),
            'forecast': ('get_weather_forecast', ('location',)),
            'route': ('get_driving_route', ('origin', 'destination')),
            'traffic': ('get_route_traffic', ('origin', 'destination')),
            'walking': ('get_walking_route', ('origin', 'destination')),
            'transit': ('get_transit_route', ('origin', 'destination', 'city')),
            'places': ('search_travel_places', ('city', 'keywords')),
        }
        if operation.domain not in schemas:
            return None
        tool, fields = schemas[operation.domain]
        values = operation.values
        if values.keys() - set(fields):
            return None
        arguments, missing = {}, []
        for field in fields:
            value = read_context.read_value(field, values.get(field, read_context.slots.get(field, '')))
            if not isinstance(value, str) or len(value) > 150:
                return None
            if not value:
                missing.append(field)
            elif value not in read_context.message and value != read_context.slots.get(field):
                return None
            else:
                arguments[field] = value
        slots = dict(read_context.slots)
        slots.update(arguments)
        slots['reference_time'] = read_context.reference_time
        slots['intents'] = [operation.domain]
        sources = dict(slots.get('field_sources', {}))
        sources.update({key: event.event_key for key, value in arguments.items() if value in event.content})
        slots['field_sources'] = sources
        task = read_context.task
        def outcome(status, reply, missing_fields=(), receipts=()):
            update = TaskUpdate(operation.domain, 'collecting' if status == 'needs_input' else status,
                task.initial_request if task else event.content, slots, tuple(missing_fields),
                task.task_id if task else '', task.version if task else None)
            return ExecutionOutcome(status, reply, update, receipts).persist(self.store, event, claim)
        if missing or (ir.action == 'clarify' and not arguments):
            labels = {'location':'地点','origin':'起点','destination':'终点','city':'城市','keywords':'场所类型'}
            requested = missing or list(ir.missing_fields)
            return outcome('needs_input', '请补充' + '、'.join(labels.get(field, field) for field in requested) + '。', requested)
        if self.mode == 'preview':
            return '已识别实时旅行查询；当前仅解析预览，尚未调用数据源。'
        if self.tool_executor:
            result = self.tool_executor(tool,arguments,AgentToolContext(event.platform,event.scope_id,event.sender_id,event.event_key))
        elif callable(getattr(type(self.travel_service),'execute_result',None)):
            result = retry_read(lambda: self.travel_service.execute_result(tool,arguments),
                max_attempts=1 if getattr(self.travel_service, 'manages_read_retries', False) is True else 3)
        else:
            result = ToolResult.from_text(tool, self.travel_service.execute_tool(tool, arguments))
        return outcome(result.status if result.status in {'completed','needs_input'} else 'failed',
                       result.data.removeprefix('工具错误：'),missing_fields=result.missing_fields,receipts=(result,))

    def _create_from_plan(self,event,claim,ir,artifacts,pending,source_text):
        if self.trip_service is None:
            return '行程服务暂不可用，未创建行程。'
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
        if self.mode=='preview':
            return f'已选择“{artifact["title"]}”的 {selected} 天方案；当前仅解析预览，尚未生成行程。'
        return self.trip_service.create_from_artifact(event,claim,artifact,selected,changes,
            source_text=source_text,semantic_task=pending)

    def _trip_context(self, event, pending=None):
        repo = getattr(self.store, 'trips', None)
        trips = tuple(repo.list_for_owner(event)) if repo else ()
        if not trips:
            return (), {}
        if hasattr(repo, 'draft'):
            enriched = []
            for trip in trips:
                draft = repo.draft(event, trip)
                trip = {**trip, '_draft_key': (draft['task'].task_id,draft['task'].version) if draft else ('',0)}
                if draft and draft['task'].slots.get('expected_trip_version') == trip['version']:
                    trip.update({key:draft['candidate'][key] for key in ('spec','plan','sources')})
                enriched.append(trip)
            trips = tuple(enriched)
        candidates = []
        for index, trip in enumerate(trips, 1):
            spec = trip.get('spec', {})
            candidates.append({'index': index, 'title': trip.get('title', ''),
                'city': spec.get('destination', ''), 'start_date': spec.get('start_date', ''),
                'end_date': spec.get('end_date', ''), 'date_is_tentative': spec.get('date_is_tentative', False),
                'days': spec.get('day_count'), 'version': trip.get('version')})
        selected = trips[0] if len(trips) == 1 else None
        ids = re.findall(r'TR-[a-f0-9]{12}', event.content)
        if ids:
            selected = next((t for t in trips if len(set(ids)) == 1 and t['trip_id'] == ids[0]), None)
        elif event.reply_to_id and hasattr(repo, 'referenced'):
            referenced = repo.referenced(event)
            selected = next((t for t in trips if referenced and t['trip_id'] == referenced['trip_id']), selected)
        elif pending and pending.slots.get('constraint_trip'):
            selected = next((t for t in trips if t['trip_id'] == pending.slots['constraint_trip']['trip_id']), None)
        context = {'trips': candidates}
        if selected:
            context['current_trip_index'] = next(i for i,t in enumerate(trips,1) if t['trip_id'] == selected['trip_id'])
            spec = selected.get('spec', {})
            context['current_trip'] = {k: spec[k] for k in ('destination','start_date','end_date','day_count','date_is_tentative',
                'gentle','pace','transport_text','transport_preferences','must_visit','preferences','constraint_overrides',
                'hotel_requested','accommodation_text','hotel_preferences','restaurant_requested','dining_text',
                'dining_preferences','budget_text','budgets') if k in spec}
            context['current_trip']['activities'] = [
                {'day': d.get('day_index'), 'places': [selected.get('sources',{}).get(a['poi_id'],{}).get('name','')
                    for a in d.get('activities',[])]} for d in selected.get('plan',{}).get('days',[])]
            context['current_trip']['research_summary'] = spec.get('source_context','')[:800]
        preferences = getattr(self.store, 'preferences', None)
        if preferences:
            context['long_term_defaults'] = preferences.defaults(event)['values']
        return trips, context

    def _update_trip(self, event, claim, ir, trips, context, pending, source_text, artifacts=()):
        if self.trip_service is None:
            return '行程服务暂不可用，未修改行程。'
        if len(ir.operations) != 1 or ir.action not in {'update','clarify'} or not ir.source_spans:
            return '行程调整的动作或依据不明确，未修改行程。请分别说明其他操作。'
        values = ir.operations[0].values
        if values.keys() - {'target_ref','trip_index','trip_reference','changes','save_preference_text','question'}:
            return '行程调整包含不支持的字段，未修改行程。'
        if 'question' in values and (not isinstance(values['question'],str) or len(values['question'])>400):
            return '模型返回的追问无效，原行程未修改，请重新说明这次调整。'
        if values.get('target_ref', 'current_trip') != 'current_trip':
            return '行程目标引用无效，未修改行程。请说明要调整哪份已保存行程。'
        try:
            _, summary = apply_constraints({}, values.get('changes'), source_text)
        except ValueError as exc:
            logger.info('Trip constraint rejected: event=%s reason=invalid_patch validation=%s',event.event_key,str(exc))
            return str(exc)
        ids = re.findall(r'TR-[a-f0-9]{12}', event.content)
        if ids and (len(set(ids)) != 1 or not any(t['trip_id'] == ids[0] for t in trips)):
            logger.info('Trip constraint rejected: event=%s reason=invalid_target',event.event_key)
            return '没有找到当前会话中属于你的这份有效行程。'
        if not trips:
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
                    return self._create_from_plan(event,claim,converted,artifacts,pending,source_text)
            return '当前会话还没有已保存行程，请先规划行程；本轮未创建交通查询。'
        index = context.get('current_trip_index')
        if pending and pending.slots.get('constraint_trip'):
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
            question = '要调整哪一份行程？请回复行程编号或唯一的城市/标题：\n' + '\n'.join(
                f"{t.get('title','行程')}（{t['trip_id']}）" for t in trips)
            return self._save_clarification(event, claim, ir, question, task=pending, source_text=source_text)
        trip = trips[index-1]
        budget_changes = ([values['changes']['budget']] if values['changes'].get('budget') else [])
        if isinstance(values['changes'].get('budgets'), list):
            budget_changes.extend(values['changes']['budgets'])
        existing_budgets = trip.get('spec',{}).get('budgets',{})
        needs_budget_unit = any(not budget.get('unit_text', existing_budgets.get(budget.get('scope'),{}).get('unit_text'))
                                for budget in budget_changes)
        if needs_budget_unit:
            ir = replace(ir, missing_fields=tuple(dict.fromkeys([*ir.missing_fields,'budget_unit'])))
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
            profile_values = PreferenceService.extract(save_text)
            if not profile_values:
                return '长期偏好暂不支持这组表达，尚未修改行程或偏好。请将本次行程调整与长期偏好分别说明。'
            preference_update = {'values':profile_values,'version':self.store.preferences.snapshot(event)['version']}
        if self.mode == 'preview':
            return '已识别对原行程的约束调整；当前仅解析预览，尚未保存行程或偏好。'
        logger.info('Trip constraint dispatch: event=%s trip=%s version=%s fields=%s',
                    event.event_key,trip['trip_id'],trip['version'],sorted(values['changes']))
        return self.trip_service.update_constraints(event, claim, trip['trip_id'], trip['version'], values['changes'],
            source_text=source_text, preference_update=preference_update, semantic_task=pending,
            expected_draft=trip.get('_draft_key'))

    @staticmethod
    def _supported(ir):
        supported = {
            ('trip', 'remove_activity'), ('trip', 'view_current_trip'),
            ('trip', 'confirm_pending_operation'), ('reminder', 'create'),
            ('trip','create_from_plan_artifact'),('research','search_travel_guides'),
            ('reminder', 'cancel_linked_reminder'),
        }
        return any((item.domain, item.operation) in supported for item in ir.operations)

    def _pending_reminders(self, event):
        if not hasattr(self.store, 'tasks'):
            return ()
        return tuple(task for task in self.store.tasks.recent(
            event.platform, event.storage_scope_id, event.sender_id, reply_to_id=event.reply_to_id)
            if task.task_type == 'reminder' and task.status == 'collecting')

    def _reminder_context(self, tasks, event):
        pending = []
        for index, task in enumerate(tasks, 1):
            question = ''
            with self.store._connect() as connection:
                row = connection.execute('SELECT prepared_reply FROM processed_events WHERE event_id=?',
                                         (task.last_event_id,)).fetchone()
                question = (row['prepared_reply'] or '')[:400] if row else ''
            pending.append({'index': index, 'initial_request': task.initial_request[:240],
                            'slots': {key: task.slots.get(key, '') for key in ('action', 'title', 'day', 'clock')},
                            'missing_fields': list(task.missing_slots), 'last_question': question})
        return json.dumps({'pending_reminders': pending, 'timezone': 'Asia/Shanghai', 'current_time': (event.occurred_at or
                          (self.clock() if self.clock else datetime.now().astimezone())).isoformat()}, ensure_ascii=False)

    def _continue_reminder(self, event, claim, ir, tasks):
        if self.reminder_service is None or not tasks:
            return '没有可接续的提醒设置，可能已经完成或过期。请重新说明事项和时间。'
        if len(ir.operations) != 1 or not ir.source_spans:
            return '这次补充涉及的操作还不明确，尚未修改提醒设置。请分别说明要补充或取消的内容。'
        values = ir.operations[0].values
        index = values.get('task_index')
        if type(index) is not int or not 1 <= index <= len(tasks):
            return '请说明要继续哪一项提醒设置：' + '；'.join(task.slots.get('title') or task.initial_request for task in tasks)
        task = tasks[index-1]
        if len(tasks) > 1:
            reference = values.get('task_reference', '')
            if not isinstance(reference, str):
                return '请明确说出要继续设置的提醒事项。'
            matches = [item for item in tasks if reference and reference in item.slots.get('title', '')]
            if reference not in event.content or len(matches) != 1 or matches[0].task_id != task.task_id:
                return '有多项提醒正在设置，请明确说出要继续的事项，或回复对应任务后再补充。'
        disposition = values.get('disposition')
        if disposition not in {'answer', 'clarify', 'cancel'}:
            return '尚未理解这次补充，没有修改提醒。请明确时间、事项或取消设置。'
        if ir.action not in {'create', 'update', 'clarify', 'cancel'} or ((disposition == 'cancel') != (ir.action == 'cancel')):
            return '解析到的动作不一致，尚未修改提醒设置。'
        if (ir.action == 'clarify' or ir.missing_fields or ir.requires_confirmation) and disposition != 'cancel':
            disposition = 'clarify'
        title = values.get('title', '')
        trigger = values.get('trigger', {})
        if not isinstance(title, str) or (title and title not in event.content) or not isinstance(trigger, dict):
            return '补充内容无法对应你的原话，没有修改提醒设置。'
        time_text, source = trigger.get('text', ''), trigger.get('source_text', '')
        if not isinstance(time_text, str) or len(time_text) > 120 or not isinstance(source, str):
            return '提醒时间解析无效，请重新说明。'
        if time_text and (not source or source not in event.content):
            return '提醒时间缺少你本轮的明确依据，请重新说明。'
        # A recognizably ambiguous clock must not be silently upgraded to AM/PM by the model.
        if source and is_time_answer(source):
            parsed_source = parse_reminder_time(source, event.occurred_at or datetime.now().astimezone(),
                day=task.slots.get('day', ''), clock=task.slots.get('clock', ''),
                period_required='period' in task.missing_slots)
            if parsed_source.needs_period:
                time_text = source
        if self.mode == 'preview':
            return '已理解你对提醒设置的补充；当前仅预览，尚未保存或创建提醒。'
        return self.reminder_service.handle_intent(event, claim, task=task, title=title,
            time_text=time_text, disposition=disposition)

    def _recover_ir(self, event, pending):
        text = event.content
        tasks_repo = getattr(self.store, 'tasks', None)
        if re.search(r'确认|同意|执行|保存|变更', text) and tasks_repo is not None:
            tasks = tasks_repo.recent(event.platform, event.storage_scope_id, event.sender_id,
                                      reply_to_id=event.reply_to_id)
            if any(task.task_type == 'itinerary' and task.status == 'collecting'
                   and 'confirmation' in task.missing_slots for task in tasks):
                return IntentIR(1, 'confirm', (IntentOperation('trip', 'confirm_pending_operation', {}),),
                                source_spans=())
        if not re.search(r'不去|删除|取消.{0,12}(?:行程|日程)|不安排', text):
            return None
        trips = self.store.trips.list_for_owner(event)
        if len(trips) != 1:
            return None
        names = list(dict.fromkeys(place['name'] for place in trips[0]['sources'].values()
                                   if place.get('name') and place['name'] in text))
        if len(names) != 1:
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

    def _pending(self, event):
        tasks_repo = getattr(self.store, 'tasks', None)
        if tasks_repo is None:
            return None
        tasks = tasks_repo.recent(event.platform, event.storage_scope_id, event.sender_id,
                                  reply_to_id=event.reply_to_id)
        pending = [task for task in tasks if task.task_type == 'semantic' and task.status == 'collecting']
        return pending[0] if len(pending) == 1 else None

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

    def _current_trip(self, event):
        trips = self.store.trips.list_for_owner(event)
        return (trips[0], trips) if len(trips) == 1 else (None, trips)

    def _trip(self, event, claim, ir):
        trip, trips = self._current_trip(event)
        if any(item.operation == 'view_current_trip' for item in ir.operations):
            if self.trip_service is None:
                return None
            return self.trip_service.handle(replace(event, content='我的行程'), claim)
        if trip is None:
            if not trips:
                return '还没有找到你在本群创建的行程，请先规划或查看行程。'
            return '你有多份行程，请先使用“查看行程”并说明要修改哪一份。'
        remove_ops = [item for item in ir.operations if item.domain == 'trip' and item.operation == 'remove_activity']
        if not remove_ops or self.trip_service is None:
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
        return self.trip_service.handle(replace(event, content=f'第{matches[0]}天不去{name}'), claim,
                                        force_confirmation=True)

    def _confirm(self, event, claim, ir):
        if self.mode == 'preview':
            return '已识别到待确认的行程变更；当前处于预览模式，尚未执行。'
        if self.trip_service and any(item.domain == 'trip' and item.operation == 'confirm_pending_operation'
                                     for item in ir.operations):
            return self.trip_service.handle(replace(event, content='确认行程修改'), claim)
        return None

    def _reminder(self, event, claim, ir):
        if self.reminder_service is None:
            return None
        linked = [item for item in ir.operations if item.domain == 'reminder' and item.operation == 'cancel_linked_reminder']
        if linked:
            entity = linked[0].values.get('entity', '')
            if not entity:
                return '请说明要取消哪一个地点的关联预约提醒。'
            trips = self.store.trips.list_for_owner(event)
            reminders = []
            for trip in trips:
                reminders.extend(self.store.trips.linked_reminders(event, trip['trip_id']))
            matches = [row for row in reminders if entity == json.loads(row['source_json']).get('entity')]
            if len(matches) != 1:
                return '没有找到唯一的关联预约提醒，请补充行程或场馆名称。' if not matches else '这个地点有多条关联预约提醒，请先说明参观日期。'
            if self.mode == 'preview':
                return f"已找到关联预约提醒 {matches[0]['reminder_id']}；当前仅生成预览，尚未取消。"
            return self.reminder_service.handle(replace(event, content=f"取消提醒 {matches[0]['reminder_id']}"), claim)
        items = [item for item in ir.operations if item.domain == 'reminder' and item.operation == 'create']
        if not items:
            return None
        if len(items) != 1 or len(ir.operations) != 1:
            return '这次涉及多个操作，请分别说明每一项提醒的事项和时间，当前没有创建提醒。'
        if ir.action not in {'create', 'clarify'}:
            return '解析到的动作与创建提醒不一致，当前没有创建提醒。'
        values = items[0].values
        title = values.get('title', '')
        trigger = values.get('trigger') if isinstance(values.get('trigger'), dict) else {}
        time_text = trigger.get('text', '')
        source = trigger.get('source_text', time_text)
        if (not isinstance(title, str) or not isinstance(time_text, str)
                or len(time_text) > 120 or not isinstance(source, str)
                or (title and title not in event.content)
                or (time_text and (not source or source not in event.content))):
            return '提醒事项或时间无法对应你的原话，尚未创建提醒。请明确要提醒的事项和时间。'
        if source and is_time_answer(source):
            parsed_source = parse_reminder_time(source, event.occurred_at or datetime.now().astimezone())
            if parsed_source.needs_period:
                time_text = source
        if self.mode == 'preview':
            return f'已识别提醒：{time_text}提醒你{title}。当前仅生成预览，尚未创建。'
        return self.reminder_service.handle_intent(event, claim, title=title, time_text=time_text,
            disposition='clarify' if ir.action == 'clarify' or ir.requires_confirmation or ir.missing_fields else 'answer')
