from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

import requests


AMAP_BASE_URL = "https://restapi.amap.com"
REQUEST_TIMEOUT = 10


class AmapError(RuntimeError):
    pass


@dataclass(frozen=True)
class Location:
    query: str
    address: str
    longitude: float
    latitude: float
    adcode: str

    @property
    def coordinates(self) -> str:
        return f"{self.longitude:.6f},{self.latitude:.6f}"


@dataclass(frozen=True)
class CurrentWeather:
    location: Location
    province: str
    city: str
    weather: str
    temperature: str
    wind_direction: str
    wind_power: str
    humidity: str
    report_time: str


@dataclass(frozen=True)
class ForecastDay:
    date: str
    day_weather: str
    night_weather: str
    day_temperature: str
    night_temperature: str
    day_wind: str
    day_power: str


@dataclass(frozen=True)
class WeatherForecast:
    location: Location
    city: str
    report_time: str
    days: tuple[ForecastDay, ...]


@dataclass(frozen=True)
class TrafficSegment:
    status: str
    road_name: str
    instruction: str
    distance_meters: int
    route_start_meters: int
    route_end_meters: int
    start_coordinates: str | None
    end_coordinates: str | None


@dataclass(frozen=True)
class RouteSummary:
    origin: Location
    destination: Location
    distance_meters: int
    duration_seconds: int
    tolls_yuan: str
    traffic_lights: int
    traffic_distances: dict[str, int]
    traffic_segments: tuple[TrafficSegment, ...] = ()


def _as_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    return []


def _to_int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


_COORDINATE_PATTERN = re.compile(
    r"(-?\d+(?:\.\d+)?),\s*(-?\d+(?:\.\d+)?)"
)


def _parse_coordinate_pair(raw_location: Any) -> tuple[float, float]:
    longitude_text, latitude_text = str(raw_location or "").split(",", 1)
    return float(longitude_text), float(latitude_text)


def _polyline_endpoints(
        raw_polyline: Any) -> tuple[str | None, str | None]:
    matches = _COORDINATE_PATTERN.findall(str(raw_polyline or ""))
    if not matches:
        return None, None

    def format_point(point: tuple[str, str]) -> str:
        longitude, latitude = point
        return f"{float(longitude):.6f},{float(latitude):.6f}"

    return format_point(matches[0]), format_point(matches[-1])


