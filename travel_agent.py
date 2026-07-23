import json
import logging
import time
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Sequence

from openai import OpenAI

from context_builder import AgentContext, render_untrusted_context
from settings import Settings
from travel_decision import TravelDecision, decide_travel_action


MAX_AGENT_STEPS = 4
MAX_TOOL_CALLS = 6
LLM_TIMEOUT_SECONDS = 90.0
MAX_HISTORY_CHARS = 3000
MAX_DOCUMENT_SUMMARY_INPUT_CHARS = 20000


logger = logging.getLogger(__name__)


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_current_weather",
            "description": "查询一个中国地点当前的行政区级实时天气。",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "完整地点名称，例如西宁或青海湖二郎剑景区。",
                    }
                },
                "required": ["location"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather_forecast",
            "description": "查询一个中国地点未来数天的行政区级天气预报。",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "完整地点名称，例如青海湖或茶卡盐湖。",
                    }
                },
                "required": ["location"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_driving_route",
            "description": "查询两地之间的高德推荐驾车路线、距离、预计耗时和收费。",
            "parameters": {
                "type": "object",
                "properties": {
                    "origin": {
                        "type": "string",
                        "description": "驾车起点。",
                    },
                    "destination": {
                        "type": "string",
                        "description": "驾车终点。",
                    },
                },
                "required": ["origin", "destination"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_route_traffic",
            "description": (
                "查询两地之间的实时交通感知预计耗时、分段路况和拥堵风险。"
                "该工具已经包含路线距离和耗时，一般不需要再调用驾车路线工具。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "origin": {
                        "type": "string",
                        "description": "驾车起点。",
                    },
                    "destination": {
                        "type": "string",
                        "description": "驾车终点。",
                    },
                },
                "required": ["origin", "destination"],
                "additionalProperties": False,
            },
        },
    },
]


@dataclass(frozen=True)
class ToolTrace:
    name: str
    arguments: dict[str, str]


@dataclass(frozen=True)
class AgentResult:
    reply: str
    traces: tuple[ToolTrace, ...]


def _system_prompt(decision: TravelDecision) -> str:
    allowed_tools = "、".join(decision.allowed_tools) or "无"
    return f"""你是青甘自驾旅行风险助手。当前日期是 {date.today().isoformat()}。

工作方式：
1. 涉及当前天气、天气预报、路线、耗时或实时路况时，必须调用工具，禁止凭常识编造。
2. 信息不完整时直接向用户追问，不要猜测起点、终点或地点。
3. get_route_traffic 已包含距离和实时预计耗时，除非用户明确只问普通路线，否则不重复调用 get_driving_route。
4. 一次需要多个互不依赖的工具时，尽量在同一轮同时调用；不要重复调用相同工具和参数。
5. 青海湖等范围很大的地点用于驾车终点时，如果用户没有说明具体入口或景区，应先追问，不得自行替换成某个入口。
6. 当前天气是高德行政区级数据，不是景点微气候。做安全判断时必须说明这一限制。
7. 不要声称道路一定安全、一定开放或一定封闭；当前尚未接入交警封路公告和权威灾害预警。
8. 最终回答使用中文，先给结论，再列依据、建议、数据时间和局限。保持简洁。
9. 不展示内部思维链，只输出对用户有用的结论和可核验依据。
10. 预约计划和提醒的查看、刷新、确认、修改与取消只能使用确定性命令。不得声称已经创建或修改预约提醒，也不得编造命令。需要操作时指导用户使用“查看预约提醒”“刷新预约 R-计划编号”或“确认预约 R-计划编号”。

本次请求的确定性策略：intent={decision.intent}；允许工具={allowed_tools}；
回答详细度={decision.response_detail}。不得调用允许列表之外的工具。"""


class TravelAgent:
    def __init__(
            self,
            settings: Settings,
            tool_executor: Callable[[str, dict[str, str]], str],
            client: Any = None):
        if not settings.llm_configured:
            raise ValueError("LLM settings are incomplete")

        self.model = settings.llm_model_id
        self.tool_executor = tool_executor
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
            knowledge_context: str = "") -> AgentResult:
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
        decision = decide_travel_action(user_message)
        logger.info(
            "Travel decision: intent=%s allowed_tools=%s "
            "needs_clarification=%s",
            decision.intent,
            decision.allowed_tools,
            decision.needs_clarification,
        )
        if decision.needs_clarification:
            return AgentResult(
                reply="请告诉我驾车起点和终点。",
                traces=(),
            )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _system_prompt(decision)},
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
        tool_cache: dict[tuple[str, str], str] = {}
        allowed_tools = set(decision.allowed_tools)
        tool_definitions = [
            tool
            for tool in TOOLS
            if tool["function"]["name"] in allowed_tools
        ]

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
                if not reply:
                    reply = "暂时无法生成回答，请换一种问法重试。"
                logger.info(
                    "Agent result: intent=%s tool_names=%s",
                    decision.intent,
                    tuple(trace.name for trace in traces),
                )
                return AgentResult(reply=reply, traces=tuple(traces))

            messages.append({
                "role": "assistant",
                "content": assistant.content,
                "tool_calls": [self._serialize_tool_call(call) for call in tool_calls],
            })

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

                traces.append(ToolTrace(name=name, arguments=arguments))

                if name not in allowed_tools:
                    result = "当前请求的工具策略不允许调用该工具。"
                elif error:
                    result = error
                else:
                    cache_key = (
                        name,
                        json.dumps(
                            arguments,
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                    )
                    if cache_key in tool_cache:
                        result = tool_cache[cache_key]
                    else:
                        result = self.tool_executor(name, arguments)
                        tool_cache[cache_key] = result

                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result,
                })

        return AgentResult(
            reply="工具调用次数已达到上限，请缩小问题范围后重试。",
            traces=tuple(traces),
        )

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
    def _parse_arguments(raw_arguments: str) -> tuple[dict[str, str], str]:
        try:
            arguments = json.loads(raw_arguments or "{}")
        except json.JSONDecodeError:
            return {}, "工具参数不是有效 JSON，请重新生成工具调用。"

        if not isinstance(arguments, dict):
            return {}, "工具参数必须是 JSON 对象。"

        normalized = {
            str(key): str(value).strip()
            for key, value in arguments.items()
            if value is not None
        }
        return normalized, ""
