import unittest
from unittest.mock import Mock

from amap_client import AmapClient


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
                        "steps": [{
                            "tmcs": [
                                {"tmc_status": "畅通", "tmc_distance": "90000"},
                                {"tmc_status": "缓行", "tmc_distance": "40000"},
                                {"tmc_status": "拥堵", "tmc_distance": "20000"},
                            ]
                        }],
                    }]
                },
            },
        ]

        route = self.client.driving_route("西宁", "青海湖")

        self.assertEqual(route.distance_meters, 150000)
        self.assertEqual(route.duration_seconds, 9000)
        self.assertEqual(route.traffic_distances["拥堵"], 20000)


if __name__ == "__main__":
    unittest.main()
