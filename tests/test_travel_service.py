import unittest

from settings import Settings
from travel_service import TravelService, _format_duration


class TravelServiceTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(
            appid="appid",
            secret="secret",
            allowed_group_openids=frozenset(),
            amap_api_key="",
            llm_api_key="",
            llm_base_url="",
            llm_model_id="",
        )
        self.service = TravelService(self.settings)

    def test_status_reports_missing_amap_key(self):
        reply = self.service.handle("状态")
        self.assertIn("未配置 AMAP_API_KEY", reply)
        self.assertIn("LLM Agent：未完整配置", reply)

    def test_weather_explains_missing_amap_key(self):
        reply = self.service.handle("查询天气 西宁")
        self.assertIn("尚未配置 AMAP_API_KEY", reply)

    def test_exact_hour_duration(self):
        self.assertEqual(_format_duration(3600), "1 小时")


if __name__ == "__main__":
    unittest.main()
