import json
import inspect
import logging
import time
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Callable, Sequence
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context

from openai import OpenAI

from agents.clarification import is_weather_clarification, missing_weather_location, resume_weather_request
from agents.context_builder import AgentContext, render_untrusted_context
from agents.travel_decision import TravelDecision, decide_travel_action, TOOL_BY_INTENT
from core.settings import Settings
from core.model_budget import ModelBudgetExceeded
from core.model_budget import MODEL_EVENT
from core.execution_scope import ExecutionRevoked
from core.tasks import TaskUpdate, READ_TASK_TYPES, read_task_continuation
from core.tool_result import ToolResult, validate_arguments
from tools.agent_tools import AgentToolContext, TOOLS_BY_NAME, RESERVATION_TOOL_NAMES, TRAVEL_TOOL_NAMES, ASK_USER_TOOL


MAX_AGENT_STEPS = 4
MAX_TOOL_CALLS = 6
LLM_TIMEOUT_SECONDS = 90.0
MAX_HISTORY_CHARS = 3000
MAX_DOCUMENT_SUMMARY_INPUT_CHARS = 20000
MAX_USER_MESSAGE_CHARS = 8000


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolTrace:
    name: str
    arguments: dict[str, object]
    call_id: str = ''


@dataclass(frozen=True)
class AgentResult:
    reply: str
    traces: tuple[ToolTrace, ...]
    status: str = "completed"
    task_update: TaskUpdate | None = None
    missing_slots: tuple[str, ...] = ()


def _system_prompt(decision: TravelDecision, reference_time: str = '') -> str:
    names = decision.allowed_tools
    if names and set(decision.intents) <= READ_TASK_TYPES:
        names = (*names, ASK_USER_TOOL)
    allowed_tools = "、".join(names) or "无"
    current_date = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    intents = "、".join(decision.intents)
    reference_note = ''
    if reference_time:
        reference_note = ('本次问题的时间参照为北京时间 ' + datetime.fromisoformat(reference_time).astimezone(ZoneInfo('Asia/Shanghai')).isoformat()
                          + '；“明天/后天”相对此时间解释。工具返回执行时能取得的数据，不得把超出覆盖范围的日期说成已查到。')
    return f"""你是彼岸旅行助手，帮助用户规划旅行、查询出行信息、整理资料和推进旅行待办。预约只是其中一种能力。当前北京时间日期是 {current_date}。
{reference_note}

工作方式：
1. 涉及当前天气、天气预报、路线、耗时或实时路况时，必须调用工具，禁止凭常识编造。
2. 信息不完整时使用 request_missing_input 提出必要追问（如果本轮提供此工具），不要猜测起点、终点或地点。
3. get_route_traffic 已包含距离和实时预计耗时，除非用户明确只问普通路线，否则不重复调用 get_driving_route。
4. 一次需要多个互不依赖的工具时，尽量在同一轮同时调用；不要重复调用相同工具和参数。
5. 青海湖等范围很大的地点用于驾车终点时，如果用户没有说明具体入口或景区，应先追问，不得自行替换成某个入口。
6. 当前天气是高德行政区级数据，不是景点微气候。做安全判断时必须说明这一限制。
7. 不要声称道路一定安全、一定开放或一定封闭；当前尚未接入交警封路公告和权威灾害预警。
8. 最终回答使用中文，先给结论，再列依据、建议、数据时间和局限。保持简洁。
9. 不展示内部思维链，只输出对用户有用的结论和可核验依据。
10. 图片预约计划及其关联提醒的查看、刷新、确认、修改与取消必须调用本轮提供的预约工具。工具已经绑定当前平台、群和发送者权限；不得猜测计划编号或项目编号，不得在没有成功工具结果时声称操作完成。
11. 根据图片创建预约草稿必须调用 create_reservation_draft_from_image，并且只能选择当前消息中由程序提供的附件，不得编造图片 URL。
12. 用户同时询问多个互不依赖的问题时，应在同一轮提出所有必要工具调用；外部只读查询可并发，业务修改按顺序执行。
13. 普通个人提醒由上层应用服务解析、保存和调度。本轮允许工具列表不代表整个应用的能力；没有成功执行结果时只能说明本次提醒尚未确认创建，不能因为本轮缺少相关工具而断言系统没有提醒功能，也不能冒充已经完成设置。

本次请求的确定性策略：intents={intents}；允许工具={allowed_tools}；
回答详细度={decision.response_detail}。不得调用允许列表之外的工具。"""


