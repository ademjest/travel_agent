import unittest

from qq_ui import build_command_keyboard, build_group_message_payload


class QQGroupUiTests(unittest.TestCase):
    def test_keyboard_uses_official_group_command_actions(self):
        keyboard = build_command_keyboard()
        rows = keyboard["content"]["rows"]

        self.assertLessEqual(len(rows), 5)
        self.assertEqual(len(rows), 4)
        button_ids = set()
        commands = set()
        for row in rows:
            self.assertLessEqual(len(row["buttons"]), 5)
            for button in row["buttons"]:
                self.assertNotIn(button["id"], button_ids)
                button_ids.add(button["id"])
                self.assertIn(button["render_data"]["style"], (0, 1))
                action = button["action"]
                commands.add(action["data"])
                self.assertEqual(action["type"], 2)
                self.assertEqual(action["permission"], {"type": 2})
                self.assertFalse(action["reply"])
                self.assertFalse(action["enter"])
                self.assertTrue(action["data"])
                self.assertTrue(action["unsupport_tips"])
        self.assertIn("查看预约提醒", commands)
        self.assertIn("刷新预约 ", commands)

    def test_help_and_menu_use_markdown_with_keyboard(self):
        for content in ("帮助", "/帮助", "菜单", "旅行面板"):
            with self.subTest(content=content):
                payload = build_group_message_payload(content, "plain help")
                self.assertEqual(payload["msg_type"], 2)
                self.assertIn("青甘自驾助手", payload["markdown"]["content"])
                self.assertIn(
                    "确认前不会发送提醒",
                    payload["markdown"]["content"],
                )
                self.assertIn(
                    "查看预约提醒",
                    payload["markdown"]["content"],
                )
                self.assertIn(
                    "刷新预约 R-20260722-001",
                    payload["markdown"]["content"],
                )
                self.assertIn("keyboard", payload)
                self.assertNotIn("content", payload)

    def test_status_uses_reply_in_markdown_panel(self):
        payload = build_group_message_payload(
            "/状态",
            "Bot 已在线。高德数据源：已配置；LLM Agent：已配置。",
        )

        self.assertEqual(payload["msg_type"], 2)
        markdown = payload["markdown"]["content"]
        self.assertIn("旅行助手状态", markdown)
        self.assertIn("高德数据源：已配置", markdown)
        self.assertIn("LLM Agent：已配置", markdown)

    def test_ordinary_reply_remains_plain_text(self):
        payload = build_group_message_payload(
            "查询天气 西宁",
            "西宁当前晴，18℃。",
        )

        self.assertEqual(payload, {
            "msg_type": 0,
            "content": "西宁当前晴，18℃。",
        })


if __name__ == "__main__":
    unittest.main()
