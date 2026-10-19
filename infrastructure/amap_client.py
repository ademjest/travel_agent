from __future__ import annotations

import re
import math
import json
import sqlite3
from contextlib import closing
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from dataclasses import dataclass
from typing import Any, Callable

import requests
from core.execution_scope import ensure_execution_active
from core.execution_scope import CURRENT_EXECUTION, ExecutionRevoked
from core.event_idempotency import event_operation_key
from core.model_budget import MODEL_EVENT
from core.tool_execution import retry_read
from core.tool_result import ToolResult


AMAP_BASE_URL = "https://restapi.amap.com"
REQUEST_TIMEOUT = 10


class AmapError(RuntimeError):
    def __init__(self, message, *, code='', retry_after_seconds=None):
        super().__init__(message)
        self.code = code
        self.retry_after_seconds = retry_after_seconds


def amap_failure(action, error):
    transient = error.code in {'network', '429', '500', '502', '503', '504', '10020', '10021'}
    code = ('rate_limit' if error.code in {'429', '10020', '10021'} else
            'temporary_upstream' if transient else
            'invalid_response' if error.code == 'invalid_response' else
            'attempt_limit' if error.code == 'attempt_limit' else
            'data_inconsistent' if error.code == 'route_data_inconsistent' else
            'permission_denied' if error.code in {'401', '403', '10001', '10009'} else 'not_found')
    return ToolResult('failed', action, data=f'工具错误：{error}', error_code=code,
                      retryable=transient and getattr(error, 'attempts', 0) < 3,
                      retry_after_seconds=error.retry_after_seconds)


def retry_after_seconds(value):
    if not isinstance(value, str):
        return None
    if value.strip().isdigit():
        return int(value.strip())
    try:
        instant = parsedate_to_datetime(value)
        if instant.tzinfo is None:
            return None
        return max(0, math.ceil((instant - datetime.now(timezone.utc)).total_seconds()))
    except (TypeError, ValueError, OverflowError):
        return None


class PlaceResolutionError(AmapError):
    def __init__(self,message,*,code='location_not_found',field=''):
        super().__init__(message,code=code)
        self.field=field


