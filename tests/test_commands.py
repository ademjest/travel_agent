import unittest

from commands import build_reply, normalize_command, parse_command


class CommandTests(unittest.TestCase):
    def test_normalize_command(self):
        self.assertEqual(normalize_command("  查询   天气  "), "查询 天气")
        self.assertEqual(normalize_command(" / 查询天气  西宁 "), "查询天气 西宁")

    def test_ping(self):
        self.assertEqual(build_reply(" PING "), "pong")

    def test_help(self):
        self.assertIn("查询天气", build_reply("帮助"))

    def test_help_menu_aliases(self):
        for content in ("菜单", "旅行面板", "/帮助", "/help"):
            with self.subTest(content=content):
                self.assertEqual(parse_command(content).name, "help")

    def test_weather_command(self):
        command = parse_command("查询天气 青海湖")
        self.assertEqual(command.name, "weather")
        self.assertEqual(command.args, ("青海湖",))

    def test_slash_prefix_is_supported_for_all_panel_commands(self):
        cases = {
            "/状态": "status",
            "/查询天气 西宁": "weather",
            "/天气预报 青海湖": "forecast",
            "/查询路线 西宁 -> 青海湖": "route",
            "/查询路况 青海湖 -> 茶卡盐湖": "traffic",
            "/上传文档": "upload_document",
        }
        for content, expected_name in cases.items():
            with self.subTest(content=content):
                self.assertEqual(parse_command(content).name, expected_name)

    def test_document_upload_commands(self):
        for content in ("上传文档", "文档上传", "导入文档"):
            with self.subTest(content=content):
                self.assertEqual(parse_command(content).name, "upload_document")

    def test_route_command_with_arrow(self):
        command = parse_command("查询路线 西宁 -> 青海湖二郎剑景区")
        self.assertEqual(command.name, "route")
        self.assertEqual(command.args, ("西宁", "青海湖二郎剑景区"))

    def test_traffic_command_with_to(self):
        command = parse_command("查询路况 青海湖 到 茶卡盐湖")
        self.assertEqual(command.name, "traffic")
        self.assertEqual(command.args, ("青海湖", "茶卡盐湖"))

    def test_route_requires_two_places(self):
        command = parse_command("查询路线 西宁")
        self.assertIn("用法", command.error)

    def test_reservation_draft_commands(self):
        cases = {
            "补充预约 R-20260722-001 2 2026-08-20": (
                "reservation_complete_date",
                ("R-20260722-001", "2", "2026-08-20"),
            ),
            "新增预约 R-20260722-001 莫高窟 2026-08-20 提前1月": (
                "reservation_add_item",
                (
                    "R-20260722-001",
                    "莫高窟",
                    "2026-08-20",
                    "1",
                    "month",
                    "1",
                ),
            ),
            "新增预约 R-20260722-001 黑独山 2026-08-22 无需预约": (
                "reservation_add_item",
                (
                    "R-20260722-001",
                    "黑独山",
                    "2026-08-22",
                    "0",
                    "none",
                    "0",
                ),
            ),
            "设置提醒 R-20260722-001 1 2026-08-15 07:30": (
                "reservation_set_times",
                ("R-20260722-001", "1", "2026-08-15 07:30"),
            ),
            "确认预约 R-20260722-001": (
                "reservation_confirm",
                ("R-20260722-001",),
            ),
            "取消预约 R-20260722-001": (
                "reservation_cancel_plan",
                ("R-20260722-001",),
            ),
        }
        for content, expected in cases.items():
            with self.subTest(content=content):
                command = parse_command(content)
                self.assertEqual((command.name, command.args), expected)

    def test_reservation_management_commands(self):
        cases = {
            "查看预约提醒": ("reservation_list", ()),
            "修改预约提醒 A-000123 游览日期 2026-08-21": (
                "reservation_modify_date",
                ("A-000123", "2026-08-21"),
            ),
            (
                "修改预约提醒 A-000123 时间 "
                "2026-07-20 20:00, 2026-07-21 07:30"
            ): (
                "reservation_modify_times",
                (
                    "A-000123",
                    "2026-07-20 20:00, 2026-07-21 07:30",
                ),
            ),
            "取消预约提醒 A-000123": (
                "reservation_cancel_item",
                ("A-000123",),
            ),
        }
        for content, expected in cases.items():
            with self.subTest(content=content):
                command = parse_command(content)
                self.assertEqual((command.name, command.args), expected)

    def test_invalid_reservation_time_is_left_for_strict_service_validation(self):
        command = parse_command("设置提醒 R-20260722-001 1 明早七点")
        self.assertEqual(command.name, "reservation_set_times")
        self.assertEqual(command.args[-1], "明早七点")


if __name__ == "__main__":
    unittest.main()
