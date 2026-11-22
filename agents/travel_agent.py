from infrastructure.skill_manager import skill_text, skill_request
import json
import logging
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Sequence
from types import SimpleNamespace
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context

from openai import OpenAI

from app.conversation_context import AgentContext, render_untrusted_context
from core.execution_outcome import TravelDecision, CONVERSATION_POLICY
from tools.registry import TOOL_BY_INTENT
from core.settings import Settings
from core.model_budget import ModelBudgetExceeded
from core.model_budget import MODEL_EVENT, model_call_context
from core.execution_scope import ExecutionRevoked
from core.execution_outcome import TravelReadRequest
from core.tasks import TaskUpdate, READ_TASK_TYPES
from core.tool_result import AgentToolContext, ToolExecutor, ToolResult, validate_arguments
from tools.travel_query import (read_fact_state, query_argument_error, finish_read_turn,
                                early_read_clarification, unexecuted_read_clarification, read_turn_policy)
from tools.registry import (request_input, TOOL_DEFINITIONS, repeatable_tool, catalog_schemas,
                            normalize_tool_call, DEFAULT_CATALOG, COMPACT_CATALOG)
from core.tool_result import TOOLS_BY_NAME, ASK_USER_TOOL, READ_TOOL_ARTIFACT_TOOL
from core.tool_result import COMPACT_RAIL_CATALOG
from core.tool_result import COMPACT_HOTEL_CATALOG, COMPACT_CATALOGS


MAX_AGENT_STEPS = 4
MAX_TOOL_CALLS = 6
LLM_TIMEOUT_SECONDS = 90.0
MAX_HISTORY_CHARS = 3000
MAX_USER_MESSAGE_CHARS = 8000


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolTrace:
    name: str
    arguments: dict[str, object]
    call_id: str = ''
    result_status: str = ''
    fact_state: str = ''
    surface_name: str = ''


@dataclass(frozen=True)
class AgentResult:
    reply: str
    traces: tuple[ToolTrace, ...]
    status: str = "completed"
    task_update: TaskUpdate | None = None
    missing_slots: tuple[str, ...] = ()
    receipts: tuple[ToolResult, ...] = ()
    answer_status: str = 'not_checked'
    answer_reasons: tuple[str, ...] = ()
    answer_facts_hash: str = ''


@dataclass
class AgentLoopState:
    messages: list
    allowed_tools: set
    tool_definitions: list
    catalog_version: str = DEFAULT_CATALOG
    traces: list = field(default_factory=list)
    receipts: list = field(default_factory=list)
    failures: list = field(default_factory=list)
    tool_cache: dict = field(default_factory=dict)
    invoked_steps: dict = field(default_factory=dict)
    executed_tools: set = field(default_factory=set)
    repeated_read_results: set = field(default_factory=set)

    def stopped(self, reason):
        committed = any(receipt.effect_status == 'committed' for receipt in self.receipts)
        prefix = '部分操作已完成；以下结果已确认，未完成部分没有被当作成功。' if committed else ''
        return AgentResult('\n'.join(dict.fromkeys([
            *([prefix] if prefix else []), *(receipt.data for receipt in self.receipts), reason,
            *(failure.data for failure in self.failures[-1:])])), tuple(self.traces),
            status='partial' if committed else 'failed', receipts=tuple(self.receipts))


