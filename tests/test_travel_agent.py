import unittest
from types import SimpleNamespace

from settings import Settings
from travel_agent import TravelAgent
from memory_store import ConversationTurn


def tool_call(call_id, name, arguments):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def assistant_message(content=None, tool_calls=None):
    return SimpleNamespace(content=content, tool_calls=tool_calls or [])


def completion(message):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message)]
    )


class FakeCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return self.responses.pop(0)


class FakeClient:
    def __init__(self, responses):
        self.completions = FakeCompletions(responses)
        self.chat = SimpleNamespace(completions=self.completions)


class TravelAgentTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(
            appid="appid",
            secret="secret",
            allowed_group_openids=frozenset(),
            amap_api_key="amap-key",
            llm_api_key="llm-key",
            llm_base_url="https://example.test/v1",
            llm_model_id="test-model",
        )

    def test_agent_calls_weather_tool_then_answers(self):
        client = FakeClient([
            completion(assistant_message(tool_calls=[
                tool_call(
                    "call-1",
                    "get_current_weather",
                    '{"location":"西宁"}',
                )
            ])),
            completion(assistant_message(content="西宁当前晴，适合正常出行。")),
        ])
        calls = []

        def execute(name, arguments):
            calls.append((name, arguments))
            return "【当前天气】西宁\n天气：晴\n发布时间：10:00"

        agent = TravelAgent(self.settings, execute, client=client)
        result = agent.run("西宁现在天气怎么样？")

        self.assertEqual(result.reply, "西宁当前晴，适合正常出行。")
        self.assertEqual(calls, [
            ("get_current_weather", {"location": "西宁"})
        ])
        self.assertEqual(result.traces[0].name, "get_current_weather")
        self.assertEqual(len(client.completions.requests), 2)

    def test_agent_can_ask_for_missing_information(self):
        client = FakeClient([
            completion(assistant_message(content="请告诉我驾车起点和终点。"))
        ])
        agent = TravelAgent(
            self.settings,
            lambda name, arguments: "should not run",
            client=client,
        )

        result = agent.run("帮我看看路况")

        self.assertEqual(result.reply, "请告诉我驾车起点和终点。")
        self.assertEqual(result.traces, ())

    def test_invalid_tool_arguments_are_returned_to_model(self):
        client = FakeClient([
            completion(assistant_message(tool_calls=[
                tool_call("call-1", "get_current_weather", "not-json")
            ])),
            completion(assistant_message(content="请重新告诉我地点。")),
        ])
        calls = []
        agent = TravelAgent(
            self.settings,
            lambda name, arguments: calls.append((name, arguments)),
            client=client,
        )

        result = agent.run("天气如何")

        self.assertEqual(result.reply, "请重新告诉我地点。")
        self.assertEqual(calls, [])
        tool_message = client.completions.requests[1]["messages"][-1]
        self.assertIn("不是有效 JSON", tool_message["content"])

    def test_duplicate_tool_calls_use_request_cache(self):
        client = FakeClient([
            completion(assistant_message(tool_calls=[
                tool_call(
                    "call-1",
                    "get_current_weather",
                    '{"location":"西宁"}',
                ),
                tool_call(
                    "call-2",
                    "get_current_weather",
                    '{"location":"西宁"}',
                ),
            ])),
            completion(assistant_message(content="西宁当前天气已查询。")),
        ])
        calls = []

        def execute(name, arguments):
            calls.append((name, arguments))
            return "天气工具结果"

        agent = TravelAgent(self.settings, execute, client=client)
        result = agent.run("再确认一次西宁天气")

        self.assertEqual(result.reply, "西宁当前天气已查询。")
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(result.traces), 2)

    def test_history_and_document_context_are_sent_to_model(self):
        client = FakeClient([
            completion(assistant_message(content="你们计划住在茶卡镇。"))
        ])
        agent = TravelAgent(
            self.settings,
            lambda name, arguments: "not used",
            client=client,
        )
        history = [
            ConversationTurn(
                user_content="第一站去哪里？",
                assistant_content="第一站是青海湖。",
                created_at="2026-07-15T00:00:00+00:00",
            )
        ]

        result = agent.run(
            "我们住哪里？",
            history=history,
            knowledge_context="[plan.docx] 住宿安排：茶卡镇",
        )

        self.assertEqual(result.reply, "你们计划住在茶卡镇。")
        messages = client.completions.requests[0]["messages"]
        self.assertTrue(any("茶卡镇" in item["content"] for item in messages))
        self.assertTrue(any(item["content"] == "第一站去哪里？" for item in messages))

    def test_document_summary_uses_plain_chat_completion(self):
        client = FakeClient([
            completion(assistant_message(content="旅行日期：8月16日。住宿：茶卡镇。"))
        ])
        agent = TravelAgent(
            self.settings,
            lambda name, arguments: "not used",
            client=client,
        )

        summary = agent.summarize_document(
            "plan.docx",
            "8月16日从西宁出发，当晚住宿茶卡镇。",
        )

        self.assertIn("住宿：茶卡镇", summary)
        request = client.completions.requests[0]
        self.assertNotIn("tools", request)


if __name__ == "__main__":
    unittest.main()