class TravelAgent:
    def __init__(
            self,
            settings: Settings,
            tool_executor: Callable[[str, dict[str, object]], str],
            client: Any = None):
        if not settings.llm_configured:
            raise ValueError("LLM settings are incomplete")

        self.model = settings.llm_model_id
        self.tool_executor = tool_executor
        try:
            parameters = inspect.signature(tool_executor).parameters
            self._tool_executor_accepts_context = len(parameters) >= 3
        except (TypeError, ValueError):
            self._tool_executor_accepts_context = False
        self.client = client or OpenAI(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            timeout=LLM_TIMEOUT_SECONDS,
            max_retries=1,
        )

    def run(
            self,
            user_message: str,
            history: Sequence[Any] | AgentContext = (),
            knowledge_context: str = "",
            tool_context: AgentToolContext | None = None) -> AgentResult:
        tasks = history.tasks if isinstance(history, AgentContext) else ()
        task, resolved_message = read_task_continuation(user_message, tasks)
        reference_time = history.message_time if isinstance(history, AgentContext) else ''
        if task and not any(word in user_message for word in ('今天', '明天', '后天')):
            reference_time = task.slots.get('reference_time') or reference_time
        decision = decide_travel_action(user_message, has_document_context=bool(
            history.document_context if isinstance(history, AgentContext) else knowledge_context))
        if task is not None:
            intents = tuple(value for value in task.slots.get('intents', [task.task_type]) if value in READ_TASK_TYPES)
            if task.task_type in {'weather', 'forecast'} and any(word in user_message for word in ('明天', '后天')):
                intents = ('forecast',)
            allowed = tuple(dict.fromkeys(tool for intent in intents for tool in TOOL_BY_INTENT[intent]))
            decision = replace(decision, intent=intents[0], intents=intents, allowed_tools=allowed,
                required_tool_groups=tuple(TOOL_BY_INTENT[intent] for intent in intents), require_live_data=True,
                needs_clarification=False, action_resources=())
        result = self._run(resolved_message, history, knowledge_context, tool_context,
                           decision_override=decision if task is not None else None, reference_time=reference_time)
        if set(decision.intents) <= READ_TASK_TYPES:
            if task is None:
                family = {'weather', 'forecast'} if set(decision.intents) <= {'weather', 'forecast'} else set(decision.intents)
                waiting = [value for value in tasks if value.task_type in family
                           and value.status == 'collecting']
                task = waiting[0] if len(waiting) == 1 else None
            missing = result.missing_slots if result.status == 'needs_input' else ()
            if not missing and result.status == 'needs_input' and decision.intent in {'weather', 'forecast'}:
                missing = ('location',)
            slots = dict(task.slots) if task else {}
            slots['intents'] = list(decision.intents)
            if reference_time:
                slots['reference_time'] = reference_time
            for trace in result.traces:
                for field in ('location', 'city', 'origin', 'destination', 'keywords'):
                    if trace.arguments.get(field):
                        slots[field] = trace.arguments[field]
            update = TaskUpdate(
                task_type=decision.intent,
                status="collecting" if missing else result.status,
                initial_request=resolved_message,
                slots=slots, missing_slots=missing,
                task_id=task.task_id if task else "",
                expected_version=task.version if task else None,
            )
            return replace(result, task_update=update)
        return result

    def _run(
            self,
            user_message: str,
            history: Sequence[Any] | AgentContext = (),
            knowledge_context: str = "",
            tool_context: AgentToolContext | None = None,
            decision_override: TravelDecision | None = None,
            reference_time: str = '') -> AgentResult:
        if len(user_message) > MAX_USER_MESSAGE_CHARS:
            return AgentResult(
                reply="消息正文超过 8000 字符限制，请精简后重试。",
                traces=(),
            )
        structured_context = (
            history if isinstance(history, AgentContext) else None
        )
        if structured_context is not None:
            recent_dialogue = structured_context.recent_dialogue
            knowledge_context = structured_context.document_context
            group_context = structured_context.group_context
            source_note = structured_context.source_note
        else:
            recent_dialogue = history
            group_context = ""
            source_note = ""
        if not structured_context or not structured_context.uses_task_state:
            user_message = resume_weather_request(user_message, recent_dialogue)
        decision = decision_override or decide_travel_action(user_message, has_document_context=bool(knowledge_context))
        if (set(decision.intents) <= {"weather", "forecast"}
                and missing_weather_location(user_message)):
            return AgentResult("请告诉我需要查询哪个城市的天气？", (), status="needs_input")
        logger.info(
            "Travel decision: intent=%s allowed_tools=%s "
            "needs_clarification=%s",
            decision.intent,
            decision.allowed_tools,
            decision.needs_clarification,
        )
        if decision.needs_clarification:
            return AgentResult(
                reply=decision.clarification_reply or "请告诉我驾车起点和终点。",
                traces=(),
                status="needs_input",
                missing_slots=('origin', 'destination') if decision.intent in {'route', 'traffic', 'transit', 'walking'} else (),
            )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _system_prompt(decision, reference_time)},
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
        messages.append({"role": "user", "content": user_message})
        traces: list[ToolTrace] = []
        tool_cache: dict[tuple[str, str], ToolResult] = {}
        receipts: list[ToolResult] = []
        failures: list[ToolResult] = []
        requested_actions = set(decision.action_resources)
        allowed_tools = set(decision.allowed_tools)
        tool_definitions = [
            TOOLS_BY_NAME[name]
            for name in decision.allowed_tools
            if name in TOOLS_BY_NAME
        ]
        if set(decision.intents) <= READ_TASK_TYPES and tool_definitions:
            allowed_tools.add(ASK_USER_TOOL)
            tool_definitions.append(TOOLS_BY_NAME[ASK_USER_TOOL])
        executed_tools: set[str] = set()

        for step_index in range(1, MAX_AGENT_STEPS + 1):
            started_at = time.monotonic()
            try:
                request = {
                    "model": self.model,
                    "messages": messages,
                }
                if tool_definitions:
                    request.update({
                        "tools": tool_definitions,
                        "tool_choice": "auto",
                    })
                response = self.client.chat.completions.create(
                    **request,
                )
            except ModelBudgetExceeded as exc:
                completed = [receipt.data for receipt in receipts if receipt.action in RESERVATION_TOOL_NAMES
                             and receipt.action != 'list_reservation_plans']
                return AgentResult('\n'.join([*completed, str(exc)]), tuple(traces), status='failed')
            except Exception:
                logger.warning(
                    "LLM step failed: llm_step=%s elapsed_seconds=%.2f "
                    "message_count=%s model=%s",
                    step_index,
                    time.monotonic() - started_at,
                    len(messages),
                    self.model,
                )
                raise
            logger.info(
                "LLM step completed: llm_step=%s elapsed_seconds=%.2f "
                "message_count=%s model=%s",
                step_index,
                time.monotonic() - started_at,
                len(messages),
                self.model,
            )
            assistant = response.choices[0].message
            tool_calls = list(assistant.tool_calls or [])

            if not tool_calls:
                reply = (assistant.content or "").strip()
                if (not executed_tools and set(decision.intents) <= {"weather", "forecast"}
                        and is_weather_clarification(reply)):
                    return AgentResult(reply=reply, traces=(), status="needs_input")
                missing_groups = [
                    group
                    for group in decision.required_tool_groups
                    if not executed_tools.intersection(group)
                ]
                missing_resources = requested_actions - {
                    (receipt.action, receipt.resource_id) for receipt in receipts
                    if receipt.status == "completed"
                }
                if (missing_groups or missing_resources) and step_index < MAX_AGENT_STEPS:
                    missing_names = "、".join(
                        "/".join(group) for group in missing_groups
                    )
                    messages.append({
                        "role": "system",
                        "content": (
                            "当前回答仍缺少必须调用的工具："
                            f"{missing_names}；未完成的动作与对象：{sorted(missing_resources)}。"
                            "请先调用工具，再给出结论。"
                        ),
                    })
                    continue
                if missing_groups or missing_resources:
                    return AgentResult(
                        reply="\n".join([
                            *(receipt.data for receipt in receipts if receipt.action in RESERVATION_TOOL_NAMES
                              and receipt.action != "list_reservation_plans"),
                            "需要的实时或预约工具未完成，请补充信息后重试。",
                            *(failure.data for failure in failures[-1:]),
                        ]),
                        status="failed",
                        traces=tuple(traces),
                    )
                reply = (assistant.content or "").strip()
                if not reply:
                    reply = "暂时无法生成回答，请换一种问法重试。"
                logger.info(
                    "Agent result: intent=%s tool_names=%s",
                    decision.intent,
                    tuple(trace.name for trace in traces),
                )
                mutations = [receipt for receipt in receipts if receipt.status == "completed"
                             and receipt.action in RESERVATION_TOOL_NAMES
                             and receipt.action != "list_reservation_plans"]
                if mutations:
                    reply = "\n".join(dict.fromkeys(
                        receipt.data for receipt in receipts
                        if receipt.action != "list_reservation_plans"
                    ))
                return AgentResult(reply=reply, traces=tuple(traces))

            messages.append({
                "role": "assistant",
                "content": assistant.content,
                "tool_calls": [self._serialize_tool_call(call) for call in tool_calls],
            })

            parallel_results = self._parallel_reads(tool_calls, allowed_tools, tool_context, tool_cache, len(traces))
            for tool_call in tool_calls:
                name = tool_call.function.name
                arguments, error = self._parse_arguments(
                    tool_call.function.arguments
                )

                if len(traces) >= MAX_TOOL_CALLS:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": "工具调用总数已达到上限，请基于已有结果回答。",
                    })
                    continue

                traces.append(ToolTrace(name=name, arguments=arguments, call_id=str(tool_call.id)))

                if not error and name in TOOLS_BY_NAME:
                    error = validate_arguments(arguments, TOOLS_BY_NAME[name]["function"]["parameters"])
                resource_id = str(arguments.get("plan_code") or arguments.get("item_code") or "")
                allowed_resources = {resource for action, resource in requested_actions if action == name}
                if name not in allowed_tools:
                    result = ToolResult("failed", name, data="当前请求的工具策略不允许调用该工具。", error_code="not_allowed")
                elif error:
                    result = ToolResult("failed", name, data=error, error_code="invalid_json", retryable=True)
                elif name == ASK_USER_TOOL:
                    question = str(arguments['question']).strip()
                    if not question or len(question) > 500:
                        result = ToolResult('failed', name, data='请提供 1 到 500 字的必要追问。', error_code='invalid_question')
                    else:
                        return AgentResult(question, tuple(traces), status='needs_input',
                                           missing_slots=tuple(dict.fromkeys(arguments['missing_fields'])))
                elif allowed_resources and resource_id not in allowed_resources:
                    result = ToolResult("failed", name, resource_id, data="工具对象不在本次请求指定范围内。", error_code="wrong_resource")
                else:
                    cache_key = (name, json.dumps(arguments, ensure_ascii=False, sort_keys=True))
                    cacheable = name not in RESERVATION_TOOL_NAMES
                    if cacheable and cache_key in tool_cache:
                        result = tool_cache[cache_key]
                    else:
                        raw_result = parallel_results[cache_key] if cache_key in parallel_results else self._invoke_tool(name, arguments, tool_context)
                        result = raw_result if isinstance(raw_result, ToolResult) else ToolResult.from_text(name, raw_result, resource_id)
                        if cacheable and result.status == "completed":
                            tool_cache[cache_key] = result
                    if result.status == "needs_input":
                        return AgentResult(result.data, tuple(traces), status="needs_input")
                    if result.status == "completed" and result.action == name and result.resource_id == resource_id:
                        executed_tools.add(name)
                        receipts.append(result)

                if result.status == "failed":
                    failures.append(result)
                logger.info('Tool receipt: %s', json.dumps({'event_id': MODEL_EVENT.get(), 'call_id': str(tool_call.id),
                            'name': name, 'status': result.status, 'error_code': result.error_code}, ensure_ascii=False))
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result.to_json(),
                })

        return AgentResult(
            reply="工具调用次数已达到上限，请缩小问题范围后重试。",
            status="failed",
            traces=tuple(traces),
        )

    def _invoke_tool(self, name, arguments, context):
        if self._tool_executor_accepts_context:
            return self.tool_executor(name, arguments, context)
        return self.tool_executor(name, arguments)

    def _parallel_reads(self, calls, allowed, context, cache, trace_count):
        if len(calls) < 2 or trace_count + len(calls) > MAX_TOOL_CALLS:
            return {}
        pending = {}
        for call in calls:
            name = call.function.name
            arguments, error = self._parse_arguments(call.function.arguments)
            if (name not in TRAVEL_TOOL_NAMES or name not in allowed or error
                    or validate_arguments(arguments, TOOLS_BY_NAME[name]['function']['parameters'])):
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

    def summarize_document(self, filename: str, text: str) -> str:
        source = text[:MAX_DOCUMENT_SUMMARY_INPUT_CHARS]
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你负责为自驾旅行 Agent 整理长期资料。只提取文档中明确写出的事实，"
                        "禁止推测。使用中文，控制在 1800 字以内。按以下字段组织："
                        "旅行日期、每日路线、住宿、集合与出发时间、车辆与成员限制、"
                        "已确认事项、待确认事项。缺失字段可以省略。"
                    ),
                },
                {
                    "role": "user",
                    "content": f"文件名：{filename}\n\n文档内容：\n{source}",
                },
            ],
        )
        return (response.choices[0].message.content or "").strip()

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
