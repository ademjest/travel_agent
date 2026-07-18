import unittest
from unittest.mock import Mock

from amap_client import AmapError, Location, RouteSummary, TrafficSegment
from settings import Settings
from travel_service import TravelService, _format_duration, _format_traffic


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

    def test_traffic_low_coverage_does_not_claim_low_risk(self):
        route = RouteSummary(
            origin=Location("西宁", "西宁市", 101.7, 36.6, "630100"),
            destination=Location(
                "青海湖",
                "青海湖二郎剑景区",
                100.2,
                36.8,
                "632800",
            ),
            distance_meters=100000,
            duration_seconds=7200,
            tolls_yuan="0",
            traffic_lights=3,
            traffic_distances={"畅通": 20000},
        )

        reply = _format_traffic(route)

        self.assertIn("路况数据覆盖率：20%", reply)
        self.assertIn("车辆拥堵风险：数据覆盖不足，暂不评级", reply)
        self.assertNotIn("车辆拥堵风险：低", reply)

    def test_traffic_lists_congested_segments_with_locations(self):
        route = RouteSummary(
            origin=Location("西宁", "西宁市", 101.7, 36.6, "630100"),
            destination=Location(
                "青海湖",
                "青海湖二郎剑景区",
                100.2,
                36.8,
                "632800",
            ),
            distance_meters=100000,
            duration_seconds=7200,
            tolls_yuan="0",
            traffic_lights=3,
            traffic_distances={
                "畅通": 80000,
                "拥堵": 10000,
                "严重拥堵": 10000,
            },
            traffic_segments=(
                TrafficSegment(
                    status="拥堵",
                    road_name="京拉线",
                    instruction="沿京拉线向西行驶",
                    distance_meters=10000,
                    route_start_meters=80000,
                    route_end_meters=90000,
                    start_coordinates="100.500000,36.800000",
                    end_coordinates="100.350000,36.850000",
                ),
                TrafficSegment(
                    status="严重拥堵",
                    road_name="环湖东路",
                    instruction="继续沿环湖东路行驶",
                    distance_meters=10000,
                    route_start_meters=90000,
                    route_end_meters=100000,
                    start_coordinates=None,
                    end_coordinates=None,
                ),
            ),
        )

        reply = _format_traffic(route)

        self.assertIn("路况数据覆盖率：100%", reply)
        self.assertIn("车辆拥堵风险：高", reply)
        self.assertIn("具体拥堵路段：", reply)
        self.assertIn("1. 拥堵：京拉线", reply)
        self.assertIn("约沿路线 80.0 公里—90.0 公里", reply)
        self.assertIn(
            "坐标 100.500000,36.800000 → 100.350000,36.850000",
            reply,
        )
        self.assertIn("2. 严重拥堵：环湖东路", reply)

    def test_fixed_route_command_surfaces_geocode_disambiguation(self):
        self.service.amap = Mock()
        self.service.amap.driving_route.side_effect = AmapError(
            "地点“朝阳”存在多个匹配结果，请补充省、市或区县后重试"
        )

        reply = self.service.handle("查询路线 朝阳 -> 西宁")

        self.assertIn("存在多个匹配结果", reply)
        self.assertIn("请补充省、市或区县", reply)


if __name__ == "__main__":
    unittest.main()