@skill_request
def _system_prompt(decision: TravelDecision, reference_time: str = '', catalog_version=DEFAULT_CATALOG) -> str:
    names = decision.allowed_tools
    if names and set(decision.intents) <= READ_TASK_TYPES:
        names = (*names, ASK_USER_TOOL)
    if catalog_version in COMPACT_CATALOGS:
        names = tuple(schema['function']['name'] for schema in catalog_schemas(names, catalog_version))
    allowed_tools = "、".join(names) or "无"
    current_date = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    planning_day = (datetime.fromisoformat(reference_time).astimezone(ZoneInfo('Asia/Shanghai')).date()
                    if reference_time else datetime.now(ZoneInfo('Asia/Shanghai')).date())
    planning_start = (planning_day + timedelta(days=1)).isoformat()
    intents = "、".join(decision.intents)
    reference_note = ''
    if reference_time:
        reference_note = (((skill_text('agent.respond', 'reference_note') + datetime.fromisoformat(reference_time).astimezone(ZoneInfo('Asia/Shanghai')).isoformat()) + skill_text('agent.respond', 'reference_note_2')))
    return (skill_text('agent.respond', 'system') + f'{planning_start}' + skill_text('agent.respond', 'system_2') + f'{current_date}' + skill_text('agent.respond', 'system_3') + f'{reference_note}' + skill_text('agent.respond', 'system_4') + f'{intents}' + skill_text('agent.respond', 'system_5') + f'{allowed_tools}' + skill_text('agent.respond', 'system_6') + f'{decision.response_detail}' + skill_text('agent.respond', 'system_7'))


