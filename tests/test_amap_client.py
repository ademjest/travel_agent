import unittest
from unittest.mock import Mock

import requests

from amap_client import AmapClient, AmapError


def geocode_response(address, location, adcode):
    return {
        "status": "1",
        "geocodes": [{
            "formatted_address": address,
            "location": location,
            "adcode": adcode,
        }],
    }


class AmapClientTests(unittest.TestCase):
    def setUp(self):
        self.client = AmapClient("test-key")
        self.client._get = Mock()

    def test_current_weather(self):
        self.client._get.side_effect = [
            geocode_response("青海省西宁市", "101.778916,36.623178", "630100"),
            {
                "status": "1",
                "lives": [{
                    "province": "青海",
                    "city": "西宁市",
                    "weather": "晴",
                    "temperature": "20",
                    "winddirection": "东",
                    "windpower": "3",
                    "humidity": "30",
                    "reporttime": "2026-07-15 10:00:00",
                }],
            },
        ]

        weather = self.client.current_weather("西宁")

        self.assertEqual(weather.temperature, "20")
        self.assertEqual(weather.location.adcode, "630100")

    def test_request_error_does_not_expose_api_key_or_signed_url(self):
        secret = "secret-amap-key"

        def failing_get(*args, **kwargs):
            request = requests.Request(
                "GET",
                args[0],
                params=kwargs["params"],
            ).prepare()
            raise requests.ConnectionError(
                "failed",
                request=request,
            )

        client = AmapClient(secret, http_get=failing_get)

        with self.assertRaises(AmapError) as raised:
            client._get("/v3/weather/weatherInfo", {"city": "630100"})

        message = str(raised.exception)
        self.assertNotIn(secret, message)
        self.assertNotIn("https://", message)
        self.assertIn("ConnectionError", message)

    def test_driving_route_aggregates_traffic(self):
        self.client._get.side_effect = [
            geocode_response("西宁市", "101.778916,36.623178", "630100"),
            geocode_response("青海湖", "100.225000,36.895000", "632800"),
            {
                "status": "1",
                "route": {
                    "paths": [{
                        "distance": "150000",
                        "cost": {
                            "duration": "9000",
                            "tolls": "20",
                            "traffic_lights": "8",
                        },
                        "steps": [
                            {
                                "road_name": "京藏高速",
                                "instruction": "沿京藏高速向西行驶",
                                "step_distance": "130000",
                                "tmcs": [
                                    {
                                        "tmc_status": "畅通",
                                        "tmc_distance": "90000",
                                        "tmc_polyline": (
                                            "101.778916,36.623178;"
                                            "101.000000,36.700000"
                                        ),
                                    },
                                    {
                                        "tmc_status": "缓行",
                                        "tmc_distance": "40000",
                                        "tmc_polyline": (
                                            "101.000000,36.700000|"
                                            "100.500000,36.800000"
                                        ),
                                    },
                                ],
                            },
                            {
                                "road_name": "京拉线",
                                "instruction": "沿京拉线继续行驶",
                                "step_distance": "20000",
                                "tmcs": [{
                                    "tmc_status": "拥堵",
                                    "tmc_distance": "20000",
                                    "tmc_polyline": (
                                        "100.500000,36.800000;"
                                        "100.225000,36.895000"
                                    ),
                                }],
                            },
                        ],
                    }]
                },
            },
        ]

        route = self.client.driving_route("西宁", "青海湖")

        self.assertEqual(route.distance_meters, 150000)
        self.assertEqual(route.duration_seconds, 9000)
        self.assertEqual(route.traffic_distances["拥堵"], 20000)
        self.assertEqual(len(route.traffic_segments), 3)

        congested_segment = route.traffic_segments[2]
        self.assertEqual(congested_segment.status, "拥堵")
        self.assertEqual(congested_segment.road_name, "京拉线")
        self.assertEqual(
            congested_segment.instruction,
            "沿京拉线继续行驶",
        )
        self.assertEqual(congested_segment.route_start_meters, 130000)
        self.assertEqual(congested_segment.route_end_meters, 150000)
        self.assertEqual(
            congested_segment.start_coordinates,
            "100.500000,36.800000",
        )
        self.assertEqual(
            congested_segment.end_coordinates,
            "100.225000,36.895000",
        )

    def test_geocode_rejects_multiple_distinct_candidates(self):
        self.client._get.return_value = {
            "status": "1",
            "geocodes": [
                {
                    "formatted_address": "北京市朝阳区",
                    "location": "116.443550,39.921900",
                    "adcode": "110105",
                },
                {
                    "formatted_address": "辽宁省朝阳市",
                    "location": "120.450372,41.573734",
                    "adcode": "211300",
                },
            ],
        }

        with self.assertRaises(AmapError) as raised:
            self.client.geocode("朝阳")

        message = str(raised.exception)
        self.assertIn("存在多个匹配结果", message)
        self.assertIn("北京市朝阳区", message)
        self.assertIn("辽宁省朝阳市", message)
        self.assertIn("请补充省、市或区县", message)

    def test_geocode_ignores_duplicate_candidate(self):
        duplicate = {
            "formatted_address": "青海省西宁市",
            "location": "101.778916,36.623178",
            "adcode": "630100",
        }
        self.client._get.return_value = {
            "status": "1",
            "geocodes": [duplicate, duplicate.copy()],
        }

        location = self.client.geocode("西宁")

        self.assertEqual(location.address, "青海省西宁市")

    def test_geocode_merges_same_address_with_different_coordinate_points(self):
        self.client._get.return_value = {
            "status": "1",
            "geocodes": [
                {
                    "formatted_address": "青海湖二郎剑景区",
                    "location": "100.225000,36.895000",
                    "adcode": "632500",
                },
                {
                    "formatted_address": "青海湖二郎剑景区",
                    "location": "100.225500,36.895500",
                    "adcode": "632500",
                },
            ],
        }

        location = self.client.geocode("青海湖二郎剑景区")

        self.assertEqual(location.address, "青海湖二郎剑景区")


if __name__ == "__main__":
    unittest.main()
