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


if __name__ == "__main__":
    unittest.main()