class TravelAgent:
    def __init__(
            self,
            settings: Settings,
            tool_executor: ToolExecutor,
            client: Any = None, *, catalog_version=DEFAULT_CATALOG, catalog_resolver=None):
        if not settings.llm_configured:
            raise ValueError("LLM settings are incomplete")

        self.model = settings.llm_model_id
        self.tool_executor = tool_executor
        self.catalog_version = catalog_version
        self.catalog_resolver = catalog_resolver
        self.client = client or OpenAI(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            timeout=LLM_TIMEOUT_SECONDS,
            max_retries=0,
        )

    @skill_request
    def run(
            self,
            user_message: str,
            history: Sequence[Any] | AgentContext = (),
            knowledge_context: str = "",
            tool_context: AgentToolContext | None = None,
            *, read_request: TravelReadRequest | None = None, decision_override: TravelDecision | None = None) -> AgentResult:
        if isinstance(history, AgentContext) and history.summary_information_incomplete:
            return AgentResult('近期更新尚未完整纳入可用上下文，无法可靠确认当前约束。请重新说明本次需要使用的最新要求。',
                               (), 'needs_input', missing_slots=('conversation_context',))
        task, resolved_message = ((read_request.context.task,read_request.context.message) if read_request is not None
                                  else (None,user_message))
        if task is None and tool_context and tool_context.execution_data:
            task=tool_context.execution_data.get('hotel_task')
        reference_time = history.message_time if isinstance(history, AgentContext) else ''
        decision = decision_override or CONVERSATION_POLICY
        decision, reference_time = read_turn_policy(read_request, decision, reference_time)
        result = self._run(resolved_message, history, knowledge_context, tool_context,
                           decision_override=decision,
                           reference_time=reference_time,read_request=read_request)
        return finish_read_turn(result, decision, task, read_request, resolved_message, reference_time)

    def _run(
            self,
            user_message: str,
            history: Sequence[Any] | AgentContext = (),
            knowledge_context: str = "",
            tool_context: AgentToolContext | None = None,
            decision_override: TravelDecision | None = None,
            reference_time: str = '',
            read_request: TravelReadRequest | None = None) -> AgentResult:
        if len(user_message) > MAX_USER_MESSAGE_CHARS:
            return AgentResult(
                reply="消息正文超过 8000 字符限制，请精简后重试。",
                traces=(),
            )
        document_context = history.document_context if isinstance(history, AgentContext) else knowledge_context
        decision = decision_override or CONVERSATION_POLICY
        clarification = early_read_clarification(user_message, decision, read_request)
        if clarification:
            reply, missing = clarification
            return AgentResult(reply, (), status='needs_input', missing_slots=missing)
        catalog_version = tool_context.catalog_version or self.catalog_version if tool_context else self.catalog_version
        if self.catalog_resolver is not None and decision.allowed_tools:
            catalog_version = self.catalog_resolver(tool_context, catalog_version)
        if tool_context is not None and (tool_context.catalog_version or catalog_version != DEFAULT_CATALOG):
            tool_context = replace(tool_context, catalog_version=catalog_version)
        messages = self._build_messages(user_message, history, knowledge_context, decision, reference_time, read_request, catalog_version)
        allowed_tools = set(decision.allowed_tools)
        names = list(decision.allowed_tools)
        if set(decision.intents) <= READ_TASK_TYPES and names:
            allowed_tools.add(ASK_USER_TOOL)
            names.append(ASK_USER_TOOL)
        tool_definitions = catalog_schemas(names, catalog_version)
        state = AgentLoopState(messages, allowed_tools, tool_definitions, catalog_version=catalog_version)

        for step_index in range(1, MAX_AGENT_STEPS + 1):
            started_at = time.monotonic()
            try:
                response = self._request_model(state, read_request)
            except ModelBudgetExceeded as exc:
                return state.stopped(str(exc))
            except ExecutionRevoked:
                raise
            except Exception:
                logger.warning(
                    "LLM step failed: llm_step=%s elapsed_seconds=%.2f "
                    "message_count=%s model=%s",
                    step_index,
                    time.monotonic() - started_at,
                    len(state.messages),
                    self.model,
                )
                if state.receipts:
                    return state.stopped('后续模型回答暂不可用，请核对上述已确认结果；不要重复提交已完成操作。')
                raise
            logger.info(
                "LLM step completed: llm_step=%s elapsed_seconds=%.2f "
                "message_count=%s model=%s",
                step_index,
                time.monotonic() - started_at,
                len(state.messages),
                self.model,
            )
            assistant = response.choices[0].message
            if getattr(response.choices[0], 'finish_reason', None) in {'length','content_filter'}:
                return state.stopped('模型回答未完整返回，本轮未据此执行新操作。请缩小问题范围。')
            tool_calls = list(assistant.tool_calls or [])

            if not tool_calls:
                result = self._finish_answer(assistant, state, decision, step_index)
                if result is not None:
                    return result
                continue

            result = self._execute_tool_calls(tool_calls, assistant, state, decision, tool_context, read_request, step_index)
            if result is not None:
                return result

        return state.stopped('工具调用次数已达到上限，请缩小问题范围后重试。')

    def _build_messages(self, user_message, history, knowledge_context, decision, reference_time, read_request, catalog_version=DEFAULT_CATALOG):
        structured_context = (
            history if isinstance(history, AgentContext) else None
        )
        if structured_context is not None:
            recent_dialogue = structured_context.recent_dialogue
            knowledge_context = structured_context.document_context
            group_context = structured_context.group_context
        else:
            recent_dialogue = history
            group_context = ""
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _system_prompt(decision, reference_time, catalog_version)},
        ]
        if structured_context is not None:
            messages.append({
                "role": "user",
                "content": render_untrusted_context(structured_context),
            })
            history_messages = []
        else:
            if knowledge_context:
                legacy_context = AgentContext(
                    recent_dialogue=(),
                    group_context="",
                    document_context=knowledge_context,
                    source_note="",
                )
                messages.append({
                    "role": "user",
                    "content": render_untrusted_context(legacy_context),
                })
            history_messages = self._history_messages(recent_dialogue)

        history_chars = sum(
            len(getattr(turn, "user_content", ""))
            + len(getattr(turn, "assistant_content", ""))
            for turn in recent_dialogue
        )
        logger.info(
            "Agent context: history_turns=%s history_chars=%s "
            "group_context_chars=%s document_context_chars=%s",
            len(recent_dialogue),
            history_chars,
            len(group_context),
            len(knowledge_context),
        )

        messages.extend(history_messages)
        if read_request is not None:
            messages.append({'role':'user','content':(
                (skill_text('agent.respond', 'feedback') + json.dumps({'queries':[
                    {'domain':query.domain,**query.values} for query in read_request.queries]},ensure_ascii=False)))})
        messages.append({"role": "user", "content": user_message})
        return messages

    def _request_model(self, state, read_request):
        request = {
            "model": self.model,
            "messages": state.messages,
        }
        if state.tool_definitions:
            request.update({
                "tools": state.tool_definitions,
                "tool_choice": "auto",
            })
        purpose = 'tool_reasoning' if not state.receipts else 'tool_answer'
        prompt_version = 'travel-agent-v5-hotel-flow' if state.catalog_version == DEFAULT_CATALOG else 'travel-agent-v2-' + state.catalog_version
        with model_call_context(purpose, prompt_version=prompt_version,
                                context_policy_version='travel-context-v3-read-owner' if read_request is not None else 'travel-context-v2', stage_id='agent.respond'):
            return self.client.chat.completions.create(
                **request,
            )

    def _finish_answer(self, assistant, state, decision, step_index):
        requested_actions = set(decision.action_resources)
        reply = (assistant.content or "").strip()
        if unexecuted_read_clarification(decision, state.executed_tools, reply):
            return AgentResult(reply=reply, traces=(), status="needs_input")
        if state.failures and not state.receipts:
            return state.stopped('本次工具操作未完成，不能把模型答文当作执行成功。')
        missing_groups = [
            group
            for group in decision.required_tool_groups
            if not state.executed_tools.intersection(group)
        ]
        missing_resources = requested_actions - {
            (receipt.action, receipt.resource_id) for receipt in state.receipts
            if receipt.status == "completed"
        }
        if (missing_groups or missing_resources) and step_index < MAX_AGENT_STEPS:
            missing_names = "、".join(
                "/".join(group) for group in missing_groups
            )
            state.messages.append({
                "role": "system",
                "content": (
                    (skill_text('agent.respond', 'system_8') + f'{missing_names}' + skill_text('agent.respond', 'system_9') + f'{sorted(missing_resources)}' + skill_text('agent.respond', 'system_10'))
                ),
            })
            return None
        if missing_groups or missing_resources:
            return state.stopped('需要的实时或预约工具未完成，请补充信息后重试。')
        reply = (assistant.content or "").strip()
        if not reply:
            reply = "暂时无法生成回答，请换一种问法重试。"
        logger.info(
            "Agent result: intent=%s tool_names=%s",
            decision.intent,
            tuple(trace.name for trace in state.traces),
        )
        mutations = [receipt for receipt in state.receipts if receipt.status == "completed"
                     and receipt.effect_status == 'committed'
                     and not repeatable_tool(receipt.action)]
        if mutations:
            reply = "\n".join(dict.fromkeys(
                receipt.data for receipt in state.receipts
                if not repeatable_tool(receipt.action)
            ))
        return AgentResult(reply=reply, traces=tuple(state.traces), receipts=tuple(state.receipts))


    def _normalize_calls(self, tool_calls, catalog_version):
        surface_names = {str(call.id): call.function.name for call in tool_calls}
        normalization_errors = {}
        normalized_calls = []
        for call in tool_calls:
            values, error = self._parse_arguments(call.function.arguments)
            if not error:
                try:
                    name, values = normalize_tool_call(call.function.name, values, catalog_version)
                    call = SimpleNamespace(id=call.id, function=SimpleNamespace(name=name,
                        arguments=json.dumps(values, ensure_ascii=False)))
                except ValueError as exc:
                    normalization_errors[str(call.id)] = str(exc)
            normalized_calls.append(call)
        return normalized_calls, surface_names, normalization_errors

    def _execute_tool_calls(self, tool_calls, assistant, state, decision, tool_context, read_request, step_index):
        requested_actions = set(decision.action_resources)
        state.messages.append({
            "role": "assistant",
            "content": assistant.content,
            "tool_calls": [self._serialize_tool_call(call) for call in tool_calls],
        })

        tool_calls, surface_names, normalization_errors = self._normalize_calls(tool_calls, state.catalog_version)

        for call in tool_calls:
            parsed, parse_error = self._parse_arguments(call.function.arguments)
            key = (call.function.name, json.dumps(parsed, ensure_ascii=False, sort_keys=True))
            if (not repeatable_tool(call.function.name) and not parse_error
                    and key in state.invoked_steps and state.invoked_steps[key] < step_index):
                return state.stopped('同一工具调用在状态未变化时重复出现，已停止继续尝试。请补充信息或稍后重试。')
        call_context = replace(tool_context,
            allowed_actions=frozenset(state.allowed_tools) if tool_context.allowed_actions is None else tool_context.allowed_actions & state.allowed_tools,
            action_resources=tool_context.action_resources or tuple(sorted(requested_actions))) if tool_context else None
        parallel_results = self._parallel_reads(tool_calls, state.allowed_tools, call_context, state.tool_cache, len(state.traces),read_request)
        for tool_call in tool_calls:
            name = tool_call.function.name
            arguments, error = self._parse_arguments(
                tool_call.function.arguments
            )

            if len(state.traces) >= MAX_TOOL_CALLS:
                state.messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": "工具调用总数已达到上限，请基于已有结果回答。",
                })
                continue

            state.traces.append(ToolTrace(name=name, arguments=arguments, call_id=str(tool_call.id),
                                         surface_name=surface_names[str(tool_call.id)] if surface_names[str(tool_call.id)] != name else ''))

            resource_id, rejection = self._validate_tool_call(name, arguments, error, state, requested_actions, read_request)
            if str(tool_call.id) in normalization_errors:
                result = ToolResult('failed', name, data=normalization_errors[str(tool_call.id)], error_code='invalid_argument')
            elif rejection is not None:
                result = rejection
            elif name == ASK_USER_TOOL:
                result = request_input(name, arguments, call_context)
                question = result.data
                if result.status == 'needs_input':
                    from core.intent_ir import DERIVED_READ_FIELDS
                    derivable={field for query in read_request.queries if query.values.get('origin') and query.values.get('destination')
                               for field in DERIVED_READ_FIELDS.get(query.domain,())} if read_request else set()
                    if (read_request is not None and (set(result.missing_fields)<=derivable or
                            not state.failures and set(result.missing_fields)<=read_request.known_fields|derivable)):
                        result = ToolResult('failed', name, data='这些字段已提供或可由已授权工具验证，请先完成查询，不要重复追问。', error_code='already_known_input')
                    elif any(receipt.effect_status == 'committed' for receipt in state.receipts):
                        return replace(state.stopped(question), missing_slots=tuple(dict.fromkeys(result.missing_fields)))
                    else:
                        return AgentResult(question, tuple(state.traces), status='needs_input',
                                           missing_slots=tuple(dict.fromkeys(result.missing_fields)))
            else:
                cache_key = (name, json.dumps(arguments, ensure_ascii=False, sort_keys=True))
                state.invoked_steps[cache_key] = step_index
                if not repeatable_tool(name) and cache_key in state.tool_cache:
                    result = state.tool_cache[cache_key]
                else:
                    try:
                        raw_result = parallel_results[cache_key] if cache_key in parallel_results else self._invoke_tool(name, arguments, call_context)
                    except ExecutionRevoked:
                        raise
                    except Exception:
                        return state.stopped('工具执行中断，当前动作的结果尚未确认，请先核对状态。')
                    result = raw_result
                    if result.status == "completed" and not repeatable_tool(name):
                        state.tool_cache[cache_key] = result
                if result.status == "needs_input":
                    if any(receipt.effect_status == 'committed' for receipt in state.receipts):
                        return state.stopped(result.data)
                    return AgentResult(result.data, tuple(state.traces), status="needs_input",
                                       missing_slots=tuple(result.missing_fields),receipts=tuple([*state.receipts,result]))
                if result.status == "completed" and result.action == name and result.resource_id == resource_id:
                    state.executed_tools.add(name)
                    if result not in state.receipts:
                        state.receipts.append(result)

            stopped = self._record_receipt(state, name, tool_call.id, result)
            if stopped is not None:
                return stopped

        writes = [receipt for receipt in state.receipts if receipt.effect_status == 'committed'
                  and not repeatable_tool(receipt.action)]
        missing = [group for group in decision.required_tool_groups if not state.executed_tools.intersection(group)]
        missing_objects = requested_actions - {(receipt.action,receipt.resource_id) for receipt in state.receipts}
        if writes and not missing and not missing_objects and not state.failures:
            return AgentResult('\n'.join(dict.fromkeys(receipt.data for receipt in state.receipts
                               if not repeatable_tool(receipt.action))),tuple(state.traces),receipts=tuple(state.receipts))

        return None

    def _validate_tool_call(self, name, arguments, parse_error, state, requested_actions, read_request):
        definition = TOOL_DEFINITIONS.get(name)
        resource = str(arguments.get(definition.resource_field, '')) if definition and definition.resource_field else ''
        if name not in state.allowed_tools or definition is None:
            return resource, ToolResult('failed', name, data='当前请求的工具策略不允许调用该工具。', error_code='not_allowed')
        error = parse_error or validate_arguments(arguments, definition.schema['function']['parameters'])
        error = error or query_argument_error(name, arguments, read_request)
        if error:
            return resource, ToolResult('failed', name, data=error, error_code='invalid_json', retryable=True)
        allowed = {value for action, value in requested_actions if action == name}
        if allowed and resource not in allowed:
            return resource, ToolResult('failed', name, resource, data='工具对象不在本次请求指定范围内。', error_code='wrong_resource')
        return resource, None

    def _record_receipt(self, state, name, call_id, result):
        if result.status == "failed":
            state.failures.append(result)
        state.traces[-1]=replace(state.traces[-1],result_status=result.status,
                           fact_state=read_fact_state(result))
        if repeatable_tool(name) and result.status == 'completed':
            if (name, result.data) in state.repeated_read_results:
                return state.stopped('工具重复返回相同状态，已停止无进展刷新。')
            state.repeated_read_results.add((name, result.data))
        if (result.artifact_id and READ_TOOL_ARTIFACT_TOOL not in state.allowed_tools):
            state.allowed_tools.add(READ_TOOL_ARTIFACT_TOOL)
            state.tool_definitions.append(TOOLS_BY_NAME[READ_TOOL_ARTIFACT_TOOL])
        logger.info('Tool receipt: %s', json.dumps({'event_id': MODEL_EVENT.get(), 'call_id': str(call_id),
                    'name': name, 'status': result.status, 'error_code': result.error_code}, ensure_ascii=False))
        state.messages.append({
            "role": "tool",
            "tool_call_id": call_id,
            "content": result.to_json(),
        })

        return None

    def _invoke_tool(self, name, arguments, context):
        result = self.tool_executor(name, arguments, context)
        if not isinstance(result, ToolResult) or result.action != name:
            raise TypeError('Tool executors must return their own ToolResult.')
        if (TOOL_DEFINITIONS[name].mode == 'write'
                and result.status == 'completed' and result.effect_status != 'committed'):
            raise TypeError('Completed writes require a committed receipt.')
        return result

    def _parallel_reads(self, calls, allowed, context, cache, trace_count,read_request=None):
        if len(calls) < 2 or trace_count + len(calls) > MAX_TOOL_CALLS:
            return {}
        pending = {}
        for call in calls:
            name = call.function.name
            arguments, error = self._parse_arguments(call.function.arguments)
            if (name not in allowed or name not in TOOL_DEFINITIONS or not TOOL_DEFINITIONS[name].parallel_safe or error
                    or validate_arguments(arguments, TOOLS_BY_NAME[name]['function']['parameters'])
                    or query_argument_error(name,arguments,read_request)):
                return {}
            key = (name, json.dumps(arguments, ensure_ascii=False, sort_keys=True))
            if key not in cache:
                pending[key] = (name, arguments)
        if len(pending) < 2:
            return {}
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix='travel-read') as pool:
            futures = {key: pool.submit(copy_context().run, self._invoke_tool, name, arguments, context)
                       for key, (name, arguments) in pending.items()}
            results = {}
            for key, future in futures.items():
                try:
                    results[key] = future.result()
                except ExecutionRevoked:
                    raise
                except Exception as exc:
                    results[key] = ToolResult('failed', key[0], data='查询执行失败，请稍后重试。',
                                              error_code=type(exc).__name__, retryable=True)
        return results

    @staticmethod
    def _history_messages(history: Sequence[Any]) -> list[dict[str, str]]:
        selected = []
        used_chars = 0
        for turn in reversed(history):
            user_content = str(getattr(turn, "user_content", ""))
            assistant_content = str(getattr(turn, "assistant_content", ""))
            turn_chars = len(user_content) + len(assistant_content)
            if selected and used_chars + turn_chars > MAX_HISTORY_CHARS:
                break
            selected.append((user_content, assistant_content))
            used_chars += turn_chars

        messages = []
        for user_content, assistant_content in reversed(selected):
            messages.append({"role": "user", "content": user_content})
            messages.append({"role": "assistant", "content": assistant_content})
        return messages

    @staticmethod
    def _serialize_tool_call(tool_call: Any) -> dict[str, Any]:
        return {
            "id": tool_call.id,
            "type": "function",
            "function": {
                "name": tool_call.function.name,
                "arguments": tool_call.function.arguments,
            },
        }

    @staticmethod
    def _parse_arguments(raw_arguments: str) -> tuple[dict[str, object], str]:
        try:
            arguments = json.loads(raw_arguments or "{}")
        except json.JSONDecodeError:
            return {}, "工具参数不是有效 JSON，请重新生成工具调用。"

        if not isinstance(arguments, dict):
            return {}, "工具参数必须是 JSON 对象。"

        normalized = {
            str(key): value.strip() if isinstance(value, str) else value
            for key, value in arguments.items()
            if value is not None
        }
        return normalized, ""