class AmapClient:
    def __init__(
            self,
            api_key: str,
            http_get: Callable[..., Any] = requests.get):
        if not api_key:
            raise ValueError("AMAP_API_KEY is required")
        self.api_key = api_key
        self.http_get = http_get

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        query = {**params, "key": self.api_key, "output": "JSON"}
        try:
            response = self.http_get(
                f"{AMAP_BASE_URL}{path}",
                params=query,
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as exc:
            raise AmapError(
                f"高德接口请求失败：{type(exc).__name__}"
            ) from exc
        except ValueError as exc:
            raise AmapError("高德接口返回了无法解析的数据") from exc

        if str(data.get("status")) != "1":
            info = data.get("info") or "未知错误"
            infocode = data.get("infocode") or ""
            raise AmapError(f"高德接口返回错误：{info} {infocode}".strip())

        return data

    def geocode(self, place: str) -> Location:
        data = self._get("/v3/geocode/geo", {"address": place})
        geocodes = _as_list(data.get("geocodes"))
        if not geocodes:
            raise AmapError(f"没有找到地点：{place}")

        candidates: list[Location] = []
        seen_candidates: set[str] = set()
        for result in geocodes:
            try:
                longitude, latitude = _parse_coordinate_pair(
                    result.get("location")
                )
            except (TypeError, ValueError):
                continue

            adcode = str(result.get("adcode") or "")
            address = str(result.get("formatted_address") or place)
            normalized_address = re.sub(
                r"[\s,，。;；]+",
                "",
                address,
            ).lower()
            if normalized_address or adcode:
                signature = f"{normalized_address}|{adcode}"
            else:
                signature = f"{longitude:.6f},{latitude:.6f}"
            if signature in seen_candidates:
                continue
            seen_candidates.add(signature)
            candidates.append(Location(
                query=place,
                address=address,
                longitude=longitude,
                latitude=latitude,
                adcode=adcode,
            ))

        if not candidates:
            raise AmapError(f"地点缺少有效坐标：{place}")

        if len(candidates) > 1:
            candidate_labels = []
            for candidate in candidates[:5]:
                suffix = (
                    f"（行政区划代码 {candidate.adcode}）"
                    if candidate.adcode
                    else f"（{candidate.coordinates}）"
                )
                candidate_labels.append(f"{candidate.address}{suffix}")
            if len(candidates) > 5:
                candidate_labels.append(f"另有 {len(candidates) - 5} 个结果")
            raise AmapError(
                f"地点“{place}”存在多个匹配结果，请补充省、市或区县后重试："
                + "；".join(candidate_labels)
            )

        return candidates[0]

    def current_weather(self, place: str) -> CurrentWeather:
        location = self.geocode(place)
        if not location.adcode:
            raise AmapError(f"无法确定 {place} 的行政区编码")

        data = self._get(
            "/v3/weather/weatherInfo",
            {"city": location.adcode, "extensions": "base"},
        )
        lives = _as_list(data.get("lives"))
        if not lives:
            raise AmapError(f"没有找到 {place} 的实时天气")

        live = lives[0]
        return CurrentWeather(
            location=location,
            province=str(live.get("province") or ""),
            city=str(live.get("city") or location.address),
            weather=str(live.get("weather") or "未知"),
            temperature=str(live.get("temperature") or "未知"),
            wind_direction=str(live.get("winddirection") or "未知"),
            wind_power=str(live.get("windpower") or "未知"),
            humidity=str(live.get("humidity") or "未知"),
            report_time=str(live.get("reporttime") or "未知"),
        )

    def weather_forecast(self, place: str) -> WeatherForecast:
        location = self.geocode(place)
        if not location.adcode:
            raise AmapError(f"无法确定 {place} 的行政区编码")

        data = self._get(
            "/v3/weather/weatherInfo",
            {"city": location.adcode, "extensions": "all"},
        )
        forecasts = _as_list(data.get("forecasts"))
        if not forecasts:
            raise AmapError(f"没有找到 {place} 的天气预报")

        forecast = forecasts[0]
        days = tuple(
            ForecastDay(
                date=str(cast.get("date") or ""),
                day_weather=str(cast.get("dayweather") or "未知"),
                night_weather=str(cast.get("nightweather") or "未知"),
                day_temperature=str(cast.get("daytemp") or "未知"),
                night_temperature=str(cast.get("nighttemp") or "未知"),
                day_wind=str(cast.get("daywind") or "未知"),
                day_power=str(cast.get("daypower") or "未知"),
            )
            for cast in _as_list(forecast.get("casts"))[:4]
        )
        if not days:
            raise AmapError(f"没有找到 {place} 的逐日天气预报")

        return WeatherForecast(
            location=location,
            city=str(forecast.get("city") or location.address),
            report_time=str(forecast.get("reporttime") or "未知"),
            days=days,
        )

    def driving_route(self, origin: str, destination: str) -> RouteSummary:
        origin_location = self.geocode(origin)
        destination_location = self.geocode(destination)
        data = self._get(
            "/v5/direction/driving",
            {
                "origin": origin_location.coordinates,
                "destination": destination_location.coordinates,
                "strategy": "32",
                "show_fields": "cost,tmcs",
            },
        )

        route = data.get("route") or {}
        paths = _as_list(route.get("paths"))
        if not paths:
            raise AmapError(f"没有找到 {origin} 到 {destination} 的驾车路线")

        path = paths[0]
        cost = path.get("cost") or {}
        traffic_distances: dict[str, int] = {}
        traffic_segments: list[TrafficSegment] = []
        route_cursor = 0
        for step in _as_list(path.get("steps")):
            step_road_name = str(step.get("road_name") or "")
            step_instruction = str(step.get("instruction") or "")
            step_distance = _to_int(
                step.get("step_distance") or step.get("distance")
            )
            tmc_cursor = route_cursor
            tmc_distance_total = 0
            for tmc in _as_list(step.get("tmcs")):
                status = str(tmc.get("tmc_status") or "未知").strip() or "未知"
                distance = _to_int(tmc.get("tmc_distance"))
                traffic_distances[status] = (
                    traffic_distances.get(status, 0) + distance
                )
                start_coordinates, end_coordinates = _polyline_endpoints(
                    tmc.get("tmc_polyline")
                )
                traffic_segments.append(TrafficSegment(
                    status=status,
                    road_name=str(tmc.get("road_name") or step_road_name),
                    instruction=str(
                        tmc.get("instruction") or step_instruction
                    ),
                    distance_meters=distance,
                    route_start_meters=tmc_cursor,
                    route_end_meters=tmc_cursor + distance,
                    start_coordinates=start_coordinates,
                    end_coordinates=end_coordinates,
                ))
                tmc_cursor += distance
                tmc_distance_total += distance
            route_cursor += max(step_distance, tmc_distance_total)

        return RouteSummary(
            origin=origin_location,
            destination=destination_location,
            distance_meters=_to_int(path.get("distance")),
            duration_seconds=_to_int(cost.get("duration")),
            tolls_yuan=str(cost.get("tolls") or "0"),
            traffic_lights=_to_int(cost.get("traffic_lights")),
            traffic_distances=traffic_distances,
            traffic_segments=tuple(traffic_segments),
        )
