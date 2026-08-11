import unittest

from agents.travel_decision import decide_travel_action


class TravelDecisionTests(unittest.TestCase):
    def test_current_weather_requires_current_weather_tool(self):
        decision = decide_travel_action("查询天气 西宁")

        self.assertEqual(decision.intent, "weather")
        self.assertTrue(decision.require_live_data)
        self.assertEqual(
            decision.allowed_tools,
            ("get_current_weather",),
        )

    def test_route_traffic_excludes_duplicate_driving_route_tool(self):
        decision = decide_travel_action("查询路况 西宁 -> 青海湖")

        self.assertEqual(decision.intent, "traffic")
        self.assertEqual(decision.allowed_tools, ("get_route_traffic",))
        self.assertNotIn("get_driving_route", decision.allowed_tools)

    def test_missing_route_endpoints_requires_clarification(self):
        decision = decide_travel_action("帮我看看路况")

        self.assertTrue(decision.needs_clarification)

    def test_document_question_does_not_force_amap_tools(self):
        decision = decide_travel_action("文档里的住宿安排是什么？")

        self.assertEqual(decision.intent, "document")
        self.assertFalse(decision.require_live_data)
        self.assertEqual(decision.allowed_tools, ())

    def test_xlsx_filename_question_is_document_intent(self):
        decision = decide_travel_action(
            "根据我上传的青甘大环线自驾行程安排.xlsx，列出8月17日的全部行程"
        )

        self.assertEqual(decision.intent, "document")
        self.assertEqual(decision.allowed_tools, ())

    def test_weather_and_traffic_can_be_selected_together(self):
        decision = decide_travel_action(
            "明天从西宁到青海湖，天气和路况怎么样？"
        )

        self.assertEqual(decision.intents, ("traffic", "forecast"))
        self.assertEqual(
            decision.allowed_tools,
            ("get_route_traffic", "get_weather_forecast"),
        )
        self.assertFalse(decision.needs_clarification)

    def test_natural_reservation_action_exposes_reservation_tools(self):
        decision = decide_travel_action("帮我确认预约 R-20260722-001")

        self.assertEqual(decision.intent, "reservation")
        self.assertIn("list_reservation_plans", decision.allowed_tools)
        self.assertIn("confirm_reservation_plan", decision.allowed_tools)
        self.assertTrue(decision.required_tool_groups)

    def test_plan_code_edit_is_reservation_intent_without_keyword(self):
        decision = decide_travel_action(
            "把 R-20260811-001 的嘉峪关日期补为 2026-08-21，"
            "水上雅丹不去参观了"
        )

        self.assertEqual(decision.intent, "reservation")
        self.assertEqual(
            decision.allowed_tools,
            ("update_reservation_draft_items",),
        )
        self.assertEqual(
            decision.required_tool_groups,
            (("update_reservation_draft_items",),),
        )

    def test_image_reservation_intent_requires_creation_tool(self):
        decision = decide_travel_action("按这张攻略帮我制定预约")

        self.assertIn(
            "create_reservation_draft_from_image",
            decision.allowed_tools,
        )
        self.assertIn(
            ("create_reservation_draft_from_image",),
            decision.required_tool_groups,
        )