@dataclass(frozen=True)
class Location:
    query: str
    address: str
    longitude: float
    latitude: float
    adcode: str
    poi_id: str = ''
    name: str = ''
    city: str = ''
    level: str = ''
    source: str = ''

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
            http_get: Callable[..., Any] = requests.get, *, sleeper=None, max_attempts=3):
        if not api_key:
            raise ValueError("AMAP_API_KEY is required")
        self.api_key = api_key
        self.http_get = http_get
        if max_attempts not in (1, 2, 3):
            raise ValueError('高德读取最多允许三次尝试')
        self.max_attempts = max_attempts
        self.sleeper = sleeper if sleeper is not None else time.sleep
        self.diagnostics = None
        self.checkpoint_store = None
        self._place_lock = threading.Lock()
        self._next_place_request_at = 0.0
        self._route_lock = threading.Lock()
        self._next_route_request_at = 0.0

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        ensure_execution_active()
        checkpoint = self._checkpoint(path, params)
        if checkpoint:
            cached = self.checkpoint_store.get_event_tool_result(checkpoint[1])
            if cached is not None:
                ensure_execution_active()
                if self.diagnostics is not None:
                    self.diagnostics('tool', {'name': path, 'status': 'checkpoint_reused', 'attempts': 0})
                return json.loads(cached)
        value, error = None, None

        def operation():
            nonlocal value, error
            try:
                if checkpoint:
                    self._reserve_http(checkpoint)
                value = self._get_once(path, params)
                return ToolResult('completed', path)
            except AmapError as caught:
                error = caught
                return amap_failure(path, caught)

        result = retry_read(operation, sleeper=self.sleeper, max_attempts=self.max_attempts)
        if self.diagnostics is not None:
            self.diagnostics('tool', {'name': path, 'status': result.status,
                'error_code': result.error_code, 'attempts': result.attempts})
        if result.status == 'failed':
            error.attempts = result.attempts
            raise error
        if checkpoint:
            self.checkpoint_store.save_event_tool_result(checkpoint[1], checkpoint[0], 'amap_http', {},
                                                        json.dumps(value, ensure_ascii=False))
        return value

    def _checkpoint(self, path, params):
        if self.checkpoint_store is None:
            return None
        event = MODEL_EVENT.get()
        scope = CURRENT_EXECUTION.get()
        with self.checkpoint_store._connect() as db:
            if not event and scope:
                row = db.execute('SELECT event_key FROM inbox_jobs WHERE id=?', (scope.job_id,)).fetchone()
                event = row['event_key'] if row else ''
            if not event or not db.execute('SELECT 1 FROM processed_events WHERE event_id=?', (event,)).fetchone():
                return None
        return event, event_operation_key(event, 'amap-http-v1:' + path, params)

    def _reserve_http(self, checkpoint):
        with self.checkpoint_store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT attempts FROM http_read_attempts WHERE operation_key=?', (checkpoint[1],)).fetchone()
            if row and row['attempts'] >= 3:
                raise AmapError('本次数据源请求已达到尝试上限，请重新发起查询。', code='attempt_limit')
            db.execute('INSERT INTO http_read_attempts VALUES (?,?,1,?) '
                       'ON CONFLICT(operation_key) DO UPDATE SET attempts=attempts+1',
                       (checkpoint[1], checkpoint[0], datetime.now(timezone.utc).isoformat()))

    def _get_once(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        ensure_execution_active()
        timeout = REQUEST_TIMEOUT
        scope = CURRENT_EXECUTION.get()
        if scope is not None:
            with closing(sqlite3.connect(scope.database_path)) as db:
                row = db.execute('SELECT deadline_at FROM inbox_jobs WHERE id=?', (scope.job_id,)).fetchone()
            if row is None:
                raise ExecutionRevoked('任务已取消或删除，停止读取外部数据。')
            timeout = min(timeout, max(0.01, (datetime.fromisoformat(row[0]) - datetime.now(timezone.utc)).total_seconds()))
        query = {**params, "key": self.api_key, "output": "JSON"}
        try:
            response = self.http_get(
                f"{AMAP_BASE_URL}{path}",
                params=query,
                timeout=timeout,
            )
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as exc:
            status = getattr(getattr(exc,'response',None),'status_code',None)
            code = str(status) if status else 'network'
            headers = getattr(getattr(exc, 'response', None), 'headers', {}) or {}
            raise AmapError(
                f"高德接口请求失败：{type(exc).__name__}", code=code,
                retry_after_seconds=retry_after_seconds(headers.get('Retry-After'))
            ) from exc
        except ValueError as exc:
            raise AmapError("高德接口返回了无法解析的数据", code='invalid_response') from exc

        if not isinstance(data, dict):
            raise AmapError('高德接口返回结构无效', code='invalid_response')
        if str(data.get("status")) != "1":
            info = data.get("info") or "未知错误"
            infocode = data.get("infocode") or ""
            raise AmapError(f"高德接口返回错误：{info} {infocode}".strip(), code=str(infocode))

        return data

    def geocode(self, place: str, *, city: str = '') -> Location:
        data = self._get("/v3/geocode/geo", {"address": place, **({'city':city} if city else {})})
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
                city=result.get('city','') if isinstance(result.get('city'),str) else '',
                level=str(result.get('level') or ''),source='geocode',
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
        if not isinstance(live, dict) or any(live.get(key) in (None, '') for key in ('weather', 'temperature', 'reporttime')):
            raise AmapError('实时天气缺少必要字段', code='invalid_response')
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

    def search_places(self, city: str, keywords: str, *, offset: int = 5, page: int = 1,
                      extensions: str = 'base', types: str = '') -> tuple[dict[str, Any], ...]:
        if not 1 <= offset <= 25 or not 1 <= page <= 100 or extensions not in {'base', 'all'}:
            raise ValueError('Invalid place search pagination or extensions')
        for attempt in range(3):
            with self._place_lock:
                delay = max(0.0, self._next_place_request_at - time.monotonic())
                if delay:
                    time.sleep(delay)
                self._next_place_request_at = time.monotonic() + 1.05
                try:
                    params = {'keywords': keywords, 'citylimit': 'true' if city else 'false',
                              'offset': offset, 'page': page, 'extensions': extensions}
                    if city:
                        params['city']=city
                    if types:
                        params['types'] = types
                    data = self._get('/v3/place/text', params)
                except AmapError as exc:
                    if exc.code != '10021' or attempt == 2:
                        raise
                    self._next_place_request_at = time.monotonic() + (attempt + 1) * 1.2
                    continue
            break
        results = []
        for poi in _as_list(data.get('pois'))[:offset]:
            result = {key: value for key in ('id', 'name', 'address', 'location', 'type', 'tel',
                                                    'adname', 'business_area', 'pname', 'cityname','adcode','typecode')
                      if isinstance((value := poi.get(key)), str)}
            business = poi.get('biz_ext')
            if isinstance(business, dict):
                for source, target in (('rating','rating'), ('cost','cost'), ('lowest_price','lowest_price')):
                    value = business.get(source)
                    if isinstance(value, (str, int, float)) and str(value).strip():
                        result[target] = str(value).strip()
            if result.get('name'):
                results.append(result)
        return tuple(results)

    def non_driving_route(self, origin: str, destination: str, *, mode: str, city: str = '') -> dict:
        if mode not in {'walking', 'transit'}:
            raise ValueError('Unsupported route mode')
        origin_location, destination_location = self.resolve_route_endpoints(origin,destination,city=city)
        return self.non_driving_route_locations(origin_location, destination_location, mode=mode, city=city)

    def search_place_tips(self, city: str, keywords: str) -> tuple[dict[str, Any], ...]:
        # Gate POIs may exist in inputtips but be absent from place/text and place/detail.
        with self._place_lock:
            delay=max(0.0,self._next_place_request_at-time.monotonic())
            if delay:
                time.sleep(delay)
            self._next_place_request_at=time.monotonic()+1.05
            data=self._get('/v3/assistant/inputtips',{'keywords':keywords,'city':city,'citylimit':'true'})
        results=[]
        for tip in _as_list(data.get('tips'))[:10]:
            if not all(isinstance(tip.get(key),str) and tip[key] for key in ('id','name','location','adcode','district')):
                continue
            region=re.fullmatch(r'(?:(.+?(?:省|自治区)))?(.+?市)(.*)',tip['district'])
            if not region or str(tip.get('typecode',''))!='991400':
                continue
            results.append({key:tip[key] for key in ('id','name','location','adcode')}
                          | {'address':tip.get('address','') if isinstance(tip.get('address'),str) else '',
                             'pname':region[1] or '', 'cityname':region[2],'adname':region[3],
                             'type':'通行设施;门','typecode':'991400'})
        return tuple(results)

    def non_driving_route_locations(self, origin_location: Location, destination_location: Location, *, mode: str, city: str = '') -> dict:
        params = {'origin': origin_location.coordinates, 'destination': destination_location.coordinates}
        if mode == 'transit':
            if not city:
                raise AmapError('公共交通查询需要明确城市')
            params.update(city=city, cityd=city, strategy=0, extensions='base')
        path = '/v3/direction/walking' if mode == 'walking' else '/v3/direction/transit/integrated'
        data = self._get(path, params)
        route = data.get('route') or {}
        if not isinstance(route, dict):
            raise AmapError('路线返回结构无效', code='invalid_response')
        options = _as_list(route.get('paths' if mode == 'walking' else 'transits'))
        if not options:
            raise AmapError('没有找到对应交通方式的路线，请核对起终点与城市')
        option = options[0]
        if (not isinstance(option, dict) or _to_int(option.get('distance') or route.get('distance')) <= 0
                or _to_int(option.get('duration')) <= 0):
            raise AmapError('路线缺少有效距离或耗时', code='invalid_response')
        instructions = []
        if mode == 'walking':
            instructions = [str(step.get('instruction') or '') for step in _as_list(option.get('steps'))[:12]]
        else:
            for segment in _as_list(option.get('segments'))[:10]:
                walking = segment.get('walking') or {}
                distance = _to_int(walking.get('distance'))
                if distance:
                    instructions.append(f'步行约 {distance} 米')
                for line in _as_list((segment.get('bus') or {}).get('buslines'))[:1]:
                    departure = (line.get('departure_stop') or {}).get('name', '')
                    arrival = (line.get('arrival_stop') or {}).get('name', '')
                    instructions.append(f"{line.get('name', '')}：{departure} → {arrival}")
        return {'mode': mode, 'origin': origin_location.address, 'destination': destination_location.address,
                'distance_meters': _to_int(option.get('distance') or route.get('distance')),
                'duration_seconds': _to_int(option.get('duration')), 'instructions': tuple(filter(None, instructions))}

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
        if not isinstance(forecast, dict):
            raise AmapError('天气预报返回结构无效', code='invalid_response')
        casts = _as_list(forecast.get('casts'))[:4]
        if any(not isinstance(cast, dict) or any(cast.get(key) in (None, '')
                for key in ('date', 'dayweather', 'nightweather', 'daytemp', 'nighttemp')) for cast in casts):
            raise AmapError('天气预报缺少必要字段', code='invalid_response')
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
            for cast in casts
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
        origin_location,destination_location = self.resolve_route_endpoints(origin,destination)
        return self.driving_route_locations(origin_location, destination_location)

    def resolve_route_endpoints(self,origin,destination,*,city=''):
        from infrastructure.place_resolution import resolve_route_location
        locations=[]
        for field,query in (('origin',origin),('destination',destination)):
            try:
                locations.append(resolve_route_location(self,query,city))
            except PlaceResolutionError as exc:
                exc.field=field
                raise
        return tuple(locations)

    def driving_route_locations(self, origin_location: Location, destination_location: Location) -> RouteSummary:
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
        if not isinstance(route, dict):
            raise AmapError('路线返回结构无效', code='invalid_response')
        paths = _as_list(route.get("paths"))
        if not paths:
            raise AmapError(f"没有找到 {origin_location.query} 到 {destination_location.query} 的驾车路线")

        path = paths[0]
        if (not isinstance(path, dict) or not isinstance(path.get('cost'), dict)
                or _to_int(path.get('distance')) <= 0 or _to_int(path['cost'].get('duration')) <= 0):
            raise AmapError('驾车路线缺少有效距离或耗时', code='invalid_response')
        if origin_location.city and origin_location.city==destination_location.city:
            longitude_delta=math.radians(destination_location.longitude-origin_location.longitude)
            latitude_delta=math.radians(destination_location.latitude-origin_location.latitude)
            a=math.sin(latitude_delta/2)**2+math.cos(math.radians(origin_location.latitude))*math.cos(math.radians(destination_location.latitude))*math.sin(longitude_delta/2)**2
            straight=6371000*2*math.asin(min(1,math.sqrt(a)))
            distance=_to_int(path.get('distance'))
            if straight<100000 and distance>max(500000,straight*20):
                raise AmapError('已核验的同城起终点与接口返回的超长路线明显不一致，暂不提供该距离/耗时，请核对地图导航。',code='route_data_inconsistent')
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

    def route_for_pois(self, origin: dict, destination: dict, *, mode: str, city: str) -> dict:
        if mode not in {'walking', 'transit', 'driving'}:
            raise ValueError('Unsupported route mode')
        locations = []
        for place in (origin, destination):
            try:
                longitude, latitude = _parse_coordinate_pair(place.get('location'))
                if not -180 <= longitude <= 180 or not -90 <= latitude <= 90:
                    raise ValueError('invalid coordinate')
            except (ValueError, TypeError):
                raise AmapError(f"地点 {place.get('name', '')} 缺少有效坐标，不能可靠查询交通段。") from None
            locations.append(Location(place['name'], place['name'], longitude, latitude, ''))
        for attempt in range(3):
            with self._route_lock:
                delay = max(0.0, self._next_route_request_at - time.monotonic())
                if delay:
                    time.sleep(delay)
                self._next_route_request_at = time.monotonic() + 1.05
                try:
                    if mode == 'driving':
                        result = self.driving_route_locations(*locations)
                        return {'mode': mode, 'origin': origin['name'], 'destination': destination['name'],
                                'distance_meters': result.distance_meters, 'duration_seconds': result.duration_seconds,
                                'instructions': ('高德推荐驾车路线',)}
                    return self.non_driving_route_locations(*locations, mode=mode, city=city)
                except AmapError as exc:
                    if exc.code != '10021' or attempt == 2:
                        raise
                    self._next_route_request_at = time.monotonic() + (attempt + 1) * 1.2