def run_read_agent(event,request,agent,agent_context,tool_context,*,decision=None,record=None):
    if request and tool_context:
        tool_context=replace(tool_context,event=event,execution_data={**(tool_context.execution_data or {}),
                              'reference_time':request.context.reference_time})
    result=agent.run(event.content,agent_context,'',tool_context,read_request=request,decision_override=decision)
    if request and result.status in {'completed','partial'}:
        pending=[query for query in request.queries if not any(trace.result_status=='completed'
            and trace.fact_state in {'known','empty'}
            and trace.name in TOOL_BY_INTENT[query.domain] and all(trace.arguments.get(k)==v for k,v in query.values.items()
                if k in TOOLS_BY_NAME[trace.name]['function']['parameters']['properties']) for trace in result.traces)]
        if pending:
            reply=result.reply+'\n\n尚未完成：'+'；'.join(' / '.join(q.values.values()) or q.domain for q in pending)+'。'
            update=replace(result.task_update,status='failed') if result.task_update else None
            result=replace(result,status='partial',reply=reply,task_update=update,answer_status='degraded',
                           answer_reasons=(*result.answer_reasons,'missing_queries'))
    if record is not None:
        record(event,'read_agent_result',decision_source=request.source if request else 'explicit_policy',
            answer_status=result.answer_status,reason='.'.join(result.answer_reasons),effect_status='none',
            execution_status=result.status,facts_hash=result.answer_facts_hash,
            candidate_count=len(request.queries) if request else 0)
    return result
